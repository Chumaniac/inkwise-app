import pytest
from pydantic import ValidationError

from inkwise.contracts import Review
from inkwise.core import DIMENSIONS


def test_model_feedback_is_not_implicitly_teacher_approved():
    review=Review(version=1,scores=dict.fromkeys(DIMENSIONS,3),comment='Teacher final comment')
    assert review.feedback=={}
    with pytest.raises(ValidationError):
        Review(version=1,scores=dict.fromkeys(DIMENSIONS,3),comment='Teacher final comment',
               feedback={'coherence':{'include':True,'reason':'Unconfirmed model claim'}})


def test_teacher_can_publish_edited_feedback_and_exclude_a_dimension():
    review=Review(version=1,scores=dict.fromkeys(DIMENSIONS,3),comment='Teacher final comment',feedbackConfirmed=True,
        feedback={'coherence':{'include':True,'reason':'One paragraph with clear event order.','suggestion':'Add a transition.'},
                  'vocabulary':{'include':False}})
    assert review.feedback['coherence'].reason.startswith('One paragraph')
    assert not review.feedback['vocabulary'].include


def test_candidate_does_not_silently_reuse_private_original_comments(tmp_path):
    import json
    from inkwise.cli import seed_demo
    from inkwise.contracts import CandidateCreate, Principal, Role
    from inkwise.database import Base, Job, create_session_factory
    from inkwise.service import create_candidate
    from inkwise.settings import Settings
    from inkwise.rubrics import RubricRepository
    settings=Settings(database_url=f'sqlite:///{tmp_path}/db.sqlite',rubric_dir=tmp_path/'rules')
    engine,sessions=create_session_factory(settings.database_url);Base.metadata.create_all(engine);seed_demo(settings,'a')
    rubric=RubricRepository(settings.rubric_dir).get('a','english-demo').model_dump(by_alias=True)
    with sessions.begin() as db:
        db.add(Job(id='j',tenant_id='a',classroom_id='c',owner_id='t',idempotency_key='i',request_hash='h',
            payload={'grade':7},status='approved',version=1,rubric=rubric,
            review={'scores':dict.fromkeys(DIMENSIONS,3),'comment':'PRIVATE_ORIGINAL_COMMENT',
                'feedback':{'content':{'include':True,'reason':'PRIVATE_ORIGINAL_REASON','evidence':[]}}}))
    with sessions() as db:
        candidate=create_candidate(db,Principal(userId='t',tenantId='a',role=Role.TEACHER,classroomIds=['c']),'j',
            CandidateCreate(version=1,anonymizedText='Anonymous synthetic text',reuseAuthorized=True,provenance='Original synthetic'))
        assert 'PRIVATE_ORIGINAL' not in json.dumps(candidate.review)
        assert candidate.review['scores']==dict.fromkeys(DIMENSIONS,3)
    engine.dispose()
