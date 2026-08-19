"""Configuration: one frozen dataclass, loaded once at start-up.

Reading ``os.environ`` at the point of use is how services end up impossible to
test and impossible to audit.  Everything is parsed and validated here, up
front, so a bad value fails at boot rather than on the first request that
happens to touch it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from typing import Any
from collections.abc import Mapping

ENV_PREFIX = "MARKETD_"


@dataclass(frozen=True, slots=True)
class Settings:
    # --- transport
    host: str = "127.0.0.1"
    port: int = 8080
    backlog: int = 512
    max_body_bytes: int = 256 * 1024
    max_header_bytes: int = 16 * 1024
    keepalive_timeout: float = 15.0
    read_timeout: float = 10.0
    shutdown_grace: float = 5.0

    # --- api
    api_secret: str = "dev-secret-do-not-ship"
    auth_cache_size: int = 4096
    auth_cache_ttl: float = 30.0
    rate_limit_rps: float = 200.0
    rate_limit_burst: float = 400.0
    route_cache_size: int = 2048
    default_page_size: int = 50
    max_page_size: int = 500

    # --- domain
    node_id: int = 1
    taker_fee_bps: int = 10
    maker_fee_bps: int = 2
    price_band_bps: int = 2_000
    max_open_orders: int = 500
    max_orders_per_second: float = 50.0
    book_snapshot_ttl: float = 0.05
    book_depth_default: int = 10

    # --- workers
    worker_concurrency: int = 4
    worker_queue_size: int = 4096
    worker_max_attempts: int = 3
    candle_interval: float = 60.0

    # --- observability
    log_level: str = "INFO"
    trace_buffer: int = 256

    def __post_init__(self) -> None:
        # 0 is legal and means "let the OS pick": the whole test suite binds
        # that way so parallel runs cannot collide on a fixed port.
        if not 0 <= self.port <= 65_535:
            raise ValueError(f"port out of range: {self.port}")
        if self.rate_limit_burst < self.rate_limit_rps:
            raise ValueError("rate_limit_burst must be >= rate_limit_rps")
        if self.max_page_size < self.default_page_size:
            raise ValueError("max_page_size must be >= default_page_size")
        if not 0 <= self.taker_fee_bps <= 10_000:
            raise ValueError("taker_fee_bps must be a sane basis-point value")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Build settings from ``MARKETD_*`` variables.

        The field's declared type drives the coercion, so adding a setting means
        adding one line to the dataclass and nothing else.
        """
        source = os.environ if env is None else env
        kwargs: dict[str, Any] = {}
        for field_def in fields(cls):
            raw = source.get(ENV_PREFIX + field_def.name.upper())
            if raw is None:
                continue
            kwargs[field_def.name] = _coerce(raw, field_def.type, field_def.name)
        return cls(**kwargs)

    def redacted(self) -> dict[str, Any]:
        """Safe to log: secrets are replaced, not truncated."""
        out = {}
        for field_def in fields(self):
            value = getattr(self, field_def.name)
            out[field_def.name] = "***" if "secret" in field_def.name else value
        return out


def _coerce(raw: str, declared: Any, name: str) -> Any:
    # ``from __future__ import annotations`` means field types arrive as strings.
    kind = declared if isinstance(declared, str) else getattr(declared, "__name__", "str")
    try:
        if kind == "int":
            return int(raw)
        if kind == "float":
            return float(raw)
        if kind == "bool":
            return raw.strip().lower() in ("1", "true", "yes", "on")
        return raw
    except ValueError as exc:
        raise ValueError(f"{ENV_PREFIX}{name.upper()}={raw!r} is not a valid {kind}") from exc
