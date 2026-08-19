"""A minimal HTTP/1.1 client connection.

Just enough to talk to this server: fixed content-length bodies, keep-alive,
and a strict read of the status line and headers.  The pooling, retrying and
circuit breaking all live one layer up in :mod:`marketd.sdk.client`, because
those are policy and this is mechanism.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..errors import ProtocolError, ServiceUnavailable
from ..util import jsonx


class ClientResponse:
    __slots__ = ("status", "headers", "body")

    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    def json(self) -> Any:
        if not self.body:
            return None
        return jsonx.loads(self.body)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def __repr__(self) -> str:
        return f"<ClientResponse {self.status} {len(self.body)}b>"


class Connection:
    """One TCP connection, reusable while the peer keeps it open."""

    __slots__ = ("host", "port", "_reader", "_writer", "_closed", "requests_served")

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._closed = False
        self.requests_served = 0

    @classmethod
    async def connect(cls, host: str, port: int, timeout: float = 5.0) -> Connection:
        connection = cls(host, port)
        try:
            connection._reader, connection._writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout
            )
        except (asyncio.TimeoutError, OSError) as exc:
            raise ServiceUnavailable(f"cannot connect to {host}:{port}: {exc}") from exc
        return connection

    @property
    def alive(self) -> bool:
        """Cheap liveness check for the pool.

        ``at_eof`` catches the common case - the peer closed while the
        connection sat idle - without a syscall.
        """
        if self._closed or self._reader is None or self._writer is None:
            return False
        return not (self._writer.is_closing() or self._reader.at_eof())

    async def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
        timeout: float = 10.0,
    ) -> ClientResponse:
        if self._reader is None or self._writer is None:
            raise ServiceUnavailable("connection is not open")

        lines = [f"{method} {path} HTTP/1.1", f"host: {self.host}:{self.port}"]
        # Always sent, even for an empty body: a server that has to guess
        # whether a body follows is a server that will eventually guess wrong.
        lines.append(f"content-length: {len(body)}")
        lines.append("connection: keep-alive")
        for name, value in (headers or {}).items():
            lines.append(f"{name.lower()}: {value}")
        head = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")

        self._writer.write(head + body if body else head)
        await self._writer.drain()

        try:
            return await asyncio.wait_for(self._read_response(), timeout)
        except asyncio.TimeoutError:
            # The response may still arrive later and would be read as the next
            # response.  The connection is no longer trustworthy.
            await self.close()
            raise ServiceUnavailable(f"{method} {path} timed out after {timeout}s") from None

    async def _read_response(self) -> ClientResponse:
        assert self._reader is not None
        try:
            raw_head = await self._reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
            await self.close()
            raise ServiceUnavailable("connection closed before a response arrived") from exc

        lines = raw_head.split(b"\r\n")
        _, _, rest = lines[0].partition(b" ")
        status_b, _, _ = rest.partition(b" ")
        try:
            status = int(status_b)
        except ValueError:
            raise ProtocolError("malformed status line") from None

        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line:
                break
            name, sep, value = line.partition(b":")
            if sep:
                headers[name.decode("ascii").strip().lower()] = value.decode("latin-1").strip()

        length = int(headers.get("content-length", "0") or 0)
        body = await self._reader.readexactly(length) if length else b""

        self.requests_served += 1
        if headers.get("connection", "").lower() == "close":
            await self.close()
        return ClientResponse(status, headers, body)

    async def close(self) -> None:
        self._closed = True
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError, OSError):
                pass
            self._writer = None
            self._reader = None
