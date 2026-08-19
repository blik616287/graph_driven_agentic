"""Request head parsing.

The parser only handles what this service actually speaks: HTTP/1.1, no
chunked request bodies, no trailers, no upgrades.  Everything it does not
support it rejects explicitly, which is the only safe way to write a parser -
a lenient one is a request smuggling vulnerability with extra steps.

Performance notes, in the order they matter:

* Split the head into lines **once**, on bytes, and decode only what is needed.
* ``bytes.partition`` beats ``split`` when you want exactly two pieces: no list
  allocation, no maxsplit argument to interpret.
* Header names are lowercased at parse time so that every later lookup is a
  plain dict hit instead of a case-insensitive scan.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import unquote, unquote_plus

from ..errors import ProtocolError

MAX_HEADERS = 100
SUPPORTED_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})


@dataclass(slots=True)
class ParsedHead:
    method: str
    target: str
    path: str
    raw_query: str
    version: str
    headers: dict[str, str]
    content_length: int
    keep_alive: bool
    expects_continue: bool


# --- HOT PATH -------------------------------------------------------------
# Once per request.
def parse_head(raw: bytes) -> ParsedHead:
    """Parse everything up to (not including) the body.

    ``raw`` must end with the blank line that terminates the head.
    """
    lines = raw.split(b"\r\n")
    request_line = lines[0]
    if not request_line:
        raise ProtocolError("empty request line")

    method_b, _, rest = request_line.partition(b" ")
    target_b, _, version_b = rest.partition(b" ")
    if not method_b or not target_b or not version_b:
        raise ProtocolError("malformed request line")

    try:
        method = method_b.decode("ascii")
        target = target_b.decode("ascii")
        version = version_b.decode("ascii")
    except UnicodeDecodeError:
        raise ProtocolError("request line must be ASCII") from None

    if method not in SUPPORTED_METHODS:
        raise ProtocolError(f"unsupported method {method}", method=method)
    if version not in ("HTTP/1.1", "HTTP/1.0"):
        raise ProtocolError(f"unsupported version {version}", version=version)

    headers: dict[str, str] = {}
    for index, line in enumerate(lines[1:]):
        if not line:
            break
        if index >= MAX_HEADERS:
            raise ProtocolError("too many headers")
        name_b, sep, value_b = line.partition(b":")
        if not sep:
            raise ProtocolError("malformed header line")
        try:
            name = name_b.decode("ascii").strip().lower()
            value = value_b.decode("latin-1").strip()
        except UnicodeDecodeError:
            raise ProtocolError("header names must be ASCII") from None
        existing = headers.get(name)
        # Repeated headers fold onto one comma-separated line (RFC 9110 5.3).
        headers[name] = value if existing is None else f"{existing}, {value}"

    if "transfer-encoding" in headers:
        # Supporting chunked means supporting the interaction between
        # Transfer-Encoding and Content-Length, which is exactly where request
        # smuggling lives.  Refuse instead.
        raise ProtocolError("transfer-encoding is not supported")

    raw_length = headers.get("content-length")
    if raw_length is None:
        content_length = 0
    else:
        try:
            content_length = int(raw_length)
        except ValueError:
            raise ProtocolError("invalid content-length") from None
        if content_length < 0:
            raise ProtocolError("negative content-length")

    path_b, _, query = target.partition("?")
    # '%' is rare in paths; skipping the call is measurably cheaper than making it.
    path = unquote(path_b) if "%" in path_b else path_b

    connection = headers.get("connection", "").lower()
    keep_alive = (
        "close" not in connection
        if version == "HTTP/1.1"
        else "keep-alive" in connection
    )

    return ParsedHead(
        method=method,
        target=target,
        path=path,
        raw_query=query,
        version=version,
        headers=headers,
        content_length=content_length,
        keep_alive=keep_alive,
        expects_continue=headers.get("expect", "").lower() == "100-continue",
    )


# --- HOT PATH -------------------------------------------------------------
def parse_query(raw: str) -> dict[str, list[str]]:
    """Parse a query string into a multi-valued mapping.

    Values are kept as lists because ``?symbol=A&symbol=B`` is legal and
    collapsing it silently loses data.  Percent-decoding is skipped entirely
    when the token contains neither ``%`` nor ``+`` - the overwhelmingly common
    case, and the check is far cheaper than the decode.
    """
    if not raw:
        return {}
    out: dict[str, list[str]] = {}
    for part in raw.split("&"):
        if not part:
            continue
        key, _, value = part.partition("=")
        if "%" in key or "+" in key:
            key = unquote_plus(key)
        if "%" in value or "+" in value:
            value = unquote_plus(value)
        existing = out.get(key)
        if existing is None:
            out[key] = [value]
        else:
            existing.append(value)
    return out
