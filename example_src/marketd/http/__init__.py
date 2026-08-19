"""HTTP/1.1 transport, written from the socket up.

Not because you should ship your own HTTP server, but because the layer above
it - routing, middleware, handlers - is much easier to reason about once you
have seen what it sits on: a byte buffer, a blank line, a content length and a
decision about whether to keep the connection open.
"""

from .message import Request, Response
from .parser import ParsedHead, parse_head, parse_query

__all__ = ["Request", "Response", "ParsedHead", "parse_head", "parse_query"]
