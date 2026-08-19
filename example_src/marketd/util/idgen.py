"""Sortable identifiers.

A 63-bit layout borrowed from Snowflake::

    | 42 bits milliseconds since epoch | 10 bits node | 11 bits sequence |

Two properties matter.  Ids sort by creation time, which means the primary key
doubles as a cursor for pagination.  And generation never blocks: if a
millisecond runs out of sequence numbers the generator borrows from the future
rather than spinning on the clock, so a burst degrades into ids that are
slightly ahead of wall time instead of a stalled event loop.
"""

from __future__ import annotations

from .clock import SYSTEM_CLOCK, Clock

EPOCH_MS = 1_600_000_000_000  # 2020-09-13, keeps ids comfortably inside 63 bits

_NODE_BITS = 10
_SEQ_BITS = 11
_MAX_NODE = (1 << _NODE_BITS) - 1
_SEQ_MASK = (1 << _SEQ_BITS) - 1
_TIME_SHIFT = _NODE_BITS + _SEQ_BITS

_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_BASE = len(_ALPHABET)


def base62(value: int) -> str:
    """Encode a non-negative integer with a lexicographically dense alphabet."""
    if value < 0:
        raise ValueError("base62 is unsigned")
    if value == 0:
        return "0"
    out: list[str] = []
    alphabet = _ALPHABET
    while value:
        value, rem = divmod(value, _BASE)
        out.append(alphabet[rem])
    return "".join(reversed(out))


class IdGenerator:
    """Monotonic id source for one process.

    Not thread safe - and it does not need to be.  The whole service runs on a
    single event loop thread, which is what lets the sequence counter be a
    plain attribute instead of an atomic.
    """

    __slots__ = ("_node", "_clock", "_last_ms", "_seq")

    def __init__(self, node_id: int = 0, clock: Clock = SYSTEM_CLOCK) -> None:
        if not 0 <= node_id <= _MAX_NODE:
            raise ValueError(f"node_id must be in [0, {_MAX_NODE}]")
        self._node = node_id
        self._clock = clock
        self._last_ms = 0
        self._seq = 0

    # --- HOT PATH ---------------------------------------------------------
    # Called at least three times per order (order id, trade id, journal id).
    def next_id(self) -> int:
        ms = int(self._clock.now() * 1000.0) - EPOCH_MS
        last = self._last_ms
        if ms > last:
            self._last_ms = ms
            self._seq = 0
        else:
            seq = (self._seq + 1) & _SEQ_MASK
            if seq == 0:
                # Sequence exhausted for this millisecond: move into the next
                # one instead of busy-waiting for the clock to catch up.
                ms = last + 1
                self._last_ms = ms
            else:
                ms = last
            self._seq = seq
        return (ms << _TIME_SHIFT) | (self._node << _SEQ_BITS) | self._seq

    def next_token(self, prefix: str = "") -> str:
        """A human-pasteable id, e.g. ``ord_3kQm1z``."""
        token = base62(self.next_id())
        return f"{prefix}_{token}" if prefix else token

    @staticmethod
    def timestamp_ms(identifier: int) -> int:
        """Recover the creation time embedded in an id."""
        return (identifier >> _TIME_SHIFT) + EPOCH_MS
