"""Metadata-only capacity checks and bounded reads for retained job caches."""

import json
import os
import shutil
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

from inkwise.errors import DomainError
from inkwise.rubrics import canonical
from inkwise.settings import ORIGINAL_IMAGE_ENTRY_LIMIT

MAX_CACHE_INPUT_BYTES = 8 * 1024 * 1024
MAX_JOB_CACHE_FILES = 128
MAX_STATUS_ENTRIES = 100_000
OCR_WRITE_RESERVE = 96 * 1024 * 1024
IMAGE_ADMISSION_LOCK = '.image-admission.lock'


def _disk_usage(path: Path):
    while not path.exists() and path != path.parent:
        path = path.parent
    return shutil.disk_usage(path)


def _safe_folder(path: Path, *, code='CACHE_STORAGE_INVALID', status=409):
    if path.is_symlink() or path.parent.is_symlink():
        raise DomainError(code, status)


def ensure_free_space(settings, reserve_bytes: int):
    """Preflight local storage only; concurrent/database failures stay transactional."""
    if _disk_usage(settings.data_dir).free < settings.cache_min_free_bytes + reserve_bytes:
        raise DomainError("STORAGE_CAPACITY_EXCEEDED", 507)


def _image_folder(settings):
    folder = settings.data_dir / 'images'
    _safe_folder(folder, code='IMAGE_STORAGE_INVALID', status=503)
    if settings.data_dir.exists():
        info = settings.data_dir.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o022):
            raise DomainError('IMAGE_STORAGE_INVALID', 503)
    if folder.exists():
        info = folder.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise DomainError('IMAGE_STORAGE_INVALID', 503)
    return folder


def _private_image_file(info):
    # Read-only retained originals are safe; new originals are still written 0600.
    return (stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) in (0o400, 0o600))


def original_image_status(settings):
    """One bounded, flat metadata scan; original contents and names never leave here."""
    result = {'files': 0, 'bytes': 0, 'complete': True,
              'maxFiles': settings.image_store_max_files, 'maxBytes': settings.image_store_max_bytes,
              'entryLimit': ORIGINAL_IMAGE_ENTRY_LIMIT}
    try:
        folder = _image_folder(settings)
        if folder.exists():
            with os.scandir(folder) as entries:
                scanned = 0
                for entry in entries:
                    metadata = entry.stat(follow_symlinks=False)
                    if not _private_image_file(metadata):
                        raise ValueError('invalid image storage')
                    if entry.name == IMAGE_ADMISSION_LOCK:
                        if metadata.st_size or stat.S_IMODE(metadata.st_mode) != 0o600:
                            raise ValueError('invalid admission lock')
                        continue
                    scanned += 1
                    if scanned > ORIGINAL_IMAGE_ENTRY_LIMIT:
                        raise ValueError('image entry limit')
                    result['files'] += 1
                    result['bytes'] += metadata.st_size
    except (OSError, ValueError, DomainError):
        result['complete'] = False
    result['hasCapacity'] = (result['complete'] and result['files'] < result['maxFiles']
                             and result['bytes'] < result['maxBytes'])
    return result


class _ImageAdmission:
    def __init__(self, settings, usage):
        self.settings = settings
        self.files = usage['files']
        self.bytes = usage['bytes']

    def reserve(self, size):
        if (self.files + 1 > self.settings.image_store_max_files
                or self.bytes + size > self.settings.image_store_max_bytes):
            raise DomainError('IMAGE_STORAGE_CAPACITY_EXCEEDED', 507)
        ensure_free_space(self.settings, size)
        self.files += 1
        self.bytes += size


@contextmanager
def image_admission(settings):
    """Kernel-released lease serializes API uploads sharing this private workspace.

    This is admission accounting, not an OS quota against unrelated disk writers.
    Scan once per upload (4096 originals maximum), then reserve each normalized page.
    """
    import fcntl
    fd = None
    try:
        folder = _image_folder(settings)
        ensure_free_space(settings, 0)
        settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        _image_folder(settings)
        fd = os.open(folder / IMAGE_ADMISSION_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        info = os.fstat(fd)
        if not _private_image_file(info) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size:
            raise DomainError('IMAGE_STORAGE_INVALID', 503)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DomainError('IMAGE_PROCESSOR_BUSY', 429) from None
        usage = original_image_status(settings)
        if not usage['complete']:
            raise DomainError('IMAGE_STORAGE_INVALID', 503)
        if not usage['hasCapacity']:
            raise DomainError('IMAGE_STORAGE_CAPACITY_EXCEEDED', 507)
        yield _ImageAdmission(settings, usage)
    except OSError:
        raise DomainError('IMAGE_STORAGE_WRITE_FAILED', 503) from None
    finally:
        if fd is not None:
            os.close(fd)


def ensure_cache_capacity(folder: Path, settings, reserve_bytes: int, reserve_files: int = 1):
    """Preflight new work; never evict existing results or promise a disk quota."""
    _safe_folder(folder)
    files = total = 0
    if folder.exists():
        with os.scandir(folder) as entries:
            for entry in entries:
                files += 1
                if files + reserve_files > MAX_JOB_CACHE_FILES:
                    raise DomainError("CACHE_CAPACITY_EXCEEDED", 507)
                metadata = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(metadata.st_mode):
                    raise DomainError("CACHE_STORAGE_INVALID")
                total += metadata.st_size
                if total + reserve_bytes > settings.cache_job_max_bytes:
                    raise DomainError("CACHE_CAPACITY_EXCEEDED", 507)
    if total + reserve_bytes > settings.cache_job_max_bytes:
        raise DomainError("CACHE_CAPACITY_EXCEEDED", 507)
    ensure_free_space(settings, reserve_bytes)


def read_cache_json(path: Path):
    """Reject changed, duplicate-key, non-regular and oversized cache inputs."""
    def unique_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate key")
            value[key] = item
        return value

    def signature(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    try:
        _safe_folder(path.parent)
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_CACHE_INPUT_BYTES:
            raise ValueError("invalid cache file")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            opened = os.fstat(source.fileno())
            if signature(opened) != signature(before):
                raise ValueError("changed cache")
            raw = source.read(MAX_CACHE_INPUT_BYTES + 1)
            if len(raw) != before.st_size or signature(os.fstat(source.fileno())) != signature(before):
                raise ValueError("changed cache")
        if signature(path.lstat()) != signature(before):
            raise ValueError("changed cache")
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_pairs)
        if not isinstance(result, dict):
            raise ValueError("invalid cache object")
        return result
    except (OSError, ValueError, RecursionError):
        raise DomainError("CACHE_STORAGE_INVALID") from None


def storage_status(settings):
    """Bounded aggregate metadata only: no job identifiers, paths or contents."""
    usage = _disk_usage(settings.data_dir)
    result = {"freeBytes": usage.free, "minimumFreeBytes": settings.cache_min_free_bytes,
              "perJobCacheMaxBytes": settings.cache_job_max_bytes,
              "perJobCacheMaxFiles": MAX_JOB_CACHE_FILES, "complete": True,
              "files": 0, "bytes": 0, "jobCacheFolders": 0,
              "cleanup": "operator-approved-erasure-only", "entryLimit": MAX_STATUS_ENTRIES}
    scanned = 0
    try:
        for kind in ("ocr-cache", "routing"):
            root = settings.data_dir / kind
            _safe_folder(root)
            if not root.exists():
                continue
            with os.scandir(root) as jobs:
                for job in jobs:
                    scanned += 1
                    if scanned > MAX_STATUS_ENTRIES:
                        raise ValueError("capacity")
                    if not job.is_dir(follow_symlinks=False):
                        raise ValueError("invalid layout")
                    result["jobCacheFolders"] += 1
                    with os.scandir(job.path) as files:
                        for file in files:
                            scanned += 1
                            if scanned > MAX_STATUS_ENTRIES:
                                raise ValueError("capacity")
                            metadata = file.stat(follow_symlinks=False)
                            if not stat.S_ISREG(metadata.st_mode):
                                raise ValueError("invalid layout")
                            result["files"] += 1
                            result["bytes"] += metadata.st_size
    except (OSError, ValueError, DomainError):
        result["complete"] = False
    result["acceptsNewOcrByDiskHeadroom"] = usage.free >= settings.cache_min_free_bytes + OCR_WRITE_RESERVE
    result['originalImages'] = original_image_status(settings)
    result['complete'] = result['complete'] and result['originalImages']['complete']
    return result


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".release-")
    try:
        with os.fdopen(fd, "w") as file:
            file.write(canonical(value))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(directory_fd)
        finally: os.close(directory_fd)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
