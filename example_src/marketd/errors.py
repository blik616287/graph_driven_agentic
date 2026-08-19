"""Error taxonomy shared by every layer.

The domain raises these; the API layer turns them into responses.  Keeping the
HTTP status *on the exception* means handlers never need a translation table,
and the transport layer never needs to import the domain.
"""

from __future__ import annotations

from typing import Any


class MarketError(Exception):
    """Base class for every expected failure.

    ``status`` and ``code`` are class attributes so subclasses read as a table
    of contents.  ``retryable`` tells the client SDK whether a retry could
    plausibly succeed - it is the only signal the circuit breaker trusts.
    """

    status: int = 500
    code: str = "internal_error"
    retryable: bool = False

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def payload(self) -> dict[str, Any]:
        """Render the wire representation of this error."""
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            body["details"] = self.details
        return {"error": body}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}({self.message!r}, status={self.status})"


class BadRequest(MarketError):
    status = 400
    code = "bad_request"


class Unauthorized(MarketError):
    status = 401
    code = "unauthorized"


class Forbidden(MarketError):
    status = 403
    code = "forbidden"


class NotFound(MarketError):
    status = 404
    code = "not_found"


class MethodNotAllowed(MarketError):
    status = 405
    code = "method_not_allowed"


class Conflict(MarketError):
    """Optimistic concurrency lost, or a uniqueness constraint was violated."""

    status = 409
    code = "conflict"


class PayloadTooLarge(MarketError):
    status = 413
    code = "payload_too_large"


class ValidationError(MarketError):
    """One or more fields failed schema validation.

    ``details`` maps a JSON path (``"order.price"``) to a human message so the
    caller can highlight the offending field without parsing prose.
    """

    status = 422
    code = "validation_failed"

    def __init__(self, message: str = "request validation failed", **fields: Any) -> None:
        super().__init__(message, **fields)


class RiskRejected(MarketError):
    """The order is well-formed but violates a trading limit."""

    status = 422
    code = "risk_rejected"


class InsufficientFunds(RiskRejected):
    code = "insufficient_funds"


class RateLimited(MarketError):
    status = 429
    code = "rate_limited"
    retryable = True

    def __init__(self, message: str, retry_after: float = 1.0, **details: Any) -> None:
        super().__init__(message, **details)
        self.retry_after = retry_after


class ServiceUnavailable(MarketError):
    status = 503
    code = "service_unavailable"
    retryable = True


class CircuitOpen(ServiceUnavailable):
    """Raised client-side: the breaker refused to make the call at all."""

    code = "circuit_open"
    retryable = True


class ProtocolError(MarketError):
    """Malformed HTTP.  Always fatal for the connection."""

    status = 400
    code = "protocol_error"
