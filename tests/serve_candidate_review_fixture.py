"""Read-only queue and explicit human-decision preview; synthetic local data only."""
import os
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import datetime, timezone, timedelta
import uvicorn
from inkwise.api import create_app
from inkwise.database import Base, Identity, Job, Candidate, TenantQuota, create_session_factory
from inkwise.settings import Settings
from inkwise.cli import seed_demo
from inkwise.core import DIMENSIONS
from inkwise.rubrics import RubricRepository


def main():
    if os.getenv('INKWISE_SYNTHETIC_PREVIEW') != '1':
        raise SystemExit('Explicit synthetic-preview flag required')
    with TemporaryDirectory(prefix='inkwise-review-only-') as directory:
        root = Path(directory)
        settings = Settings(database_url=f'sqlite:///{root}/preview.sqlite', data_dir=root,
            rubric_dir=root/'rubrics', model_base_url='', model_name='',
            ocr_backend='vision', vision_executable=Path('artifacts/inkwise-vision').resolve())
        engine, sessions = create_session_factory(settings.database_url)
        Base.metadata.create_all(engine)
        seed_demo(settings, 'synthetic-review')
        rubric = RubricRepository(settings.rubric_dir).get('synthetic-review', 'english-demo').model_dump(by_alias=True)
        with sessions.begin() as db:
            db.add(TenantQuota(tenant_id='synthetic-review'))
            for role in ('teacher', 'reviewer'):
                db.add(Identity(id='synthetic-'+role, tenant_id='synthetic-review', role=role,
                    classroom_ids=['review-class'], token_hash=hashlib.sha256(('synthetic-queue-'+role).encode()).hexdigest()))
            for index, (name, owner, version) in enumerate([
                ('review-first','synthetic-teacher',1), ('review-second','synthetic-teacher',1),
                ('review-stale','synthetic-teacher',2), ('review-own','synthetic-reviewer',1)]):
                db.add(Job(id='job-'+name, tenant_id='synthetic-review', classroom_id='review-class', owner_id=owner,
                    idempotency_key=name, request_hash='synthetic', status='approved', version=version, rubric=rubric,
                    payload={'title':'Synthetic source '+name,'grade':7}, review={'scores':dict.fromkeys(DIMENSIONS,3),'comment':'Synthetic teacher review'}))
                db.add(Candidate(id=name, tenant_id='synthetic-review', classroom_id='review-class', job_id='job-'+name,
                    report_version=1, submitted_by=owner, status='pending', version=1, text='Anonymized synthetic case '+name,
                    provenance='Synthetic fixture permission', rubric=rubric, review={'comment':'Synthetic case feedback'},
                    created_at=datetime(2026,1,1,tzinfo=timezone.utc)+timedelta(seconds=index)))
        try:
            uvicorn.run(create_app(settings), host='127.0.0.1', port=int(os.getenv('INKWISE_PREVIEW_PORT','19303')), access_log=False)
        finally:
            engine.dispose()


if __name__ == '__main__':
    main()
