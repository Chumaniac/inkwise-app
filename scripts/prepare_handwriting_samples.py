"""Read only small ranges of a CC BY GNHK mirror; retain common-word crops only."""
import hashlib
import io
import json
import re
import zipfile
import tempfile
from contextlib import ExitStack
from pathlib import Path

import httpx
from PIL import Image,ImageOps

REVISION='eaa67d396fd29b8b04e38d630da79f67db11b212'
URL=f'https://huggingface.co/datasets/staghado/GNHK-Dataset/resolve/{REVISION}/GNHK-dataset.zip'


class RemoteArchive(io.RawIOBase):
    def __init__(self,client):
        self.client=client;self.position=0;self.requests=0
        with client.stream('GET',URL,headers={'Range':'bytes=0-0'}) as response:
            if response.status_code!=206:raise ValueError('Source does not support bounded range access')
            self.size=int(response.headers['content-range'].split('/')[-1])
    def seekable(self):return True
    def readable(self):return True
    def tell(self):return self.position
    def seek(self,offset,whence=0):
        self.position=offset if whence==0 else self.position+offset if whence==1 else self.size+offset
        return self.position
    def read(self,size=-1):
        if size<0:size=self.size-self.position
        if not 0<=size<=8_000_000 or self.requests>160:raise ValueError('Source range budget exceeded')
        if size==0:return b''
        end=min(self.size,self.position+size)-1;self.requests+=1
        with self.client.stream('GET',URL,headers={'Range':f'bytes={self.position}-{end}'}) as response:
            if response.status_code!=206:raise ValueError('Unexpected full archive response')
            content=bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content)>size:raise ValueError('Range response exceeds limit')
        self.position+=len(content)
        return bytes(content)


def main():
    destination=Path('artifacts/handwriting-public');destination.mkdir(parents=True,exist_ok=True)
    if (destination/'manifest.json').exists():
        print('Existing successful handwriting sample pack reused');return
    allowed=set('the and this that with from have make read write writing learning book paper text word words school work good can will about your more when what for not all are was were is it to of in on as a an be by'.split())
    with ExitStack() as stack:
        cached_zip=Path('artifacts/public-sources/gnhk-test.zip')
        remote=None
        if not cached_zip.exists():
            client=stack.enter_context(httpx.Client(timeout=60,follow_redirects=True))
            remote=stack.enter_context(RemoteArchive(client));outer=stack.enter_context(zipfile.ZipFile(remote))
            info=outer.getinfo('test_data.zip')
            if info.file_size>350_000_000:raise ValueError('Unexpected source test archive size')
            temporary=cached_zip.with_suffix('.partial');downloaded=0
            with outer.open('test_data.zip') as source,temporary.open('wb') as output:
                while chunk:=source.read(4*1024*1024):
                    downloaded+=len(chunk)
                    if downloaded>350_000_000:raise ValueError('Source archive exceeds budget')
                    output.write(chunk)
                    if downloaded%(64*1024*1024)<4*1024*1024:print('Public source archive MiB:',downloaded//(1024*1024),flush=True)
            temporary.chmod(0o600);temporary.replace(cached_zip)
        if cached_zip.stat().st_size!=285995725:raise ValueError('Source test archive size changed')
        archive=stack.enter_context(zipfile.ZipFile(cached_zip))
        names=archive.namelist();annotations=sorted(name for name in names if name.lower().endswith('.json'))
        print('Public archive annotations:',len(annotations))
        records=[]
        for name in annotations[:8]:
            if archive.getinfo(name).file_size>500000:continue
            labels=json.loads(archive.read(name))
            # Original GNHK annotates word quadrilaterals, text and printed/handwritten type.
            if not isinstance(labels,list):raise ValueError('Unexpected GNHK annotation shape')
            candidates=[]
            for row in labels:
                if row.get('type') not in (None,'H','h','handwritten'):continue
                word=row.get('text','').strip()
                if word.lower() not in allowed:continue
                polygon=row.get('polygon',row.get('poly'))
                if polygon is None:continue
                candidates.append((word,polygon))
            if not candidates:continue
            image_name=name.rsplit('.',1)[0]+'.jpg'
            if image_name not in names or archive.getinfo(image_name).file_size>8_000_000:continue
            raw=archive.read(image_name)
            with Image.open(io.BytesIO(raw)) as original:
                image=ImageOps.exif_transpose(original)
                if image.width*image.height>30_000_000:continue
                for word,polygon in candidates[:4]:
                    if isinstance(polygon,dict):
                        xs=[polygon[key] for key in polygon if key.startswith('x')];ys=[polygon[key] for key in polygon if key.startswith('y')]
                    else:
                        xs=[point[0] for point in polygon];ys=[point[1] for point in polygon]
                    box=(max(0,min(xs)-3),max(0,min(ys)-3),min(image.width,max(xs)+3),min(image.height,max(ys)+3))
                    if box[2]-box[0]<5 or box[3]-box[1]<5:continue
                    crop=image.crop(box)
                    identifier=f'word-{len(records)+1:02d}';target=destination/(identifier+'.png');crop.convert('RGB').save(target)
                    records.append({'id':identifier,'image':target.name,'reference':word,'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),
                        'sourceFile':image_name,'sourceImageSha256':hashlib.sha256(raw).hexdigest()})
            if len(records)>=12:break
        if not records:raise ValueError('No eligible non-identifying word crops found')
        manifest={'source':'https://github.com/GoodNotes/GNHK-dataset','mirror':'staghado/GNHK-Dataset',
            'mirrorRevision':REVISION,'license':'CC-BY-4.0','attribution':'Alex W. C. Lee, Jonathan Chung and Marco Lee, GNHK, ICDAR 2021',
            'scope':'selected common-word crops; not a representative K12 handwriting benchmark','rows':records}
        (destination/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
        print('Prepared common-word crops:',len(records),'bounded HTTP ranges:',remote.requests if remote else 0)


if __name__=='__main__':main()
