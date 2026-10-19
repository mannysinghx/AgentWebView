"""Stable error codes shared by the REST API, the MCP tools and the dashboard.

Retryability is reported separately from the code, with an optional bounded retry delay.
"""

from __future__ import annotations

import enum
from typing import Any


class ErrorCode(enum.StrEnum):
    NO_ELIGIBLE_ENGINE = "NO_ELIGIBLE_ENGINE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    NETWORK_BLOCKED = "NETWORK_BLOCKED"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    AUTH_SCOPE_DENIED = "AUTH_SCOPE_DENIED"
    SESSION_STATE_UNKNOWN = "SESSION_STATE_UNKNOWN"
    CAPABILITY_UNSUPPORTED = "CAPABILITY_UNSUPPORTED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    CSRF_FAILED = "CSRF_FAILED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    RATE_LIMITED = "RATE_LIMITED"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    CANCELED = "CANCELED"
    ENGINE_FAILURE = "ENGINE_FAILURE"
    QUALITY_FAILED = "QUALITY_FAILED"
    UNSUPPORTED_ENVIRONMENT = "UNSUPPORTED_ENVIRONMENT"
    DECISION_REQUIRED = "DECISION_REQUIRED"
    # A connected provider rejected the stored key, or reported its credits exhausted.
    PROVIDER_AUTH_FAILED = "PROVIDER_AUTH_FAILED"
    PROVIDER_CREDITS_EXHAUSTED = "PROVIDER_CREDITS_EXHAUSTED"
    INTERNAL = "INTERNAL"


HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.NO_ELIGIBLE_ENGINE: 422,
    ErrorCode.INSUFFICIENT_EVIDENCE: 422,
    ErrorCode.BUDGET_EXHAUSTED: 429,
    ErrorCode.NETWORK_BLOCKED: 403,
    ErrorCode.DEADLINE_EXCEEDED: 504,
    ErrorCode.AUTH_SCOPE_DENIED: 403,
    ErrorCode.SESSION_STATE_UNKNOWN: 409,
    ErrorCode.CAPABILITY_UNSUPPORTED: 422,
    ErrorCode.AUTH_REQUIRED: 401,
    ErrorCode.CSRF_FAILED: 403,
    ErrorCode.VALIDATION_FAILED: 422,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.PAYLOAD_TOO_LARGE: 413,
    ErrorCode.CANCELED: 409,
    ErrorCode.ENGINE_FAILURE: 502,
    ErrorCode.QUALITY_FAILED: 422,
    ErrorCode.UNSUPPORTED_ENVIRONMENT: 503,
    ErrorCode.DECISION_REQUIRED: 409,
    ErrorCode.PROVIDER_AUTH_FAILED: 502,
    ErrorCode.PROVIDER_CREDITS_EXHAUSTED: 429,
    ErrorCode.INTERNAL: 500,
}


class GatewayError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool = False,
        retry_after_ms: int | None = None,
        details: dict[str, Any] | None = None,
        remediation: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after_ms = retry_after_ms
        self.details = details or {}
        self.remediation = remediation

    @property
    def http_status(self) -> int:
        return HTTP_STATUS[self.code]

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
            "retry_after_ms": self.retry_after_ms,
        }
        if self.details:
            out["details"] = self.details
        if self.remediation:
            out["remediation"] = self.remediation
        return out


def not_found(what: str) -> GatewayError:
    return GatewayError(ErrorCode.NOT_FOUND, f"{what} not found")


def invalid(message: str) -> GatewayError:
    return GatewayError(ErrorCode.VALIDATION_FAILED, message)
