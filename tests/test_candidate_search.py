"""Candidate lookup uses synthetic tenants, classrooms and source titles only."""
from inkwise.database import Candidate, Job
from test_api import auth, client as client, payload


def seed(http, key, title, status='pending', user='alice', classroom='a1'):
    response = http.post('/api/v1/jobs', headers={**auth(user), 'Idempotency-Key': key},
                         json={**payload(), 'title': title, 'classroomId': classroom})
    assert response.status_code == 201
    job = response.json()
    with http.app.state.sessions.begin() as db:
        db.add(Candidate(id='case-' + key, tenant_id='school-a', classroom_id=classroom,
                         job_id=job['id'], report_version=1, submitted_by=user,
                         text='Synthetic anonymized case', provenance='Synthetic source',
                         rubric={}, review={}, status=status, version=1))
    return job


def test_candidates_compose_state_classroom_literal_title_and_id_search(client):
    first = seed(client, 'first', 'Garden report')
    seed(client, 'second', 'Garden 100%_ review', 'approved')
    seed(client, 'otherclass', 'Garden other classroom', 'rejected', 'eve', 'a2')
    query = {'classroom': 'a1', 'status': 'pending', 'search': 'GARDEN'}
    page = client.get('/api/v1/candidates', headers=auth(), params=query)
    assert page.status_code == 200
    assert [row['id'] for row in page.json()['items']] == ['case-first']
    assert page.json()['items'][0]['sourceTitle'] == 'Garden report'
    for term in ['case-first', first['id']]:
        result = client.get('/api/v1/candidates', headers=auth(), params={'search': term}).json()
        assert [row['id'] for row in result['items']] == ['case-first']
    literal = client.get('/api/v1/candidates', headers=auth(), params={'search': '%_'}).json()
    assert [row['id'] for row in literal['items']] == ['case-second']
    empty = client.get('/api/v1/candidates', headers=auth(), params={'search': "' OR 1=1--"}).json()
    assert empty['items'] == []
    first_page = client.get('/api/v1/candidates?limit=1&search=Garden', headers=auth()).json()
    next_page = client.get('/api/v1/candidates?limit=1&offset=1&search=Garden', headers=auth()).json()
    assert first_page['hasMore'] and not next_page['hasMore']
    assert {row['id'] for page in [first_page, next_page] for row in page['items']} == {'case-first', 'case-second'}


def test_candidate_filters_cannot_widen_tenant_or_classroom_or_join_foreign_source_titles(client):
    seed(client, 'scope', 'Visible source')
    for user in ['bob', 'eve']:
        assert client.get('/api/v1/candidates?search=Visible', headers=auth(user)).json()['items'] == []
    for value in ['a2', "a1' OR 1=1--"]:
        assert client.get('/api/v1/candidates', headers=auth(), params={'classroom': value}).status_code == 404
    assert client.get('/api/v1/candidates?status=invalid', headers=auth()).status_code == 422
    assert client.get('/api/v1/candidates', headers=auth(), params={'search': 'x' * 101}).status_code == 422
    with client.app.state.sessions.begin() as db:
        db.add(Job(id='foreign-source', tenant_id='school-b', classroom_id='a1', owner_id='bob',
                   idempotency_key='foreign', request_hash='synthetic', payload={'title': 'PRIVATE_SOURCE_TITLE'},
                   rubric={}, status='approved'))
        db.add(Candidate(id='case-mismatched-source', tenant_id='school-a', classroom_id='a1',
                         job_id='foreign-source', report_version=1, submitted_by='alice',
                         text='Own synthetic case', provenance='Synthetic', rubric={}, review={}, status='pending'))
    result = client.get('/api/v1/candidates?search=PRIVATE_SOURCE_TITLE', headers=auth())
    assert result.json()['items'] == [] and 'PRIVATE_SOURCE_TITLE' not in result.text
