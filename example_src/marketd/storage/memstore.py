"""In-memory repositories with secondary indexes and optimistic versioning.

Deliberately not thread safe.  The whole service runs on one event loop thread,
so a lock here would protect against a caller that cannot exist and would cost
every caller that does.  If you port this to threads, that decision - not the
data structures - is what has to change.

Two ideas worth carrying to a real database:

**Secondary indexes are declared, not discovered.**  A repository is told which
attributes will be looked up by, and maintains ``dict[value, set[key]]`` for
each.  A lookup is then O(1) instead of a scan.  Index maintenance happens on
every write, which is the trade you are making.

**Versioning replaces locking.**  Mutable entities carry ``version``; an update
states the version it read.  A mismatch is a 409, not a corrupted record.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable, Iterator
from typing import Generic, Protocol, TypeVar

from ..errors import Conflict, NotFound
from .query import Query

T = TypeVar("T")


class Versioned(Protocol):
    version: int


class MemoryRepository(Generic[T]):
    """A keyed collection of entities."""

    __slots__ = ("name", "_key_of", "_rows", "_index_specs", "_indexes")

    def __init__(
        self,
        name: str,
        key_of: Callable[[T], str],
        indexes: dict[str, Callable[[T], Hashable]] | None = None,
    ) -> None:
        self.name = name
        self._key_of = key_of
        self._rows: dict[str, T] = {}
        self._index_specs = indexes or {}
        self._indexes: dict[str, dict[Hashable, set[str]]] = {
            index_name: {} for index_name in self._index_specs
        }

    # ------------------------------------------------------------------ read
    def get(self, key: str) -> T | None:
        return self._rows.get(key)

    def require(self, key: str) -> T:
        row = self._rows.get(key)
        if row is None:
            raise NotFound(f"no {self.name} with id {key}", id=key)
        return row

    def find_by(self, index_name: str, value: Hashable) -> list[T]:
        """Index lookup.  Raises if the index was never declared - a silent
        fallback to a full scan is how an O(1) path quietly becomes O(n)."""
        index = self._indexes.get(index_name)
        if index is None:
            raise KeyError(
                f"{self.name} has no index {index_name!r}; declared: {sorted(self._indexes)}"
            )
        keys = index.get(value)
        if not keys:
            return []
        rows = self._rows
        return [rows[key] for key in keys if key in rows]

    def query(self, query: Query[T], index: tuple[str, Hashable] | None = None) -> list[T]:
        """Run ``query``, optionally narrowing the candidate set by an index first.

        Passing an index turns "filter every order in the venue" into "filter
        this account's orders" - the difference between O(all) and O(few).
        """
        candidates: Iterable[T] = (
            self.find_by(*index) if index is not None else self._rows.values()
        )
        return query.run(candidates)

    def count(self) -> int:
        return len(self._rows)

    def all(self) -> list[T]:
        return list(self._rows.values())

    def __iter__(self) -> Iterator[T]:
        return iter(self._rows.values())

    def __len__(self) -> int:
        return len(self._rows)

    def __contains__(self, key: object) -> bool:
        return key in self._rows

    # ----------------------------------------------------------------- write
    def add(self, row: T) -> T:
        key = self._key_of(row)
        if key in self._rows:
            raise Conflict(f"{self.name} {key} already exists", id=key)
        self._rows[key] = row
        self._index_add(key, row)
        return row

    def put(self, row: T) -> T:
        """Insert or replace, keeping indexes consistent."""
        key = self._key_of(row)
        existing = self._rows.get(key)
        if existing is not None:
            self._index_remove(key, existing)
        self._rows[key] = row
        self._index_add(key, row)
        return row

    def update(self, row: T, expected_version: int | None = None) -> T:
        """Persist a mutated entity, bumping its version.

        The caller mutates the object it read (it is the same object - this is
        an in-memory store) and then calls this to make the change *official*.
        With a real database this is where the UPDATE ... WHERE version = ?
        goes, and the pattern is unchanged.
        """
        key = self._key_of(row)
        current = self._rows.get(key)
        if current is None:
            raise NotFound(f"no {self.name} with id {key}", id=key)
        if expected_version is not None and getattr(current, "version", None) != expected_version:
            raise Conflict(
                f"{self.name} {key} was modified concurrently",
                id=key,
                expected_version=expected_version,
                actual_version=getattr(current, "version", None),
            )
        self._index_remove(key, current)
        self._rows[key] = row
        self._index_add(key, row)
        return row

    def delete(self, key: str) -> bool:
        row = self._rows.pop(key, None)
        if row is None:
            return False
        self._index_remove(key, row)
        return True

    # --------------------------------------------------------------- indexes
    def _index_add(self, key: str, row: T) -> None:
        for index_name, extract in self._index_specs.items():
            value = extract(row)
            if value is None:
                continue
            self._indexes[index_name].setdefault(value, set()).add(key)

    def _index_remove(self, key: str, row: T) -> None:
        for index_name, extract in self._index_specs.items():
            value = extract(row)
            if value is None:
                continue
            bucket = self._indexes[index_name].get(value)
            if bucket is not None:
                bucket.discard(key)
                if not bucket:
                    del self._indexes[index_name][value]

    def index_stats(self) -> dict[str, int]:
        return {name: len(index) for name, index in self._indexes.items()}
