"""Build a private, checksum-bound local app from immutable tracked source only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = ('src/inkwise/', 'web/', 'teaching/')
FILES = {'THIRD_PARTY_NOTICES.md', 'scripts/first_run.py', 'scripts/prepare_local_model.py',
         'docs/user-guide.md', 'docs/privacy.md', 'docs/build.md', 'docs/troubleshooting.md', 'docs/verification.md', 'docs/references.md', 'docs/local-model-setup.md',
         'pyproject.toml', 'uv.lock', 'README.md', 'LICENSE', 'Start Inkwise.command',
         'native/vision-ocr.swift', 'scripts/start_local.py',
         'docs/local-distribution.md', 'docs/quickstart.md', 'docs/writing-revisions.md', 'scripts/package_local_release.py'}


def allowed(name: str) -> bool:
    parts = name.split('/')
    return (not name.startswith('/') and '\\' not in name and
            not any(part in {'', '.', '..', 'node_modules', '__pycache__'} or part.startswith('.') for part in parts) and
            (name in FILES or name.startswith(DIRECTORIES)))


def assemble(files: dict[str, bytes], version: str, revision: str, target: str, output: Path,
             vision: bytes | None = None) -> Path:
    if not re.fullmatch(r'\d+\.\d+\.\d+', version) or not re.fullmatch('[0-9a-f]{40}', revision):
        raise ValueError('RELEASE_ID_INVALID')
    if target not in {'macos-arm64', 'macos-x86_64', 'source'} or not files or len(files) > 2000:
        raise ValueError('RELEASE_TARGET_OR_CAPACITY')
    if any(not allowed(name) or len(value) > 10_000_000 for name, value in files.items()):
        raise ValueError('RELEASE_PAYLOAD_INVALID')
    payload = dict(files)
    if target.startswith('macos-'):
        if not vision or len(vision) > 10_000_000:
            raise ValueError('RELEASE_NATIVE_ASSET_MISSING')
        payload['bin/inkwise-vision'] = vision
    if sum(map(len, payload.values())) > 32 * 1024 * 1024:
        raise ValueError('RELEASE_CAPACITY')
    manifest = {'version': version, 'source_sha': revision, 'target': target,
                'data_features': ['baseline/v1', 'writing-revisions/v1', 'revision-reassessment/v1'],
                'local_upgrade': {'schema': 'inkwise.local-upgrade/v1', 'automatic_migration': False, 'stopped_private_backup_required': True},
                'files': {name: hashlib.sha256(data).hexdigest() for name, data in sorted(payload.items())}}
    payload['distribution.json'] = (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode()
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f'inkwise-k12-v{version}-{target}.zip'
    prefix = f'inkwise-k12-{version}'
    with tempfile.TemporaryDirectory(prefix='.release-', dir=output) as temporary:
        staged = Path(temporary) / archive.name
        with zipfile.ZipFile(staged, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
            for name, data in sorted(payload.items()):
                entry = zipfile.ZipInfo(prefix + '/' + name, (2020, 1, 1, 0, 0, 0))
                entry.external_attr = (0o100755 if name in {'Start Inkwise.command', 'bin/inkwise-vision'} else 0o100644) << 16
                entry.compress_type = zipfile.ZIP_DEFLATED
                bundle.writestr(entry, data)
        os.link(staged, archive)  # Never overwrite an existing successful distribution.
    return archive


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--revision', required=True)
    parser.add_argument('--target', choices=['macos-arm64', 'macos-x86_64', 'source'], required=True)
    parser.add_argument('--vision', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch('[0-9a-f]{40}', args.revision):
        raise SystemExit('RELEASE_SOURCE_INVALID')
    listing = subprocess.check_output(['git', 'ls-tree', '-r', args.revision], cwd=ROOT, text=True)
    files = {}
    for line in listing.splitlines():
        metadata, name = line.split('\t', 1)
        if not allowed(name):
            continue
        if metadata.split()[0] not in {'100644', '100755'}:
            raise SystemExit('RELEASE_LINK_NOT_ALLOWED')
        files[name] = subprocess.check_output(['git', 'show', f'{args.revision}:{name}'], cwd=ROOT)
    for required in ['pyproject.toml', 'uv.lock', 'web/index.html', 'teaching/manifest.json', 'Start Inkwise.command', 'scripts/start_local.py']:
        if required not in files:
            raise SystemExit('RELEASE_SOURCE_INCOMPLETE')
    version = tomllib.loads(files['pyproject.toml'].decode())['project']['version']
    archive = assemble(files, version, args.revision, args.target, args.output,
                       args.vision.read_bytes() if args.vision else None)
    print(archive)


if __name__ == '__main__':
    main()
