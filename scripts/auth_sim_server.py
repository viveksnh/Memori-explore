#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse


LOGIN_PAGE_PATH = Path(__file__).with_name("auth_sim_login.html")
try:
    LOGIN_PAGE_HTML = LOGIN_PAGE_PATH.read_text(encoding="utf-8")
except OSError:
    LOGIN_PAGE_HTML = (
        "<!doctype html><html><body>Missing auth_sim_login.html</body></html>"
    )


def _generate_code(length: int = 6) -> str:
    digits = "0123456789"
    return "".join(secrets.choice(digits) for _ in range(length))


@dataclass
class TokenFlow:
    token_flow_id: str
    wait_secret: str
    code: str
    localhost_port: int | None
    next_url: str | None
    utm_source: str | None
    api_key: str
    email: str
    created_at: float = field(default_factory=time.time)
    active: bool = False
    event: threading.Event = field(default_factory=threading.Event)


class TokenFlowStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._flows: dict[str, TokenFlow] = {}
        self._last_id: str | None = None

    def create(
        self,
        *,
        localhost_port: int | None,
        next_url: str | None,
        utm_source: str | None,
    ) -> TokenFlow:
        token_flow_id = uuid.uuid4().hex
        flow = TokenFlow(
            token_flow_id=token_flow_id,
            wait_secret=uuid.uuid4().hex,
            code=_generate_code(),
            localhost_port=localhost_port,
            next_url=next_url,
            utm_source=utm_source,
            api_key=f"memori_test_{token_flow_id[:12]}",
            email="smoke@test.local",
        )
        with self._lock:
            self._flows[token_flow_id] = flow
            self._last_id = token_flow_id
        return flow

    def get(self, token_flow_id: str) -> TokenFlow | None:
        with self._lock:
            return self._flows.get(token_flow_id)

    def latest(self) -> TokenFlow | None:
        with self._lock:
            if not self._last_id:
                return None
            return self._flows.get(self._last_id)

    def activate(self, token_flow_id: str, *, api_key: str | None, email: str | None) -> bool:
        with self._lock:
            flow = self._flows.get(token_flow_id)
            if not flow:
                return False
            if api_key:
                flow.api_key = api_key
            if email:
                flow.email = email
            flow.active = True
            flow.event.set()
            return True

    def snapshot(self, flow: TokenFlow) -> dict:
        return {
            "token_flow_id": flow.token_flow_id,
            "active": flow.active,
            "localhost_port": flow.localhost_port,
            "next_url": flow.next_url,
            "utm_source": flow.utm_source,
            "created_at": flow.created_at,
            "email": flow.email,
        }


class AuthSimHandler(BaseHTTPRequestHandler):
    server: "AuthSimServer"

    def _send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, status: int, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid JSON payload") from exc

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/login", "/login/"):
            self._send_html(200, LOGIN_PAGE_HTML)
            return

        if parsed.path == "/health":
            self._send_json(200, {"ok": True})
            return

        if parsed.path == "/v1/token-flow/latest":
            flow = self.server.store.latest()
            if not flow:
                self._send_json(200, {"token_flow_id": None, "active": False})
                return
            self._send_json(200, self.server.store.snapshot(flow))
            return

        if parsed.path == "/v1/token-flow/info":
            params = parse_qs(parsed.query)
            token_flow_id = params.get("token_flow_id", [None])[0]
            if not token_flow_id:
                self._send_json(200, {"token_flow_id": None})
                return
            flow = self.server.store.get(token_flow_id)
            if not flow:
                self._send_json(404, {"error": "token_flow_id not found"})
                return
            self._send_json(200, self.server.store.snapshot(flow))
            return

        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return

        if parsed.path == "/v1/token-flow/create":
            localhost_port = payload.get("localhost_port")
            try:
                localhost_port = int(localhost_port) if localhost_port is not None else None
            except (TypeError, ValueError):
                localhost_port = None

            flow = self.server.store.create(
                localhost_port=localhost_port,
                next_url=payload.get("next_url"),
                utm_source=payload.get("utm_source"),
            )

            params = {"token_flow_id": flow.token_flow_id, "code": flow.code}
            if flow.localhost_port is not None:
                params["localhost_port"] = flow.localhost_port
            web_url = f"{self.server.public_base_url}/login?{urlencode(params)}"

            print(f"AUTH_SIM_CREATE token_flow_id={flow.token_flow_id}", flush=True)
            self._send_json(
                200,
                {
                    "token_flow_id": flow.token_flow_id,
                    "wait_secret": flow.wait_secret,
                    "web_url": web_url,
                    "code": flow.code,
                },
            )
            return

        if parsed.path == "/v1/token-flow/wait":
            token_flow_id = payload.get("token_flow_id")
            if not token_flow_id:
                self._send_json(400, {"error": "token_flow_id required"})
                return

            flow = self.server.store.get(token_flow_id)
            if not flow:
                self._send_json(404, {"error": "token_flow_id not found"})
                return

            wait_secret = payload.get("wait_secret")
            if wait_secret and wait_secret != flow.wait_secret:
                self._send_json(403, {"error": "wait_secret invalid"})
                return

            timeout = payload.get("timeout", 0)
            try:
                timeout = float(timeout)
            except (TypeError, ValueError):
                timeout = 0

            if not flow.active and timeout > 0:
                flow.event.wait(timeout)

            if flow.active:
                self._send_json(
                    200,
                    {
                        "timeout": False,
                        "api_key": flow.api_key,
                        "email": flow.email,
                        "token_flow_id": flow.token_flow_id,
                    },
                )
            else:
                self._send_json(200, {"timeout": True})
            return

        if parsed.path == "/v1/token-flow/activate":
            token_flow_id = payload.get("token_flow_id")
            if not token_flow_id:
                params = parse_qs(parsed.query)
                token_flow_id = params.get("token_flow_id", [None])[0]
            if not token_flow_id:
                self._send_json(400, {"error": "token_flow_id required"})
                return

            ok = self.server.store.activate(
                token_flow_id,
                api_key=payload.get("api_key"),
                email=payload.get("email"),
            )
            if not ok:
                self._send_json(404, {"error": "token_flow_id not found"})
                return

            print(f"AUTH_SIM_ACTIVATE token_flow_id={token_flow_id}", flush=True)
            self._send_json(200, {"token_flow_id": token_flow_id, "active": True})
            return

        self._send_json(404, {"error": "not found"})

    def log_message(self, _format: str, *_args) -> None:
        return


class AuthSimServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler]):
        super().__init__(address, handler)
        self.store = TokenFlowStore()
        host, port = self.server_address
        host_for_links = "127.0.0.1" if host == "0.0.0.0" else host
        self.public_base_url = f"http://{host_for_links}:{port}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Memori auth flow simulator")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=0, help="Bind port (0 for random)")
    args = parser.parse_args()

    server = AuthSimServer((args.host, args.port), AuthSimHandler)
    port = server.server_address[1]
    server.public_base_url = f"http://{args.host if args.host != '0.0.0.0' else '127.0.0.1'}:{port}"

    print(f"AUTH_SIM_PORT={port}", flush=True)
    print(f"AUTH_SIM_URL={server.public_base_url}", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
