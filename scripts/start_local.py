"""Start an installed local workspace without exposing or replacing private state."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parents[1]


def verify_distribution(root: Path) -> None:
    manifest = root / "distribution.json"
    if not manifest.exists():
        if (root / ".git").exists():
            return
        raise ValueError("DISTRIBUTION_MANIFEST_MISSING")
    if manifest.stat().st_size > 1_000_000:
        raise ValueError("DISTRIBUTION_MANIFEST_INVALID")
    value = json.loads(manifest.read_text())
    files = value.get("files")
    if not isinstance(files, dict) or not 1 <= len(files) <= 2000:
        raise ValueError("DISTRIBUTION_MANIFEST_INVALID")
    total = 0
    for name, expected in files.items():
        parts = PurePosixPath(name).parts
        if not parts or name.startswith('/') or any(part in {'', '.', '..'} for part in name.split('/')) or '\\' in name:
            raise ValueError("DISTRIBUTION_PATH_INVALID")
        path = root.joinpath(*parts)
        if any(parent.is_symlink() for parent in [path, *path.parents]):
            raise ValueError("DISTRIBUTION_LINK_INVALID")
        if not path.is_file() or path.stat().st_size > 10_000_000:
            raise ValueError("DISTRIBUTION_FILE_INVALID")
        total += path.stat().st_size
        if total > 32 * 1024 * 1024:
            raise ValueError("DISTRIBUTION_CAPACITY")
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if digest != expected:
            raise ValueError("DISTRIBUTION_DIGEST_MISMATCH")


def find_vision_executable(root: Path) -> Path:
    """Prefer the packaged asset; source checkouts may reuse their own native build."""
    packaged = root / 'bin/inkwise-vision'
    if packaged.is_file():
        return packaged
    candidate = root / 'artifacts/inkwise-vision'
    if ((root / '.git').exists() and not (root / 'distribution.json').exists()
            and candidate.is_file() and not candidate.is_symlink()
            and os.access(candidate, os.X_OK)):
        return candidate
    return packaged


def process_stamp(pid: int) -> str:
    return subprocess.check_output(['ps', '-p', str(pid), '-o', 'lstart='], text=True, env={**os.environ, 'LC_ALL': 'C'}).strip()


def process_directory(pid: int) -> Path:
    if platform.system() == 'Linux':
        return (Path('/proc') / str(pid) / 'cwd').resolve(strict=True)
    output = subprocess.check_output(['lsof', '-a', '-p', str(pid), '-d', 'cwd', '-Fn'],
                                     text=True, stderr=subprocess.DEVNULL)
    return Path(next(line[1:] for line in output.splitlines() if line.startswith('n'))).resolve(strict=True)


def owned_process(state: dict) -> bool:
    if (not isinstance(state, dict) or type(state.get('pid')) is not int or state['pid'] <= 1
            or not isinstance(state.get('started'), str) or not state['started']
            or type(state.get('port')) is not int or not 1024 <= state['port'] <= 65535):
        return False
    try:
        command = subprocess.check_output(['ps', '-p', str(state['pid']), '-o', 'command='], text=True,
            env={**{key: value for key, value in os.environ.items() if key != 'LC_ALL'}, 'LC_CTYPE': 'en_US.UTF-8' if platform.system() == 'Darwin' else 'C.UTF-8', 'LC_TIME': 'C'})
        if process_stamp(state['pid']) != state['started']:
            return False
        # ps does not quote argv. Match the known interpreter and script literally,
        # so moving the application into a folder with spaces remains verifiable.
        interpreters = {sys.executable, str(Path(sys.executable).resolve()), Path(sys.executable).name,
                        "python", "python3", f"python{sys.version_info.major}.{sys.version_info.minor}"}
        interpreter = next((value for value in interpreters if command.startswith(value + ' ')), None)
        if interpreter is None:
            return False
        arguments = command[len(interpreter):].strip()
        flags = {'-u', '-B', '-E', '-I', '-O', '-OO', '-s', '-S'}
        while arguments.partition(' ')[0] in flags:
            arguments = arguments.partition(' ')[2].lstrip()
        script = str(ROOT / 'scripts/start_local.py')
        if arguments == script or arguments.startswith(script + ' '):
            return True
        relative = ('scripts/start_local.py', './scripts/start_local.py')
        return (any(arguments == value or arguments.startswith(value + ' ') for value in relative)
                and process_directory(state['pid']) == ROOT.resolve())
    except (OSError, ValueError, StopIteration, subprocess.CalledProcessError):
        return False


def retire_stale_state(running: Path, state: dict) -> None:
    """Remove metadata only after both its process and loopback listener are gone."""
    if (not isinstance(state, dict) or type(state.get('pid')) is not int or state['pid'] <= 1
            or type(state.get('port')) is not int or not 1024 <= state['port'] <= 65535):
        raise ValueError('LOCAL_APP_STATE_INVALID')
    try:
        os.kill(state['pid'], 0)
    except ProcessLookupError:
        pass
    except PermissionError:
        raise SystemExit('LOCAL_APP_PROCESS_CHANGED; existing metadata and service preserved') from None
    else:
        raise SystemExit('LOCAL_APP_PROCESS_CHANGED; existing metadata and service preserved')
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(('127.0.0.1', state['port']))
        except OSError:
            raise SystemExit('LOCAL_PORT_UNAVAILABLE; existing metadata and service preserved') from None
    running.unlink()


def main() -> int:
    default = (Path.home() / 'Library/Application Support/InkwiseK12/workspace' if platform.system() == 'Darwin'
               else Path.home() / '.local/share/inkwise-k12/workspace')
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path, default=default)
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--no-open', action='store_true')
    parser.add_argument('--start-model', action='store_true')
    parser.add_argument('--model-profile', choices=['qwen2.5-7b', 'deepseek-r1-7b'], default='qwen2.5-7b', help='Only used when creating a new private workspace')
    parser.add_argument('--show-setup', action='store_true', help='Show local setup guidance when a model is unavailable')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--stop', action='store_true')
    parser.add_argument('--upgrade-check', action='store_true', help='Read-only selected workspace compatibility check; no model or API start')
    parser.add_argument('--backup-file', type=Path)
    args = parser.parse_args()
    data = args.data_dir.expanduser().absolute()
    running = data / 'running.json'
    if args.stop:
        if not running.is_file() or running.stat().st_size > 4096:
            raise SystemExit('LOCAL_APP_NOT_RUNNING')
        state = json.loads(running.read_text())
        if not owned_process(state):
            raise SystemExit('LOCAL_APP_PROCESS_CHANGED; no process stopped')
        os.kill(state['pid'], signal.SIGTERM)
        print('Local workspace stop requested; saved data is retained.')
        return 0
    if not 1024 <= args.port <= 65535:
        raise SystemExit('LOCAL_PORT_INVALID')
    verify_distribution(ROOT)
    if running.exists() and not args.upgrade_check:
        if running.stat().st_size > 4096:
            raise SystemExit('LOCAL_APP_STATE_INVALID')
        state = json.loads(running.read_text())
        if owned_process(state):
            url = f'http://127.0.0.1:{int(state["port"])}/workspace/'
            print('Local workspace already running: ' + url)
            if not args.no_open and not args.check:
                webbrowser.open(url)
            return 0
        retire_stale_state(running, state)
    # The selected workspace file, rather than another project's shell settings, configures this app.
    for key in list(os.environ):
        if key.startswith('INKWISE_'):
            os.environ.pop(key)
    from inkwise.errors import DomainError
    from inkwise.setup import doctor, run_local, setup_local, ensure_loopback_port_available
    from inkwise.settings import Settings
    env_file = data / '.env'
    vision = find_vision_executable(ROOT)
    if not env_file.is_file():
        if args.check or args.upgrade_check:
            raise SystemExit('LOCAL_CONFIGURATION_MISSING; start once to create a private workspace')
        setup_local(data, ROOT / 'teaching', frontend_dir=ROOT / 'web',
                    vision_executable=vision if vision.is_file() else None, model_profile=args.model_profile)
    os.environ['INKWISE_SERVED_FRONTEND_DIR'] = str(ROOT / 'web')
    os.environ['INKWISE_REFERENCE_PACK'] = str(ROOT / 'teaching/reference-pack.json')
    if vision.is_file():
        os.environ['INKWISE_VISION_EXECUTABLE'] = str(vision)
    os.environ['NO_PROXY'] = ','.join(filter(None, [os.environ.get('NO_PROXY', ''), '127.0.0.1', 'localhost', '::1']))
    settings = Settings(_env_file=env_file)
    from inkwise.local_upgrade import inspect_upgrade
    compatibility = inspect_upgrade(settings, backup=args.backup_file)
    if args.upgrade_check or compatibility['status'] != 'compatible':
        print(json.dumps(compatibility, ensure_ascii=False))
        return 0 if compatibility['status'] == 'compatible' else 3 if compatibility['status'] == 'blocked' else 2
    owned_model = None
    stop = threading.Event()
    def interrupt(_signal, _frame):
        raise KeyboardInterrupt
    previous_signal = signal.signal(signal.SIGTERM, interrupt)
    try:
        report = doctor(settings, connect=True)
        if not report['ready'] and args.start_model and not args.check and settings.model_base_url == 'http://127.0.0.1:11434/v1':
            ollama = shutil.which('ollama') or str(Path('/Applications/Ollama.app/Contents/Resources/ollama'))
            if Path(ollama).is_file():
                environment = os.environ.copy()
                environment.update(OLLAMA_HOST='127.0.0.1:11434', OLLAMA_NUM_PARALLEL='1',
                                   OLLAMA_MAX_LOADED_MODELS='1', OLLAMA_DEBUG='false')
                owned_model = subprocess.Popen([ollama, 'serve'], env=environment,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                for _ in range(60):
                    if owned_model.poll() is not None:
                        break
                    time.sleep(.5)
                    report = doctor(settings, connect=True)
                    if report['ready']:
                        break
        if not report['ready'] and args.show_setup and not args.check:
            from first_run import serve_setup
            ensure_loopback_port_available(args.port)
            from inkwise.cli import write_private
            write_private(running, json.dumps({'pid': os.getpid(), 'started': process_stamp(os.getpid()), 'port': args.port}))
            prepared = serve_setup(args.port, lambda: doctor(settings, connect=True),
                                   model_name=settings.model_name, model_base_url=settings.model_base_url)
            running.unlink(missing_ok=True)
            if prepared:
                report = doctor(settings, connect=True)
        if not report['ready']:
            print(json.dumps(report, ensure_ascii=False))
            return 2
        if args.check:
            print(json.dumps(report, ensure_ascii=False))
            return 0
        try:
            ensure_loopback_port_available(args.port)
        except DomainError:
            raise SystemExit('LOCAL_PORT_UNAVAILABLE; existing service left unchanged') from None
        url = f'http://127.0.0.1:{args.port}/workspace/'
        print(f'Open {url}\nTeacher access file: {data / "teacher-access.txt"}\nCtrl+C stops this app and its owned model process.', flush=True)
        from inkwise.cli import write_private
        write_private(running, json.dumps({'pid': os.getpid(), 'started': process_stamp(os.getpid()), 'port': args.port}))
        def open_when_ready():
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for _ in range(100):
                if stop.wait(.2):
                    return
                try:
                    opener.open(url, timeout=1).close()
                    webbrowser.open(url)
                    return
                except OSError:
                    pass
        if not args.no_open:
            threading.Thread(target=open_when_ready, daemon=True).start()
        return run_local(env_file, args.port)
    finally:
        signal.signal(signal.SIGTERM, previous_signal)
        stop.set()
        if running.is_file():
            try:
                if json.loads(running.read_text()).get('pid') == os.getpid():
                    running.unlink()
            except (OSError, ValueError):
                pass
        if owned_model is not None and owned_model.poll() is None:
            owned_model.terminate()
            try:
                owned_model.wait(timeout=10)
            except subprocess.TimeoutExpired:
                owned_model.kill()
                owned_model.wait()


if __name__ == '__main__':
    from inkwise.errors import DomainError
    try:
        raise SystemExit(main())
    except (ValueError, OSError, DomainError):
        raise SystemExit('LOCAL_DISTRIBUTION_OR_RUNTIME_INVALID; no private values printed') from None
    except KeyboardInterrupt:
        raise SystemExit(0) from None
