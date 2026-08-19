"""A schema validator that compiles to closures.

The idea is worth understanding beyond this file.  A naive validator walks the
schema *and* the data on every request, re-deciding "is this field required?
what type? what bounds?" each time.  All of that is known when the schema is
defined and none of it changes per request.

So each node exposes ``compile()``, which returns a plain function specialised
to that node's configuration.  An ``Obj`` compiles its children once and stores
them in a tuple of ``(key, validate_fn, required, default)``.  Validation then
costs one tuple iteration and one call per field - no schema traversal, no
attribute lookups on the node, no ``isinstance`` chain over possible node types.

Errors accumulate rather than short-circuit: an API that reports one bad field
per round trip makes clients play twenty questions.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from ..errors import ValidationError

Validator = Callable[[Any, str], Any]

_MISSING = object()


class Invalid(Exception):
    """Internal signal: one field failed.  Never escapes this module."""

    __slots__ = ("message",)

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class Node:
    """Base schema node.  Subclasses implement :meth:`compile` only."""

    __slots__ = ()

    def compile(self) -> Validator:  # pragma: no cover - abstract
        raise NotImplementedError

    def validate(self, value: Any, path: str = "$") -> Any:
        """Convenience for one-off use; prefer a compiled validator."""
        return self.compile()(value, path)


class Str(Node):
    __slots__ = ("min_len", "max_len", "pattern", "choices", "strip", "lower")

    def __init__(
        self,
        *,
        min_len: int = 0,
        max_len: int = 4096,
        pattern: str | None = None,
        choices: tuple[str, ...] | None = None,
        strip: bool = True,
        lower: bool = False,
    ) -> None:
        self.min_len = min_len
        self.max_len = max_len
        self.pattern = re.compile(pattern) if pattern else None
        self.choices = frozenset(choices) if choices else None
        self.strip = strip
        self.lower = lower

    def compile(self) -> Validator:
        # Everything the checker needs is bound as a local now, so the returned
        # closure touches no attributes at call time.
        min_len, max_len = self.min_len, self.max_len
        matcher = self.pattern.match if self.pattern is not None else None
        pattern_src = self.pattern.pattern if self.pattern is not None else ""
        choices, strip, lower = self.choices, self.strip, self.lower

        def check(value: Any, path: str) -> str:
            if not isinstance(value, str):
                raise Invalid("expected a string")
            if strip:
                value = value.strip()
            if lower:
                value = value.lower()
            length = len(value)
            if length < min_len:
                raise Invalid(f"must be at least {min_len} characters")
            if length > max_len:
                raise Invalid(f"must be at most {max_len} characters")
            if matcher is not None and matcher(value) is None:
                raise Invalid(f"must match {pattern_src}")
            if choices is not None and value not in choices:
                raise Invalid(f"must be one of: {', '.join(sorted(choices))}")
            return value

        return check


class Int(Node):
    __slots__ = ("minimum", "maximum")

    def __init__(self, *, minimum: int | None = None, maximum: int | None = None) -> None:
        self.minimum = minimum
        self.maximum = maximum

    def compile(self) -> Validator:
        minimum, maximum = self.minimum, self.maximum

        def check(value: Any, path: str) -> int:
            # bool is an int subclass; accepting True as 1 hides real bugs.
            if isinstance(value, bool) or not isinstance(value, int):
                raise Invalid("expected an integer")
            if minimum is not None and value < minimum:
                raise Invalid(f"must be >= {minimum}")
            if maximum is not None and value > maximum:
                raise Invalid(f"must be <= {maximum}")
            return value

        return check


class Dec(Node):
    """A decimal quantity, accepted as a JSON string or number.

    Strings are preferred on the wire and the docs say so, but JSON numbers are
    accepted and converted via ``str`` - which for a float is the shortest
    representation that round-trips, the least-wrong reading of a value the
    client already damaged by sending it as a float.
    """

    __slots__ = ("minimum", "maximum", "max_places", "allow_zero")

    def __init__(
        self,
        *,
        minimum: str | None = None,
        maximum: str | None = None,
        max_places: int = 18,
        allow_zero: bool = False,
    ) -> None:
        self.minimum = Decimal(minimum) if minimum is not None else None
        self.maximum = Decimal(maximum) if maximum is not None else None
        self.max_places = max_places
        self.allow_zero = allow_zero

    def compile(self) -> Validator:
        minimum, maximum = self.minimum, self.maximum
        max_places, allow_zero = self.max_places, self.allow_zero

        def check(value: Any, path: str) -> Decimal:
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise Invalid("expected a decimal number as a string")
            try:
                dec = Decimal(str(value))
            except (InvalidOperation, ValueError):
                raise Invalid("not a valid decimal") from None
            if not dec.is_finite():
                raise Invalid("must be finite")
            exponent = -dec.as_tuple().exponent
            if exponent > max_places:
                raise Invalid(f"at most {max_places} decimal places")
            if not allow_zero and dec == 0:
                raise Invalid("must not be zero")
            if minimum is not None and dec < minimum:
                raise Invalid(f"must be >= {minimum}")
            if maximum is not None and dec > maximum:
                raise Invalid(f"must be <= {maximum}")
            return dec

        return check


class Bool(Node):
    __slots__ = ()

    def compile(self) -> Validator:
        def check(value: Any, path: str) -> bool:
            if not isinstance(value, bool):
                raise Invalid("expected a boolean")
            return value

        return check


class EnumOf(Node):
    """Coerce a string into an ``Enum`` member."""

    __slots__ = ("enum_cls", "_lookup")

    def __init__(self, enum_cls: type[Enum]) -> None:
        self.enum_cls = enum_cls
        # Built once: the membership test is then a dict hit, not a linear scan.
        self._lookup = {member.value: member for member in enum_cls}

    def compile(self) -> Validator:
        lookup = self._lookup
        allowed = ", ".join(sorted(lookup))

        def check(value: Any, path: str) -> Enum:
            if not isinstance(value, str):
                raise Invalid(f"must be one of: {allowed}")
            member = lookup.get(value.lower())
            if member is None:
                raise Invalid(f"must be one of: {allowed}")
            return member

        return check


class ListOf(Node):
    __slots__ = ("item", "min_items", "max_items")

    def __init__(self, item: Node, *, min_items: int = 0, max_items: int = 1000) -> None:
        self.item = item
        self.min_items = min_items
        self.max_items = max_items

    def compile(self) -> Validator:
        check_item = self.item.compile()
        min_items, max_items = self.min_items, self.max_items

        def check(value: Any, path: str) -> list:
            if not isinstance(value, list):
                raise Invalid("expected an array")
            if len(value) < min_items:
                raise Invalid(f"needs at least {min_items} items")
            if len(value) > max_items:
                raise Invalid(f"accepts at most {max_items} items")
            return [check_item(item, f"{path}[{index}]") for index, item in enumerate(value)]

        return check


class Field:
    """A named slot in an object schema."""

    __slots__ = ("node", "required", "default")

    def __init__(self, node: Node, *, required: bool = True, default: Any = None) -> None:
        self.node = node
        self.required = required
        self.default = default


def optional(node: Node, default: Any = None) -> Field:
    return Field(node, required=False, default=default)


def required(node: Node) -> Field:
    return Field(node, required=True)


class Obj(Node):
    """An object schema.  This is where the compilation pays off."""

    __slots__ = ("fields", "allow_extra")

    def __init__(self, fields: dict[str, Field | Node], *, allow_extra: bool = False) -> None:
        self.fields = {
            name: (spec if isinstance(spec, Field) else Field(spec))
            for name, spec in fields.items()
        }
        self.allow_extra = allow_extra

    def compile(self) -> Validator:
        # One tuple, built at import time, iterated per request.
        plan: tuple[tuple[str, Validator, bool, Any], ...] = tuple(
            (name, spec.node.compile(), spec.required, spec.default)
            for name, spec in self.fields.items()
        )
        known = frozenset(self.fields)
        allow_extra = self.allow_extra

        # --- HOT PATH -----------------------------------------------------
        def check(value: Any, path: str) -> dict[str, Any]:
            if not isinstance(value, dict):
                raise Invalid("expected an object")
            errors: dict[str, str] = {}
            result: dict[str, Any] = {}
            for name, validate, is_required, default in plan:
                raw = value.get(name, _MISSING)
                if raw is _MISSING or raw is None:
                    if is_required:
                        errors[f"{path}.{name}"] = "field is required"
                    else:
                        result[name] = default
                    continue
                try:
                    result[name] = validate(raw, f"{path}.{name}")
                except Invalid as exc:
                    errors[f"{path}.{name}"] = exc.message
            if not allow_extra:
                for extra in value.keys() - known:
                    errors[f"{path}.{extra}"] = "unknown field"
            if errors:
                raise ValidationError(**errors)
            return result

        return check


def compile_schema(schema: Obj) -> Validator:
    """Compile once at import time; call per request.

    Handler modules do this at module scope so the cost lands at start-up.
    """
    return schema.compile()
