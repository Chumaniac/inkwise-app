"""Prepare one verified public model; never edit workspaces or replace existing models."""
import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import urllib.request

FILES = [
    ('qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf', 3993201344, 'dfce12e3862a5283ccfb88221b48480e58745165de856439950d0f22590580db'),
    ('qwen2.5-7b-instruct-q4_k_m-00002-of-00002.gguf', 689872288, '539cf93f78e887edea1c04e2d7d8cdaca9d01dae9c9025bcb8accbe29df3d72a'),
]
SOURCE = 'https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GGUF'
MODEL = 'inkwise-qwen-local'


def verified(path, size, digest):
    if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
        return False
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest() == digest


def download(cache, item):
    name, size, digest = item
    destination = cache / name
    if destination.exists() or destination.is_symlink():
        if not verified(destination, size, digest):
            raise ValueError('EXISTING_MODEL_FILE_DIFFERS; existing file retained')
        print(name + ': verified cached file reused', flush=True)
        return
    partial = cache / (name + '.part')
    if partial.is_symlink() or partial.exists() and not partial.is_file():
        raise ValueError('MODEL_PARTIAL_PATH_INVALID')
    start = partial.stat().st_size if partial.exists() else 0
    if start > size:
        raise ValueError('MODEL_PARTIAL_SIZE_INVALID')
    if start < size:
        request = urllib.request.Request(SOURCE + '/resolve/main/' + name,
                                        headers={'Range': f'bytes={start}-'} if start else {})
        with urllib.request.urlopen(request, timeout=180) as response:
            if start and (response.status != 206 or not response.headers.get('Content-Range', '').startswith(f'bytes {start}-')):
                raise ValueError('MODEL_RESUME_UNAVAILABLE; existing partial retained')
            descriptor = os.open(partial, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            completed = start
            checkpoint = int(10 * start / size)
            with os.fdopen(descriptor, 'ab') as output:
                while chunk := response.read(1024 * 1024):
                    completed += len(chunk)
                    if completed > size:
                        raise ValueError('MODEL_SIZE_INVALID')
                    output.write(chunk)
                    progress = int(10 * completed / size)
                    if progress != checkpoint:
                        checkpoint = progress
                        print(f'{name}: {progress * 10}%', flush=True)
    if not verified(partial, size, digest):
        raise ValueError('MODEL_CHECKSUM_MISMATCH; partial retained')
    os.link(partial, destination)  # Never overwrite another existing file.
    partial.unlink()
    print(name + ': official SHA-256 verified', flush=True)


def install(cache):
    # Only the existing numeric-loopback provider, with no essay data or credentials.
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with client.open('http://127.0.0.1:11434/api/tags', timeout=5) as response:
        names = {row['name'].removesuffix(':latest') for row in json.load(response)['models']}
    if MODEL in names:
        print('Model name already exists; kept unchanged. Run the workspace doctor to verify it.')
        return
    for name, size, digest in FILES:
        if not verified(cache / name, size, digest):
            raise ValueError('VERIFIED_MODEL_FILES_REQUIRED')
    if {path.name for path in cache.glob('qwen2.5-7b-instruct-q4_k_m-*.gguf')} != {row[0] for row in FILES}:
        raise ValueError('MODEL_SPLIT_SET_INVALID; unrelated files retained')
    candidates = [shutil.which('ollama'), str(Path.home() / '.local/bin/ollama'),
                  '/Applications/Ollama.app/Contents/Resources/ollama']
    executable = next((value for value in candidates if value and Path(value).is_file()), None)
    if not executable:
        raise ValueError('OLLAMA_CLI_REQUIRED; install the official Ollama application')
    environment = {**os.environ, 'OLLAMA_HOST': '127.0.0.1:11434', 'NO_PROXY': '127.0.0.1,localhost,::1'}
    version = subprocess.check_output([executable, '--version'], text=True, timeout=10, env=environment)
    match = re.search(r'(\d+)\.(\d+)\.(\d+)', version)
    if not match or tuple(map(int, match.groups())) < (0, 35, 0):
        raise ValueError('OLLAMA_035_OR_NEWER_REQUIRED_FOR_SPLIT_GGUF')
    modelfile = cache / 'Modelfile.inkwise-qwen'
    if modelfile.is_symlink():
        raise ValueError('MODEL_CONFIGURATION_LINK_INVALID')
    content = ('FROM ./qwen2.5-7b-instruct-q4_k_m-*.gguf'
               + '\nPARAMETER num_ctx 32768\nPARAMETER num_predict 4096\nPARAMETER temperature 0\n')
    if modelfile.exists() and modelfile.read_text() != content:
        raise ValueError('EXISTING_MODEL_CONFIGURATION_DIFFERS; file retained')
    if not modelfile.exists():
        with modelfile.open('x') as output:
            output.write(content)
    environment = {**os.environ, 'OLLAMA_HOST': '127.0.0.1:11434', 'NO_PROXY': '127.0.0.1,localhost,::1'}
    subprocess.run([executable, 'create', MODEL, '-f', str(modelfile)], env=environment,
                   cwd=cache, check=True, timeout=300)
    print('Local scoring model prepared. Existing models and saved reports retained.')


def main():
    default = (Path.home() / 'Library/Application Support/InkwiseK12/models/qwen2.5'
               if platform.system() == 'Darwin' else Path.home() / '.local/share/inkwise-k12/models/qwen2.5')
    parser = argparse.ArgumentParser()
    parser.add_argument('--cache-dir', type=Path, default=default)
    parser.add_argument('--download', action='store_true', help='Download about 4.7 GB of fixed official model files')
    parser.add_argument('--import-model', action='store_true', help='Create only the missing inkwise-qwen-local model')
    args = parser.parse_args()
    cache = args.cache_dir.expanduser().absolute()
    if cache.is_symlink():
        raise ValueError('MODEL_CACHE_LINK_INVALID')
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.download:
        missing = sum(max(0, size - ((cache / (name + '.part')).stat().st_size if (cache / (name + '.part')).is_file() else 0))
                      for name, size, digest in FILES if not verified(cache / name, size, digest))
        if shutil.disk_usage(cache).free < missing + sum(row[1] for row in FILES) + 256 * 1024**2:
            raise ValueError('MODEL_FREE_SPACE_REQUIRED; leave room for download and provider import')
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda item: download(cache, item), FILES))
    if args.import_model:
        install(cache)
    if not args.download and not args.import_model:
        print(json.dumps({'model': MODEL, 'filesVerified': [verified(cache / name, size, digest) for name, size, digest in FILES],
                          'source': SOURCE, 'license': 'Apache-2.0', 'networkAccess': False}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Signed redirect URLs and provider internals never enter shared diagnostics.
        candidate = str(error).split(';', 1)[0] if isinstance(error, ValueError) else type(error).__name__
        code = candidate if re.fullmatch(r'[A-Za-z_0-9]{1,80}', candidate) else type(error).__name__
        raise SystemExit('MODEL_PREPARATION_FAILED: ' + code) from None
