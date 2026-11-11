"""Local periodic learning proposals and explicitly approved, class-level weight trials."""
import hashlib
import hmac
import json
import math
from contextlib import contextmanager
from datetime import datetime,timezone,timedelta
from pathlib import Path

from pydantic import Field,model_validator
from sqlalchemy import select

from inkwise.contracts import Contract,Principal,Role,Rubric
from inkwise.core import DIMENSIONS
from inkwise.database import Candidate,Identity,Job,JobEvent,create_session_factory
from inkwise.errors import DomainError
from inkwise.evaluation import cohort,propose_weights
from inkwise.cache_storage import atomic_json
from inkwise.knowledge import compatible_anchor, read_bundle_text
from inkwise.rubrics import RubricRepository,canonical,digest,safe_segment


class Experiment(Contract):
    id: str = Field(min_length=1,max_length=60,pattern=r'^[a-zA-Z0-9][a-zA-Z0-9_-]*$')
    rubric_id: str
    grade: int = Field(default=7,ge=1,le=12)
    candidate_release: str
    classrooms: list[str] = Field(min_length=1,max_length=100)
    mode: str = Field(default='shadow',pattern=r'^(shadow|live)$')
    min_observations: int = Field(default=20,ge=2,le=1000)
    max_mae_increase: float = Field(default=2,ge=0,le=20,allow_inf_nan=False)
    enabled: bool = True
    model_revision: str = Field(min_length=1,max_length=200)
    pipeline_revision: str = Field(default="inkwise-0.4-v1",min_length=1,max_length=100)
    duration_days: int = Field(default=14,ge=1,le=90)

    @model_validator(mode='after')
    def unique_classes(self):
        if len(set(self.classrooms))!=len(self.classrooms):raise ValueError('Duplicate classroom')
        if self.mode=='live' and len(self.classrooms)<4:raise ValueError('Live class trials require at least four classrooms')
        return self


def root(settings,tenant):return settings.data_dir/'evolution'/safe_segment(tenant)


@contextmanager
def tenant_lock(settings,tenant):
    import fcntl
    folder=root(settings,tenant);folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (folder/'.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        yield folder


def authorize_reviewer(settings,tenant,actor,classes):
    engine,sessions=create_session_factory(settings.database_url)
    try:
        with sessions() as db:
            identity=db.get(Identity,actor)
            if (not identity or not identity.active or identity.tenant_id!=tenant or identity.role not in ('reviewer','admin')
                or (identity.role!='admin' and not set(classes)<=set(identity.classroom_ids))):
                raise DomainError('EVOLUTION_REVIEWER_REQUIRED',403)
    finally:engine.dispose()


def require_salt(settings):
    value=settings.evaluation_salt.get_secret_value()
    if len(value)<32:raise DomainError('EVALUATION_SALT_REQUIRED')
    return value


def subject_key(settings,tenant,reference):
    return hmac.new(require_salt(settings).encode(),(tenant+':'+reference).encode(),hashlib.sha256).hexdigest()


def list_proposals(settings,principal):
    folder=root(settings,principal.tenant_id);index=folder/'proposal-index.json'
    if not index.exists():return []
    items=[]
    for identifier in json.loads(read_bundle_text(index)).get('ids',[])[:10]:
        path=folder/'proposals'/(safe_segment(identifier)+'.json')
        if not path.exists():continue
        record=json.loads(read_bundle_text(path))
        if principal.role!=Role.ADMIN and not set(record['classrooms'])<=set(principal.classroom_ids):continue
        # Responses contain aggregates and opaque provenance, never example text.
        record['sourceValid']=source_still_authorized(settings,principal.tenant_id,record)
        record['hasProposedRubric']='proposedRubric' in record
        record['proposedRelease']=record.get('proposedRubric',{}).get('release')
        if 'proposedRubric' in record:record['proposedWeights']=record['proposedRubric']['weights']
        record.pop('proposedRubric',None);record.pop('sources',None)
        items.append(record)
    return items


def source_still_authorized(settings,tenant,record):
    engine,sessions=create_session_factory(settings.database_url)
    try:
        with sessions() as db:
            sources=record['sources'];found={}
            for start in range(0,len(sources),400):
                ids=[source['id'] for source in sources[start:start+400]]
                for identifier,version,status in db.execute(select(Candidate.id,Candidate.version,Candidate.status).where(Candidate.tenant_id==tenant,Candidate.id.in_(ids))):
                    found[identifier]=(version,status)
            if any(source['id'] not in found or found[source['id']][0]!=source['version'] or found[source['id']][1] not in ('approved','published') for source in sources):return False
        return True
    finally:engine.dispose()


def aggregation_record(settings,tenant,rubric,model_revision,pipeline_revision):
    path=root(settings,tenant)/'model-weights'/(safe_segment(rubric.id)+'.json')
    if not path.exists():return None
    record=json.loads(read_bundle_text(path))
    if not aggregation_valid(settings,tenant,rubric,model_revision,pipeline_revision,record):return None
    return record


def aggregation_valid(settings,tenant,rubric,model_revision,pipeline_revision,record):
    if (record['baselineHash']!=digest(canonical(rubric.model_dump(by_alias=True)))
        or record['modelRevision']!=model_revision or record['pipelineRevision']!=pipeline_revision):return False
    proposal=root(settings,tenant)/'proposals'/(safe_segment(record['proposalId'])+'.json')
    return proposal.exists() and source_still_authorized(settings,tenant,json.loads(read_bundle_text(proposal)))


def apply_model_weights(settings,tenant,rubric,report,record):
    variant=load_variant(settings,tenant,record['variant'])
    value=round(sum(variant.weights[row['dimension']]*row['band']/4 for row in report['scores']),2)
    report['uncalibratedTotal']=report['total'];report['total']=value
    report['aggregation']={'profileHash':record['variant']['sha256'],'weights':variant.weights,
        'use':'model-only calibration; teacher final grades retain normative rubric weights'}


def learning_cycle(settings,tenant,clock=None):
    """A period can create proposals, never publish a rubric or change existing grades."""
    require_salt(settings);clock=clock or datetime.now(timezone.utc)
    period=int(clock.timestamp())//settings.evolution_interval_seconds
    engine,sessions=create_session_factory(settings.database_url);groups={}
    try:
        with sessions() as db:
            columns=[Candidate.id,Candidate.version,Candidate.classroom_id,Candidate.rubric,Candidate.review['scores'],
                Job.payload['grade'].as_integer(),Job.payload['studentRef'].as_string(),Job.result['model'].as_string(),
                Job.result['modelRevision'].as_string(),Job.result['pipelineRevision'].as_string()]
            for i in range(5):columns.extend([Job.result['scores'][i]['dimension'].as_string(),Job.result['scores'][i]['band'].as_integer()])
            rows=db.execute(select(*columns).join(Job,Job.id==Candidate.job_id).where(
                Candidate.tenant_id==tenant,Job.tenant_id==tenant,Candidate.classroom_id==Job.classroom_id,
                Candidate.status.in_(['approved','published']),Candidate.review['reuseScope'].as_string()=='explicit-anonymized-v1',
                Candidate.reviewed_by.is_not(None),Candidate.reviewed_by!=Candidate.submitted_by)
                .order_by(Candidate.created_at.desc()).limit(settings.evolution_max_cases).execution_options(yield_per=25))
            for values in rows:
                identifier,version,classroom,rubric_raw,teacher,grade,student,model,model_revision,pipeline_revision,*parts=values
                rubric=Rubric.model_validate(rubric_raw);key=digest(canonical(rubric_raw))+':'+str(grade)+':'+str(model_revision)+':'+str(pipeline_revision)
                if key not in groups:
                    if len(groups)>=100:raise DomainError('EVOLUTION_SCOPE_TOO_LARGE')
                    groups[key]={'rubric':rubric,'grade':grade,'model':model,'modelRevision':model_revision,'pipelineRevision':pipeline_revision,'rows':[],'classes':set(),'invalid':0,'missingSubjects':0}
                group=groups[key];group['classes'].add(classroom)
                bands=dict(zip(parts[::2],parts[1::2]))
                if set(bands)!=set(DIMENSIONS) or set(teacher or {})!=set(DIMENSIONS) or any(type(x) is not int or not 0<=x<=4 for x in [*bands.values(),*teacher.values()]):
                    group['invalid']+=1;continue
                row={'id':identifier,'version':version,'features':[bands[d]/4 for d in DIMENSIONS],
                    'label':sum(rubric.weights[d]*teacher[d]/4 for d in DIMENSIONS)/100}
                if student:row['subject']=subject_key(settings,tenant,student)
                else:group['missingSubjects']+=1
                group['rows'].append(row)
        results=[]
        with tenant_lock(settings,tenant) as folder:
            index_path=folder/'proposal-index.json'
            index=json.loads(read_bundle_text(index_path)) if index_path.exists() else {'ids':[],'total':0}
            for group in groups.values():
                rows=group['rows'];rubric=group['rubric']
                sources=[{'id':row['id'],'version':row['version']} for row in rows]
                aggregation=aggregation_record(settings,tenant,rubric,group['modelRevision'],group['pipelineRevision'])
                fingerprint=digest(canonical({'aggregation':aggregation,'sources':sources,'rubric':rubric.model_dump(by_alias=True),'model':group['model'],'modelRevision':group['modelRevision'],'pipelineRevision':group['pipelineRevision'],'grade':group['grade']}))
                identifier=f'{period}-{fingerprint[:20]}'
                path=folder/'proposals'/(identifier+'.json')
                if path.exists():results.append(identifier);continue
                if index['total']>=2000:raise DomainError('EVOLUTION_ARCHIVE_REQUIRED')
                reasons=[]
                if aggregation:
                    previous=json.loads(read_bundle_text(root(settings,tenant)/'proposals'/(aggregation['proposalId']+'.json')))
                    if previous.get('sources')==sources:reasons.append('NO_NEW_APPROVED_FEEDBACK')
                if not group['modelRevision'] or not group['pipelineRevision']:reasons.append('PINNED_MODEL_AND_PIPELINE_REQUIRED')
                current_rule=RubricRepository(settings.rubric_dir).get(tenant,rubric.id)
                baseline_hash=digest(canonical(rubric.model_dump(by_alias=True)))
                if digest(canonical(current_rule.model_dump(by_alias=True)))!=baseline_hash:reasons.append('BASELINE_NOT_CURRENT')
                if len(rows)<settings.evolution_min_cases:reasons.append('MORE_APPROVED_CASES_REQUIRED')
                if group['missingSubjects']:reasons.append('STUDENT_GROUP_KEYS_REQUIRED')
                if group['invalid']:reasons.append('INVALID_SOURCE_SCORES')
                if not rubric.weight_learning_allowed:reasons.append('WEIGHTS_FIXED_BY_POLICY')
                record={'id':identifier,'tenantId':tenant,'classrooms':sorted(group['classes']),'period':period,
                    'state':'proposal-not-active','version':1,'rubricId':rubric.id,'rubricRelease':rubric.release,'model':group['model'],
                    'grade':group['grade'],'modelRevision':group['modelRevision'],'pipelineRevision':group['pipelineRevision'],
                    'baselineAggregationHash':aggregation['variant']['sha256'] if aggregation else None,'baselineHash':baseline_hash,'cases':len(rows),'sourceHash':fingerprint,'sources':sources,'reasons':reasons,
                    'labelType':'single-teacher-confirmed scores with independent reuse review, not double-blind gold'}
                if not reasons:
                    train=[row for row in rows if int(row['subject'][:8],16)%5!=0]
                    test=[row for row in rows if int(row['subject'][:8],16)%5==0]
                    if min(len({row['subject'] for row in train}),len({row['subject'] for row in test}))<2:
                        reasons.append('INDEPENDENT_HOLDOUT_REQUIRED')
                    else:
                        normative=[rubric.weights[d]/100 for d in DIMENSIONS]
                        baseline=[load_variant(settings,tenant,aggregation['variant']).weights[d]/100 for d in DIMENSIONS] if aggregation else normative
                        remaining=.05-max(abs(a-b) for a,b in zip(baseline,normative))
                        if remaining<=1e-6:
                            reasons.append('CUMULATIVE_WEIGHT_LIMIT');candidate=baseline
                        else:candidate=propose_weights([row['features'] for row in train],[row['label'] for row in train],baseline,max_change=min(.02,remaining))
                        from inkwise.calibration import score_metrics
                        labels=[row['label']*100 for row in test]
                        predict=lambda weights:[sum(a*b for a,b in zip(row['features'],weights))*100 for row in test]
                        before=score_metrics(labels,predict(baseline),100);after=score_metrics(labels,predict(candidate),100)
                        record['metrics']={'train':len(train),'test':len(test),'baseline':before,'candidate':after}
                        if after['mae']>before['mae']+1e-9:reasons.append('HELD_OUT_MAE_REGRESSED')
                        if after['qwkFiveBands'] is None or after['qwkFiveBands']<settings.evolution_min_qwk:reasons.append('ORDINAL_AGREEMENT_BELOW_POLICY')
                        if before['qwkFiveBands'] is not None and after['qwkFiveBands'] is not None and after['qwkFiveBands']<before['qwkFiveBands']-1e-9:reasons.append('ORDINAL_AGREEMENT_REGRESSED')
                        if max(abs(a-b) for a,b in zip(candidate,baseline))<1e-6:reasons.append('NO_MATERIAL_WEIGHT_CHANGE')
                        if not reasons:
                            proposed=rubric.model_copy(update={'weights':{d:weight*100 for d,weight in zip(DIMENSIONS,candidate)},'release':'learn-'+fingerprint[:12]})
                            record['proposedRubric']=proposed.model_dump(by_alias=True)
                atomic_json(path,record);index['ids'].insert(0,identifier);index['ids']=index['ids'][:100];index['total']+=1
                results.append(identifier)
            atomic_json(index_path,index)
        return {'period':period,'proposals':results,'automaticPublication':False}
    finally:engine.dispose()


def create_experiment(settings,tenant,document,actor):
    experiment=Experiment.model_validate(document);require_salt(settings)
    authorize_reviewer(settings,tenant,actor,experiment.classrooms)
    repository=RubricRepository(settings.rubric_dir);control=repository.get(tenant,experiment.rubric_id)
    if not control.grade_min<=experiment.grade<=control.grade_max:raise DomainError('EXPERIMENT_GRADE_SCOPE_INVALID')
    current=repository.active(tenant)[control.id]
    folder=settings.rubric_dir/safe_segment(tenant)
    filename=safe_segment(control.id+'-'+experiment.candidate_release+'.json')
    candidate=Rubric.model_validate_json(read_bundle_text(folder/filename))
    approval=folder/(filename+'.approval.json')
    if not approval.exists():raise DomainError('EXPERIMENT_APPROVED_VARIANT_REQUIRED')
    audit=json.loads(read_bundle_text(approval))
    if audit.get('sha256')!=digest(canonical(candidate.model_dump(by_alias=True))):raise DomainError('EXPERIMENT_APPROVED_VARIANT_REQUIRED')
    if (candidate.id!=control.id or candidate.grade_min!=control.grade_min or candidate.grade_max!=control.grade_max
        or not compatible_anchor(control.model_dump(by_alias=True),candidate.model_dump(by_alias=True),control.grade_min)):
        raise DomainError('EXPERIMENT_ONLY_WEIGHT_CHANGES_SUPPORTED')
    if experiment.mode=='live' and (control.synthetic or candidate.synthetic):raise DomainError('EXPERIMENT_FORMAL_RULES_REQUIRED')
    ordered=sorted(experiment.classrooms,key=lambda classroom:hmac.new(require_salt(settings).encode(),(tenant+':'+experiment.id+':'+classroom).encode(),hashlib.sha256).digest())
    assignments={classroom:'A' if index%2==0 else 'B' for index,classroom in enumerate(ordered)}
    proposal_link=audit.get('context',{}).get('modelWeightProposal')
    if proposal_link:
        if proposal_link['modelRevision']!=experiment.model_revision or proposal_link['pipelineRevision']!=experiment.pipeline_revision:raise DomainError('EXPERIMENT_MODEL_SCOPE_INVALID')
        proposal=json.loads(read_bundle_text(root(settings,tenant)/'proposals'/(safe_segment(proposal_link['id'])+'.json')))
        if not source_still_authorized(settings,tenant,proposal):raise DomainError('EXPERIMENT_SOURCE_REVOKED')
    record={**experiment.model_dump(by_alias=True),'assignments':assignments,'randomization':'balanced-classroom-hmac','tenantId':tenant,'approvedBy':actor,'status':'active',
        'version':1,'createdAt':datetime.now(timezone.utc).isoformat(),'expiresAt':(datetime.now(timezone.utc)+timedelta(days=experiment.duration_days)).isoformat(),
        'controlAggregation':aggregation_record(settings,tenant,control,experiment.model_revision,experiment.pipeline_revision),
        'control':current,'variant':{'file':filename,'sha256':digest(canonical(candidate.model_dump(by_alias=True)))},'proposalId':proposal_link['id'] if proposal_link else None}
    with tenant_lock(settings,tenant) as output:
        path=output/'experiments'/(experiment.id+'.json')
        if path.exists():raise DomainError('EXPERIMENT_EXISTS')
        # One active trial per rubric/class avoids competing randomization.
        old_experiments=list_experiments(settings,tenant)
        if len(old_experiments)>=100:raise DomainError('EXPERIMENT_ARCHIVE_REQUIRED')
        for old in old_experiments:
            if experiment_active(old) and old['rubricId']==control.id and set(old['classrooms'])&set(experiment.classrooms):
                raise DomainError('EXPERIMENT_SCOPE_CONFLICT')
        atomic_json(path,record)
    return record


def experiment_active(record):
    return record['enabled'] and datetime.fromisoformat(record['expiresAt'])>datetime.now(timezone.utc)


def list_experiments(settings,tenant):
    directory=root(settings,tenant)/'experiments'
    from itertools import islice
    paths=list(islice(directory.glob('*.json'),101)) if directory.exists() else []
    if len(paths)>100:raise DomainError('EXPERIMENT_ARCHIVE_REQUIRED')
    return [json.loads(read_bundle_text(path)) for path in sorted(paths)]


def load_variant(settings,tenant,entry):
    path=settings.rubric_dir/safe_segment(tenant)/safe_segment(entry['file'])
    raw=read_bundle_text(path)
    if digest(raw)!=entry['sha256']:raise DomainError('EXPERIMENT_RULE_INTEGRITY_FAILED')
    return Rubric.model_validate_json(raw)


def choose_rubric(settings,job,rubric,db=None):
    if not job.payload.get('experimentConsent') or job.payload.get('intent','assess')!='assess':return rubric,None
    for experiment in list_experiments(settings,job.tenant_id):
        if not experiment_active(experiment) or experiment['rubricId']!=rubric.id or job.classroom_id not in experiment['classrooms'] or job.payload.get('grade')!=experiment['grade']:continue
        if digest(canonical(rubric.model_dump(by_alias=True)))!=experiment['control']['sha256']:continue
        if experiment.get('proposalId'):
            proposal=json.loads(read_bundle_text(root(settings,job.tenant_id)/'proposals'/(safe_segment(experiment['proposalId'])+'.json')))
            if not source_still_authorized(settings,job.tenant_id,proposal):continue
        group=experiment['assignments'][job.classroom_id]
        if not job.payload.get('studentRef'):raise DomainError('STUDENT_GROUP_KEY_REQUIRED',422)
        if db is not None and experiment['mode']=='live':
            prior=db.scalar(select(JobEvent.id).join(Job,Job.id==JobEvent.job_id).where(
                Job.tenant_id==job.tenant_id,Job.id!=job.id,Job.payload['studentRef'].as_string()==job.payload['studentRef'],
                JobEvent.event=='experiment.assigned',JobEvent.detail['id'].as_string()==experiment['id'],
                JobEvent.detail['group'].as_string()!=group).limit(1))
            if prior is not None:raise DomainError('EXPERIMENT_SUBJECT_CROSSOVER',422)
        return rubric,{'id':experiment['id'],'proposalId':experiment.get('proposalId'),'group':group,'mode':experiment['mode'],'control':experiment['control'],'variant':experiment['variant'],
            'controlAggregation':experiment.get('controlAggregation'),'modelRevision':experiment['modelRevision'],'pipelineRevision':experiment['pipelineRevision']}
    return rubric,None


def report_experiment(settings,sessions,job,report):
    if report.get('kind')=='feedback':return None
    with sessions() as db:
        event=db.scalar(select(JobEvent).where(JobEvent.job_id==job.id,JobEvent.tenant_id==job.tenant_id,
            JobEvent.event=='experiment.assigned').order_by(JobEvent.id.desc()).limit(1))
        if not event or event.detail.get('textHash')!=job.text_hash:return None
        assignment=event.detail
    current_path=root(settings,job.tenant_id)/'experiments'/(safe_segment(assignment['id'])+'.json')
    if not current_path.is_file() or not experiment_active(json.loads(read_bundle_text(current_path))):
        return {**assignment,'status':'inactive','shadowTotal':None}
    if (assignment['modelRevision']!=report.get('modelRevision') or assignment['pipelineRevision']!=report.get('pipelineRevision')):
        return {**assignment,'status':'model-scope-mismatch','shadowTotal':None}
    with sessions() as db:
        withdrawn=db.scalar(select(JobEvent.id).where(JobEvent.job_id==job.id,JobEvent.event=='experiment.withdrawn').limit(1))
    if withdrawn:return {**assignment,'status':'withdrawn','shadowTotal':None}
    normative=Rubric.model_validate(job.rubric)
    control=assignment.get('controlAggregation')
    if control and not aggregation_valid(settings,job.tenant_id,normative,report['modelRevision'],report['pipelineRevision'],control):
        return {**assignment,'status':'control-source-changed','shadowTotal':None}
    if control:apply_model_weights(settings,job.tenant_id,normative,report,control)
    proposal_id=assignment.get('proposalId') or assignment['variant'].get('proposalId')
    if proposal_id:
        proposal=json.loads(read_bundle_text(root(settings,job.tenant_id)/'proposals'/(safe_segment(proposal_id)+'.json')))
        if not source_still_authorized(settings,job.tenant_id,proposal):return {**assignment,'status':'variant-source-changed','shadowTotal':None}
    variant=load_variant(settings,job.tenant_id,assignment['variant'])
    shadow=round(sum(variant.weights[row['dimension']]*row['band']/4 for row in report['scores']),2)
    if assignment['mode']=='live' and assignment['group']=='B':
        report['uncalibratedTotal']=report['total'];report['total']=shadow
        report['aggregation']={'profileHash':assignment['variant']['sha256'],'weights':variant.weights,'use':'model-only weight trial'}
    return {'id':assignment['id'],'group':assignment['group'],'mode':assignment['mode'],'status':'active','shadowTotal':shadow,
        'note':'Only the model suggestion is calibrated; teacher final grades use the frozen normative rubric.'}


def evaluate_experiment(settings,tenant,identifier):
    path=root(settings,tenant)/'experiments'/(safe_segment(identifier)+'.json')
    if not path.is_file():raise DomainError('NOT_FOUND',404)
    experiment=json.loads(read_bundle_text(path));engine,sessions=create_session_factory(settings.database_url)
    groups={'A':[],'B':[]};classes={'A':set(),'B':set()}
    try:
        with sessions() as db:
            rows=db.execute(select(Job.id,Job.result['experiment'],Job.result['total'].as_float(),Job.review['scores'],
                Job.payload['studentRef'].as_string(),Job.payload['experimentConsent'].as_boolean(),Job.classroom_id).where(Job.tenant_id==tenant,
                Job.classroom_id.in_(experiment['classrooms']),Job.status.in_(['approved','published']))
                .order_by(Job.updated_at.desc()).limit(settings.evolution_max_cases)).all()
            identifiers=[row[0] for row in rows]
            withdrawn=set(db.scalars(select(JobEvent.job_id).where(JobEvent.tenant_id==tenant,JobEvent.job_id.in_(identifiers),JobEvent.event=='experiment.withdrawn')).all()) if identifiers else set()
        control=load_variant(settings,tenant,experiment['control'])
        invalid=0
        for job_id,assignment,total,scores,student,consent,classroom in rows:
            if job_id in withdrawn or not consent or not assignment:continue
            if assignment.get('id')!=identifier or assignment.get('status')!='active':continue
            if not student or not scores or set(scores)!=set(DIMENSIONS) or any(type(value) is not int or not 0<=value<=4 for value in scores.values()):invalid+=1;continue
            expected=sum(control.weights[d]*scores[d]/4 for d in DIMENSIONS)
            group=assignment.get('group')
            valid_score=lambda value:type(value) in (int,float) and math.isfinite(value) and 0<=value<=100
            if (group not in groups or assignment.get('mode')!=experiment['mode'] or not valid_score(total)
                or not valid_score(assignment.get('shadowTotal'))
                or (experiment['mode']=='live' and experiment['assignments'].get(classroom)!=group)):
                invalid+=1;continue
            predicted=total if experiment['mode']=='live' else assignment['shadowTotal'] if group=='B' else total
            if group not in groups or type(predicted) not in (int,float) or not math.isfinite(predicted) or not 0<=predicted<=100:invalid+=1;continue
            key=subject_key(settings,tenant,student)
            if experiment['mode']=='shadow':
                groups['A'].append((key,abs(total-expected)));groups['B'].append((key,abs(assignment['shadowTotal']-expected)))
                classes['A'].add(classroom);classes['B'].add(classroom)
            else:
                groups[group].append((key,abs(predicted-expected)));classes[group].add(classroom)
        report={'id':identifier,'mode':experiment['mode'],'comparison':'paired-shadow' if experiment['mode']=='shadow' else 'classroom-randomized','groups':{},'decision':'insufficient-evidence','automaticGradeRewrite':False}
        for group,values in groups.items():
            report['groups'][group]={'observations':len(values),'subjects':len({key for key,_ in values}),'classes':len(classes[group]),
                'mae':sum(error for _,error in values)/len(values) if values else None}
        enough=all(report['groups'][g]['subjects']>=experiment['minObservations'] and report['groups'][g]['classes']>=(1 if experiment['mode']=='shadow' else 2) for g in ('A','B'))
        crossover={key for key,_ in groups['A']}&{key for key,_ in groups['B']}
        if invalid:report['decision']='invalid-observations';report['invalidObservations']=invalid;enough=False
        if crossover and experiment['mode']=='live':
            report['decision']='subject-crossover';report['crossGroupSubjects']=len(crossover);enough=False
        if enough:
            difference=report['groups']['B']['mae']-report['groups']['A']['mae']
            report['maeDifference']=difference
            report['decision']='pause-regressed-variant' if difference>experiment['maxMaeIncrease']+1e-9 else 'no-regression-observed'
            if report['decision']=='pause-regressed-variant':
                with tenant_lock(settings,tenant):
                    current=json.loads(read_bundle_text(path));current['enabled']=False;current['status']='paused-regression';current['version']+=1
                    atomic_json(path,current)
                    pointer=root(settings,tenant)/'model-weights'/(safe_segment(experiment['rubricId'])+'.json')
                    if pointer.exists():
                        active=json.loads(read_bundle_text(pointer))
                        if active['variant']['sha256']==experiment['variant']['sha256']:
                            atomic_json(root(settings,tenant)/'model-weights'/'history'/(identifier+'-regressed.json'),active)
                            previous=experiment.get('controlAggregation')
                            if previous:atomic_json(pointer,previous)
                            else:pointer.unlink()
                            report['modelAggregationRolledBack']=True
        atomic_json(root(settings,tenant)/'experiment-reports'/(identifier+'.json'),report)
        return report
    finally:engine.dispose()


async def run_evolution_worker(settings,tenant):
    import asyncio
    if not settings.evolution_enabled:raise DomainError('EVOLUTION_NOT_ENABLED')
    while True:
        try:
            await asyncio.to_thread(learning_cycle,settings,tenant)
            for experiment in list_experiments(settings,tenant):
                if experiment_active(experiment):await asyncio.to_thread(evaluate_experiment,settings,tenant,experiment['id'])
        except DomainError as error:
            print(error.code,flush=True)
        await asyncio.sleep(settings.evolution_interval_seconds)


def validate_publication_experiment(experiment,proposal,tenant,identifier):
    expected={'id':identifier,'tenantId':tenant,'rubricId':proposal['rubricId'],
        'candidateRelease':proposal['proposedRubric']['release'],'proposalId':proposal['id'],
        'modelRevision':proposal['modelRevision'],'pipelineRevision':proposal['pipelineRevision'],'grade':proposal['grade']}
    if any(experiment.get(key)!=value for key,value in expected.items()) or set(experiment.get('classrooms',[]))!=set(proposal['classrooms']):
        raise DomainError('EXPERIMENT_EVIDENCE_REQUIRED')


def proposal_action(settings,principal,identifier,body):
    import tempfile
    from inkwise.knowledge import import_rubric
    tenant=principal.tenant_id
    if body.action=='publish':
        pre_path=root(settings,tenant)/'proposals'/(safe_segment(identifier)+'.json')
        if not pre_path.is_file():raise DomainError('NOT_FOUND',404)
        pre=json.loads(read_bundle_text(pre_path))
        authorize_reviewer(settings,tenant,principal.user_id,pre['classrooms'])
        if pre.get('submittedBy')==principal.user_id:raise DomainError('INDEPENDENT_REVIEWER_REQUIRED')
        if pre['version']!=body.version:raise DomainError('VERSION_CONFLICT')
        if pre['state']!='approved-inactive':raise DomainError('INVALID_STATE')
        if not source_still_authorized(settings,tenant,pre):raise DomainError('PROPOSAL_SOURCE_REVOKED')
        if not body.experiment_id:raise DomainError('EXPERIMENT_EVIDENCE_REQUIRED')
        trial_path=root(settings,tenant)/'experiments'/(safe_segment(body.experiment_id)+'.json')
        if not trial_path.is_file():raise DomainError('NOT_FOUND',404)
        trial=json.loads(read_bundle_text(trial_path))
        authorize_reviewer(settings,tenant,principal.user_id,trial['classrooms'])
        validate_publication_experiment(trial,pre,tenant,body.experiment_id)
        evaluate_experiment(settings,tenant,body.experiment_id)
    with tenant_lock(settings,tenant) as folder:
        path=folder/'proposals'/(safe_segment(identifier)+'.json')
        if not path.is_file():raise DomainError('NOT_FOUND',404)
        record=json.loads(read_bundle_text(path))
        if principal.role!=Role.ADMIN and not set(record['classrooms'])<=set(principal.classroom_ids):raise DomainError('NOT_FOUND',404)
        if body.version!=record['version']:raise DomainError('VERSION_CONFLICT')
        pointer_path=folder/'model-weights'/(safe_segment(record['rubricId'])+'.json')
        pointer=json.loads(read_bundle_text(pointer_path)) if pointer_path.exists() else None
        if body.action=='publish' and record['state']=='approved-inactive' and pointer and pointer.get('proposalId')==identifier:
            authorize_reviewer(settings,tenant,principal.user_id,record['classrooms'])
            record.update(state='published',version=record['version']+1,recoveredBy=principal.user_id)
            atomic_json(path,record)
            return {'id':identifier,'state':record['state'],'version':record['version']}
        current_rule=RubricRepository(settings.rubric_dir).get(tenant,record['rubricId'])
        if digest(canonical(current_rule.model_dump(by_alias=True)))!=record['baselineHash']:raise DomainError('PROPOSAL_BASELINE_CHANGED')
        active_aggregation=aggregation_record(settings,tenant,current_rule,record['modelRevision'],record['pipelineRevision'])
        active_hash=active_aggregation['variant']['sha256'] if active_aggregation else None
        if active_hash!=record.get('baselineAggregationHash'):raise DomainError('PROPOSAL_BASELINE_CHANGED')
        if not record.get('proposedRubric') or record['reasons']:raise DomainError('PROPOSAL_NOT_READY')
        if not source_still_authorized(settings,tenant,record):raise DomainError('PROPOSAL_SOURCE_REVOKED')
        if body.action=='submit':
            if record['state']!='proposal-not-active':raise DomainError('INVALID_STATE')
            record.update(state='submitted',submittedBy=principal.user_id,submitNote=body.note)
        else:
            authorize_reviewer(settings,tenant,principal.user_id,record['classrooms'])
            if record.get('submittedBy')==principal.user_id:raise DomainError('INDEPENDENT_REVIEWER_REQUIRED')
            publish=body.action=='publish'
            if record['state']!=('approved-inactive' if publish else 'submitted'):raise DomainError('INVALID_STATE')
            if publish:
                if not body.experiment_id:raise DomainError('EXPERIMENT_EVIDENCE_REQUIRED')
                experiment_path=folder/'experiments'/(safe_segment(body.experiment_id)+'.json')
                if not experiment_path.is_file():raise DomainError('NOT_FOUND',404)
                experiment=json.loads(read_bundle_text(experiment_path))
                validate_publication_experiment(experiment,record,tenant,body.experiment_id)
                report_path=folder/'experiment-reports'/(body.experiment_id+'.json')
                if not report_path.is_file():raise DomainError('EXPERIMENT_EVIDENCE_REQUIRED')
                report=json.loads(read_bundle_text(report_path))
                if report.get('id')!=body.experiment_id or report.get('mode')!=experiment['mode'] or report.get('decision')!='no-regression-observed':
                    raise DomainError('EXPERIMENT_EVIDENCE_REQUIRED')
            with tempfile.TemporaryDirectory(prefix='inkwise-proposal-') as temp:
                rule_path=Path(temp)/'rubric.json';atomic_json(rule_path,record['proposedRubric'])
                import_rubric(settings,tenant,rule_path,submitted_by=record['submittedBy'],approved_by=principal.user_id,
                    source='Reviewed model-only weight proposal '+identifier+': '+body.note,activate=False,
                    approval_context={'modelWeightProposal':{'id':identifier,'modelRevision':record['modelRevision'],'pipelineRevision':record['pipelineRevision']}})
            if publish:
                filename=safe_segment(record['proposedRubric']['id']+'-'+record['proposedRubric']['release']+'.json')
                atomic_json(folder/'model-weights'/(record['rubricId']+'.json'),{'proposalId':identifier,
                    'baselineHash':record['baselineHash'],'modelRevision':record['modelRevision'],'pipelineRevision':record['pipelineRevision'],
                    'publishedBy':principal.user_id,'publishedAt':datetime.now(timezone.utc).isoformat(),
                    'variant':{'file':filename,'sha256':digest(canonical(record['proposedRubric']))}})
            record.update(state='published' if publish else 'approved-inactive',approvedBy=principal.user_id)
        record['version']+=1;atomic_json(path,record)
        return {'id':identifier,'state':record['state'],'version':record['version']}


def control_experiment(settings,principal,identifier,body):
    with tenant_lock(settings,principal.tenant_id) as folder:
        path=folder/'experiments'/(safe_segment(identifier)+'.json')
        if not path.is_file():raise DomainError('NOT_FOUND',404)
        record=json.loads(read_bundle_text(path))
        authorize_reviewer(settings,principal.tenant_id,principal.user_id,record['classrooms'])
        if body.version!=record['version']:raise DomainError('VERSION_CONFLICT')
        if body.enabled and record['status']=='paused-regression':raise DomainError('EXPERIMENT_REEVALUATION_REQUIRED')
        record.update(enabled=body.enabled,status='active' if body.enabled else 'paused-manual',version=record['version']+1)
        atomic_json(path,record)
        return record


def analysis_rubric(settings,sessions,job,default):
    with sessions() as db:
        event=db.scalar(select(JobEvent).where(JobEvent.job_id==job.id,JobEvent.tenant_id==job.tenant_id,
            JobEvent.event=='experiment.assigned').order_by(JobEvent.id.desc()).limit(1))
        if not event or event.detail.get('textHash')!=job.text_hash:return default
        return load_variant(settings,job.tenant_id,event.detail['control'])
