#!/usr/bin/env python3
"""Bounded file primitives used by the stopped Inkwise workspace bundle.

Derived from the owner-maintained SkillTape snapshot tool; no runtime dependency
on another checkout. Upstream: Chumaniac/skilltape ebae14c, scripts/workspace_backup.py.
Local adaptations: format/limits and deferred restore finalization; no CLI entry.
Keep the strict path, manifest and no-overwrite contracts.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import shutil

MAX_ENTRIES = 10000
MAX_FILE_BYTES = 2 * 1024**3
MAX_TOTAL_BYTES = 8 * 1024**3
MAX_MANIFEST_BYTES = 4 * 1024**2
CHUNK = 1024**2
SCHEMA = 'inkwise.workspace-backup/v1'
MIN_HEADROOM = 64 * 1024**2


class WorkspaceBackupError(ValueError):
    pass


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode)


def _path(value):
    if (not isinstance(value, str) or not value or len(value.encode('utf-8')) > 1024
            or '\\' in value or ':' in value or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or value.startswith('/') or any(p in {'', '.', '..'} for p in value.split('/'))
            or len(value.split('/')) > 64):
        raise WorkspaceBackupError('INVALID_PORTABLE_PATH')
    return value


def _scoped_path(value):
    # The operator selects a trusted context by changing directory before invocation.
    # Normalize before checking: string-prefix checks without a separator are unsafe.
    basedir = os.path.realpath(os.getcwd())
    filepath = os.path.realpath(os.path.abspath(os.path.expanduser(value)))
    prefix = basedir.rstrip(os.sep) + os.sep
    if filepath == basedir:
        return Path(basedir)
    if not filepath.startswith(prefix):
        raise WorkspaceBackupError('PATH_OUTSIDE_INVOCATION_ROOT')
    return Path(filepath)


def _root(path):
    path = _scoped_path(path)
    if path.is_symlink() or not path.is_dir():
        raise WorkspaceBackupError('REGULAR_DIRECTORY_REQUIRED')
    return path.resolve(strict=True)


def _inventory(root):
    result, todo, total = {}, [(root, '')], 0
    while todo:
        folder, prefix = todo.pop()
        with os.scandir(folder) as entries:
            for entry in entries:
                name = _path(prefix + entry.name)
                info = entry.stat(follow_symlinks=False)
                if len(result) >= MAX_ENTRIES:
                    raise WorkspaceBackupError('ENTRY_LIMIT')
                if entry.is_symlink() or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                    raise WorkspaceBackupError('LINK_OR_SPECIAL_FILE')
                if hasattr(os, 'getuid') and info.st_uid != os.getuid():
                    raise WorkspaceBackupError('WORKSPACE_OWNER_MISMATCH')
                kind = 'directory' if stat.S_ISDIR(info.st_mode) else 'file'
                if kind == 'file':
                    if info.st_nlink != 1:
                        raise WorkspaceBackupError('HARD_LINK_NOT_SUPPORTED')
                    if (entry.name in {'running.json', '.workspace.lock', 'workspace.lock'}
                            or entry.name.endswith(('-wal', '-journal')) and info.st_size):
                        raise WorkspaceBackupError('WORKSPACE_MAY_BE_RUNNING')
                    total += info.st_size
                    if info.st_size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                        raise WorkspaceBackupError('BYTE_LIMIT')
                result[name] = (kind, info)
                if kind == 'directory':
                    todo.append((Path(entry.path), name + '/'))
    return result


def _sync_directory(path):
    if os.name == 'nt':
        return  # Node/stdlib do not provide equivalent directory flushing on Windows.
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def _copy(source, target, expected, mode=0o600):
    descriptor = os.open(source, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    digest = hashlib.sha256()
    with os.fdopen(descriptor, 'rb') as reader:
        if _stamp(os.fstat(reader.fileno())) != _stamp(expected):
            raise WorkspaceBackupError('SOURCE_CHANGED')
        writer_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        count = 0
        with os.fdopen(writer_fd, 'wb') as writer:
            while True:
                chunk = reader.read(CHUNK)
                if not chunk:
                    break
                count += len(chunk)
                if count > expected.st_size or count > MAX_FILE_BYTES:
                    raise WorkspaceBackupError('SOURCE_CHANGED')
                digest.update(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        if (count != expected.st_size or _stamp(os.fstat(reader.fileno())) != _stamp(expected)
                or _stamp(Path(source).lstat()) != _stamp(expected)):
            raise WorkspaceBackupError('SOURCE_CHANGED')
    os.chmod(target, mode & 0o777)
    return digest.hexdigest()


def _destination(value, protected, required_bytes=0):
    raw = _scoped_path(value)
    if raw.exists() or raw.is_symlink():
        raise WorkspaceBackupError('DESTINATION_EXISTS')
    target = raw.parent.resolve(strict=True) / raw.name
    if target == protected or protected in target.parents:
        raise WorkspaceBackupError('DESTINATION_INSIDE_SOURCE')
    if shutil.disk_usage(target.parent).free < required_bytes + MIN_HEADROOM:
        raise WorkspaceBackupError('INSUFFICIENT_SPACE')
    target.mkdir(mode=0o700)  # Exclusive creation, never replace another directory.
    _write(target / 'INCOMPLETE', b'Operation has not completed. Preserve this directory.\n')
    return target


def _finish(target, report_name, data):
    _write(target / report_name, data)
    _write(target / 'COMPLETE', hashlib.sha256(data).hexdigest().encode('ascii'))
    _sync_directory(target)
    (target / 'INCOMPLETE').unlink()
    _sync_directory(target)


def backup(source, output, *, stopped=False):
    if not stopped:
        raise WorkspaceBackupError('STOP_WORKSPACE_FIRST')
    source = _root(source)
    root_stamp = _stamp(source.stat())
    inventory = _inventory(source)
    records = []
    for name, (kind, info) in sorted(inventory.items()):
        record = {'path': name, 'kind': kind, 'mode': stat.S_IMODE(info.st_mode) & 0o777}
        if kind == 'file':
            record.update(bytes=info.st_size, sha256='0' * 64)
        records.append(record)
    manifest = {'schema': SCHEMA, 'createdAt': datetime.now(timezone.utc).isoformat(),
                'scope': 'stopped-files-only', 'entries': records,
                'totalBytes': sum(v[1].st_size for v in inventory.values() if v[0] == 'file')}
    if len(json.dumps(manifest, ensure_ascii=False).encode('utf-8')) > MAX_MANIFEST_BYTES:
        raise WorkspaceBackupError('MANIFEST_LIMIT')
    target = _destination(output, source, manifest['totalBytes'] + MAX_MANIFEST_BYTES)
    tree = target / 'tree'
    tree.mkdir(mode=0o700)
    for record in records:
        destination = tree / record['path']
        if record['kind'] == 'directory':
            destination.mkdir(mode=0o700)
        else:
            record['sha256'] = _copy(source / record['path'], destination, inventory[record['path']][1])
    after = _inventory(source)
    if (_stamp(source.stat()) != root_stamp or set(after) != set(inventory)
            or any(after[p][0] != kind or _stamp(after[p][1]) != _stamp(info) for p, (kind, info) in inventory.items())):
        raise WorkspaceBackupError('SOURCE_CHANGED')
    for folder in sorted((tree / r['path'] for r in records if r['kind'] == 'directory'), key=lambda p: len(p.parts), reverse=True):
        _sync_directory(folder)
    _sync_directory(tree)
    _finish(target, 'MANIFEST.json', json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode('utf-8'))
    return {'status': 'backed-up', 'entries': len(records), 'bytes': manifest['totalBytes']}


def _pairs(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise WorkspaceBackupError('DUPLICATE_JSON_KEY')
        result[name] = value
    return result


def _read_bounded(path, limit):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
        raise WorkspaceBackupError('INVALID_METADATA_FILE')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise WorkspaceBackupError('SOURCE_CHANGED')
        data = stream.read(limit + 1)
        if len(data) != before.st_size or _stamp(os.fstat(stream.fileno())) != _stamp(before) or _stamp(path.lstat()) != _stamp(before):
            raise WorkspaceBackupError('SOURCE_CHANGED')
    return data


def _manifest(root):
    if (root / 'INCOMPLETE').exists() or (root / 'INCOMPLETE').is_symlink():
        raise WorkspaceBackupError('INCOMPLETE_BACKUP')
    raw = _read_bounded(root / 'MANIFEST.json', MAX_MANIFEST_BYTES)
    if _read_bounded(root / 'COMPLETE', 64) != hashlib.sha256(raw).hexdigest().encode('ascii'):
        raise WorkspaceBackupError('MANIFEST_DIGEST_MISMATCH')
    data = json.loads(raw, object_pairs_hook=_pairs, parse_constant=lambda _: (_ for _ in ()).throw(WorkspaceBackupError('INVALID_JSON')))
    if (not isinstance(data, dict) or set(data) != {'schema', 'createdAt', 'scope', 'entries', 'totalBytes'}
            or data['schema'] != SCHEMA or data['scope'] != 'stopped-files-only'
            or not isinstance(data['createdAt'], str) or len(data['createdAt']) > 40
            or datetime.fromisoformat(data['createdAt']).tzinfo is None
            or not isinstance(data['entries'], list) or len(data['entries']) > MAX_ENTRIES
            or type(data['totalBytes']) is not int):
        raise WorkspaceBackupError('INVALID_MANIFEST')
    names, total = {}, 0
    for record in data['entries']:
        if not isinstance(record, dict):
            raise WorkspaceBackupError('INVALID_ENTRY')
        name, kind = _path(record.get('path')), record.get('kind')
        fields = {'path', 'kind', 'mode'} | ({'bytes', 'sha256'} if kind == 'file' else set())
        if (name in names or kind not in {'file', 'directory'} or set(record) != fields
                or type(record['mode']) is not int or not 0 <= record['mode'] <= 0o777):
            raise WorkspaceBackupError('INVALID_ENTRY')
        names[name] = record
        if kind == 'file':
            size, digest = record['bytes'], record['sha256']
            if (type(size) is not int or not 0 <= size <= MAX_FILE_BYTES or not isinstance(digest, str)
                    or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest)):
                raise WorkspaceBackupError('INVALID_ENTRY')
            total += size
    if total != data['totalBytes'] or total > MAX_TOTAL_BYTES:
        raise WorkspaceBackupError('BYTE_LIMIT')
    for name in names:
        parent = PurePosixPath(name).parent
        while str(parent) != '.':
            if names.get(str(parent), {}).get('kind') != 'directory':
                raise WorkspaceBackupError('MISSING_PARENT')
            parent = parent.parent
    return data


def check(backup_path):
    root = _root(backup_path)
    data = _manifest(root)
    inventory = _inventory(_root(root / 'tree'))
    if set(inventory) != {r['path'] for r in data['entries']}:
        raise WorkspaceBackupError('PAYLOAD_CONTENT_MISMATCH')
    for record in data['entries']:
        kind, info = inventory[record['path']]
        if kind != record['kind']:
            raise WorkspaceBackupError('PAYLOAD_CONTENT_MISMATCH')
        if kind == 'file':
            path = root / 'tree' / record['path']
            descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(descriptor, 'rb') as source:
                if _stamp(os.fstat(source.fileno())) != _stamp(info):
                    raise WorkspaceBackupError('SOURCE_CHANGED')
                digest = hashlib.sha256()
                count = 0
                while True:
                    chunk = source.read(min(CHUNK, info.st_size + 1 - count))
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > info.st_size:
                        raise WorkspaceBackupError('SOURCE_CHANGED')
                    digest.update(chunk)
                digest = digest.hexdigest()
                if _stamp(os.fstat(source.fileno())) != _stamp(info) or _stamp(path.lstat()) != _stamp(info):
                    raise WorkspaceBackupError('SOURCE_CHANGED')
            if info.st_size != record['bytes'] or digest != record['sha256']:
                raise WorkspaceBackupError('PAYLOAD_DIGEST_MISMATCH')
    return data


def restore(backup_path, output, *, finalize=True):
    root = _root(backup_path)
    data = check(root)  # Validate all input before creating any destination.
    inventory = _inventory(root / 'tree')
    target = _destination(output, root, data['totalBytes'])
    workspace = target / 'workspace'
    workspace.mkdir(mode=0o700)
    for record in sorted(data['entries'], key=lambda r: r['path']):
        path = workspace / record['path']
        if record['kind'] == 'directory':
            path.mkdir(mode=0o700)
        else:
            digest = _copy(root / 'tree' / record['path'], path, inventory[record['path']][1], record['mode'])
            if digest != record['sha256']:
                raise WorkspaceBackupError('PAYLOAD_DIGEST_MISMATCH')
    for record in sorted(data['entries'], key=lambda r: len(PurePosixPath(r['path']).parts), reverse=True):
        if record['kind'] == 'directory':
            path = workspace / record['path']
            _sync_directory(path)
            os.chmod(path, record['mode'])
    _sync_directory(workspace)
    result = {'status': 'restored', 'entries': len(data['entries']), 'bytes': data['totalBytes'], 'workspace': 'workspace'}
    if finalize:
        _finish(target, 'RESTORE.json', json.dumps(result, sort_keys=True).encode('utf-8'))
    return result
