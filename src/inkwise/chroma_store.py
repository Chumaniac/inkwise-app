"""Minimal Chroma v2 HTTP operations with TLS verification and bounded lifetime."""

import json
from uuid import UUID

import httpx

from inkwise.errors import DomainError
from inkwise.provider_io import service_url


class ChromaStore:
    def __init__(self, host: str, port: int, ssl: bool = False):
        self.base = service_url(f"{'https' if ssl else 'http'}://{host}:{port}",
            "api/v2/tenants/default_tenant/databases/default_database/collections")
        self.client = httpx.AsyncClient(timeout=8, trust_env=False, follow_redirects=False)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.client.aclose()

    async def request(self, method: str, suffix: str = "", body: dict | None = None):
        try:
            async with self.client.stream(method, self.base + suffix, json=body) as response:
                if response.status_code >= 400:
                    raise DomainError("VECTOR_SERVICE_FAILED", 503)
                buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > 2_000_000:
                        raise DomainError("VECTOR_RESPONSE_TOO_LARGE", 502)
                return json.loads(buffer) if buffer else None
        except (httpx.HTTPError, ValueError):
            raise DomainError("VECTOR_SERVICE_UNAVAILABLE", 503) from None

    async def collection_id(self, *, create=False) -> str:
        if create:
            data = await self.request("POST", body={"name": "inkwise_cases", "get_or_create": True,
                                                    "metadata": {"application": "inkwise"}})
        else:
            data = await self.request("GET", "/inkwise_cases")
        try:
            return str(UUID(data["id"]))
        except (ValueError, TypeError, KeyError):
            raise DomainError("VECTOR_COLLECTION_INVALID", 502) from None

    async def query(self, vector: list[float], where: dict) -> dict:
        identifier = await self.collection_id()
        return await self.request("POST", f"/{identifier}/query", {"query_embeddings": [vector],
             "n_results": 20, "where": where, "include": ["documents", "metadatas", "distances"]})

    async def add(self, *, ids: list[str], embeddings: list, documents: list, metadatas: list):
        identifier = await self.collection_id(create=True)
        await self.request("POST", f"/{identifier}/upsert", {"ids":ids,"embeddings":embeddings,
                                                            "documents":documents,"metadatas":metadatas})

    async def delete(self, ids: list[str]):
        identifier = await self.collection_id()
        await self.request("POST", f"/{identifier}/delete", {"ids":ids})
