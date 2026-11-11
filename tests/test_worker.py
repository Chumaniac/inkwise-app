import hashlib
import json
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from inkwise.api import create_app
from inkwise.cli import seed_demo
from inkwise.database import Base, CallReceipt, Candidate, Identity, Job, TenantQuota, create_session_factory, now
from inkwise.retrieval import HybridRetriever
from inkwise.settings import Settings
from inkwise.worker import ReceiptGateway, claim_job, finalize, process_job, recover_expired


class EmptyRoute:
    async def query(self, *args):
        return []


@pytest.fixture
def setup(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path}/db.sqlite", data_dir=tmp_path,
        rubric_dir=tmp_path / "rules", model_name="synthetic-http-model", model_base_url="http://provider.invalid/v1")
    engine, sessions = create_session_factory(settings.database_url)
    Base.metadata.create_all(engine)
    seed_demo(settings, "a")
    with sessions.begin() as db:
        db.add(TenantQuota(tenant_id="a"))
        db.add(Identity(id="teacher", tenant_id="a", role="teacher", classroom_ids=["c"],
                        token_hash=hashlib.sha256(b"synthetic-token").hexdigest()))
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer synthetic-token"
        payload = {"classroomId": "c", "title": "Story", "prompt": "Write a story", "text": "A synthetic story.",
                   "grade": 7, "languagePolicy": "en", "genre": "short-text", "rubricId": "english-demo"}
        job = client.post("/api/v1/jobs", json=payload, headers={"Idempotency-Key": "one"}).json()
        client.post(f"/api/v1/jobs/{job['id']}/text-confirmations", json={"version": 1, "text": payload["text"]})
        yield settings, sessions, client, job["id"]
    engine.dispose()


def completion(request):
    body = json.loads(request.content)
    data = json.loads(body["messages"][1]["content"])
    if "initialScores" in data:
        result = {"challenges": [], "summary": "Synthetic reflection finds no conflict."}
    else:
        quote = data["essay"][:80]
        result = {"dimension": data["dimension"], "band": 3, "reason": "Synthetic testing explanation.",
            "evidence": [{"start": 0, "end": len(quote), "quote": quote, "ruleId": data["rules"][0]["id"]}],
            "suggestion": "Add a concrete detail."}
    return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]})


@pytest.mark.asyncio
async def test_full_persisted_workflow_and_teacher_confirmation(setup):
    settings, sessions, client, identifier = setup
    job = claim_job(sessions, settings)
    assert job.id == identifier
    async with httpx.AsyncClient(transport=httpx.MockTransport(completion)) as transport:
        gateway = ReceiptGateway(settings, sessions, job, transport)
        await process_job(job, settings, sessions, gateway, HybridRetriever(settings, EmptyRoute(), EmptyRoute()))
    report = client.get(f"/api/v1/jobs/{identifier}").json()
    assert report["status"] == "teacher_review"
    assert report["result"]["total"] == 75
    assert report["result"]["requiresTeacherReview"] is True
    review = client.post(f"/api/v1/jobs/{identifier}/reviews", json={"version": report["version"],
        "scores": dict.fromkeys(["content", "coherence", "expression", "vocabulary", "grammar"], 3),
        "comment": "Teacher verified this synthetic report."})
    assert review.status_code == 200 and review.json()["status"] == "approved"
    assert review.json()["review"]["feedback"] == {}
    assert review.json()["review"]["feedbackConfirmed"] is False
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(CallReceipt)) == 6
    approved = review.json()
    candidate = client.post(f"/api/v1/jobs/{identifier}/candidates", json={"version": approved["version"],
        "anonymizedText": "An anonymized synthetic story.", "reuseAuthorized": True,
        "provenance": "Synthetic test authorship"})
    assert candidate.status_code == 201
    assert client.post(f"/api/v1/jobs/{identifier}/text-confirmations",json={"version":approved["version"],"text":"Change approved text"}).status_code==409
    candidate_id=candidate.json()['id']
    with sessions.begin() as db:
        db.get(Identity,"teacher").role="reviewer"
        db.add(Identity(id="reviewer",tenant_id="a",role="reviewer",classroom_ids=["c"],token_hash=hashlib.sha256(b"synthetic-reviewer").hexdigest()))
    decision={"version":1,"decision":"approve","reason":"Independent synthetic review"}
    own=client.post(f"/api/v1/candidates/{candidate_id}/decisions",json=decision)
    assert own.status_code==403 and own.json()['error']['code']=='INDEPENDENT_REVIEWER_REQUIRED'
    other=client.post(f"/api/v1/candidates/{candidate_id}/decisions",json=decision,headers={"Authorization":"Bearer synthetic-reviewer"})
    assert other.status_code==200
    with sessions() as db:assert db.get(Candidate,candidate_id).decision_reason==decision['reason']
    from inkwise.worker import load_allowed_cases
    from inkwise.retrieval import Hit,Scope
    from inkwise.contracts import Rubric
    from inkwise.rubrics import digest
    from dataclasses import replace
    anchor=Hit('reference','Synthetic excerpt',{'source':candidate_id,'sourceReportVersion':approved['version'],
                                              'textHash':digest('An anonymized synthetic story.')},'test')
    scope=Scope('a','synthetic-v1','en','short-text',7,'other-essay','english-demo')
    rubric=Rubric.model_validate(report['rubric'])
    assert load_allowed_cases(sessions,scope,rubric,[anchor])=={'reference'}
    assert load_allowed_cases(sessions,replace(scope,grade=8),rubric,[anchor])==set()
    assert load_allowed_cases(sessions,replace(scope,rubric_id='different-standard'),rubric,[anchor])==set()
    assert client.post(f"/api/v1/candidates/{candidate_id}/withdraw",json={"version":1}).status_code == 409
    assert client.post(f"/api/v1/candidates/{candidate_id}/withdraw",json={"version":2}).status_code == 200
    assert client.post(f"/api/v1/candidates/{candidate.json()['id']}/withdraw",json={"version":1}).status_code == 409
    with sessions.begin() as db:db.get(Identity,'teacher').role='admin'
    erased=client.post(f'/api/v1/jobs/{identifier}/erasures',json={'version':approved['version']})
    assert erased.status_code==200 and erased.json()['exportedStorageCleanupRequired'] is True


def test_cancelled_job_rejects_late_worker_result(setup):
    settings, sessions, client, identifier = setup
    job = claim_job(sessions, settings)
    response = client.post(f"/api/v1/jobs/{identifier}/cancel", json={"version": job.version})
    assert response.status_code == 200
    assert not finalize(sessions, job, status="teacher_review", result={"fake": True})
    assert client.get(f"/api/v1/jobs/{identifier}").json()["status"] == "cancelled"


def test_expired_worker_requires_explicit_recovery(setup):
    settings, sessions, client, identifier = setup
    job = claim_job(sessions, settings)
    with sessions.begin() as db:
        db.get(Job, identifier).lease_until = now() - timedelta(seconds=1)
    recover_expired(sessions)
    result = client.get(f"/api/v1/jobs/{identifier}").json()
    assert result["status"] == "failed" and result["errorCode"] == "WORKER_INTERRUPTED"


@pytest.mark.asyncio
async def test_unknown_receipt_never_reissues_provider_call(setup):
    settings, sessions, client, identifier = setup
    job = claim_job(sessions, settings)
    calls = []
    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("synthetic")
    from inkwise.contracts import DimensionScore
    from inkwise.providers import CallBudget
    from inkwise.errors import DomainError
    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as transport:
        gateway = ReceiptGateway(settings, sessions, job, transport)
        for _ in range(2):
            with pytest.raises(DomainError):
                await gateway.complete(DimensionScore, "same task", {"dimension": "content"}, CallBudget(2))
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_operator_reconciliation_preserves_attempt_budget(setup):
    settings,sessions,client,identifier=setup
    job=claim_job(sessions,settings)
    from inkwise.contracts import DimensionScore
    from inkwise.providers import CallBudget
    from inkwise.errors import DomainError
    from inkwise.recovery import resolve_receipt
    with sessions.begin() as db:
        row=CallReceipt(id="unknown-synthetic",tenant_id="a",job_id=identifier,input_hash="hash",stage="DimensionScore:content",status="unknown")
        db.add(row)
    finalize(sessions,job,status="failed",error_code="PROVIDER_TIMEOUT_UNKNOWN")
    with pytest.raises(DomainError):
        resolve_receipt(sessions,"unknown-synthetic",action="retry",actor="operator",reason="checked",provider_checked=False)
    resolve_receipt(sessions,"unknown-synthetic",action="retry",actor="operator",reason="Provider confirmed no completed output",provider_checked=True)
    with sessions() as db:
        row=db.get(CallReceipt,"unknown-synthetic")
        assert row.status=="retry_authorized" and row.attempt==1 and len(row.resolutions)==1


@pytest.mark.asyncio
async def test_expired_lease_blocks_provider_and_late_result_before_reaper(setup):
    from inkwise.contracts import DimensionScore
    from inkwise.errors import DomainError
    from inkwise.providers import CallBudget
    settings, sessions, client, identifier = setup
    job = claim_job(sessions, settings)
    with sessions.begin() as db:
        db.get(Job, identifier).lease_until = now() - timedelta(seconds=1)
    def never_call(request):
        pytest.fail('Expired lease sent a model request')
    async with httpx.AsyncClient(transport=httpx.MockTransport(never_call)) as transport:
        gateway = ReceiptGateway(settings, sessions, job, transport)
        with pytest.raises(DomainError, match='JOB_CANCELLED'):
            await gateway.complete(DimensionScore, 'task', {'dimension':'content'}, CallBudget(1))
    assert not finalize(sessions, job, status='teacher_review', result={'invalid':'late'})
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(CallReceipt)) == 0


@pytest.mark.asyncio
async def test_expired_parent_lease_does_not_start_chunk_work(setup):
    from inkwise.chunks import prepare_chunks, process_chunk
    from inkwise.database import ChunkTask
    settings,sessions,client,identifier=setup
    job=claim_job(sessions,settings)
    settings.model_calls_per_job=40
    prepare_chunks(sessions,job,settings)
    with sessions.begin() as db:
        db.get(Job,identifier).lease_until=now()-timedelta(seconds=1)
        chunk_id=db.scalar(select(ChunkTask.id))
    def never_start(job):pytest.fail('Expired parent lease started a chunk gateway')
    await process_chunk(chunk_id,settings,sessions,never_start)
    with sessions() as db:assert db.get(ChunkTask,chunk_id).status=='queued'


@pytest.mark.asyncio
async def test_lease_is_rechecked_after_waiting_for_model_capacity(setup):
    import asyncio
    from inkwise.contracts import DimensionScore
    from inkwise.errors import DomainError
    from inkwise.providers import CallBudget
    settings,sessions,client,identifier=setup
    job=claim_job(sessions,settings)
    def never_call(request):pytest.fail('Expired queued request reached provider')
    async with httpx.AsyncClient(transport=httpx.MockTransport(never_call)) as transport:
        gateway=ReceiptGateway(settings,sessions,job,transport)
        gateway.semaphore=asyncio.Semaphore(0)
        pending=asyncio.create_task(gateway.complete(DimensionScore,'task',{'dimension':'content'},CallBudget(1)))
        try:
            for _ in range(100):
                with sessions() as db:
                    reserved=db.scalar(select(func.count()).select_from(CallReceipt))
                if reserved:break
                await asyncio.sleep(.01)
            assert reserved==1
            with sessions.begin() as db:db.get(Job,identifier).lease_until=now()-timedelta(seconds=1)
            gateway.semaphore.release()
            with pytest.raises(DomainError,match='JOB_CANCELLED'):await pending
        finally:
            if not pending.done():pending.cancel()


@pytest.mark.asyncio
async def test_proofread_produces_no_grade_and_cannot_enter_scoring_cases(setup):
    from inkwise.chunks import ChunkFeedback
    from inkwise.contracts import Evidence
    from inkwise.rubrics import RubricRepository
    settings,sessions,client,identifier=setup
    with sessions.begin() as db:
        row=db.get(Job,identifier);row.payload={**row.payload,'intent':'proofread'}
    rubric=RubricRepository(settings.rubric_dir).get('a','english-demo')
    rule=next(rule for rule in rubric.rules if rule.dimension=='grammar')
    class Proofreader:
        def __init__(self):self.settings=settings;self.calls=0
        async def complete(self,*args):
            self.calls+=1
            return ChunkFeedback(findings=[{'dimension':'grammar','suggestion':'Synthetic check',
                'evidence':{'start':0,'end':1,'quote':'A','ruleId':rule.id}}])
    model=Proofreader();job=claim_job(sessions,settings)
    await process_job(job,settings,sessions,gateway=model)
    result=client.get(f'/api/v1/jobs/{identifier}').json()
    assert result['status']=='teacher_review' and result['result']['total'] is None and result['result']['scores']==[]
    assert model.calls==1
    approved=client.post(f'/api/v1/jobs/{identifier}/feedback-approval',json={'version':result['version'],'comment':'Teacher checked'})
    assert approved.status_code==200 and approved.json()['review']['findings']==[]
    rejected=client.post(f'/api/v1/jobs/{identifier}/candidates',json={'version':approved.json()['version'],'anonymizedText':'Example',
        'reuseAuthorized':True,'provenance':'synthetic'})
    assert rejected.status_code==409
    explanation=client.get(f'/api/v1/jobs/{identifier}/explanation').json()
    assert explanation['dimensions']==[] and explanation['generatedWithoutModelCall']


@pytest.mark.asyncio
async def test_non_language_text_never_reaches_model(setup):
    settings,sessions,client,identifier=setup
    with sessions.begin() as db:
        row=db.get(Job,identifier);row.text='!!! 😀';row.text_hash='synthetic-symbols'
    class NoModel:
        def __init__(self):self.settings=settings
        async def complete(self,*args):pytest.fail('Undetermined language reached scoring')
    await process_job(claim_job(sessions,settings),settings,sessions,gateway=NoModel())
    assert client.get(f'/api/v1/jobs/{identifier}').json()['errorCode']=='LANGUAGE_REQUIRES_TEACHER'

@pytest.mark.asyncio
async def test_worker_internal_failure_records_only_fixed_category(setup, monkeypatch, caplog):
    from types import SimpleNamespace
    import inkwise.worker
    from inkwise.database import JobEvent
    settings,sessions,client,identifier=setup
    job=claim_job(sessions,settings)
    def fail(*args, **kwargs): raise TypeError('SYNTHETIC_PRIVATE_WORKER_EXCEPTION')
    monkeypatch.setattr(inkwise.worker,'build_grading_graph',fail)
    await process_job(job,settings,sessions,SimpleNamespace(),HybridRetriever(settings,EmptyRoute(),EmptyRoute()))
    assert client.get(f'/api/v1/jobs/{identifier}').json()['errorCode']=='PROCESSING_FAILED'
    with sessions() as db:
        event=db.scalars(select(JobEvent).where(JobEvent.job_id==identifier,JobEvent.event=='failed')).one()
        assert event.detail=={'failureCategory':'internal'}
    assert 'SYNTHETIC_PRIVATE_WORKER_EXCEPTION' not in caplog.text
