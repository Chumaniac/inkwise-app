"""Bounded, read-only compatibility inspection for stopped local SQLite workspaces."""
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import stat

from sqlalchemy.dialects.sqlite import dialect

from inkwise.database import Base

FEATURE_TABLES = {'writing-revisions/v1': 'writing_revisions',
                  'revision-reassessment/v1': 'revision_assessments'}
DATA_FEATURES = ['baseline/v1', *FEATURE_TABLES]
MAX_DATABASE_BYTES = 2 * 1024**3


def _file(path, *, private=False):
    path = path.expanduser().absolute()
    if any(p.is_symlink() for p in [path, *path.parents]):
        raise ValueError('database_file_invalid')
    info = path.stat()
    parent = path.parent.stat()
    protected_directory = parent.st_uid == os.getuid() and stat.S_IMODE(parent.st_mode) & 0o077 == 0
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o022 or (private or not protected_directory) and stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > MAX_DATABASE_BYTES):
        raise ValueError('database_file_invalid')
    return path, info


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _digest(path, expected):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(expected):
            raise ValueError('database_changed')
        digest = hashlib.file_digest(stream, 'sha256').digest()
        if _stamp(os.fstat(stream.fileno())) != _stamp(expected):
            raise ValueError('database_changed')
    return digest


def _quiescent(path, settings):
    running = settings.data_dir / 'running.json'
    if running.exists() or running.is_symlink():
        raise ValueError('workspace_running')
    for suffix in ['-wal', '-journal']:
        sidecar = path.with_name(path.name + suffix)
        if sidecar.is_symlink() or sidecar.exists() and sidecar.stat().st_size:
            raise ValueError('database_not_quiescent')


def inspect_upgrade(settings, *, backup: Path | None = None, target_features=None):
    """No writes, initialization, backup creation, network or content-bearing output.

    A 2GiB DB bound makes hashing predictable; larger workspaces need an operator-led
    upgrade. This local helper does not claim remote DB or live-WAL compatibility.
    """
    features = DATA_FEATURES if target_features is None else list(target_features)
    report = {'schema': 'inkwise.local-upgrade/v1', 'readOnly': True, 'status': 'blocked',
              'reason': None, 'targetFeatures': features, 'missingTables': [],
              'populatedFeatures': [], 'backupVerified': False, 'canInitialize': False,
              'databaseBytes': None, 'requiredFreeBytes': None}
    try:
        if (not features or 'baseline/v1' not in features or len(set(features)) != len(features)
                or any(f not in DATA_FEATURES for f in features)
                or 'revision-reassessment/v1' in features and 'writing-revisions/v1' not in features):
            raise ValueError('target_features_invalid')
        if not settings.database_url.startswith('sqlite:///') or '?' in settings.database_url:
            raise ValueError('local_sqlite_required')
        if (settings.data_dir / 'running.json').exists() or (settings.data_dir / 'running.json').is_symlink():
            raise ValueError('workspace_running')
        raw = Path(settings.database_url.removeprefix('sqlite:///'))
        if not raw.exists() and not raw.is_symlink():
            report.update(status='new_database', reason='explicit_initialization_required', canInitialize=True)
            return report
        path, before = _file(raw)
        _quiescent(path, settings)
        report['databaseBytes'] = before.st_size
        report['requiredFreeBytes'] = before.st_size * 2 + 64 * 1024**2
        if shutil.disk_usage(path.parent).free < report['requiredFreeBytes']:
            raise ValueError('insufficient_space')
        required = set(Base.metadata.tables) - set(FEATURE_TABLES.values())
        required.update(FEATURE_TABLES[f] for f in features if f in FEATURE_TABLES)
        with sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True, timeout=2) as db:
            db.execute('PRAGMA query_only=ON')
            if db.execute("SELECT 1 FROM sqlite_master WHERE type IN ('view','trigger') LIMIT 1").fetchone():
                raise ValueError('schema_mismatch')
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 65")}
            if len(tables) > 64 or tables - set(Base.metadata.tables):
                raise ValueError('schema_mismatch')
            for name in tables:
                table = Base.metadata.tables[name]
                actual = {(r[1], r[2].upper(), bool(r[3]) if not r[5] else True, bool(r[5]))
                          for r in db.execute(f'PRAGMA table_info("{name}")')}
                expected = {(c.name, c.type.compile(dialect=dialect()).upper(), not c.nullable, c.primary_key) for c in table.columns}
                if actual != expected:
                    raise ValueError('schema_mismatch')
                unique = {tuple(r[0] for r in db.execute('SELECT name FROM pragma_index_info(?) ORDER BY seqno', (row[1],)))
                          for row in db.execute(f'PRAGMA index_list("{name}")') if row[2]}
                for constraint in table.constraints:
                    if constraint.__class__.__name__ == 'UniqueConstraint' and tuple(c.name for c in constraint.columns) not in unique:
                        raise ValueError('schema_mismatch')
                foreign = {(r[3], r[2], r[4], r[6]) for r in db.execute(f'PRAGMA foreign_key_list("{name}")')}
                if any((c.name, fk.column.table.name, fk.column.name, fk.ondelete or 'NO ACTION') not in foreign for c in table.columns for fk in c.foreign_keys):
                    raise ValueError('schema_mismatch')
            for feature, table in FEATURE_TABLES.items():
                if table in tables and db.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone():
                    report['populatedFeatures'].append(feature)
                    if feature not in features:
                        raise ValueError('downgrade_data_present')
            missing = required - tables
            report['missingTables'] = sorted(missing)
            if missing - set(FEATURE_TABLES.values()):
                raise ValueError('baseline_schema_missing')
        if _stamp(path.stat()) != _stamp(before):
            raise ValueError('database_changed')
        report.update(status='upgrade_required' if missing else 'compatible', reason=None)
        if backup is not None:
            other, backup_info = _file(backup, private=True)
            if (other == path or (backup_info.st_dev, backup_info.st_ino) == (before.st_dev, before.st_ino)
                    or _digest(other, backup_info) != _digest(path, before)):
                raise ValueError('backup_mismatch')
            report['backupVerified'] = True
        _quiescent(path, settings)
        if _stamp(path.stat()) != _stamp(before):
            raise ValueError('database_changed')
        report['canInitialize'] = not missing or report['backupVerified']
    except (OSError, ValueError, sqlite3.Error, KeyError) as error:
        allowed = {'database_file_invalid', 'database_changed', 'target_features_invalid', 'local_sqlite_required',
                   'workspace_running', 'database_not_quiescent', 'insufficient_space', 'schema_mismatch',
                   'downgrade_data_present', 'baseline_schema_missing', 'backup_mismatch'}
        reason = str(error)
        report.update(status='blocked', reason=reason if reason in allowed else 'inspection_unavailable', canInitialize=False)
    return report
