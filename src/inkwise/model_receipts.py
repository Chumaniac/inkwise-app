"""Durable model-call receipts fenced by the current job lease."""

import asyncio
from uuid import uuid4
from sqlalchemy import func, select, update
from inkwise.database import CallReceipt, Job, JobEvent, now
from inkwise.errors import DomainError
from inkwise.providers import ModelGateway
from inkwise.provider_io import service_url
from inkwise.rubrics import canonical, digest


def lock_job_lease(db, job, status="running"):
    """Fence all new work and result writes with the current, unexpired lease."""
    result = db.execute(update(Job).where(
        Job.id == job.id, Job.tenant_id == job.tenant_id, Job.lease_id == job.lease_id,
        Job.version == job.version, Job.status == status, Job.lease_until > now(),
    ).values(lease_until=Job.lease_until))
    if result.rowcount != 1:
        raise DomainError("JOB_CANCELLED")
    from inkwise.reassessments import assert_assessment_binding
    current = db.get(Job, job.id)
    assert_assessment_binding(db, current)



class ReceiptGateway(ModelGateway):
    def __init__(self, settings, sessions, job, client=None):
        super().__init__(settings, client)
        self.sessions, self.job = sessions, job
        self.route_plan = None
        self.cache_scope = "main"

    def reserve(self, key: str, stage: str) -> tuple[str, dict | None, int]:
        with self.sessions.begin() as db:
            lock_job_lease(db, self.job)
            current = db.get(Job, self.job.id)
            if not current or current.status != "running" or current.lease_id != self.job.lease_id:
                raise DomainError("JOB_CANCELLED")
            found = db.scalar(select(CallReceipt).where(CallReceipt.job_id == current.id,
                                  CallReceipt.input_hash == key, CallReceipt.stage == stage))
            if found:
                if found.status == "succeeded":
                    return found.id, found.result, found.attempt
                if found.status=="failed" and found.attempt>=2:
                    raise DomainError("MODEL_REPAIR_LIMIT_REACHED")
                if found.status not in ("retry_authorized","failed"):
                    raise DomainError("PROVIDER_RESULT_UNKNOWN_REQUIRES_REVIEW")
            count = db.scalar(select(func.coalesce(func.sum(CallReceipt.attempt),0)).where(CallReceipt.job_id == current.id))
            if count >= self.settings.model_calls_per_job:
                raise DomainError("BUDGET_EXCEEDED")
            if found:
                found.status = "started"
                found.attempt += 1
                return found.id, None, found.attempt
            identifier = str(uuid4())
            db.add(CallReceipt(id=identifier, tenant_id=current.tenant_id, job_id=current.id,
                               input_hash=key, stage=stage, status="started"))
            return identifier, None, 1

    async def before_request(self):
        def verify():
            with self.sessions.begin() as db:
                lock_job_lease(db,self.job)
        await asyncio.to_thread(verify)
        if self.route_plan:
            from inkwise.routing import verify_pin
            await verify_pin(self.settings,self.route_plan)

    async def record_usage(self,response):
        usage=response.get('usage',{})
        if not isinstance(usage,dict):return
        detail={key:usage[key] for key in ('prompt_tokens','completion_tokens','total_tokens')
            if type(usage.get(key)) is int and 0<=usage[key]<=10_000_000}
        if not detail:return
        def save():
            with self.sessions.begin() as db:
                db.add(JobEvent(job_id=self.job.id,tenant_id=self.job.tenant_id,actor_id='worker',event='model.usage',
                    job_version=self.job.version,detail=detail))
        await asyncio.to_thread(save)

    def finish(self, identifier, attempt, status, result=None):
        with self.sessions.begin() as db:
            db.execute(update(CallReceipt).where(CallReceipt.id == identifier,CallReceipt.attempt==attempt,
                       CallReceipt.status=="started").values(status=status, result=result))

    async def complete(self, schema, task, data, budget):
        if not self.settings.model_name:
            raise DomainError("MODEL_NOT_CONFIGURED", 503)
        service_url(self.settings.model_base_url, "chat/completions")
        key = digest(canonical({"schema": schema.model_json_schema(), "task": task, "data": data,
                                "model": self.settings.model_name, "baseUrl": self.settings.model_base_url,
                                "revision":self.settings.model_revision,"cacheScope":self.cache_scope}))
        stage = schema.__name__ + ":" + str(data.get("dimension", "all"))
        self.request_payload(schema,task,data)
        # The job is handled by one leased worker; serialize reservation before await.
        identifier, cached, attempt = await asyncio.to_thread(self.reserve, key, stage)
        if cached is not None:
            return schema.model_validate(cached)
        try:
            result = await super().complete(schema, task, data, budget)
        except BaseException as exc:
            known_failed=isinstance(exc,DomainError) and exc.code in {
                "MODEL_INVALID_SCHEMA","MODEL_OUTPUT_INCOMPLETE","PROVIDER_RATE_LIMITED","BUDGET_EXCEEDED","JOB_CANCELLED",
                "MODEL_CONTEXT_BUDGET_EXCEEDED","MODEL_REVISION_CHANGED","MODEL_CAPACITY_BUSY"}
            diagnostics={"errorCode":exc.code,"details":exc.details} if isinstance(exc,DomainError) else None
            await asyncio.shield(asyncio.to_thread(self.finish, identifier, attempt, "failed" if known_failed else "unknown",diagnostics))
            raise
        await asyncio.to_thread(self.finish, identifier, attempt, "succeeded", result.model_dump(mode="json", by_alias=True))
        return result
