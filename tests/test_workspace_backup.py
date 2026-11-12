"""Stopped synthetic restore roundtrips; no provider, OCR or student material."""
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from inkwise.database import initialize_database
from inkwise.settings import Settings
from inkwise.workspace_backup import backup_workspace, check_workspace, restore_workspace
from inkwise import workspace_files as files


def workspace(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    monkeypatch.chdir(root)
    data = root / 'private'
    data.mkdir(mode=0o700)
    rules = root / 'rubrics'
    rules.mkdir()
    (rules / 'synthetic.json').write_text('{"revision":"synthetic-v1"}')
    reference = root / 'public-reference.json'
    reference.write_text('{"synthetic":true}')
    settings = Settings(data_dir=data, database_url=f'sqlite:///{data}/inkwise.sqlite3',
                        rubric_dir=rules, reference_pack=reference, calibration_dir=data / 'calibration')
    initialize_database(settings.database_url)
    (data / 'inkwise.sqlite3').chmod(0o600)
    (data / 'images').mkdir()
    (data / 'images/synthetic-original.png').write_bytes(b'synthetic PNG bytes')
    (data / 'reports').mkdir()
    (data / 'reports/synthetic.json').write_text('{"textHash":"synthetic-text","imageId":"synthetic-original","rubricRelease":"synthetic-v1","version":3}')
    # A version-bound job is recovered byte-for-byte, including its original evidence.
    with sqlite3.connect(data / 'inkwise.sqlite3') as db:
        db.execute("INSERT INTO jobs(id,tenant_id,classroom_id,owner_id,idempotency_key,request_hash,payload,text,text_hash,text_version,version,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ('synthetic-job', 'synthetic-tenant', 'class', 'teacher', 'request', 'hash', '{"imageId":"synthetic-original","reportVersion":3}', 'synthetic text', 'synthetic-text', 2, 3, 'completed', '2026-11-12', '2026-11-12'))
    return settings, root / 'archive'


def test_bundle_restores_originals_rules_reports_and_database_bindings(tmp_path, monkeypatch):
    settings, archive = workspace(tmp_path, monkeypatch)
    before = (settings.data_dir / 'inkwise.sqlite3').read_bytes()
    result = backup_workspace(settings, archive, stopped=True)
    assert result['status'] == 'backed-up'
    assert check_workspace(archive)['databaseCompatibility'] == 'compatible'
    output = tmp_path.resolve() / 'recovered'
    restored = restore_workspace(archive, output)
    assert restored['status'] == 'restored'
    configured = Settings(_env_file=output / 'restore.env')
    assert configured.cloud_enabled is False and configured.max_cloud_cost_microusd == 0
    assert configured.model_base_url == '' and configured.kafka_enabled is False
    assert configured.database_url != settings.database_url
    assert (configured.data_dir / 'images/synthetic-original.png').read_bytes() == b'synthetic PNG bytes'
    assert (configured.rubric_dir / 'synthetic.json').read_text() == '{"revision":"synthetic-v1"}'
    assert json.loads((configured.data_dir / 'reports/synthetic.json').read_text())['version'] == 3
    with sqlite3.connect(Path(configured.database_url.removeprefix('sqlite:///')).as_uri() + '?mode=ro&immutable=1', uri=True) as db:
        assert db.execute('SELECT tenant_id,text_hash,version,payload FROM jobs').fetchone() == ('synthetic-tenant', 'synthetic-text', 3, '{"imageId":"synthetic-original","reportVersion":3}')
    assert (settings.data_dir / 'inkwise.sqlite3').read_bytes() == before
    assert not (output / 'INCOMPLETE').exists() and (output / 'COMPLETE').exists()


def test_running_workspace_and_missing_frozen_rules_are_refused(tmp_path, monkeypatch):
    settings, archive = workspace(tmp_path, monkeypatch)
    (settings.data_dir / 'running.json').write_text('{}')
    with pytest.raises(ValueError):
        backup_workspace(settings, archive, stopped=True)
    assert not archive.exists()
    (settings.data_dir / 'running.json').unlink()
    settings.rubric_dir = tmp_path.resolve() / 'absent'
    with pytest.raises(ValueError):
        backup_workspace(settings, archive, stopped=True)
    assert not archive.exists()


def test_altered_original_or_layout_never_creates_a_restore_target(tmp_path, monkeypatch):
    settings, archive = workspace(tmp_path, monkeypatch)
    backup_workspace(settings, archive, stopped=True)
    (archive / 'tree/data/images/synthetic-original.png').write_bytes(b'changed')
    output = tmp_path.resolve() / 'recovered'
    with pytest.raises(ValueError):
        restore_workspace(archive, output)
    assert not output.exists()


def test_interruption_is_retained_and_not_published_as_success(tmp_path, monkeypatch):
    settings, archive = workspace(tmp_path, monkeypatch)
    with patch.object(files, '_copy', side_effect=OSError('synthetic storage failure')):
        with pytest.raises(OSError):
            backup_workspace(settings, archive, stopped=True)
    assert (archive / 'INCOMPLETE').is_file()
    assert not (archive / 'COMPLETE').exists()
    with pytest.raises(ValueError):
        check_workspace(archive)


def test_rule_update_during_other_components_copy_invalidates_entire_bundle(tmp_path, monkeypatch):
    settings, archive = workspace(tmp_path, monkeypatch)
    copy = files._copy
    def edit_after_copy(source, destination, metadata, mode=0o600):
        result = copy(source, destination, metadata, mode)
        if Path(source).name == 'synthetic.json' and Path(source).parent == settings.rubric_dir:
            Path(source).write_text('changed synthetic rule revision')
        return result
    with patch.object(files, '_copy', side_effect=edit_after_copy):
        with pytest.raises(ValueError, match='SOURCE_CHANGED'):
            backup_workspace(settings, archive, stopped=True)
    assert (archive / 'INCOMPLETE').exists() and not (archive / 'COMPLETE').exists()


def test_restore_config_failure_does_not_leave_a_complete_recovery(tmp_path, monkeypatch):
    settings, archive = workspace(tmp_path, monkeypatch)
    backup_workspace(settings, archive, stopped=True)
    write = files._write
    def fail_configuration(path, data):
        if Path(path).name == 'restore.env':
            raise OSError('synthetic configuration write failure')
        return write(path, data)
    output = tmp_path.resolve() / 'recovered'
    with patch.object(files, '_write', side_effect=fail_configuration):
        with pytest.raises(OSError):
            restore_workspace(archive, output)
    assert (output / 'INCOMPLETE').exists() and not (output / 'COMPLETE').exists()
    assert check_workspace(archive)['status'] == 'verified'


def test_operator_cli_dispatches_success_and_redacts_invalid_input(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys
    settings, archive = workspace(tmp_path, monkeypatch)
    source = str(Path(__file__).resolve().parents[1] / 'src')
    env = dict(os.environ, PYTHONPATH=source, LANGSMITH_TRACING='false', ANONYMIZED_TELEMETRY='false',
               INKWISE_DATABASE_URL=settings.database_url, INKWISE_DATA_DIR=str(settings.data_dir),
               INKWISE_RUBRIC_DIR=str(settings.rubric_dir), INKWISE_REFERENCE_PACK=str(settings.reference_pack),
               INKWISE_CALIBRATION_DIR=str(settings.calibration_dir))
    command = [sys.executable, '-m', 'inkwise']
    result = subprocess.run(command + ['workspace-backup', '--output', str(archive), '--stopped'], env=env, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['status'] == 'backed-up'
    checked = subprocess.run(command + ['workspace-check', '--backup', str(archive)], env=env, text=True, capture_output=True, timeout=20)
    assert checked.returncode == 0 and json.loads(checked.stdout)['status'] == 'verified'
    denied = subprocess.run(command + ['workspace-backup', '--output', str(tmp_path.resolve() / 'new-output')], env=env, text=True, capture_output=True, timeout=20)
    assert denied.returncode != 0 and 'STOP_WORKSPACE_FIRST' in denied.stderr
    assert 'synthetic text' not in denied.stderr and str(tmp_path) not in denied.stderr
