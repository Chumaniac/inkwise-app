import hashlib
import importlib.util
from io import BytesIO
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('prepare_local_model', Path(__file__).parents[1] / 'scripts/prepare_local_model.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def item(data=b'valid'):
    return ('synthetic.gguf', len(data), hashlib.sha256(data).hexdigest())


def test_verified_existing_model_never_redownloads(tmp_path, monkeypatch):
    (tmp_path / 'synthetic.gguf').write_bytes(b'valid')
    monkeypatch.setattr(module.urllib.request, 'urlopen', lambda *args, **kwargs: pytest.fail('verified asset redownloaded'))
    module.download(tmp_path, item())


def test_existing_mismatched_model_is_preserved(tmp_path):
    path = tmp_path / 'synthetic.gguf'
    path.write_bytes(b'other')
    with pytest.raises(ValueError, match='EXISTING_MODEL_FILE_DIFFERS'):
        module.download(tmp_path, item())
    assert path.read_bytes() == b'other'


def test_partial_link_is_refused_without_touching_target(tmp_path):
    target = tmp_path / 'retained'
    target.write_bytes(b'original')
    (tmp_path / 'synthetic.gguf.part').symlink_to(target)
    with pytest.raises(ValueError, match='MODEL_PARTIAL_PATH_INVALID'):
        module.download(tmp_path, item())
    assert target.read_bytes() == b'original'


@pytest.mark.parametrize('response,code', [(b'too much output', 'MODEL_SIZE_INVALID'), (b'wrong', 'MODEL_CHECKSUM_MISMATCH')])
def test_download_must_match_both_size_and_sha256(tmp_path, monkeypatch, response, code):
    stream = BytesIO(response)
    stream.status = 200
    stream.headers = {}
    monkeypatch.setattr(module.urllib.request, 'urlopen', lambda *args, **kwargs: stream)
    with pytest.raises(ValueError, match=code):
        module.download(tmp_path, item())
    assert not (tmp_path / 'synthetic.gguf').exists()


def test_legacy_and_new_workspace_profiles_have_distinct_identity(tmp_path):
    from inkwise.settings import Settings
    from inkwise.setup import setup_local
    teaching = Path(__file__).parents[1] / 'teaching'
    for profile, name, context in [('qwen2.5-7b', 'inkwise-qwen-local', 32768), ('deepseek-r1-7b', 'inkwise-deepseek-local', 65536)]:
        path = tmp_path / profile
        setup_local(path, teaching, model_profile=profile)
        settings = Settings(_env_file=path / '.env')
        assert settings.model_name == name and settings.model_context_tokens == context


def test_unknown_model_profile_creates_no_workspace(tmp_path):
    from inkwise.errors import DomainError
    from inkwise.setup import setup_local
    path = tmp_path / 'must-not-exist'
    with pytest.raises(DomainError, match='LOCAL_MODEL_PROFILE_UNSUPPORTED'):
        setup_local(path, Path(__file__).parents[1] / 'teaching', model_profile='unsupported')
    assert not path.exists()
