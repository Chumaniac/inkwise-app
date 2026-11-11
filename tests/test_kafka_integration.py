"""Real Kafka transport and durable chunk deduplication, with a synthetic model."""

import asyncio
from datetime import timedelta
import json
import os
from uuid import uuid4

import pytest

pytestmark=pytest.mark.skipif(os.getenv("INKWISE_RUN_KAFKA")!="1",reason="Disposable Kafka service not requested")


@pytest.mark.asyncio
async def test_real_broker_outbox_and_duplicate_consume(tmp_path):
    from aiokafka import AIOKafkaConsumer
    from sqlalchemy import select
    from inkwise.chunks import ChunkFeedback, dispatch_outbox, prepare_chunks, process_chunk
    from inkwise.cli import seed_demo
    from inkwise.database import Base, ChunkTask, Job, Outbox, create_session_factory, now
    from inkwise.rubrics import RubricRepository,digest
    from inkwise.settings import Settings
    settings=Settings(database_url=f"sqlite:///{tmp_path}/db.sqlite",rubric_dir=tmp_path/"rules",
        kafka_enabled=True,kafka_bootstrap_servers="127.0.0.1:19192",kafka_topic="inkwise-test-"+uuid4().hex,
        model_calls_per_job=40,model_name="synthetic-test",model_base_url="http://synthetic.invalid")
    engine,sessions=create_session_factory(settings.database_url);Base.metadata.create_all(engine);seed_demo(settings,"a")
    rubric=RubricRepository(settings.rubric_dir).get("a","english-demo").model_dump(by_alias=True)
    text=("A synthetic sentence.\n"*150)[:3000]
    job=Job(id=str(uuid4()),tenant_id="a",classroom_id="c",owner_id="teacher",idempotency_key="test",request_hash="test",
        payload={"grade":7},text=text,text_hash=digest(text),rubric=rubric,status="running",lease_id=str(uuid4()),lease_until=now()+timedelta(seconds=120),version=1)
    with sessions.begin() as db:db.add(job)
    manifest,count=prepare_chunks(sessions,job,settings)
    await dispatch_outbox(settings,sessions)
    with sessions() as db:
        assert all(row.delivered for row in db.scalars(select(Outbox)).all())
        rows=db.scalars(select(ChunkTask).order_by(ChunkTask.ordinal)).all()
        assert "".join(text[r.start:r.end] for r in rows)==text
    consumer=AIOKafkaConsumer(settings.kafka_topic,bootstrap_servers=settings.kafka_bootstrap_servers,
        group_id="synthetic-"+uuid4().hex,enable_auto_commit=False,auto_offset_reset="earliest")
    await consumer.start()
    class Model:
        calls=0
        async def complete(self,*args):
            Model.calls+=1
            return ChunkFeedback(findings=[])
        async def close(self):pass
    try:
        messages=[]
        async with asyncio.timeout(30):
            while len(messages)<count:
                record=await consumer.getone()
                messages.append(json.loads(record.value)["chunkId"])
        for identifier in messages:
            await process_chunk(identifier,settings,sessions,lambda job:Model())
            await process_chunk(identifier,settings,sessions,lambda job:Model())
        await consumer.commit()
        assert Model.calls==count
        with sessions() as db:
            assert all(r.status=="succeeded" for r in db.scalars(select(ChunkTask)).all())
    finally:
        await consumer.stop();engine.dispose()
