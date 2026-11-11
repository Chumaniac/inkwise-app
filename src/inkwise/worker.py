"""Durable leased worker with paid-call receipts and stale-result rejection."""

import asyncio
import logging
import hashlib
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from inkwise.contracts import Rubric
from inkwise.database import CallReceipt, Candidate, Job, JobEvent, create_session_factory, now
from inkwise.errors import DomainError, FAILURE_CATEGORIES, failure_category
from inkwise.grading import build_grading_graph
from inkwise.ocr import recognize_image
from inkwise.providers import CallBudget
from inkwise.model_receipts import ReceiptGateway, lock_job_lease
from inkwise.retrieval import HybridRetriever, Scope
from inkwise.rubrics import canonical, digest
from inkwise.settings import Settings


def claim_job(sessions, settings):
    with sessions.begin() as db:
        candidate = db.scalar(select(Job).where(Job.status.in_(["queued", "ocr_queued"]))
                              .order_by(Job.created_at, Job.id).limit(1))
        if candidate is None:
            return None
        lease = str(uuid4())
        status = "ocr_running" if candidate.status == "ocr_queued" else "running"
        result = db.execute(update(Job).where(Job.id == candidate.id, Job.version == candidate.version,
                            Job.status == candidate.status).values(status=status, lease_id=lease,
                            lease_until=now() + timedelta(seconds=settings.job_timeout_seconds + 30),
                            version=candidate.version + 1, updated_at=now()))
        if result.rowcount != 1:
            return None
        db.refresh(candidate)
        db.add(JobEvent(job_id=candidate.id, tenant_id=candidate.tenant_id, actor_id="worker",
                         event=status, job_version=candidate.version, detail={}))
        return candidate


def finalize(sessions, job, *, status, diagnostic_category=None, **values):
    if diagnostic_category is not None and diagnostic_category not in FAILURE_CATEGORIES:
        raise ValueError('Invalid diagnostic category')
    with sessions.begin() as db:
        if status == 'teacher_review':
            from inkwise.reassessments import assert_assessment_binding
            current = db.get(Job, job.id)
            if current is not None:
                assert_assessment_binding(db, current)
        result = db.execute(update(Job).execution_options(synchronize_session=False).where(Job.id == job.id, Job.tenant_id == job.tenant_id,
                            Job.lease_id == job.lease_id, Job.version == job.version,
                            Job.status.in_(["running", "ocr_running"]), Job.lease_until > now())
                .values(status=status, lease_id=None, lease_until=None, updated_at=now(),
                        version=job.version + 1, **values))
        if result.rowcount:
            db.add(JobEvent(job_id=job.id, tenant_id=job.tenant_id, actor_id="worker", event=status,
                             job_version=job.version + 1, detail={"failureCategory": diagnostic_category} if diagnostic_category else {}))
        return result.rowcount == 1


def recover_expired(sessions):
    """A dead worker never silently repeats an unknown external inference call."""
    with sessions.begin() as db:
        expired = db.scalars(select(Job).where(Job.status.in_(["running", "ocr_running"]),
                                              Job.lease_until < now()).limit(100)).all()
        for job in expired:
            changed = db.execute(update(Job).where(Job.id == job.id, Job.version == job.version,
                          Job.lease_id == job.lease_id).values(status="failed", error_code="WORKER_INTERRUPTED",
                          lease_id=None, lease_until=None, version=job.version + 1, updated_at=now()))
            if changed.rowcount:
                db.add(JobEvent(job_id=job.id, tenant_id=job.tenant_id, actor_id="worker",
                    event="worker.interrupted", job_version=job.version + 1, detail={}))


def load_allowed_cases(sessions,scope,rubric,hits,reference_pack=None):
    """Recheck current consent and the source task's grade/standard in SQL."""
    from inkwise.knowledge import compatible_anchor
    references=[hit for hit in hits if hit.metadata.get('sourceKind')=='curriculum-reference']
    allowed_references=set()
    if references and reference_pack:
        from inkwise.reference import validate_reference_hits
        allowed_references=validate_reference_hits(reference_pack,scope,references)
    hits=[hit for hit in hits if hit.metadata.get('sourceKind')!='curriculum-reference']
    ids={hit.metadata.get('source') for hit in hits}
    with sessions() as db:
        rows=db.execute(select(Candidate,Job).join(Job,Job.id==Candidate.job_id).where(
            Candidate.tenant_id==scope.tenant,Job.tenant_id==scope.tenant,Candidate.id.in_(ids),
            Candidate.status.in_(['approved','published']))).all()
        allowed={c.id:c for c,source_job in rows if c.reviewed_by and c.reviewed_by!=c.submitted_by
            and c.rubric.get('id')==scope.rubric_id and source_job.payload.get('grade')==scope.grade
            and compatible_anchor(c.rubric,rubric.model_dump(by_alias=True),scope.grade)}
        return allowed_references | {hit.id for hit in hits if (case:=allowed.get(hit.metadata.get('source')))
            and case.report_version==hit.metadata.get('sourceReportVersion')
            and digest(case.text)==hit.metadata.get('textHash')}


async def process_job(job, settings, sessions, gateway=None, retriever=None):
    owned_gateway = gateway is None
    try:
        async with asyncio.timeout(settings.job_timeout_seconds) as job_timeout:
            def ensure_lease():
                with sessions.begin() as db:
                    lock_job_lease(db,job,status=job.status)
            await asyncio.to_thread(ensure_lease)
            if job.status == "ocr_running":
                from inkwise.document_ocr import existing_pages,recognize_document
                pages=await asyncio.to_thread(existing_pages,job,settings)
                payload=await recognize_document(pages,settings,tenant=job.tenant_id,job_id=job.id,
                    language_policy=job.payload['languagePolicy'])
                payload['supersededPages']=(job.ocr or {}).get('supersededPages',[])
                await asyncio.to_thread(finalize, sessions, job, status="needs_text_review", ocr=payload, text=payload["text"], error_code=None)
                return
            rubric = Rubric.model_validate(job.rubric)
            from inkwise.evolution import analysis_rubric
            actual_rubric=rubric
            rubric=await asyncio.to_thread(analysis_rubric,settings,sessions,job,rubric)
            scope = Scope(job.tenant_id, rubric.release, rubric.language_policy, rubric.genre,
                          job.payload["grade"], job.text_hash)
            route_plan=None
            if gateway is None:
                from inkwise.routing import select_route
                routed_settings,route_plan=await select_route(settings,sessions,job)
                gateway=ReceiptGateway(routed_settings,sessions,job)
                gateway.route_plan=route_plan
            from inkwise.language import language_context
            routing=await asyncio.to_thread(language_context,job.text,rubric)
            gateway.language_context=routing
            if not routing["eligibleLanguages"]:
                raise DomainError("LANGUAGE_REQUIRES_TEACHER")
            chunk_findings=[]
            if settings.kafka_enabled and len(job.text)>2000:
                from inkwise.chunks import await_chunks
                chunk_findings=await await_chunks(settings,sessions,job,deadline=job_timeout.when())
            async def notify(kind):
                def write():
                    with sessions.begin() as db:
                        lock_job_lease(db, job)
                        db.add(JobEvent(job_id=job.id, tenant_id=job.tenant_id, actor_id="worker", event=kind,
                                         job_version=job.version, detail={}))
                await asyncio.to_thread(write)
            async def validate_cases(scope, hits):
                return await asyncio.to_thread(load_allowed_cases,sessions,scope,rubric,hits,settings.reference_pack)
            from inkwise.orchestration import teacher_feedback_pool,proofread_job,plan_for
            intent=job.payload.get('intent','assess')
            from langsmith import tracing_context
            with tracing_context(enabled=False):
                if intent=='proofread':
                    result={'report':await proofread_job(gateway,job,rubric,CallBudget(settings.model_calls_per_job),
                        chunk_findings if settings.kafka_enabled and len(job.text)>2000 else None)}
                else:
                    feedback_pool=await asyncio.to_thread(teacher_feedback_pool,sessions,job)
                    with sessions() as db:
                        trial=db.scalar(select(JobEvent.id).where(JobEvent.job_id==job.id,JobEvent.event=='experiment.assigned').limit(1))
                    if trial:feedback_pool=[]
                    graph=build_grading_graph(gateway,retriever or HybridRetriever(settings,case_validator=validate_cases),scope,rubric,
                        CallBudget(settings.model_calls_per_job),notify,chunk_findings,feedback_pool)
                    result=await graph.ainvoke({'text':job.text,'prompt':job.payload['prompt']},{'recursion_limit':15})
            result['report']['orchestration']={'intent':intent,'steps':plan_for(intent)}
            result['report']['modelRevision']=gateway.settings.model_revision or None
            result['report']['pipelineRevision']=settings.pipeline_revision
            from inkwise.evolution import report_experiment
            experiment=await asyncio.to_thread(report_experiment,settings,sessions,job,result['report'])
            if experiment:
                result['report']['experiment']=experiment
                result['report']['analysisRelease']=rubric.release
                result['report']['release']=actual_rubric.release
                if experiment['mode']=='live' and experiment.get('status')=='active':result['report']['warnings'].append('LIVE_WEIGHT_EXPERIMENT')
            result['report']['warnings']=sorted(set(result['report'].get('warnings',[])+routing['warnings']))
            if intent=='assess':
                if not experiment:
                    from inkwise.evolution import aggregation_record,apply_model_weights
                    aggregation=await asyncio.to_thread(aggregation_record,settings,job.tenant_id,actual_rubric,result['report']['modelRevision'],settings.pipeline_revision)
                    if aggregation:await asyncio.to_thread(apply_model_weights,settings,job.tenant_id,actual_rubric,result['report'],aggregation)
                from inkwise.calibration import apply_calibration
                calibration=await asyncio.to_thread(apply_calibration,gateway.settings,job.tenant_id,actual_rubric,result['report'],job.payload['grade'])
                result['report']['calibration']=calibration;result['report']['confidence']=calibration['confidence']
            if route_plan:
                result['report']['modelRoute']={key:route_plan['route'][key] for key in ('id','location','model','revision','contextTokens')}
                result['report']['modelRoute']['estimatedMaximumCostMicrousd']=route_plan['estimatedMaximumCostMicrousd']
            result["report"]["localFindings"]=chunk_findings
            result["report"]["languageRouting"]=routing
            def call_count():
                with sessions() as db:
                    return db.scalar(select(func.coalesce(func.sum(CallReceipt.attempt),0)).where(CallReceipt.job_id==job.id))
            result["report"]["modelCallsTotal"]=await asyncio.to_thread(call_count)
            await asyncio.to_thread(finalize, sessions, job, status="teacher_review", result=result["report"], error_code=None)
    except asyncio.CancelledError:
        await asyncio.shield(asyncio.to_thread(finalize, sessions, job, status="failed", error_code="WORKER_INTERRUPTED"))
        raise
    except (DomainError, TimeoutError) as exc:
        code = exc.code if isinstance(exc, DomainError) else "JOB_TIMEOUT_UNKNOWN"
        await asyncio.to_thread(finalize, sessions, job, status="failed", error_code=code, diagnostic_category=failure_category(exc))
    except Exception as error:
        category=failure_category(error)
        logging.getLogger(__name__).warning('worker.processing_failed category=%s', category)
        await asyncio.to_thread(finalize, sessions, job, status="failed", error_code="PROCESSING_FAILED", diagnostic_category=category)
    finally:
        if owned_gateway and gateway:
            await gateway.close()


async def run_worker(settings: Settings):
    engine, sessions = create_session_factory(settings.database_url)
    try:
        while True:
            await asyncio.to_thread(recover_expired, sessions)
            job = await asyncio.to_thread(claim_job, sessions, settings)
            if job:
                await process_job(job, settings, sessions)
            else:
                await asyncio.sleep(1)
    finally:
        engine.dispose()
