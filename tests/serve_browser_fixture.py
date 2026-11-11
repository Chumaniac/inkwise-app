"""Loopback-only browser fixture with synthetic identity and real local OCR.

No model endpoint is configured: requesting a score must display an explicit
unconfigured-service error, never a fake grade. Never use this fixture for users.
"""

import asyncio
import hashlib
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn

from inkwise.api import create_app
from inkwise.cli import seed_demo
from inkwise.database import Base, Identity, Job, Candidate, TenantQuota, create_session_factory, now
from inkwise.rubrics import RubricRepository,digest
from inkwise.core import DIMENSIONS
from inkwise.settings import Settings
from inkwise.worker import run_worker


def main():
    if os.getenv("INKWISE_SYNTHETIC_PREVIEW")!="1":
        raise SystemExit("Explicit synthetic-preview flag required")
    from PIL import Image,ImageDraw,ImageFont
    images=Path('artifacts/browser-check');images.mkdir(parents=True,exist_ok=True)
    font_path=Path('/System/Library/Fonts/Supplemental/Arial.ttf')
    font=ImageFont.truetype(str(font_path),36) if font_path.is_file() else ImageFont.load_default(size=36)
    for number,text in enumerate(['First page. We planted a garden.\nThis is a synthetic OCR fixture.',
                                  'Second page. We watered the flowers.\nThis is another synthetic test page.',
                                  'Replacement page. We shared the tools.\nThis is a new synthetic scan.'],1):
        path=images/f'page-{number}.png'
        if not path.exists():
            image=Image.new('RGB',(1100,450),'white')
            ImageDraw.Draw(image).multiline_text((40,60),text,fill='black',font=font,spacing=30);image.save(path)
    with TemporaryDirectory(prefix="inkwise-ui-") as directory:
        root=Path(directory)
        settings=Settings(database_url=f"sqlite:///{root}/preview.sqlite",data_dir=root,
            rubric_dir=root/"rules",ocr_backend="vision",vision_executable=Path("artifacts/inkwise-vision").resolve(),
            model_base_url="",model_name="",evaluation_salt="synthetic-browser-evaluation-salt-32-characters")
        engine,sessions=create_session_factory(settings.database_url);Base.metadata.create_all(engine)
        with sessions.begin() as db:
            db.add(TenantQuota(tenant_id="synthetic-preview"))
            db.add(Identity(id="synthetic-teacher",tenant_id="synthetic-preview",role="teacher",classroom_ids=["demo-class"],
                token_hash=hashlib.sha256(b"synthetic-local-preview-only").hexdigest()))
        with sessions.begin() as db:
            for role in ('reviewer','admin'):
                db.add(Identity(id='synthetic-'+role,tenant_id='synthetic-preview',role=role,classroom_ids=['demo-class'],
                    token_hash=hashlib.sha256(('synthetic-'+role+'-preview-only').encode()).hexdigest()))
        seed_demo(settings,"synthetic-preview")
        rubric=RubricRepository(settings.rubric_dir).get('synthetic-preview','english-demo').model_dump(by_alias=True)
        essay='A short synthetic paragraph for testing teacher feedback editing.'
        with sessions.begin() as db:
            db.add(Job(id='synthetic-feedback-edit',tenant_id='synthetic-preview',classroom_id='demo-class',owner_id='synthetic-teacher',
                idempotency_key='synthetic-feedback-edit',request_hash='synthetic',payload={'title':'合成教师编辑测试','grade':7,'classroomId':'demo-class','prompt':'Synthetic teacher review test','text':essay,'languagePolicy':'en','genre':'short-text','rubricId':'english-demo'},
                text=essay,text_hash=digest(essay),text_version=1,version=1,status='teacher_review',rubric=rubric,
                result={'total':75,'textHash':digest(essay),'rubricId':rubric['id'],'release':rubric['release'],'modelRevision':'synthetic-fixture-v1','pipelineRevision':'synthetic-fixture-v1','warnings':['SYNTHETIC_RUBRIC_NOT_SCHOOL_STANDARD'],'model':'synthetic-browser-fixture',
                    'scores':[{'dimension':d,'band':3,'reason':'MODEL_DRAFT_SENTINEL','suggestion':'MODEL_SUGGESTION_SENTINEL',
                    'evidence':[{'start':0,'end':len(essay),'quote':essay,'ruleId':rubric['rules'][i]['id']}]} for i,d in enumerate(DIMENSIONS)]}))
        with sessions.begin() as db:
            db.add(Job(id='synthetic-proofread',tenant_id='synthetic-preview',classroom_id='demo-class',owner_id='synthetic-teacher',
                idempotency_key='synthetic-proofread',request_hash='synthetic',payload={'title':'合成校对测试','grade':7,'classroomId':'demo-class',
                    'prompt':'Proofread only','text':essay,'languagePolicy':'en','genre':'short-text','rubricId':'english-demo','intent':'proofread'},
                text=essay,text_hash=digest(essay),text_version=1,version=1,status='teacher_review',rubric=rubric,
                result={'kind':'feedback','total':None,'scores':[],'model':'synthetic-browser-fixture','warnings':[],
                    'findings':[{'dimension':'grammar','suggestion':'SYNTHETIC_PROOFREAD_SUGGESTION',
                    'evidence':{'start':0,'end':len(essay),'quote':essay,'ruleId':rubric['rules'][-1]['id']}}]}))
        from inkwise.database import JobEvent
        with sessions.begin() as db:
            db.add_all(JobEvent(job_id='synthetic-feedback-edit',tenant_id='synthetic-preview',
                actor_id='synthetic-worker',event='scoring.started',job_version=1,detail={}) for _ in range(205))
        # Synthetic history exercises windowed lists without any live student records.
        from datetime import timedelta
        with sessions.begin() as db:
            for i in range(35):
                identifier=f'synthetic-history-{i:02}'
                db.add(Job(id=identifier,tenant_id='synthetic-preview',classroom_id='demo-class',owner_id='synthetic-teacher',
                    idempotency_key=identifier,request_hash='synthetic',payload={'title':f'历史练习 {i:02}','grade':7,'intent':'assess'},
                    status='approved',rubric=rubric,review={'scores':dict.fromkeys(DIMENSIONS,3),'total':75,'comment':'Synthetic history'},
                    created_at=now()-timedelta(days=i+1)))
                if i<22:db.add(Candidate(id=identifier,tenant_id='synthetic-preview',classroom_id='demo-class',job_id=identifier,
                    report_version=1,submitted_by='synthetic-teacher',status='rejected',text=f'Synthetic archived case {i:02}',
                    provenance='Original synthetic fixture',rubric=rubric,review={'comment':'Synthetic history','scores':dict.fromkeys(DIMENSIONS,3)},created_at=now()-timedelta(days=i+1)))
        with sessions.begin() as db:
            db.add(Job(id='synthetic-independent-review',tenant_id='synthetic-preview',classroom_id='demo-class',owner_id='synthetic-teacher',
                idempotency_key='independent-review',request_hash='synthetic',status='approved',rubric=rubric,
                payload={'title':'独立审核往返案例','grade':7},review={'scores':dict.fromkeys(DIMENSIONS,3),'total':75,'comment':'Synthetic teacher review'}))
            db.add(Candidate(id='synthetic-independent-review',tenant_id='synthetic-preview',classroom_id='demo-class',job_id='synthetic-independent-review',
                report_version=1,submitted_by='synthetic-teacher',status='pending',text='Independent synthetic review case',provenance='Original synthetic fixture',
                rubric=rubric,review={'scores':dict.fromkeys(DIMENSIONS,3),'comment':'Synthetic independent-review comment','feedback':{},'reuseScope':'explicit-anonymized-v1'}))
        if os.getenv('INKWISE_REUSE_ACCEPTED_REPORT')=='1':
            # Read-only reuse of the project's own completed synthetic benchmark.
            import json,sqlite3
            source=Path('artifacts/long-text-check/real-local')
            receipt=json.loads((source/'summary.json').read_text())
            assert receipt['mode']=='real-local' and receipt['status']=='teacher_review' and receipt['characters']==15000
            connection=sqlite3.connect(f'file:{(source/"db.sqlite").resolve()}?mode=ro',uri=True)
            try:
                rows=connection.execute("SELECT text,text_hash,rubric,result,payload FROM jobs WHERE tenant_id='synthetic-perf' AND status='teacher_review'").fetchall()
                assert len(rows)==1
                text,text_hash,raw_rubric,raw_report,raw_payload=rows[0]
                assert len(text)==15000 and digest(text)==text_hash
                report=json.loads(raw_report);source_payload=json.loads(raw_payload)
                assert report['textHash']==text_hash and len(report['scores'])==5
            finally:connection.close()
            with sessions.begin() as db:
                db.add(Job(id='preserved-local-report',tenant_id='synthetic-preview',classroom_id='demo-class',owner_id='synthetic-teacher',
                    idempotency_key='preserved-local-report',request_hash='synthetic',text=text,text_hash=text_hash,text_version=1,status='teacher_review',
                    payload={**source_payload,'title':'已有本机模型报告 · 合成长文','classroomId':'demo-class'},rubric=json.loads(raw_rubric),result=report))
        # Keep editing a teacher report while viewing a different original page.
        from inkwise.ocr import normalize_image,store_image
        pages=[]
        for i in (1,2):
            content=normalize_image((images/f'page-{i}.png').read_bytes(),settings)
            identifier=f'synthetic-view-page-{i}'
            store_image(root/'images'/(identifier+'.png'),content)
            pages.append({'imageId':identifier,'sha256':hashlib.sha256(content).hexdigest(),'page':i})
        with sessions.begin() as db:
            row=db.get(Job,'synthetic-feedback-edit');row.image_id=pages[0]['imageId'];row.ocr={'pages':pages,'text':essay,'engine':'synthetic-review-fixture','lines':[],'uncertainLines':[]}
        app=create_app(settings)
        original=app.router.lifespan_context
        @asynccontextmanager
        async def lifespan(app):
            async with original(app):
                task=asyncio.create_task(run_worker(settings))
                try:yield
                finally:
                    task.cancel()
                    with suppress(asyncio.CancelledError):await task
        app.router.lifespan_context=lifespan
        try:uvicorn.run(app,host="127.0.0.1",port=int(os.getenv("INKWISE_PREVIEW_PORT", "8766")),access_log=False)
        finally:engine.dispose()


if __name__=="__main__":main()
