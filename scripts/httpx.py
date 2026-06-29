"""Minimal httpx-compatible shim backed by urllib for plugin runtime startup."""

from __future__ import annotations

from dataclasses import dataclass
import json
from http.cookiejar import CookieJar
from typing import Any
from urllib import error, parse, request


class HTTPError(Exception):
    pass


@dataclass
class Response:
    status_code: int
    _body: bytes
    headers: dict[str, str]
    url: str

    @property
    def text(self) -> str:
        return self._body.decode("utf-8", errors="replace")

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> Any:
        return json.loads(self.text)


class Client:
    def __init__(
        self,
        *,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        follow_redirects: bool = True,
    ) -> None:
        self._default_headers = headers or {}
        self._timeout = timeout
        self._follow_redirects = follow_redirects
        self._cookie_jar = CookieJar()
        self._opener = request.build_opener(request.HTTPCookieProcessor(self._cookie_jar))

    def __enter__(self) -> Client:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        return None

    def get(self, url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> Response:
        return self.request("GET", url, params=params, headers=headers)

    def post(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        return self.request("POST", url, params=params, json=json, data=data, headers=headers)

    def patch(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        return self.request("PATCH", url, params=params, json=json, headers=headers)

    def delete(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        return self.request("DELETE", url, params=params, headers=headers)

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        final_url = _with_query(url, params)
        final_headers = {**self._default_headers, **(headers or {})}
        body: bytes | None = None
        if json is not None:
            body = _json_body(json)
            final_headers.setdefault("Content-Type", "application/json")
        elif data is not None:
            body = parse.urlencode({k: "" if v is None else str(v) for k, v in data.items()}).encode("utf-8")
            final_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

        req = request.Request(final_url, data=body, headers=final_headers, method=method.upper())
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                payload = resp.read()
                return Response(resp.getcode(), payload, dict(resp.headers.items()), final_url)
        except error.HTTPError as exc:
            payload = exc.read()
            return Response(exc.code, payload, dict(exc.headers.items()), final_url)


def _with_query(url: str, params: dict[str, Any] | None) -> str:
    if not params:
        return url
    parsed = parse.urlparse(url)
    existing = parse.parse_qsl(parsed.query, keep_blank_values=True)
    merged = existing + [(key, str(value)) for key, value in params.items() if value is not None]
    return parse.urlunparse(parsed._replace(query=parse.urlencode(merged)))


def _json_body(value: Any) -> bytes:
    return json.dumps(value).encode("utf-8")
