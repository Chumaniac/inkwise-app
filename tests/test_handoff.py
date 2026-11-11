"""Synthetic metadata-only handoff boundaries and state/version behavior."""

from sqlalchemy import event, func, select

from inkwise.database import CallReceipt, Job, JobEvent
from test_api import auth, client, create  # noqa: F401 - shared isolated fixture


def test_handoff_is_authorized_versioned_and_does_not_write_or_return_bodies(client):
    job = create(client).json()
    sessions = client.app.state.sessions
    with sessions.begin() as db:
        record = db.get(Job, job["id"])
        record.text = "PRIVATE_ESSAY" * 3000
        record.payload["studentRef"] = "PRIVATE_STUDENT"
        record.result = {"modelRevision": "model-v1", "pipelineRevision": "pipeline-v1",
                         "scores": ["PRIVATE_REPORT" * 1000], "rawProvider": "PRIVATE_PROVIDER"}
        record.review = {"comment": "PRIVATE_REVIEW"}
        record.ocr = {"raw": "PRIVATE_OCR"}
        record.rubric = {"release": "rubric-v1", "raw": "PRIVATE_RULE"}
    with sessions() as db:
        before = (db.scalar(select(func.count()).select_from(JobEvent)),
                  db.scalar(select(func.count()).select_from(CallReceipt)))
    url = f"/api/v1/jobs/{job['id']}/handoff?version=1"
    for user in ("bob", "eve"):
        assert client.get(url, headers=auth(user)).status_code == 404
    assert client.get(url).status_code == 401
    statements = []
    engine = sessions.kw["bind"]
    def record_sql(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        response = client.get(url, headers=auth())
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)
    assert response.status_code == 200
    data = response.json()
    assert data["schema"] == "inkwise.task-handoff/v1"
    assert data["nextAction"] == "confirm_text"
    assert data["modelRevision"] == "model-v1"
    assert data["lastEvent"]["event"] == "job.created"
    assert len(response.content) < 4096
    assert "PRIVATE_" not in response.text
    assert all("jobs.text," not in sql and "jobs.payload" not in sql and "jobs.review" not in sql and "jobs.ocr" not in sql for sql in statements)
    with sessions() as db:
        assert before == (db.scalar(select(func.count()).select_from(JobEvent)),
                          db.scalar(select(func.count()).select_from(CallReceipt)))
        assert db.get(Job, job["id"]).version == 1
    assert client.get(url.replace("version=1", "version=2"), headers=auth()).status_code == 409
    assert client.get(url, headers=auth("admin")).status_code == 200


def test_handoff_statuses_unknown_requests_erasure_and_literal_identifiers(client):
    job = create(client).json()
    for state, action in [("teacher_review", "review_report"), ("failed", "inspect_failure"),
                          ("approved", "read_report"), ("running", "wait"), ("cancelled", "closed"),
                          ("not-a-known-state", "inspect_events")]:
        with client.app.state.sessions.begin() as db:
            record = db.get(Job, job["id"])
            record.status = state
            record.result = {"modelRevision": "synthetic-model-v1"}
            record.error_code = "PROVIDER_TIMEOUT_UNKNOWN" if state == "failed" else None
        response = client.get(f"/api/v1/jobs/{job['id']}/handoff?version=1", headers=auth())
        assert response.status_code == 200
        assert response.json()["nextAction"] == action
        if state == "failed":
            assert "管理员核查" in response.json()["instruction"]
    assert client.get("/api/v1/jobs/%27%20OR%201%3D1--/handoff?version=1", headers=auth()).status_code == 404
    with client.app.state.sessions.begin() as db:
        record = db.get(Job, job["id"])
        record.status = "approved"
        record.result = None
    assert client.get(f"/api/v1/jobs/{job['id']}/handoff?version=1", headers=auth()).json()["nextAction"] == "inspect_events"
    with client.app.state.sessions.begin() as db:
        db.get(Job, job["id"]).status = "erased"
    assert client.get(f"/api/v1/jobs/{job['id']}/handoff?version=1", headers=auth()).status_code == 404
