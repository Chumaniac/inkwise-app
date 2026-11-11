import json
from datetime import datetime,timezone
from types import SimpleNamespace

import pytest

from inkwise.cli import seed_demo,provision
from inkwise.contracts import Principal,ProposalAction,Role
from inkwise.core import DIMENSIONS
from inkwise.database import Base,Candidate,Job,create_session_factory
from inkwise.errors import DomainError
from inkwise.evolution import learning_cycle,list_proposals,proposal_action,create_experiment,choose_rubric,evaluate_experiment
from inkwise.knowledge import import_rubric
from inkwise.rubrics import RubricRepository,canonical
from inkwise.settings import Settings


@pytest.fixture
def school(tmp_path):
    settings=Settings(database_url=f'sqlite:///{tmp_path}/db.sqlite',data_dir=tmp_path/'data',rubric_dir=tmp_path/'rules',
        evaluation_salt='synthetic-functional-salt-32-characters',evolution_min_cases=20)
    engine,sessions=create_session_factory(settings.database_url);Base.metadata.create_all(engine);seed_demo(settings,'a')
    teacher=provision(settings,'a',['c1','c2','c3','c4'],'teacher',tmp_path/'teacher')
    reviewer=provision(settings,'a',['c1','c2','c3','c4'],'reviewer',tmp_path/'reviewer')
    rubric=RubricRepository(settings.rubric_dir).get('a','english-demo').model_copy(update={'synthetic':False,'release':'base-v1','weight_learning_allowed':True})
    path=tmp_path/'formal.json';path.write_text(canonical(rubric.model_dump(by_alias=True)))
    import_rubric(settings,'a',path,submitted_by=teacher,approved_by=reviewer,source='Synthetic functional test approval, not school validation')
    yield settings,engine,sessions,rubric,teacher,reviewer
    engine.dispose()


def seed_cases(school,count=120):
    settings,engine,sessions,rubric,teacher,reviewer=school
    with sessions.begin() as db:
        for i in range(count):
            value=i%5;model={d:value for d in DIMENSIONS};model['grammar']=min(4,value+1)
            labels={**model,'grammar':min(4,value+2)};classroom='c1' if i%2 else 'c2'
            db.add(Job(id=str(i),tenant_id='a',classroom_id=classroom,owner_id=teacher,idempotency_key=str(i),request_hash='x',
                payload={'grade':7,'studentRef':'p-'+str(i),'text':'SYNTHETIC_PRIVATE_TEXT'},rubric=rubric.model_dump(by_alias=True),status='approved',
                result={'model':'synthetic','modelRevision':'fixed-v1','pipelineRevision':settings.pipeline_revision,
                    'scores':[{'dimension':d,'band':model[d]} for d in DIMENSIONS]},review={'scores':labels}))
            db.add(Candidate(id='case-'+str(i),tenant_id='a',classroom_id=classroom,job_id=str(i),report_version=1,
                submitted_by=teacher,reviewed_by=reviewer,status='approved',text='SYNTHETIC_PRIVATE_TEXT',provenance='original synthetic',
                rubric=rubric.model_dump(by_alias=True),review={'scores':labels,'reuseScope':'explicit-anonymized-v1'}))


def test_periodic_proposal_is_idempotent_private_and_cannot_auto_publish(school):
    settings,engine,sessions,rubric,teacher,reviewer=school;seed_cases(school)
    clock=datetime(2026,10,12,tzinfo=timezone.utc)
    first=learning_cycle(settings,'a',clock);second=learning_cycle(settings,'a',clock)
    assert first==second and first['automaticPublication'] is False
    principal=Principal(userId=teacher,tenantId='a',role=Role.TEACHER,classroomIds=['c1','c2'])
    proposals=list_proposals(settings,principal)
    assert len(proposals)==1 and proposals[0]['hasProposedRubric']
    assert 'SYNTHETIC_PRIVATE_TEXT' not in json.dumps(proposals)
    assert RubricRepository(settings.rubric_dir).get('a',rubric.id).release=='base-v1'
    identifier=proposals[0]['id']
    submitted=proposal_action(settings,principal,identifier,ProposalAction(version=1,action='submit',note='Synthetic review'))
    with pytest.raises(DomainError):proposal_action(settings,principal,identifier,ProposalAction(version=submitted['version'],action='approve',note='self approval'))
    authorized=Principal(userId=reviewer,tenantId='a',role=Role.REVIEWER,classroomIds=['c1','c2'])
    approved=proposal_action(settings,authorized,identifier,ProposalAction(version=2,action='approve',note='Independent synthetic review'))
    assert approved['state']=='approved-inactive'
    assert RubricRepository(settings.rubric_dir).get('a',rubric.id).release=='base-v1'
    record=list_proposals(settings,authorized)[0]
    experiment=create_experiment(settings,'a',{'id':'shadow-one','rubricId':rubric.id,'candidateRelease':record['proposedRelease'],
        'classrooms':['c1','c2'],'mode':'shadow','modelRevision':'fixed-v1'},reviewer)
    job=SimpleNamespace(id='new',tenant_id='a',classroom_id='c1',payload={'grade':7,'intent':'assess','studentRef':'p-new','experimentConsent':True})
    selected,assignment=choose_rubric(settings,job,rubric)
    assert selected.release=='base-v1' and assignment['mode']=='shadow'
    from inkwise.database import JobEvent
    from inkwise.evolution import report_experiment
    from inkwise.knowledge import atomic_json
    job.rubric=rubric.model_dump(by_alias=True);job.text_hash='synthetic-text'
    with sessions.begin() as db:
        db.add(Job(id='new',tenant_id='a',classroom_id='c1',owner_id=teacher,idempotency_key='new',request_hash='x',payload={}))
        db.add(JobEvent(job_id='new',tenant_id='a',actor_id=teacher,event='experiment.assigned',job_version=1,detail={**assignment,'textHash':job.text_hash}))
    model_report={'modelRevision':'fixed-v1','pipelineRevision':settings.pipeline_revision,'total':50,
        'scores':[{'dimension':d,'band':2} for d in DIMENSIONS]}
    trial_path=settings.data_dir/'evolution'/'a'/'experiments'/'shadow-one.json'
    experiment['enabled']=False;atomic_json(trial_path,experiment)
    assert report_experiment(settings,sessions,job,model_report)['status']=='inactive'
    experiment['enabled']=True;atomic_json(trial_path,experiment)
    with sessions.begin() as db:db.get(Candidate,'case-0').status='withdrawn'
    assert report_experiment(settings,sessions,job,model_report)['status']=='variant-source-changed'
    assert choose_rubric(settings,job,rubric)[1] is None

    with pytest.raises(DomainError,match='PROPOSAL_SOURCE_REVOKED'):
        proposal_action(settings,authorized,identifier,ProposalAction(version=3,action='publish',experimentId='shadow-one',note='must fail'))


def test_live_trial_preserves_normative_rules_and_regression_pauses_future_assignment(school,tmp_path):
    from inkwise.evaluation import cohort
    from inkwise.database import Identity
    settings,engine,sessions,rubric,teacher,reviewer=school
    variant=rubric.model_copy(update={'release':'variant-v1','weights':{**rubric.weights,'content':rubric.weights['content']-2,'grammar':rubric.weights['grammar']+2}})
    path=tmp_path/'variant.json';path.write_text(canonical(variant.model_dump(by_alias=True)))
    import_rubric(settings,'a',path,submitted_by=teacher,approved_by=reviewer,source='Synthetic weight-only variant',activate=False)
    classes={'A':[],'B':[]}
    for i in range(100):
        classroom='trial-'+str(i);group=cohort('a',classroom,'live-one',settings.evaluation_salt.get_secret_value())
        if len(classes[group])<2:classes[group].append(classroom)
        if all(len(value)==2 for value in classes.values()):break
    all_classes=classes['A']+classes['B']
    with sessions.begin() as db:
        db.get(Identity,reviewer).classroom_ids=all_classes
    experiment=create_experiment(settings,'a',{'id':'live-one','rubricId':rubric.id,'candidateRelease':'variant-v1','classrooms':all_classes,
        'mode':'live','minObservations':2,'maxMaeIncrease':1,'modelRevision':'fixed-v1'},reviewer)
    classes={group:[classroom for classroom in all_classes if experiment['assignments'][classroom]==group] for group in ('A','B')}
    job=SimpleNamespace(id='new',tenant_id='a',classroom_id=classes['B'][0],payload={'grade':7,'intent':'assess','studentRef':'new','experimentConsent':True})
    selected,assignment=choose_rubric(settings,job,rubric)
    assert selected.release=='base-v1' and assignment['group']=='B'
    with sessions.begin() as db:
        for group in ('A','B'):
            for c,classroom in enumerate(classes[group]):
                for i in range(2):
                    identifier=f'{group}-{c}-{i}';scores=dict.fromkeys(DIMENSIONS,0);scores['grammar']=4
                    db.add(Job(id=identifier,tenant_id='a',classroom_id=classroom,owner_id=teacher,idempotency_key=identifier,request_hash='x',
                        payload={'grade':7,'studentRef':identifier,'experimentConsent':True},status='approved',rubric=rubric.model_dump(by_alias=True),
                        result={'total':rubric.weights['grammar'] if group=='A' else rubric.weights['grammar']+2,'experiment':{'id':'live-one','group':group,'mode':'live','status':'active','shadowTotal':rubric.weights['grammar']+2}},
                        review={'scores':scores,'total':rubric.weights['grammar']}))
    result=evaluate_experiment(settings,'a','live-one')
    assert result['decision']=='pause-regressed-variant' and result['automaticGradeRewrite'] is False
    selected,assignment=choose_rubric(settings,job,rubric)
    assert selected.release=='base-v1' and assignment is None
    with sessions() as db:assert db.get(Job,'B-0-0').result['total']==rubric.weights['grammar']+2


def test_study_withdrawal_excludes_metrics_without_rewriting_grade(school,tmp_path):
    from inkwise.database import JobEvent,Identity
    settings,engine,sessions,rubric,teacher,reviewer=school
    variant=rubric.model_copy(update={'release':'variant-v1'})
    path=tmp_path/'variant.json';path.write_text(canonical(variant.model_dump(by_alias=True)))
    import_rubric(settings,'a',path,submitted_by=teacher,approved_by=reviewer,source='Synthetic variant',activate=False)
    create_experiment(settings,'a',{'id':'shadow-withdraw','rubricId':rubric.id,'candidateRelease':'variant-v1','classrooms':['c1'],
        'mode':'shadow','modelRevision':'fixed-v1'},reviewer)
    with sessions.begin() as db:
        db.add(Job(id='j',tenant_id='a',classroom_id='c1',owner_id=teacher,idempotency_key='j',request_hash='x',
            payload={'grade':7,'studentRef':'p1','experimentConsent':True},status='approved',
            result={'total':75,'experiment':{'id':'shadow-withdraw','mode':'shadow','group':'A','status':'active','shadowTotal':75}},
            review={'scores':dict.fromkeys(DIMENSIONS,3)}))
        db.add(JobEvent(job_id='j',tenant_id='a',actor_id=teacher,event='experiment.withdrawn',job_version=1,detail={}))
    report=evaluate_experiment(settings,'a','shadow-withdraw')
    assert report['groups']['A']['observations']==0 and report['decision']=='insufficient-evidence'


def test_reviewed_paired_shadow_can_publish_model_weights_without_changing_school_rubric(school):
    from inkwise.evolution import aggregation_record,apply_model_weights
    settings,engine,sessions,rubric,teacher,reviewer=school;seed_cases(school)
    learning_cycle(settings,'a',datetime(2026,10,12,tzinfo=timezone.utc))
    author=Principal(userId=teacher,tenantId='a',role=Role.TEACHER,classroomIds=['c1','c2'])
    approver=Principal(userId=reviewer,tenantId='a',role=Role.REVIEWER,classroomIds=['c1','c2'])
    item=list_proposals(settings,author)[0];identifier=item['id']
    proposal_action(settings,author,identifier,ProposalAction(version=1,action='submit',note='Synthetic endorsement'))
    proposal_action(settings,approver,identifier,ProposalAction(version=2,action='approve',note='Synthetic independent review'))
    create_experiment(settings,'a',{'id':'paired-shadow','rubricId':rubric.id,'candidateRelease':item['proposedRelease'],
        'classrooms':['c1','c2'],'mode':'shadow','modelRevision':'fixed-v1','minObservations':2,'maxMaeIncrease':0},reviewer)
    variant=RubricRepository(settings.rubric_dir).get('a',rubric.id)
    record=json.loads((settings.data_dir/'evolution'/'a'/'proposals'/(identifier+'.json')).read_text())
    weights_for_shadow=record['proposedRubric']['weights']
    with sessions.begin() as db:
        for i in range(6):
            value=i%3;model=dict.fromkeys(DIMENSIONS,value);model['grammar']=value+1
            teacher_scores={**model,'grammar':value+2}
            control_total=sum(rubric.weights[d]*model[d]/4 for d in DIMENSIONS)
            shadow_total=sum(weights_for_shadow[d]*model[d]/4 for d in DIMENSIONS)
            db.add(Job(id='shadow-'+str(i),tenant_id='a',classroom_id='c1',owner_id=teacher,idempotency_key='shadow-'+str(i),request_hash='x',
                payload={'grade':7,'studentRef':'shadow-student-'+str(i),'experimentConsent':True},status='approved',
                result={'total':control_total,'experiment':{'id':'paired-shadow','group':'A','mode':'shadow','status':'active','shadowTotal':shadow_total}},
                review={'scores':teacher_scores}))
    report=evaluate_experiment(settings,'a','paired-shadow')
    assert report['comparison']=='paired-shadow' and report['decision']=='no-regression-observed'
    published=proposal_action(settings,approver,identifier,ProposalAction(version=3,action='publish',note='Shadow evidence checked',experimentId='paired-shadow'))
    assert published['state']=='published'
    assert RubricRepository(settings.rubric_dir).get('a',rubric.id).release=='base-v1'
    weights=aggregation_record(settings,'a',rubric,'fixed-v1',settings.pipeline_revision)
    assert weights is not None
    report={'total':55,'scores':[{'dimension':d,'band':3 if d=='grammar' else 2} for d in DIMENSIONS]}
    apply_model_weights(settings,'a',rubric,report,weights)
    assert report['total']>55 and report['uncalibratedTotal']==55
    assert aggregation_record(settings,'a',rubric,'different-model',settings.pipeline_revision) is None

    with sessions.begin() as db:
        for i in range(20):
            model=dict.fromkeys(DIMENSIONS,1);model['grammar']=2
            control_total=sum(rubric.weights[d]*model[d]/4 for d in DIMENSIONS)
            shadow_total=sum(weights_for_shadow[d]*model[d]/4 for d in DIMENSIONS)
            db.add(Job(id='drift-'+str(i),tenant_id='a',classroom_id='c1',owner_id=teacher,idempotency_key='drift-'+str(i),request_hash='x',
                payload={'grade':7,'studentRef':'drift-student-'+str(i),'experimentConsent':True},status='approved',
                result={'total':control_total,'experiment':{'id':'paired-shadow','group':'A','mode':'shadow','status':'active','shadowTotal':shadow_total}},
                review={'scores':model}))
    drift=evaluate_experiment(settings,'a','paired-shadow')
    assert drift['decision']=='pause-regressed-variant' and drift['modelAggregationRolledBack']
    assert aggregation_record(settings,'a',rubric,'fixed-v1',settings.pipeline_revision) is None
