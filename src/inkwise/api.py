"""Authenticated teaching API; public demo assets never receive server secrets."""

import hashlib
from collections import OrderedDict
from contextlib import asynccontextmanager
from threading import Lock, BoundedSemaphore
from time import monotonic
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, Query, Request, File, Form, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from sqlalchemy import select, text, update, func, and_, or_
from sqlalchemy.exc import SQLAlchemyError

from inkwise.contracts import CandidateCreate, CandidateDecision, JobCreate, Principal, Review, Role, TextConfirmation, Versioned, PageOrder, IntentRequest, FeedbackApproval, LanguagePreview, ProposalAction, ExperimentControl
from inkwise.database import Candidate, CallReceipt, Identity, Job, JobEvent, WritingRevision, create_session_factory, now, utc_iso
from inkwise.errors import DomainError
from inkwise.rubrics import RubricRepository
from inkwise.service import (authorize_classroom, cancel_job, create_candidate, create_job, event,
                             get_job, job_scope, review_job, snapshot, confirm_text)
from inkwise.service import mutate
from inkwise.ocr import normalize_image, store_image
from inkwise.cache_storage import image_admission
from inkwise import __version__
from inkwise.settings import Settings
from inkwise.handoff import task_handoff
from inkwise.report_readiness import inspect_report
from inkwise.citations import CitationLookup, locate_citation
from inkwise.revisions import (RevisionEdit, RevisionConfirmation, source_job, get_revision,
                               list_revisions, create_revision, edit_revision, confirm_revision, view as revision_view)
from inkwise.reassessments import read_reassessment, request_reassessment

JOB_QUEUE_ACTIONS = {
    "needs_text_review": "confirm_text",
    "teacher_review": "review_report",
    "failed": "inspect_failure",
}


def candidate_receipt(item: Candidate) -> dict:
    return {"id": item.id, "jobId": item.job_id, "reportVersion": item.report_version,
            "status": item.status, "version": item.version}


def candidate_view(item: Candidate) -> dict:
    return {**candidate_receipt(item),
            "text": item.text if item.status != "withdrawn" else "",
            "provenance": item.provenance if item.status != "withdrawn" else "",
            "review": item.review if item.status != "withdrawn" else {},
            "decisionReason": item.decision_reason, "submittedBy": item.submitted_by}


def candidate_source_view(item: Candidate, title, version, status) -> dict:
    return {**candidate_view(item), "classroomId": item.classroom_id, "sourceTitle": title,
            "sourceVersion": version, "sourceStatus": status or "unavailable",
            "sourceCurrent": version is not None and version == item.report_version
            and status in ("approved", "published")}


class RateLimiter:
    """Bounded per-process abuse limiter; ingress enforces deployment-wide quotas."""
    def __init__(self):
        self.entries = OrderedDict()
        self.lock = Lock()

    def check(self, key: str, limit: int = 120) -> None:
        with self.lock:
            clock = monotonic()
            since, count = self.entries.pop(key, (clock, 0))
            if clock - since >= 60:
                since, count = clock, 0
            self.entries[key] = (since, count + 1)
            while len(self.entries) > 2048:
                self.entries.popitem(last=False)
            if count >= limit:
                raise DomainError("RATE_LIMITED", 429)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    engine, sessions = create_session_factory(settings.database_url)
    rubrics = RubricRepository(settings.rubric_dir)
    limiter = RateLimiter()
    # A 20 MP decoded image can occupy hundreds of MB; reject excess decoders
    # rather than filling the API thread pool with simultaneous pixel buffers.
    image_slots = BoundedSemaphore(1)
    language_slots = BoundedSemaphore(1)
    learning_slots = BoundedSemaphore(1)
    bearer = HTTPBearer(auto_error=False)

    @asynccontextmanager
    async def lifespan(app):
        yield
        engine.dispose()

    app = FastAPI(title="Inkwise teaching API", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.sessions = sessions
    app.state.rubrics = rubrics

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse({"error": {"code": exc.code}}, status_code=exc.status,
                            headers={"Cache-Control": "no-store"})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # FastAPI's default includes submitted input; redact it from responses.
        return JSONResponse({"error": {"code": "INVALID_REQUEST", "fields": [
            {"location": list(error["loc"]), "type": error["type"]} for error in exc.errors()
        ]}}, status_code=422)

    @app.exception_handler(SQLAlchemyError)
    async def storage_error(request, exc):
        return JSONResponse({"error": {"code": "STORAGE_UNAVAILABLE"}}, status_code=503)

    @app.middleware("http")
    async def protect(request: Request, call_next):
        limit = settings.max_upload_bytes + 65536 if request.url.path.endswith("/image") else 100000
        if request.url.path.endswith("/pages"):limit=settings.ocr_document_max_bytes+65536
        length = request.headers.get("content-length")
        if length and (not length.isdigit() or int(length) > limit):
            return JSONResponse({"error": {"code": "REQUEST_TOO_LARGE"}}, status_code=413)
        if request.method in ("POST", "PUT", "PATCH") and length is None:
            return JSONResponse({"error": {"code": "CONTENT_LENGTH_REQUIRED"}}, status_code=411)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        return response

    def principal(request: Request, credential: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> Principal:
        limiter.check("ip:" + (request.client.host if request.client else "unknown"))
        if not credential or len(credential.credentials) > 256:
            raise DomainError("UNAUTHENTICATED", 401)
        token_hash = hashlib.sha256(credential.credentials.encode()).hexdigest()
        with sessions() as db:
            identity = db.scalar(select(Identity).where(Identity.token_hash == token_hash, Identity.active.is_(True)))
            if not identity:
                raise DomainError("UNAUTHENTICATED", 401)
            return Principal(user_id=identity.id, tenant_id=identity.tenant_id,
                             role=Role(identity.role), classroom_ids=identity.classroom_ids)

    def session():
        with sessions() as db:
            yield db

    User = Annotated[Principal, Depends(principal)]
    DB = Depends(session)

    @app.get("/health/live")
    def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    def ready(db=DB):
        db.execute(select(Identity.id).limit(1))
        db.execute(select(WritingRevision.id).limit(1))
        return {"status": "ready"}

    @app.get("/api/v1/runtime-status")
    def runtime_status(user: User):
        from inkwise.settings import model_configured
        from inkwise.ocr import ocr_configured
        return {"modelConfigured": model_configured(settings), "ocrConfigured": ocr_configured(settings)}

    @app.get("/api/v1/me")
    def me(user: User):
        return user.model_dump(by_alias=True)

    @app.get("/api/v1/rubrics")
    def list_rubrics(user: User):
        return {"items": rubrics.list(user.tenant_id)}

    @app.get("/api/v1/learning")
    def learning(user: User):
        from inkwise.evolution import list_proposals,list_experiments
        experiments=[row for row in list_experiments(settings,user.tenant_id) if user.role==Role.ADMIN or set(row['classrooms'])<=set(user.classroom_ids)]
        return {'proposals':list_proposals(settings,user),'experiments':experiments}

    @app.post("/api/v1/jobs/{job_id}/experiment-withdrawal")
    def withdraw_experiment(job_id: str,body: Versioned,user: User,db=DB):
        job=get_job(db,user,job_id)
        locked=db.execute(update(Job).where(Job.id==job.id,Job.version==body.version).values(updated_at=Job.updated_at))
        if locked.rowcount!=1:raise DomainError('VERSION_CONFLICT')
        assigned = db.scalar(select(JobEvent.id).where(
            JobEvent.job_id == job.id,
            JobEvent.tenant_id == job.tenant_id,
            JobEvent.event == 'experiment.assigned',
        ).limit(1))
        # Keep historical participation revocable, including completed reports.
        if (assigned is None and not (job.result or {}).get('experiment')
                and job.payload.get('experimentParticipation') != 'withdrawn'):
            raise DomainError('INVALID_STATE')
        exists=db.scalar(select(JobEvent.id).where(JobEvent.job_id==job.id,JobEvent.event=='experiment.withdrawn').limit(1))
        if exists is None:event(db,job,user.user_id,'experiment.withdrawn')
        # Independent participation state must not invalidate a running grading lease.
        job.payload={**job.payload,'experimentConsent':False,'experimentParticipation':'withdrawn'}
        db.commit()
        return {'id':job.id,'experimentParticipation':'withdrawn','gradeUnchanged':True}

    @app.post("/api/v1/learning/run")
    def learning_run(user: User):
        if user.role!=Role.ADMIN:raise DomainError('FORBIDDEN',403)
        from inkwise.evolution import learning_cycle
        if not learning_slots.acquire(blocking=False):raise DomainError('LEARNING_PROCESSOR_BUSY',429)
        try:return learning_cycle(settings,user.tenant_id)
        finally:learning_slots.release()

    @app.post("/api/v1/learning/proposals/{proposal_id}")
    def learning_decision(proposal_id: str,body: ProposalAction,user: User):
        from inkwise.evolution import proposal_action
        return proposal_action(settings,user,proposal_id,body)

    @app.post("/api/v1/experiments")
    def experiment_create(body: dict,user: User):
        from inkwise.evolution import create_experiment,Experiment
        # Revalidate a bounded strict contract; no client-controlled tenant or actor.
        try:document=Experiment.model_validate(body).model_dump(by_alias=True)
        except ValueError:raise DomainError('INVALID_REQUEST',422) from None
        return create_experiment(settings,user.tenant_id,document,user.user_id)

    @app.post("/api/v1/experiments/{experiment_id}/control")
    def experiment_control(experiment_id: str,body: ExperimentControl,user: User):
        from inkwise.evolution import control_experiment
        return control_experiment(settings,user,experiment_id,body)

    @app.post("/api/v1/experiments/{experiment_id}/evaluate")
    def experiment_evaluate(experiment_id: str,user: User):
        from inkwise.evolution import list_experiments,authorize_reviewer,evaluate_experiment
        experiment=next((row for row in list_experiments(settings,user.tenant_id) if row['id']==experiment_id),None)
        if not experiment:raise DomainError('NOT_FOUND',404)
        authorize_reviewer(settings,user.tenant_id,user.user_id,experiment['classrooms'])
        if not learning_slots.acquire(blocking=False):raise DomainError('LEARNING_PROCESSOR_BUSY',429)
        try:return evaluate_experiment(settings,user.tenant_id,experiment_id)
        finally:learning_slots.release()

    @app.post("/api/v1/language-preview")
    def language_preview(body: LanguagePreview,user: User):
        from inkwise.language import detect_language_spans
        if not language_slots.acquire(blocking=False):raise DomainError('LANGUAGE_PROCESSOR_BUSY',429)
        try:return detect_language_spans(body.text,body.policy)
        finally:language_slots.release()

    @app.post("/api/v1/intent")
    def intent(body: IntentRequest,user: User):
        from inkwise.orchestration import resolve_intent,plan_for
        value=resolve_intent(body.mode,body.request)
        return {'intent':value,'steps':plan_for(value)}

    @app.get("/api/v1/jobs/{job_id}/explanation")
    def explanation(job_id: str,user: User,db=DB):
        from inkwise.orchestration import explain_report
        return explain_report(get_job(db,user,job_id))

    @app.post("/api/v1/jobs/{job_id}/feedback-approval")
    def approve_feedback(job_id: str,body: FeedbackApproval,user: User,db=DB):
        job=get_job(db,user,job_id)
        if job.status!='teacher_review' or not job.result or job.result.get('kind')!='feedback':raise DomainError('REPORT_NOT_READY')
        if not body.comment.strip():raise DomainError('INVALID_REQUEST',422)
        from inkwise.contracts import Rubric
        from inkwise.providers import validate_evidence
        rubric=Rubric.model_validate(job.rubric)
        for finding in body.findings:
            if finding.dimension not in ('grammar','vocabulary'):raise DomainError('PROOFREAD_DIMENSION_INVALID',422)
            finding.evidence=validate_evidence(finding.evidence,job.text,rubric,finding.dimension)
        review={'kind':'feedback','comment':body.comment,'reviewerId':user.user_id,'reviewedAt':now().isoformat(),
                'textHash':job.text_hash,'findings':[finding.model_dump(by_alias=True) for finding in body.findings]}
        return snapshot(mutate(db,job,body.version,user,'feedback.approved',review=review,status='approved'))

    @app.get("/api/v1/library")
    def library(user: User, language: str = "zh", grade: int = Query(default=7,ge=1,le=12), query: str = Query(default="",max_length=1000)):
        from inkwise.reference import search_references
        return search_references(settings.reference_pack,language,grade,query)

    @app.post("/api/v1/jobs", status_code=201)
    def submit(body: JobCreate, user: User, idempotency_key: Annotated[str, Header(min_length=1, max_length=100)], db=DB):
        return snapshot(create_job(db, user, body, idempotency_key, settings.max_pending_per_tenant))

    @app.get("/api/v1/jobs")
    def list_jobs(user: User, limit: Annotated[int, Query(ge=1, le=50)] = 20,
                  offset: Annotated[int, Query(ge=0, le=10000)] = 0, search: str = Query(default="",max_length=100),
                  status: Literal["", "needs_text_review", "ocr_queued", "ocr_running", "queued", "running", "teacher_review", "approved", "published", "failed", "cancelled"] = Query(default=""),
                  attention: Literal["", "needs_action"] = Query(default=""),
                  classroom: str = Query(default="", max_length=80),
                  sort: Literal["created", "updated"] = Query(default="created"),
                  view: Literal["full", "summary"] = Query(default="full"), db=DB):
        clauses=job_scope(user)
        if classroom:
            authorize_classroom(user, classroom)
            clauses.append(Job.classroom_id == classroom)
        if search.strip():clauses.append(Job.payload["title"].as_string().icontains(search.strip(),autoescape=True))
        # Counts use the same authorized/classroom/search scope, before the selected status.
        counts = dict(db.execute(select(Job.status, func.count(Job.id)).where(*clauses).group_by(Job.status)).all())
        action_count = sum(counts.get(state, 0) for state in JOB_QUEUE_ACTIONS)
        if status:
            clauses.append(Job.status == status)
        if attention == "needs_action":
            clauses.append(Job.status.in_(tuple(JOB_QUEUE_ACTIONS)))
        if view == "summary":
            # Project only navigation metadata in SQL; do not load essays or reports.
            statement = select(
                Job.id, Job.version, Job.status, Job.classroom_id,
                Job.payload["title"].as_string().label("title"),
                Job.payload["grade"].as_integer().label("grade"),
                Job.created_at, Job.updated_at,
            )
        else:
            statement = select(Job)
        sort_column = Job.updated_at if sort == "updated" else Job.created_at
        rows = db.execute(statement.where(*clauses).order_by(sort_column.desc(), Job.id)
                          .limit(limit + 1).offset(offset)).all()
        if view == "summary":
            items = [{"id": row.id, "version": row.version, "status": row.status,
                      "classroomId": row.classroom_id, "payload": {"title": row.title, "grade": row.grade},
                      "queueAction": JOB_QUEUE_ACTIONS.get(row.status),
                      "createdAt": utc_iso(row.created_at), "updatedAt": utc_iso(row.updated_at)}
                     for row in rows[:limit]]
        else:
            items = [snapshot(row[0]) for row in rows[:limit]]
        return {"items": items, "limit": limit, "offset": offset, "hasMore": len(rows) > limit,
                "statusCounts": counts, "status": status, "attention": attention, "classroom": classroom,
                "actionRequiredCount": action_count, "view": view, "sort": sort}

    @app.get("/api/v1/jobs/{job_id}")
    def read_job(job_id: str, user: User, db=DB):
        job=get_job(db,user,job_id);view=snapshot(job)
        # Older withdrawals were recorded only in audit events.
        if view['experimentParticipation'] is None and (job.result or {}).get('experiment'):
            if db.scalar(select(JobEvent.id).where(JobEvent.job_id==job.id,JobEvent.tenant_id==user.tenant_id,
                JobEvent.event=='experiment.withdrawn').limit(1)) is not None:view['experimentParticipation']='withdrawn'
        return view

    @app.post("/api/v1/jobs/{job_id}/citation-location")
    def citation_location(job_id: str, body: CitationLookup, user: User, db=DB):
        return locate_citation(get_job(db, user, job_id), body)

    def attach_pages(job_id,user,version,files,db,append=False,replace_page=None):
        job=get_job(db,user,job_id)
        editable=job.status=='needs_text_review' or (job.status=='failed' and job.text_version==0)
        if not editable or job.version!=version:raise DomainError('VERSION_OR_STATE_CONFLICT')
        if job.image_id and not append and replace_page is None:raise DomainError('IMAGE_ALREADY_ATTACHED')
        from inkwise.document_ocr import existing_pages
        previous=existing_pages(job,settings)
        archived=list((job.ocr or {}).get('supersededPages',[]))
        if replace_page is not None:
            if len(files)!=1 or not 1<=replace_page<=len(previous):raise DomainError('PAGE_ORDER_INVALID',422)
            if len(archived)>=settings.ocr_max_pages*2:raise DomainError('OCR_REPLACEMENT_LIMIT',422)
        elif not files or len(previous)+len(files)>settings.ocr_max_pages:raise DomainError('OCR_PAGE_LIMIT',422)
        if not image_slots.acquire(blocking=False):raise DomainError('IMAGE_PROCESSOR_BUSY',429)
        created=[];pages=list(previous);size=0
        try:
            with image_admission(settings) as admission:
                committing = False
                try:
                    for image in files:
                        data=image.file.read(settings.max_upload_bytes+1);size+=len(data)
                        if size>settings.ocr_document_max_bytes:raise DomainError('IMAGE_TOO_LARGE',413)
                        content=normalize_image(data,settings);checksum=hashlib.sha256(content).hexdigest()
                        if any(page['sha256']==checksum for page in pages+archived):raise DomainError('DUPLICATE_PAGE',422)
                        admission.reserve(len(content))
                        identifier=str(uuid4());path=settings.data_dir/'images'/(identifier+'.png')
                        store_image(path,content);created.append(path)
                        if replace_page is None:pages.append({'imageId':identifier,'sha256':checksum,'page':len(pages)+1})
                        else:
                            archived.append(pages[replace_page-1])
                            pages[replace_page-1]={'imageId':identifier,'sha256':checksum,'page':replace_page}
                    committing = True
                    return mutate(db,job,version,user,'image.attached',image_id=pages[0]['imageId'],
                        ocr={'pages':pages,'supersededPages':archived,'stage':'queued'},status='ocr_queued',result=None,review=None,error_code=None)
                except BaseException as error:
                    # A lost DB acknowledgement may follow a successful commit.
                    # Retain complete originals until that outcome is reconciled.
                    if not committing or isinstance(error, DomainError):
                        for path in created:path.unlink(missing_ok=True)
                    raise
        finally:image_slots.release()

    @app.post("/api/v1/jobs/{job_id}/image")
    def upload_image(job_id: str,user: User,version: Annotated[int,Form(ge=1)],image: Annotated[UploadFile,File()],db=DB):
        return snapshot(attach_pages(job_id,user,version,[image],db))

    @app.post("/api/v1/jobs/{job_id}/pages")
    def upload_pages(job_id: str,user: User,version: Annotated[int,Form(ge=1)],pages: Annotated[list[UploadFile],File()],db=DB):
        return snapshot(attach_pages(job_id,user,version,pages,db,append=True))

    @app.post("/api/v1/jobs/{job_id}/page-replacement")
    def replace_page(job_id: str,user: User,version: Annotated[int,Form(ge=1)],page: Annotated[int,Form(ge=1,le=10)],image: Annotated[UploadFile,File()],db=DB):
        return snapshot(attach_pages(job_id,user,version,[image],db,replace_page=page))

    @app.post("/api/v1/jobs/{job_id}/page-order")
    def page_order(job_id: str,user: User,body: PageOrder,db=DB):
        job=get_job(db,user,job_id)
        if job.status!='needs_text_review':raise DomainError('INVALID_STATE')
        from inkwise.document_ocr import existing_pages
        pages=existing_pages(job,settings);by_id={page['imageId']:page for page in pages}
        if len(body.image_ids)!=len(pages) or set(body.image_ids)!=set(by_id):raise DomainError('PAGE_ORDER_INVALID',422)
        ordered=[{**by_id[key],'page':i+1} for i,key in enumerate(body.image_ids)]
        return snapshot(mutate(db,job,body.version,user,'image.reordered',image_id=ordered[0]['imageId'],
            ocr={'pages':ordered,'supersededPages':(job.ocr or {}).get('supersededPages',[]),'stage':'queued'},status='ocr_queued',result=None,review=None))

    @app.get("/api/v1/jobs/{job_id}/image")
    def image(job_id: str,user: User,page: int=Query(default=1,ge=1,le=20),archived: bool=False,db=DB):
        job=get_job(db,user,job_id)
        pages=((job.ocr or {}).get('supersededPages',[]) if archived else (job.ocr or {}).get('pages') or ([{'imageId':job.image_id}] if job.image_id else []))
        if page>len(pages):raise DomainError('NOT_FOUND',404)
        path=settings.data_dir/'images'/(pages[page-1]['imageId']+'.png')
        if not path.is_file():raise DomainError('IMAGE_UNAVAILABLE',404)
        return FileResponse(path,media_type='image/png',headers={'Cache-Control':'no-store'})

    @app.post("/api/v1/jobs/{job_id}/text-confirmations")
    def text_confirmation(job_id: str, body: TextConfirmation, user: User, db=DB):
        return snapshot(confirm_text(db, user, job_id, body, rubrics,settings))

    @app.post("/api/v1/jobs/{job_id}/cancel")
    def cancel(job_id: str, body: Versioned, user: User, db=DB):
        return snapshot(cancel_job(db, user, job_id, body.version))

    @app.post("/api/v1/jobs/{job_id}/erasures")
    def erase(job_id: str, body: Versioned, user: User, db=DB):
        from inkwise.privacy import erase_job
        return erase_job(db,user,job_id,body.version,settings.data_dir)

    @app.post("/api/v1/jobs/{job_id}/retries")
    def retry(job_id: str, body: Versioned, user: User, db=DB):
        job = get_job(db, user, job_id)
        if job.status != "failed":
            raise DomainError("INVALID_STATE")
        if db.scalar(select(CallReceipt.id).where(CallReceipt.job_id==job.id,
                     CallReceipt.status.in_(["started","unknown"])).limit(1)):
            raise DomainError("PROVIDER_RESULT_UNKNOWN_REQUIRES_REVIEW")
        if job.text_version and job.rubric:
            state = "queued"
        elif job.image_id:
            state = "ocr_queued"
        else:
            state = "needs_text_review"
        return snapshot(mutate(db, job, body.version, user, "job.retry_requested", status=state,
                                error_code=None, lease_id=None, lease_until=None))

    @app.post("/api/v1/jobs/{job_id}/reviews")
    def review(job_id: str, body: Review, user: User, db=DB):
        return snapshot(review_job(db, user, job_id, body))

    @app.get("/api/v1/jobs/{job_id}/revisions")
    def revisions(job_id: str, user: User, db=DB):
        return list_revisions(db, user, job_id)

    @app.post("/api/v1/jobs/{job_id}/revisions", status_code=201)
    def new_revision(job_id: str, body: Versioned, user: User,
                     idempotency_key: Annotated[str, Header(min_length=1, max_length=100)], db=DB):
        return create_revision(db, user, job_id, body, idempotency_key, settings)

    @app.get("/api/v1/jobs/{job_id}/revisions/{revision_id}")
    def read_revision(job_id: str, revision_id: str, user: User, db=DB):
        job = source_job(db, user, job_id)
        return revision_view(job, get_revision(db, user, job, revision_id))

    @app.patch("/api/v1/jobs/{job_id}/revisions/{revision_id}")
    def save_revision(job_id: str, revision_id: str, body: RevisionEdit, user: User, db=DB):
        return edit_revision(db, user, job_id, revision_id, body, settings)

    @app.post("/api/v1/jobs/{job_id}/revisions/{revision_id}/confirmation")
    def confirm_writing_revision(job_id: str, revision_id: str, body: RevisionConfirmation, user: User, db=DB):
        return confirm_revision(db, user, job_id, revision_id, body, settings)

    @app.get("/api/v1/jobs/{job_id}/revisions/{revision_id}/reassessment")
    def revision_reassessment(job_id: str, revision_id: str, user: User, db=DB):
        return read_reassessment(db, user, job_id, revision_id)

    @app.post("/api/v1/jobs/{job_id}/revisions/{revision_id}/reassessment", status_code=201)
    def new_reassessment(job_id: str, revision_id: str, body: RevisionConfirmation, user: User,
                         idempotency_key: Annotated[str, Header(min_length=1, max_length=100)], db=DB):
        return request_reassessment(db, user, job_id, revision_id, body, idempotency_key, settings)

    @app.get("/api/v1/jobs/{job_id}/handoff")
    def handoff(job_id: str, user: User, version: Annotated[int, Query(ge=1)], db=DB):
        return task_handoff(db, user, job_id, version)

    def checked_report(job_id, user, version, db):
        record = get_job(db, user, job_id)
        if record.version != version:
            raise DomainError("VERSION_CONFLICT")
        if record.status == "erased":
            raise DomainError("NOT_FOUND", 404)
        readiness = inspect_report(record)
        if db.scalar(select(Job.version).where(Job.id == job_id, *job_scope(user))) != version:
            raise DomainError("VERSION_CONFLICT")
        return record, readiness

    @app.get("/api/v1/jobs/{job_id}/report-readiness")
    def report_readiness(job_id: str, user: User, version: Annotated[int, Query(ge=1)], db=DB):
        return checked_report(job_id, user, version, db)[1]

    @app.get("/api/v1/jobs/{job_id}/export-snapshot")
    def export_snapshot(job_id: str, user: User, version: Annotated[int, Query(ge=1)],
                        purpose: Literal['diagnostic','printable'] = 'diagnostic', db=DB):
        record, readiness = checked_report(job_id, user, version, db)
        if purpose == 'printable' and not readiness['canExport']:
            raise DomainError('REPORT_EXPORT_NOT_READY', 409, {'nextAction':readiness['nextAction']})
        result = record.result or {}
        return {"job": snapshot(record), "readiness": readiness, "binding": {
            "tenantId": record.tenant_id, "classroomId": record.classroom_id,
            "jobId": record.id, "jobVersion": record.version,
            "textVersion": record.text_version,
            "textHash": hashlib.sha256(record.text.encode("utf-8")).hexdigest(),
            "rubricRelease": (record.rubric or {}).get("release"),
            "modelRevision": result.get("modelRevision"),
            "pipelineRevision": result.get("pipelineRevision"),
            "exportedAt": utc_iso(now()),
        }}

    @app.get("/api/v1/jobs/{job_id}/events")
    def events(job_id: str, user: User, after: Annotated[int, Query(ge=0, le=2**63-1)] = 0,
               limit: Annotated[int, Query(ge=1, le=100)] = 100, db=DB):
        get_job(db, user, job_id)
        rows = db.scalars(select(JobEvent).where(JobEvent.tenant_id == user.tenant_id,
                JobEvent.job_id == job_id, JobEvent.id > after).order_by(JobEvent.id).limit(limit+1)).all()
        page = rows[:limit]
        return {"items": [{"id": e.id, "event": e.event, "version": e.job_version,
                           "createdAt": utc_iso(e.created_at)} for e in page],
                "nextCursor": page[-1].id if page else after, "hasMore": len(rows) > limit,
                "limit": limit}

    @app.post("/api/v1/jobs/{job_id}/candidates", status_code=201)
    def candidate(job_id: str, body: CandidateCreate, user: User, db=DB):
        result = create_candidate(db, user, job_id, body)
        return candidate_receipt(result)

    def candidate_scope(user: Principal, classroom=""):
        clauses = [Candidate.tenant_id == user.tenant_id]
        if user.role != Role.ADMIN:
            clauses.append(Candidate.classroom_id.in_(user.classroom_ids))
        if classroom:
            authorize_classroom(user, classroom)
            clauses.append(Candidate.classroom_id == classroom)
        return clauses

    def candidate_selection(user: Principal, classroom="", search="", status=""):
        clauses = candidate_scope(user, classroom)
        if status:
            clauses.append(Candidate.status == status)
        title = Job.payload["title"].as_string()
        if search.strip():
            term = search.strip()
            clauses.append(or_(title.icontains(term, autoescape=True),
                               Candidate.id.icontains(term, autoescape=True),
                               Candidate.job_id.icontains(term, autoescape=True)))
        return select(Candidate, title.label("source_title"), Job.version, Job.status).outerjoin(Job, and_(
            Job.id == Candidate.job_id, Job.tenant_id == Candidate.tenant_id,
            Job.classroom_id == Candidate.classroom_id)).where(*clauses)

    def candidate_source(job_id: str, user: Principal, db):
        # Authorize from small source metadata; status readback need not copy the
        # original text, images or full grading report into another ORM object.
        source = db.execute(select(Job.id, Job.version, Job.status, Job.classroom_id,
                                   Job.payload["title"].as_string().label("source_title")).where(
            Job.id == job_id, *job_scope(user))).one_or_none()
        if source is None:
            raise DomainError("NOT_FOUND", 404)
        return source

    @app.get("/api/v1/jobs/{job_id}/candidates")
    def job_candidate(job_id: str, user: User, db=DB):
        job = candidate_source(job_id, user, db)
        item = None if job.status == "erased" else db.scalar(select(Candidate).where(
            Candidate.tenant_id == user.tenant_id, Candidate.job_id == job.id,
            Candidate.classroom_id == job.classroom_id, Candidate.report_version == job.version))
        return {"jobId": job.id, "reportVersion": job.version, "candidate": candidate_receipt(item) if item else None}

    @app.get("/api/v1/candidates/{candidate_id}")
    def read_candidate(candidate_id: str, user: User, db=DB):
        item = db.scalar(select(Candidate).where(Candidate.id == candidate_id, Candidate.tenant_id == user.tenant_id))
        if not item:
            raise DomainError("NOT_FOUND", 404)
        authorize_classroom(user, item.classroom_id)
        job = candidate_source(item.job_id, user, db)
        if item.classroom_id != job.classroom_id:
            raise DomainError("NOT_FOUND", 404)
        return candidate_source_view(item, job.source_title, job.version, job.status)

    @app.get("/api/v1/candidates")
    def candidates(user: User, limit: Annotated[int, Query(ge=1, le=50)] = 20,
                   offset: Annotated[int,Query(ge=0,le=10000)] = 0,
                   status: Literal["", "pending", "approved", "rejected", "published", "withdrawn"] = Query(default=""),
                   classroom: str = Query(default="", max_length=80),
                   search: str = Query(default="", max_length=100), db=DB):
        # Source metadata must match both tenant and classroom, even for legacy rows.
        rows = db.execute(candidate_selection(user, classroom, search, status)
            .order_by(Candidate.created_at.desc(), Candidate.id).offset(offset).limit(limit + 1)).all()
        return {"items": [candidate_source_view(item, title, version, state)
                          for item, title, version, state in rows[:limit]],
                "offset": offset, "limit": limit, "hasMore": len(rows) > limit,
                "status": status, "classroom": classroom}

    @app.get("/api/v1/candidate-review-queue/next")
    def next_candidate_review(user: User,
                              after_id: str = Query(default="", max_length=80, pattern=r"^(?:[A-Za-z0-9][A-Za-z0-9_-]{0,79})?$"),
                              classroom: str = Query(default="", max_length=80),
                              search: str = Query(default="", max_length=100), db=DB):
        if user.role not in (Role.REVIEWER, Role.ADMIN):
            raise DomainError("FORBIDDEN", 403)
        query = candidate_selection(user, classroom, search, "pending").where(
            Candidate.submitted_by != user.user_id, Candidate.version >= 1, Candidate.report_version >= 1,
            Job.version == Candidate.report_version, Job.status.in_(("approved", "published")))
        if after_id:
            # Reviewed/withdrawn rows still anchor chronology; do not offset a shrinking queue.
            anchor = db.execute(select(Candidate.id, Candidate.created_at).where(
                Candidate.id == after_id, *candidate_scope(user, classroom))).one_or_none()
            if anchor is None:
                raise DomainError("NOT_FOUND", 404)
            query = query.where(or_(Candidate.created_at > anchor.created_at,
                and_(Candidate.created_at == anchor.created_at, Candidate.id > anchor.id)))
        row = db.execute(query.order_by(Candidate.created_at, Candidate.id).limit(1)).first()
        item = candidate_source_view(*row) if row else None
        return {"item": item, "nextAfter": item["id"] if item else None, "order": "oldest-created"}

    @app.post("/api/v1/candidates/{candidate_id}/revisions")
    def revise_case(candidate_id: str,body: CandidateCreate,user: User,db=DB):
        from inkwise.service import revise_candidate
        item=revise_candidate(db,user,candidate_id,body)
        return {'id':item.id,'status':item.status,'version':item.version}

    @app.post("/api/v1/candidates/{candidate_id}/withdraw")
    def withdraw(candidate_id: str, body: Versioned, user: User, db=DB):
        item = db.scalar(select(Candidate).where(Candidate.id == candidate_id, Candidate.tenant_id == user.tenant_id))
        if not item:
            raise DomainError("NOT_FOUND", 404)
        authorize_classroom(user, item.classroom_id)
        if item.submitted_by != user.user_id and user.role != Role.ADMIN:
            raise DomainError("FORBIDDEN", 403)
        changed = db.execute(update(Candidate).where(Candidate.id==item.id, Candidate.version==body.version,
                    Candidate.status.in_(["pending","approved","published","rejected"]))
                    .values(status="withdrawn",text="",provenance="",review={},version=body.version+1))
        if changed.rowcount!=1:
            raise DomainError("CANDIDATE_STATE_CONFLICT")
        db.refresh(item)
        job = get_job(db, user, item.job_id)
        event(db, job, user.user_id, "candidate.withdrawn")
        db.commit()
        return {"id": item.id, "status": item.status, "version":item.version}

    @app.post("/api/v1/candidates/{candidate_id}/decisions")
    def decide_candidate(candidate_id: str, body: CandidateDecision, user: User, db=DB):
        if user.role not in (Role.REVIEWER, Role.ADMIN):
            raise DomainError("FORBIDDEN", 403)
        item = db.scalar(select(Candidate).where(Candidate.id == candidate_id, Candidate.tenant_id == user.tenant_id))
        if not item:
            raise DomainError("NOT_FOUND", 404)
        authorize_classroom(user, item.classroom_id)
        if item.submitted_by == user.user_id:
            raise DomainError("INDEPENDENT_REVIEWER_REQUIRED", 403)
        state = "approved" if body.decision == "approve" else "rejected"
        changed = db.execute(update(Candidate).where(Candidate.id == item.id, Candidate.status == "pending",
                    Candidate.version==body.version).values(status=state, reviewed_by=user.user_id,
                    decision_reason=body.reason,reviewed_at=now(),version=body.version+1))
        if changed.rowcount != 1:
            raise DomainError("CANDIDATE_STATE_CONFLICT")
        job = get_job(db, user, item.job_id)
        event(db, job, user.user_id, "candidate." + state, {"candidateId": item.id})
        db.commit()
        return {"id": item.id, "status": state, "version":body.version+1}

    if settings.served_frontend_dir.is_dir():
        app.mount("/workspace", StaticFiles(directory=settings.served_frontend_dir, html=True), name="workspace")

    @app.get("/")
    def home():
        return RedirectResponse("/workspace/")

    return app
