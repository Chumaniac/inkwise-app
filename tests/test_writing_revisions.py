"""Synthetic revision flows; no OCR, model calls or actual student material."""
import copy
import hashlib

import pytest
from sqlalchemy import text

from test_api import auth, client, create, payload  # Existing isolated API fixture.
from inkwise.contracts import JobCreate
from inkwise.database import Identity, Job


@pytest.fixture
def approved(client):
    item=create(client).json()
    original="First synthetic sentence.\nSecond synthetic sentence."
    quote="Second synthetic sentence."
    rubric=client.app.state.rubrics.for_job("school-a",JobCreate.model_validate(payload())).model_dump(mode="json",by_alias=True)
    with client.app.state.sessions.begin() as db:
        job=db.get(Job,item['id']);job.text=original;job.text_version=1
        job.text_hash=hashlib.sha256(original.encode()).hexdigest();job.version=3;job.status='approved';job.rubric=rubric
        job.result={'modelRevision':'synthetic-no-provider','textHash':job.text_hash,'textVersion':1,'scores':[]}
        job.review={'scores':dict.fromkeys(['content','coherence','expression','vocabulary','grammar'],3),
            'comment':'Review the ending.','reviewerId':'alice','textHash':job.text_hash,
            'feedback':{'grammar':{'include':True,'reason':'Review this sentence.','suggestion':'Use a clear ending.',
                'evidence':[{'start':len(original)-len(quote),'end':len(original),'quote':quote,'ruleId':'synthetic-rule'}]}}}
        db.add(Identity(id='reader',tenant_id='school-a',classroom_ids=['a1'],role='reviewer',token_hash=hashlib.sha256(b'synthetic-reader').hexdigest()))
    return client.get('/api/v1/jobs/'+item['id'],headers=auth()).json()


def begin(client,job,key='revision-one'):
    return client.post(f"/api/v1/jobs/{job['id']}/revisions",headers={**auth(),'Idempotency-Key':key},json={'version':job['version']})


def test_revision_reopen_compare_confirm_preserves_first_draft(client,approved):
    original=copy.deepcopy(approved);base=f"/api/v1/jobs/{approved['id']}"
    response=begin(client,approved);assert response.status_code==201
    revision=response.json();assert begin(client,approved).json()['id']==revision['id']
    assert revision['source']['textHash']==original['textHash']
    assert revision['text']==original['text']
    listing=client.get(base+'/revisions',headers=auth()).json()
    assert len(listing['items'])==1 and 'text' not in listing['items'][0]
    changed='First synthetic sentence.\nA clearer ending.'
    states={item['id']:'addressed' for item in revision['feedback']}
    edited=client.patch(base+'/revisions/'+revision['id'],headers=auth(),json={'version':1,'text':changed,'resolutions':states,'note':'Ending checked.'})
    assert edited.status_code==200;revised=edited.json()
    assert ''.join(part['before']for part in revised['diff']['parts'])==original['text']
    assert ''.join(part['after']for part in revised['diff']['parts'])==changed
    assert revised['feedback'][-1]['evidence'][0]['quote']=='Second synthetic sentence.'
    stale=client.patch(base+'/revisions/'+revision['id'],headers=auth(),json={'version':1,'text':'stale','resolutions':states})
    assert stale.status_code==409
    confirmed=client.post(base+'/revisions/'+revision['id']+'/confirmation',headers=auth(),json={'version':2,'confirmed':True})
    assert confirmed.status_code==200 and confirmed.json()['status']=='confirmed'
    assert client.get(base+'/revisions/'+revision['id'],headers=auth()).json()['text']==changed
    assert client.patch(base+'/revisions/'+revision['id'],headers=auth(),json={'version':3,'text':'overwrite','resolutions':states}).status_code==409
    assert client.get(base,headers=auth()).json()==original
    with client.app.state.sessions() as db:
        assert db.execute(text('SELECT count(*) FROM call_receipts')).scalar()==0
        events=db.execute(text("SELECT detail FROM job_events WHERE event LIKE 'revision.%'" )).all()
        assert original['text'] not in str(events) and changed not in str(events)


def test_revision_scope_and_source_version_are_enforced(client,approved):
    revision=begin(client,approved).json();base=f"/api/v1/jobs/{approved['id']}/revisions"
    for user in ['bob','eve']:
        assert client.get(base,headers=auth(user)).status_code==404
        assert client.get(base+'/'+revision['id'],headers=auth(user)).status_code==404
        assert client.post(base,headers={**auth(user),'Idempotency-Key':'different'},json={'version':3}).status_code==404
        assert client.patch(base+'/'+revision['id'],headers=auth(user),json={'version':1,'text':'foreign','resolutions':{}}).status_code==404
        assert client.post(base+'/'+revision['id']+'/confirmation',headers=auth(user),json={'version':1,'confirmed':True}).status_code==404
    assert client.post(base,headers={**auth('reader'),'Idempotency-Key':'different'},json={'version':3}).status_code==403
    assert client.post(base,headers={**auth(),'Idempotency-Key':'old'},json={'version':2}).status_code==409
    with client.app.state.sessions.begin()as db:
        item=db.get(Job,approved['id']);item.review={**item.review,'comment':'Changed outside the approved workflow.'}
    assert client.get(base+'/'+revision['id'],headers=auth()).status_code==409


def test_revision_limits_validation_and_explicit_confirmation(client,approved):
    revision=begin(client,approved).json();base=f"/api/v1/jobs/{approved['id']}/revisions"
    assert client.post(base+'/'+revision['id']+'/confirmation',headers=auth(),json={'version':1,'confirmed':True}).status_code==409
    response=client.patch(base+'/'+revision['id'],headers=auth(),json={'version':1,'text':'PRIVATE_SYNTHETIC_'*1000,'resolutions':{}})
    assert response.status_code==422 and 'PRIVATE_SYNTHETIC_'not in response.text
    assert client.patch(base+'/'+revision['id'],headers=auth(),json={'version':1,'text':'changed','resolutions':{'invented':'addressed'}}).status_code==422
    private_key=client.patch(base+'/'+revision['id'],headers=auth(),json={'version':1,'text':'changed','resolutions':{'PRIVATE_SYNTHETIC_NOTE':'invalid'}})
    assert private_key.status_code==422 and 'PRIVATE_SYNTHETIC_NOTE'not in private_key.text
    assert begin(client,approved,'two').status_code==201
    assert begin(client,approved,'three').status_code==201
    assert begin(client,approved,'four').status_code==429
    assert begin(client,approved).json()['id']==revision['id']
    assert client.post(base+'/'+revision['id']+'/confirmation',headers=auth(),json={'version':1,'confirmed':1}).status_code==422


def test_erasure_covers_revision_text_without_resurrecting_it(client,approved):
    revision=begin(client,approved).json();base=f"/api/v1/jobs/{approved['id']}"
    assert client.post(base+'/erasures',headers=auth('admin'),json={'version':3}).status_code==200
    assert client.get(base+'/revisions/'+revision['id'],headers=auth()).status_code==404
    with client.app.state.sessions() as db:
        assert db.execute(text('SELECT count(*) FROM writing_revisions')).scalar()==0


def test_revision_input_is_literal_and_low_storage_preserves_saved_work(client,approved,monkeypatch):
    from types import SimpleNamespace
    import inkwise.cache_storage as storage
    revision=begin(client,approved).json();url=f"/api/v1/jobs/{approved['id']}/revisions/{revision['id']}"
    states={item['id']:'needs_work'for item in revision['feedback']}
    content="'; DROP TABLE jobs; -- <img src=x onerror=alert(1)>"
    result=client.patch(url,headers=auth(),json={'version':1,'text':content,'resolutions':states})
    assert result.status_code==200 and result.json()['text']==content
    monkeypatch.setattr(storage,'_disk_usage',lambda path:SimpleNamespace(free=0))
    assert client.patch(url,headers=auth(),json={'version':2,'text':'must not replace','resolutions':states}).status_code==507
    assert client.get(url,headers=auth()).json()['text']==content
    assert client.get('/api/v1/jobs/'+approved['id'],headers=auth()).json()==approved


def test_publication_metadata_keeps_the_original_revision_binding(client,approved):
    revision=begin(client,approved).json();base=f"/api/v1/jobs/{approved['id']}/revisions"
    with client.app.state.sessions.begin()as db:
        job=db.get(Job,approved['id']);job.status='published';job.version=4
    retained=client.get(base+'/'+revision['id'],headers=auth())
    assert retained.status_code==200 and retained.json()['source']['jobVersion']==3
    assert begin(client,approved).json()['id']==revision['id']
    next_revision=client.post(base,headers={**auth(),'Idempotency-Key':'after-publication'},json={'version':4})
    assert next_revision.status_code==201 and next_revision.json()['text']==approved['text']


@pytest.mark.parametrize('before,after',[
    ('甲。'*7500,'乙。'*7500),
    ('same sentence.\n'*1000,'same sentence!\n'*1000),
    ('😀first\nSecond.','😀first\nA changed second.'),
])
def test_text_diff_is_bounded_and_reconstructs_exact_unicode(before,after):
    from inkwise.revisions import compare_text
    value=compare_text(before,after)
    assert len(value['parts'])<=512
    assert ''.join(part['before']for part in value['parts'])==before
    assert ''.join(part['after']for part in value['parts'])==after
