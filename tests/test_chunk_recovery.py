import asyncio
from datetime import timedelta
import json
from uuid import uuid4
import httpx
import pytest
from sqlalchemy import select
from inkwise.chunks import await_chunks, prepare_chunks, process_chunk
from inkwise.cli import seed_demo
from inkwise.database import Base, ChunkTask, Job, JobEvent, create_session_factory, now
from inkwise.errors import DomainError
from inkwise.rubrics import RubricRepository, digest
from inkwise.settings import Settings

@pytest.fixture
def chunk_job(tmp_path):
    settings=Settings(_env_file=None,database_url=f'sqlite:///{tmp_path}/db.sqlite',rubric_dir=tmp_path/'rules',
                      data_dir=tmp_path,model_calls_per_job=40,model_name='synthetic-model',model_base_url='http://provider.invalid/v1')
    engine,sessions=create_session_factory(settings.database_url);Base.metadata.create_all(engine);seed_demo(settings,'synthetic')
    text=('A synthetic garden sentence.\n'*120)[:2800]
    job=Job(id=str(uuid4()),tenant_id='synthetic',classroom_id='class',owner_id='teacher',idempotency_key='synthetic',request_hash='hash',
            payload={'grade':7},text=text,text_hash=digest(text),rubric=RubricRepository(settings.rubric_dir).get('synthetic','english-demo').model_dump(by_alias=True),
            status='running',lease_id=str(uuid4()),lease_until=now()+timedelta(seconds=180),version=1)
    with sessions.begin() as db:db.add(job)
    try:yield settings,sessions,job
    finally:engine.dispose()

@pytest.mark.asyncio
async def test_chunk_deadline_records_bounded_counts_without_discarding_success(chunk_job,monkeypatch):
    import inkwise.chunks
    settings,sessions,job=chunk_job
    manifest,count=prepare_chunks(sessions,job,settings)
    with sessions.begin() as db:
        row=db.scalars(select(ChunkTask).order_by(ChunkTask.ordinal)).first()
        row.status='succeeded';row.result={'findings':[]};saved_id=row.id
    async def no_broker(*args):pass
    monkeypatch.setattr(inkwise.chunks,'dispatch_outbox',no_broker)
    with pytest.raises(DomainError,match='CHUNK_WAIT_TIMEOUT_UNKNOWN'):
        await await_chunks(settings,sessions,job,deadline=asyncio.get_running_loop().time()+.08)
    with sessions() as db:
        assert db.get(ChunkTask,saved_id).status=='succeeded'
        events=db.scalars(select(JobEvent).where(JobEvent.event=='chunks.progress')).all()
        assert events[-1].detail['waitStatus']=='deadline'
        assert events[-1].detail['completed']==1 and events[-1].detail['total']==count
        assert all(set(e.detail)=={'total','completed','failed','running','queued','waitStatus'} for e in events)
    assert prepare_chunks(sessions,job,settings)==(manifest,count)
    with sessions() as db:assert db.get(ChunkTask,saved_id).result=={'findings':[]}

@pytest.mark.asyncio
async def test_resumed_unknown_chunk_receipt_never_repeats_the_model_call(chunk_job):
    from inkwise.model_receipts import ReceiptGateway
    settings,sessions,job=chunk_job
    prepare_chunks(sessions,job,settings)
    with sessions() as db:identifier=db.scalars(select(ChunkTask).order_by(ChunkTask.ordinal)).first().id
    calls=[]
    def lost_response(request):calls.append(1);raise httpx.ReadTimeout('Synthetic unconfirmed response')
    async with httpx.AsyncClient(transport=httpx.MockTransport(lost_response)) as transport:
        def factory(current):
            gateway=ReceiptGateway(settings,sessions,current,transport);gateway.cache_scope=identifier;return gateway
        await process_chunk(identifier,settings,sessions,factory)
        with pytest.raises(DomainError,match='PROVIDER_RESULT_UNKNOWN_REQUIRES_REVIEW'):
            prepare_chunks(sessions,job,settings)
    assert len(calls)==1
    from inkwise.database import CallReceipt
    with sessions() as db:
        receipt=db.scalars(select(CallReceipt).where(CallReceipt.job_id==job.id)).one()
        assert receipt.status=='unknown' and receipt.attempt==1
        assert db.get(ChunkTask,identifier).status=='failed'

@pytest.mark.asyncio
async def test_wait_cancellation_preserves_chunks_and_records_interruption(chunk_job,monkeypatch):
    import inkwise.chunks
    settings,sessions,job=chunk_job
    async def no_broker(*args):pass
    monkeypatch.setattr(inkwise.chunks,'dispatch_outbox',no_broker)
    task=asyncio.create_task(await_chunks(settings,sessions,job))
    for _ in range(100):
        with sessions() as db:observed=db.scalar(select(JobEvent.id).where(JobEvent.event=='chunks.progress'))
        if observed:break
        await asyncio.sleep(.01)
    assert observed is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    with sessions() as db:
        events=db.scalars(select(JobEvent).where(JobEvent.event=='chunks.progress')).all()
        assert events[-1].detail['waitStatus']=='interrupted'
        assert len(db.scalars(select(ChunkTask)).all())==events[-1].detail['total']

@pytest.mark.parametrize('receipt_status',['started','unknown'])
def test_resume_refuses_unconfirmed_legacy_receipts_even_with_a_different_input_key(chunk_job,receipt_status):
    from inkwise.database import CallReceipt
    settings,sessions,job=chunk_job
    prepare_chunks(sessions,job,settings)
    with sessions.begin() as db:
        row=db.scalars(select(ChunkTask).order_by(ChunkTask.ordinal)).first();row.status='succeeded';row.result={'findings':[]};saved_id=row.id
        db.add(CallReceipt(id=str(uuid4()),tenant_id=job.tenant_id,job_id=job.id,input_hash='old-diagnostic-dependent-key',stage='ChunkFeedback:all',status=receipt_status))
    with pytest.raises(DomainError,match='PROVIDER_RESULT_UNKNOWN_REQUIRES_REVIEW'):prepare_chunks(sessions,job,settings)
    with sessions() as db:assert db.get(ChunkTask,saved_id).status=='succeeded'
