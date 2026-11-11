import pytest

from inkwise.chunks import dispatch_outbox
from inkwise.settings import Settings


@pytest.mark.asyncio
async def test_broker_start_failure_closes_the_producer(monkeypatch):
    import aiokafka
    class Producer:
        stopped=False
        def __init__(self,**kwargs):pass
        async def start(self):raise RuntimeError('synthetic unavailable broker')
        async def stop(self):Producer.stopped=True
    monkeypatch.setattr(aiokafka,'AIOKafkaProducer',Producer)
    with pytest.raises(RuntimeError):await dispatch_outbox(Settings(),None)
    assert Producer.stopped
