"""Request and response objects.

``Request`` is built once per request and read many times, so the expensive
derivations - query parsing, JSON decoding - are lazy and cached on the
instance.  A handler that never looks at the query string never pays to parse
it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..errors import BadRequest
from ..util import jsonx
from .parser import parse_query

STATUS_PHRASES: dict[int, str] = {
    200: "OK", 201: "Created", 202: "Accepted", 204: "No Content",
    301: "Moved Permanently", 304: "Not Modified",
    400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
    405: "Method Not Allowed", 408: "Request Timeout", 409: "Conflict",
    411: "Length Required", 413: "Content Too Large", 415: "Unsupported Media Type",
    422: "Unprocessable Content", 429: "Too Many Requests", 431: "Request Header Fields Too Large",
    500: "Internal Server Error", 501: "Not Implemented", 503: "Service Unavailable",
}

_JSON_CONTENT_TYPE = "application/json; charset=utf-8"

# The Date header is mandatory and only has one-second resolution, so format it
# at most once per second no matter how many responses go out.
_date_value = ""
_date_second = 0.0


def http_date(now: float) -> str:
    global _date_value, _date_second
    whole = int(now)
    if whole != _date_second:
        _date_second = whole
        _date_value = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(whole))
    return _date_value


@dataclass(slots=True)
class Request:
    method: str
    path: str
    raw_query: str
    headers: dict[str, str]
    body: bytes
    remote: str
    received_at: float
    version: str = "HTTP/1.1"
    # Per-request scratch space: middleware writes here, handlers read.
    ctx: dict[str, Any] = field(default_factory=dict)
    path_params: dict[str, str] = field(default_factory=dict)
    _query: dict[str, list[str]] | None = field(default=None, repr=False)
    _json: Any = field(default=None, repr=False)

    @property
    def query(self) -> dict[str, list[str]]:
        if self._query is None:
            self._query = parse_query(self.raw_query)
        return self._query

    def param(self, name: str, default: str | None = None) -> str | None:
        """First value of a query parameter."""
        values = self.query.get(name)
        return values[0] if values else default

    def int_param(self, name: str, default: int) -> int:
        raw = self.param(name)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            raise BadRequest(f"{name} must be an integer", parameter=name, value=raw) from None

    def header(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name.lower(), default)

    def json(self) -> Any:
        """Parse the body as JSON, once.

        Missing and malformed bodies both surface as a 400 with a usable
        message; letting a ``JSONDecodeError`` reach the error middleware would
        report a 500 for what is squarely the client's mistake.
        """
        if self._json is None:
            if not self.body:
                raise BadRequest("a JSON body is required")
            content_type = self.headers.get("content-type", "")
            if content_type and not content_type.startswith("application/json"):
                raise BadRequest(
                    f"expected application/json, got {content_type}", content_type=content_type
                )
            try:
                self._json = jsonx.loads(self.body)
            except ValueError as exc:
                raise BadRequest(f"invalid JSON: {exc}") from None
        return self._json

    def __str__(self) -> str:
        return f"{self.method} {self.path}"


@dataclass(slots=True)
class Response:
    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def json(
        cls, payload: Any, status: int = 200, headers: dict[str, str] | None = None
    ) -> Response:
        response = cls(status=status, body=jsonx.dumps(payload), headers=headers or {})
        response.headers["content-type"] = _JSON_CONTENT_TYPE
        return response

    @classmethod
    def text(
        cls, body: str, status: int = 200, content_type: str = "text/plain; charset=utf-8"
    ) -> Response:
        return cls(status=status, body=body.encode("utf-8"), headers={"content-type": content_type})

    @classmethod
    def empty(cls, status: int = 204) -> Response:
        return cls(status=status, body=b"")

    # --- HOT PATH ---------------------------------------------------------
    def render(self, *, keep_alive: bool, now: float, include_body: bool = True) -> bytes:
        """Serialise to wire bytes.

        Assembled into a list and joined once.  Repeated ``+=`` on a bytes
        object reallocates and copies the whole buffer every time, which turns a
        fixed cost into a quadratic one as headers accumulate.
        """
        phrase = STATUS_PHRASES.get(self.status, "Unknown")
        parts = [
            b"HTTP/1.1 ", str(self.status).encode("ascii"), b" ", phrase.encode("ascii"), b"\r\n",
            b"date: ", http_date(now).encode("ascii"), b"\r\n",
            b"content-length: ", str(len(self.body)).encode("ascii"), b"\r\n",
            b"connection: ", b"keep-alive" if keep_alive else b"close", b"\r\n",
        ]
        for name, value in self.headers.items():
            parts.append(name.encode("ascii"))
            parts.append(b": ")
            parts.append(str(value).encode("latin-1"))
            parts.append(b"\r\n")
        parts.append(b"\r\n")
        if include_body and self.body:
            parts.append(self.body)
        return b"".join(parts)
