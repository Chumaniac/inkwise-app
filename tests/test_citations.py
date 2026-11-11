import copy
import hashlib
from types import SimpleNamespace

import pytest

from inkwise.citations import CitationLookup, locate_citation
from inkwise.database import Job
from inkwise.errors import DomainError
from test_api import auth, client, create


def sample(text='😀 First sentence.\n\nSecond sentence.', quote='Second sentence.'):
    start = text.index(quote)
    evidence = {'start': start, 'end': start + len(quote), 'quote': quote, 'ruleId': 'synthetic-rule'}
    checksum = hashlib.sha256(text.encode()).hexdigest()
    job = SimpleNamespace(version=3, text_version=1, text_hash=checksum, text=text, review=None,
                          result={'textHash': checksum, 'scores': [{'evidence': [evidence]}]},
                          ocr={'text': text, 'pages': [{'page': n, 'imageId': f'synthetic-{n}', 'sha256': str(n)*64} for n in [1, 2]],
                               'lines': [{'page': n, 'text': line, 'box': [.1, .2, .8, .1]} for n, line in enumerate(text.split('\n\n'), 1)]})
    request = CitationLookup(version=3, text_version=1, text_hash=checksum,
                             quote=quote, start=start, end=start + len(quote))
    return job, request


def test_exact_multi_page_navigation_and_utf16_offsets():
    job, request = sample()
    original = copy.deepcopy(job.__dict__)
    result = locate_citation(job, request)
    assert result['status'] == 'region' and result['pages'][0]['page'] == 2
    assert result['startUtf16'] == request.start + 1
    assert result['pages'][0]['imageSha256'] == '2'*64
    assert job.__dict__ == original
    assert request.quote not in str(result), 'navigation metadata does not echo the essay'
    job, request = sample(quote='First sentence.\n\nSecond sentence.')
    assert [page['page'] for page in locate_citation(job, request)['pages']] == [1, 2]


@pytest.mark.parametrize('case,reason', [('repeated', 'repeated-quote'), ('edited', 'transcript-edited'),
                                        ('legacy', 'report-binding-unrecorded'), ('missing', 'coordinates-unavailable'),
                                        ('invalid', 'coordinates-unavailable')])
def test_uncertain_geometry_falls_back_to_text(case, reason):
    job, request = sample()
    if case == 'repeated':
        job, request = sample(text='Same sentence.\n\nSame sentence.', quote='Same sentence.')
    if case == 'edited':
        job.ocr['text'] += ' old OCR'
    if case == 'legacy':
        del job.result['textHash']
    if case == 'missing':
        job.ocr['pages'] = []
    if case == 'invalid':
        job.ocr['lines'][1]['box'] = [0, 0, 2, 1]
    result = locate_citation(job, request)
    assert result['status'] == 'text-only' and result['reason'] == reason
    assert not any(page['boxes'] for page in result['pages'])


@pytest.mark.parametrize('field,value', [('version', 2), ('text_version', 2), ('text_hash', '0'*64)])
def test_old_versions_never_get_current_coordinates(field, value):
    job, request = sample()
    with pytest.raises(DomainError, match='VERSION_CONFLICT'):
        locate_citation(job, request.model_copy(update={field: value}))


def test_scoped_api_reuses_existing_report_without_writes(client):
    row = create(client).json()
    job, request = sample(text='Synthetic essay.', quote='Synthetic essay.')
    with client.app.state.sessions.begin() as db:
        record = db.get(Job, row['id'])
        for name in ('version', 'text_version', 'text_hash', 'text', 'result', 'ocr'):
            setattr(record, name, getattr(job, name))
        record.status = 'teacher_review'
    path = f"/api/v1/jobs/{row['id']}/citation-location"
    body = request.model_dump(by_alias=True)
    assert client.post(path, headers=auth(), json=body).status_code == 200
    for user in ('bob', 'eve'):
        assert client.post(path, headers=auth(user), json=body).status_code == 404
    assert client.post(path, json=body).status_code == 401
    assert client.post(path, headers=auth(), json={**body, 'version': 1}).status_code == 409
    assert client.post(path, headers=auth(), json={**body, 'quote': 'unrecorded'}).status_code == 422
    assert client.post("/api/v1/jobs/' OR 1=1--/citation-location", headers=auth(), json=body).status_code == 404
    with client.app.state.sessions.begin() as db:
        record = db.get(Job, row['id'])
        assert record.version == 3 and record.result == job.result and record.ocr == job.ocr
