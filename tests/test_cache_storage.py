"""Synthetic capacity checks preserve successes and never inspect content in status."""
import json
from collections import namedtuple

import pytest

from inkwise import cache_storage
from inkwise.cache_storage import ensure_cache_capacity, read_cache_json, storage_status
from inkwise.errors import DomainError
from inkwise.settings import Settings


def test_low_space_stops_new_work_without_deleting_existing_cache(tmp_path, monkeypatch):
    folder = tmp_path / 'ocr-cache' / 'synthetic-job'
    folder.mkdir(parents=True)
    saved = folder / 'saved.json'
    saved.write_text('{"result":"synthetic-success"}')
    settings = Settings(data_dir=tmp_path)
    monkeypatch.setattr(cache_storage, '_disk_usage', lambda _: namedtuple('Usage', 'free')(1))
    with pytest.raises(DomainError, match='STORAGE_CAPACITY_EXCEEDED'):
        ensure_cache_capacity(folder, settings, cache_storage.OCR_WRITE_RESERVE)
    assert read_cache_json(saved)['result'] == 'synthetic-success'
    assert storage_status(settings)['acceptsNewOcrByDiskHeadroom'] is False
    assert saved.exists()


def test_job_file_count_and_bytes_are_bounded(tmp_path):
    settings = Settings(data_dir=tmp_path, cache_job_max_bytes=1024*1024)
    folder = tmp_path / 'routing' / 'synthetic-job'
    folder.mkdir(parents=True)
    for number in range(cache_storage.MAX_JOB_CACHE_FILES):
        (folder / f'{number}.json').write_text('{}')
    with pytest.raises(DomainError, match='CACHE_CAPACITY_EXCEEDED'):
        ensure_cache_capacity(folder, settings, 1)
    with pytest.raises(DomainError, match='CACHE_CAPACITY_EXCEEDED'):
        ensure_cache_capacity(tmp_path / 'empty', settings, 1024*1024+1)


def test_cache_reads_reject_duplicate_keys_symlinks_and_large_files(tmp_path):
    saved = tmp_path / 'saved.json'
    saved.write_text('{"result":1,"result":2}')
    with pytest.raises(DomainError, match='CACHE_STORAGE_INVALID'):
        read_cache_json(saved)
    saved.write_text(json.dumps({'result':'synthetic'}))
    linked = tmp_path / 'linked.json'
    linked.symlink_to(saved)
    with pytest.raises(DomainError, match='CACHE_STORAGE_INVALID'):
        read_cache_json(linked)
    with saved.open('wb') as file:
        file.truncate(cache_storage.MAX_CACHE_INPUT_BYTES+1)
    with pytest.raises(DomainError, match='CACHE_STORAGE_INVALID'):
        read_cache_json(saved)


def test_status_is_metadata_only_bounded_and_marks_partial_totals(tmp_path, monkeypatch):
    folder = tmp_path / 'routing' / 'private-synthetic-id'
    folder.mkdir(parents=True)
    (folder / 'plan.json').write_text('synthetic text that is deliberately not JSON')
    settings = Settings(data_dir=tmp_path)
    report = storage_status(settings)
    assert report['complete'] and report['files'] == 1
    assert 'private-synthetic-id' not in json.dumps(report)
    monkeypatch.setattr(cache_storage, 'MAX_STATUS_ENTRIES', 1)
    assert storage_status(settings)['complete'] is False
