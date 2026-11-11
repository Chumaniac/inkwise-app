"""Compare bounded queue views with synthetic rows in private temporary databases."""
import argparse
import gc
import hashlib
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
from tempfile import TemporaryDirectory
import tracemalloc
from uuid import uuid4


def measure(view):
    from fastapi.testclient import TestClient
    from inkwise.api import create_app
    from inkwise.database import Base, Identity, Job, create_session_factory
    from inkwise.settings import Settings
    with TemporaryDirectory(prefix='inkwise-summary-') as temporary:
        root = Path(temporary)
        settings = Settings(_env_file=None, database_url=f'sqlite:///{root}/synthetic.sqlite', data_dir=root,
                            rubric_dir=root/'rules', cloud_enabled=False)
        engine, sessions = create_session_factory(settings.database_url)
        Base.metadata.create_all(engine)
        text = 'SYNTHETIC_BODY ' * 1000
        with sessions.begin() as db:
            db.add(Identity(id='teacher', tenant_id='synthetic', role='teacher', classroom_ids=['class'],
                            token_hash=hashlib.sha256(b'synthetic-access').hexdigest()))
            ids = [str(uuid4()) for _ in range(30)]
            for i, identifier in enumerate(ids):
                db.add(Job(id=identifier, tenant_id='synthetic', classroom_id='class', owner_id='teacher',
                           idempotency_key=str(i), request_hash='0'*64, status='teacher_review', text=text,
                           payload={'title':'Synthetic load','grade':7,'prompt':text}, result={'synthetic':text}))
        try:
            with TestClient(create_app(settings)) as client:
                client.headers['Authorization'] = 'Bearer synthetic-access'
                gc.collect(); tracemalloc.start()
                sizes=[]
                for _ in range(5):
                    response=client.get('/api/v1/jobs', params={'limit':30,'view':view})
                    assert response.status_code == 200
                    assert len(response.json()['items']) == 30
                    sizes.append(len(response.content))
                    if view == 'summary': assert 'SYNTHETIC_BODY' not in response.text
                _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
                assert client.get('/api/v1/jobs/'+ids[0]).json()['text'] == text
            return {'view':view,'rows':30,'responseBytes':max(sizes),'pythonAllocationPeakBytes':peak,
                    'processPeakRssBytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform=='darwin' else 1024),
                    'detailPreserved':True}
        finally:
            engine.dispose()


if __name__ == '__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--view',choices=['full','summary']);args=parser.parse_args()
    if args.view:
        print(json.dumps(measure(args.view)))
    else:
        rows=[json.loads(subprocess.check_output([sys.executable,__file__,'--view',view],text=True)) for view in ('full','summary')]
        assert rows[1]['responseBytes'] < rows[0]['responseBytes']/4
        print(json.dumps({'scope':'isolated synthetic HTTP/SQLite; five reads per fresh process; no Provider',
                          'rows':rows,'benchmarkSha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                          'limits':['RSS is whole-process peak including imports/setup, not request-only or long-term memory',
                                    'No school throughput or CPU improvement claim; full remains the compatibility default']},indent=2))
