"""Stable public errors that never contain submitted text or secret values."""


class DomainError(Exception):
    def __init__(self, code: str, status: int = 409, details: dict | None = None):
        self.code = code
        self.status = status
        self.details = details
        super().__init__(code)


FAILURE_CATEGORIES = frozenset({'cancelled', 'timeout', 'permission', 'unavailable', 'contract', 'internal', 'outcome_unknown'})


def scope_denied(error: BaseException) -> bool:
    return isinstance(error, DomainError) and (error.status in (401, 403) or error.code in {
        'RULESET_SCOPE_MISMATCH', 'TENANT_SCOPE_MISMATCH', 'UNAUTHORIZED', 'FORBIDDEN'})


def failure_category(error: BaseException) -> str:
    """Return a fixed enum; exception messages and arbitrary codes never leave here."""
    import asyncio
    import json
    import httpx
    from pydantic import ValidationError
    if isinstance(error, asyncio.CancelledError): return 'cancelled'
    if isinstance(error, (TimeoutError, httpx.TimeoutException)): return 'timeout'
    if scope_denied(error) or isinstance(error, PermissionError): return 'permission'
    if isinstance(error, httpx.HTTPStatusError):
        status=error.response.status_code
        return 'permission' if status in (401,403) else 'unavailable' if status >= 500 or status == 429 else 'contract'
    if isinstance(error, (ConnectionError, OSError, httpx.TransportError)): return 'unavailable'
    if isinstance(error, (ValidationError, json.JSONDecodeError)): return 'contract'
    if isinstance(error, DomainError):
        if error.code in {'PROVIDER_RESULT_UNKNOWN_REQUIRES_REVIEW','CHUNK_WAIT_TIMEOUT_UNKNOWN'}: return 'outcome_unknown'
        if error.code in {'OCR_SECONDARY_NOT_CONFIGURED', 'GRAPH_NOT_CONFIGURED', 'VECTOR_NOT_CONFIGURED',
                          'OCR_NOT_CONFIGURED', 'MODEL_NOT_CONFIGURED'}: return 'unavailable'
        return 'contract'
    return 'internal'
