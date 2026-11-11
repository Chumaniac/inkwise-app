from types import SimpleNamespace

import pytest

from inkwise.errors import DomainError
from inkwise.orchestration import resolve_intent,duplicate_feedback_groups,explain_report


@pytest.mark.parametrize('operator_text,expected',[('只校对，不要打分','proofread'),('Please explain this grade','explain'),('解释这份报告','explain'),('评分并解释','assess')])
def test_intent_uses_teacher_control_only(operator_text,expected):
    assert resolve_intent('auto',operator_text)==expected
    assert resolve_intent('assess',operator_text)=='assess'


def test_unknown_intent_and_duplicate_feedback_are_visible():
    with pytest.raises(DomainError):resolve_intent('auto','some unrelated command')
    scores=[{'dimension':key,'suggestion':'Check the tense','evidence':[{'start':0,'end':5}]} for key in ('grammar','vocabulary')]
    assert duplicate_feedback_groups(scores)[0]['dimensions']==['grammar','vocabulary']


def test_explanation_never_represents_excluded_model_feedback_as_final():
    job=SimpleNamespace(version=2,result={'scores':[{'dimension':'grammar','band':1,'reason':'PRIVATE_MODEL_DRAFT'}]},
        review={'scores':{'grammar':3},'feedback':{}},rubric={'release':'v1','rules':[{'id':'r','dimension':'grammar','levels':['0','1','2','3','4']}]})
    explanation=explain_report(job)
    assert explanation['generatedWithoutModelCall'] and explanation['dimensions'][0]['band']==3
    assert explanation['dimensions'][0]['reason']==''


def test_feedback_pool_requires_consented_cases_and_exact_class(tmp_path):
    import json
    from inkwise.database import Base,Job,Candidate,create_session_factory
    from inkwise.orchestration import teacher_feedback_pool
    from inkwise.core import DIMENSIONS
    engine,sessions=create_session_factory(f'sqlite:///{tmp_path}/db.sqlite');Base.metadata.create_all(engine)
    rubric={'id':'r','release':'v1'};target=SimpleNamespace(id='current',tenant_id='a',classroom_id='c',rubric=rubric,payload={'grade':7})
    with sessions.begin() as db:
        for i in range(12):
            tenant='b' if i>=9 else 'a';classroom='other' if 6<=i<9 else 'c'
            db.add(Job(id=str(i),tenant_id=tenant,classroom_id=classroom,owner_id='t',idempotency_key=str(i),request_hash='x',
                payload={'grade':7,'text':'PRIVATE_SENTINEL'},rubric=rubric,status='approved',
                result={'scores':[{'dimension':key,'band':1} for key in DIMENSIONS]},
                review={'scores':dict.fromkeys(DIMENSIONS,3),'comment':'PRIVATE_SENTINEL'}))
            if i>=3:
                db.add(Candidate(id='case'+str(i),tenant_id=tenant,classroom_id=classroom,job_id=str(i),report_version=1,
                    submitted_by='t',reviewed_by='r',status='approved',text='PRIVATE_SENTINEL',provenance='synthetic',rubric=rubric,
                    review={'scores':dict.fromkeys(DIMENSIONS,3),'reuseScope':'explicit-anonymized-v1'}))
    result=teacher_feedback_pool(sessions,target)
    assert all(row['reviewCount']==3 and row['meanTeacherDelta']==2 for row in result)
    assert len(result)==5 and 'PRIVATE_SENTINEL' not in json.dumps(result)
    engine.dispose()
