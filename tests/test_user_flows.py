from datetime import timedelta

from sqlalchemy import select

from inkwise.core import DIMENSIONS
from inkwise.database import Job,Candidate,JobEvent,now
from test_api import client,auth,payload,create


def test_lists_reach_older_records_and_preserve_classroom_scope(client):
    with client.app.state.sessions.begin() as db:
        for i in range(35):
            identifier=f'old-{i:02}'
            db.add(Job(id=identifier,tenant_id='school-a',classroom_id='a1',owner_id='alice',idempotency_key=identifier,
                request_hash='synthetic',status='approved',payload={**payload(),'title':f'Lesson {i}'},created_at=now()-timedelta(days=i)))
            db.add(Candidate(id=identifier,tenant_id='school-a',classroom_id='a1',job_id=identifier,report_version=1,
                submitted_by='alice',text='Synthetic',provenance='Synthetic',rubric={},review={},created_at=now()-timedelta(days=i)))
    first=client.get('/api/v1/jobs?limit=30',headers=auth()).json()
    assert first['hasMore'] and len(first['items'])==30
    last=client.get('/api/v1/jobs?limit=30&offset=30',headers=auth()).json()
    assert not last['hasMore'] and len(last['items'])==5
    found=client.get('/api/v1/jobs?search=Lesson%2034',headers=auth()).json()['items']
    assert [row['id'] for row in found]==['old-34']
    assert client.get('/api/v1/jobs?search=%25',headers=auth()).json()['items']==[]
    candidates=client.get('/api/v1/candidates?limit=20&offset=20',headers=auth()).json()
    assert len(candidates['items'])==15 and not candidates['hasMore']
    assert client.get('/api/v1/candidates?offset=20',headers=auth('eve')).json()['items']==[]


def test_withdrawal_is_visible_after_reload_without_changing_grade_or_worker_lease(client):
    identifier=create(client).json()['id']
    with client.app.state.sessions.begin() as db:
        row=db.get(Job,identifier);row.status='running';row.lease_id='synthetic-lease';row.result={'total':75,'experiment':{'id':'trial'}}
    url=f'/api/v1/jobs/{identifier}'
    assert client.post(url+'/experiment-withdrawal',headers=auth(),json={'version':1}).status_code==200
    snapshot=client.get(url,headers=auth()).json()
    assert snapshot['experimentParticipation']=='withdrawn' and snapshot['result']['total']==75 and snapshot['version']==1
    with client.app.state.sessions.begin() as db:
        row=db.get(Job,identifier);assert row.lease_id=='synthetic-lease';row.status='teacher_review'
    reconfirmed=client.post(url+'/text-confirmations',headers=auth(),json={'version':1,'text':'Reconfirmed synthetic text'})
    assert reconfirmed.status_code==200 and reconfirmed.json()['experimentParticipation']=='withdrawn'


def test_unassigned_job_cannot_be_marked_as_withdrawn(client):
    identifier = create(client).json()['id']
    url = f'/api/v1/jobs/{identifier}'
    response = client.post(url + '/experiment-withdrawal', headers=auth(), json={'version': 1})
    assert response.status_code == 409
    assert response.json()['error']['code'] == 'INVALID_STATE'
    assert client.get(url, headers=auth()).json()['experimentParticipation'] is None
    with client.app.state.sessions() as db:
        assert db.scalar(select(JobEvent.id).where(
            JobEvent.job_id == identifier, JobEvent.event == 'experiment.withdrawn',
        )) is None


def test_assigned_running_job_withdrawal_is_scoped_versioned_and_idempotent(client):
    identifier = create(client).json()['id']
    with client.app.state.sessions.begin() as db:
        job = db.get(Job, identifier)
        job.status = 'running'
        job.lease_id = 'synthetic-lease'
        db.add(JobEvent(job_id=identifier, tenant_id=job.tenant_id, actor_id='alice',
            event='experiment.assigned', job_version=1, detail={'id': 'synthetic-trial'}))
    url = f'/api/v1/jobs/{identifier}/experiment-withdrawal'
    for user in ('bob', 'eve'):
        assert client.post(url, headers=auth(user), json={'version': 1}).status_code == 404
    assert client.post(url, headers=auth(), json={'version': 2}).status_code == 409
    for _ in range(2):
        assert client.post(url, headers=auth(), json={'version': 1}).status_code == 200
    with client.app.state.sessions() as db:
        job = db.get(Job, identifier)
        assert job.status == 'running' and job.lease_id == 'synthetic-lease'
        assert job.version == 1 and job.result is None
        withdrawals = db.scalars(select(JobEvent).where(
            JobEvent.job_id == identifier, JobEvent.event == 'experiment.withdrawn',
        )).all()
        assert len(withdrawals) == 1


def test_completed_experiment_can_withdraw_without_changing_approved_grade(client):
    identifier = create(client).json()['id']
    with client.app.state.sessions.begin() as db:
        job = db.get(Job, identifier)
        job.status = 'approved'
        job.result = {'total': 75, 'experiment': {'id': 'synthetic-trial'}}
        job.review = {'total': 80}
    response = client.post(f'/api/v1/jobs/{identifier}/experiment-withdrawal',
        headers=auth(), json={'version': 1})
    assert response.status_code == 200
    with client.app.state.sessions() as db:
        job = db.get(Job, identifier)
        assert job.status == 'approved' and job.review == {'total': 80}
        assert job.result['total'] == 75 and job.version == 1


def approved_job(client):
    from inkwise.rubrics import RubricRepository
    identifier=create(client).json()['id']
    with client.app.state.sessions.begin() as db:
        row=db.get(Job,identifier);row.status='approved'
        row.rubric=RubricRepository(client.app.state.settings.rubric_dir).get('school-a','english-demo').model_dump(by_alias=True)
        row.review={'scores':dict.fromkeys(DIMENSIONS,3),'total':75,'comment':'Private synthetic original'}
    return identifier


def test_changed_candidate_is_not_silently_discarded_and_author_can_revise_pending(client):
    identifier=approved_job(client)
    content={'version':1,'anonymizedText':'Anonymous one','anonymizedComment':'Sanitized one','reuseAuthorized':True,'provenance':'Synthetic permission'}
    url=f'/api/v1/jobs/{identifier}/candidates'
    first=client.post(url,headers=auth(),json=content).json()
    assert client.post(url,headers=auth(),json=content).json()['id']==first['id']
    changed={**content,'anonymizedText':'Anonymous two'}
    result=client.post(url,headers=auth(),json=changed)
    assert result.status_code==409 and result.json()['error']['code']=='CANDIDATE_ALREADY_EXISTS'
    revise=f"/api/v1/candidates/{first['id']}/revisions"
    assert client.post(revise,headers=auth('eve'),json=changed).status_code==404
    assert client.post(revise,headers=auth('admin'),json=changed).status_code==403
    saved=client.post(revise,headers=auth(),json=changed)
    assert saved.status_code==200 and saved.json()['version']==2
    assert client.post(revise,headers=auth(),json=changed).status_code==409
    with client.app.state.sessions.begin() as db:
        item=db.get(Candidate,first['id']);item.status='rejected';item.reviewed_by='other-reviewer';item.decision_reason='Needs edits'
    resubmitted=client.post(revise,headers=auth(),json={**changed,'version':2})
    assert resubmitted.status_code==200 and resubmitted.json()['status']=='pending'
    with client.app.state.sessions.begin() as db:
        item=db.get(Candidate,first['id']);assert item.text=='Anonymous two' and item.reviewed_by is None and item.decision_reason is None;item.status='approved'
    assert client.post(revise,headers=auth(),json={**changed,'version':3}).status_code==409
    assert client.post(f'/api/v1/jobs/{identifier}/cancel',headers=auth(),json={'version':1}).status_code==409


def test_failed_ocr_page_can_be_replaced_without_deleting_originals(client):
    from io import BytesIO
    from PIL import Image
    def png(color):
        output=BytesIO();Image.new('RGB',(100,100),color).save(output,format='PNG');return output.getvalue()
    identifier=create(client).json()['id'];url=f'/api/v1/jobs/{identifier}'
    initial=client.post(url+'/pages',headers=auth(),data={'version':'1'},files=[('pages',('one.png',png('white'),'image/png')),('pages',('two.png',png('gray'),'image/png'))]).json()
    with client.app.state.sessions.begin() as db:
        row=db.get(Job,identifier);row.status='failed';row.error_code='OCR_NO_TEXT'
    original_ids=[row['imageId'] for row in initial['ocr']['pages']]
    files={'image':('replacement.png',png('black'),'image/png')};body={'version':str(initial['version']),'page':'2'}
    assert client.post(url+'/page-replacement',headers=auth('bob'),data=body,files=files).status_code==404
    replaced=client.post(url+'/page-replacement',headers=auth(),data=body,files=files)
    assert replaced.status_code==200
    data=replaced.json();assert data['status']=='ocr_queued'
    assert data['ocr']['pages'][0]['imageId']==original_ids[0]
    assert data['ocr']['pages'][1]['imageId']!=original_ids[1]
    assert data['ocr']['supersededPages'][0]['imageId']==original_ids[1]
    assert client.get(url+'/image?archived=true&page=1',headers=auth()).status_code==200
    assert client.post(url+'/page-replacement',headers=auth(),data=body,files=files).status_code==409
    with client.app.state.sessions.begin() as db:db.get(Job,identifier).status='needs_text_review'
    result=client.post(url+'/text-confirmations',headers=auth(),json={'version':data['version'],'text':'Manually confirmed synthetic text','pageOrderConfirmed':True}).json()
    assert client.post(url+'/page-replacement',headers=auth(),data={'version':str(result['version']),'page':'2'},files=files).status_code==409
    image_ids=original_ids+[data['ocr']['pages'][1]['imageId']]
    erased=client.post(url+'/erasures',headers=auth('admin'),json={'version':result['version']})
    assert erased.status_code==200
    assert all(not (client.app.state.settings.data_dir/'images'/(key+'.png')).exists() for key in image_ids)


def test_concurrent_candidate_submissions_are_idempotent(client,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    import inkwise.service as service
    identifier=approved_job(client);original=service.get_job;barrier=Barrier(2)
    def together(*args):
        item=original(*args);barrier.wait(timeout=5);return item
    monkeypatch.setattr(service,'get_job',together)
    content={'version':1,'anonymizedText':'Anonymous concurrency sample','reuseAuthorized':True,'provenance':'Synthetic'}
    def submit():return client.post(f'/api/v1/jobs/{identifier}/candidates',headers=auth(),json=content)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first,second=[future.result(timeout=10) for future in [pool.submit(submit),pool.submit(submit)]]
    assert first.status_code==second.status_code==201
    assert first.json()['id']==second.json()['id']
