"""Synthetic adversarial integration cases for authorization and versioning."""

import hashlib
from pathlib import Path
import tomllib

import pytest
from fastapi.testclient import TestClient

from inkwise.api import create_app
from inkwise.database import Base, Identity, Job, TenantQuota, create_session_factory
from inkwise.settings import Settings
from inkwise.cli import seed_demo


@pytest.fixture
def client(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path}/test.sqlite", data_dir=tmp_path, rubric_dir=tmp_path / "rubrics")
    engine, sessions = create_session_factory(settings.database_url)
    Base.metadata.create_all(engine)
    seed_demo(settings, "school-a")
    with sessions.begin() as db:
        for user, tenant, classes, role in [("alice", "school-a", ["a1"], "teacher"),
                                            ("bob", "school-b", ["a1"], "teacher"),
                                            ("eve", "school-a", ["a2"], "teacher"),
                                            ("admin", "school-a", [], "admin")]:
            db.add(Identity(id=user, tenant_id=tenant, classroom_ids=classes, role=role,
                            token_hash=hashlib.sha256(f"synthetic-{user}".encode()).hexdigest()))
        db.add_all([TenantQuota(tenant_id="school-a"), TenantQuota(tenant_id="school-b")])
    with TestClient(create_app(settings)) as http:
        yield http
    engine.dispose()


def auth(user="alice"):
    return {"Authorization": f"Bearer synthetic-{user}"}


def test_api_version_describes_the_selected_source(client):
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert client.app.version == project["project"]["version"]


def payload():
    return {"classroomId": "a1", "title": "A story", "prompt": "Tell a story", "text": "Synthetic essay.",
            "grade": 7, "languagePolicy": "en", "genre": "short-text", "rubricId": "english-demo"}


def create(client, key="sample"):
    return client.post("/api/v1/jobs", headers={**auth(), "Idempotency-Key": key}, json=payload())


def test_authentication_and_scope_are_not_client_claims(client):
    assert client.get("/api/v1/jobs").status_code == 401
    response = create(client)
    assert response.status_code == 201
    job = response.json()
    for user in ("bob", "eve"):
        assert client.get(f"/api/v1/jobs/{job['id']}", headers=auth(user)).status_code == 404
        assert client.post(f"/api/v1/jobs/{job['id']}/cancel", headers=auth(user), json={"version": 1}).status_code == 404
    spoofed = {**payload(), "tenantId": "school-b"}
    assert client.post("/api/v1/jobs", headers={**auth(), "Idempotency-Key": "spoof"}, json=spoofed).status_code == 422


def test_idempotency_and_changed_request_conflict(client):
    first = create(client).json()
    assert create(client).json()["id"] == first["id"]
    response = client.post("/api/v1/jobs", headers={**auth(), "Idempotency-Key": "sample"},
                           json={**payload(), "text": "different"})
    assert response.status_code == 409


def test_sql_like_input_is_literal_not_executed(client):
    response = client.post("/api/v1/jobs", headers={**auth(), "Idempotency-Key": "sql"},
                           json={**payload(), "text": "'; DROP TABLE jobs; --"})
    assert response.status_code == 201
    assert client.get("/api/v1/jobs", headers=auth()).json()["items"][0]["text"] == "'; DROP TABLE jobs; --"


def test_cancel_and_stale_text_confirmation(client):
    job = create(client).json()
    url = f"/api/v1/jobs/{job['id']}"
    result = client.post(url + "/cancel", headers=auth(), json={"version": 1})
    assert result.status_code == 200
    assert result.json()["version"] == 2
    assert client.post(url + "/text-confirmations", headers=auth(),
                       json={"version": 1, "text": "Do not resurrect"}).status_code == 409


def test_missing_rubric_stops_grading_and_does_not_fabricate_result(client):
    job = client.post("/api/v1/jobs", headers={**auth(), "Idempotency-Key": "missing"},
                      json={**payload(), "rubricId": "missing-rule"}).json()
    url = f"/api/v1/jobs/{job['id']}"
    response = client.post(url + "/text-confirmations", headers=auth(), json={"version": 1, "text": "Confirmed text"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RULESET_UNAVAILABLE"
    assert client.get(url, headers=auth()).json()["result"] is None


def test_user_text_not_in_validation_error_response(client):
    secret_like_text = "PRIVATE_SYNTHETIC_TEXT_" * 1000
    response = client.post("/api/v1/jobs", headers={**auth(), "Idempotency-Key": "long"}, json={**payload(), "text": secret_like_text})
    assert response.status_code == 422
    assert "PRIVATE_SYNTHETIC_TEXT" not in response.text


def test_review_cannot_approve_unprocessed_task(client):
    job = create(client).json()
    response = client.post(f"/api/v1/jobs/{job['id']}/reviews", headers=auth(),
                           json={"version": 1, "scores": dict.fromkeys(["content", "coherence", "expression", "vocabulary", "grammar"], 4), "comment": "Synthetic review"})
    assert response.status_code == 409


def test_text_confirmation_freezes_rubric_and_rejects_old_version(client):
    job = create(client).json()
    url = f"/api/v1/jobs/{job['id']}/text-confirmations"
    result = client.post(url, headers=auth(), json={"version": 1, "text": "Confirmed synthetic essay."})
    assert result.status_code == 200
    assert result.json()["status"] == "queued"
    assert result.json()["textVersion"] == 1
    assert result.json()["rubric"]["release"] == "synthetic-v1"
    assert client.post(url, headers=auth(), json={"version": 1, "text": "Stale"}).status_code == 409


def test_queue_admission_is_bounded(client):
    for i in range(20):
        assert create(client, key=f"job-{i}").status_code == 201
    assert create(client, key="over-quota").status_code == 429
    assert create(client, key="job-0").status_code == 201


def test_cross_school_event_stream_is_not_accessible(client):
    job = create(client).json()
    assert client.get(f"/api/v1/jobs/{job['id']}/events", headers=auth('bob')).status_code == 404
    events = client.get(f"/api/v1/jobs/{job['id']}/events", headers=auth()).json()["items"]
    assert len(events) == 1 and events[0]["event"] == "job.created"


def test_content_erasure_requires_admin_and_removes_live_student_text(client):
    job=create(client).json()
    url=f"/api/v1/jobs/{job['id']}"
    assert client.post(url+"/erasures",headers=auth(),json={"version":1}).status_code==403
    result=client.post(url+"/erasures",headers=auth("admin"),json={"version":1})
    assert result.status_code==200 and result.json()["liveContentErased"]
    record=client.get(url,headers=auth("admin")).json()
    assert record["text"]=="" and record["status"]=="erased" and "Synthetic essay" not in str(record)


def test_image_decoding_has_a_hard_concurrency_limit(client,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from io import BytesIO
    from PIL import Image
    import inkwise.api as module
    first=create(client,'image-one').json();second=create(client,'image-two').json()
    image=BytesIO();Image.new('RGB',(100,100),'white').save(image,format='PNG');content=image.getvalue()
    started,release=Event(),Event();original=module.normalize_image
    def delayed(data,settings):
        started.set();release.wait(5);return original(data,settings)
    monkeypatch.setattr(module,'normalize_image',delayed)
    def upload(job):return client.post(f"/api/v1/jobs/{job['id']}/image",headers=auth(),data={'version':'1'},files={'image':('test.png',content,'image/png')})
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending=pool.submit(upload,first)
        try:
            assert started.wait(3)
            rejected=upload(second)
            assert rejected.status_code==429 and rejected.json()['error']['code']=='IMAGE_PROCESSOR_BUSY'
        finally:release.set()
        assert pending.result().status_code==200


def test_curriculum_library_requires_auth_and_filters_grade(client):
    assert client.get('/api/v1/library').status_code==401
    result=client.get('/api/v1/library?language=zh&grade=7',headers=auth())
    assert result.status_code==200 and result.json()['items']
    assert all(item['gradeMin']<=7<=item['gradeMax'] for item in result.json()['items'])
    assert client.get('/api/v1/library?language=unknown&grade=7',headers=auth()).status_code==422


def test_multiple_pages_reject_duplicates_and_require_order_confirmation(client):
    import io
    from PIL import Image
    from inkwise.database import Job
    identifier=create(client).json()['id']
    def png(color):
        stream=io.BytesIO();Image.new('RGB',(100,100),color).save(stream,format='PNG');return stream.getvalue()
    first,second=png('white'),png('gray')
    duplicate=client.post(f'/api/v1/jobs/{identifier}/pages',headers=auth(),data={'version':'1'},
        files=[('pages',('one.png',first,'image/png')),('pages',('two.png',first,'image/png'))])
    assert duplicate.status_code==422 and duplicate.json()['error']['code']=='DUPLICATE_PAGE'
    result=client.post(f'/api/v1/jobs/{identifier}/pages',headers=auth(),data={'version':'1'},
        files=[('pages',('one.png',first,'image/png')),('pages',('two.png',second,'image/png'))])
    assert result.status_code==200 and len(result.json()['ocr']['pages'])==2
    assert client.get(f'/api/v1/jobs/{identifier}/image?page=2',headers=auth()).status_code==200
    assert client.get(f'/api/v1/jobs/{identifier}/image?page=3',headers=auth()).status_code==404
    with client.app.state.sessions.begin() as db:db.get(Job,identifier).status='needs_text_review'
    version=result.json()['version']
    response=client.post(f'/api/v1/jobs/{identifier}/text-confirmations',headers=auth(),json={'version':version,'text':'Synthetic'})
    assert response.json()['error']['code']=='PAGE_ORDER_CONFIRMATION_REQUIRED'
    response=client.post(f'/api/v1/jobs/{identifier}/text-confirmations',headers=auth(),json={'version':version,'text':'Page 1 of 3','pageOrderConfirmed':True})
    assert response.json()['error']['code']=='MISSING_PAGE_DETECTED'


def test_event_cursor_reads_all_pages_without_cross_job_or_classroom_leaks(client):
    from inkwise.database import JobEvent
    job = create(client).json()
    other = create(client, key="another").json()
    with client.app.state.sessions.begin() as db:
        db.add_all(JobEvent(job_id=job['id'], tenant_id='school-a', actor_id='synthetic',
                           event='scoring.started', job_version=1, detail={}) for _ in range(205))
        db.add(JobEvent(job_id=other['id'], tenant_id='school-a', actor_id='synthetic',
                        event='other-job', job_version=1, detail={}))
    cursor = 0
    seen = []
    while True:
        page = client.get(f"/api/v1/jobs/{job['id']}/events", headers=auth(), params={'after':cursor,'limit':100}).json()
        assert len(page['items']) <= 100
        assert all(item['event'] != 'other-job' for item in page['items'])
        seen.extend(item['id'] for item in page['items'])
        assert page['nextCursor'] >= cursor
        cursor = page['nextCursor']
        if not page['hasMore']:
            break
    assert len(seen) == 206 and seen == sorted(set(seen))
    for user in ('bob', 'eve'):
        assert client.get(f"/api/v1/jobs/{job['id']}/events", headers=auth(user), params={'after':cursor}).status_code == 404
    assert client.get(f"/api/v1/jobs/{job['id']}/events", headers=auth(), params={'limit':101}).status_code == 422


def test_export_snapshot_binds_current_version_and_rejects_stale_or_unauthorized_reads(client):
    job = create(client).json()
    url = f"/api/v1/jobs/{job['id']}/export-snapshot"
    exported = client.get(url, headers=auth(), params={'version':1})
    assert exported.status_code == 200
    document = exported.json()
    assert document['job']['id'] == job['id']
    assert document['binding']['textHash'] == hashlib.sha256(job['text'].encode()).hexdigest()
    assert document['binding']['tenantId'] == 'school-a'
    assert document['binding']['jobVersion'] == 1 and document['binding']['exportedAt']
    client.post(f"/api/v1/jobs/{job['id']}/cancel", headers=auth(), json={'version':1})
    assert client.get(url, headers=auth(), params={'version':1}).status_code == 409
    assert client.get(url, headers=auth(), params={'version':2}).json()['job']['status'] == 'cancelled'
    for user in ('bob', 'eve'):
        assert client.get(url, headers=auth(user), params={'version':2}).status_code == 404


def test_job_queue_filters_and_counts_respect_scope(client):
    first = create(client, 'queue-first').json()
    create(client, 'queue-second')
    client.post(f"/api/v1/jobs/{first['id']}/cancel", headers=auth(), json={'version': 1})
    response = client.get('/api/v1/jobs?status=cancelled&search=A%20story&limit=1', headers=auth()).json()
    assert [row['id'] for row in response['items']] == [first['id']]
    assert response['statusCounts']['cancelled'] == 1
    assert response['statusCounts']['needs_text_review'] == 1
    for user in ('bob', 'eve'):
        scoped = client.get('/api/v1/jobs?status=cancelled', headers=auth(user)).json()
        assert scoped['items'] == []
        assert sum(scoped['statusCounts'].values()) == 0
    assert client.get('/api/v1/jobs?status=made-up', headers=auth()).status_code == 422
    assert client.get('/api/v1/jobs?status=cancelled&search=%27%20OR%201%3D1--', headers=auth()).json()['items'] == []


def test_action_required_queue_is_scoped_paginated_and_composable(client):
    jobs = [create(client, f"attention-{index}").json() for index in range(4)]
    with client.app.state.sessions.begin() as db:
        for item, status in zip(jobs, ["needs_text_review", "teacher_review", "failed", "running"]):
            db.get(Job, item["id"]).status = status
    query = "/api/v1/jobs?attention=needs_action&view=summary&limit=2"
    first = client.get(query, headers=auth()).json()
    second = client.get(query + "&offset=2", headers=auth()).json()
    assert len(first["items"]) == 2 and first["hasMore"] is True
    assert len(second["items"]) == 1 and second["hasMore"] is False
    assert {row["status"] for row in first["items"] + second["items"]} == {
        "needs_text_review", "teacher_review", "failed"
    }
    assert first["actionRequiredCount"] == 3 and first["statusCounts"]["running"] == 1
    assert first["attention"] == "needs_action" and first["view"] == "summary"
    filtered = client.get(query + "&status=teacher_review", headers=auth()).json()
    assert [row["id"] for row in filtered["items"]] == [jobs[1]["id"]]
    assert filtered["actionRequiredCount"] == 3
    for user in ("bob", "eve"):
        result = client.get(query, headers=auth(user)).json()
        assert result["items"] == [] and result["actionRequiredCount"] == 0
        assert sum(result["statusCounts"].values()) == 0
    missing = client.get(query + "&search=no-such-title", headers=auth()).json()
    assert missing["items"] == [] and missing["actionRequiredCount"] == 0
    assert client.get(query).status_code == 401


def test_queue_summary_omits_private_bodies_and_preserves_full_detail_compatibility(client):
    body = "SYNTHETIC_PRIVATE_BODY_" * 400
    item = client.post("/api/v1/jobs", headers={**auth(), "Idempotency-Key": "summary"},
                       json={**payload(), "text": body, "prompt": "SYNTHETIC_PRIVATE_PROMPT"}).json()
    with client.app.state.sessions.begin() as db:
        saved = db.get(Job, item["id"])
        saved.status = "teacher_review"
        saved.result = {"private": "SYNTHETIC_PRIVATE_REPORT"}
        saved.review = {"private": "SYNTHETIC_PRIVATE_REVIEW"}
    summary = client.get("/api/v1/jobs?view=summary", headers=auth())
    row = summary.json()["items"][0]
    assert row["id"] == item["id"] and row["queueAction"] == "review_report"
    assert set(row["payload"]) == {"title", "grade"}
    assert not {"text", "result", "review", "ocr", "rubric"}.intersection(row)
    assert "SYNTHETIC_PRIVATE_" not in summary.text
    full = client.get("/api/v1/jobs", headers=auth())
    assert full.json()["items"][0]["text"] == body
    assert len(summary.content) < len(full.content) / 4
    assert client.get(f"/api/v1/jobs/{item['id']}", headers=auth()).json()["text"] == body


def test_queue_new_options_reject_injection_and_invalid_views(client):
    create(client, "attention-input")
    for params in [{"attention": "failed' OR 1=1--"}, {"view": "private"}]:
        result = client.get("/api/v1/jobs", headers=auth(), params=params)
        assert result.status_code == 422
        assert "OR 1=1" not in result.text
    result = client.get("/api/v1/jobs", headers=auth(),
                        params={"attention": "needs_action", "view": "summary", "search": "' OR 1=1--"})
    assert result.json()["items"] == [] and result.json()["actionRequiredCount"] == 0


def test_queue_can_sort_updates_without_changing_scope_or_loading_private_details(client):
    from datetime import datetime, timedelta, timezone
    first, second = create(client, "sort-first").json(), create(client, "sort-second").json()
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    with client.app.state.sessions.begin() as db:
        older, newer = db.get(Job, first["id"]), db.get(Job, second["id"])
        older.created_at, newer.created_at = start, start + timedelta(hours=1)
        older.updated_at, newer.updated_at = start + timedelta(hours=4), start + timedelta(hours=2)
    query = {"view": "summary", "classroom": "a1", "search": "A story"}
    original = client.get("/api/v1/jobs", headers=auth(), params=query).json()
    updated = client.get("/api/v1/jobs", headers=auth(), params={**query, "sort": "updated"}).json()
    assert [row["id"] for row in original["items"]] == [second["id"], first["id"]]
    assert [row["id"] for row in updated["items"]] == [first["id"], second["id"]]
    assert updated["sort"] == "updated" and updated["statusCounts"] == original["statusCounts"]
    assert all(not {"text", "rubric", "result", "review"}.intersection(row) for row in updated["items"])
    for user in ("bob", "eve"):
        assert client.get("/api/v1/jobs?view=summary&sort=updated", headers=auth(user)).json()["items"] == []
    with client.app.state.sessions.begin() as db:
        db.get(Job, second["id"]).updated_at = start + timedelta(hours=4)
    pages = [client.get("/api/v1/jobs", headers=auth(), params={**query, "sort": "updated", "limit": 1, "offset": offset}).json() for offset in (0, 1)]
    assert [page["items"][0]["id"] for page in pages] == sorted([first["id"], second["id"]])
    assert pages[0]["hasMore"] and not pages[1]["hasMore"]
    for value in ("updated_at DESC; DROP TABLE jobs", "__proto__"):
        response = client.get("/api/v1/jobs", headers=auth(), params={"sort": value})
        assert response.status_code == 422 and value not in response.text


def test_classroom_filter_composes_with_summary_paging_and_counts(client):
    first = create(client, "class-filter-first").json()
    second = create(client, "class-filter-second").json()
    other = client.post("/api/v1/jobs", headers={**auth("eve"), "Idempotency-Key": "other-class"},
                        json={**payload(), "classroomId": "a2"}).json()
    client.post(f"/api/v1/jobs/{first['id']}/cancel", headers=auth(), json={"version": 1})
    with client.app.state.sessions.begin() as db:
        db.get(Identity, "alice").classroom_ids = ["a1", "a2"]
    query = {"classroom": "a1", "view": "summary", "limit": 1, "search": "A story"}
    page = client.get("/api/v1/jobs", headers=auth(), params=query).json()
    next_page = client.get("/api/v1/jobs", headers=auth(), params={**query, "offset": 1}).json()
    assert {row["id"] for row in page["items"] + next_page["items"]} == {first["id"], second["id"]}
    assert page["hasMore"] and not next_page["hasMore"]
    assert page["statusCounts"] == {"cancelled": 1, "needs_text_review": 1}
    assert page["actionRequiredCount"] == 1 and page["classroom"] == "a1"
    filtered = client.get("/api/v1/jobs", headers=auth(), params={**query, "attention": "needs_action"}).json()
    assert [item["id"] for item in filtered["items"]] == [second["id"]]
    assert sum(filtered["statusCounts"].values()) == 2
    assert "text" not in filtered["items"][0]
    all_classes = client.get("/api/v1/jobs", headers=auth(), params={"view": "summary"}).json()
    assert other["id"] in {row["id"] for row in all_classes["items"]}


def test_classroom_filter_cannot_widen_scope_or_execute_injected_input(client):
    create(client, "class-filter-scope")
    for classroom in ["a2", "not-a-class", "a1' OR 1=1--"]:
        response = client.get("/api/v1/jobs", headers=auth(), params={"classroom": classroom})
        assert response.status_code == 404
        assert "A story" not in response.text
    other_school = client.get("/api/v1/jobs", headers=auth("bob"), params={"classroom": "a1"}).json()
    assert other_school["items"] == [] and sum(other_school["statusCounts"].values()) == 0
    literal = client.get("/api/v1/jobs", headers=auth("admin"), params={"classroom": "a1' OR 1=1--"}).json()
    assert literal["items"] == [] and literal["statusCounts"] == {}
    invalid = client.get("/api/v1/jobs", headers=auth(), params={"classroom": "PRIVATE_CLASS_SENTINEL_" * 8})
    assert invalid.status_code == 422 and "PRIVATE_CLASS_SENTINEL" not in invalid.text

def test_readiness_is_public_and_runtime_configuration_is_scoped(client):
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert client.get("/api/v1/runtime-status").status_code == 401
    assert client.get("/api/v1/runtime-status", headers={"Authorization": "Bearer invalid"}).status_code == 401
    details = client.get("/api/v1/runtime-status", headers=auth())
    assert details.status_code == 200
    assert set(details.json()) == {"modelConfigured", "ocrConfigured"}
    assert all(isinstance(value, bool) for value in details.json().values())
