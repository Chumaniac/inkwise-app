"""Pre-request local/cloud selection, immutable route pins and explicit cost ceilings."""
import asyncio
import hashlib
import json
import math
import re
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

import httpx
from sqlalchemy import func,select

from inkwise.cache_storage import ensure_cache_capacity, read_cache_json
from inkwise.database import CallReceipt
from inkwise.errors import DomainError
from inkwise.cache_storage import atomic_json
from inkwise.provider_io import service_url, local_endpoint
from inkwise.rubrics import canonical,digest,safe_segment


@lru_cache(maxsize=2)
def _tokenizer(path,checksum):
    from tokenizers import Tokenizer
    source=Path(path)
    if source.stat().st_size>25_000_000 or hashlib.sha256(source.read_bytes()).hexdigest()!=checksum:
        raise DomainError('MODEL_TOKENIZER_INVALID')
    return Tokenizer.from_file(str(source))


def token_count(text,settings):
    if settings.model_tokenizer_path is not None:
        if not settings.model_tokenizer_sha256:raise DomainError('MODEL_TOKENIZER_PIN_REQUIRED')
        try:return len(_tokenizer(str(settings.model_tokenizer_path.resolve()),settings.model_tokenizer_sha256).encode(text).ids)+256
        except (ImportError,OSError,ValueError):raise DomainError('MODEL_TOKENIZER_INVALID') from None
    # A UTF-8 byte upper bound is conservative when a matching tokenizer is absent.
    return len(text.encode('utf-8'))+256


def candidates(settings):
    values=[]
    if settings.model_base_url and settings.model_name:
        location=settings.model_location
        if location=='auto':location='local' if local_endpoint(settings.model_base_url) else 'cloud'
        if location not in ('local','cloud'):raise DomainError('MODEL_ROUTE_INVALID')
        values.append({'id':'primary','location':location,'baseUrl':settings.model_base_url,'model':settings.model_name,
            'revision':settings.model_revision,'contextTokens':settings.model_context_tokens,'keySlot':'primary',
            'structuredOutput':settings.model_structured_output,
            'tokenizerPath':str(settings.model_tokenizer_path) if settings.model_tokenizer_path else None,
            'tokenizerHash':settings.model_tokenizer_sha256,
            'inputPrice':0 if location=='local' else settings.model_input_price_microusd_per_million,
            'outputPrice':0 if location=='local' else settings.model_output_price_microusd_per_million})
    if settings.cloud_model_base_url and settings.cloud_model_name:
        values.append({'id':'cloud','location':'cloud','baseUrl':settings.cloud_model_base_url,'model':settings.cloud_model_name,
            'revision':settings.cloud_model_revision,'contextTokens':settings.cloud_context_tokens,'keySlot':'cloud',
            'structuredOutput':settings.cloud_structured_output,'tokenizerPath':None,'tokenizerHash':'',
            'inputPrice':settings.cloud_input_price_microusd_per_million,'outputPrice':settings.cloud_output_price_microusd_per_million})
    for route in values:
        service_url(route['baseUrl'],'models')
        if route['location']=='local' and not local_endpoint(route['baseUrl']):raise DomainError('LOCAL_MODEL_ENDPOINT_REQUIRED')
        if route['location']=='cloud' and urlparse(route['baseUrl']).scheme!='https' and not local_endpoint(route['baseUrl']):
            raise DomainError('CLOUD_MODEL_HTTPS_REQUIRED')
    return values


def budget_bound(route,maximum_calls,output_tokens):
    if route['inputPrice'] is None or route['outputPrice'] is None:raise DomainError('MODEL_PRICE_CONFIGURATION_REQUIRED')
    return math.ceil(maximum_calls*((route['contextTokens']-output_tokens)*route['inputPrice']+output_tokens*route['outputPrice'])/1_000_000)


def eligible(route,settings,job):
    if route['location']=='local':return 0
    if not settings.cloud_enabled or job.payload.get('routingPreference','local')!='hybrid' or not job.payload.get('cloudConsent'):
        raise DomainError('CLOUD_PROCESSING_NOT_AUTHORIZED')
    if not route['revision']:raise DomainError('CLOUD_MODEL_REVISION_REQUIRED')
    ceiling=budget_bound(route,settings.model_calls_per_job,settings.model_max_output_tokens)
    allowed=min(job.payload.get('maxCostMicrousd',0),settings.max_cloud_cost_microusd)
    if ceiling>allowed:raise DomainError('CLOUD_COST_BOUND_EXCEEDED')
    return ceiling


async def get_json(client,url,headers=None):
    async with client.stream('GET',url,headers=headers or {}) as response:
        if response.status_code!=200:raise DomainError('MODEL_ROUTE_UNAVAILABLE')
        buffer=bytearray()
        async for chunk in response.aiter_bytes():
            buffer.extend(chunk)
            if len(buffer)>262144:raise DomainError('MODEL_CATALOG_TOO_LARGE')
        try:return json.loads(buffer)
        except ValueError:raise DomainError('MODEL_ROUTE_UNAVAILABLE') from None


async def probe_route(route,settings):
    key=settings.model_api_key if route['keySlot']=='primary' else settings.cloud_model_api_key
    headers={'Authorization':'Bearer '+key.get_secret_value()} if key.get_secret_value() else {}
    async with httpx.AsyncClient(timeout=settings.route_health_timeout_seconds,follow_redirects=False,trust_env=False) as client:
        parsed=urlparse(route['baseUrl'])
        if route['location']=='local' and parsed.port==11434 and parsed.path.rstrip('/')=='/v1':
            base=route['baseUrl'].rstrip('/')[:-3]
            catalog=await get_json(client,base+'/api/tags',headers)
            found=next((row for row in catalog.get('models',[]) if row.get('name') in (route['model'],route['model']+':latest')),None)
            if not found:raise DomainError('MODEL_ROUTE_UNAVAILABLE')
            if route['revision'] and route['revision']!=found['digest']:raise DomainError('MODEL_REVISION_CHANGED')
            # The project Modelfile pins context explicitly. Do not assume a server default.
            response=await client.post(base+'/api/show',json={'model':route['model']},headers=headers)
            if response.status_code!=200 or len(response.content)>262144:raise DomainError('LOCAL_CONTEXT_NOT_PINNED')
            match=re.search(r'(?m)^num_ctx\s+(\d+)',response.json().get('parameters',''))
            if not match:raise DomainError('LOCAL_CONTEXT_NOT_PINNED')
            return {'revision':found['digest'],'contextTokens':min(route['contextTokens'],int(match.group(1))),
                    'revisionSource':'ollama-tags'}
        catalog=await get_json(client,service_url(route['baseUrl'],'models'),headers)
        identifiers={row.get('id') for row in catalog.get('data',[])}
        if route['model'] not in identifiers and route['model']+':latest' not in identifiers:raise DomainError('MODEL_ROUTE_UNAVAILABLE')
        return {'revision':route['revision'] or 'operator-unpinned','contextTokens':route['contextTokens'],'revisionSource':'configured'}


def configuration_hash(route,settings):
    return digest(canonical({'route':route,'maxCalls':settings.model_calls_per_job,'outputTokens':settings.model_max_output_tokens,
        'pipelineRevision':settings.pipeline_revision}))


def bind_settings(settings,route,plan):
    return settings.model_copy(update={'model_base_url':route['baseUrl'],'model_name':route['model'],'model_revision':plan['route']['revision'],
        'model_context_tokens':plan['route']['contextTokens'],'model_calls_per_job':plan['maxCalls'],'model_max_output_tokens':plan['outputTokens'],
        'model_structured_output':route['structuredOutput'],
        'model_tokenizer_path':Path(route['tokenizerPath']) if route['tokenizerPath'] else None,'model_tokenizer_sha256':route['tokenizerHash'],
        'model_api_key':settings.model_api_key if route['keySlot']=='primary' else settings.cloud_model_api_key})


async def select_route(settings,sessions,job,probe=None,require_existing=False):
    options=candidates(settings)
    if not options:raise DomainError('MODEL_NOT_CONFIGURED',503)
    scope={'tenant':job.tenant_id,'jobId':job.id,'textHash':job.text_hash,'rubricHash':digest(canonical(job.rubric))}
    path=settings.data_dir/'routing'/safe_segment(job.id)/(digest(canonical(scope))+'.json')
    if path.exists():
        plan=read_cache_json(path)
        route=next((route for route in options if route['id']==plan['route']['id']),None)
        if not route or plan['scope']!=scope or configuration_hash(route,settings)!=plan['configurationHash']:
            raise DomainError('MODEL_ROUTE_CONFIGURATION_CHANGED')
        eligible(route,settings,job)
        return bind_settings(settings,route,plan),plan
    if require_existing:raise DomainError('MODEL_ROUTE_PLAN_REQUIRED')
    ensure_cache_capacity(path.parent, settings, 1024 * 1024)
    with sessions() as db:
        prior=db.scalar(select(func.coalesce(func.sum(CallReceipt.attempt),0)).where(CallReceipt.job_id==job.id))
    if prior:raise DomainError('LEGACY_ROUTE_RECONCILIATION_REQUIRED')
    if job.payload.get('routingPreference')=='hybrid' and len(job.text)>=settings.cloud_long_text_threshold:
        options.sort(key=lambda route:route['location']!='cloud')
    else:options.sort(key=lambda route:route['location']!='local')
    errors=[]
    for route in options:
        try:
            ceiling=eligible(route,settings,job)
            pinned=await (probe or probe_route)(route,settings)
            preliminary={**route,**pinned}
            temporary={'route':preliminary,'maxCalls':settings.model_calls_per_job,'outputTokens':settings.model_max_output_tokens}
            chosen=bind_settings(settings,route,temporary)
            minimum=token_count(job.text+job.payload.get('prompt','')+canonical(job.rubric),chosen)
            if minimum+settings.model_max_output_tokens+4096>pinned['contextTokens']:raise DomainError('MODEL_CONTEXT_BUDGET_EXCEEDED')
            plan={'scope':scope,'route':preliminary,'maxCalls':settings.model_calls_per_job,'outputTokens':settings.model_max_output_tokens,
                'configurationHash':configuration_hash(route,settings),'estimatedMaximumCostMicrousd':ceiling,
                'costBasis':'configured rates and maximum calls/context; not a provider invoice','newCloudConsentRequired':False}
            atomic_json(path,plan)
            return chosen,plan
        except asyncio.CancelledError:raise
        except DomainError as error:errors.append(error.code)
        except (httpx.HTTPError,ValueError,KeyError):errors.append('MODEL_ROUTE_UNAVAILABLE')
    if not errors:raise DomainError('MODEL_ROUTE_UNAVAILABLE')
    # No text has been sent to a scoring model during selection.
    raise DomainError(errors[-1] if len(set(errors))==1 else 'NO_AUTHORIZED_MODEL_ROUTE',503)


async def verify_pin(settings,plan):
    if plan.get('route',{}).get('revisionSource')!='ollama-tags':return
    route=plan['route'];base=route['baseUrl'].rstrip('/')[:-3]
    async with httpx.AsyncClient(timeout=settings.route_health_timeout_seconds,follow_redirects=False,trust_env=False) as client:
        catalog=await get_json(client,base+'/api/tags')
    found=next((row for row in catalog.get('models',[]) if row.get('name') in (route['model'],route['model']+':latest')),None)
    if not found or found['digest']!=route['revision']:raise DomainError('MODEL_REVISION_CHANGED')


@asynccontextmanager
async def model_capacity(settings):
    """Kernel-released file leases bound concurrency across local workers sharing data_dir."""
    import fcntl
    folder=settings.data_dir/'model-slots'/digest(settings.model_base_url)[:20]
    folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    deadline=asyncio.get_running_loop().time()+settings.job_timeout_seconds
    acquired=None
    try:
        while acquired is None:
            for index in range(settings.model_global_concurrency):
                file=(folder/f'{index}.lock').open('a')
                try:fcntl.flock(file,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:file.close();continue
                acquired=file;break
            if acquired is None:
                if asyncio.get_running_loop().time()>=deadline:raise DomainError('MODEL_CAPACITY_BUSY')
                await asyncio.sleep(.05)
        yield
    finally:
        if acquired is not None:acquired.close()
