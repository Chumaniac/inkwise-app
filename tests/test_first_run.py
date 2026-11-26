import importlib.util
from pathlib import Path
import re
import socket
import threading
import urllib.error
import urllib.request
import pytest


@pytest.mark.parametrize('cloud', [False, True])
def test_setup_requires_local_host_origin_and_token_before_transition(cloud):
    spec = importlib.util.spec_from_file_location('first_run', Path(__file__).parents[1] / 'scripts/first_run.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    calls = []
    def check():
        calls.append(True)
        return {'ready': True, 'checks': []}
    options = {'model_name': 'deepseek-flash', 'model_base_url': 'https://api.deepseek.com'} if cloud else {}
    worker = threading.Thread(target=module.serve_setup, args=(port, check), kwargs=options, daemon=True)
    worker.start()
    import time
    origin = f'http://127.0.0.1:{port}'
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(100):
        try:
            page = client.open(origin + '/workspace/', timeout=1).read().decode()
            break
        except OSError:
            time.sleep(.01)
    else:
        raise AssertionError('setup server did not start')
    token = re.search('nonce="([A-Za-z0-9_-]+)"', page).group(1)
    if cloud:
        assert 'DeepSeek API' in page and 'CLOUD_MODEL_API_KEY' in page
        assert 'ollama pull' not in page
        assert not calls
    for headers in [{'Host': 'untrusted.example'}, {'Origin': 'https://untrusted.example', 'X-Setup-Token': token}, {'Origin': origin, 'X-Setup-Token': 'wrong'}]:
        request = urllib.request.Request(origin + '/setup/check', data=b'', headers=headers, method='POST')
        try:
            client.open(request, timeout=2)
        except urllib.error.HTTPError as error:
            assert error.code == 403
        else:
            raise AssertionError('unauthorized setup transition accepted')
    assert not calls
    request = urllib.request.Request(origin + '/setup/check', data=b'', headers={'Origin': origin, 'X-Setup-Token': token}, method='POST')
    assert client.open(request, timeout=2).read() == b'{"ready": true, "failed": []}'
    worker.join(3)
    assert not worker.is_alive()
    assert len(calls) == 1


def test_deepseek_recheck_reads_saved_settings_and_pins_the_destination(tmp_path):
    from inkwise.settings import Settings
    from scripts.first_run import recheck_deepseek

    config = tmp_path / '.env'
    base = 'INKWISE_CLOUD_MODEL_BASE_URL=https://api.deepseek.com\nINKWISE_CLOUD_MODEL_NAME=deepseek-flash\n'
    config.write_text(base)
    original = Settings(_env_file=config)
    probes = []
    def catalog(settings, *, connect):
        probes.append((settings.cloud_model_name, connect))
        return {'ready': True, 'checks': []}

    assert not recheck_deepseek(config, original, catalog)['ready']
    assert not probes
    ready = base + ('INKWISE_CLOUD_ENABLED=true\nINKWISE_CLOUD_MODEL_API_KEY=synthetic-placeholder\n'
                    'INKWISE_MAX_CLOUD_COST_MICROUSD=1000000\nINKWISE_CLOUD_MODEL_REVISION=test\n'
                    'INKWISE_CLOUD_INPUT_PRICE_MICROUSD_PER_MILLION=300000\nINKWISE_CLOUD_OUTPUT_PRICE_MICROUSD_PER_MILLION=1200000\n')
    config.write_text(ready)
    assert recheck_deepseek(config, original, catalog)['ready']
    config.write_text(ready.replace('api.deepseek.com', 'unreviewed.invalid'))
    assert not recheck_deepseek(config, original, catalog)['ready']
    assert probes == [('deepseek-flash', True)]
