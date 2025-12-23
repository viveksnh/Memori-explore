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

from dataclasses import dataclass

import requests


@dataclass(frozen=True)
class TokenFlowCreateResponse:
    token_flow_id: str
    wait_secret: str | None
    web_url: str | None
    code: str | None


class TokenFlowClient:
    def __init__(self, base_url: str, timeout_seconds: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._session = requests.Session()

    def create(
        self,
        *,
        localhost_port: int,
        next_url: str | None = None,
        utm_source: str | None = None,
    ) -> TokenFlowCreateResponse:
        payload: dict[str, object] = {"localhost_port": localhost_port}
        if next_url:
            payload["next_url"] = next_url
        if utm_source:
            payload["utm_source"] = utm_source

        response = self._session.post(
            f"{self.base_url}/v1/token-flow/create",
            json=payload,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()

        return TokenFlowCreateResponse(
            token_flow_id=data.get("token_flow_id") or data.get("flow_id"),
            wait_secret=data.get("wait_secret"),
            web_url=data.get("web_url"),
            code=data.get("code"),
        )

    def wait(
        self,
        *,
        token_flow_id: str,
        wait_secret: str | None,
        timeout_seconds: float,
    ) -> dict:
        payload: dict[str, object] = {
            "token_flow_id": token_flow_id,
            "timeout": timeout_seconds,
        }
        if wait_secret:
            payload["wait_secret"] = wait_secret

        response = self._session.post(
            f"{self.base_url}/v1/token-flow/wait",
            json=payload,
            timeout=timeout_seconds + 5,
        )
        response.raise_for_status()
        return response.json()
