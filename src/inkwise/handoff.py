"""Version-bound, read-only task metadata for an authorized teacher handoff."""

import re

from sqlalchemy import or_, select

from inkwise.database import Job, JobEvent, now, utc_iso
from inkwise.errors import DomainError
from inkwise.service import job_scope

NEXT = {
    "needs_text_review": ("confirm_text", "对照原稿核对文字，再由任课教师确认。"),
    "teacher_review": ("review_report", "核对报告与原文依据，再由任课教师复核。"),
    "failed": ("inspect_failure", "查看失败代码和记录；结果未知的请求先由管理员核查，不自动重试。"),
    "approved": ("read_report", "查看已保存报告；后续修改需重新核对版本。"),
    "published": ("read_report", "查看已发布报告；后续修改需重新核对版本。"),
    "cancelled": ("closed", "本任务已取消，保留现有记录。"),
    "queued": ("wait", "等待已提交的评阅；不要重复提交。"),
    "running": ("wait", "评阅尚未结束，可稍后回到同一任务查看。"),
    "ocr_queued": ("wait", "等待已提交的识别；不要重复上传。"),
    "ocr_running": ("wait", "识别尚未结束，可稍后回到同一任务查看。"),
}


def metadata(value, limit=256):
    return value if isinstance(value, str) and len(value) <= limit and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]*", value) else None


def task_handoff(db, principal, job_id: str, version: int) -> dict:
    # Project only metadata: student text, payload, OCR, review and report bodies
    # never become application objects in this path. JSON extraction is bound SQL.
    record = db.execute(select(
        Job.id, Job.tenant_id, Job.classroom_id, Job.version, Job.text_version,
        Job.status, Job.error_code, Job.updated_at,
        Job.rubric["release"].as_string().label("rubric_release"),
        Job.result["modelRevision"].as_string().label("model_revision"),
        Job.result["pipelineRevision"].as_string().label("pipeline_revision"),
        or_(Job.result["modelRevision"].as_string().is_not(None),
            Job.result["model"].as_string().is_not(None),
            Job.result["kind"].as_string() == "feedback").label("has_report"),
    ).where(Job.id == job_id, *job_scope(principal))).mappings().first()
    if record is None or record.status == "erased":
        raise DomainError("NOT_FOUND", 404)
    if record.version != version:
        raise DomainError("VERSION_CONFLICT")
    latest = db.execute(select(JobEvent.id, JobEvent.event, JobEvent.job_version, JobEvent.created_at)
        .where(JobEvent.tenant_id == principal.tenant_id, JobEvent.job_id == job_id,
               JobEvent.job_version <= version).order_by(JobEvent.id.desc()).limit(1)).first()
    if db.scalar(select(Job.version).where(Job.id == job_id, *job_scope(principal))) != version:
        raise DomainError("VERSION_CONFLICT")
    action, instruction = NEXT.get(record.status, ("inspect_events", "状态尚不明确，请核对处理记录。"))
    if record.status in {"teacher_review", "approved", "published"} and not record.has_report:
        action, instruction = "inspect_events", "报告元数据暂不可用，请先核对处理记录；不要重复提交。"
    return {
        "schema": "inkwise.task-handoff/v1", "jobId": record.id,
        "tenantId": record.tenant_id, "classroomId": record.classroom_id,
        "jobVersion": record.version, "textVersion": record.text_version,
        "status": record.status if record.status in NEXT else "unknown",
        "rubricRelease": metadata(record.rubric_release),
        "modelRevision": metadata(record.model_revision),
        "pipelineRevision": metadata(record.pipeline_revision),
        "errorCode": metadata(record.error_code, 64),
        "lastEvent": {"id": latest.id, "event": metadata(latest.event, 64),
                      "version": latest.job_version, "createdAt": utc_iso(latest.created_at)} if latest else None,
        "nextAction": action, "instruction": instruction,
        "updatedAt": utc_iso(record.updated_at), "generatedAt": utc_iso(now()),
    }
