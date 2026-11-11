"""Parallel graph/vector retrieval with mandatory local rules and strict scope."""

import asyncio
import logging
import math
from dataclasses import dataclass, field, replace

import httpx

from inkwise.contracts import Rubric
from inkwise.core import fuse_ranks
from inkwise.errors import DomainError, failure_category, scope_denied
from inkwise.provider_io import read_json_response, service_url
from inkwise.rubrics import digest
from inkwise.settings import Settings


@dataclass(frozen=True)
class Scope:
    tenant: str
    release: str
    language: str
    genre: str
    grade: int
    essay_hash: str
    rubric_id: str = ''


@dataclass
class Hit:
    id: str
    text: str
    metadata: dict
    route: str


@dataclass
class Bundle:
    rubric: Rubric
    examples: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    provenance: list[dict] = field(default_factory=list)


def in_scope(hit: Hit, scope: Scope) -> bool:
    m = hit.metadata
    return (m.get("tenantId") == scope.tenant and m.get("release") == scope.release
            and (not scope.rubric_id or m.get('rubricId')==scope.rubric_id)
            and m.get("languagePolicy") == scope.language and m.get("genre") == scope.genre
            and type(m.get("gradeMin")) is int and type(m.get("gradeMax")) is int
            and m["gradeMin"] <= scope.grade <= m["gradeMax"]
            and m.get("approved") is True
            and m.get("textHash") != scope.essay_hash
            and digest(hit.text) != scope.essay_hash)


class Embeddings:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts or len(texts) > 32 or any(len(t) > 2000 for t in texts):
            raise DomainError("EMBEDDING_INPUT_TOO_LARGE")
        from inkwise.provider_io import local_endpoint
        if not local_endpoint(self.settings.embedding_base_url):
            raise DomainError('LOCAL_EMBEDDING_ENDPOINT_REQUIRED')
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
            result = await read_json_response(client, service_url(self.settings.embedding_base_url, "embeddings"),
                    payload={"model": self.settings.embedding_model, "input": texts}, max_bytes=2_000_000)
        try:
            rows = sorted(result["data"], key=lambda row: row["index"])
            if [row["index"] for row in rows] != list(range(len(texts))):
                raise ValueError("Wrong embedding indexes")
            vectors = [row["embedding"] for row in rows]
            if any(not isinstance(v, list) or len(v) != 1024
                   or any(type(n) not in (float, int) or not math.isfinite(n) for n in v) for v in vectors):
                raise ValueError("Expected BGE-M3 1024-dimensional vectors")
            return vectors
        except (KeyError, TypeError, ValueError):
            raise DomainError("EMBEDDING_INVALID_RESPONSE", 502) from None


class GraphRetriever:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def query(self, scope: Scope, rule_ids: list[str], rubric: Rubric) -> list[Hit]:
        if not self.settings.neo4j_uri:
            raise DomainError("GRAPH_NOT_CONFIGURED")
        from neo4j import AsyncGraphDatabase, Query
        auth = ((self.settings.neo4j_user, self.settings.neo4j_password.get_secret_value())
                if self.settings.neo4j_user else None)
        async with AsyncGraphDatabase.driver(self.settings.neo4j_uri, auth=auth,
                                              connection_timeout=3, max_connection_pool_size=4,
                                              notifications_min_severity="OFF") as driver:
            async with driver.session(default_access_mode="READ") as session:
                from inkwise.graph_rules import verify_graph_rules
                await verify_graph_rules(session,scope.tenant,rubric)
                result = await session.run(Query("""
                    MATCH (r:Criterion {tenantId: $tenant, release: $release,rubricId:$rubric})
                    WHERE r.id IN $rule_ids
                    MATCH p=(r)-[:EXEMPLIFIED_BY|REQUIRES*1..3]->(e:Evidence)
                    WHERE all(n IN nodes(p) WHERE n.tenantId=$tenant AND n.release=$release)
                      AND e.languagePolicy=$language AND e.genre=$genre
                      AND e.gradeMin <= $grade AND e.gradeMax >= $grade AND e.approved=true
                    RETURN DISTINCT e.id AS id, e.text AS text, properties(e) AS metadata, length(p) AS hops
                    ORDER BY hops, id LIMIT 20
                """, timeout=3), tenant=scope.tenant, release=scope.release, rule_ids=rule_ids,
                    language=scope.language, genre=scope.genre, grade=scope.grade,rubric=rubric.id)
                return [Hit(row["id"], row["text"], dict(row["metadata"]), "graph") async for row in result]


class VectorRetriever:
    def __init__(self, settings: Settings, embeddings: Embeddings):
        self.settings, self.embeddings = settings, embeddings

    async def query(self, scope: Scope, query: str) -> list[Hit]:
        if not self.settings.chroma_host:
            raise DomainError("VECTOR_NOT_CONFIGURED")
        vectors = await self.embeddings.embed([query[:2000]])
        from inkwise.chroma_store import ChromaStore
        async with ChromaStore(self.settings.chroma_host, self.settings.chroma_port, self.settings.chroma_ssl) as store:
            result = await store.query(vectors[0],
                where={"$and": [{"tenantId": scope.tenant}, {"release": scope.release},
                              {"languagePolicy": scope.language}, {"genre": scope.genre}, {"rubricId":scope.rubric_id},
                              {"gradeMin": {"$lte": scope.grade}}, {"gradeMax": {"$gte": scope.grade}},
                              {"approved": True}]})
        hits = []
        for identifier, document, metadata in zip(result["ids"][0], result["documents"][0], result["metadatas"][0]):
            if isinstance(document, str) and isinstance(metadata, dict):
                hits.append(Hit(identifier, document, metadata, "vector"))
        return hits


class HybridRetriever:
    def __init__(self, settings: Settings, graph=None, vector=None, case_validator=None):
        self.graph = graph or GraphRetriever(settings)
        self.vector = vector or VectorRetriever(settings, Embeddings(settings))
        self.case_validator = case_validator

    async def retrieve(self, scope: Scope, rubric: Rubric, query: str) -> Bundle:
        if scope.rubric_id and scope.rubric_id!=rubric.id:raise DomainError('RULESET_SCOPE_MISMATCH')
        scope=replace(scope,rubric_id=rubric.id)
        if rubric.release != scope.release or rubric.language_policy != scope.language or rubric.genre != scope.genre:
            raise DomainError("RULESET_SCOPE_MISMATCH")
        if not rubric.grade_min<=scope.grade<=rubric.grade_max:raise DomainError('RULESET_SCOPE_MISMATCH')
        async def bounded(route, awaitable):
            try:
                async with asyncio.timeout(8):
                    return route, await awaitable
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if scope_denied(error): raise
                logging.getLogger(__name__).warning('retrieval.%s degraded category=%s', route, failure_category(error))
                return route, None
        tasks = [asyncio.create_task(bounded("graph", self.graph.query(scope, [rule.id for rule in rubric.rules],rubric))),
                 asyncio.create_task(bounded("vector", self.vector.query(scope, query)))]
        try:
            results = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        bundle = Bundle(rubric=rubric)
        teacher_hits=[hit for _,hits in results if hits for hit in hits[:20]
                      if hit.metadata.get("sourceKind") in ("teacher-case","curriculum-reference")]
        allowed_cases=set()
        if teacher_hits and self.case_validator:
            try:
                allowed_cases=await self.case_validator(scope,teacher_hits)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if scope_denied(error): raise
                logging.getLogger(__name__).warning('retrieval.case_authorization degraded category=%s', failure_category(error))
                bundle.warnings.append("CASE_AUTHORIZATION_UNAVAILABLE")
        routes, documents, conflicts = [], {}, set()
        for route, hits in results:
            if hits is None:
                bundle.warnings.append(route.upper() + "_UNAVAILABLE_LOCAL_RULES_USED")
                routes.append([])
                continue
            ranked = []
            for hit in hits[:20]:
                if hit.metadata.get("sourceKind") in ("teacher-case","curriculum-reference") and hit.id not in allowed_cases:
                    bundle.warnings.append("REVOKED_OR_UNVERIFIED_CASE_REJECTED")
                    continue
                if not in_scope(hit, scope):
                    bundle.warnings.append("OUT_OF_SCOPE_EVIDENCE_REJECTED")
                    continue
                if hit.id in documents and documents[hit.id].text != hit.text:
                    conflicts.add(hit.id)
                    bundle.warnings.append("EVIDENCE_VERSION_CONFLICT")
                    continue
                documents[hit.id] = hit
                ranked.append(hit.id)
            routes.append(ranked)
        remaining = 6000
        fused=fuse_ranks(routes)
        # Prefer a spread of teacher-rated anchors where available, then preserve
        # reciprocal-rank ordering. No more than two chunks from one source case.
        selected=[]
        for band in ('low','middle','high'):
            found=next((pair for pair in fused if pair[0] not in conflicts and documents[pair[0]].metadata.get('anchorBand')==band),None)
            if found:selected.append(found)
        selected += [pair for pair in fused if pair not in selected]
        source_counts={}
        for identifier, score in selected:
            if identifier in conflicts or len(bundle.examples) >= 8:
                continue
            hit = documents[identifier]
            source=hit.metadata.get('source',identifier)
            if source_counts.get(source,0)>=2:continue
            if len(hit.text) > remaining:
                continue
            bundle.examples.append({"id": identifier, "text": hit.text, "source": hit.metadata.get("source", "reviewed-case")})
            bundle.provenance.append({"id": identifier, "rrf": score,
                        "ranks": [route.index(identifier) + 1 if identifier in route else None for route in routes]})
            remaining -= len(hit.text)
            source_counts[source]=source_counts.get(source,0)+1
        bundle.warnings = sorted(set(bundle.warnings))
        return bundle
