"""Kafka-backed, idempotent chunk evidence extraction; holistic scores stay global."""

import asyncio
import json
import logging
import math
from uuid import UUID, uuid4

from pydantic import Field
from sqlalchemy import select, update

from inkwise.contracts import Contract, Dimension, Evidence, Rubric
from inkwise.core import split_text
from inkwise.database import CallReceipt, ChunkTask, Job, JobEvent, Outbox, create_session_factory, now
from inkwise.errors import DomainError, failure_category
from inkwise.providers import CallBudget, validate_evidence
from inkwise.rubrics import canonical, digest


class Finding(Contract):
    dimension: Dimension
    evidence: Evidence
    suggestion: str = Field(min_length=1,max_length=700)


class ChunkFeedback(Contract):
    findings: list[Finding] = Field(max_length=12)


def prepare_chunks(sessions,job,settings):
    chunks=split_text(job.text,limit=1000,overlap=80)
    reserve=0 if job.payload.get('intent')=='proofread' else 10
    if len(chunks)+reserve>settings.model_calls_per_job:
        raise DomainError("LONG_TEXT_BUDGET_NOT_CONFIGURED")
    manifest=digest(canonical({"text":job.text_hash,"rubric":job.rubric,"model":settings.model_name,
                               "endpoint":settings.model_base_url}))
    with sessions.begin() as db:
        locked=db.execute(update(Job).where(Job.id==job.id,Job.lease_id==job.lease_id,Job.status=="running",Job.lease_until>now()).values(lease_until=Job.lease_until))
        if locked.rowcount!=1:raise DomainError("JOB_CANCELLED")
        # Legacy receipts have no per-chunk scope column. A resumed phase must
        # reconcile any started/unknown extraction before resetting or creating
        # chunks, even if diagnostic/model input hashes changed across restarts.
        unresolved=db.scalar(select(CallReceipt.id).where(CallReceipt.job_id==job.id,
            CallReceipt.stage.like('ChunkFeedback:%'),CallReceipt.status.in_(['started','unknown'])).limit(1))
        if unresolved:raise DomainError('PROVIDER_RESULT_UNKNOWN_REQUIRES_REVIEW')
        existing=db.scalars(select(ChunkTask).where(ChunkTask.job_id==job.id,ChunkTask.manifest_hash==manifest)
                            .order_by(ChunkTask.ordinal)).all()
        if existing:
            if len(existing)!=len(chunks):
                raise DomainError("CHUNK_MANIFEST_INCOMPLETE")
            retry_ids=[row.id for row in existing if row.status!="succeeded"]
            if retry_ids:
                db.execute(update(ChunkTask).where(ChunkTask.id.in_(retry_ids)).values(status="queued",lease_id=None,error_code=None))
                db.execute(update(Outbox).where(Outbox.chunk_id.in_(retry_ids)).values(delivered=False))
            return manifest,len(chunks)
        for ordinal,chunk in enumerate(chunks):
            identifier=str(uuid4())
            db.add(ChunkTask(id=identifier,tenant_id=job.tenant_id,job_id=job.id,manifest_hash=manifest,
                            ordinal=ordinal,start=chunk.start,end=chunk.end,context_start=chunk.context_start,
                            context_end=chunk.context_end,status="queued"))
            db.add(Outbox(id=str(uuid4()),job_id=job.id,chunk_id=identifier))
    return manifest,len(chunks)


async def dispatch_outbox(settings,sessions):
    from aiokafka import AIOKafkaProducer
    producer=AIOKafkaProducer(bootstrap_servers=settings.kafka_bootstrap_servers,enable_idempotence=True,
                             request_timeout_ms=10000,max_request_size=16384)
    try:
        await producer.start()
        with sessions() as db:
            rows=db.scalars(select(Outbox).where(Outbox.delivered.is_(False)).order_by(Outbox.created_at).limit(100)).all()
        for row in rows:
            # No essay or student metadata crosses the broker boundary.
            await producer.send_and_wait(settings.kafka_topic,json.dumps({"chunkId":row.chunk_id}).encode(),key=row.chunk_id.encode())
            def mark():
                with sessions.begin() as db:
                    db.execute(update(Outbox).where(Outbox.id==row.id).values(delivered=True))
            await asyncio.to_thread(mark)
    finally:
        await producer.stop()


async def await_chunks(settings,sessions,job,*,deadline=None):
    """Bound waiting and retain last-observed counts; never retry model calls here."""
    loop=asyncio.get_running_loop()
    remaining=settings.job_timeout_seconds if deadline is None else deadline-loop.time()
    if not math.isfinite(remaining):raise DomainError('CHUNK_DEADLINE_INVALID')
    # Leave a small interval to persist the phase outcome before the parent timer.
    effective_deadline=loop.time()+remaining-min(.25,max(0,remaining)/10)
    manifest=None;count=0;rows=[];previous=None
    def record(status):
        nonlocal previous
        detail={'total':count,'completed':sum(r.status=='succeeded' for r in rows),
                'failed':sum(r.status=='failed' for r in rows),'running':sum(r.status=='running' for r in rows),
                'queued':sum(r.status=='queued' for r in rows),'waitStatus':status}
        if detail==previous:return
        from inkwise.model_receipts import lock_job_lease
        with sessions.begin() as db:
            lock_job_lease(db,job)
            db.add(JobEvent(job_id=job.id,tenant_id=job.tenant_id,actor_id='worker',event='chunks.progress',
                            job_version=job.version,detail=detail))
        previous=detail
    async def terminal(status):
        try:await asyncio.shield(asyncio.to_thread(record,status))
        except Exception as error:
            logging.getLogger(__name__).warning('chunks.progress unavailable category=%s',failure_category(error))
    try:
        async with asyncio.timeout_at(effective_deadline):
            manifest,count=await asyncio.to_thread(prepare_chunks,sessions,job,settings)
            await dispatch_outbox(settings,sessions)
            while True:
                def read():
                    with sessions() as db:
                        current=db.scalar(select(Job.id).where(Job.id==job.id,Job.status=='running',
                            Job.lease_id==job.lease_id,Job.lease_until>now()))
                        if not current:raise DomainError('JOB_CANCELLED')
                        return db.scalars(select(ChunkTask).where(ChunkTask.job_id==job.id,ChunkTask.manifest_hash==manifest)
                                           .order_by(ChunkTask.ordinal)).all()
                rows=await asyncio.to_thread(read)
                if len(rows)!=count:raise DomainError('CHUNK_MANIFEST_INCOMPLETE')
                if any(row.status=='failed' for row in rows):
                    await asyncio.to_thread(record,'failed')
                    raise DomainError('CHUNK_PROCESSING_FAILED')
                if all(row.status=='succeeded' for row in rows):
                    await asyncio.to_thread(record,'completed')
                    seen,findings=set(),[]
                    for row in rows:
                        for finding in row.result['findings']:
                            evidence=finding['evidence']
                            key=(finding['dimension'],evidence['start'],evidence['end'],evidence['ruleId'])
                            if key not in seen:
                                seen.add(key);findings.append(finding)
                    return findings
                await asyncio.to_thread(record,'waiting')
                await asyncio.sleep(.5)
    except TimeoutError:
        await terminal('deadline')
        raise DomainError('CHUNK_WAIT_TIMEOUT_UNKNOWN') from None
    except asyncio.CancelledError:
        await terminal('interrupted')
        raise


async def process_chunk(identifier,settings,sessions,gateway_factory=None):
    from inkwise.model_receipts import ReceiptGateway
    def claim():
        with sessions.begin() as db:
            row=db.get(ChunkTask,identifier)
            if not row or row.status=="succeeded": return None,None
            job=db.scalar(select(Job).where(Job.id==row.job_id,Job.lease_until>now()))
            if not job or job.status!="running" or row.tenant_id!=job.tenant_id: return None,None
            lease=str(uuid4())
            changed=db.execute(update(ChunkTask).where(ChunkTask.id==identifier,ChunkTask.status=="queued")
                                .values(status="running",lease_id=lease))
            if changed.rowcount!=1: return None,None
            db.refresh(row)
            return row,job
    row,job=await asyncio.to_thread(claim)
    if row is None:return
    gateway=None
    try:
        if gateway_factory:gateway=gateway_factory(job)
        else:
            from inkwise.routing import select_route
            routed,plan=await select_route(settings,sessions,job,require_existing=True)
            gateway=ReceiptGateway(routed,sessions,job);gateway.route_plan=plan;gateway.cache_scope=row.id
        rubric=Rubric.model_validate(job.rubric)
        context=job.text[row.context_start:row.context_end]
        from inkwise.language import language_context,evidence_protected
        routing=await asyncio.to_thread(language_context,context,rubric)
        feedback=await gateway.complete(ChunkFeedback,"只抽取局部文字问题，不给整体分数。只报告 core 范围内的原文证据；前后文仅供理解。",
            {"essayContext":context,"coreStart":row.start-row.context_start,"coreEnd":row.end-row.context_start,
             "rules":[r.model_dump(by_alias=True) for r in rubric.rules],"languagePolicy":rubric.language_policy,"languageRouting":routing},
            CallBudget(settings.model_calls_per_job))
        kept=[]
        for finding in feedback.findings:
            e=validate_evidence(finding.evidence,context,rubric,finding.dimension)
            if finding.dimension in ('grammar','vocabulary') and evidence_protected(e,routing):continue
            e=e.model_copy(update={"start":e.start+row.context_start,"end":e.end+row.context_start})
            if e.start<row.start or e.end>row.end: raise DomainError("CHUNK_EVIDENCE_OUTSIDE_CORE")
            finding.evidence=e;kept.append(finding)
        feedback.findings=kept
        def save():
            with sessions.begin() as db:
                from inkwise.model_receipts import lock_job_lease
                lock_job_lease(db,job)
                db.execute(update(ChunkTask).where(ChunkTask.id==identifier,ChunkTask.lease_id==row.lease_id)
                            .values(status="succeeded",result=feedback.model_dump(by_alias=True),lease_id=None))
        await asyncio.to_thread(save)
    except BaseException as exc:
        def fail():
            with sessions.begin() as db:
                db.execute(update(ChunkTask).where(ChunkTask.id==identifier,ChunkTask.lease_id==row.lease_id)
                    .values(status="failed",lease_id=None,error_code=exc.code if isinstance(exc,DomainError) else "CHUNK_FAILED"))
        await asyncio.shield(asyncio.to_thread(fail))
        if isinstance(exc,asyncio.CancelledError):raise
    finally:
        if gateway:await gateway.close()


async def run_chunk_worker(settings):
    from aiokafka import AIOKafkaConsumer
    if not settings.kafka_enabled or not settings.kafka_bootstrap_servers:
        raise DomainError("KAFKA_NOT_CONFIGURED")
    engine,sessions=create_session_factory(settings.database_url)
    consumer=AIOKafkaConsumer(settings.kafka_topic,bootstrap_servers=settings.kafka_bootstrap_servers,
        group_id="inkwise-chunk-workers",enable_auto_commit=False,auto_offset_reset="earliest",
        max_poll_interval_ms=600000,max_partition_fetch_bytes=16384)
    try:
        await consumer.start()
        while True:
            batches=await consumer.getmany(timeout_ms=1000,max_records=settings.model_concurrency)
            tasks=[]
            for records in batches.values():
                for record in records:
                    try:
                        identifier=str(UUID(json.loads(record.value)["chunkId"]))
                        tasks.append(process_chunk(identifier,settings,sessions))
                    except (ValueError,KeyError,TypeError):
                        # Poison messages contain no useful task identity; acknowledge without logging payload.
                        continue
            await asyncio.gather(*tasks)
            if batches: await consumer.commit()
    finally:
        await consumer.stop();engine.dispose()
