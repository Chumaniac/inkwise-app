"""Read-only report structure and binding checks; no essay or feedback is returned."""
import hashlib
from datetime import datetime
import math

from pydantic import ValidationError

from inkwise.contracts import DIMENSIONS, DimensionScore, FeedbackFinding, Review, Rubric, ScoringResult, feedback_requires_rewrite
from inkwise.errors import DomainError
from inkwise.handoff import NEXT, metadata
from inkwise.providers import validate_evidence


def inspect_report(job):
    result = job.result if isinstance(job.result, dict) else {}
    has_review = job.review is not None
    review = job.review if isinstance(job.review, dict) else None
    checks = []

    def add(code, state, reason):
        checks.append({'code': code, 'state': state, 'reason': reason})

    active = job.status in {'teacher_review', 'approved', 'published'}
    add('task_state', 'passed' if active else 'blocked', 'REPORT_STATE_READY' if active else 'REPORT_STATE_PENDING')
    rubric = None
    try:
        rubric = Rubric.model_validate(job.rubric)
        add('rubric_snapshot', 'passed', 'RUBRIC_RECORDED')
    except (ValidationError, TypeError, ValueError):
        add('rubric_snapshot', 'blocked', 'RUBRIC_UNAVAILABLE')
    checksum = hashlib.sha256(job.text.encode('utf-8')).hexdigest()
    text_ok = (job.text_version >= 1 and job.text_hash == checksum and result.get('textHash') == checksum
               and (review is None or review.get('textHash') == checksum))
    add('text_binding', 'passed' if text_ok else 'blocked', 'TEXT_BINDING_MATCHED' if text_ok else 'TEXT_BINDING_UNAVAILABLE_OR_CHANGED')
    kind = 'feedback' if result.get('kind') == 'feedback' else 'scoring'
    renderable = bool(result) and rubric is not None and (not has_review or bool(review))
    content_reason = 'REPORT_CONTENT_UNAVAILABLE_OR_INVALID'
    try:
        if not renderable:
            raise ValueError('missing report')
        if kind == 'feedback':
            findings = result.get('findings')
            if not isinstance(findings, list) or len(findings) > 12:
                raise ValueError('invalid findings')
            if result.get('total') is not None or result.get('scores', []) != []:
                raise ValueError('feedback cannot publish scores')
            if review and (not isinstance(review.get('comment'), str) or not 1 <= len(review['comment'].strip()) <= 3000):
                raise ValueError('invalid teacher feedback')
            chosen = review.get('findings', []) if review else findings
            if not isinstance(chosen, list) or len(chosen) > 12:
                raise ValueError('invalid reviewed findings')
            for raw in chosen:
                item = FeedbackFinding.model_validate(raw)
                validate_evidence(item.evidence, job.text, rubric, item.dimension)
        else:
            scores = result.get('scores')
            if not isinstance(scores, list) or any(not isinstance(row, dict) for row in scores):
                raise ValueError('invalid scores')
            fields = DimensionScore.model_fields
            parsed = ScoringResult.model_validate({'scores': [{k: v for k, v in row.items() if k in fields} for row in scores],
                                                  'model': result.get('model'), 'warnings': result.get('warnings', [])})
            total = result.get('total')
            if isinstance(total, bool) or not isinstance(total, (int, float)) or not math.isfinite(total) or not 0 <= total <= 100:
                raise ValueError('invalid total')
            expected_total = round(sum(rubric.weights[score.dimension] * score.band / 4 for score in parsed.scores), 2)
            # Use the grading/review two-decimal rule, allowing only binary
            # serialization noise. Inspection never repairs the saved result.
            if not math.isclose(total, expected_total, rel_tol=0, abs_tol=1e-9):
                content_reason = 'REPORT_TOTAL_MISMATCH'
                raise ValueError('inconsistent total')
            if review:
                selected = {k: v for k, v in review.items() if k in {'version','scores','comment','feedback','feedbackConfirmed'}}
                checked = Review.model_validate(selected)
                if set(checked.scores) != set(DIMENSIONS):
                    raise ValueError('incomplete teacher scores')
                for dimension, feedback in checked.feedback.items():
                    if feedback.include:
                        if feedback_requires_rewrite(feedback.reason, feedback.suggestion):
                            raise ValueError('no-change correction')
                        for evidence in feedback.evidence:
                            validate_evidence(evidence, job.text, rubric, dimension)
                final_total = review.get('total')
                if isinstance(final_total, bool) or not isinstance(final_total, (int, float)) or not math.isfinite(final_total) or not 0 <= final_total <= 100:
                    raise ValueError('invalid teacher total')
                expected_final = round(sum(rubric.weights[dimension] * band / 4 for dimension, band in checked.scores.items()), 2)
                if not math.isclose(final_total, expected_final, rel_tol=0, abs_tol=1e-9):
                    content_reason = 'TEACHER_TOTAL_MISMATCH'
                    raise ValueError('inconsistent teacher total')
            else:
                for score in parsed.scores:
                    for evidence in score.evidence:
                        validate_evidence(evidence, job.text, rubric, score.dimension)
    except (ValidationError, DomainError, TypeError, ValueError, AttributeError, KeyError):
        renderable = False
    add('report_content', 'passed' if renderable else 'blocked', 'REPORT_STRUCTURE_CHECKED' if renderable else content_reason)
    if rubric:
        mismatch = (result.get('rubricId') not in (None, rubric.id) or result.get('release') not in (None, rubric.release))
        recorded = result.get('rubricId') is not None and result.get('release') is not None
        add('report_rubric_binding', 'blocked' if mismatch else 'passed' if recorded else 'warning',
            'RUBRIC_BINDING_CHANGED' if mismatch else 'RUBRIC_BINDING_MATCHED' if recorded else 'LEGACY_RUBRIC_BINDING_UNRECORDED')
    model = result.get('model')
    model_ok = isinstance(model, str) and 0 < len(model.strip()) <= 200
    add('model_identifier', 'passed' if model_ok else 'blocked', 'MODEL_RECORDED' if model_ok else 'MODEL_UNRECORDED')
    for code, key in [('model_revision','modelRevision'), ('pipeline_revision','pipelineRevision')]:
        value = result.get(key)
        valid = metadata(value) is not None
        add(code, 'passed' if valid else 'warning' if value is None else 'blocked',
            'VERSION_RECORDED' if valid else 'VERSION_UNRECORDED' if value is None else 'VERSION_METADATA_INVALID')
    confirmation_recorded = False
    if review and isinstance(review.get('reviewerId'), str) and 0 < len(review['reviewerId'].strip()) <= 200:
        try:
            value = review.get('reviewedAt')
            if isinstance(value, str) and len(value) <= 40:
                datetime.fromisoformat(value.replace('Z','+00:00'))
                confirmation_recorded = True
        except ValueError:
            pass
    confirmed = confirmation_recorded and job.status in {'approved','published'}
    add('teacher_confirmation', 'passed' if confirmed else 'blocked' if job.status in {'approved','published'} or has_review else 'warning',
        'TEACHER_CONFIRMED' if confirmed else 'TEACHER_CONFIRMATION_UNAVAILABLE' if job.status in {'approved','published'} or has_review else 'TEACHER_REVIEW_REQUIRED')
    allowed = not any(item['state'] == 'blocked' for item in checks)
    state = 'blocked' if not allowed else 'review' if any(item['state'] == 'warning' for item in checks) else 'ready'
    action = NEXT.get(job.status, ('inspect_events', ''))[0]
    if not allowed and active:
        action = 'inspect_events'
    return {'schema':'inkwise.report-readiness/v1', 'jobId':job.id, 'jobVersion':job.version,
            'state':state, 'canExport':allowed, 'reportKind':kind if renderable else 'unavailable',
            'teacherConfirmed':confirmed, 'nextAction':action, 'checks':checks}
