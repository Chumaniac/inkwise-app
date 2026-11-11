"""Confirmed manual revisions enter the existing leased grading queue once."""
from copy import deepcopy
from uuid import uuid4

from sqlalchemy import select

from inkwise.cache_storage import ensure_free_space
from inkwise.contracts import JobCreate, Rubric
from inkwise.core import DIMENSIONS
from inkwise.database import Job, RevisionAssessment, WritingRevision, now, utc_iso
from inkwise.errors import DomainError
from inkwise.handoff import metadata
from inkwise.report_readiness import inspect_report
from inkwise.revisions import assert_source, get_revision, source_job
from inkwise.rubrics import canonical, digest
from inkwise.service import create_job, event, get_job


def _association(db, tenant, revision_id):
    return db.scalar(select(RevisionAssessment).where(
        RevisionAssessment.tenant_id == tenant, RevisionAssessment.revision_id == revision_id))


def assert_assessment_binding(db, child):
    """Recheck source pins before each provider reservation and final result write."""
    link = db.scalar(select(RevisionAssessment).where(
        RevisionAssessment.child_job_id == child.id, RevisionAssessment.tenant_id == child.tenant_id))
    if link is None:
        return
    parent = db.scalar(select(Job).where(Job.id == link.source_job_id, Job.tenant_id == link.tenant_id))
    revision = db.scalar(select(WritingRevision).where(WritingRevision.id == link.revision_id,
        WritingRevision.job_id == link.source_job_id, WritingRevision.tenant_id == link.tenant_id))
    if parent is None or revision is None or parent.status not in ('approved', 'published'):
        raise DomainError('REASSESSMENT_SOURCE_UNAVAILABLE')
    assert_source(parent, revision)
    pins = link.binding
    if (revision.status != 'confirmed' or revision.version != link.revision_version
            or revision.source_binding != pins['source'] or digest(revision.text) != pins['revisionTextHash']
            or child.classroom_id != link.classroom_id or parent.classroom_id != link.classroom_id
            or child.text_version != 1 or digest(child.text) != pins['revisionTextHash']
            or child.text_hash != pins['revisionTextHash'] or child.text != revision.text
            or digest(canonical(child.rubric)) != pins['rubricHash']
            or digest(canonical(child.payload)) != pins['payloadHash'] or child.image_id or child.ocr):
        raise DomainError('REASSESSMENT_BINDING_CHANGED')


def _bands(value):
    if not isinstance(value, dict) or set(value) != set(DIMENSIONS):
        return None
    if any(type(band) is not int or not 0 <= band <= 4 for band in value.values()):
        return None
    return {dimension: value[dimension] for dimension in DIMENSIONS}


def _total(bands, rubric):
    return round(sum(rubric.weights[dimension] * bands[dimension] / 4 for dimension in DIMENSIONS), 2)


def _view(db, user, parent, revision, link):
    if link is None:
        return {'schema': 'inkwise.revision-assessment/v1', 'revisionId': revision.id,
                'revisionVersion': revision.version, 'status': 'not_requested', 'job': None}
    child = get_job(db, user, link.child_job_id)
    if child.status == 'erased':
        return {'schema': 'inkwise.revision-assessment/v1', 'revisionId': revision.id,
                'revisionVersion': revision.version, 'status': 'erased', 'job': {'id': child.id, 'status': 'erased'}}
    assert_assessment_binding(db, child)
    rubric = Rubric.model_validate(child.rubric)
    before = _bands(parent.review.get('scores'))
    if before is None:
        raise DomainError('REASSESSMENT_BASELINE_INVALID')
    result = child.result or {}
    summary = {'schema': 'inkwise.revision-assessment/v1', 'id': link.id, 'revisionId': revision.id,
        'revisionVersion': revision.version, 'createdAt': utc_iso(link.created_at),
        'job': {'id': child.id, 'version': child.version, 'status': child.status, 'errorCode': child.error_code,
                'textVersion': child.text_version, 'textHash': child.text_hash, 'rubricHash': digest(canonical(child.rubric))},
        'sourceJobId': parent.id, 'before': {'scores': before, 'total': _total(before, rubric), 'teacherConfirmed': True},
        'status': child.status if child.status in ('queued', 'running', 'failed', 'cancelled') else 'incomplete',
        'after': None, 'delta': None,
        'comparison': {'rubric': 'same-frozen-release', 'modelContext': 'unrecorded', 'causalImprovement': False}}
    if child.status not in ('teacher_review', 'approved', 'published') or not result:
        return summary
    readiness = inspect_report(child)
    required = {'task_state', 'rubric_snapshot', 'text_binding', 'report_content', 'report_rubric_binding', 'model_identifier'}
    if any(check['state'] != 'passed' for check in readiness['checks'] if check['code'] in required):
        summary['status'] = 'incomplete'
        return summary
    reviewed = child.status in ('approved', 'published') and readiness['teacherConfirmed']
    after = _bands(child.review.get('scores')) if reviewed else _bands({row['dimension']: row['band'] for row in result.get('scores', [])})
    if after is None:
        summary['status'] = 'incomplete'
        return summary
    summary.update(status='reviewed' if reviewed else 'teacher_review',
        after={'scores': after, 'total': _total(after, rubric), 'teacherConfirmed': reviewed})
    # Deltas are teacher-final to teacher-final only. They describe the two
    # recorded evaluations, not a causal claim of learning or model accuracy.
    if reviewed:
        summary['delta'] = {'scores': {dimension: after[dimension] - before[dimension] for dimension in DIMENSIONS},
                            'total': round(_total(after, rubric) - _total(before, rubric), 2)}
    versions = [{key: metadata(report.get(key)) for key in ('modelRevision', 'pipelineRevision')}
                for report in (parent.result, result)]
    summary['versions'] = {'before': versions[0], 'after': versions[1]}
    summary['comparison']['modelContext'] = ('unrecorded' if any(value is None for row in versions for value in row.values())
                                            else 'same' if versions[0] == versions[1] else 'changed')
    summary['comparison']['aggregationContextChanged'] = any(
        digest(canonical((parent.result or {}).get(key))) != digest(canonical(result.get(key)))
        for key in ('aggregation', 'calibration', 'experiment'))
    return summary


def read_reassessment(db, user, job_id, revision_id):
    parent = source_job(db, user, job_id)
    revision = get_revision(db, user, parent, revision_id)
    return _view(db, user, parent, revision, _association(db, user.tenant_id, revision.id))


def request_reassessment(db, user, job_id, revision_id, body, key, settings):
    parent = source_job(db, user, job_id, writing=True)
    revision = get_revision(db, user, parent, revision_id)
    if revision.version != body.version or revision.status != 'confirmed' or not revision.confirmed_by or not revision.confirmed_at:
        raise DomainError('CONFIRMED_REVISION_REQUIRED')
    if _bands(parent.review.get('scores')) is None:
        raise DomainError('REASSESSMENT_BASELINE_INVALID')
    if digest(revision.text) == parent.text_hash:
        raise DomainError('REASSESSMENT_TEXT_UNCHANGED')
    # Three manual revisions per original report, one child per revision. A
    # child cannot recursively spawn reassessment trees or unbounded erasure.
    if db.scalar(select(RevisionAssessment.id).where(RevisionAssessment.child_job_id == parent.id,
                                                    RevisionAssessment.tenant_id == user.tenant_id)):
        raise DomainError('REASSESSMENT_CHAIN_LIMIT')
    previous_key = db.scalar(select(RevisionAssessment).where(RevisionAssessment.tenant_id == user.tenant_id,
        RevisionAssessment.requested_by == user.user_id, RevisionAssessment.idempotency_key == key))
    if previous_key and (previous_key.revision_id != revision.id or previous_key.revision_version != body.version):
        raise DomainError('IDEMPOTENCY_CONFLICT')
    old = _association(db, user.tenant_id, revision.id)
    if old:
        response = _view(db, user, parent, revision, old); db.commit(); return response
    ensure_free_space(settings, 256 * 1024)
    rubric = Rubric.model_validate(parent.rubric)
    allowed = {field.alias or name for name, field in JobCreate.model_fields.items()}
    payload = {name: value for name, value in parent.payload.items() if name in allowed}
    payload.update(text=revision.text, title=('修订复评 · ' + parent.payload['title'])[:100], intent='assess',
                   operatorRequest='', routingPreference='local', cloudConsent=False, maxCostMicrousd=0,
                   experimentConsent=False)
    child = create_job(db, user, JobCreate.model_validate(payload), 'revision-' + str(uuid4()),
                       settings.max_pending_per_tenant, commit=False)
    child.payload = {**child.payload, 'revisionAssessment': {'sourceJobId': parent.id,
        'revisionId': revision.id, 'revisionVersion': revision.version}}
    child.text_hash = digest(revision.text); child.text_version = 1; child.version = 2
    child.rubric = rubric.model_dump(mode='json', by_alias=True); child.status = 'queued'; child.updated_at = now()
    link = RevisionAssessment(id=str(uuid4()), tenant_id=user.tenant_id, classroom_id=parent.classroom_id,
        source_job_id=parent.id, revision_id=revision.id, revision_version=revision.version, child_job_id=child.id,
        requested_by=user.user_id, idempotency_key=key,
        binding={'source': deepcopy(revision.source_binding), 'revisionTextHash': child.text_hash,
                 'rubricHash': digest(canonical(child.rubric)), 'payloadHash': digest(canonical(child.payload))})
    db.add(link); db.flush()
    event(db, child, user.user_id, 'text.confirmed')
    event(db, parent, user.user_id, 'revision.reassessment_requested', {'revisionId': revision.id, 'childJobId': child.id})
    response = _view(db, user, parent, revision, link)
    db.commit()
    return response
