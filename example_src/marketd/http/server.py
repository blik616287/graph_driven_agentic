"""The asyncio connection loop.

One task per connection.  The loop is: read a head, read a body, call the
application, write a response, decide whether to keep the connection open.
Everything hard about a real server lives in the edges of that sentence:

* **Timeouts on every read.**  An idle keep-alive connection and a client that
  opened a socket and went quiet look identical until you time them out.
* **Backpressure.**  ``drain()`` after a write is what stops a slow reader from
  making the server buffer the response in memory.
* **Errors must not desynchronise the stream.**  If the request cannot be
  parsed, the position of the next request in the byte stream is unknown, so
  the only correct move is to reply once and close.
* **Shutdown has to be graceful.**  Stop accepting, let in-flight requests
  finish, then close what is left.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from ..config import Settings
from ..errors import MarketError, PayloadTooLarge, ProtocolError
from ..telemetry.logging import get_logger
from ..telemetry.metrics import Registry
from ..util.clock import SYSTEM_CLOCK, Clock
from .message import Request, Response
from .parser import parse_head

Application = Callable[[Request], Awaitable[Response]]

_logger = get_logger("http.server")


class HTTPServer:
    def __init__(
        self,
        app: Application,
        settings: Settings,
        registry: Registry,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._app = app
        self._settings = settings
        self._clock = clock
        self._server: asyncio.AbstractServer | None = None
        self._connections: set[asyncio.Task] = set()
        self._closing = False
        self._g_conns = registry.gauge("http_connections", "open connections")
        self._m_conns = registry.counter("http_connections_total", "connections accepted")
        self._m_requests = registry.counter("http_server_requests_total", "requests read")
        self._m_errors = registry.counter("http_protocol_errors_total", "malformed requests")

    @property
    def port(self) -> int:
        """The bound port - the point of binding to 0 in tests."""
        if self._server is None or not self._server.sockets:
            raise RuntimeError("server is not listening")
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._on_connection,
            host=self._settings.host,
            port=self._settings.port,
            backlog=self._settings.backlog,
            # StreamReader's buffer limit doubles as the max head size: a head
            # larger than this makes readuntil raise instead of buffering
            # without bound.
            limit=self._settings.max_header_bytes,
        )
        _logger.info(
            "listening", extra={"host": self._settings.host, "port": self.port}
        )

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        """Stop accepting, drain in-flight requests, then drop the rest."""
        self._closing = True
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        if self._connections:
            done, pending = await asyncio.wait(
                self._connections, timeout=self._settings.shutdown_grace
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        _logger.info("server stopped")

    def _on_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.get_running_loop().create_task(self._serve_connection(reader, writer))
        self._connections.add(task)
        task.add_done_callback(self._connections.discard)

    async def _serve_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        remote = f"{peer[0]}:{peer[1]}" if peer else "-"
        self._m_conns.inc()
        self._g_conns.inc()
        settings = self._settings
        try:
            while not self._closing:
                head = await self._read_head(reader, settings.keepalive_timeout)
                if head is None:
                    return  # clean close, or an idle connection that timed out

                try:
                    parsed = parse_head(head)
                except ProtocolError as exc:
                    self._m_errors.inc()
                    await self._write(writer, _error_response(exc), keep_alive=False)
                    return

                if parsed.content_length > settings.max_body_bytes:
                    await self._write(
                        writer,
                        _error_response(
                            PayloadTooLarge(
                                f"body exceeds {settings.max_body_bytes} bytes",
                                limit=settings.max_body_bytes,
                            )
                        ),
                        keep_alive=False,
                    )
                    return

                if parsed.expects_continue:
                    writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
                    await writer.drain()

                body = b""
                if parsed.content_length:
                    try:
                        body = await asyncio.wait_for(
                            reader.readexactly(parsed.content_length), settings.read_timeout
                        )
                    except (asyncio.IncompleteReadError, asyncio.TimeoutError):
                        return  # truncated body: nothing useful left to say

                request = Request(
                    method=parsed.method,
                    path=parsed.path,
                    raw_query=parsed.raw_query,
                    headers=parsed.headers,
                    body=body,
                    remote=remote,
                    received_at=self._clock.now(),
                    version=parsed.version,
                )
                self._m_requests.inc()

                # The application is expected to convert its own errors; a leak
                # to here is a bug in the middleware, so it is logged loudly and
                # answered with a bare 500.
                try:
                    response = await self._app(request)
                except MarketError as exc:
                    response = _error_response(exc)
                except Exception:
                    _logger.exception("unhandled application error", extra={"path": request.path})
                    response = Response.json(
                        {"error": {"code": "internal_error", "message": "internal error"}},
                        status=500,
                    )

                keep_alive = parsed.keep_alive and not self._closing
                if parsed.method == "HEAD":
                    response.body = b""
                await self._write(writer, response, keep_alive=keep_alive)
                if not keep_alive:
                    return
        except (ConnectionResetError, BrokenPipeError):
            pass  # the client vanished; entirely routine
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception("connection failed", extra={"remote": remote})
        finally:
            self._g_conns.dec()
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError, OSError):
                pass

    async def _read_head(self, reader: asyncio.StreamReader, timeout: float) -> bytes | None:
        try:
            return await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
        except asyncio.IncompleteReadError:
            return None       # peer closed between requests
        except asyncio.TimeoutError:
            return None       # keep-alive expired
        except asyncio.LimitOverrunError:
            self._m_errors.inc()
            return None       # head bigger than the buffer limit

    async def _write(
        self, writer: asyncio.StreamWriter, response: Response, *, keep_alive: bool
    ) -> None:
        writer.write(response.render(keep_alive=keep_alive, now=self._clock.now()))
        # Yield until the kernel buffer has room: without this a slow client
        # would be absorbed by unbounded memory on the server.
        await writer.drain()


def _error_response(exc: MarketError) -> Response:
    return Response.json(exc.payload(), status=exc.status)
