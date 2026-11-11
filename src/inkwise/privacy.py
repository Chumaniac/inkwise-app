"""Immediate content erasure with explicit follow-up for indexes and backups."""

from uuid import uuid4

from sqlalchemy import delete, select, update

from inkwise.contracts import Principal, Role
from inkwise.database import CallReceipt, Candidate, ChunkTask, Job, JobEvent, RevisionAssessment, TenantQuota, WritingRevision, now
from inkwise.errors import DomainError
from inkwise.service import get_job


def erase_job(db, principal: Principal, job_id: str, version: int, data_dir, *, _commit=True):
    """School admins can erase content while retaining minimal non-content audit.

    Already exported snapshots require operator storage cleanup; this is exposed
    explicitly, not declared complete merely because live rows were cleared.
    """
    if principal.role!=Role.ADMIN:
        raise DomainError("FORBIDDEN",403)
    job=get_job(db,principal,job_id)
    # Serialize erasure with revision/reassessment admission. Otherwise a child
    # could be admitted after the related-child query but before source erasure.
    locked=db.execute(update(TenantQuota).where(TenantQuota.tenant_id==principal.tenant_id)
        .values(revision=TenantQuota.revision+1))
    if locked.rowcount!=1:raise DomainError('TENANT_NOT_PROVISIONED',403)
    db.refresh(job,with_for_update=True)
    if job.version!=version or job.status=="erased":
        raise DomainError("VERSION_OR_STATE_CONFLICT")
    links=db.scalars(select(RevisionAssessment).where(RevisionAssessment.source_job_id==job.id,
        RevisionAssessment.tenant_id==principal.tenant_id).limit(4)).all()
    if len(links)>3:raise DomainError('REASSESSMENT_CHAIN_LIMIT')
    related_exported=False;related_erased=0
    for link in links:
        child=get_job(db,principal,link.child_job_id)
        if child.status!='erased':
            result=erase_job(db,principal,child.id,child.version,data_dir,_commit=False)
            related_exported=related_exported or result['exportedStorageCleanupRequired'];related_erased+=1
    image_id=job.image_id
    page_ids={page['imageId'] for page in (job.ocr or {}).get('pages',[])+(job.ocr or {}).get('supersededPages',[])}
    if image_id:page_ids.add(image_id)
    candidates=db.scalars(select(Candidate).where(Candidate.tenant_id==principal.tenant_id,Candidate.job_id==job.id)).all()
    approval_history=db.scalar(select(JobEvent.id).where(JobEvent.job_id==job.id,
        JobEvent.tenant_id==principal.tenant_id,JobEvent.event.in_(['candidate.approved','candidate.published'])).limit(1))
    exported=related_exported or approval_history is not None or any(c.status in ("approved","published") for c in candidates)
    changed=db.execute(update(Job).where(Job.id==job.id,Job.version==version).values(
        status="erased",text="",text_hash="",payload={"title":"内容已删除","grade":job.payload.get("grade")},
        result=None,review=None,ocr=None,image_id=None,error_code=None,lease_id=None,lease_until=None,
        request_hash="erased",idempotency_key="erased-"+job.id,version=version+1,updated_at=now()))
    if changed.rowcount!=1:
        raise DomainError("VERSION_CONFLICT")
    for candidate in candidates:
        candidate.status="withdrawn";candidate.version+=1
        candidate.text="";candidate.provenance="";candidate.review={};candidate.decision_reason=None
    db.execute(update(CallReceipt).where(CallReceipt.job_id==job.id).values(result=None,resolutions=[],status="erased"))
    db.execute(update(ChunkTask).where(ChunkTask.job_id==job.id).values(result=None,status="erased",lease_id=None))
    db.execute(delete(WritingRevision).where(WritingRevision.job_id==job.id,
                                           WritingRevision.tenant_id==principal.tenant_id))
    db.add(JobEvent(job_id=job.id,tenant_id=job.tenant_id,actor_id=principal.user_id,event="content.erased",
                    job_version=version+1,detail={"exportedStorageCleanupRequired":exported}))
    # Perform unlink before committing so a failed filesystem operation remains retryable.
    for identifier in page_ids:
        (data_dir/"images"/(identifier+".png")).unlink(missing_ok=True)
    import shutil
    cache=data_dir/'ocr-cache'/job.id
    if cache.exists():shutil.rmtree(cache)
    route_cache=data_dir/'routing'/job.id
    if route_cache.exists():shutil.rmtree(route_cache)
    if _commit:db.commit()
    else:db.flush()
    return {"id":job.id,"status":"erased","version":version+1,
            "liveContentErased":True,"relatedJobsErased":related_erased,"exportedStorageCleanupRequired":exported}
