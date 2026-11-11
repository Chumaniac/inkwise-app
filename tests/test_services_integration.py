"""Opt-in integration tests against this repository's disposable Compose stack.

Uses synthetic vectors and records, not a BGE-M3 or teaching-quality evaluation.
"""

import asyncio
import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(os.getenv("INKWISE_RUN_INTEGRATION") != "1", reason="Disposable services not requested")


def test_postgres_concurrent_admission_is_exactly_bounded(tmp_path):
    from sqlalchemy import create_engine, text
    from fastapi.testclient import TestClient
    from inkwise.api import create_app
    from inkwise.database import Base, Identity, TenantQuota, create_session_factory
    from inkwise.settings import Settings
    schema = "inkwise_test_" + uuid4().hex
    admin = create_engine("postgresql+psycopg://postgres@127.0.0.1:17654/inkwise_test")
    with admin.begin() as db:
        db.execute(text(f'CREATE SCHEMA "{schema}"'))
    url = f"postgresql+psycopg://postgres@127.0.0.1:17654/inkwise_test?options=-csearch_path%3D{schema}"
    engine, sessions = create_session_factory(url)
    try:
        Base.metadata.create_all(engine)
        with sessions.begin() as db:
            db.add(TenantQuota(tenant_id="synthetic"))
            db.add(Identity(id="teacher", tenant_id="synthetic", role="teacher", classroom_ids=["c"],
                            token_hash=hashlib.sha256(b"synthetic-integration-token").hexdigest()))
        settings = Settings(database_url=url, data_dir=tmp_path, rubric_dir=tmp_path, max_pending_per_tenant=5)
        with TestClient(create_app(settings)) as client:
            def submit(index):
                return client.post("/api/v1/jobs", headers={"Authorization":"Bearer synthetic-integration-token",
                    "Idempotency-Key":str(index)}, json={"classroomId":"c","title":"Synthetic","prompt":"Write",
                    "text":"Test", "grade":7,"languagePolicy":"en","genre":"short-text","rubricId":"not-loaded"}).status_code
            with ThreadPoolExecutor(max_workers=12) as pool:
                codes = list(pool.map(submit, range(20)))
            assert codes.count(201) == 5
            assert codes.count(429) == 15
    finally:
        engine.dispose()
        with admin.begin() as db:
            db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.mark.asyncio
async def test_neo4j_chroma_parallel_real_queries(tmp_path):
    from inkwise.chroma_store import ChromaStore
    from neo4j import AsyncGraphDatabase
    from inkwise.cli import seed_demo
    from inkwise.retrieval import GraphRetriever, HybridRetriever, Scope, VectorRetriever
    from inkwise.rubrics import RubricRepository
    from inkwise.settings import Settings
    tenant = "integration-" + uuid4().hex
    settings = Settings(rubric_dir=tmp_path, neo4j_uri="bolt://127.0.0.1:17687", chroma_host="127.0.0.1", chroma_port=18080)
    seed_demo(settings, tenant)
    rubric = RubricRepository(tmp_path).get(tenant, "english-demo")
    scope = Scope(tenant, rubric.release, "en", "short-text", 7, "unrelated-essay")
    metadata = {"tenantId":tenant,"release":rubric.release,"languagePolicy":"en","genre":"short-text","rubricId":rubric.id,
                "gradeMin":5,"gradeMax":12,"approved":True,"source":"synthetic-integration"}
    identifier = "case-" + uuid4().hex
    vector = [1.0] + [0.0] * 1023
    driver = AsyncGraphDatabase.driver(settings.neo4j_uri, auth=None)
    collection = ChromaStore("127.0.0.1", 18080)
    try:
        async with driver.session() as session:
            from inkwise.graph_rules import store_rules
            await store_rules(session,tenant,rubric)
            from inkwise.errors import DomainError
            with pytest.raises(DomainError):
                await store_rules(session,tenant,rubric.model_copy(update={"title":"Unversioned change"}))
            result=await session.run("MATCH (v:RubricRelease {tenantId:$tenant})-[p:HAS_DIMENSION]->() RETURN sum(p.weight) AS weight",tenant=tenant)
            assert abs((await result.single())["weight"]-1)<1e-8
            await session.run("MATCH (r:Criterion {id:$rule, tenantId:$tenant, release:$release,rubricId:$rubric}) "
                    "CREATE (e:Evidence) SET e=$props CREATE (r)-[:EXEMPLIFIED_BY]->(e)",
                rule=rubric.rules[0].id, tenant=tenant, release=rubric.release,rubric=rubric.id,
                props={**metadata,"id":identifier,"text":"Synthetic approved anchor"})
        await collection.add(ids=[identifier], embeddings=[vector], documents=["Synthetic approved anchor"], metadatas=[metadata])
        class SyntheticEmbedding:
            async def embed(self, texts):
                return [vector for _ in texts]
        retriever = HybridRetriever(settings, GraphRetriever(settings), VectorRetriever(settings, SyntheticEmbedding()))
        result = await retriever.retrieve(scope, rubric, "synthetic query")
        assert result.warnings == []
        assert result.examples[0]["id"] == identifier
        assert result.provenance[0]["ranks"] == [1,1]
    finally:
        await collection.delete(ids=[identifier])
        async with driver.session() as session:
            await session.run("MATCH (n {tenantId:$tenant}) DETACH DELETE n", tenant=tenant)
        await driver.close()
        await collection.client.aclose()


def test_sqlite_vec_real_extension_roundtrip(tmp_path):
    from inkwise.lexicon import build_index, nearest
    from inkwise.errors import DomainError
    path = tmp_path / "lexicon.sqlite"
    vectors = [[1.0]+[0.0]*1023, [0.0,1.0]+[0.0]*1022]
    build_index(path, [{"word":"synthetic-one"},{"word":"synthetic-two"}], vectors, "synthetic-source", "synthetic-embedding")
    assert nearest(path, vectors[0], model_revision="synthetic-embedding")[0]["entry"]["word"] == "synthetic-one"
    with pytest.raises(DomainError):
        nearest(path, vectors[0], model_revision="different-model")


@pytest.mark.asyncio
async def test_real_knowledge_release_build_activate_and_rollback(tmp_path):
    import json
    from neo4j import AsyncGraphDatabase
    from inkwise.chroma_store import ChromaStore
    from inkwise.cli import seed_demo
    from inkwise.database import Base, Candidate, Identity, Job, create_session_factory
    from inkwise.knowledge import export_bundle, index_bundle, activate_bundle, rollback_release, atomic_json, reference_chunks
    from inkwise.rubrics import RubricRepository, digest
    from inkwise.settings import Settings
    tenant='release-'+uuid4().hex
    settings=Settings(database_url=f'sqlite:///{tmp_path}/db.sqlite',rubric_dir=tmp_path/'rules',
        neo4j_uri='bolt://127.0.0.1:17687',chroma_host='127.0.0.1',chroma_port=18080)
    engine,sessions=create_session_factory(settings.database_url);Base.metadata.create_all(engine);seed_demo(settings,tenant)
    rubric=RubricRepository(settings.rubric_dir).get(tenant,'english-demo').model_dump(by_alias=True)
    identifier=str(uuid4())
    with sessions.begin() as db:
        db.add(Identity(id='reviewer',tenant_id=tenant,role='reviewer',classroom_ids=['c'],token_hash='synthetic'))
        db.add(Job(id='job',tenant_id=tenant,classroom_id='c',owner_id='teacher',idempotency_key='x',request_hash='x',payload={'grade':7}))
        db.add(Candidate(id=identifier,tenant_id=tenant,classroom_id='c',job_id='job',report_version=1,
            submitted_by='teacher',reviewed_by='reviewer',status='approved',text='A synthetic approved example.',
            provenance='Original synthetic integration fixture',rubric=rubric,
            review={'scores':{key:3 for key in rubric['weights']},'comment':'Synthetic teacher comment','feedback':{},'reuseScope':'explicit-anonymized-v1'}))
    bundle=export_bundle(settings,tenant,'v2');body=json.loads(bundle.read_text())
    ids=[f'v2:{identifier}:{kind}:{start}' for kind,start,end,text in reference_chunks(body['cases'][0])]
    class Encoder:
        async def embed(self,texts):return [[1.0]+[0.0]*1023 for _ in texts]
    driver=AsyncGraphDatabase.driver(settings.neo4j_uri,auth=None)
    try:
        record=await index_bundle(settings,bundle,Encoder())
        assert record['sourceCases']==1 and record['indexedCases']==len(ids)
        evaluation=tmp_path/'evaluation.json'
        atomic_json(evaluation,{'bundleHash':digest(bundle.read_text()),'decision':'approved','reviewer':'reviewer',
            'tenantId':tenant,'release':'v2','modelRevision':'synthetic-test-model','labelFileHash':'a'*64,
            'metrics':{'overall':{'count':5,'qwk':.8,'mae':.2,'adjacentAgreement':1.0}}})
        activate_bundle(settings,bundle,evaluation)
        assert RubricRepository(settings.rubric_dir).get(tenant,'english-demo').release=='v2'
        with sessions() as db:assert db.get(Candidate,identifier).status=='published'
        async with driver.session() as session:
            result=await session.run('MATCH (e:Evidence {tenantId:$tenant,release:"v2"}) RETURN count(e) AS count',tenant=tenant)
            assert (await result.single())['count']==len(ids)
        async with ChromaStore('127.0.0.1',18080) as chroma:
            result=await chroma.query([1.0]+[0.0]*1023,{'tenantId':tenant})
            assert set(result['ids'][0])==set(ids)
        rollback_release(settings,tenant,'v2')
        assert RubricRepository(settings.rubric_dir).get(tenant,'english-demo').release=='synthetic-v1'
    finally:
        async with ChromaStore('127.0.0.1',18080) as chroma:await chroma.delete(ids)
        async with driver.session() as session:await session.run('MATCH (n {tenantId:$tenant}) DETACH DELETE n',tenant=tenant)
        await driver.close();engine.dispose()


@pytest.mark.asyncio
async def test_public_reference_index_and_revocation(tmp_path):
    import json
    from inkwise.cli import seed_demo
    from inkwise.database import Base,create_session_factory
    from inkwise.reference import index_references,load_reference_pack,card_text
    from inkwise.retrieval import HybridRetriever,GraphRetriever,VectorRetriever,Scope
    from inkwise.worker import load_allowed_cases
    from inkwise.settings import Settings
    from inkwise.rubrics import RubricRepository
    from inkwise.chroma_store import ChromaStore
    from neo4j import AsyncGraphDatabase
    tenant='reference-'+uuid4().hex
    settings=Settings(database_url=f'sqlite:///{tmp_path}/db.sqlite',data_dir=tmp_path/'data',rubric_dir=tmp_path/'rules',
        neo4j_uri='bolt://127.0.0.1:17687',chroma_host='127.0.0.1',chroma_port=18080,embedding_revision='synthetic-fixed-v1')
    engine,sessions=create_session_factory(settings.database_url);Base.metadata.create_all(engine);seed_demo(settings,tenant)
    class Encoder:
        calls=0
        async def embed(self,texts):
            self.calls+=len(texts);return [[1.0]+[0.0]*1023 for _ in texts]
    encoder=Encoder();ids=[];driver=AsyncGraphDatabase.driver(settings.neo4j_uri,auth=None)
    try:
        result=await index_references(settings,tenant,encoder)
        initial=encoder.calls
        await index_references(settings,tenant,encoder)
        assert result['indexedReferences']>0 and encoder.calls==initial and initial<=30
        rubric=RubricRepository(settings.rubric_dir).get(tenant,'english-demo')
        scope=Scope(tenant,rubric.release,'en','short-text',7,'different',rubric.id)
        async def validate(scope,hits):return load_allowed_cases(sessions,scope,rubric,hits,settings.reference_pack)
        retriever=HybridRetriever(settings,GraphRetriever(settings),VectorRetriever(settings,encoder),validate)
        bundle=await retriever.retrieve(scope,rubric,'organising writing')
        assert bundle.examples and all('curriculum-reference-not-scored-example' in item['text'] for item in bundle.examples)
        async with driver.session() as session:
            result=await session.run('MATCH (e:Evidence {tenantId:$tenant}) RETURN e.id AS id',tenant=tenant)
            ids=[row['id'] async for row in result]
        pack,_=load_reference_pack(settings.reference_pack);raw=pack.model_dump(by_alias=True)
        for source in raw['sources']:source['enabled']=False
        settings.reference_pack=tmp_path/'disabled.json';settings.reference_pack.write_text(json.dumps(raw))
        empty=await retriever.retrieve(scope,rubric,'organising writing')
        assert empty.examples==[]
    finally:
        if ids:
            async with ChromaStore('127.0.0.1',18080) as chroma:await chroma.delete(ids)
        async with driver.session() as session:await session.run('MATCH (n {tenantId:$tenant}) DETACH DELETE n',tenant=tenant)
        await driver.close();engine.dispose()
