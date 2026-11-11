"""Synthetic original-image admission preserves old data and failed job versions."""
import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from inkwise import cache_storage, ocr
from inkwise.errors import DomainError
from inkwise.settings import Settings
from test_api import auth, client, create


def png(color='white'):
    output = io.BytesIO()
    Image.new('RGB', (30, 30), color).save(output, format='PNG')
    return output.getvalue()


def upload(http, job, content=None):
    return http.post(f"/api/v1/jobs/{job['id']}/image", headers=auth(),
                     data={'version': str(job['version'])},
                     files={'image': ('synthetic.png', content or png(), 'image/png')})


def test_original_upload_respects_disk_headroom_and_keeps_job(client, monkeypatch):
    job = create(client, 'low-disk-original').json()
    monkeypatch.setattr(cache_storage, '_disk_usage', lambda _: SimpleNamespace(free=1))
    response = upload(client, job)
    assert response.status_code == 507
    assert response.json()['error']['code'] == 'STORAGE_CAPACITY_EXCEEDED'
    current = client.get(f"/api/v1/jobs/{job['id']}", headers=auth()).json()
    assert current['version'] == job['version'] and not current['hasImage']
    assert not list((client.app.state.settings.data_dir / 'images').glob('*.png'))


def test_partial_original_write_is_removed(tmp_path, monkeypatch):
    path = tmp_path / 'images' / 'synthetic.png'
    original = ocr.os.fdopen

    class FailingWrite:
        def __init__(self, fd, mode):
            self.stream = original(fd, mode)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def write(self, data):
            self.stream.write(data[:2])
            raise OSError('synthetic storage failure')

    monkeypatch.setattr(ocr.os, 'fdopen', FailingWrite)
    with pytest.raises(OSError):
        ocr.store_image(path, png())
    assert not path.exists()


def test_status_includes_only_original_image_metadata(tmp_path):
    folder = tmp_path / 'images'
    folder.mkdir(mode=0o700)
    marker = 'retained-synthetic-original'
    content = png()
    (folder / (marker + '.png')).write_bytes(content)
    (folder / (marker + '.png')).chmod(0o600)
    report = cache_storage.storage_status(Settings(data_dir=tmp_path))
    assert report['originalImages']['files'] == 1
    assert report['originalImages']['bytes'] == len(content)
    assert report['originalImages']['complete']
    assert marker not in json.dumps(report)


def test_workspace_file_budget_preserves_old_image_and_scope(client):
    settings = client.app.state.settings
    settings.image_store_max_files = 1
    first = create(client, 'retained-original').json()
    saved = upload(client, first)
    assert saved.status_code == 200
    old = client.get(f"/api/v1/jobs/{first['id']}/image", headers=auth()).content
    second = create(client, 'over-file-budget').json()
    for denied in ('bob', 'eve'):
        assert client.post(f"/api/v1/jobs/{second['id']}/image", headers=auth(denied),
                           data={'version': '1'}, files={'image': ('synthetic.png', png(), 'image/png')}).status_code == 404
    response = upload(client, second, png('blue'))
    assert response.status_code == 507
    assert response.json()['error']['code'] == 'IMAGE_STORAGE_CAPACITY_EXCEEDED'
    assert client.get(f"/api/v1/jobs/{first['id']}/image", headers=auth()).content == old
    assert client.get(f"/api/v1/jobs/{second['id']}", headers=auth()).json()['version'] == 1


def test_normalized_byte_budget_rolls_back_multi_page_upload(client):
    settings = client.app.state.settings
    settings.image_store_max_bytes = len(png())
    job = create(client, 'multi-page-budget').json()
    response = client.post(f"/api/v1/jobs/{job['id']}/pages", headers=auth(), data={'version': '1'},
                           files=[('pages', ('one.png', png(), 'image/png')),
                                  ('pages', ('two.png', png('blue'), 'image/png'))])
    assert response.status_code == 507
    assert not list((settings.data_dir / 'images').glob('*.png'))
    current = client.get(f"/api/v1/jobs/{job['id']}", headers=auth()).json()
    assert current['version'] == 1 and not current['hasImage']
    assert cache_storage.storage_status(settings)['originalImages']['files'] == 0


def test_conflict_and_write_failure_keep_upload_atomic(client, monkeypatch):
    import inkwise.api as api
    job = create(client, 'write-failure').json()
    saved_mutate = api.mutate

    def conflict(*args, **kwargs):
        raise DomainError('VERSION_CONFLICT')

    monkeypatch.setattr(api, 'mutate', conflict)
    assert upload(client, job).status_code == 409
    folder = client.app.state.settings.data_dir / 'images'
    assert not list(folder.glob('*.png'))
    monkeypatch.setattr(api, 'mutate', saved_mutate)

    def failed(*args, **kwargs):
        raise OSError('synthetic write failure')

    monkeypatch.setattr(api, 'store_image', failed)
    response = upload(client, job)
    assert response.status_code == 503
    assert response.json()['error']['code'] == 'IMAGE_STORAGE_WRITE_FAILED'
    assert not list(folder.glob('*.png'))
    assert client.get(f"/api/v1/jobs/{job['id']}", headers=auth()).json()['version'] == 1


def test_storage_lease_is_nonblocking_and_released(tmp_path):
    settings = Settings(data_dir=tmp_path)
    with cache_storage.image_admission(settings):
        with pytest.raises(DomainError, match='IMAGE_PROCESSOR_BUSY'):
            with cache_storage.image_admission(settings):
                pytest.fail('second lease acquired')
    with cache_storage.image_admission(settings) as admission:
        admission.reserve(10)
    assert cache_storage.original_image_status(settings)['files'] == 0


def test_original_metadata_rejects_links_and_stops_at_scan_limit(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    folder = tmp_path / 'images'
    folder.mkdir(mode=0o700)
    saved = tmp_path / 'kept.png'
    saved.write_bytes(png())
    linked = folder / 'linked.png'
    linked.symlink_to(saved)
    assert not cache_storage.original_image_status(settings)['complete']
    with pytest.raises(DomainError, match='IMAGE_STORAGE_INVALID'):
        with cache_storage.image_admission(settings):
            pytest.fail('unsafe store accepted')
    linked.unlink()
    for n in range(3):
        ocr.store_image(folder / f'{n}.png', png())
    monkeypatch.setattr(cache_storage, 'ORIGINAL_IMAGE_ENTRY_LIMIT', 2)
    report = cache_storage.original_image_status(settings)
    assert not report['complete'] and report['files'] == 2 and not report['hasCapacity']
    assert saved.read_bytes() == png()


def test_failed_exclusive_write_never_removes_an_existing_original(tmp_path):
    path = tmp_path / 'saved.png'
    original = png()
    path.write_bytes(original)
    with pytest.raises(FileExistsError):
        ocr.store_image(path, png('blue'))
    assert path.read_bytes() == original


def test_uncertain_database_ack_does_not_erase_a_committed_original(client, monkeypatch):
    import inkwise.api as api
    from sqlalchemy.exc import SQLAlchemyError
    job = create(client, 'unconfirmed-database-ack').json()
    mutate = api.mutate

    def committed_then_disconnected(*args, **kwargs):
        mutate(*args, **kwargs)
        raise SQLAlchemyError('synthetic lost acknowledgement after commit')

    monkeypatch.setattr(api, 'mutate', committed_then_disconnected)
    assert upload(client, job).status_code == 503
    current = client.get(f"/api/v1/jobs/{job['id']}", headers=auth()).json()
    assert current['version'] == 2 and current['hasImage']
    original = client.get(f"/api/v1/jobs/{job['id']}/image", headers=auth())
    assert original.status_code == 200 and original.content == png()


@pytest.mark.parametrize('unsafe', ['directory', 'original', 'lock', 'owner'])
def test_insecure_existing_image_layout_refuses_new_writes(tmp_path, monkeypatch, unsafe):
    settings = Settings(data_dir=tmp_path)
    folder = tmp_path / 'images'
    original = folder / 'kept.png'
    ocr.store_image(original, png())
    lock = folder / cache_storage.IMAGE_ADMISSION_LOCK
    lock.touch(mode=0o600)
    if unsafe == 'directory':
        folder.chmod(0o755)
    elif unsafe == 'original':
        original.chmod(0o644)
    elif unsafe == 'lock':
        lock.chmod(0o644)
    else:
        actual = cache_storage.os.getuid()
        monkeypatch.setattr(cache_storage.os, 'getuid', lambda: actual + 1)
    assert not cache_storage.original_image_status(settings)['complete']
    with pytest.raises(DomainError, match='IMAGE_STORAGE_INVALID'):
        with cache_storage.image_admission(settings):
            pytest.fail('unsafe layout admitted')
    assert original.read_bytes() == png()
