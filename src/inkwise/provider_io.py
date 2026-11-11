"""Bounded HTTP transport and endpoint checks; no orchestration dependencies."""

import ipaddress
import json
from urllib.parse import urlparse
import httpx
from inkwise.errors import DomainError


def service_url(base: str, path: str) -> str:
    """Endpoints come only from operator config, never a student's request."""
    parsed = urlparse(base)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise DomainError("PROVIDER_NOT_CONFIGURED", 503)
    if parsed.query or parsed.fragment:
        raise DomainError("INVALID_PROVIDER_CONFIGURATION", 503)
    return base.rstrip("/") + "/" + path.lstrip("/")



async def read_json_response(client: httpx.AsyncClient, url: str, *, payload: dict,
                             headers: dict | None = None, max_bytes: int = 262144) -> dict:
    """No automatic retries: timed-out inference may still be in flight."""
    try:
        async with client.stream("POST", url, json=payload, headers=headers) as response:
            if response.status_code == 429:
                raise DomainError("PROVIDER_RATE_LIMITED", 503)
            if response.status_code >= 400:
                raise DomainError("PROVIDER_REQUEST_FAILED", 503)
            buffer = bytearray()
            async for chunk in response.aiter_bytes():
                buffer.extend(chunk)
                if len(buffer) > max_bytes:
                    raise DomainError("PROVIDER_RESPONSE_TOO_LARGE", 502)
            decoded = json.loads(buffer)
            if not isinstance(decoded, dict):
                raise DomainError("PROVIDER_INVALID_RESPONSE", 502)
            return decoded
    except httpx.TimeoutException:
        raise DomainError("PROVIDER_TIMEOUT_UNKNOWN", 504) from None
    except (httpx.HTTPError, json.JSONDecodeError, UnicodeDecodeError):
        raise DomainError("PROVIDER_UNAVAILABLE", 503) from None


def local_endpoint(url):
    host=urlparse(url).hostname
    if host in ('localhost','host.docker.internal'):return True
    try:return ipaddress.ip_address(host).is_private
    except (ValueError,TypeError):return False
