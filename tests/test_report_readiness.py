"""Synthetic version-bound report checks; no model or student data."""
import hashlib
from copy import deepcopy

import pytest

from inkwise.contracts import DIMENSIONS
from inkwise.database import Job
from test_api import auth, client, create


def saved_report(client, *, status='teacher_review', legacy=False):
    identifier = create(client, 'readiness-synthetic').json()['id']
    text = 'Synthetic essay.'
    checksum = hashlib.sha256(text.encode()).hexdigest()
    rubric = client.app.state.rubrics.get('school-a', 'english-demo')
    result = {'scores': [{'dimension': dimension, 'band': 3, 'reason': 'Synthetic reason',
                         'suggestion': 'Synthetic suggestion', 'evidence': [{'start': 0, 'end': 9,
                         'quote': 'Synthetic', 'ruleId': next(rule.id for rule in rubric.rules if rule.dimension == dimension)}]}
                        for dimension in DIMENSIONS], 'total': 75, 'model': 'synthetic-model',
              'textHash': checksum, 'rubricId': rubric.id, 'release': rubric.release, 'warnings': []}
    if not legacy:
        result.update(modelRevision='synthetic-model-v1', pipelineRevision='synthetic-pipeline-v1')
    with client.app.state.sessions.begin() as db:
        record = db.get(Job, identifier)
        record.text_version = 1; record.text_hash = checksum; record.status = status
        record.rubric = rubric.model_dump(mode='json', by_alias=True); record.result = result
    return identifier


def test_unready_job_explains_next_step_without_echoing_content(client):
    job = create(client).json()
    url = f"/api/v1/jobs/{job['id']}"
    response = client.get(url+'/report-readiness', headers=auth(), params={'version': 1})
    assert response.status_code == 200
    data = response.json(); assert data['schema'] == 'inkwise.report-readiness/v1'
    assert data['state'] == 'blocked' and data['canExport'] is False and data['nextAction'] == 'confirm_text'
    assert len(data['checks']) <= 12
    for key in ['text','result','review','payload','binding']:
        assert key not in data
    assert job['text'] not in response.text
    # Diagnostic snapshots remain available; the printable purpose has a separate gate.
    assert client.get(url+'/export-snapshot', headers=auth(), params={'version': 1}).status_code == 200
    blocked = client.get(url+'/export-snapshot', headers=auth(), params={'version': 1, 'purpose': 'printable'})
    assert blocked.status_code == 409 and blocked.json()['error']['code'] == 'REPORT_EXPORT_NOT_READY'


def test_bound_saved_report_can_export_as_teacher_review_draft(client):
    identifier = saved_report(client)
    url = f'/api/v1/jobs/{identifier}'
    ready = client.get(url+'/report-readiness', headers=auth(), params={'version': 1}).json()
    assert ready['canExport'] and ready['state'] == 'review' and not ready['teacherConfirmed']
    exported = client.get(url+'/export-snapshot', headers=auth(), params={'version': 1, 'purpose': 'printable'})
    assert exported.status_code == 200 and exported.json()['readiness'] == ready


def test_missing_legacy_version_metadata_is_visible_and_not_invented(client):
    identifier = saved_report(client, legacy=True)
    data = client.get(f'/api/v1/jobs/{identifier}/report-readiness', headers=auth(), params={'version': 1}).json()
    assert data['canExport'] and data['state'] == 'review'
    assert {'model_revision','pipeline_revision'} <= {x['code'] for x in data['checks'] if x['state']=='warning'}
    exported = client.get(f'/api/v1/jobs/{identifier}/export-snapshot', headers=auth(), params={'version': 1, 'purpose': 'printable'}).json()
    assert exported['binding']['modelRevision'] is None and exported['binding']['pipelineRevision'] is None


@pytest.mark.parametrize('damage', ['text','rubric','shape'])
def test_mismatched_or_invalid_saved_data_cannot_become_a_printable_report(client, damage):
    identifier = saved_report(client)
    with client.app.state.sessions.begin() as db:
        record = db.get(Job, identifier); value = dict(record.result)
        if damage == 'text': value['textHash'] = '0'*64
        elif damage == 'rubric': value['release'] = 'different-release'
        else: value['scores'] = [None]
        record.result = value
    data = client.get(f'/api/v1/jobs/{identifier}/report-readiness', headers=auth(), params={'version': 1}).json()
    assert data['state']=='blocked' and not data['canExport']
    assert client.get(f'/api/v1/jobs/{identifier}/export-snapshot', headers=auth(), params={'version': 1, 'purpose': 'printable'}).status_code == 409


def test_readiness_keeps_version_tenant_and_classroom_boundaries(client):
    identifier = saved_report(client)
    url = f'/api/v1/jobs/{identifier}/report-readiness'
    assert client.get(url, headers=auth(), params={'version': 2}).status_code == 409
    for user in ['bob','eve']:
        assert client.get(url, headers=auth(user), params={'version': 1}).status_code == 404
    assert client.get(url, params={'version':1}).status_code == 401


def test_teacher_confirmed_saved_report_has_a_complete_ready_state(client):
    identifier = saved_report(client)
    confirmed = client.post(f'/api/v1/jobs/{identifier}/reviews', headers=auth(), json={
        'version':1,'scores':{dimension:3 for dimension in DIMENSIONS},'comment':'Synthetic teacher confirmation'})
    assert confirmed.status_code == 200
    version = confirmed.json()['version']
    response = client.get(f'/api/v1/jobs/{identifier}/report-readiness', headers=auth(), params={'version':version})
    data=response.json();assert data['state']=='ready' and data['canExport'] and data['teacherConfirmed']


def test_no_change_feedback_requires_rewriting_without_regrading(client):
    identifier = saved_report(client)
    url = f'/api/v1/jobs/{identifier}'
    invalid = "The verb 'polished' should be 'polished'."
    with client.app.state.sessions.begin() as db:
        record = db.get(Job, identifier)
        original = deepcopy(record.result)
        original['scores'][-1]['reason'] = invalid
        record.result = original
    view = client.get(url, headers=auth()).json()
    assert view['feedbackIssues'] == {'grammar': 'NO_CHANGE_CORRECTION'}
    assert view['result'] == original and view['version'] == 1
    review = {'version': 1, 'scores': dict.fromkeys(DIMENSIONS, 3), 'comment': 'Synthetic teacher review',
              'feedbackConfirmed': True, 'feedback': {'grammar': {'include': True, 'reason': invalid,
              'suggestion': 'Check the tense.', 'evidence': original['scores'][-1]['evidence']}}}
    rejected = client.post(url+'/reviews', headers=auth(), json=review)
    assert rejected.status_code == 422 and rejected.json()['error']['code'] == 'FEEDBACK_REWRITE_REQUIRED'
    assert client.get(url, headers=auth()).json()['version'] == 1
    review['feedback']['grammar']['reason'] = 'The past tense is consistent.'
    accepted = client.post(url+'/reviews', headers=auth(), json=review)
    assert accepted.status_code == 200 and accepted.json()['result'] == original
    version = accepted.json()['version']
    ready = client.get(url+'/report-readiness', headers=auth(), params={'version': version}).json()
    assert ready['state'] == 'ready' and ready['teacherConfirmed']
    # Previously saved teacher feedback gets the same read-only export check.
    with client.app.state.sessions.begin() as db:
        record = db.get(Job, identifier); changed = deepcopy(record.review)
        changed['feedback']['grammar']['suggestion'] = invalid; record.review = changed
    blocked = client.get(url+'/report-readiness', headers=auth(), params={'version': version}).json()
    assert not blocked['canExport']
    with client.app.state.sessions() as db:
        assert db.get(Job, identifier).result == original and db.get(Job, identifier).review == changed


@pytest.mark.parametrize('text,expected', [
    ("'polished' should be 'polished'", True), ('“写了”改为“写了”', True),
    ("'polished' -> 'polished'", True), ("'polish' should be 'polished'", False),
    ("'i' should be 'I'", False), ("'a  b' should be 'a b'", False),
    ('Keep the past tense consistent.', False),
])
def test_no_change_guard_keeps_real_capitalization_and_spacing_corrections(text, expected):
    from inkwise.contracts import feedback_requires_rewrite
    assert feedback_requires_rewrite(text, '') is expected


@pytest.mark.parametrize('dimension', [[], {}])
def test_feedback_flags_do_not_crash_on_a_malformed_saved_dimension(client, dimension):
    identifier = saved_report(client)
    with client.app.state.sessions.begin() as db:
        record = db.get(Job, identifier); changed = deepcopy(record.result)
        changed['scores'][-1]['dimension'] = dimension; record.result = changed
    view = client.get(f'/api/v1/jobs/{identifier}', headers=auth())
    assert view.status_code == 200 and view.json()['feedbackIssues'] == {}
    ready = client.get(f'/api/v1/jobs/{identifier}/report-readiness', headers=auth(), params={'version': 1}).json()
    assert not ready['canExport']


def test_feedback_report_is_exportable_as_feedback_without_a_score(client):
    identifier = saved_report(client)
    with client.app.state.sessions.begin() as db:
        record=db.get(Job,identifier);value=dict(record.result);value.update(kind='feedback',scores=[],total=None,findings=[]);record.result=value
    data=client.get(f'/api/v1/jobs/{identifier}/report-readiness',headers=auth(),params={'version':1}).json()
    assert data['canExport'] and data['reportKind']=='feedback' and not data['teacherConfirmed']


@pytest.mark.parametrize('source', ['model', 'teacher'])
def test_inconsistent_totals_block_printing_without_rewriting_saved_reports(client, source):
    identifier = saved_report(client)
    version = 1
    if source == 'teacher':
        reviewed = client.post(f'/api/v1/jobs/{identifier}/reviews', headers=auth(), json={
            'version': version, 'scores': dict.fromkeys(DIMENSIONS, 3), 'comment': 'Synthetic confirmation'})
        assert reviewed.status_code == 200
        version = reviewed.json()['version']
    field = 'result' if source == 'model' else 'review'
    with client.app.state.sessions.begin() as db:
        record = db.get(Job, identifier)
        altered = deepcopy(getattr(record, field)); altered['total'] = 100
        setattr(record, field, altered)
        preserved = deepcopy(altered)
    url = f'/api/v1/jobs/{identifier}'
    response = client.get(url+'/report-readiness', headers=auth(), params={'version': version})
    assert response.status_code == 200
    readiness = response.json()
    assert readiness['state'] == 'blocked' and not readiness['canExport']
    reason = 'REPORT_TOTAL_MISMATCH' if source == 'model' else 'TEACHER_TOTAL_MISMATCH'
    assert {'code': 'report_content', 'state': 'blocked', 'reason': reason} in readiness['checks']
    assert 'Synthetic reason' not in response.text
    blocked = client.get(url+'/export-snapshot', headers=auth(), params={'version': version, 'purpose': 'printable'})
    assert blocked.status_code == 409 and blocked.json()['error']['code'] == 'REPORT_EXPORT_NOT_READY'
    assert client.get(url+'/export-snapshot', headers=auth(), params={'version': version}).status_code == 200
    with client.app.state.sessions() as db:
        record = db.get(Job, identifier)
        assert getattr(record, field) == preserved and record.version == version


@pytest.mark.parametrize('bands,weights,total', [
    ([0, 0, 0, 0, 0], [20, 20, 20, 20, 20], 0),
    ([4, 4, 4, 4, 4], [20, 20, 20, 20, 20], 100),
    ([0, 1, 2, 3, 4], [10.01, 19.99, 20, 25, 25], 58.75),
])
def test_total_validation_keeps_zero_full_and_rounded_weighted_scores(client, bands, weights, total):
    identifier = saved_report(client)
    with client.app.state.sessions.begin() as db:
        record = db.get(Job, identifier)
        rubric = deepcopy(record.rubric); rubric['weights'] = dict(zip(DIMENSIONS, weights)); record.rubric = rubric
        result = deepcopy(record.result)
        for score, band in zip(result['scores'], bands):
            score['band'] = band
        result['total'] = total; record.result = result
    url = f'/api/v1/jobs/{identifier}'
    assert client.get(url+'/report-readiness', headers=auth(), params={'version': 1}).json()['canExport']
    assert client.get(url+'/export-snapshot', headers=auth(), params={'version': 1, 'purpose': 'printable'}).status_code == 200
