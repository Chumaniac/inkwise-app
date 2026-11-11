"""Operator-owned immutable knowledge bundles, index preparation and activation."""

import asyncio
import json
import os
import tempfile
import math
import re
from contextlib import contextmanager
from uuid import uuid4
from pathlib import Path

from sqlalchemy import select, update

from inkwise.chroma_store import ChromaStore
from inkwise.cache_storage import atomic_json
from inkwise.contracts import Rubric
from inkwise.database import Candidate, Identity, Job, JobEvent, create_session_factory, now
from inkwise.errors import DomainError
from inkwise.retrieval import Embeddings
from inkwise.rubrics import RubricRepository, canonical, digest, safe_segment
from inkwise.settings import Settings


MAX_BUNDLE_BYTES = 8 * 1024 * 1024


def read_bundle_text(path: Path) -> str:
    # The initial in-memory format is deliberately bounded; larger releases must split.
    with path.open("rb") as source:
        raw = source.read(MAX_BUNDLE_BYTES + 1)
    if len(raw) > MAX_BUNDLE_BYTES:
        raise DomainError("RELEASE_TOO_LARGE")
    return raw.decode("utf-8")


@contextmanager
def release_lock(settings, tenant):
    """Serialize file-pointer mutations and recover interrupted committed switches."""
    import fcntl
    folder = settings.rubric_dir / safe_segment(tenant)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (folder / ".release.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        pending = folder / "pending-activation.json"
        if pending.exists():
            operation = json.loads(pending.read_text())
            engine, sessions = create_session_factory(settings.database_url)
            try:
                with sessions() as db:
                    committed = db.scalar(select(JobEvent.id).where(
                        JobEvent.tenant_id == tenant, JobEvent.job_id == operation["id"],
                        JobEvent.event == "knowledge.activation_committed")) is not None
            finally:
                engine.dispose()
            target = operation["next"] if committed else operation["previous"]
            for entry in target.values():
                if digest((folder / safe_segment(entry["file"])).read_text()) != entry["sha256"]:
                    raise DomainError("RELEASE_RECOVERY_INTEGRITY_FAILED")
            atomic_json(folder / "active.json", target)
            pending.unlink()
        yield folder


def recover_release(settings, tenant):
    """Replay only a journal whose database commit marker exists; otherwise roll back."""
    with release_lock(settings, tenant):
        pass


def import_rubric(settings, tenant, path, **approval):
    with release_lock(settings, tenant):
        return _import_rubric(settings, tenant, path, **approval)


def _import_rubric(settings: Settings,tenant: str,path: Path,*,submitted_by: str,approved_by: str,source: str,activate: bool=True,approval_context: dict | None=None):
    """Operator-mediated import of a school's independently approved rule file."""
    safe_segment(tenant)
    if path.stat().st_size>262144 or not source.strip() or submitted_by==approved_by:
        raise DomainError("RULE_APPROVAL_REQUIRED")
    rubric=Rubric.model_validate_json(path.read_text())
    if rubric.synthetic:
        raise DomainError("OFFICIAL_RUBRIC_REQUIRED")
    engine,sessions=create_session_factory(settings.database_url)
    try:
        with sessions() as db:
            submitter=db.get(Identity,submitted_by);reviewer=db.get(Identity,approved_by)
            if (not submitter or not reviewer or not submitter.active or not reviewer.active
                or submitter.tenant_id!=tenant or reviewer.tenant_id!=tenant or reviewer.role not in ("reviewer","admin")):
                raise DomainError("RULE_APPROVAL_REQUIRED")
    finally:engine.dispose()
    folder=settings.rubric_dir/tenant
    filename=safe_segment(rubric.id+"-"+rubric.release+".json")
    raw=canonical(rubric.model_dump(mode="json",by_alias=True))
    destination=folder/filename
    if destination.exists() and digest(destination.read_text())!=digest(raw):
        raise DomainError("IMMUTABLE_RULE_CONFLICT")
    manifest=RubricRepository(settings.rubric_dir).active(tenant) if (folder/"active.json").exists() else {}
    atomic_json(destination,rubric.model_dump(mode="json",by_alias=True))
    atomic_json(folder/(filename+".approval.json"),{"submittedBy":submitted_by,"approvedBy":approved_by,
        "source":source,"approvedAt":now().isoformat(),"sha256":digest(raw),"workflow":"operator-recorded independent school approval","context":approval_context or {}})
    manifest[rubric.id]={"file":filename,"sha256":digest(raw)}
    if activate:atomic_json(folder/"active.json",manifest)
    return rubric.id


def compatible_anchor(source: dict,target: dict,grade: int) -> bool:
    """Trait ratings survive a release/aggregate-weight change, not new criteria."""
    left,right=Rubric.model_validate(source),Rubric.model_validate(target)
    if not right.grade_min<=grade<=right.grade_max:return False
    excluded={'release','title','grade_min','grade_max','weights'}
    return left.model_dump(exclude=excluded)==right.model_dump(exclude=excluded)


def export_bundle(settings: Settings, tenant: str, release: str) -> Path:
    """Export only independently approved, explicitly authorized candidates."""
    safe_segment(tenant); safe_segment(release)
    destination = settings.rubric_dir.parent / "artifacts" / tenant / release
    if destination.exists():
        raise DomainError("RELEASE_ALREADY_EXISTS")
    source_rules = RubricRepository(settings.rubric_dir).list(tenant)
    if not source_rules:
        raise DomainError("RULESET_UNAVAILABLE")
    rules = [Rubric.model_validate(r).model_copy(update={"release":release}).model_dump(mode="json",by_alias=True)
             for r in source_rules]
    engine, sessions = create_session_factory(settings.database_url)
    try:
        with sessions() as db:
            rows = db.execute(select(Candidate,Job).join(Job,Job.id==Candidate.job_id).where(
                Candidate.tenant_id==tenant,Job.tenant_id==tenant,Candidate.status.in_(["approved","published"]))
                .order_by(Candidate.id).execution_options(yield_per=50))
            by_id={r['id']:r for r in rules}
            cases, excluded_cases = [], []
            byte_count = len(canonical(rules).encode())
            for count, (candidate, job) in enumerate(rows, 1):
                if count > 10000:
                    raise DomainError("RELEASE_TOO_LARGE")
                if candidate.review.get("reuseScope") != "explicit-anonymized-v1":
                    excluded_cases.append({'id':candidate.id,'reason':'CASE_REQUIRES_REDACTION_REVIEW'})
                    continue
                if (candidate.rubric['id'] not in by_id or not compatible_anchor(
                        candidate.rubric,by_id[candidate.rubric['id']],job.payload['grade'])):
                    excluded_cases.append({'id':candidate.id,'reason':'RUBRIC_SEMANTICS_CHANGED'})
                    continue
                if not candidate.reviewed_by or candidate.reviewed_by == candidate.submitted_by:
                    continue
                case={"id":candidate.id,"text":candidate.text,"rubricId":candidate.rubric["id"],
                      "scores":candidate.review["scores"],"comment":candidate.review["comment"],
                      "feedback":candidate.review.get('feedback',{}),"grade":job.payload['grade'],
                      "provenance":candidate.provenance,"sourceReportVersion":candidate.report_version}
                byte_count += len(canonical(case).encode())
                if byte_count > MAX_BUNDLE_BYTES:
                    raise DomainError("RELEASE_TOO_LARGE")
                cases.append(case)
        bundle = {"schemaVersion":1,"tenantId":tenant,"release":release,"rubrics":rules,"cases":cases,
                  "embeddingModel":settings.embedding_model,"sourceCount":len(cases),'excludedCases':excluded_cases}
        if len(canonical(bundle).encode()) > MAX_BUNDLE_BYTES:
            raise DomainError("RELEASE_TOO_LARGE")
        destination.mkdir(parents=True,mode=0o700)
        atomic_json(destination/"bundle.json",bundle)
        return destination/"bundle.json"
    finally:
        engine.dispose()


def validate_bundle(settings: Settings, path: Path, *, raw: str | None = None) -> dict:
    """Reject edited, withdrawn, cross-tenant or never-reviewed source cases."""
    bundle=json.loads(read_bundle_text(path) if raw is None else raw)
    tenant,release=safe_segment(bundle["tenantId"]),safe_segment(bundle["release"])
    rubrics={r["id"]:Rubric.model_validate(r) for r in bundle["rubrics"]}
    if not rubrics or any(r.release!=release for r in rubrics.values()) or len(bundle["cases"])>10000:
        raise DomainError("RELEASE_INVALID")
    ids=[case["id"] for case in bundle["cases"]]
    if len(ids)!=len(set(ids)):
        raise DomainError("RELEASE_DUPLICATE_CASE")
    engine,sessions=create_session_factory(settings.database_url)
    try:
        with sessions() as db:
            rows=db.scalars(select(Candidate).where(Candidate.tenant_id==tenant,Candidate.id.in_(ids))).all() if ids else []
            candidates={c.id:c for c in rows}
            for case in bundle["cases"]:
                current=candidates.get(case["id"])
                source_job=db.get(Job,current.job_id) if current else None
                if (not current or current.status not in ("approved","published")
                    or current.review.get("reuseScope") != "explicit-anonymized-v1"
                    or not current.reviewed_by or current.reviewed_by==current.submitted_by
                    or current.report_version!=case.get("sourceReportVersion")
                    or current.text!=case.get("text") or current.provenance!=case.get("provenance")
                    or current.review.get("scores")!=case.get("scores")
                    or current.review.get("comment")!=case.get("comment")
                    or current.review.get('feedback',{})!=case.get('feedback',{})
                    or not source_job or source_job.tenant_id!=tenant or source_job.classroom_id!=current.classroom_id
                    or source_job.payload.get('grade')!=case.get('grade')
                    or current.rubric["id"]!=case.get("rubricId") or case["rubricId"] not in rubrics):
                    raise DomainError("RELEASE_CASE_AUTHORIZATION_INVALID")
                if not compatible_anchor(current.rubric,rubrics[case['rubricId']].model_dump(by_alias=True),case['grade']):
                    raise DomainError('RELEASE_RUBRIC_INCOMPATIBLE')
        return bundle
    finally:
        engine.dispose()


def reference_chunks(case: dict):
    """Index complete case text and teacher-selected feedback under a hard budget."""
    teacher={dimension:value for dimension,value in case.get('feedback',{}).items() if value.get('include')}
    blocks=[('essay',case['text']),('teacher-feedback',canonical({'comment':case['comment'],'feedback':teacher}))]
    for kind,content in blocks:
        start=0
        while start<len(content):
            end=min(start+1400,len(content))
            def serialize(stop):
                return canonical({'contentKind':kind,'text':content[start:stop],'teacherBands':case['scores'],
                    'essayContext':case['text'][:120] if kind=='teacher-feedback' else ''})
            while end>start and len(serialize(end))>2000:
                end=start+(end-start)//2
            if end==start:raise DomainError('REFERENCE_CONTEXT_TOO_LARGE')
            yield kind,start,end,serialize(end)
            start=end


async def index_bundle(settings: Settings, path: Path, embeddings=None) -> dict:
    """Build inactive namespaces first; promotion is a separate operator step."""
    from neo4j import AsyncGraphDatabase
    raw=read_bundle_text(path); bundle=validate_bundle(settings,path,raw=raw)
    tenant,release=safe_segment(bundle["tenantId"]),safe_segment(bundle["release"])
    if not settings.neo4j_uri or not settings.chroma_host:
        raise DomainError("RETRIEVAL_NOT_CONFIGURED")
    rubrics={r["id"]:Rubric.model_validate(r) for r in bundle["rubrics"]}
    def entries():
        for case in bundle["cases"]:
            rubric=rubrics[case["rubricId"]]
            text=case["text"]
            mean_band=sum(case['scores'].values())/len(case['scores'])
            anchor_band='low' if mean_band<1.5 else 'high' if mean_band>=3 else 'middle'
            for kind,start,end,document in reference_chunks(case):
                yield {"id":f"{release}:{case['id']}:{kind}:{start}","text":document,"rubricId":rubric.id,
                    "rules":[rule.id for rule in rubric.rules],"metadata":{"tenantId":tenant,"release":release,
                        "languagePolicy":rubric.language_policy,"genre":rubric.genre,'rubricId':rubric.id,'anchorBand':anchor_band,"gradeMin":case['grade'],
                        "gradeMax":case['grade'],"approved":True,"textHash":digest(text),"source":case["id"],
                        "contentKind":kind,"start":start,"end":end,
                        "sourceKind":"teacher-case","sourceReportVersion":case["sourceReportVersion"]}}
    embedder=embeddings or Embeddings(settings)
    auth=(settings.neo4j_user,settings.neo4j_password.get_secret_value()) if settings.neo4j_user else None
    async with AsyncGraphDatabase.driver(settings.neo4j_uri,auth=auth) as driver:
        async with driver.session() as session:
            from inkwise.graph_rules import store_rules
            for rubric in rubrics.values():
                await store_rules(session,tenant,rubric)
            async with ChromaStore(settings.chroma_host,settings.chroma_port,settings.chroma_ssl) as chroma:
                from itertools import islice
                iterator=iter(entries())
                indexed_count=0
                while batch := list(islice(iterator,16)):
                    indexed_count += len(batch)
                    vectors=await embedder.embed([e["text"] for e in batch])
                    await chroma.add(ids=[e["id"] for e in batch],embeddings=vectors,
                                     documents=[e["text"] for e in batch],metadatas=[e["metadata"] for e in batch])
                    for entry in batch:
                        await session.run("MERGE (e:Evidence {tenantId:$tenant,release:$release,id:$id}) SET e += $props "
                            "WITH e UNWIND $rules AS ruleId MATCH (r:Criterion {tenantId:$tenant,release:$release,rubricId:$rubric,id:ruleId}) "
                            "MERGE (r)-[:EXEMPLIFIED_BY]->(e)",tenant=tenant,release=release,id=entry["id"],
                            props={**entry["metadata"],"text":entry["text"]},rules=entry["rules"],rubric=entry["rubricId"])
    record={"bundleHash":digest(raw),"tenantId":tenant,"release":release,"indexedCases":indexed_count,"sourceCases":len(bundle["cases"]),
            "embeddingModel":settings.embedding_model,"state":"indexed-not-active"}
    atomic_json(path.parent/"indexed.json",record)
    return record


def validate_evaluation(evaluation, bundle, bundle_hash):
    if not isinstance(evaluation, dict):
        raise DomainError("RELEASE_EVALUATION_REQUIRED")
    if evaluation.get("bundleHash") != bundle_hash:
        raise DomainError("RELEASE_HASH_MISMATCH")
    if (evaluation.get("decision") != "approved" or evaluation.get("tenantId") != bundle["tenantId"]
            or evaluation.get("release") != bundle["release"]
            or not isinstance(evaluation.get("modelRevision"), str) or not evaluation["modelRevision"].strip()
            or not isinstance(evaluation.get("labelFileHash"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", evaluation["labelFileHash"])):
        raise DomainError("RELEASE_EVALUATION_REQUIRED")
    metrics = evaluation.get("metrics")
    if not isinstance(metrics, dict) or not isinstance(metrics.get("overall"), dict):
        raise DomainError("RELEASE_EVALUATION_REQUIRED")
    values = metrics["overall"]
    def finite(value, low, high):
        return type(value) in (float,int) and math.isfinite(value) and low <= value <= high
    if (type(values.get("count")) is not int or values["count"] < 2
            or not finite(values.get("mae"),0,4) or not finite(values.get("adjacentAgreement"),0,1)
            or "qwk" not in values or (values["qwk"] is not None and not finite(values["qwk"],-1,1))):
        raise DomainError("RELEASE_EVALUATION_REQUIRED")


def activate_bundle(settings: Settings, path: Path, evaluation_path: Path) -> None:
    """Commit approval with a recovery journal, then atomically switch the pointer."""
    raw=read_bundle_text(path)
    bundle=validate_bundle(settings,path,raw=raw)
    indexed=json.loads(read_bundle_text(path.parent/"indexed.json"))
    evaluation=json.loads(read_bundle_text(evaluation_path))
    tenant,release=safe_segment(bundle["tenantId"]),safe_segment(bundle["release"])
    if (not isinstance(indexed,dict) or indexed.get("bundleHash")!=digest(raw) or indexed.get("tenantId")!=tenant
            or indexed.get("release")!=release or indexed.get("embeddingModel")!=bundle["embeddingModel"]
            or indexed.get("state")!="indexed-not-active"):
        raise DomainError("RELEASE_HASH_MISMATCH")
    validate_evaluation(evaluation,bundle,digest(raw))
    with release_lock(settings, tenant) as folder:
        # Revalidate after acquiring the mutation lock; consent is checked again in SQL below.
        validate_bundle(settings,path,raw=raw)
        current=RubricRepository(settings.rubric_dir).active(tenant) if (folder/"active.json").exists() else {}
        new_manifest={}
        for content in bundle["rubrics"]:
            rubric=Rubric.model_validate(content)
            filename=safe_segment(rubric.id+"-"+release+".json")
            destination=folder/filename
            if destination.exists() and digest(destination.read_text())!=digest(canonical(content)):
                raise DomainError("IMMUTABLE_RULE_CONFLICT")
            atomic_json(destination,content)
            new_manifest[rubric.id]={"file":filename,"sha256":digest(canonical(content))}
        operation_id=str(uuid4())
        engine,sessions=create_session_factory(settings.database_url)
        try:
            with sessions.begin() as db:
                reviewer=db.get(Identity,evaluation.get("reviewer"))
                if not reviewer or not reviewer.active or reviewer.tenant_id!=tenant or reviewer.role not in ("reviewer","admin"):
                    raise DomainError("RELEASE_REVIEWER_INVALID")
                if current==new_manifest:
                    return
                for case in bundle["cases"]:
                    candidate=db.scalar(select(Candidate).where(Candidate.id==case["id"],Candidate.tenant_id==tenant).with_for_update())
                    source=db.get(Job,candidate.job_id) if candidate else None
                    if (not candidate or candidate.status not in ("approved","published")
                            or candidate.review.get("reuseScope") != "explicit-anonymized-v1"
                            or not source or source.tenant_id!=tenant or source.classroom_id!=candidate.classroom_id
                            or candidate.text!=case["text"] or candidate.report_version!=case["sourceReportVersion"]
                            or candidate.submitted_by==reviewer.id):
                        raise DomainError("RELEASE_CASE_AUTHORIZATION_INVALID")
                    changed=db.execute(update(Candidate).where(
                        Candidate.id==candidate.id,Candidate.tenant_id==tenant,
                        Candidate.version==candidate.version,Candidate.status.in_(["approved","published"]),
                    ).values(status="published",version=candidate.version+1).execution_options(synchronize_session=False))
                    if changed.rowcount!=1:
                        raise DomainError("RELEASE_CASE_AUTHORIZATION_INVALID")
                    db.add(JobEvent(job_id=candidate.job_id,tenant_id=tenant,actor_id=reviewer.id,
                        event="candidate.published",job_version=candidate.report_version,
                        detail={"candidateId":candidate.id,"release":release}))
                backup=folder/"history"/(release+"-previous.json")
                if not backup.exists():atomic_json(backup,current)
                atomic_json(folder/"history"/(release+"-active.json"),new_manifest)
                atomic_json(folder/"pending-activation.json",{"id":operation_id,"previous":current,"next":new_manifest})
                db.add(JobEvent(job_id=operation_id,tenant_id=tenant,actor_id=reviewer.id,
                    event="knowledge.activation_committed",job_version=1,
                    detail={"release":release,"bundleHash":digest(raw),"modelRevision":evaluation["modelRevision"],
                            "labelFileHash":evaluation["labelFileHash"]}))
        finally:
            engine.dispose()
        atomic_json(folder/"active.json",new_manifest)
        (folder/"pending-activation.json").unlink()


def rollback_release(settings: Settings, tenant: str, release: str) -> None:
    with release_lock(settings,tenant) as folder:
        release=safe_segment(release)
        previous=json.loads((folder/"history"/(release+"-previous.json")).read_text())
        expected=json.loads((folder/"history"/(release+"-active.json")).read_text())
        current=RubricRepository(settings.rubric_dir).active(tenant)
        if current!=expected:
            raise DomainError("ROLLBACK_RELEASE_NOT_CURRENT")
        for entry in previous.values():
            if digest((folder/safe_segment(entry["file"])).read_text())!=entry["sha256"]:
                raise DomainError("ROLLBACK_INTEGRITY_FAILED")
        atomic_json(folder/"active.json",previous)
