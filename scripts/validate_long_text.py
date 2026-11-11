"""Durable 15k-character local-only benchmark. Never retries an existing failed/unknown run."""
import argparse
import asyncio
import json
import resource
import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select,func

from inkwise.api import create_app
from inkwise.cli import seed_demo,provision
from inkwise.database import initialize_database,create_session_factory,Job,CallReceipt,JobEvent
from inkwise.knowledge import atomic_json
from inkwise.settings import Settings
from inkwise.worker import claim_job,process_job,ReceiptGateway
from inkwise.retrieval import HybridRetriever


class EmptyRoute:
    async def query(self,*args):return []


def synthetic_completion(request):
    data=json.loads(json.loads(request.content)['messages'][1]['content'])
    if 'initialScores' in data:answer={'challenges':[],'summary':'Synthetic no-conflict result'}
    else:
        quote=data['essay'][:80]
        answer={'dimension':data['dimension'],'band':3,'reason':'Synthetic performance fixture',
            'evidence':[{'start':0,'end':len(quote),'quote':quote,'ruleId':data['rules'][0]['id']}],'suggestion':'Add detail.'}
    return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(answer)}}]})


def cumulative_summary(root,result):
    previous=[json.loads(path.read_text()) for path in sorted(root.glob('*failure*.json'))]
    result['timings']['cumulativeProcessingMs']=sum(item['timings'].get('processingMs',0) for item in previous)+result['timings']['processingMs']
    result['peakCoordinatorRssAcrossAttemptsBytes']=max([result['processPeakRssBytes']]+[item['processPeakRssBytes'] for item in previous])
    result['recoveredAttempts']=len(previous)
    for key in ('createMs','confirmAndEnqueueMs'):
        if key not in result['timings']:
            value=next((item['timings'][key] for item in previous if key in item['timings']),None)
            if value is not None:result['timings'][key]=value
    result['fullProcessingUnder1500Ms']=result['status']=='teacher_review' and result['timings']['cumulativeProcessingMs']<1500
    result['performanceScope']='Includes recorded failed attempts and successful recovery; excludes time spent implementing repairs. One sample, not p95.'
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--real-local',action='store_true');parser.add_argument('--retry-preflight',action='store_true');parser.add_argument('--resume-known-failure',action='store_true');args=parser.parse_args()
    mode='real-local' if args.real_local else 'synthetic'
    root=Path('artifacts/long-text-check')/mode;root.mkdir(parents=True,exist_ok=True)
    receipt=root/'summary.json'
    if receipt.exists() and not (args.retry_preflight or args.resume_known_failure):
        saved=cumulative_summary(root,json.loads(receipt.read_text()));atomic_json(receipt,saved);print(json.dumps(saved,indent=2));return
    settings=Settings(database_url=f'sqlite:///{root}/db.sqlite',data_dir=root,rubric_dir=root/'rules',
        model_base_url='http://127.0.0.1:11434/v1',model_name='inkwise-deepseek-local',model_structured_output='json_schema',
        model_concurrency=1,model_global_concurrency=1,model_timeout_seconds=120,job_timeout_seconds=600,model_max_output_tokens=4096)
    initialize_database(settings.database_url)
    if not (settings.rubric_dir/'synthetic-perf'/'active.json').exists():seed_demo(settings,'synthetic-perf')
    engine,sessions=create_session_factory(settings.database_url)
    paragraph='We planned a small garden beside our school. First, we asked our teacher to help us choose a safe place. Then we shared the tools and planted flowers. Each day, a different group watered the soil. At the end of the month we wrote down what we had learned. Working together made a difficult task easier.\n'
    essay=(paragraph*60)[:15000]
    assert len(essay)==15000
    with sessions() as db:existing=db.scalar(select(Job).limit(1))
    timings={};started=time.perf_counter();cpu_started=time.process_time()
    if existing and args.resume_known_failure:
        with sessions.begin() as db:
            prior=db.scalar(select(func.coalesce(func.sum(CallReceipt.attempt),0)))
            unknown=db.scalar(select(CallReceipt.id).where(CallReceipt.status.in_(['started','unknown'])).limit(1))
            if existing.status!='failed' or existing.error_code not in ('EVIDENCE_RULE_INVALID','EVIDENCE_QUOTE_INVALID') or unknown or prior>=settings.model_calls_per_job:
                raise SystemExit('Known failure with remaining budget required; unknown requests are never retried.')
            row=db.get(Job,existing.id);row.status='queued';row.version+=1
        receipt.rename(root/f'known-failure-{prior}-calls.json')
        existing.status='queued'
    if existing and args.retry_preflight:
        with sessions.begin() as db:
            prior=db.scalar(select(func.coalesce(func.sum(CallReceipt.attempt),0)))
            if existing.status!='failed' or existing.error_code!='MODEL_CONTEXT_BUDGET_EXCEEDED' or prior:
                raise SystemExit('Only a confirmed zero-call preflight failure can be retried here.')
            row=db.get(Job,existing.id);row.status='queued';row.version+=1
        receipt.rename(root/'preflight-failure.json')
        existing.status='queued'
    if existing:
        if existing.status!='queued':raise SystemExit('Existing benchmark requires operator inspection; no inference repeated.')
    else:
        access=root/'access.txt'
        provision(settings,'synthetic-perf',['bench'],'teacher',access)
        token=access.read_text().strip()
        with TestClient(create_app(settings)) as client:
            client.headers['Authorization']='Bearer '+token
            mark=time.perf_counter()
            response=client.post('/api/v1/jobs',headers={'Idempotency-Key':'one-15k-v04'},json={'classroomId':'bench','title':'Synthetic 15k benchmark',
                'prompt':'Review the school garden narrative. Repeated paragraphs are deliberate synthetic load, not a real student essay.',
                'text':essay,'grade':7,'languagePolicy':'en','genre':'short-text','rubricId':'english-demo'})
            response.raise_for_status();job=response.json();timings['createMs']=(time.perf_counter()-mark)*1000
            mark=time.perf_counter();response=client.post(f"/api/v1/jobs/{job['id']}/text-confirmations",json={'version':job['version'],'text':essay})
            response.raise_for_status();timings['confirmAndEnqueueMs']=(time.perf_counter()-mark)*1000
    claimed=claim_job(sessions,settings);mark=time.perf_counter()
    if args.real_local:await process_job(claimed,settings,sessions,retriever=HybridRetriever(settings,EmptyRoute(),EmptyRoute()))
    else:
        async with httpx.AsyncClient(transport=httpx.MockTransport(synthetic_completion)) as transport:
            gateway=ReceiptGateway(settings,sessions,claimed,transport)
            await process_job(claimed,settings,sessions,gateway,HybridRetriever(settings,EmptyRoute(),EmptyRoute()))
    timings['processingMs']=(time.perf_counter()-mark)*1000
    with sessions() as db:
        job=db.get(Job,claimed.id)
        usage=[row.detail for row in db.scalars(select(JobEvent).where(JobEvent.event=='model.usage')).all()]
        result={'mode':mode,'characters':len(essay),'status':job.status,'errorCode':job.error_code,'timings':timings,
            'coordinatorCpuSecondsThisRun':time.process_time()-cpu_started,
            'processPeakRssBytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            'peakRssScope':'Python coordinator only, macOS bytes; Ollama server memory excluded',
            'modelCalls':db.scalar(select(func.coalesce(func.sum(CallReceipt.attempt),0))),
            'modelRoute':(job.result or {}).get('modelRoute'),'usage':usage,'machine':'Apple M4 Pro 48 GiB',
            'fullProcessingUnder1500Ms':job.status=='teacher_review' and timings['processingMs']<1500,'elapsedSeconds':time.perf_counter()-started,
            'scope':'one 15000-character synthetic English essay; no classroom quality claim; direct whole-text scoring, Kafka tested separately'}
    result=cumulative_summary(root,result)
    atomic_json(receipt,result);engine.dispose();print(json.dumps(result,indent=2))


if __name__=='__main__':asyncio.run(main())
