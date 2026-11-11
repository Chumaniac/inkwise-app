import json

import pytest

from inkwise.cli import seed_demo
from inkwise.database import Base, Candidate, Identity, Job, create_session_factory
from inkwise.errors import DomainError
from inkwise.knowledge import activate_bundle, atomic_json, export_bundle, rollback_release
from inkwise.rubrics import RubricRepository, digest
from inkwise.settings import Settings


def release_fixture(tmp_path):
    settings=Settings(database_url=f"sqlite:///{tmp_path}/db.sqlite",rubric_dir=tmp_path/"rules")
    engine,sessions=create_session_factory(settings.database_url)
    Base.metadata.create_all(engine);seed_demo(settings,"a")
    with sessions.begin() as db:
        db.add(Identity(id="synthetic-reviewer",tenant_id="a",role="reviewer",classroom_ids=["c"],token_hash="synthetic"))
    bundle=export_bundle(settings,"a","v2")
    atomic_json(bundle.parent/"indexed.json",{"bundleHash":digest(bundle.read_text()),"tenantId":"a",
        "release":"v2","embeddingModel":settings.embedding_model,"state":"indexed-not-active"})
    evaluation=tmp_path/"evaluation.json"
    atomic_json(evaluation,{"bundleHash":digest(bundle.read_text()),"decision":"approved","reviewer":"synthetic-reviewer",
        "tenantId":"a","release":"v2","modelRevision":"synthetic-model-v1","labelFileHash":"a"*64,
        "metrics":{"overall":{"count":5,"qwk":.8,"mae":.2,"adjacentAgreement":1.0}}})
    return settings,engine,sessions,bundle,evaluation


def test_release_requires_matching_evaluation_and_rolls_back_all_rules(tmp_path):
    settings,engine,sessions,bundle,evaluation=release_fixture(tmp_path)
    body=json.loads(bundle.read_text())
    assert body["cases"] == []
    approved=json.loads(evaluation.read_text())
    atomic_json(evaluation,{**approved,"bundleHash":"wrong"})
    with pytest.raises(DomainError): activate_bundle(settings,bundle,evaluation)
    assert RubricRepository(settings.rubric_dir).get("a","english-demo").release == "synthetic-v1"
    atomic_json(evaluation,approved)
    activate_bundle(settings,bundle,evaluation)
    assert all(r["release"]=="v2" for r in RubricRepository(settings.rubric_dir).list("a"))
    rollback_release(settings,"a","v2")
    assert all(r["release"]=="synthetic-v1" for r in RubricRepository(settings.rubric_dir).list("a"))
    engine.dispose()


def test_export_never_includes_unreviewed_candidate(tmp_path):
    settings=Settings(database_url=f"sqlite:///{tmp_path}/db.sqlite",data_dir=tmp_path/"data",rubric_dir=tmp_path/"rules")
    engine,sessions=create_session_factory(settings.database_url);Base.metadata.create_all(engine);seed_demo(settings,"a")
    rubric=RubricRepository(settings.rubric_dir).get("a","english-demo").model_dump(by_alias=True)
    with sessions.begin() as db:
        db.add(Candidate(id="synthetic",tenant_id="a",classroom_id="c",job_id="synthetic-job",report_version=1,
            submitted_by="teacher",text="Unreviewed synthetic content",provenance="synthetic",rubric=rubric,
            review={"scores":{},"comment":"test"},status="pending"))
    assert json.loads(export_bundle(settings,"a","v2").read_text())["cases"] == []
    engine.dispose()


def test_official_rule_import_requires_two_active_school_identities(tmp_path):
    from inkwise.database import Identity
    from inkwise.knowledge import import_rubric
    settings=Settings(database_url=f"sqlite:///{tmp_path}/db.sqlite",data_dir=tmp_path/"data",rubric_dir=tmp_path/"rules")
    engine,sessions=create_session_factory(settings.database_url);Base.metadata.create_all(engine);seed_demo(settings,"a")
    raw=RubricRepository(settings.rubric_dir).get("a","english-demo").model_dump(by_alias=True)
    raw.update({"id":"school-rule","release":"school-v1","synthetic":False})
    source=tmp_path/"approved-rule.json";source.write_text(json.dumps(raw))
    with sessions.begin() as db:
        db.add(Identity(id="teacher",tenant_id="a",role="teacher",classroom_ids=["c"],token_hash="synthetic-one"))
        db.add(Identity(id="reviewer",tenant_id="a",role="reviewer",classroom_ids=["c"],token_hash="synthetic-two"))
    with pytest.raises(DomainError):
        import_rubric(settings,"a",source,submitted_by="teacher",approved_by="teacher",source="Synthetic approval document")
    import_rubric(settings,"a",source,submitted_by="teacher",approved_by="reviewer",source="Synthetic approval document")
    assert not RubricRepository(settings.rubric_dir).get("a","school-rule").synthetic
    engine.dispose()


def test_reference_chunks_include_only_teacher_selected_feedback_without_losing_text():
    from inkwise.knowledge import reference_chunks
    text='含引号的“作文”\\n"'*1000
    case={'text':text,'scores':{'content':3},'comment':'Teacher summary',
          'feedback':{'content':{'include':True,'reason':'TEACHER_FINAL','suggestion':'Next step'},
                      'grammar':{'include':False,'reason':'UNAPPROVED_MODEL_DRAFT'}}}
    chunks=list(reference_chunks(case))
    assert ''.join(json.loads(data)['text'] for kind,start,end,data in chunks if kind=='essay')==text
    feedback=''.join(json.loads(data)['text'] for kind,start,end,data in chunks if kind=='teacher-feedback')
    assert 'TEACHER_FINAL' in feedback and 'UNAPPROVED_MODEL_DRAFT' not in feedback
    assert all(len(data)<=2000 for _,_,_,data in chunks)


def test_changed_criteria_cannot_relabel_an_old_teacher_anchor(tmp_path):
    from inkwise.knowledge import compatible_anchor
    settings=Settings(rubric_dir=tmp_path);seed_demo(settings,'a')
    original=RubricRepository(tmp_path).get('a','english-demo').model_dump(by_alias=True)
    new=json.loads(json.dumps(original));new['release']='new-data-release'
    assert compatible_anchor(original,new,7)
    new['rules'][0]['levels'][3]='A different teacher standard'
    assert not compatible_anchor(original,new,7)


def test_activation_rejects_unregistered_reviewer_and_untyped_metrics(tmp_path):
    settings=Settings(database_url=f'sqlite:///{tmp_path}/db.sqlite',rubric_dir=tmp_path/'rules')
    engine,_=create_session_factory(settings.database_url);Base.metadata.create_all(engine);seed_demo(settings,'a')
    bundle=export_bundle(settings,'a','v2')
    atomic_json(bundle.parent/'indexed.json',{'bundleHash':digest(bundle.read_text())})
    evaluation=tmp_path/'evaluation.json'
    atomic_json(evaluation,{'bundleHash':digest(bundle.read_text()),'decision':'approved',
        'reviewer':'made-up','metrics':{'x':1}})
    with pytest.raises(DomainError):activate_bundle(settings,bundle,evaluation)
    assert RubricRepository(settings.rubric_dir).get('a','english-demo').release=='synthetic-v1'
    engine.dispose()


def test_oversized_bundle_rejected_before_parsing(tmp_path):
    from inkwise.knowledge import validate_bundle
    path=tmp_path/'oversized.json'
    path.write_bytes(b' '*(8*1024*1024+1))
    with pytest.raises(DomainError,match='RELEASE_TOO_LARGE'):
        validate_bundle(Settings(),path)


@pytest.mark.parametrize("change", [
    {"reviewer":"unknown"}, {"modelRevision":""}, {"labelFileHash":"bad"},
    {"tenantId":"b"}, {"metrics":None}, {"labelFileHash":[]}, {"metrics":{"x":1}}, {"metrics":{"overall":{"count":5,"qwk":float("nan"),"mae":0,"adjacentAgreement":1}}},
])
def test_release_rejects_invalid_approval_provenance(tmp_path,change):
    settings,engine,sessions,bundle,evaluation=release_fixture(tmp_path)
    original=json.loads(evaluation.read_text())
    atomic_json(evaluation,{**original,**change})
    with pytest.raises(DomainError):activate_bundle(settings,bundle,evaluation)
    assert RubricRepository(settings.rubric_dir).get('a','english-demo').release=='synthetic-v1'
    engine.dispose()


def test_activation_manifest_failure_recovers_committed_switch(tmp_path,monkeypatch):
    import inkwise.knowledge as knowledge
    settings,engine,sessions,bundle,evaluation=release_fixture(tmp_path)
    original=knowledge.atomic_json
    def fail_pointer(path,value):
        if path.name=='active.json':raise OSError('synthetic write failure')
        original(path,value)
    monkeypatch.setattr(knowledge,'atomic_json',fail_pointer)
    with pytest.raises(OSError):activate_bundle(settings,bundle,evaluation)
    assert (settings.rubric_dir/'a'/'pending-activation.json').exists()
    assert RubricRepository(settings.rubric_dir).get('a','english-demo').release=='synthetic-v1'
    monkeypatch.setattr(knowledge,'atomic_json',original)
    knowledge.recover_release(settings,'a')
    assert RubricRepository(settings.rubric_dir).get('a','english-demo').release=='v2'
    assert not (settings.rubric_dir/'a'/'pending-activation.json').exists()
    rollback_release(settings,'a','v2')
    with pytest.raises(DomainError,match='ROLLBACK_RELEASE_NOT_CURRENT'):
        rollback_release(settings,'a','v2')
    engine.dispose()


def test_activation_database_failure_recovers_previous_pointer(tmp_path):
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    from inkwise.knowledge import recover_release
    settings,engine,sessions,bundle,evaluation=release_fixture(tmp_path)
    def fail_commit(session):raise RuntimeError('synthetic commit failure')
    event.listen(Session,'before_commit',fail_commit)
    try:
        with pytest.raises(RuntimeError):activate_bundle(settings,bundle,evaluation)
    finally:event.remove(Session,'before_commit',fail_commit)
    recover_release(settings,'a')
    assert RubricRepository(settings.rubric_dir).get('a','english-demo').release=='synthetic-v1'
    assert not (settings.rubric_dir/'a'/'pending-activation.json').exists()
    engine.dispose()


def test_bundle_rejects_candidate_attached_to_other_school_job(tmp_path):
    from inkwise.knowledge import validate_bundle
    settings,engine,sessions,bundle,evaluation=release_fixture(tmp_path)
    rubric=RubricRepository(settings.rubric_dir).get('a','english-demo').model_dump(by_alias=True)
    with sessions.begin() as db:
        db.add(Job(id='job-b',tenant_id='b',classroom_id='c',owner_id='teacher',idempotency_key='x',request_hash='x',payload={'grade':7}))
        db.add(Candidate(id='case-a',tenant_id='a',classroom_id='c',job_id='job-b',report_version=1,
            submitted_by='teacher',reviewed_by='reviewer',text='Synthetic',provenance='original',rubric=rubric,
            review={'scores':{key:3 for key in rubric['weights']},'comment':'test','reuseScope':'explicit-anonymized-v1'},status='approved'))
    body=json.loads(bundle.read_text())
    body['cases']=[{'id':'case-a','text':'Synthetic','rubricId':rubric['id'],'scores':{key:3 for key in rubric['weights']},
        'comment':'test','feedback':{},'grade':7,'provenance':'original','sourceReportVersion':1}]
    atomic_json(bundle,body)
    with pytest.raises(DomainError,match='RELEASE_CASE_AUTHORIZATION_INVALID'):validate_bundle(settings,bundle)
    engine.dispose()


def test_concurrent_withdrawal_cannot_be_resurrected_by_activation(tmp_path,monkeypatch):
    import inkwise.knowledge as knowledge
    from sqlalchemy import select, update
    settings,engine,sessions,bundle,evaluation=release_fixture(tmp_path)
    rubric=RubricRepository(settings.rubric_dir).get('a','english-demo').model_dump(by_alias=True)
    with sessions.begin() as db:
        db.add(Job(id='source',tenant_id='a',classroom_id='c',owner_id='teacher',idempotency_key='x',request_hash='x',payload={'grade':7}))
        db.add(Candidate(id='candidate',tenant_id='a',classroom_id='c',job_id='source',report_version=1,
            submitted_by='teacher',reviewed_by='synthetic-reviewer',text='Synthetic',provenance='original',rubric=rubric,
            review={'scores':{key:3 for key in rubric['weights']},'comment':'safe','reuseScope':'explicit-anonymized-v1'},status='approved'))
    bundle=export_bundle(settings,'a','v3')
    atomic_json(bundle.parent/'indexed.json',{'bundleHash':digest(bundle.read_text()),'tenantId':'a',
        'release':'v3','embeddingModel':settings.embedding_model,'state':'indexed-not-active'})
    proof=json.loads(evaluation.read_text());proof.update(bundleHash=digest(bundle.read_text()),release='v3')
    atomic_json(evaluation,proof)
    # Simulate a withdrawal committed after validation and candidate read, before publish.
    from sqlalchemy.orm import Session
    original=Session.get
    reads=0
    def get_with_withdrawal(db,entity,ident,*args,**kwargs):
        nonlocal reads
        if entity is Job and ident=='source':
            reads+=1
            if reads==3:
                with sessions.begin() as other:
                    other.execute(update(Candidate).where(Candidate.id=='candidate').values(status='withdrawn',version=2,text=''))
        return original(db,entity,ident,*args,**kwargs)
    monkeypatch.setattr(Session,'get',get_with_withdrawal)
    with pytest.raises(DomainError,match='RELEASE_CASE_AUTHORIZATION_INVALID'):
        activate_bundle(settings,bundle,evaluation)
    with sessions() as db:
        row=db.get(Candidate,'candidate')
        assert row.status=='withdrawn' and row.text==''
    engine.dispose()
