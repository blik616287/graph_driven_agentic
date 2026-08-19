"""Parser, router and middleware."""

from __future__ import annotations

import pytest

from marketd.api.auth import ApiKeyStore
from marketd.api.pagination import CursorCodec, paginate
from marketd.api.router import Router
from marketd.errors import BadRequest, MethodNotAllowed, NotFound, ProtocolError, Unauthorized
from marketd.http.message import Request, Response
from marketd.http.parser import parse_head, parse_query
from marketd.telemetry.metrics import Registry
from marketd.util.idgen import IdGenerator


def head(*lines: str) -> bytes:
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def test_headers_are_lowercased_and_duplicates_folded():
    parsed = parse_head(head("GET / HTTP/1.1", "Host: x", "X-Tag: a", "X-Tag: b"))
    assert parsed.headers["host"] == "x"
    assert parsed.headers["x-tag"] == "a, b"


@pytest.mark.parametrize(
    "raw,reason",
    [
        (head("GET"), "malformed request line"),
        (head("TRACE / HTTP/1.1"), "unsupported method"),
        (head("GET / HTTP/2.0"), "unsupported version"),
        (head("GET / HTTP/1.1", "Transfer-Encoding: chunked"), "transfer-encoding"),
        (head("GET / HTTP/1.1", "Content-Length: abc"), "invalid content-length"),
        (head("GET / HTTP/1.1", "Content-Length: -1"), "negative content-length"),
        (head("GET / HTTP/1.1", "bad-header-line"), "malformed header"),
    ],
)
def test_malformed_requests_are_rejected(raw, reason):
    """A lenient parser is a request-smuggling vulnerability."""
    with pytest.raises(ProtocolError, match=reason):
        parse_head(raw)


def test_keep_alive_defaults_differ_by_version():
    assert parse_head(head("GET / HTTP/1.1", "Host: x")).keep_alive is True
    assert parse_head(head("GET / HTTP/1.0", "Host: x")).keep_alive is False
    assert parse_head(head("GET / HTTP/1.1", "Connection: close")).keep_alive is False


def test_query_parsing_keeps_repeated_keys_and_decodes():
    assert parse_query("a=1&a=2&b=x%20y&c=&d") == {
        "a": ["1", "2"], "b": ["x y"], "c": [""], "d": [""]
    }
    assert parse_query("") == {}


def test_response_render_is_well_formed():
    raw = Response.json({"ok": True}, 201).render(keep_alive=False, now=0.0)
    head_bytes, _, body = raw.partition(b"\r\n\r\n")
    assert head_bytes.startswith(b"HTTP/1.1 201 Created\r\n")
    assert b"content-length: 11\r\n" in head_bytes
    assert b"connection: close\r\n" in head_bytes
    assert body == b'{"ok":true}'


def test_json_body_errors_are_client_errors():
    request = Request(
        method="POST", path="/", raw_query="", headers={"content-type": "application/json"},
        body=b"{not json", remote="-", received_at=0.0,
    )
    with pytest.raises(BadRequest, match="invalid JSON"):
        request.json()


def test_static_routes_beat_dynamic_ones():
    router = Router()

    async def handler(request):
        return None

    router.get("/v1/orders/{order_id}", handler, name="by_id")
    router.get("/v1/orders/open", handler, name="open")

    assert router.resolve("GET", "/v1/orders/open")[0].name == "open"
    assert router.resolve("GET", "/v1/orders/abc")[0].name == "by_id"


def test_router_distinguishes_wrong_path_from_wrong_verb():
    router = Router()

    async def handler(request):
        return None

    router.get("/v1/orders", handler)
    with pytest.raises(NotFound):
        router.resolve("GET", "/v1/nope")
    with pytest.raises(MethodNotAllowed) as caught:
        router.resolve("PUT", "/v1/orders")
    assert caught.value.details["allow"] == "GET"


def test_adding_a_route_invalidates_the_lookup_cache():
    router = Router()

    async def handler(request):
        return None

    router.get("/v1/x/{id}", handler, name="dynamic")
    assert router.resolve("GET", "/v1/x/thing")[0].name == "dynamic"
    router.get("/v1/x/thing", handler, name="static")
    # Without invalidation this would still return the cached dynamic route.
    assert router.resolve("GET", "/v1/x/thing")[0].name == "static"


def test_api_keys_verify_and_revoke(clock):
    registry = Registry()
    store = ApiKeyStore("server-secret", IdGenerator(1, clock), clock, registry)
    api_key, record = store.issue("acct_1", tier="pro")

    principal = store.authenticate(f"Bearer {api_key}")
    assert (principal.account_id, principal.tier) == ("acct_1", "pro")

    store.revoke(record.key_id)
    with pytest.raises(Unauthorized):
        store.authenticate(f"Bearer {api_key}")


@pytest.mark.parametrize(
    "header", [None, "", "Basic abc", "Bearer", "Bearer nodot", "Bearer key_x.wrong"]
)
def test_bad_authorization_headers_are_rejected(header, clock):
    store = ApiKeyStore("server-secret", IdGenerator(1, clock), clock, Registry())
    store.issue("acct_1")
    with pytest.raises(Unauthorized):
        store.authenticate(header)


def test_secrets_are_never_stored_in_the_clear(clock):
    store = ApiKeyStore("server-secret", IdGenerator(1, clock), clock, Registry())
    api_key, record = store.issue("acct_1")
    secret = api_key.split(".", 1)[1]
    assert secret not in record.secret_digest
    assert len(record.secret_digest) == 64  # sha256 hex


def test_cursors_round_trip_and_resist_tampering():
    codec = CursorCodec("secret")
    cursor = codec.encode("ord_123")
    assert codec.decode(cursor) == "ord_123"
    with pytest.raises(BadRequest):
        codec.decode(cursor[:-4] + "AAAA")
    with pytest.raises(BadRequest):
        codec.decode("not-a-cursor")


def test_paginate_reports_more_from_the_extra_row():
    class Row:
        def __init__(self, identifier):
            self.id = identifier

    codec = CursorCodec("secret")
    rows = [Row(f"id{i}") for i in range(6)]

    page = paginate(rows, limit=5, codec=codec)
    assert len(page.items) == 5 and page.has_more
    assert codec.decode(page.next_cursor) == "id4"

    last = paginate(rows[:3], limit=5, codec=codec)
    assert last.has_more is False and last.next_cursor is None
