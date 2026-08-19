"""Signed cursor pagination.

Offset pagination (``?page=3``) is wrong for anything that changes: insert a row
and page 4 repeats what page 3 showed.  Cursors point at *a position in the
data*, so concurrent writes cannot shift the window.

The cursor is opaque and signed.  Opaque so clients cannot come to depend on
its shape; signed so they cannot forge one.  It is not encrypted - the contents
are the caller's own last-seen id, which is not a secret - but tampering is
detectable, which is what matters when the id is fed back into a query.
"""

from __future__ import annotations

import base64
import hmac
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from ..errors import BadRequest


@dataclass(frozen=True, slots=True)
class Page:
    items: list[Any]
    next_cursor: str | None
    has_more: bool

    def to_dict(self, present) -> dict[str, Any]:
        return {
            "data": [present(item) for item in self.items],
            "page": {"next_cursor": self.next_cursor, "has_more": self.has_more},
        }


class CursorCodec:
    __slots__ = ("_secret",)

    def __init__(self, secret: str) -> None:
        self._secret = secret.encode("utf-8")

    def encode(self, position: str) -> str:
        signature = self._sign(position)
        raw = f"{position}.{signature}".encode()
        # urlsafe + stripped padding: cursors travel in query strings.
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    def decode(self, cursor: str) -> str:
        padding = "=" * (-len(cursor) % 4)
        try:
            raw = base64.urlsafe_b64decode(cursor + padding).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            raise BadRequest("malformed cursor") from None
        position, _, signature = raw.rpartition(".")
        if not position or not hmac.compare_digest(self._sign(position), signature):
            raise BadRequest("invalid or tampered cursor")
        return position

    def _sign(self, position: str) -> str:
        return hmac.new(self._secret, position.encode("utf-8"), sha256).hexdigest()[:16]


def paginate(
    rows: list[Any],
    *,
    limit: int,
    codec: CursorCodec,
    key: str = "id",
) -> Page:
    """Slice ``rows`` into a page and mint the next cursor.

    Fetching ``limit + 1`` rows is how ``has_more`` is answered without a second
    COUNT query: if the extra row exists there is another page.  The caller is
    expected to have applied that ``+1`` when querying.
    """
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = None
    if has_more and items:
        next_cursor = codec.encode(str(getattr(items[-1], key)))
    return Page(items=items, next_cursor=next_cursor, has_more=has_more)
