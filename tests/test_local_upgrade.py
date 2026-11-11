"""Synthetic stopped-workspace upgrade checks; no model or current user data."""
import hashlib
import shutil
import sqlite3

import pytest

from inkwise.database import initialize_database
from inkwise.settings import Settings


def workspace(tmp_path):
    root = tmp_path.resolve() / 'workspace'
    root.mkdir(mode=0o700)
    settings = Settings(data_dir=root, database_url=f'sqlite:///{root}/inkwise.sqlite3')
    initialize_database(settings.database_url)
    path = root / 'inkwise.sqlite3'
    path.chmod(0o600)
    return settings, path


def test_old_workspace_needs_verified_backup_before_additive_upgrade(tmp_path):
    from inkwise.local_upgrade import inspect_upgrade
    settings, path = workspace(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute('DROP TABLE revision_assessments')
        db.execute('DROP TABLE writing_revisions')
        db.execute("INSERT INTO tenant_quotas VALUES('synthetic',7)")
    original = path.read_bytes()
    report = inspect_upgrade(settings)
    assert report['status'] == 'upgrade_required'
    assert report['missingTables'] == ['revision_assessments', 'writing_revisions']
    assert not report['canInitialize'] and path.read_bytes() == original
    backup = tmp_path / 'backup.sqlite3'
    shutil.copyfile(path, backup); backup.chmod(0o600)
    report = inspect_upgrade(settings, backup=backup)
    assert report['canInitialize'] and report['backupVerified']
    initialize_database(settings.database_url)
    assert inspect_upgrade(settings)['status'] == 'compatible'
    assert inspect_upgrade(settings)['status'] == 'compatible'
    assert backup.read_bytes() == original
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT revision FROM tenant_quotas').fetchone() == (7,)


def test_bad_or_linked_backup_never_allows_upgrade(tmp_path):
    from inkwise.local_upgrade import inspect_upgrade
    settings, path = workspace(tmp_path)
    with sqlite3.connect(path) as db: db.execute('DROP TABLE revision_assessments')
    backup = tmp_path / 'bad.sqlite3'; backup.write_bytes(b'wrong'); backup.chmod(0o600)
    assert not inspect_upgrade(settings, backup=backup)['canInitialize']
    link = tmp_path / 'linked.sqlite3'; link.symlink_to(path)
    assert not inspect_upgrade(settings, backup=link)['canInitialize']
    assert not inspect_upgrade(settings, backup=path)['canInitialize']


def test_unsupported_nonempty_features_refuse_downgrade(tmp_path):
    from inkwise.local_upgrade import inspect_upgrade
    settings, path = workspace(tmp_path)
    assert inspect_upgrade(settings, target_features=['baseline/v1'])['status'] == 'compatible'
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO writing_revisions(id,tenant_id,classroom_id,job_id,owner_id,idempotency_key,ordinal,source_binding,text,resolutions,note,status,version,created_at,updated_at) VALUES('r','t','c','j','u','i',1,'{}','synthetic','{}','','draft',1,'2026-01-01','2026-01-01')")
    report = inspect_upgrade(settings, target_features=['baseline/v1'])
    assert report['status'] == 'blocked' and report['reason'] == 'downgrade_data_present'
    assert inspect_upgrade(settings)['status'] == 'compatible'


def test_changed_schema_active_sidecars_and_running_workspace_fail_closed(tmp_path):
    from inkwise.local_upgrade import inspect_upgrade
    settings, path = workspace(tmp_path)
    with sqlite3.connect(path) as db: db.execute('ALTER TABLE jobs ADD COLUMN future_field TEXT')
    assert inspect_upgrade(settings)['reason'] == 'schema_mismatch'
    (path.parent / 'running.json').write_text('{}')
    assert inspect_upgrade(settings)['reason'] == 'workspace_running'
    (path.parent / 'running.json').unlink()
    sidecar = path.with_name(path.name + '-wal'); sidecar.write_bytes(b'active')
    assert inspect_upgrade(settings)['reason'] == 'database_not_quiescent'


def test_space_and_database_links_fail_without_mutation(tmp_path, monkeypatch):
    import inkwise.local_upgrade as upgrade
    settings, path = workspace(tmp_path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(upgrade.shutil, 'disk_usage', lambda _: shutil._ntuple_diskusage(100,99,1))
    assert upgrade.inspect_upgrade(settings)['reason'] == 'insufficient_space'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    original = path.with_suffix('.original'); path.rename(original); path.symlink_to(original)
    assert upgrade.inspect_upgrade(settings)['reason'] == 'database_file_invalid'


def test_private_workspace_accepts_default_sqlite_permissions_without_chmod(tmp_path):
    from inkwise.local_upgrade import inspect_upgrade
    settings, path = workspace(tmp_path)
    path.chmod(0o644)
    assert inspect_upgrade(settings)['status'] == 'compatible'
    assert path.stat().st_mode & 0o777 == 0o644
