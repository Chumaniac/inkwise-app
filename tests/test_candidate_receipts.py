"""Synthetic case handoff/readback; no model, OCR, or real pupil data."""
import copy
from sqlalchemy import func,select
from test_api import auth,client
from test_writing_revisions import approved
from inkwise.database import Candidate,JobEvent


def body(job,**changes):
    return {'version':job['version'],'anonymizedText':'Synthetic anonymized teaching case.',
            'anonymizedComment':'Synthetic teaching note.','provenance':'Synthetic permission record.',
            'reuseAuthorized':True,'reuseReviewFeedback':False,**changes}


def test_case_receipt_is_version_bound_reopenable_and_does_not_change_the_report(client,approved):
    before=copy.deepcopy(approved);url=f"/api/v1/jobs/{approved['id']}/candidates"
    assert client.get(url,headers=auth()).json()=={'jobId':approved['id'],'reportVersion':approved['version'],'candidate':None}
    response=client.post(url,headers=auth(),json=body(approved));assert response.status_code==201
    receipt=response.json();assert receipt['jobId']==approved['id'];assert receipt['reportVersion']==approved['version']
    assert client.post(url,headers=auth(),json=body(approved)).json()==receipt
    saved=client.get(url,headers=auth()).json()['candidate'];assert saved==receipt
    assert not {'text','review','provenance'} & saved.keys()
    detail=client.get('/api/v1/candidates/'+receipt['id'],headers=auth()).json()
    assert detail['id']==receipt['id'] and detail['jobId']==approved['id']
    assert detail['text']=='Synthetic anonymized teaching case.'
    with client.app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Candidate))==1
        assert db.scalar(select(func.count()).select_from(JobEvent).where(JobEvent.event=='candidate.submitted'))==1
    assert client.get('/api/v1/jobs/'+approved['id'],headers=auth()).json()==before


def test_case_readback_scopes_tenant_classroom_and_does_not_echo_input(client,approved):
    url=f"/api/v1/jobs/{approved['id']}/candidates";receipt=client.post(url,headers=auth(),json=body(approved)).json()
    for user in ['bob','eve']:
        for path in [url,'/api/v1/candidates/'+receipt['id']]:
            response=client.get(path,headers=auth(user));assert response.status_code==404
            assert 'Synthetic anonymized teaching case.' not in response.text
    assert client.get(url).status_code==401
    assert client.get('/api/v1/candidates/'+"' OR 1=1 --",headers=auth()).status_code==404
    assert client.get('/api/v1/candidates/'+receipt['id'],headers=auth('reader')).status_code==200


def test_case_consent_version_and_independence_remain_required(client,approved):
    url=f"/api/v1/jobs/{approved['id']}/candidates"
    assert client.post(url,headers=auth(),json=body(approved,reuseAuthorized=False)).status_code==422
    assert client.post(url,headers=auth(),json=body(approved,version=1)).status_code==409
    # Existing classroom authoring permissions stay unchanged. A research author
    # must still obtain the separate consent and cannot independently approve self.
    response=client.post(url,headers=auth('reader'),json=body(approved));assert response.status_code==201
    receipt=response.json();identifier=receipt['id']
    assert client.post('/api/v1/candidates/'+identifier+'/decisions',headers=auth('reader'),json={'version':receipt['version'],'decision':'approve','reason':'No self approval.'}).status_code==403
    assert client.post('/api/v1/candidates/'+identifier+'/withdraw',headers=auth('reader'),json={'version':receipt['version']}).status_code==200


def test_rejected_case_reopens_for_explicit_revision_and_remains_independently_reviewed(client,approved):
    url=f"/api/v1/jobs/{approved['id']}/candidates";receipt=client.post(url,headers=auth(),json=body(approved)).json();path='/api/v1/candidates/'+receipt['id']
    assert client.post(path+'/decisions',headers=auth(),json={'version':1,'decision':'approve','reason':'Self approval is not allowed.'}).status_code==403
    rejected=client.post(path+'/decisions',headers=auth('reader'),json={'version':1,'decision':'reject','reason':'Synthetic review requires a clearer note.'});assert rejected.status_code==200
    assert client.get(url,headers=auth()).json()['candidate']['status']=='rejected'
    assert client.get(path,headers=auth()).json()['decisionReason']=='Synthetic review requires a clearer note.'
    revised=client.post(path+'/revisions',headers=auth(),json=body(approved,version=2,anonymizedComment='Revised synthetic teaching note.'));assert revised.status_code==200
    assert revised.json()['status']=='pending'
    decision=client.post(path+'/decisions',headers=auth('reader'),json={'version':3,'decision':'approve','reason':'Synthetic independent review complete.'});assert decision.status_code==200
    assert client.get(url,headers=auth()).json()['candidate']['status']=='approved'
    assert client.get('/api/v1/jobs/'+approved['id'],headers=auth()).json()==approved


def test_receipt_scope_follows_the_current_job_version_and_historical_case_stays_readable(client,approved):
    url=f"/api/v1/jobs/{approved['id']}/candidates";receipt=client.post(url,headers=auth(),json=body(approved)).json()
    from inkwise.database import Job
    with client.app.state.sessions.begin() as db:db.get(Job,approved['id']).version+=1
    latest=client.get(url,headers=auth()).json();assert latest['reportVersion']==approved['version']+1;assert latest['candidate'] is None
    detail=client.get('/api/v1/candidates/'+receipt['id'],headers=auth());assert detail.status_code==200 and detail.json()['sourceCurrent'] is False
    assert detail.json()['reportVersion']==approved['version']
