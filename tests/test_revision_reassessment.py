"""Synthetic linked reassessment, without live OCR or model requests."""
import copy
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from sqlalchemy import func, select

from test_api import auth, client
from test_writing_revisions import approved, begin
from inkwise.database import CallReceipt, Job, RevisionAssessment, WritingRevision
from inkwise.retrieval import HybridRetriever
from inkwise.worker import ReceiptGateway, claim_job, finalize, process_job
from test_worker import EmptyRoute, completion


def confirmed(client, approved, key="revision-one"):
    revision = begin(client, approved, key).json()
    url = f"/api/v1/jobs/{approved['id']}/revisions/{revision['id']}"
    response = client.patch(url, headers=auth(), json={"version": revision["version"],
        "text": "First synthetic sentence.\nA revised and clearer ending.",
        "resolutions": {item["id"]: "addressed" for item in revision["feedback"]}})
    assert response.status_code == 200
    response = client.post(url + "/confirmation", headers=auth(),
                           json={"version": response.json()["version"], "confirmed": True})
    assert response.status_code == 200
    return response.json(), url + "/reassessment"


def submit(client, revision, url, key="assessment-one", user="alice"):
    return client.post(url, headers={**auth(user), "Idempotency-Key": key},
                       json={"version": revision["version"], "confirmed": True})


def test_confirmed_revision_creates_one_frozen_independent_queued_job(client, approved):
    original = copy.deepcopy(approved)
    revision, url = confirmed(client, approved)
    response = submit(client, revision, url)
    assert response.status_code == 201
    data = response.json()
    assert data["job"]["status"] == "queued"
    assert data["job"]["id"] != approved["id"]
    assert data["job"]["textHash"] != approved["textHash"]
    assert data["job"]["rubricHash"] == revision["source"]["rubricHash"]
    assert submit(client, revision, url).json()["job"]["id"] == data["job"]["id"]
    assert submit(client, revision, url, key="second-click").json()["job"]["id"] == data["job"]["id"]
    assert client.get(f"/api/v1/jobs/{approved['id']}", headers=auth()).json() == original


def test_research_reviewer_cannot_approve_a_classroom_grade(client, approved):
    with client.app.state.sessions.begin() as db:
        job = db.get(Job, approved['id']); job.status = 'teacher_review'
    response = client.post(f"/api/v1/jobs/{approved['id']}/reviews", headers=auth('reader'),
        json={"version": approved['version'], "scores": dict.fromkeys(['content','coherence','expression','vocabulary','grammar'], 3), "comment": "Synthetic review"})
    assert response.status_code == 403


def test_confirmation_version_scope_and_idempotency_are_enforced(client, approved):
    draft = begin(client, approved).json()
    url = f"/api/v1/jobs/{approved['id']}/revisions/{draft['id']}/reassessment"
    assert submit(client, draft, url).status_code == 409
    revision, url = confirmed(client, approved, 'another-revision')
    for user in ['bob', 'eve']:
        assert submit(client, revision, url, user=user).status_code == 404
        assert client.get(url, headers=auth(user)).status_code == 404
    assert submit(client, revision, url, user='reader').status_code == 403
    assert submit(client, {**revision, 'version': 1}, url).status_code == 409
    assert client.post(url, headers={**auth(), 'Idempotency-Key': 'strict'}, json={'version': revision['version'], 'confirmed': 1}).status_code == 422
    assert submit(client, revision, url).status_code == 201
    third, third_url = confirmed(client, approved, 'third-revision')
    assert submit(client, third, third_url).json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'


def test_queue_admission_and_concurrent_clicks_create_only_one_child(client, approved):
    from test_api import create
    pending = create(client, 'occupy-queue').json()
    client.app.state.settings.max_pending_per_tenant = 1
    revision, url = confirmed(client, approved)
    assert submit(client, revision, url).status_code == 429
    with client.app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(RevisionAssessment)) == 0
    client.post(f"/api/v1/jobs/{pending['id']}/cancel", headers=auth(), json={'version': 1})
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda key: submit(client, revision, url, key), ['click-one', 'click-two']))
    assert [response.status_code for response in responses] == [201, 201]
    assert responses[0].json()['job']['id'] == responses[1].json()['job']['id']
    with client.app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(RevisionAssessment)) == 1


def test_child_inputs_remain_frozen_and_cancel_does_not_create_another_assessment(client, approved):
    revision, url = confirmed(client, approved); child = submit(client, revision, url).json()['job']
    snapshot = client.get('/api/v1/jobs/' + child['id'], headers=auth()).json()
    assert snapshot['rubric'] == approved['rubric']
    assert snapshot['payload']['routingPreference'] == 'local' and not snapshot['payload']['cloudConsent']
    assert not snapshot['payload']['experimentConsent'] and snapshot['payload']['maxCostMicrousd'] == 0
    assert client.post(f"/api/v1/jobs/{child['id']}/text-confirmations", headers=auth(), json={'version': child['version'], 'text': 'must not replace'}).json()['error']['code'] == 'REASSESSMENT_INPUT_FROZEN'
    cancelled = client.post(f"/api/v1/jobs/{child['id']}/cancel", headers=auth(), json={'version': child['version']}).json()
    again = submit(client, revision, url).json()
    assert again['job']['id'] == child['id'] and again['job']['status'] == 'cancelled' and again['status'] == 'cancelled'
    assert client.get(url, headers=auth()).json()['delta'] is None
    assert client.post(f"/api/v1/jobs/{child['id']}/retries", headers=auth(), json={'version': cancelled['version']}).status_code == 409


@pytest.mark.asyncio
async def test_existing_worker_review_and_bound_comparison_preserve_first_report(client, approved):
    revision, url = confirmed(client, approved); child = submit(client, revision, url).json()['job']
    settings = client.app.state.settings.model_copy(update={'model_name': 'synthetic-http-model', 'model_revision': 'synthetic-test-revision', 'model_base_url': 'http://provider.invalid/v1'})
    sessions = client.app.state.sessions; leased = claim_job(sessions, settings)
    assert leased.id == child['id']
    async with httpx.AsyncClient(transport=httpx.MockTransport(completion)) as transport:
        gateway = ReceiptGateway(settings, sessions, leased, transport)
        await process_job(leased, settings, sessions, gateway, HybridRetriever(settings, EmptyRoute(), EmptyRoute()))
    draft = client.get(url, headers=auth()).json()
    assert draft['status'] == 'teacher_review' and draft['after']['teacherConfirmed'] is False and draft['delta'] is None
    scores = dict.fromkeys(['content','coherence','expression','vocabulary','grammar'], 3); scores['grammar'] = 4
    response = client.post(f"/api/v1/jobs/{child['id']}/reviews", headers=auth(), json={'version': draft['job']['version'], 'scores': scores, 'comment': 'Synthetic independent reassessment confirmation.'})
    assert response.status_code == 200
    final = client.get(url, headers=auth()).json()
    assert final['status'] == 'reviewed' and final['delta']['scores']['grammar'] == 1
    assert final['comparison']['causalImprovement'] is False
    assert client.get(f"/api/v1/jobs/{approved['id']}", headers=auth()).json() == approved
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(CallReceipt).where(CallReceipt.job_id == child['id'])) > 0
    # Repeated reads/submission reuse the same result; they do not enqueue calls.
    assert submit(client, revision, url).json()['job']['id'] == child['id']


@pytest.mark.asyncio
async def test_source_change_stops_work_before_any_provider_request(client, approved):
    revision, url = confirmed(client, approved); child = submit(client, revision, url).json()['job']
    with client.app.state.sessions.begin() as db:
        parent = db.get(Job, approved['id']); parent.review = {**parent.review, 'comment': 'Synthetic source changed'}
    class NeverCall:
        settings = client.app.state.settings
        calls = 0
        async def complete(self, *args):
            self.calls += 1
            pytest.fail('Changed binding reached provider')
    gateway = NeverCall(); leased = claim_job(client.app.state.sessions, client.app.state.settings)
    await process_job(leased, client.app.state.settings, client.app.state.sessions, gateway)
    assert gateway.calls == 0
    saved = client.get('/api/v1/jobs/' + child['id'], headers=auth()).json()
    assert saved['status'] == 'failed' and saved['errorCode'] == 'REVISION_SOURCE_CHANGED'
    assert client.get(url, headers=auth()).status_code == 409


def test_source_erasure_clears_related_revision_and_child_content_and_fences_late_results(client, approved):
    revision, url = confirmed(client, approved); child = submit(client, revision, url).json()['job']
    leased = claim_job(client.app.state.sessions, client.app.state.settings)
    with client.app.state.sessions.begin() as db:
        db.add(CallReceipt(id='synthetic-receipt', tenant_id='school-a', job_id=child['id'], input_hash='a'*64, stage='synthetic', status='succeeded', result={'synthetic': 'retained-only-until-erasure'}))
    response = client.post(f"/api/v1/jobs/{approved['id']}/erasures", headers=auth('admin'), json={'version': approved['version']})
    assert response.status_code == 200 and response.json()['relatedJobsErased'] == 1
    with client.app.state.sessions() as db:
        saved = db.get(Job, child['id']); assert saved.status == 'erased' and not saved.text and saved.result is None and saved.review is None
        assert db.scalar(select(func.count()).select_from(WritingRevision)) == 0
        assert db.scalar(select(func.count()).select_from(RevisionAssessment)) == 0
        assert db.get(CallReceipt, 'synthetic-receipt').result is None
    assert not finalize(client.app.state.sessions, leased, status='teacher_review', result={'invalid': 'late'})
    assert client.get(url, headers=auth()).status_code == 404


def test_failure_is_distinct_and_explicit_retry_reuses_the_same_job_but_unknown_calls_stay_blocked(client, approved):
    revision, url = confirmed(client, approved); child = submit(client, revision, url).json()['job']
    with client.app.state.sessions.begin() as db:
        row = db.get(Job, child['id']); row.status = 'failed'; row.error_code = 'MODEL_NOT_CONFIGURED'; row.version += 1
    failed = client.get(url, headers=auth()).json(); assert failed['status'] == 'failed'
    retried = client.post(f"/api/v1/jobs/{child['id']}/retries", headers=auth(), json={'version': failed['job']['version']})
    assert retried.status_code == 200 and retried.json()['id'] == child['id']
    assert client.get(url, headers=auth()).json()['status'] == 'queued'
    with client.app.state.sessions.begin() as db:
        row = db.get(Job, child['id']); row.status = 'failed'; row.version += 1
        version = row.version
        db.add(CallReceipt(id='synthetic-unknown', tenant_id='school-a', job_id=child['id'], input_hash='b'*64, stage='synthetic-unknown', status='unknown'))
    blocked = client.post(f"/api/v1/jobs/{child['id']}/retries", headers=auth(), json={'version': version})
    assert blocked.status_code == 409 and blocked.json()['error']['code'] == 'PROVIDER_RESULT_UNKNOWN_REQUIRES_REVIEW'
    assert submit(client, revision, url).json()['job']['id'] == child['id']


def test_unchanged_revision_reuses_original_assessment_without_queueing(client, approved):
    revision = begin(client, approved).json(); base = f"/api/v1/jobs/{approved['id']}/revisions/{revision['id']}"
    edited = client.patch(base, headers=auth(), json={'version': 1, 'text': approved['text'], 'resolutions': {item['id']: 'addressed' for item in revision['feedback']}}).json()
    revision = client.post(base+'/confirmation', headers=auth(), json={'version': edited['version'], 'confirmed': True}).json()
    response = submit(client, revision, base+'/reassessment')
    assert response.status_code == 409 and response.json()['error']['code'] == 'REASSESSMENT_TEXT_UNCHANGED'
    with client.app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(RevisionAssessment)) == 0


def test_explicit_additive_initialization_preserves_existing_jobs(tmp_path):
    from sqlalchemy import inspect
    from inkwise.database import Base, create_session_factory, initialize_database
    url = f"sqlite:///{tmp_path}/previous.sqlite"
    engine, sessions = create_session_factory(url)
    Base.metadata.create_all(engine, tables=[table for name, table in Base.metadata.tables.items() if name != 'revision_assessments'])
    report = {'model': 'synthetic-model', 'modelRevision': 'synthetic-revision', 'scores': [], 'textHash': 'b'*64}
    review = {'comment': 'Synthetic prior teacher review', 'total': 75}
    with sessions.begin() as db:
        db.add(Job(id='synthetic-prior',tenant_id='school-a',classroom_id='a1',owner_id='alice',idempotency_key='prior',request_hash='a'*64,payload={'title':'Synthetic prior'},text='Keep synthetic prior text.',status='approved',version=7,text_version=1,text_hash='b'*64,result=report,review=review))
    engine.dispose(); initialize_database(url)
    engine, sessions = create_session_factory(url)
    assert inspect(engine).has_table('revision_assessments')
    with sessions() as db:
        prior = db.get(Job, 'synthetic-prior')
        assert prior.text == 'Keep synthetic prior text.'
        assert prior.status == 'approved' and prior.version == 7
        assert prior.result == report and prior.review == review and prior.text_hash == 'b'*64
    engine.dispose()


def test_child_origin_is_server_owned_and_source_reads_remain_classroom_scoped(client, approved):
    from test_api import payload
    revision, url = confirmed(client, approved)
    child = submit(client, revision, url).json()['job']
    detail = client.get('/api/v1/jobs/' + child['id'], headers=auth()).json()
    origin = {'sourceJobId': approved['id'], 'revisionId': revision['id'], 'revisionVersion': revision['version']}
    assert detail['payload']['revisionAssessment'] == origin
    for user in ('bob', 'eve'):
        assert client.get('/api/v1/jobs/' + child['id'], headers=auth(user)).status_code == 404
        assert client.get('/api/v1/jobs/' + approved['id'], headers=auth(user)).status_code == 404
        assert client.get(url, headers=auth(user)).status_code == 404
    forged = {**payload(), 'revisionAssessment': origin}
    rejected = client.post('/api/v1/jobs', headers={**auth(), 'Idempotency-Key': 'forged-return-origin'}, json=forged)
    assert rejected.status_code == 422
    assert client.get('/api/v1/jobs/' + approved['id'], headers=auth()).json() == approved
