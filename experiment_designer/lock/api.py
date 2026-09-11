"""The one HTTP seam for the CLI paths. The MCP server has its own (_api_request)."""

from __future__ import annotations

from typing import Any

import requests


class ApiError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, detail: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail


class ApiSession:
    def __init__(self, base_url: str, email: str, password: str, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._email = email
        self._password = password
        self._timeout = timeout
        self._token: str | None = None

    def _headers(self) -> dict[str, str]:
        if self._token is None:
            r = requests.post(
                f"{self.base_url}/login",
                json={"email": self._email, "password": self._password},
                headers={"Accept": "application/json"},
                timeout=self._timeout,
            )
            self._raise_for(r, "login")
            self._token = r.json().get("access_token")
            if not self._token:
                raise ApiError("login: no access_token in response")
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}

    @staticmethod
    def _raise_for(r: requests.Response, what: str) -> None:
        if 200 <= r.status_code < 300:
            return
        try:
            detail = r.json().get("message") or r.text[:500]
        except ValueError:
            detail = r.text[:500]
        raise ApiError(f"{what}: HTTP {r.status_code}", status=r.status_code, detail=str(detail))

    @staticmethod
    def _unwrap(r: requests.Response) -> Any:
        body = r.json() if r.content else {}
        return body.get("data", body) if isinstance(body, dict) else body

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        r = requests.get(
            f"{self.base_url}/{path}", headers=self._headers(), params=params, timeout=self._timeout
        )
        self._raise_for(r, f"GET {path}")
        return self._unwrap(r)

    def post(self, path: str, json: dict[str, Any]) -> Any:
        r = requests.post(
            f"{self.base_url}/{path}", headers=self._headers(), json=json, timeout=self._timeout
        )
        self._raise_for(r, f"POST {path}")
        return self._unwrap(r)

    def put(self, path: str, json: dict[str, Any]) -> Any:
        r = requests.put(
            f"{self.base_url}/{path}", headers=self._headers(), json=json, timeout=self._timeout
        )
        self._raise_for(r, f"PUT {path}")
        return self._unwrap(r)

    def get_bytes(self, path: str, params: dict[str, Any] | None = None) -> bytes:
        r = requests.get(
            f"{self.base_url}/{path}",
            headers=self._headers(),
            params=params,
            timeout=max(self._timeout, 300.0),
        )
        self._raise_for(r, f"GET {path}")
        return r.content

    def post_file(
        self, path: str, field: str, filename: str, content: str, content_type: str
    ) -> Any:
        r = requests.post(
            f"{self.base_url}/{path}",
            headers=self._headers(),
            files={field: (filename, content, content_type)},
            timeout=max(self._timeout, 300.0),
        )
        self._raise_for(r, f"POST {path}")
        return self._unwrap(r)
