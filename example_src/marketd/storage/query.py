"""A tiny query language that compiles to a predicate.

Same idea as the schema validator: the shape of a query is known before the
data is.  ``Query.compile()`` turns the filter list into one chained predicate
and the sort key into one ``attrgetter``, so executing it over N rows costs N
calls instead of N x (parse + dispatch).

Filters compose with AND.  That is a real limitation and a deliberate one - the
moment you need OR trees you should be generating SQL, not walking dicts.
"""

from __future__ import annotations

import operator
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

T = TypeVar("T")

_OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": operator.eq,
    "ne": operator.ne,
    "lt": operator.lt,
    "le": operator.le,
    "gt": operator.gt,
    "ge": operator.ge,
    "in": lambda value, options: value in options,
    "nin": lambda value, options: value not in options,
    "contains": lambda value, needle: needle in (value or ""),
    "startswith": lambda value, prefix: str(value).startswith(prefix),
}


@dataclass(frozen=True, slots=True)
class Filter:
    field: str
    op: str
    value: Any

    def __post_init__(self) -> None:
        if self.op not in _OPERATORS:
            raise ValueError(
                f"unsupported operator {self.op!r}; expected one of {sorted(_OPERATORS)}"
            )


@dataclass(slots=True)
class Query(Generic[T]):
    """Immutable-ish builder.  Each method returns a new query."""

    filters: tuple[Filter, ...] = ()
    sort_field: str | None = None
    descending: bool = False
    limit_value: int | None = None
    offset_value: int = 0

    def where(self, field_name: str, op: str, value: Any) -> Query[T]:
        return Query(
            filters=self.filters + (Filter(field_name, op, value),),
            sort_field=self.sort_field,
            descending=self.descending,
            limit_value=self.limit_value,
            offset_value=self.offset_value,
        )

    def eq(self, field_name: str, value: Any) -> Query[T]:
        return self.where(field_name, "eq", value)

    def order_by(self, field_name: str, descending: bool = False) -> Query[T]:
        return Query(self.filters, field_name, descending, self.limit_value, self.offset_value)

    def limit(self, count: int) -> Query[T]:
        return Query(self.filters, self.sort_field, self.descending, count, self.offset_value)

    def offset(self, count: int) -> Query[T]:
        return Query(self.filters, self.sort_field, self.descending, self.limit_value, count)

    def compile(self) -> Callable[[Iterable[T]], list[T]]:
        """Build the executor.  Do this once per query shape, not per call."""
        # Resolve the operator functions and bind the comparison values now.
        checks: tuple[tuple[Callable[[Any], Any], Callable[[Any, Any], bool], Any], ...] = tuple(
            (operator.attrgetter(f.field), _OPERATORS[f.op], f.value) for f in self.filters
        )
        sort_key = operator.attrgetter(self.sort_field) if self.sort_field else None
        descending = self.descending
        limit_value, offset_value = self.limit_value, self.offset_value

        def execute(rows: Iterable[T]) -> list[T]:
            if checks:
                selected = [
                    row for row in rows
                    if all(compare(get(row), expected) for get, compare, expected in checks)
                ]
            else:
                selected = list(rows)
            if sort_key is not None:
                selected.sort(key=sort_key, reverse=descending)
            if offset_value:
                selected = selected[offset_value:]
            if limit_value is not None:
                selected = selected[:limit_value]
            return selected

        return execute

    def run(self, rows: Iterable[T]) -> list[T]:
        """One-shot execution.  Convenient; not for hot paths."""
        return self.compile()(rows)
