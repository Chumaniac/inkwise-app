import importlib.util
from pathlib import Path
import re
import socket
import threading
import urllib.error
import urllib.request


def test_setup_requires_local_host_origin_and_token_before_transition():
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
    worker = threading.Thread(target=module.serve_setup, args=(port, check), daemon=True)
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
