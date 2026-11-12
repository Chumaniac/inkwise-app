"""Stopped local SQLite + original files + frozen rules, never live/remote backup."""
from datetime import datetime, timezone
import json
import hashlib
from pathlib import Path
import stat

from inkwise import workspace_files as files
from inkwise.local_upgrade import _file, _quiescent, inspect_upgrade
from inkwise.settings import Settings

LAYOUT = '.inkwise-workspace.json'
EXTERNAL = ['model-artifacts', 'external-indexes', 'provider-credentials', 'deployment-configuration']


def _database(settings):
    if not settings.database_url.startswith('sqlite:///') or '?' in settings.database_url:
        raise files.WorkspaceBackupError('LOCAL_SQLITE_REQUIRED')
    try:
        path, _ = _file(files._scoped_path(Path(settings.database_url.removeprefix('sqlite:///'))))
        _quiescent(path, settings)
    except ValueError as error:
        known = {'workspace_running', 'database_not_quiescent', 'database_file_invalid', 'database_changed'}
        if str(error) in known:
            raise files.WorkspaceBackupError(str(error).upper()) from None
        raise
    report = inspect_upgrade(settings)
    if report['status'] not in {'compatible', 'upgrade_required'}:
        raise files.WorkspaceBackupError('WORKSPACE_COMPATIBILITY_BLOCKED')
    return path.resolve(), report


def _layout(raw):
    value = json.loads(raw, object_pairs_hook=files._pairs)
    if (not isinstance(value, dict) or set(value) != {'schema', 'roles', 'externalDependencies'}
            or value['schema'] != 'inkwise.workspace-layout/v1' or value['externalDependencies'] != EXTERNAL
            or not isinstance(value['roles'], dict)
            or set(value['roles']) != {'data', 'database', 'rubrics', 'reference', 'calibration'}):
        raise files.WorkspaceBackupError('WORKSPACE_LAYOUT_INVALID')
    for name, path in value['roles'].items():
        if name == 'calibration' and path is None:
            continue
        files._path(path)
    return value


def _settings(root, layout):
    roles = layout['roles']
    return Settings(data_dir=root / roles['data'], database_url=f"sqlite:///{root / roles['database']}",
                    rubric_dir=root / roles['rubrics'], reference_pack=root / roles['reference'],
                    calibration_dir=root / (roles['calibration'] or 'data/calibration'))


def backup_workspace(settings, output, *, stopped=False):
    if not stopped:
        raise files.WorkspaceBackupError('STOP_WORKSPACE_FIRST')
    data_root = files._root(settings.data_dir)
    database, _ = _database(settings)
    roots = [('data', data_root)]
    roles = {'data': 'data'}
    single_files = []

    def location(path, label, *, directory=True):
        path = files._root(path) if directory else files._scoped_path(path)
        for prefix, root in roots:
            if path == root or path.is_relative_to(root):
                return prefix + (('/' + path.relative_to(root).as_posix()) if path != root else '')
        if directory:
            roots.append((label, path))
            return label
        name = label + '/' + path.name
        files._path(name)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > files.MAX_FILE_BYTES:
            raise files.WorkspaceBackupError('INVALID_COMPONENT_FILE')
        single_files.append((name, path, info))
        return name

    roles['database'] = location(database, 'database', directory=False)
    roles['rubrics'] = location(settings.rubric_dir, 'rubrics')
    roles['reference'] = location(settings.reference_pack, 'reference', directory=False)
    calibration = Path(settings.calibration_dir)
    roles['calibration'] = location(calibration, 'calibration') if calibration.exists() else None
    layout = {'schema': 'inkwise.workspace-layout/v1', 'roles': roles, 'externalDependencies': EXTERNAL}
    layout_raw = json.dumps(layout, sort_keys=True).encode('utf-8')
    _layout(layout_raw)
    records, source_files, before = [], {}, {}
    for prefix, root in roots:
        before[prefix] = (files._stamp(root.stat()), files._inventory(root))
        records.append({'path': prefix, 'kind': 'directory', 'mode': 0o700})
        for name, (kind, info) in sorted(before[prefix][1].items()):
            relative = prefix + '/' + name
            record = {'path': relative, 'kind': kind, 'mode': stat.S_IMODE(info.st_mode) & 0o777}
            if kind == 'file':
                record.update(bytes=info.st_size, sha256='0' * 64)
                source_files[relative] = (root / name, info)
            records.append(record)
    known = {r['path'] for r in records}
    for name, source, info in single_files:
        parent = str(Path(name).parent)
        if parent not in known:
            records.append({'path': parent, 'kind': 'directory', 'mode': 0o700})
            known.add(parent)
        records.append({'path': name, 'kind': 'file', 'mode': 0o600, 'bytes': info.st_size, 'sha256': '0' * 64})
        source_files[name] = (source, info)
    records.append({'path': LAYOUT, 'kind': 'file', 'mode': 0o600, 'bytes': len(layout_raw), 'sha256': hashlib.sha256(layout_raw).hexdigest()})
    total = sum(r.get('bytes', 0) for r in records)
    manifest = {'schema': files.SCHEMA, 'scope': 'stopped-files-only', 'createdAt': datetime.now(timezone.utc).isoformat(), 'entries': records, 'totalBytes': total}
    if (len(records) > files.MAX_ENTRIES or total > files.MAX_TOTAL_BYTES
            or len(json.dumps(manifest).encode()) > files.MAX_MANIFEST_BYTES):
        raise files.WorkspaceBackupError('WORKSPACE_BUNDLE_LIMIT')
    output_path = files._scoped_path(output)
    for _, root in roots:
        if output_path == root or output_path.is_relative_to(root):
            raise files.WorkspaceBackupError('DESTINATION_INSIDE_SOURCE')
    target = files._destination(output_path, roots[0][1], total + files.MAX_MANIFEST_BYTES)
    tree = target / 'tree'
    tree.mkdir(mode=0o700)
    for record in sorted(records, key=lambda r: r['path']):
        destination = tree / record['path']
        if record['kind'] == 'directory':
            destination.mkdir(mode=0o700)
        elif record['path'] == LAYOUT:
            files._write(destination, layout_raw)
        else:
            source, metadata = source_files[record['path']]
            record['sha256'] = files._copy(source, destination, metadata)
    _database(settings)
    for prefix, root in roots:
        root_stamp, initial = before[prefix]
        current = files._inventory(root)
        if (files._stamp(root.stat()) != root_stamp or set(current) != set(initial)
                or any(files._stamp(current[p][1]) != files._stamp(info) for p, (_, info) in initial.items())):
            raise files.WorkspaceBackupError('SOURCE_CHANGED')
    for source, info in source_files.values():
        if files._stamp(source.lstat()) != files._stamp(info):
            raise files.WorkspaceBackupError('SOURCE_CHANGED')
    for folder in sorted((tree / r['path'] for r in records if r['kind'] == 'directory'), key=lambda p: len(p.parts), reverse=True):
        files._sync_directory(folder)
    files._sync_directory(tree)
    files._finish(target, 'MANIFEST.json', json.dumps(manifest, sort_keys=True, indent=2).encode())
    return {'status': 'backed-up', 'entries': len(records), 'bytes': total, 'externalDependencies': EXTERNAL}


def check_workspace(path):
    root = files._root(path)
    manifest = files.check(root)
    layout = _layout(files._read_bounded(root / 'tree' / LAYOUT, 16384))
    roles = layout['roles']
    declared = {r['path']: r['kind'] for r in manifest['entries']}
    for role, name in roles.items():
        if name is None:
            continue
        expected = 'file' if role in {'database', 'reference'} else 'directory'
        if declared.get(name) != expected:
            raise files.WorkspaceBackupError('WORKSPACE_LAYOUT_BINDING_INVALID')
    _, report = _database(_settings(root / 'tree', layout))
    return {'status': 'verified', 'entries': len(manifest['entries']), 'bytes': manifest['totalBytes'],
            'databaseCompatibility': report['status'], 'externalDependencies': EXTERNAL}


def restore_workspace(path, output):
    root = files._root(path)
    check_workspace(root)
    layout = _layout(files._read_bounded(root / 'tree' / LAYOUT, 16384))
    # The file layer validates every item again and only writes into a new directory.
    result = files.restore(root, output, finalize=False)
    target = files._scoped_path(output)
    workspace = target / 'workspace'
    _, compatibility = _database(_settings(workspace, layout))
    roles = layout['roles']
    configuration = {'DATA_DIR': str(workspace / roles['data']), 'DATABASE_URL': f"sqlite:///{workspace / roles['database']}",
                     'RUBRIC_DIR': str(workspace / roles['rubrics']), 'REFERENCE_PACK': str(workspace / roles['reference']),
                     'CALIBRATION_DIR': str(workspace / (roles['calibration'] or 'data/calibration')),
                     'CLOUD_ENABLED': False, 'MAX_CLOUD_COST_MICROUSD': 0, 'MODEL_BASE_URL': '', 'MODEL_API_KEY': '',
                     'KAFKA_ENABLED': False, 'EVOLUTION_ENABLED': False, 'OCR_BACKEND': 'disabled',
                     'NEO4J_URI': '', 'NEO4J_PASSWORD': '', 'CHROMA_HOST': '', 'EMBEDDING_BASE_URL': ''}
    # Explicit config is inert. No service/worker starts and old private config is not executed.
    content = ''.join('INKWISE_' + name + '=' + json.dumps(value, ensure_ascii=False) + '\n' for name, value in configuration.items())
    files._write(target / 'restore.env', content.encode())
    result = {**result, 'databaseCompatibility': compatibility['status'], 'configuration': 'restore.env', 'externalDependencies': EXTERNAL}
    files._finish(target, 'RESTORE.json', json.dumps(result, sort_keys=True).encode())
    return result
