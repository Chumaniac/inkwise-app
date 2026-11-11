"""Held-out teacher alignment and isotonic confidence, with strict deployment scope."""

import json
import math
from pathlib import Path

import numpy as np
from sqlalchemy import select

from inkwise.errors import DomainError
from inkwise.evaluation import qwk
from inkwise.cache_storage import atomic_json
from inkwise.knowledge import read_bundle_text
from inkwise.rubrics import canonical, digest, safe_segment


def finite(value, low=0, high=1):
    return type(value) in (int,float) and math.isfinite(value) and low<=value<=high


def isotonic_fit(x,y):
    """Pool adjacent violators; duplicate inputs have one weighted fitted value."""
    if not 2<=len(x)==len(y)<=20000 or any(not finite(v) for v in [*x,*y]):
        raise DomainError('CALIBRATION_DATA_INVALID')
    grouped={}
    for a,b in zip(x,y):
        total,count=grouped.get(a,(0,0));grouped[a]=(total+b,count+1)
    blocks=[]
    for value,(total,count) in sorted(grouped.items()):
        blocks.append(([value],total,count))
        while len(blocks)>1 and blocks[-2][1]/blocks[-2][2]>blocks[-1][1]/blocks[-1][2]:
            right,left=blocks.pop(),blocks.pop()
            left[0].extend(right[0])
            blocks.append((left[0],left[1]+right[1],left[2]+right[2]))
    return [[value,total/count] for values,total,count in blocks for value in values]


def isotonic_predict(knots,values):
    if not knots or any(len(row)!=2 or any(not finite(v) for v in row) for row in knots):
        raise DomainError('CALIBRATION_PROFILE_INVALID')
    if any(knots[i][0]>=knots[i+1][0] or knots[i][1]>knots[i+1][1] for i in range(len(knots)-1)):
        raise DomainError('CALIBRATION_PROFILE_INVALID')
    if any(not finite(value) for value in values):raise DomainError('CALIBRATION_DATA_INVALID')
    return np.interp(values,[row[0] for row in knots],[row[1] for row in knots]).tolist()


def reliability(probabilities, outcomes, bins=10):
    if not probabilities or len(probabilities)!=len(outcomes) or not 2<=bins<=20:
        raise DomainError('CALIBRATION_DATA_INVALID')
    if any(not finite(p) for p in probabilities) or any(v not in (0,1) for v in outcomes):
        raise DomainError('CALIBRATION_DATA_INVALID')
    groups=[[] for _ in range(bins)]
    for probability,outcome in zip(probabilities,outcomes):groups[min(int(probability*bins),bins-1)].append((probability,outcome))
    curve=[];ece=0
    for index,group in enumerate(groups):
        if not group:continue
        predicted=sum(p for p,_ in group)/len(group);observed=sum(y for _,y in group)/len(group)
        ece+=len(group)/len(probabilities)*abs(predicted-observed)
        curve.append({'lower':index/bins,'upper':(index+1)/bins,'count':len(group),'predicted':predicted,'observed':observed})
    return {'count':len(probabilities),'brier':sum((p-y)**2 for p,y in zip(probabilities,outcomes))/len(outcomes),'ece':ece,'curve':curve}


def validate_dataset(document):
    rows=document.get('rows',[]);scale=document.get('scale',{})
    if not 6<=len(rows)<=20000 or not finite(scale.get('maximum'),1,1000) or not finite(scale.get('tolerance'),0,scale['maximum']):
        raise DomainError('ALIGNMENT_DATA_INVALID')
    if not document.get('source',{}).get('provenance') or not document.get('featureDefinition'):
        raise DomainError('ALIGNMENT_PROVENANCE_REQUIRED')
    subjects={};ids=set();splits={'train':[],'calibration':[],'test':[]}
    for row in rows:
        if (not isinstance(row.get('id'),str) or row['id'] in ids or not row.get('subject')
            or row.get('split') not in splits or not finite(row.get('prediction'),0,scale['maximum'])
            or not finite(row.get('rawConfidence')) or not isinstance(row.get('teacherScores'),list)
            or not 2<=len(row['teacherScores'])<=10 or any(not finite(v,0,scale['maximum']) for v in row['teacherScores'])):
            raise DomainError('ALIGNMENT_DATA_INVALID')
        ids.add(row['id']);splits[row['split']].append(row)
        subjects.setdefault(row['subject'],set()).add(row['split'])
    if any(len(value)>1 for value in subjects.values()):raise DomainError('ALIGNMENT_SUBJECT_LEAKAGE')
    if any(len({r['subject'] for r in group})<2 for group in splits.values()):raise DomainError('ALIGNMENT_SPLITS_REQUIRED')
    return splits,scale


def score_metrics(expected,predicted,maximum):
    n=len(expected)
    bands=lambda values:[min(4,max(0,math.floor(value/maximum*4+.5))) for value in values]
    return {'count':n,'mae':sum(abs(a-b) for a,b in zip(expected,predicted))/n,
        'meanBias':sum(b-a for a,b in zip(expected,predicted))/n,'qwkFiveBands':qwk(bands(expected),bands(predicted)),
        'qwkMapping':'nearest of five equally spaced bands, half up','maximum':maximum}


def fit_profile(document):
    splits,scale=validate_dataset(document);maximum=scale['maximum'];tolerance=scale['tolerance']
    teacher=lambda row:sum(row['teacherScores'])/len(row['teacherScores'])
    train=splits['train'];cal=splits['calibration'];test=splits['test']
    mapping=isotonic_fit([r['prediction']/maximum for r in train],[teacher(r)/maximum for r in train])
    calibrated=[x*maximum for x in isotonic_predict(mapping,[r['prediction']/maximum for r in cal])]
    outcomes=[int(abs(p-teacher(r))<=tolerance) for p,r in zip(calibrated,cal)]
    confidence=isotonic_fit([r['rawConfidence'] for r in cal],outcomes)
    predicted=[x*maximum for x in isotonic_predict(mapping,[r['prediction']/maximum for r in test])]
    probabilities=isotonic_predict(confidence,[r['rawConfidence'] for r in test])
    expected=[teacher(r) for r in test]
    success=[int(abs(a-b)<=tolerance) for a,b in zip(expected,predicted)]
    pairwise=[]
    if len({len(r['teacherScores']) for r in test})==1:
        for first in range(len(test[0]['teacherScores'])):
            for second in range(first+1,len(test[0]['teacherScores'])):
                pairwise.append(score_metrics([r['teacherScores'][first] for r in test],[r['teacherScores'][second] for r in test],maximum))
    profile={'schemaVersion':1,'state':'evaluated-not-approved','source':document['source'],'binding':document.get('binding',{}),
        'featureDefinition':document['featureDefinition'],'scale':scale,'inputHash':digest(canonical(document)),
        'scoreMapping':mapping,'confidenceMapping':confidence,'splitCounts':{k:len(v) for k,v in splits.items()},
        'metrics':{'raw':score_metrics(expected,[r['prediction'] for r in test],maximum),
            'aligned':score_metrics(expected,predicted,maximum),'confidence':reliability(probabilities,success),
            'teacherPairs':pairwise},'referenceOnly':document.get('referenceOnly',True)}
    return profile


def profile_readiness(profile):
    reasons=[]
    counts=profile.get('splitCounts',{})
    if min(counts.get('calibration',0),counts.get('test',0))<20:reasons.append('INSUFFICIENT_HELD_OUT_SUPPORT')
    metrics=profile.get('metrics',{});raw=metrics.get('raw',{});aligned=metrics.get('aligned',{})
    if not finite(raw.get('mae'),0,1000) or not finite(aligned.get('mae'),0,1000):reasons.append('METRICS_INVALID')
    elif aligned['mae']>raw['mae']+1e-9:reasons.append('MAE_REGRESSED')
    before,after=raw.get('qwkFiveBands'),aligned.get('qwkFiveBands')
    if before is None or after is None:reasons.append('ORDINAL_AGREEMENT_UNDEFINED')
    elif not finite(before,-1,1) or not finite(after,-1,1) or after<before-1e-9:reasons.append('ORDINAL_AGREEMENT_REGRESSED')
    return reasons


def publish_profile(settings,tenant,path,reviewer_id):
    from inkwise.database import Identity,create_session_factory
    safe_segment(tenant)
    profile=json.loads(read_bundle_text(path));binding=profile.get('binding',{})
    if (profile.get('referenceOnly') is not False or binding.get('tenantId')!=tenant
        or not binding.get('rubricHash') or not binding.get('modelRevision') or binding.get('pipelineRevision')!=settings.pipeline_revision
        or profile.get('featureDefinition')!='inkwise-observable-v1'
        or profile.get('scale',{}).get('maximum')!=100
        or profile.get('state')!='evaluated-not-approved'):
        raise DomainError('CALIBRATION_SCOPE_INVALID')
    if profile_readiness(profile):raise DomainError('CALIBRATION_EVALUATION_GATE_FAILED')
    isotonic_predict(profile.get('scoreMapping'),[0]);isotonic_predict(profile.get('confidenceMapping'),[0])
    engine,sessions=create_session_factory(settings.database_url)
    try:
        with sessions() as db:
            reviewer=db.get(Identity,reviewer_id)
            if not reviewer or not reviewer.active or reviewer.tenant_id!=tenant or reviewer.role not in ('reviewer','admin'):
                raise DomainError('CALIBRATION_REVIEWER_REQUIRED')
    finally:engine.dispose()
    # Publication records operator-supplied school review, never invents a reviewer.
    atomic_json(settings.calibration_dir/tenant/'active.json',{'profile':profile,
        'approval':{'profileHash':digest(canonical(profile)),'reviewerId':reviewer_id}})
    return profile['metrics']


def apply_calibration(settings,tenant,rubric,report,grade):
    path=settings.calibration_dir/safe_segment(tenant)/'active.json'
    if not path.exists():return {'status':'not-calibrated','confidence':None}
    try:
        document=json.loads(read_bundle_text(path));profile=document['profile'];binding=profile['binding']
        if (document['approval']['profileHash']!=digest(canonical(profile)) or profile.get('referenceOnly') is not False
            or profile.get('featureDefinition')!='inkwise-observable-v1' or binding.get('tenantId')!=tenant
            or binding.get('rubricHash')!=digest(canonical(rubric.model_dump(by_alias=True)))
            or not settings.model_revision or binding.get('modelRevision')!=settings.model_revision or binding.get('pipelineRevision')!=settings.pipeline_revision
            or binding.get('aggregationProfileHash')!=report.get('aggregation',{}).get('profileHash')
            or binding.get('language')!=rubric.language_policy or not binding.get('gradeMin',99)<=grade<=binding.get('gradeMax',0)):
            return {'status':'scope-mismatch','confidence':None}
        initial={row['dimension']:row['band'] for row in report['initialScores']}
        raw=max(0,1-sum(abs(row['band']-initial[row['dimension']]) for row in report['scores'])/20
                -min(len(report.get('warnings',[]))*.05,.3))
        probability=isotonic_predict(profile['confidenceMapping'],[raw])[0]
        total=isotonic_predict(profile['scoreMapping'],[report['total']/100])[0]*100
        return {'status':'calibrated-reference','confidence':probability,'alignedTotal':round(total,2),
            'tolerance':profile['scale']['tolerance'],'profileHash':document['approval']['profileHash'],
            'reviewTier':'confirm' if probability>=.9 else 'standard' if probability>=.75 else 'intensive',
            'note':'校准参考不替代教师最终评分，也不自动发布成绩。'}
    except (ValueError,KeyError,TypeError,DomainError):
        return {'status':'invalid-profile','confidence':None}
