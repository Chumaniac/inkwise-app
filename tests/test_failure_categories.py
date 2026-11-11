import asyncio
import httpx
import pytest
from inkwise.errors import DomainError, failure_category

@pytest.mark.parametrize(('error','expected'),[
    (TimeoutError('SYNTHETIC_PRIVATE'),'timeout'),
    (httpx.ConnectError('SYNTHETIC_PRIVATE'),'unavailable'),
    (PermissionError('SYNTHETIC_PRIVATE'),'permission'),
    (TypeError('SYNTHETIC_PRIVATE'),'internal'),
    (DomainError('OCR_INVALID_RESPONSE',502),'contract'),
    (DomainError('PROVIDER_RESULT_UNKNOWN_REQUIRES_REVIEW'),'outcome_unknown'),
    (DomainError('RULESET_SCOPE_MISMATCH',403),'permission'),
    (asyncio.CancelledError('SYNTHETIC_PRIVATE'),'cancelled'),
])
def test_only_fixed_failure_categories_leave_exception_boundary(error,expected):
    assert failure_category(error)==expected
