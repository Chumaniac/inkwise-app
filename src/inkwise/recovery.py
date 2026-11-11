"""Explicit operator reconciliation for unknown external requests."""

from sqlalchemy import select

from inkwise.contracts import DimensionScore
from inkwise.database import CallReceipt, Job, JobEvent, now
from inkwise.errors import DomainError
from inkwise.providers import Reflection


def resolve_receipt(sessions, receipt_id: str, *, action: str, actor: str, reason: str,
                    provider_checked: bool, recovered_result: dict | None = None):
    """Only a trusted operator may call this after checking the provider.

    The ordinary retry API cannot invoke this function. Retry authorization does
    not reset attempts or budgets. A recovered successful result is preferred.
    """
    if not provider_checked or not actor.strip() or not reason.strip():
        raise DomainError("PROVIDER_RECONCILIATION_REQUIRED")
    with sessions.begin() as db:
        receipt = db.scalar(select(CallReceipt).where(CallReceipt.id==receipt_id).with_for_update())
        if not receipt or receipt.status not in ("started","unknown"):
            raise DomainError("RECEIPT_STATE_CONFLICT")
        job = db.get(Job,receipt.job_id)
        if not job or job.status in ("running","ocr_running"):
            raise DomainError("JOB_STILL_RUNNING")
        previous = receipt.status
        if action=="recover":
            from inkwise.chunks import ChunkFeedback
            schemas={"DimensionScore":DimensionScore,"Reflection":Reflection,"ChunkFeedback":ChunkFeedback}
            schema=schemas.get(receipt.stage.split(":")[0])
            if schema is None or recovered_result is None:
                raise DomainError("RECOVERED_RESULT_REQUIRED")
            receipt.result=schema.model_validate(recovered_result).model_dump(mode="json",by_alias=True)
            receipt.status="succeeded"
        elif action=="retry":
            receipt.status="retry_authorized"
        else:
            raise DomainError("INVALID_RECOVERY_ACTION")
        receipt.resolutions=[*receipt.resolutions,{"actor":actor,"reason":reason,"at":now().isoformat(),
                                "previousStatus":previous,"attempt":receipt.attempt,"action":action}]
        db.add(JobEvent(job_id=job.id,tenant_id=job.tenant_id,actor_id=actor,event="provider.reconciled",
                        job_version=job.version,detail={"receiptId":receipt.id,"action":action}))
