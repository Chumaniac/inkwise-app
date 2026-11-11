"""Continuous review uses synthetic candidates, no scoring or student records."""
import hashlib
from datetime import datetime, timezone, timedelta
from inkwise.database import Candidate, Job, Identity
from test_api import auth, client as client


def reviewer(http):
    with http.app.state.sessions.begin() as db:
        db.add(Identity(id='reviewer', tenant_id='school-a', classroom_ids=['a1'], role='reviewer',
                        token_hash=hashlib.sha256(b'synthetic-reviewer').hexdigest()))
    return auth('reviewer')


def seed(http, name, *, second=0, tenant='school-a', classroom='a1', author='alice', version=1,
         bound=1, state='approved', title='Garden report'):
    with http.app.state.sessions.begin() as db:
        db.add(Job(id='job-'+name, tenant_id=tenant, classroom_id=classroom, owner_id=author,
                   idempotency_key=name, request_hash='synthetic', payload={'title': title},
                   rubric={}, review={}, status=state, version=version))
        db.add(Candidate(id=name, tenant_id=tenant, classroom_id=classroom, job_id='job-'+name,
                         report_version=bound, submitted_by=author, text='Synthetic anonymized case',
                         provenance='Synthetic permission', rubric={}, review={}, status='pending', version=1,
                         created_at=datetime(2026,1,1,tzinfo=timezone.utc)+timedelta(seconds=second)))


def test_review_queue_has_stable_cursor_after_a_record_is_reviewed_and_source_metadata(client):
    headers=reviewer(client)
    seed(client,'alpha'); seed(client,'beta')
    first=client.get('/api/v1/candidate-review-queue/next',headers=headers)
    assert first.status_code==200
    item=first.json()['item']; assert item['id']=='alpha' and first.json()['nextAfter']=='alpha'
    assert item['sourceCurrent'] is True and item['sourceVersion']==item['reportVersion']==1
    assert item['sourceStatus']=='approved'
    with client.app.state.sessions.begin() as db:
        db.get(Candidate,'alpha').status='approved'
    following=client.get('/api/v1/candidate-review-queue/next',headers=headers,params={'after_id':'alpha'}).json()
    assert following['item']['id']=='beta'
    assert client.get('/api/v1/candidate-review-queue/next',headers=headers,params={'after_id':'beta'}).json()['item'] is None
    assert client.get('/api/v1/candidate-review-queue/next',headers=headers).json()['item']['id']=='beta'
    with client.app.state.sessions() as db:
        assert db.get(Candidate,'beta').version==1 and db.get(Candidate,'beta').status=='pending'
    snapshot=client.get('/api/v1/candidates/beta',headers=headers).json()
    assert snapshot['sourceVersion']==1 and snapshot['sourceStatus']=='approved'
    listed=client.get('/api/v1/candidates',headers=headers).json()['items']
    assert all(row['sourceVersion']==1 and row['sourceCurrent'] for row in listed)


def test_review_queue_never_widens_scope_or_returns_stale_self_or_unready_sources(client):
    headers=reviewer(client)
    seed(client,'visible',title='Garden 100%_')
    seed(client,'stale',version=2);seed(client,'not-ready',state='teacher_review')
    seed(client,'own',author='reviewer');seed(client,'other-class',classroom='a2');seed(client,'other-tenant',tenant='school-b')
    response=client.get('/api/v1/candidate-review-queue/next',headers=headers,params={'classroom':'a1','search':'%_'})
    assert response.status_code==200 and response.json()['item']['id']=='visible'
    assert client.get('/api/v1/candidate-review-queue/next',headers=headers,params={'search':"' OR 1=1--"}).json()['item'] is None
    assert client.get('/api/v1/candidate-review-queue/next',headers=auth()).status_code==403
    assert client.get('/api/v1/candidate-review-queue/next',headers=headers,params={'classroom':'a2'}).status_code==404
    assert client.get('/api/v1/candidate-review-queue/next',headers=headers,params={'after_id':'other-class'}).status_code==404
    assert client.get('/api/v1/candidate-review-queue/next',headers=headers,params={'search':'x'*101}).status_code==422
    assert client.get('/api/v1/candidate-review-queue/next',headers=headers,params={'after_id':'../outside'}).status_code==422
    stale=client.get('/api/v1/candidates/stale',headers=headers).json()
    assert stale['sourceCurrent'] is False and stale['sourceVersion']==2


def test_review_queue_cannot_join_a_foreign_source_even_in_a_legacy_candidate(client):
    headers=reviewer(client)
    with client.app.state.sessions.begin() as db:
        db.add(Job(id='private-source',tenant_id='school-b',classroom_id='a1',owner_id='bob',
                   idempotency_key='foreign',request_hash='synthetic',payload={'title':'PRIVATE_FOREIGN_TITLE'},
                   rubric={},status='approved',version=1))
        db.add(Candidate(id='legacy',tenant_id='school-a',classroom_id='a1',job_id='private-source',
                         report_version=1,submitted_by='alice',text='Own synthetic case',provenance='Synthetic',
                         rubric={},review={},status='pending',version=1))
    page=client.get('/api/v1/candidate-review-queue/next',headers=headers)
    assert page.status_code==200 and page.json()['item'] is None
    assert 'PRIVATE_FOREIGN_TITLE' not in page.text
    listed=client.get('/api/v1/candidates',headers=headers).json()['items'][0]
    assert listed['sourceVersion'] is None and listed['sourceStatus']=='unavailable' and not listed['sourceCurrent']
