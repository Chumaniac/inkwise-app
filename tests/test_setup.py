import json
import os
from pathlib import Path

import pytest

from inkwise.errors import DomainError
from inkwise.rubrics import RubricRepository
from inkwise.settings import Settings
from inkwise.setup import doctor, load_teaching_pack, setup_local

TEACHING = Path(__file__).resolve().parents[1] / 'teaching'


def test_local_configuration_round_trips_unicode_workspace_paths(tmp_path, monkeypatch):
    for key in os.environ:
        if key.startswith('INKWISE_'):
            monkeypatch.delenv(key)
    destination = tmp_path / '中文 工作区'
    setup_local(destination, TEACHING, frontend_dir=TEACHING.parent / 'web')
    settings = Settings(_env_file=destination / '.env')
    assert settings.data_dir == destination
    assert Path(settings.database_url.removeprefix('sqlite:///')).is_file()
    assert settings.rubric_dir == destination / 'rules'


def test_operator_cli_can_revoke_a_locally_provisioned_identity(tmp_path, monkeypatch, capsys):
    import sys
    from inkwise import cli
    from inkwise.database import Identity, create_session_factory, initialize_database

    settings = Settings(_env_file=None, database_url=f'sqlite:///{tmp_path}/operator.sqlite')
    initialize_database(settings.database_url)
    private_file = tmp_path / 'synthetic-access'
    user_id = cli.provision(settings, 'synthetic-school', ['classroom'], 'teacher', private_file)
    monkeypatch.setattr(cli, 'Settings', lambda **kwargs: settings)
    monkeypatch.setattr(sys, 'argv', ['inkwise', 'revoke', '--user-id', user_id])
    cli._main()
    engine, sessions = create_session_factory(settings.database_url)
    try:
        with sessions() as database:
            assert database.get(Identity, user_id).active is False
    finally:
        engine.dispose()
    assert private_file.read_text().strip() not in capsys.readouterr().out


def test_pack_is_traceable_and_never_presents_teacher_gold():
    manifest, rubrics = load_teaching_pack(TEACHING)
    assert len(rubrics) == 13
    assert all(rule.synthetic for rule in rubrics)
    words = json.loads((TEACHING / manifest['lexiconFile']).read_text())
    assert len({(row['incorrect'], row['correct']) for row in words['entries']}) == 6000
    assert words['license'] == 'CC-BY-SA-3.0'


def test_setup_is_isolated_private_and_refuses_overwrite(tmp_path):
    destination = tmp_path / 'space in path' / 'local'
    result = setup_local(destination, TEACHING)
    assert result['schoolApproved'] is False
    assert result['externalLexiconActivated'] is False
    settings = Settings(_env_file=destination / '.env')
    assert doctor(settings)['ready']
    assert len(RubricRepository(settings.rubric_dir).list('local-demo')) == 13
    assert len(set(result['identities'].values())) == 3
    for filename in ('.env', 'setup.json', 'teacher-access.txt', 'reviewer-access.txt', 'admin-access.txt'):
        assert os.stat(destination / filename).st_mode & 0o777 == 0o600
    original = (destination / 'teacher-access.txt').read_bytes()
    with pytest.raises(DomainError, match='LOCAL_WORKSPACE_ALREADY_EXISTS'):
        setup_local(destination, TEACHING)
    assert (destination / 'teacher-access.txt').read_bytes() == original


def test_packaged_runtime_assets_work_outside_the_source_directory(tmp_path, monkeypatch):
    frontend = tmp_path / 'installed app' / 'web'
    frontend.mkdir(parents=True)
    (frontend / 'index.html').write_text('<html>synthetic installed workspace</html>')
    vision = tmp_path / 'installed app' / 'vision'
    vision.write_text('synthetic native executable; never run by this test')
    vision.chmod(0o755)
    destination = tmp_path / 'private workspace'
    setup_local(destination, TEACHING, frontend_dir=frontend, vision_executable=vision)
    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=destination / '.env')
    assert settings.served_frontend_dir == frontend.resolve()
    assert settings.ocr_backend == 'vision' and settings.vision_executable == vision.resolve()
    assert doctor(settings)['ready']
    assert settings.cloud_enabled is False and settings.max_cloud_cost_microusd == 0


def test_missing_packaged_assets_do_not_create_a_partial_workspace(tmp_path):
    destination = tmp_path / 'workspace'
    with pytest.raises(DomainError, match='LOCAL_RUNTIME_ASSET_MISSING'):
        setup_local(destination, TEACHING, frontend_dir=tmp_path / 'missing')
    assert not destination.exists()


def test_doctor_does_not_create_a_database_or_expose_credentials(tmp_path):
    database = tmp_path / 'missing.sqlite'
    settings = Settings(database_url=f'sqlite:///{database}', model_api_key='synthetic-secret-never-print')
    report = doctor(settings)
    assert not report['ready'] and not database.exists()
    assert 'synthetic-secret-never-print' not in json.dumps(report)


def test_doctor_explains_missing_additive_schema_without_initializing_it(tmp_path):
    import hashlib
    import sqlite3
    from inkwise.database import initialize_database
    database = tmp_path / 'previous.sqlite3'
    settings = Settings(database_url=f'sqlite:///{database}')
    initialize_database(settings.database_url)
    with sqlite3.connect(database) as db:
        db.execute('DROP TABLE revision_assessments')
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    check = next(item for item in doctor(settings)['checks'] if item['name'] == 'database')
    assert check['status'] == 'fail' and 'upgrade-check' in check['detail']
    assert 'baseline schema missing' not in check['detail']
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_pack_rejects_hash_tampering_and_path_escape(tmp_path):
    import shutil
    folder = tmp_path / 'teaching'
    shutil.copytree(TEACHING, folder)
    manifest = json.loads((folder / 'manifest.json').read_text())
    path = folder / manifest['rubrics'][0]['file']
    path.write_text(path.read_text() + ' ')
    with pytest.raises(DomainError, match='TEACHING_PACK_INVALID'):
        load_teaching_pack(folder)
    manifest['rubrics'][0]['file'] = '../outside.json'
    (folder / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(DomainError, match='TEACHING_PACK_INVALID'):
        load_teaching_pack(folder)


@pytest.mark.parametrize("profile,model", [("qwen2.5-7b","inkwise-qwen-local"), ("deepseek-r1-7b","inkwise-deepseek-local")])
def test_doctor_accepts_default_model_tag_and_redacts_headers(tmp_path, monkeypatch, profile, model):
    import httpx
    original_client = httpx.Client
    def handler(request):
        assert request.method == 'GET' and request.url.path == '/v1/models'
        return httpx.Response(200, json={'data': [{'id': model+':latest'}]})
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs))
    destination = tmp_path / 'local'
    setup_local(destination, TEACHING, model_profile=profile)
    report = doctor(Settings(_env_file=destination / '.env'), connect=True)
    assert report['ready']
    assert next(item for item in report['checks'] if item['name'] == 'model-catalog')['status'] == 'pass'


def test_cloud_only_and_trocr_configuration_are_reported_consistently(tmp_path):
    from inkwise.ocr import ocr_configured
    from inkwise.settings import model_configured
    destination=tmp_path/'workspace';setup_local(destination,TEACHING)
    model=tmp_path/'trocr';model.mkdir()
    settings=Settings(_env_file=destination/'.env',model_name='',model_base_url='',cloud_enabled=True,
        cloud_model_name='synthetic-cloud',cloud_model_base_url='https://synthetic.invalid/v1',ocr_backend='trocr',ocr_model_path=model)
    assert model_configured(settings) and not ocr_configured(settings)
    for name in ('model.safetensors','config.json','preprocessor_config.json'):(model/name).touch()
    report=doctor(settings)
    assert report['ready'] and next(row for row in report['checks'] if row['name']=='ocr')['status']=='pass'

def test_candidate_readiness_binds_coverage_without_claiming_school_acceptance(tmp_path):
    import shutil
    from inkwise.setup import teaching_readiness
    report = teaching_readiness(TEACHING)
    assert report['syntheticChecksPassed'] is True
    assert report['schoolAccepted'] is False and report['realProviderValidated'] is False
    assert report['teacherApprovalVerified'] is False
    assert report['rubricCount'] == 13 and len(report['coverage']) == 13
    assert all(row['syntheticExamples'] >= 1 for row in report['coverage'])
    assert len(report['manifestSha256']) == 64 and len(report['examplesSha256']) == 64
    assert 'AUTHORIZED_BLIND_TEACHER_REVIEW_REQUIRED' in report['remainingGateCodes']
    assert '"text":' not in json.dumps(report)
    for example in json.loads((TEACHING / 'examples.json').read_text())['examples']:
        assert example['text'] not in json.dumps(report)
    target = tmp_path / 'pack'; shutil.copytree(TEACHING, target)
    examples = json.loads((target / 'examples.json').read_text())
    examples['examples'].pop()
    (target / 'examples.json').write_text(json.dumps(examples))
    with pytest.raises(DomainError, match='TEACHING_COVERAGE_INCOMPLETE'):
        teaching_readiness(target)
