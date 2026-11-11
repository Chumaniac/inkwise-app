import copy
import json
from pathlib import Path

import pytest

from inkwise.calibration import fit_profile,isotonic_fit,isotonic_predict,profile_readiness,publish_profile
from inkwise.errors import DomainError
from inkwise.settings import Settings
from inkwise.benchmarks import ocr_metrics,retrieval_metrics,feedback_metrics


def dataset():
    rows=[]
    for i in range(90):
        value=(i%10)*10+5
        rows.append({'id':str(i),'subject':str(i),'split':'train' if i<30 else 'calibration' if i<60 else 'test',
                     'prediction':value,'teacherScores':[value-1,value+1],'rawConfidence':.5+(i%5)/10})
    return {'source':{'provenance':'Original synthetic functional calibration'},'scale':{'maximum':100,'tolerance':5},
            'featureDefinition':'inkwise-observable-v1','referenceOnly':True,'rows':rows}


def test_monotonic_fit_groups_duplicates_and_does_not_use_test_labels():
    knots=isotonic_fit([0,.5,.5,1],[0,1,0,1])
    assert isotonic_predict(knots,[.5])==[.5]
    original=dataset();profile=fit_profile(original)
    changed=copy.deepcopy(original)
    for row in changed['rows']:
        if row['split']=='test':row['teacherScores']=[0,0]
    other=fit_profile(changed)
    assert profile['scoreMapping']==other['scoreMapping'] and profile['confidenceMapping']==other['confidenceMapping']
    assert profile['metrics']['aligned']['mae']==pytest.approx(0,abs=1e-10)
    assert profile_readiness(profile)==[]


def test_leakage_and_reference_profile_cannot_enter_runtime(tmp_path):
    document=dataset();document['rows'][-1]['subject']=document['rows'][0]['subject']
    with pytest.raises(DomainError,match='ALIGNMENT_SUBJECT_LEAKAGE'):fit_profile(document)
    profile=fit_profile(dataset());path=tmp_path/'profile.json';path.write_text(json.dumps(profile))
    with pytest.raises(DomainError,match='CALIBRATION_SCOPE_INVALID'):
        publish_profile(Settings(),'school',path,'invented-reviewer')


def test_content_free_ocr_and_retrieval_metrics_have_known_answers():
    result=ocr_metrics([{'reference':'cat','prediction':'cut'}])
    assert result['cer']==pytest.approx(1/3) and result['wer']==1
    assert 'cat' not in json.dumps(result)
    result=retrieval_metrics([{'relevance':{'a':3,'b':1},'retrieved':['x','a'],'mandatory':['R1'],'appliedRules':['R1']}],2)
    assert result['recallAtK']==.5 and result['mrr']==.5 and result['mandatoryRuleCoverage']==1
    assert feedback_metrics([{'usefulness':4,'correct':True}])['meanUsefulness']==4


def test_scoped_profile_publication_application_and_model_change(tmp_path):
    from inkwise.calibration import apply_calibration
    from inkwise.cli import seed_demo,provision
    from inkwise.database import initialize_database
    from inkwise.rubrics import RubricRepository,canonical,digest
    settings=Settings(database_url=f'sqlite:///{tmp_path}/db.sqlite',rubric_dir=tmp_path/'rules',
        calibration_dir=tmp_path/'calibration',model_revision='synthetic-v1')
    initialize_database(settings.database_url);seed_demo(settings,'a')
    reviewer=provision(settings,'a',['c'],'reviewer',tmp_path/'reviewer-access')
    rubric=RubricRepository(settings.rubric_dir).get('a','english-demo')
    document=dataset();document['referenceOnly']=False
    document['binding']={'tenantId':'a','rubricHash':digest(canonical(rubric.model_dump(by_alias=True))),
        'modelRevision':'synthetic-v1','pipelineRevision':settings.pipeline_revision,'language':'en','gradeMin':5,'gradeMax':12}
    profile=fit_profile(document);path=tmp_path/'profile.json';path.write_text(json.dumps(profile))
    publish_profile(settings,'a',path,reviewer)
    scores=[{'dimension':key,'band':3} for key in rubric.weights]
    report={'initialScores':scores,'scores':scores,'total':75,'warnings':[]}
    result=apply_calibration(settings,'a',rubric,report,7)
    assert result['status']=='calibrated-reference' and result['confidence']==1
    settings.model_revision='other-model'
    assert apply_calibration(settings,'a',rubric,report,7)['confidence'] is None
