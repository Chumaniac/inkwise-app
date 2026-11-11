"""Bounded teacher intents, scoped feedback summaries and explainable arbitration."""
import re
from pathlib import Path

from sqlalchemy import select

from inkwise.core import DIMENSIONS
from inkwise.database import Job,Candidate
from inkwise.errors import DomainError
from inkwise.rubrics import canonical,digest


def resolve_intent(explicit='assess',request=''):
    if explicit in ('assess','proofread','explain'):return explicit
    if explicit!='auto' or not request.strip():raise DomainError('INTENT_UNCLEAR',422)
    text=request.lower()
    no_grade=bool(re.search(r'不(?:要|用|必|需要)?(?:评分|打分)|(?:do not|without|no)\s+(?:score|scoring|grade|grading)',text))
    if no_grade:text=re.sub(r'评分|打分|scor(?:e|ing)|grad(?:e|ing)','',text)
    grade=bool(re.search(r'评分|打分|批改|assess|scor(?:e|ing)|grad(?:e|ing)',text))
    proof=bool(re.search(r'校对|纠错|拼写|proofread|spelling|grammar|copyedit',text))
    explain=bool(re.search(r'解释|说明.*(?:分数|评分)|explain|why',text))
    if explain:
        regrade=bool(re.search(r'重新(?:评分|打分)|(?:评分|打分|批改)[，, ]*(?:并|再|然后)|(?:score|grade|assess)\s+(?:(?:this|my|the)\s+)?(?:essay|text|paper)|(?:score|grade|assess)\s+and\s+explain',text))
        if regrade and not no_grade:return 'assess'
        if proof:raise DomainError('INTENT_UNCLEAR',422)
        return 'explain'
    if grade and not no_grade:return 'assess'
    if proof:return 'proofread'
    raise DomainError('INTENT_UNCLEAR',422)


def plan_for(intent):
    return {'assess':['confirm-text','retrieve','score-five-dimensions','reflect','revise','arbitrate','teacher-review'],
            'proofread':['confirm-text','proofread','validate-quotes','teacher-review'],
            'explain':['authorize-report','read-frozen-rules','explain-published-feedback']}[intent]


def teacher_feedback_pool(sessions,job):
    """Only same-class, same-grade and exact-rule statistics; never copy old prose."""
    expected=digest(canonical(job.rubric));by_dimension={key:[] for key in DIMENSIONS}
    with sessions() as db:
        rows=db.execute(select(Candidate.review,Job.result,Candidate.rubric,Job.payload["grade"].as_integer())
            .join(Job,Job.id==Candidate.job_id).where(Candidate.tenant_id==job.tenant_id,
            Candidate.classroom_id==job.classroom_id,Job.tenant_id==job.tenant_id,
            Job.classroom_id==job.classroom_id,Job.id!=job.id,Candidate.status.in_(['approved','published']),
            Candidate.reviewed_by.is_not(None),Candidate.reviewed_by!=Candidate.submitted_by)
            .order_by(Candidate.created_at.desc()).limit(100)).all()
        for review,result,rubric,grade in rows:
            if not review or review.get('reuseScope')!='explicit-anonymized-v1' or not result or not rubric or digest(canonical(rubric))!=expected or grade!=job.payload.get('grade'):continue
            initial={row['dimension']:row['band'] for row in result.get('scores',[])}
            for dimension in DIMENSIONS:
                if dimension in initial and dimension in review.get('scores',{}):
                    by_dimension[dimension].append(review['scores'][dimension]-initial[dimension])
    return [{'dimension':key,'reviewCount':len(values),'meanTeacherDelta':round(sum(values)/len(values),3)}
            for key,values in by_dimension.items() if len(values)>=3]


def duplicate_feedback_groups(scores):
    groups={}
    for score in scores:
        suggestion=re.sub(r'\s+','',score.get('suggestion','').casefold())
        if not suggestion:continue
        for evidence in score.get('evidence',[]):
            key=(evidence['start'],evidence['end'],suggestion)
            groups.setdefault(key,[]).append(score['dimension'])
    return [{'start':key[0],'end':key[1],'dimensions':sorted(set(dimensions)),'action':'teacher-resolve-once'}
            for key,dimensions in groups.items() if len(set(dimensions))>1]


def explain_report(job):
    if not job.result:raise DomainError('REPORT_NOT_READY',409)
    if job.result.get('kind')=='feedback':
        return {'kind':'explanation','sourceVersion':job.version,'status':'teacher-confirmed' if job.review else 'draft',
                'dimensions':[],'message':'本任务仅校对，不产生分数。','generatedWithoutModelCall':True}
    final=job.review
    dimensions=[]
    for score in job.result.get('scores',[]):
        key=score['dimension'];band=final['scores'][key] if final else score['band']
        feedback=final.get('feedback',{}).get(key,{}) if final else score
        rules=[rule for rule in job.rubric['rules'] if rule['dimension']==key]
        dimensions.append({'dimension':key,'band':band,'ruleIds':[rule['id'] for rule in rules],
            'descriptors':[rule['levels'][band] for rule in rules],
            'reason':feedback.get('reason','') if feedback.get('include',True) else '',
            'evidence':feedback.get('evidence',[]) if feedback.get('include',True) else []})
    return {'kind':'explanation','sourceVersion':job.version,'status':'teacher-confirmed' if final else 'draft',
            'rubricRelease':job.rubric['release'],'dimensions':dimensions,'generatedWithoutModelCall':True}


async def proofread_job(gateway,job,rubric,budget,findings=None):
    from inkwise.chunks import ChunkFeedback
    from inkwise.providers import validate_evidence
    if findings is None:
        response=await gateway.complete(ChunkFeedback,'仅检查词汇、拼写、语法和标点，不评分、不代写。每条建议必须引用确认原文。',
            {'essayContext':job.text,'coreStart':0,'coreEnd':len(job.text),
             'rules':[rule.model_dump(by_alias=True) for rule in rubric.rules],
             'dimension':'proofread','languagePolicy':rubric.language_policy,'languageRouting':getattr(gateway,'language_context',{})},budget)
        checked=[]
        for finding in response.findings:
            if finding.dimension not in ('grammar','vocabulary'):raise DomainError('PROOFREAD_DIMENSION_INVALID')
            finding.evidence=validate_evidence(finding.evidence,job.text,rubric,finding.dimension)
            from inkwise.language import evidence_protected
            if evidence_protected(finding.evidence,getattr(gateway,'language_context',{})):continue
            checked.append(finding.model_dump(by_alias=True))
    else:checked=[row for row in findings if row['dimension'] in ('grammar','vocabulary')]
    return {'kind':'feedback','intent':'proofread','scores':[],'total':None,'findings':checked[:12],
        'additionalFindings':max(0,len(checked)-12),'warnings':[],'requiresTeacherReview':True,'confidence':None,
        'model':gateway.settings.model_name,'textHash':job.text_hash,'rubricId':rubric.id,'release':rubric.release}
