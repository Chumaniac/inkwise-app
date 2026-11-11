"""Pin the public MIT TrOCR model; verify existing files and download only missing ones."""
import argparse
import hashlib
from pathlib import Path

import httpx

from inkwise.knowledge import atomic_json

REVISION='eaacaf452b06415df8f10bb6fad3a4c11e609406'
FILES=['README.md','config.json','generation_config.json','merges.txt','model.safetensors','preprocessor_config.json',
       'special_tokens_map.json','tokenizer_config.json','vocab.json']
BASE=f'https://huggingface.co/microsoft/trocr-base-handwritten/resolve/{REVISION}/'


def hashes(path,size):
    sha256=hashlib.sha256();git=hashlib.sha1(f'blob {size}\0'.encode())
    with path.open('rb') as stream:
        while chunk:=stream.read(1024*1024):sha256.update(chunk);git.update(chunk)
    return sha256.hexdigest(),git.hexdigest()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--download',action='store_true');args=parser.parse_args()
    root=Path('artifacts/models/trocr')/REVISION;root.mkdir(parents=True,exist_ok=True)
    rows=[]
    with httpx.Client(timeout=60,follow_redirects=False,trust_env=False) as client:
        response=client.get(f'https://huggingface.co/api/models/microsoft/trocr-base-handwritten/revision/{REVISION}?blobs=true')
        response.raise_for_status()
        if len(response.content)>262144:raise ValueError('Model metadata too large')
        metadata=response.json()
        if metadata.get('sha')!=REVISION:raise ValueError('Model revision mismatch')
        files={row['rfilename']:row for row in metadata['siblings']}
        for name in FILES:
            entry=files[name];expected=entry.get('lfs',{}).get('sha256') or entry.get('blobId','')
            size=entry.get('size',0)
            if len(expected) not in (40,64) or not 0<size<=1_600_000_000:raise ValueError('Unverifiable upstream file metadata')
            path=root/name;downloaded=False
            if not path.exists():
                if not args.download:raise SystemExit('Model file missing. Use --download to fetch the fixed public release.')
                partial=path.with_suffix(path.suffix+'.partial');count=0
                try:
                    with client.stream('GET',BASE+name,follow_redirects=True) as source,partial.open('wb') as target:
                        source.raise_for_status()
                        for chunk in source.iter_bytes(1024*1024):
                            count+=len(chunk)
                            if count>size:raise ValueError('Download exceeds pinned size')
                            target.write(chunk)
                    if count!=size:raise ValueError('Incomplete download')
                    a,b=hashes(partial,size)
                    if expected!=(a if len(expected)==64 else b):raise ValueError('Model checksum mismatch')
                    partial.replace(path);downloaded=True
                finally:partial.unlink(missing_ok=True)
            if path.stat().st_size!=size:raise ValueError('Preserved model size mismatch')
            sha256,git=hashes(path,size)
            if expected!=(sha256 if len(expected)==64 else git):raise ValueError('Preserved model checksum mismatch')
            rows.append({'file':name,'bytes':size,'sha256':sha256,'upstreamDigest':expected})
            print(name, 'downloaded' if downloaded else 'verified and reused',flush=True)
    atomic_json(root/'inkwise-manifest.json',{'model':'microsoft/trocr-base-handwritten','revision':REVISION,'license':'MIT',
        'source':'https://huggingface.co/microsoft/trocr-base-handwritten','files':rows})


if __name__=='__main__':main()
