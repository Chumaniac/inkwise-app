import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]


def script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_allowlist_excludes_private_data_and_unsafe_paths():
    module = script('package_local_release')
    for name in ['.env', 'data/private/teacher-access.txt', 'artifacts/raw-report.json',
                 'src/inkwise/.env', '../outside', 'web/../data', 'web/node_modules/private.json']:
        assert not module.allowed(name)
    assert module.allowed('web/assets/fonts/inter-latin-wght-normal.woff2')


def test_distribution_is_deterministic_and_never_overwrites(tmp_path):
    module = script('package_local_release')
    files = {'web/index.html': b'original synthetic UI', 'pyproject.toml': b'[project]\nversion="0.6.0"\n'}
    first = module.assemble(files, '0.6.0', 'a' * 40, 'source', tmp_path / 'first')
    second = module.assemble(files, '0.6.0', 'a' * 40, 'source', tmp_path / 'second')
    original = first.read_bytes()
    assert original == second.read_bytes()
    with pytest.raises(FileExistsError):
        module.assemble(files, '0.6.0', 'a' * 40, 'source', tmp_path / 'first')
    assert first.read_bytes() == original
    with zipfile.ZipFile(first) as archive:
        manifest = json.loads(archive.read('inkwise-k12-0.6.0/distribution.json'))
        assert manifest['source_sha'] == 'a' * 40
        assert manifest['data_features'] == ['baseline/v1', 'writing-revisions/v1', 'revision-reassessment/v1']
        assert manifest['local_upgrade']['automatic_migration'] is False
        assert manifest['files']['web/index.html'] == hashlib.sha256(files['web/index.html']).hexdigest()


def test_native_target_requires_explicit_native_asset(tmp_path):
    module = script('package_local_release')
    with pytest.raises(ValueError, match='NATIVE_ASSET'):
        module.assemble({'web/index.html': b'synthetic'}, '0.6.0', 'a' * 40, 'macos-arm64', tmp_path)


def test_installed_manifest_detects_tampering_and_path_escape(tmp_path):
    module = script('start_local')
    root = tmp_path.resolve() / 'installed'
    (root / 'web').mkdir(parents=True)
    path = root / 'web/index.html'
    path.write_bytes(b'original')
    manifest = root / 'distribution.json'
    manifest.write_text(json.dumps({'files': {'web/index.html': hashlib.sha256(b'original').hexdigest()}}))
    module.verify_distribution(root)
    path.write_bytes(b'changed')
    with pytest.raises(ValueError, match='DIGEST_MISMATCH'):
        module.verify_distribution(root)
    manifest.write_text(json.dumps({'files': {'../private': '0' * 64}}))
    with pytest.raises(ValueError, match='PATH_INVALID'):
        module.verify_distribution(root)


def test_stop_does_not_claim_another_process(monkeypatch):
    module = script('start_local')
    monkeypatch.setattr(module.subprocess, 'check_output', lambda *a, **k: '/another/application')
    assert not module.owned_process({'pid': 999999, 'started': 'synthetic-time', 'port': 8766})


@pytest.mark.parametrize('command,directory,expected', [
    ('python3 scripts/start_local.py --port 8766', ROOT, True),
    ('python3 scripts/start_local.py --port 8766', ROOT.parent, False),
    ('python3 other.py scripts/start_local.py', ROOT, False),
    (f'python3 {ROOT}/scripts/start_local.py --port 8766', ROOT, True),
    (f'python3 -u {ROOT}/scripts/start_local.py --port 8766', ROOT, True),
    (f'python3 other.py {ROOT}/scripts/start_local.py', ROOT, False),
])
def test_relative_launcher_ownership_requires_its_actual_checkout(monkeypatch, command, directory, expected):
    module = script('start_local')
    monkeypatch.setattr(module.subprocess, 'check_output', lambda *a, **k: command)
    monkeypatch.setattr(module, 'process_stamp', lambda pid: 'synthetic-time')
    monkeypatch.setattr(module, 'process_directory', lambda pid: directory, raising=False)
    state = {'pid': 123, 'started': 'synthetic-time', 'port': 8766}
    assert module.owned_process(state) is expected


@pytest.mark.parametrize('state', [None, {}, {'pid': -1},
    {'pid': 123, 'started': 'synthetic', 'port': 80},
    {'pid': '123', 'started': 'synthetic', 'port': 8766}])
def test_stop_rejects_malformed_process_metadata(state):
    assert not script('start_local').owned_process(state)


def test_source_startup_reuses_the_existing_native_ocr_build(tmp_path):
    module = script('start_local')
    root = tmp_path / 'checkout'
    (root / '.git').mkdir(parents=True)
    (root / 'artifacts').mkdir()
    native = root / 'artifacts/inkwise-vision'
    native.write_bytes(b'synthetic executable metadata only')
    native.chmod(0o755)
    assert module.find_vision_executable(root) == native
    (root / 'bin').mkdir()
    packaged = root / 'bin/inkwise-vision'
    packaged.write_bytes(b'packaged executable metadata')
    assert module.find_vision_executable(root) == packaged


def test_source_ocr_fallback_does_not_expand_the_release_or_link_boundary(tmp_path):
    module = script('start_local')
    (tmp_path / 'artifacts').mkdir()
    native = tmp_path / 'artifacts/inkwise-vision'
    native.write_bytes(b'synthetic metadata')
    native.chmod(0o755)
    assert module.find_vision_executable(tmp_path) == tmp_path / 'bin/inkwise-vision'
    (tmp_path / '.git').mkdir()
    (tmp_path / 'distribution.json').write_text('{}')
    assert module.find_vision_executable(tmp_path) == tmp_path / 'bin/inkwise-vision'
    (tmp_path / 'distribution.json').unlink()
    native.chmod(0o644)
    assert module.find_vision_executable(tmp_path) == tmp_path / 'bin/inkwise-vision'
    native.unlink()
    native.symlink_to(__file__)
    assert module.find_vision_executable(tmp_path) == tmp_path / 'bin/inkwise-vision'
