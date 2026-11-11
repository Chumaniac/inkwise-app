"""Optional real loopback launch, authorization and process-shutdown check."""
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from inkwise.setup import setup_local


@pytest.mark.skipif(os.environ.get('INKWISE_RUN_LOCAL') != '1', reason='explicit loopback runtime check')
def test_local_launcher_authentication_and_shutdown(tmp_path):
    root = Path(__file__).resolve().parents[1]
    destination = tmp_path / 'runtime'
    setup_local(destination, root / 'teaching')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    command = [sys.executable, '-m', 'inkwise', '--env-file', str(destination / '.env'), 'local', '--port', str(port)]
    process = subprocess.Popen(command, cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=1) as client:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                assert process.poll() is None, 'launcher exited before readiness'
                try:
                    if client.get('/health/ready').status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(.1)
            else:
                pytest.fail('local API did not become ready')
            assert client.get('/workspace/').status_code == 200
            assert client.get('/api/v1/rubrics').status_code in (401, 403)
            token = (destination / 'teacher-access.txt').read_text().strip()
            response = client.get('/api/v1/rubrics', headers={'Authorization': 'Bearer ' + token})
            assert response.status_code == 200
            assert len(response.json()['items']) == 13
            duplicate = subprocess.run(command, cwd=root, capture_output=True, timeout=5)
            assert duplicate.returncode != 0 and b'LOCAL_PORT_UNAVAILABLE' in duplicate.stderr
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=15) == 0
        with socket.socket() as probe:
            assert probe.connect_ex(('127.0.0.1', port)) != 0, 'API leaked after parent shutdown'
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=15)


def test_local_port_preflight_accepts_time_wait_after_owned_connection_closes():
    import errno
    import socket
    from inkwise.setup import ensure_loopback_port_available
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('127.0.0.1', 0)); server.listen(1)
    port = server.getsockname()[1]
    client = socket.create_connection(('127.0.0.1', port))
    accepted, _ = server.accept()
    accepted.shutdown(socket.SHUT_RDWR); accepted.close()
    client.recv(1); client.close(); server.close()
    # This is the actual old failure: no process listens, but a plain bind fails.
    with socket.socket() as old_probe:
        try:
            old_probe.bind(('127.0.0.1', port))
        except OSError as error:
            assert error.errno == errno.EADDRINUSE
    ensure_loopback_port_available(port)


def test_local_port_preflight_keeps_another_active_listener_untouched():
    import socket
    import pytest
    from inkwise.errors import DomainError
    from inkwise.setup import ensure_loopback_port_available
    with socket.socket() as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(('127.0.0.1', 0)); server.listen(1)
        port = server.getsockname()[1]
        with pytest.raises(DomainError, match='LOCAL_PORT_UNAVAILABLE'):
            ensure_loopback_port_available(port)
        client = socket.create_connection(('127.0.0.1', port))
        accepted, _ = server.accept(); accepted.sendall(b'owned')
        assert client.recv(5) == b'owned'
        accepted.close(); client.close()
def test_owned_launcher_matches_unquoted_paths_with_spaces(tmp_path, monkeypatch):
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location('start_local_owned', Path(__file__).parents[1] / 'scripts/start_local.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'ROOT', tmp_path / 'source with spaces')
    executable = str(tmp_path / 'runtime with spaces/python3')
    monkeypatch.setattr(sys, 'executable', executable)
    monkeypatch.setattr(module, 'process_stamp', lambda pid: 'known-start')
    command = f'{executable} -B {module.ROOT}/scripts/start_local.py --no-open'
    monkeypatch.setattr(module.subprocess, 'check_output', lambda *args, **kwargs: command)
    state = {'pid': 1234, 'started': 'known-start', 'port': 8766}
    assert module.owned_process(state)
    assert not module.owned_process({**state, 'started': 'old-start'})
    command = f'{executable} -B {module.ROOT}/scripts/other.py --no-open'
    assert not module.owned_process(state)


def test_foreign_live_process_metadata_is_preserved(tmp_path):
    import importlib.util
    import os
    spec = importlib.util.spec_from_file_location('start_local_preserve', Path(__file__).parents[1] / 'scripts/start_local.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    running = tmp_path / 'running.json'
    running.write_text('{"synthetic": true}')
    before = running.read_bytes()
    with pytest.raises(SystemExit, match='LOCAL_APP_PROCESS_CHANGED'):
        module.retire_stale_state(running, {'pid': os.getpid(), 'port': 8766})
    assert running.read_bytes() == before

