"""Bounded manual draft comparison against an unchanged teacher-approved source."""
from difflib import SequenceMatcher
from typing import Literal
from uuid import uuid4

from pydantic import Field, field_validator
from sqlalchemy import select, update

from inkwise.contracts import Role, Versioned
from inkwise.cache_storage import ensure_free_space
from inkwise.core import DIMENSIONS
from inkwise.database import TenantQuota, WritingRevision, now, utc_iso
from inkwise.errors import DomainError
from inkwise.rubrics import canonical, digest
from inkwise.service import event, get_job

MAX_REVISIONS = 3
MAX_PIECES = 512


class RevisionEdit(Versioned):
    text: str = Field(min_length=1, max_length=15000)
    resolutions: dict[str, Literal["pending", "addressed", "needs_work"]] = Field(default_factory=dict, max_length=6)
    note: str = Field(default="", max_length=1000)

    @field_validator("resolutions", mode="before")
    @classmethod
    def known_feedback_fields(cls, value):
        if isinstance(value, dict) and not set(value) <= {"overall", *DIMENSIONS}:
            # Reject before nested validation, which could echo a submitted key.
            raise ValueError("Unknown feedback field")
        return value

    @field_validator("text")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Text must not be blank")
        return value


class RevisionConfirmation(Versioned):
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit confirmation required")
        return value


def source_binding(job):
    return {"jobVersion": job.version, "textVersion": job.text_version, "textHash": digest(job.text),
            "reportHash": digest(canonical(job.result)), "reviewHash": digest(canonical(job.review)),
            "rubricHash": digest(canonical(job.rubric))}


def source_job(db, user, job_id, writing=False):
    job = get_job(db, user, job_id)
    if job.status == "erased":
        raise DomainError("NOT_FOUND", 404)
    if writing:
        if user.role not in (Role.TEACHER, Role.ADMIN):
            raise DomainError("FORBIDDEN", 403)
        # This existing tenant row serializes admissions on SQLite and PostgreSQL.
        locked = db.execute(update(TenantQuota).where(TenantQuota.tenant_id == user.tenant_id)
                            .values(revision=TenantQuota.revision + 1))
        if locked.rowcount != 1:
            raise DomainError("TENANT_NOT_PROVISIONED", 403)
        db.refresh(job, with_for_update=True)
    if job.status == "erased":
        raise DomainError("NOT_FOUND", 404)
    if job.status not in ("approved", "published") or not job.result or not job.review:
        raise DomainError("APPROVED_REPORT_REQUIRED")
    if not job.review.get("scores") or job.result.get("kind") == "feedback":
        raise DomainError("REVISION_ASSESSMENT_REQUIRED")
    if not job.text.strip() or len(job.text) > 15000 or job.text_hash != digest(job.text):
        raise DomainError("REVISION_SOURCE_CHANGED")
    return job


def feedback_items(job):
    items = []
    if job.review.get("comment"):
        items.append({"id": "overall", "reason": job.review["comment"], "suggestion": "", "evidence": []})
    for dimension in DIMENSIONS:
        raw = job.review.get("feedback", {}).get(dimension)
        if not raw or not raw.get("include", True):
            continue
        evidence = []
        for original in raw.get("evidence", [])[:8]:
            start, end, quote = original.get("start"), original.get("end"), original.get("quote", "")
            locatable = (type(start) is int and type(end) is int and 0 <= start < end <= len(job.text)
                         and job.text[start:end] == quote)
            evidence.append({**original, "locatable": locatable})
        items.append({"id": dimension, "reason": raw.get("reason", ""),
                      "suggestion": raw.get("suggestion", ""), "evidence": evidence})
    return items


def assert_source(job, revision):
    binding = source_binding(job)
    # Publishing a candidate may advance the Job version without changing text,
    # rubric, result or teacher review. These content pins must all still match.
    if revision.classroom_id != job.classroom_id or any(
        binding[key] != revision.source_binding.get(key) for key in binding if key != "jobVersion"
    ):
        raise DomainError("REVISION_SOURCE_CHANGED")


def get_revision(db, user, job, revision_id):
    revision = db.scalar(select(WritingRevision).where(WritingRevision.id == revision_id,
                        WritingRevision.job_id == job.id, WritingRevision.tenant_id == user.tenant_id))
    if revision is None:
        raise DomainError("NOT_FOUND", 404)
    assert_source(job, revision)
    return revision


def _pieces(value):
    parts, start = [], 0
    for index, char in enumerate(value):
        if char in "\n。！？.!?":
            parts.append(value[start:index + 1]); start = index + 1
    if start < len(value):
        parts.append(value[start:])
    if len(parts) > MAX_PIECES:
        width = (len(parts) + MAX_PIECES - 1) // MAX_PIECES
        return ["".join(parts[index:index + width]) for index in range(0, len(parts), width)]
    return parts


def _changed_parts(before, after):
    prefix = 0
    while prefix < min(len(before), len(after)) and before[prefix] == after[prefix]:
        prefix += 1
    suffix = 0
    while suffix < min(len(before), len(after)) - prefix and before[-suffix - 1] == after[-suffix - 1]:
        suffix += 1
    end_a, end_b = len(before) - suffix, len(after) - suffix
    parts = []
    if prefix:
        parts.append({"kind": "equal", "before": before[:prefix], "after": after[:prefix]})
    a, b = before[prefix:end_a], after[prefix:end_b]
    if a or b:
        parts.append({"kind": "replace" if a and b else "delete" if a else "insert", "before": a, "after": b})
    if suffix:
        parts.append({"kind": "equal", "before": before[end_a:], "after": after[end_b:]})
    return parts


def compare_text(before, after):
    """At most 512 sentence groups; large diffs fall back to bounded passages."""
    left, right = _pieces(before), _pieces(after)
    parts = []
    for kind, a, b, c, d in SequenceMatcher(None, left, right, autojunk=False).get_opcodes():
        old, new = "".join(left[a:b]), "".join(right[c:d])
        parts.extend([{"kind": "equal", "before": old, "after": new}] if kind == "equal" else _changed_parts(old, new))
        if len(parts) > MAX_PIECES:
            return {"granularity": "passage", "parts": _changed_parts(before, after)}
    return {"granularity": "sentence", "parts": parts}


def view(job, revision):
    return {"id": revision.id, "ordinal": revision.ordinal, "version": revision.version,
            "status": revision.status, "source": revision.source_binding, "text": revision.text,
            "resolutions": revision.resolutions, "note": revision.note, "feedback": feedback_items(job),
            "diff": compare_text(job.text, revision.text), "confirmedBy": revision.confirmed_by,
            "confirmedAt": utc_iso(revision.confirmed_at) if revision.confirmed_at else None,
            "updatedAt": utc_iso(revision.updated_at)}


def list_revisions(db, user, job_id):
    job = source_job(db, user, job_id)
    rows = db.execute(select(WritingRevision.id, WritingRevision.ordinal, WritingRevision.version,
                             WritingRevision.status, WritingRevision.updated_at)
                      .where(WritingRevision.tenant_id == user.tenant_id, WritingRevision.job_id == job.id)
                      .order_by(WritingRevision.ordinal).limit(MAX_REVISIONS)).all()
    return {"items": [{"id": r.id, "ordinal": r.ordinal, "version": r.version, "status": r.status,
                       "updatedAt": utc_iso(r.updated_at)} for r in rows], "limit": MAX_REVISIONS}


def create_revision(db, user, job_id, body, key, settings):
    job = source_job(db, user, job_id, writing=True)
    old = db.scalar(select(WritingRevision).where(WritingRevision.job_id == job.id,
                    WritingRevision.tenant_id == user.tenant_id, WritingRevision.owner_id == user.user_id,
                    WritingRevision.idempotency_key == key))
    if old:
        if old.source_binding["jobVersion"] != body.version:
            raise DomainError("IDEMPOTENCY_CONFLICT")
        assert_source(job, old); db.commit()
        return view(job, old)
    if job.version != body.version:
        raise DomainError("VERSION_CONFLICT")
    existing = db.execute(select(WritingRevision.id, WritingRevision.source_binding, WritingRevision.classroom_id)
                          .where(WritingRevision.job_id == job.id, WritingRevision.tenant_id == user.tenant_id)
                          .order_by(WritingRevision.ordinal).limit(MAX_REVISIONS + 1)).all()
    if len(existing) >= MAX_REVISIONS:
        raise DomainError("REVISION_LIMIT", 429)
    for item in existing:
        assert_source(job, item)
    ensure_free_space(settings, 128 * 1024)
    revision = WritingRevision(id=str(uuid4()), tenant_id=user.tenant_id, classroom_id=job.classroom_id,
        job_id=job.id, owner_id=user.user_id, idempotency_key=key, ordinal=len(existing) + 1,
        source_binding=source_binding(job), text=job.text, resolutions={item["id"]: "pending" for item in feedback_items(job)})
    db.add(revision); db.flush()
    event(db, job, user.user_id, "revision.created", {"revisionId": revision.id, "revisionVersion": 1})
    db.commit()
    return view(job, revision)


def edit_revision(db, user, job_id, revision_id, body, settings):
    job = source_job(db, user, job_id, writing=True)
    revision = get_revision(db, user, job, revision_id)
    if set(body.resolutions) != {item["id"] for item in feedback_items(job)}:
        raise DomainError("REVISION_FEEDBACK_MISMATCH", 422)
    if revision.version != body.version or revision.status != "draft":
        raise DomainError("VERSION_OR_STATE_CONFLICT")
    ensure_free_space(settings, 128 * 1024)
    changed = db.execute(update(WritingRevision).where(WritingRevision.id == revision.id,
        WritingRevision.tenant_id == user.tenant_id, WritingRevision.version == body.version,
        WritingRevision.status == "draft").values(text=body.text, resolutions=body.resolutions, note=body.note,
                                                 version=body.version + 1, updated_at=now()))
    if changed.rowcount != 1:
        raise DomainError("VERSION_OR_STATE_CONFLICT")
    event(db, job, user.user_id, "revision.saved", {"revisionId": revision.id, "revisionVersion": body.version + 1})
    db.commit(); db.refresh(revision)
    return view(job, revision)


def confirm_revision(db, user, job_id, revision_id, body, settings):
    job = source_job(db, user, job_id, writing=True)
    revision = get_revision(db, user, job, revision_id)
    expected = {item["id"] for item in feedback_items(job)}
    if set(revision.resolutions) != expected or any(value == "pending" for value in revision.resolutions.values()):
        raise DomainError("REVISION_REVIEW_INCOMPLETE")
    if revision.version != body.version or revision.status != "draft":
        raise DomainError("VERSION_OR_STATE_CONFLICT")
    ensure_free_space(settings, 32 * 1024)
    changed = db.execute(update(WritingRevision).where(WritingRevision.id == revision.id,
        WritingRevision.tenant_id == user.tenant_id, WritingRevision.version == body.version,
        WritingRevision.status == "draft").values(status="confirmed", confirmed_by=user.user_id,
            confirmed_at=now(), version=body.version + 1, updated_at=now()))
    if changed.rowcount != 1:
        raise DomainError("VERSION_OR_STATE_CONFLICT")
    event(db, job, user.user_id, "revision.confirmed", {"revisionId": revision.id, "revisionVersion": body.version + 1})
    db.commit(); db.refresh(revision)
    return view(job, revision)
