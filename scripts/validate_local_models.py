"""Opt-in local-only model check with durable success reuse and no cloud API.

Runs one synthetic English essay. This is a connectivity/protocol evaluation,
not a validation of handwriting accuracy or agreement with school teachers.
"""

import argparse
import asyncio
import json
import time
from pathlib import Path

import httpx
from neo4j import AsyncGraphDatabase
from sqlalchemy import select

from inkwise.chroma_store import ChromaStore
from inkwise.cli import seed_demo
from inkwise.contracts import JobCreate, Principal, TextConfirmation
from inkwise.database import Base, Job, TenantQuota, create_session_factory
from inkwise.knowledge import atomic_json
from inkwise.retrieval import Embeddings, HybridRetriever, Scope
from inkwise.rubrics import RubricRepository, digest
from inkwise.service import create_job, confirm_text
from inkwise.settings import Settings
from inkwise.worker import claim_job, process_job


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--retry-known-failure",action="store_true")
    args=parser.parse_args()
    root=Path("artifacts/local-model-check").resolve()
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    settings=Settings(database_url=f"sqlite:///{root}/state.sqlite",data_dir=root,rubric_dir=root/"rules",
        model_base_url="http://127.0.0.1:11434/v1",model_name="deepseek-r1:7b",model_api_key="",
        model_timeout_seconds=120,model_max_output_tokens=4096,model_concurrency=1,job_timeout_seconds=600,
        model_structured_output="json_schema",
        embedding_base_url="http://127.0.0.1:11434/v1",embedding_model="bge-m3",
        neo4j_uri="bolt://127.0.0.1:17687",neo4j_user="",neo4j_password="",
        chroma_host="127.0.0.1",chroma_port=18080)
    tenant="synthetic-local-model"
    if not (settings.rubric_dir/tenant/"active.json").exists():seed_demo(settings,tenant)
    rubric=RubricRepository(settings.rubric_dir).get(tenant,"english-demo")
    async with httpx.AsyncClient(trust_env=False,timeout=5) as client:
        tags=(await client.get("http://127.0.0.1:11434/api/tags")).json()["models"]
    model_digests={m["name"]:m["digest"] for m in tags if m["name"] in ("deepseek-r1:7b","bge-m3:latest")}
    atomic_json(root/"model-digests.json",model_digests)
    anchor="Synthetic reference: a short narrative clearly presents a helpful action and a personal reflection. A teacher should check the past tense and ask for a concrete detail."
    metadata={"tenantId":tenant,"release":rubric.release,"languagePolicy":"en","genre":"short-text","rubricId":rubric.id,
              "gradeMin":5,"gradeMax":12,"approved":True,"source":"original-synthetic-anchor","textHash":digest(anchor)}
    vector_path=root/"anchor-vector.json"
    if vector_path.exists():
        saved=json.loads(vector_path.read_text())
        if saved["modelDigests"].get("bge-m3:latest")!=model_digests.get("bge-m3:latest"):
            raise SystemExit("Embedding model changed; create a separately versioned check")
        vectors=saved["vectors"]
    else:
        vectors=await Embeddings(settings).embed([anchor])
        atomic_json(vector_path,{"vectors":vectors,"modelDigests":model_digests})
    async with ChromaStore(settings.chroma_host,settings.chroma_port) as store:
        await store.add(ids=["inkwise-local-anchor-v1"],embeddings=vectors,documents=[anchor],metadatas=[metadata])
    async with AsyncGraphDatabase.driver(settings.neo4j_uri,auth=None) as driver:
        async with driver.session() as db:
            from inkwise.graph_rules import store_rules
            await store_rules(db,tenant,rubric)
            await db.run("MERGE (r:Criterion {tenantId:$tenant,release:$release,rubricId:$rubric,id:$rule}) "
                         "MERGE (e:Evidence {tenantId:$tenant,release:$release,id:$id}) SET e += $props "
                         "MERGE (r)-[:EXEMPLIFIED_BY]->(e)",tenant=tenant,release=rubric.release,
                         rule=rubric.rules[0].id,rubric=rubric.id,id="inkwise-local-anchor-v1",props={**metadata,"text":anchor})
    scope=Scope(tenant,rubric.release,"en","short-text",7,"not-the-reference")
    retrieved=await HybridRetriever(settings).retrieve(scope,rubric,"A story about helping a boy find a kite.")
    atomic_json(root/"retrieval-summary.json",{"exampleCount":len(retrieved.examples),"warnings":retrieved.warnings,
        "provenance":retrieved.provenance,"embeddingDimensions":len(vectors[0]),"modelDigests":model_digests})
    if retrieved.warnings or not retrieved.examples:raise SystemExit("Real local RAG check failed; see private summary")
    engine,sessions=create_session_factory(settings.database_url);Base.metadata.create_all(engine)
    user=Principal(user_id="synthetic-teacher",tenant_id=tenant,role="teacher",classroom_ids=["test-class"])
    with sessions.begin() as db:
        if db.get(TenantQuota,tenant) is None:db.add(TenantQuota(tenant_id=tenant))
    essay="Last Saturday, I went to the park with my sister. We saw a little boy looking for his kite. My sister helped him find it behind a bench. He smiled and said thank you. I learned that helping someone can make an ordinary day special."
    with sessions() as db:
        job=create_job(db,user,JobCreate(classroom_id="test-class",title="A helpful afternoon",prompt="Write a short story about helping someone.",
            text=essay,grade=7,language_policy="en",genre="short-text",rubric_id="english-demo"),"one-durable-local-check",20)
        if job.status=="needs_text_review":
            job=confirm_text(db,user,job.id,TextConfirmation(version=job.version,text=essay),RubricRepository(settings.rubric_dir))
        elif job.status=="failed" and args.retry_known_failure:
            from inkwise.database import CallReceipt
            unknown=db.scalar(select(CallReceipt.id).where(CallReceipt.job_id==job.id,CallReceipt.status.in_(["started","unknown"])).limit(1))
            if unknown:raise SystemExit("Unknown provider result requires explicit reconciliation")
            job.status="queued";job.version+=1;db.commit()
    start=time.perf_counter()
    if job.status=="queued":
        claimed=claim_job(sessions,settings)
        await process_job(claimed,settings,sessions)
    with sessions() as db:
        job=db.get(Job,job.id)
        summary={"scope":"one synthetic essay, local distilled model, not school acceptance","status":job.status,
                 "errorCode":job.error_code,"elapsedSecondsThisRun":round(time.perf_counter()-start,2),
                 "modelDigests":model_digests,"report":job.result}
        atomic_json(root/"grading-summary.json",summary)
        print({k:v for k,v in summary.items() if k not in ("report","modelDigests")})
        if job.status!="teacher_review":raise SystemExit(1)
    engine.dispose()


if __name__=="__main__":asyncio.run(main())
