"""JSON encode/decode with the project's conventions baked in.

Centralised for two reasons.  It is the single place to swap in a faster
encoder (``orjson`` drops in behind the same two functions), and it fixes the
separator and default-encoding rules so that responses are byte-stable - which
is what makes ETags and response caching possible.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

_SEPARATORS = (",", ":")  # no whitespace: ~8% smaller bodies for free


def _default(obj: Any) -> Any:
    """Fallback for types :mod:`json` cannot encode.

    ``Decimal`` becomes a *string*, never a float - the whole reason the domain
    uses exact arithmetic is undone the moment a price round-trips through
    binary floating point.
    """
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, (set, frozenset, tuple)):
        return list(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    raise TypeError(f"{type(obj).__name__} is not JSON serialisable")


# A pre-built encoder instance: constructing one per call shows up in profiles
# once you are past a few thousand responses per second.
_ENCODER = json.JSONEncoder(
    separators=_SEPARATORS,
    default=_default,
    ensure_ascii=False,
    check_circular=False,
)
_DECODER = json.JSONDecoder()


# --- HOT PATH -------------------------------------------------------------
def dumps(obj: Any) -> bytes:
    """Serialise to UTF-8 bytes (the only form the socket accepts)."""
    return _ENCODER.encode(obj).encode("utf-8")


def dumps_str(obj: Any) -> str:
    return _ENCODER.encode(obj)


# --- HOT PATH -------------------------------------------------------------
def loads(data: bytes | str) -> Any:
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    return _DECODER.decode(data)
