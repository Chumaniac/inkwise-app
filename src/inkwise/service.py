"""Transactional classroom authorization and optimistic job mutations."""

from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from inkwise.contracts import CandidateCreate, DIMENSIONS, JobCreate, Principal, Review, Role, TextConfirmation, feedback_requires_rewrite
from inkwise.database import Candidate, Job, JobEvent, RevisionAssessment, TenantQuota, now, utc_iso
from inkwise.errors import DomainError
from inkwise.rubrics import RubricRepository, canonical, digest


def authorize_classroom(principal: Principal, classroom: str) -> None:
    if principal.role != Role.ADMIN and classroom not in principal.classroom_ids:
        raise DomainError("NOT_FOUND", 404)


def job_scope(principal: Principal):
    clauses = [Job.tenant_id == principal.tenant_id]
    if principal.role != Role.ADMIN:
        clauses.append(Job.classroom_id.in_(principal.classroom_ids))
    return clauses


def get_job(db: Session, principal: Principal, job_id: str) -> Job:
    job = db.scalar(select(Job).where(Job.id == job_id, *job_scope(principal)))
    if job is None:
        raise DomainError("NOT_FOUND", 404)
    return job


def snapshot(job: Job) -> dict:
    scores = job.result.get('scores', []) if isinstance(job.result, dict) else []
    issues = {}
    if not job.review and isinstance(scores, list):
        for score in scores[:5]:
            if not isinstance(score, dict) or not isinstance(score.get('dimension'), str):
                continue
            if score['dimension'] in DIMENSIONS and feedback_requires_rewrite(score.get('reason'), score.get('suggestion')):
                issues[score['dimension']] = 'NO_CHANGE_CORRECTION'
    return {"id": job.id, "classroomId": job.classroom_id, "payload": job.payload,
            "text": job.text, "textVersion": job.text_version, "textHash": job.text_hash, "version": job.version,
            "status": job.status, "experimentParticipation":job.payload.get("experimentParticipation"), "hasImage": bool(job.image_id), "ocr": job.ocr,
            "rubric": job.rubric, "result": job.result, "review": job.review, "feedbackIssues": issues,
            "errorCode": job.error_code, "createdAt": utc_iso(job.created_at),
            "updatedAt": utc_iso(job.updated_at)}


def event(db: Session, job: Job, actor: str, kind: str, detail: dict | None = None) -> None:
    db.add(JobEvent(job_id=job.id, tenant_id=job.tenant_id, actor_id=actor,
                    event=kind, job_version=job.version, detail=detail or {}))


def mutate(db: Session, job: Job, expected: int, principal: Principal, kind: str, **values) -> Job:
    if {'text', 'text_hash', 'text_version', 'rubric', 'payload', 'image_id', 'ocr'} & values.keys():
        assert_mutable_input(db, job)
    if job.version != expected:
        raise DomainError("VERSION_CONFLICT")
    result = db.execute(update(Job).where(Job.id == job.id, Job.tenant_id == principal.tenant_id,
                                          Job.version == expected).values(**values, version=expected + 1, updated_at=now()))
    if result.rowcount != 1:
        db.rollback()
        raise DomainError("VERSION_CONFLICT")
    db.refresh(job)
    event(db, job, principal.user_id, kind)
    db.commit()
    return job


def assert_mutable_input(db: Session, job: Job) -> None:
    if db.scalar(select(RevisionAssessment.id).where(RevisionAssessment.child_job_id == job.id,
                                                   RevisionAssessment.tenant_id == job.tenant_id)):
        raise DomainError('REASSESSMENT_INPUT_FROZEN')


def create_job(db: Session, principal: Principal, payload: JobCreate, key: str, max_pending: int, *, commit=True) -> Job:
    authorize_classroom(principal, payload.classroom_id)
    from inkwise.orchestration import resolve_intent
    intent=resolve_intent(payload.intent,payload.operator_request)
    if intent=="explain":raise DomainError("EXPLANATION_REQUIRES_REPORT",422)
    payload=payload.model_copy(update={"intent":intent})
    body = payload.model_dump(mode="json", by_alias=True)
    hash_body=dict(body)
    if hash_body.get("intent")=="assess":hash_body.pop("intent")
    if not hash_body.get("operatorRequest"):hash_body.pop("operatorRequest",None)
    if not hash_body.get("studentRef"):hash_body.pop("studentRef",None)
    if not hash_body.get("experimentConsent"):hash_body.pop("experimentConsent",None)
    if hash_body.get("routingPreference")=="local":hash_body.pop("routingPreference",None)
    if not hash_body.get("cloudConsent"):hash_body.pop("cloudConsent",None)
    if not hash_body.get("maxCostMicrousd"):hash_body.pop("maxCostMicrousd",None)
    request_hash = digest(canonical(hash_body))
    # Serialize per-tenant admission in both PostgreSQL and SQLite.
    locked = db.execute(update(TenantQuota).where(TenantQuota.tenant_id == principal.tenant_id)
                        .values(revision=TenantQuota.revision + 1))
    if locked.rowcount != 1:
        raise DomainError("TENANT_NOT_PROVISIONED", 403)
    old = db.scalar(select(Job).where(Job.tenant_id == principal.tenant_id, Job.owner_id == principal.user_id,
                                     Job.idempotency_key == key))
    if old:
        if old.request_hash != request_hash:
            raise DomainError("IDEMPOTENCY_CONFLICT")
        if commit:
            db.commit()
        return old
    pending = db.scalar(select(func.count()).select_from(Job).where(Job.tenant_id == principal.tenant_id,
                         Job.status.not_in(["approved", "published", "cancelled", "erased"])))
    if pending >= max_pending:
        raise DomainError("QUEUE_FULL", 429)
    job = Job(id=str(uuid4()), tenant_id=principal.tenant_id, classroom_id=payload.classroom_id,
              owner_id=principal.user_id, idempotency_key=key, request_hash=request_hash,
              payload=body, text=payload.text, status="needs_text_review", version=1)
    db.add(job)
    db.flush()
    event(db, job, principal.user_id, "job.created")
    if commit:
        db.commit()
    return job


def confirm_text(db: Session, principal: Principal, job_id: str, body: TextConfirmation,
                 rubrics: RubricRepository, settings=None) -> Job:
    job = get_job(db, principal, job_id)
    assert_mutable_input(db, job)
    if job.version != body.version or job.status not in ["needs_text_review", "failed", "teacher_review"]:
        raise DomainError("VERSION_OR_STATE_CONFLICT")
    pages=(job.ocr or {}).get('pages',[])
    if len(pages)>1 and not body.page_order_confirmed:raise DomainError('PAGE_ORDER_CONFIRMATION_REQUIRED',422)
    from inkwise.document_ocr import missing_pages
    if pages and missing_pages(body.text,len(pages)):raise DomainError('MISSING_PAGE_DETECTED',422)
    # Participation is server-owned metadata, not part of the creation contract.
    creation={key:value for key,value in job.payload.items() if key!="experimentParticipation"}
    rubric = rubrics.for_job(principal.tenant_id, JobCreate.model_validate(creation))
    if settings is not None:
        from inkwise.evolution import choose_rubric
        rubric,assignment=choose_rubric(settings,job,rubric,db)
        if assignment:
            db.add(JobEvent(job_id=job.id,tenant_id=job.tenant_id,actor_id=principal.user_id,event='experiment.assigned',
                job_version=body.version+1,detail={**assignment,'textHash':digest(body.text)}))
    return mutate(db, job, body.version, principal, "text.confirmed", text=body.text, text_hash=digest(body.text),
                  text_version=job.text_version + 1, rubric=rubric.model_dump(mode="json", by_alias=True),
                  status="queued", result=None, review=None, error_code=None, lease_id=None, lease_until=None)


def cancel_job(db: Session, principal: Principal, job_id: str, version: int) -> Job:
    job = get_job(db, principal, job_id)
    if job.status in ("cancelled", "approved", "published", "erased"):
        raise DomainError("INVALID_STATE")
    return mutate(db, job, version, principal, "job.cancelled", status="cancelled", lease_id=None, lease_until=None)


def review_job(db: Session, principal: Principal, job_id: str, body: Review) -> Job:
    job = get_job(db, principal, job_id)
    if principal.role not in (Role.TEACHER, Role.ADMIN):
        raise DomainError('FORBIDDEN', 403)
    from inkwise.reassessments import assert_assessment_binding
    assert_assessment_binding(db, job)
    if job.status != "teacher_review" or not job.rubric or not job.result or job.result.get("kind")=="feedback":
        raise DomainError("REPORT_NOT_READY")
    total = round(sum(job.rubric["weights"][d] * score / 4 for d, score in body.scores.items()), 2)
    from inkwise.contracts import Rubric
    from inkwise.providers import validate_evidence
    rubric=Rubric.model_validate(job.rubric)
    for dimension,item in body.feedback.items():
        if item.include:
            if feedback_requires_rewrite(item.reason, item.suggestion):
                raise DomainError('FEEDBACK_REWRITE_REQUIRED', 422)
            item.evidence=[validate_evidence(e,job.text,rubric,dimension) for e in item.evidence]
        else:
            item.reason="";item.suggestion="";item.evidence=[]
    review = {**body.model_dump(mode="json", by_alias=True), "total": total, "reviewerId": principal.user_id,
              "reviewedAt": now().isoformat(), "textHash": job.text_hash}
    return mutate(db, job, body.version, principal, "report.approved", review=review, status="approved")


def create_candidate(db: Session, principal: Principal, job_id: str, body: CandidateCreate) -> Candidate:
    job = get_job(db, principal, job_id)
    if job.status not in ("approved", "published") or job.version != body.version or not job.review or not job.review.get("scores"):
        raise DomainError("APPROVED_REPORT_REQUIRED")
    # Serialize first submission, duplicate retry and erasure against the source report.
    locked=db.execute(update(Job).where(Job.id==job.id,Job.tenant_id==principal.tenant_id,
        Job.version==body.version,Job.status.in_(['approved','published'])).values(updated_at=Job.updated_at))
    if locked.rowcount!=1:raise DomainError('APPROVED_REPORT_REQUIRED')
    existing = db.scalar(select(Candidate).where(Candidate.tenant_id == principal.tenant_id,
                            Candidate.job_id == job.id, Candidate.report_version == job.version))
    review = candidate_review(job,body)
    if existing:
        if existing.status == "withdrawn":raise DomainError("CANDIDATE_WITHDRAWN")
        if existing.text!=body.anonymized_text or existing.provenance!=body.provenance or existing.review!=review:
            raise DomainError("CANDIDATE_ALREADY_EXISTS")
        return existing
    candidate = Candidate(id=str(uuid4()), tenant_id=principal.tenant_id, classroom_id=job.classroom_id,
                          job_id=job.id, report_version=job.version, submitted_by=principal.user_id,
                          text=body.anonymized_text, provenance=body.provenance, rubric=job.rubric,
                          review=review, status="pending")
    db.add(candidate)
    event(db, job, principal.user_id, "candidate.submitted")
    db.commit()
    return candidate


def candidate_review(job,body):
    # Redacting the essay does not redact comments. Copy no original prose implicitly.
    from inkwise.contracts import Rubric, TeacherFeedback
    from inkwise.providers import validate_evidence
    feedback = {}
    if body.reuse_review_feedback:
        rubric = Rubric.model_validate(job.rubric)
        for dimension, raw in job.review.get("feedback", {}).items():
            item = TeacherFeedback.model_validate(raw)
            if item.include:
                item.evidence = [validate_evidence(e, body.anonymized_text, rubric, dimension) for e in item.evidence]
                feedback[dimension] = item.model_dump(mode="json", by_alias=True)
    return {"scores": dict(job.review["scores"]),
              "comment": body.anonymized_comment.strip() or "仅复用教师分项评分；原评语未授权回流。",
              "feedback": feedback, "reuseScope": "explicit-anonymized-v1"}


def revise_candidate(db,principal,identifier,body):
    """Revise an unapproved case; body.version fences the candidate, not the report."""
    item=db.scalar(select(Candidate).where(Candidate.id==identifier,Candidate.tenant_id==principal.tenant_id))
    if item is None:raise DomainError('NOT_FOUND',404)
    authorize_classroom(principal,item.classroom_id)
    if item.submitted_by!=principal.user_id:raise DomainError('FORBIDDEN',403)
    job=get_job(db,principal,item.job_id)
    if job.status not in ('approved','published') or job.version!=item.report_version or not job.review or not job.review.get('scores'):raise DomainError('APPROVED_REPORT_REQUIRED')
    review=candidate_review(job,body)
    changed=db.execute(update(Candidate).where(Candidate.id==identifier,Candidate.version==body.version,
        Candidate.status.in_(['pending','rejected'])).values(text=body.anonymized_text,provenance=body.provenance,
        review=review,status='pending',version=body.version+1,reviewed_by=None,reviewed_at=None,decision_reason=None))
    if changed.rowcount!=1:raise DomainError('CANDIDATE_STATE_CONFLICT')
    event(db,job,principal.user_id,'candidate.revised',{'candidateId':identifier})
    db.commit();db.refresh(item)
    return item
