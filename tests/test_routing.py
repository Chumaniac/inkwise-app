import asyncio

import httpx
import pytest
from sqlalchemy import func,select

from inkwise.contracts import DimensionScore
from inkwise.database import CallReceipt,JobEvent
from inkwise.errors import DomainError
from inkwise.providers import CallBudget
from inkwise.routing import select_route,model_capacity
from inkwise.worker import ReceiptGateway,claim_job
from test_worker import setup,completion


@pytest.mark.asyncio
async def test_cloud_requires_operator_consent_prices_budget_and_frozen_plan(setup):
    settings,sessions,_,_=setup
    settings.model_base_url='https://provider.invalid/v1'
    settings.model_revision='pinned-v1'
    job=claim_job(sessions,settings)
    probes=[]
    async def probe(route,settings):
        probes.append(route['id']);return {'revision':route['revision'],'contextTokens':65536,'revisionSource':'configured'}
    with pytest.raises(DomainError,match='CLOUD_PROCESSING_NOT_AUTHORIZED'):await select_route(settings,sessions,job,probe)
    settings.cloud_enabled=True
    job.payload={**job.payload,'routingPreference':'hybrid','cloudConsent':True,'maxCostMicrousd':1000000}
    settings.max_cloud_cost_microusd=1000000
    with pytest.raises(DomainError,match='MODEL_PRICE_CONFIGURATION_REQUIRED'):await select_route(settings,sessions,job,probe)
    settings.model_input_price_microusd_per_million=1_000_000
    settings.model_output_price_microusd_per_million=2_000_000
    settings.max_cloud_cost_microusd=1
    with pytest.raises(DomainError,match='CLOUD_COST_BOUND_EXCEEDED'):await select_route(settings,sessions,job,probe)
    assert probes==[]
    settings.max_cloud_cost_microusd=1000000
    routed,plan=await select_route(settings,sessions,job,probe)
    assert routed.model_revision=='pinned-v1' and 0<plan['estimatedMaximumCostMicrousd']<=1000000
    await select_route(settings,sessions,job,probe)
    assert probes==['primary']
    settings.model_revision='different'
    with pytest.raises(DomainError,match='MODEL_ROUTE_CONFIGURATION_CHANGED'):await select_route(settings,sessions,job,probe)


@pytest.mark.asyncio
async def test_fallback_before_request_only_and_no_legacy_restart(setup):
    settings,sessions,_,_=setup
    settings.model_base_url='http://127.0.0.1:12345/v1'
    settings.cloud_model_base_url='https://provider.invalid/v1';settings.cloud_model_name='cloud'
    settings.cloud_model_revision='r1';settings.cloud_enabled=True;settings.max_cloud_cost_microusd=1_000_000
    settings.cloud_input_price_microusd_per_million=10;settings.cloud_output_price_microusd_per_million=20
    job=claim_job(sessions,settings)
    job.payload={**job.payload,'routingPreference':'hybrid','cloudConsent':True,'maxCostMicrousd':1000000}
    async def probe(route,settings):
        if route['location']=='local':raise DomainError('MODEL_ROUTE_UNAVAILABLE')
        return {'revision':'r1','contextTokens':65536,'revisionSource':'configured'}
    routed,plan=await select_route(settings,sessions,job,probe)
    assert plan['route']['id']=='cloud'
    # Chunk processes reload the operator settings, then bind the same immutable plan.
    assert (await select_route(settings,sessions,job,probe,require_existing=True))[0].model_name=='cloud'
    for path in (settings.data_dir/'routing'/job.id).glob('*.json'):path.unlink()
    with sessions.begin() as db:db.add(CallReceipt(id='unknown',tenant_id=job.tenant_id,job_id=job.id,
        input_hash='x',stage='x',status='unknown'))
    with pytest.raises(DomainError,match='LEGACY_ROUTE_RECONCILIATION_REQUIRED'):await select_route(settings,sessions,job,probe)


@pytest.mark.asyncio
async def test_context_rejected_before_receipt_and_capacity_is_global(setup):
    settings,sessions,_,_=setup;settings.model_context_tokens=4096;settings.model_global_concurrency=1
    job=claim_job(sessions,settings)
    async with httpx.AsyncClient(transport=httpx.MockTransport(completion)) as client:
        gateway=ReceiptGateway(settings,sessions,job,client)
        with pytest.raises(DomainError,match='MODEL_CONTEXT_BUDGET_EXCEEDED'):
            await gateway.complete(DimensionScore,'task',{'essay':'synthetic '*5000},CallBudget(1))
        with sessions() as db:assert db.scalar(select(func.count()).select_from(CallReceipt))==0
    entered=asyncio.Event()
    async def second():
        async with model_capacity(settings):entered.set()
    async with model_capacity(settings):
        waiter=asyncio.create_task(second());await asyncio.sleep(.1);assert not entered.is_set()
    await asyncio.wait_for(waiter,1)
    assert entered.is_set()


@pytest.mark.asyncio
async def test_usage_redacted_success_reused_and_chunk_cache_separated(setup):
    settings,sessions,_,_=setup;job=claim_job(sessions,settings);calls=[]
    def provider(request):
        calls.append(1);response=completion(request);payload=response.json()
        payload['usage']={'prompt_tokens':10,'completion_tokens':20,'private':'do-not-store'}
        return httpx.Response(200,json=payload)
    data={'dimension':'content','essay':'A','rules':[{'id':'rule'}]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        gateway=ReceiptGateway(settings,sessions,job,client)
        await gateway.complete(DimensionScore,'task',data,CallBudget(3))
        await gateway.complete(DimensionScore,'task',data,CallBudget(3))
        gateway.cache_scope='chunk-2'
        await gateway.complete(DimensionScore,'task',data,CallBudget(3))
    assert len(calls)==2
    with sessions() as db:
        usage=db.scalars(select(JobEvent).where(JobEvent.event=='model.usage')).all()
        assert len(usage)==2 and all(row.detail=={'prompt_tokens':10,'completion_tokens':20} for row in usage)
