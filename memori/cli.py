r"""
 __  __                           _
|  \/  | ___ _ __ ___   ___  _ __(_)
| |\/| |/ _ \ '_ ` _ \ / _ \| '__| |
| |  | |  __/ | | | | | (_) | |  | |
|_|  |_|\___|_| |_| |_|\___/|_|  |_|
                  perfectam memoriam
                       memorilabs.ai
"""

from __future__ import annotations

import argparse
import itertools
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from memori._auth import delete_api_key, get_account_email, resolve_api_key, save_api_key
from memori._cli import Cli as LegacyCli
from memori._config import Config
from memori._setup import Manager as SetupManager
from memori._token_flow import TokenFlowClient
from memori.api._quota import Manager as ApiQuotaManager
from memori.api._sign_up import Manager as ApiSignUpManager
from memori.storage.cockroachdb._cluster_manager import (
    ClusterManager as CockroachDBClusterManager,
)


DEFAULT_AUTH_BASE = os.environ.get(
    "MEMORI_AUTH_BASE",
    os.environ.get("MEMORI_API_URL_BASE", "https://api.memorilabs.ai"),
)
DEFAULT_LOGIN_URL = os.environ.get("MEMORI_LOGIN_URL", "https://memorilabs.ai/login")


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


DEFAULT_CALLBACK_PORT = _env_int("MEMORI_LOGIN_PORT", 54321)
DEFAULT_WAIT_TIMEOUT_SECONDS = _env_int("MEMORI_LOGIN_WAIT_TIMEOUT", 40)
DEFAULT_NEXT_URL = os.environ.get("MEMORI_LOGIN_NEXT_URL", "/home")


class _LoginServer(HTTPServer):
    allow_reuse_address = True


def _print_banner(console: Console, config: Config) -> None:
    try:
        import pyfiglet

        banner = pyfiglet.figlet_format("Memori", font="standard").rstrip()
        console.print(Text(banner, style="bold cyan"))
    except Exception:
        console.print(Text("Memori", style="bold cyan"))

    console.print(Text("perfectam memoriam", style="dim"))
    console.print(Text("memorilabs.ai", style="dim"))
    console.print(Text(f"v{config.version}", style="dim"))


def _build_login_url(base_url: str, token_flow_id: str, code: str | None) -> str:
    parts = urlsplit(base_url)
    query = dict(parse_qsl(parts.query))
    query["token_flow_id"] = token_flow_id
    if code:
        query["code"] = code
    query["source"] = "cli"
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def _make_token_flow_handler(state: dict):
    class TokenFlowHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            token_flow_id = state.get("token_flow_id")
            if not token_flow_id:
                self.send_response(503)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b"pending")
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(str(token_flow_id).encode("utf-8"))

        def log_message(self, _format, *_args):
            return

    return TokenFlowHandler


def _start_token_flow_server(port: int):
    state: dict[str, str | None] = {"token_flow_id": None}
    handler = _make_token_flow_handler(state)

    try:
        server = _LoginServer(("127.0.0.1", port), handler)
    except OSError:
        server = _LoginServer(("127.0.0.1", 0), handler)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, state


def _config_with_auth() -> Config:
    config = Config()
    config.api_key = resolve_api_key()[0]
    return config


def _cmd_login(args) -> int:
    console = Console()
    config = Config()
    _print_banner(console, config)

    server, thread, state = _start_token_flow_server(args.port)
    port = server.server_address[1]

    if port != args.port:
        console.print(
            Text(
                f"Port {args.port} was unavailable. Using port {port}.",
                style="dim",
            )
        )

    try:
        flow_client = TokenFlowClient(args.auth_base, timeout_seconds=10)
        create_response = flow_client.create(
            localhost_port=port,
            next_url=args.next_url,
            utm_source="cli",
        )
    except Exception as exc:
        server.shutdown()
        server.server_close()
        console.print(Text(f"Failed to start login flow: {exc}", style="red"))
        return 1

    if not create_response.token_flow_id:
        server.shutdown()
        server.server_close()
        console.print(Text("Login flow did not return a token id.", style="red"))
        return 1

    state["token_flow_id"] = create_response.token_flow_id

    web_url = create_response.web_url or _build_login_url(
        args.login_url,
        create_response.token_flow_id,
        create_response.code,
    )

    console.print()
    console.print(Panel(Text("Opening your browser to sign in...", style="bold cyan")))

    if webbrowser.open(web_url):
        console.print(
            "The web browser should have opened for you to authenticate.\n"
            "If it didn't, please copy this URL into your web browser manually:\n"
        )
    else:
        console.print(
            Text(
                "Was not able to launch web browser.\n"
                "Please go to this URL manually and complete the flow:\n",
                style="yellow",
            )
        )

    console.print(Text(web_url, style="cyan"))
    if create_response.code:
        console.print(Text(f"Enter this code: {create_response.code}", style="yellow"))

    console.print()

    wait_payload = {}
    try:
        with console.status(
            "[bold cyan]Waiting for token flow to complete...[/bold cyan]",
            spinner="dots",
        ) as status:
            for attempt in itertools.count(1):
                wait_payload = flow_client.wait(
                    token_flow_id=create_response.token_flow_id,
                    wait_secret=create_response.wait_secret,
                    timeout_seconds=args.timeout,
                )
                if not wait_payload.get("timeout"):
                    break
                status.update(
                    f"[bold cyan]Waiting for token flow to complete... (attempt {attempt + 1})[/bold cyan]"
                )
    except KeyboardInterrupt:
        server.shutdown()
        server.server_close()
        console.print(Text("Login cancelled.", style="red"))
        return 1
    except Exception as exc:
        server.shutdown()
        server.server_close()
        console.print(Text(f"Login failed: {exc}", style="red"))
        return 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)

    api_key = (
        wait_payload.get("api_key")
        or wait_payload.get("token")
        or wait_payload.get("token_secret")
    )
    email = wait_payload.get("email")

    if not api_key:
        console.print(Text("No API key returned from login.", style="red"))
        return 1

    try:
        save_api_key(api_key, email)
    except Exception as exc:
        console.print(Text(f"Failed to save API key: {exc}", style="red"))
        console.print(Text("You can set MEMORI_API_KEY manually.", style="yellow"))
        return 1

    email = email or get_account_email()
    if email:
        console.print(Text(f"✅ Authenticated as {email}. Welcome to Memori!"))
    else:
        console.print(Text("✅ Authenticated. Welcome to Memori!"))

    return 0


def _cmd_status(_args) -> int:
    console = Console()
    api_key, source = resolve_api_key()
    email = get_account_email()

    if not api_key:
        console.print(Text("⚠️  Not logged in. Run `memori login` to get started."))
        return 1

    if email:
        console.print(Text(f"✅ Authenticated as {email}."))
    else:
        console.print(Text("✅ Authenticated."))

    if source == "env":
        console.print(Text("Using MEMORI_API_KEY from your environment.", style="dim"))
    else:
        console.print(Text("Using credentials stored in your system keychain.", style="dim"))

    return 0


def _cmd_logout(_args) -> int:
    console = Console()
    env_key = os.environ.get("MEMORI_API_KEY")

    try:
        delete_api_key()
        console.print(Text("✅ Logged out of Memori.", style="green"))
    except Exception as exc:
        console.print(Text(f"Failed to clear keychain credentials: {exc}", style="red"))

    if env_key:
        console.print(
            Text(
                "MEMORI_API_KEY is still set in your environment. Unset it to fully log out.",
                style="yellow",
            )
        )

    return 0


def _cmd_quota(_args) -> int:
    ApiQuotaManager(_config_with_auth()).execute()
    return 0


def _cmd_setup(_args) -> int:
    SetupManager(_config_with_auth()).execute()
    return 0


def _cmd_sign_up(args) -> int:
    ApiSignUpManager(_config_with_auth()).execute(email=args.email)
    return 0


def _cmd_cockroachdb_cluster(args) -> int:
    config = _config_with_auth()
    cli = LegacyCli(config)
    manager = CockroachDBClusterManager(config)

    if args.action == "start":
        manager.start(cli)
    elif args.action == "claim":
        manager.claim(cli)
    elif args.action == "delete":
        manager.delete(cli)
    return 0


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="memori",
        description="Memori CLI",
    )
    subparsers = parser.add_subparsers(dest="command")

    login_parser = subparsers.add_parser("login", help="Authenticate via browser")
    login_parser.add_argument(
        "--auth-base",
        default=DEFAULT_AUTH_BASE,
        help="Token flow service base URL",
    )
    login_parser.add_argument(
        "--login-url",
        default=DEFAULT_LOGIN_URL,
        help="Login URL to open in the browser",
    )
    login_parser.add_argument(
        "--next-url",
        default=DEFAULT_NEXT_URL,
        help="URL path to open after authentication",
    )
    login_parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_CALLBACK_PORT,
        help="Local loopback port",
    )
    login_parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_WAIT_TIMEOUT_SECONDS,
        help="Seconds to wait per auth polling request",
    )
    login_parser.set_defaults(func=_cmd_login)

    status_parser = subparsers.add_parser("status", help="Check auth status")
    status_parser.set_defaults(func=_cmd_status)

    logout_parser = subparsers.add_parser("logout", help="Remove stored credentials")
    logout_parser.set_defaults(func=_cmd_logout)

    quota_parser = subparsers.add_parser("quota", help="Check your quota")
    quota_parser.set_defaults(func=_cmd_quota)

    setup_parser = subparsers.add_parser("setup", help="Run suggested setup steps")
    setup_parser.set_defaults(func=_cmd_setup)

    signup_parser = subparsers.add_parser(
        "sign-up",
        help="Sign up for an API key via email",
        aliases=["signup"],
    )
    signup_parser.add_argument("email", help="Email address for sign-up")
    signup_parser.set_defaults(func=_cmd_sign_up)

    cockroach_parser = subparsers.add_parser(
        "cockroachdb",
        help="Manage CockroachDB clusters",
    )
    cockroach_subparsers = cockroach_parser.add_subparsers(dest="cockroach_cmd")
    cluster_parser = cockroach_subparsers.add_parser(
        "cluster", help="Manage a CockroachDB cluster"
    )
    cluster_parser.add_argument(
        "action", choices=["start", "claim", "delete"], help="Cluster action"
    )
    cluster_parser.set_defaults(func=_cmd_cockroachdb_cluster)

    return parser, cockroach_parser


def main(argv: list[str] | None = None) -> int:
    parser, cockroach_parser = _build_parser()
    args = parser.parse_args(argv)

    if not hasattr(args, "func"):
        if args.command == "cockroachdb":
            cockroach_parser.print_help()
            return 1
        parser.print_help()
        return 1

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
