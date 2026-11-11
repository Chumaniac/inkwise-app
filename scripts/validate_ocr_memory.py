"""Pillow/preprocessing/cache memory baseline; synthetic recognizer, no OCR model."""
import argparse
import asyncio
import gc
import hashlib
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
from tempfile import TemporaryDirectory
import time


def rss():
    return int(subprocess.check_output(['ps','-o','rss=','-p',str(os.getpid())],text=True).strip())*1024


async def measure(count):
    from PIL import Image,ImageDraw
    import inkwise.ocr
    from inkwise.document_ocr import recognize_document
    from inkwise.settings import Settings
    with TemporaryDirectory(prefix='inkwise-ocr-memory-') as temporary:
        root=Path(temporary);(root/'images').mkdir();pages=[];calls=[]
        for i in range(count):
            path=root/'images'/f'page-{i}.png'
            with Image.new('RGB',(1600,2200),'white') as image:
                ImageDraw.Draw(image).text((50,50),f'Synthetic page {i}',fill='black');image.save(path)
            pages.append({'imageId':f'page-{i}','sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'page':i+1})
        async def synthetic(*args,**kwargs):
            calls.append(1)
            return {'engine':'synthetic-no-model','revision':1,'text':'Synthetic page','lines':[{'text':'Synthetic page','box':[0,0,1,.1],'confidence':.5,'alternatives':[]}],
                    'uncertainLines':[0],'lexiconSuggestions':[],'requiresReview':True}
        previous=inkwise.ocr.recognize_image;inkwise.ocr.recognize_image=synthetic
        try:
            settings=Settings(_env_file=None,data_dir=root,ocr_backend='disabled',ocr_model_path=root/'absent-model',vision_executable=root/'absent-vision',cloud_enabled=False)
            gc.collect();before=rss();start=time.perf_counter()
            first=await recognize_document(pages,settings,tenant='synthetic',job_id='memory',language_policy='en')
            cold=(time.perf_counter()-start)*1000;after_cold=rss();start=time.perf_counter()
            second=await recognize_document(pages,settings,tenant='synthetic',job_id='memory',language_policy='en')
            warm=(time.perf_counter()-start)*1000
            assert first['text']==second['text'] and len(calls)==count and all(p['cached'] for p in second['pages'])
            del first,second;gc.collect()
            return {'pages':count,'pixelsPerPage':3520000,'concurrency':1,'coldMs':cold,'cachedMs':warm,'recognizerCalls':len(calls),
                    'rssBeforeBytes':before,'rssAfterColdBytes':after_cold,'rssAfterReleaseBytes':rss(),
                    'processPeakRssBytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024)}
        finally:inkwise.ocr.recognize_image=previous


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--pages',type=int,choices=[1,6]);args=parser.parse_args()
    if args.pages:print(json.dumps(asyncio.run(measure(args.pages))))
    else:
        rows=[json.loads(subprocess.check_output([sys.executable,__file__,'--pages',str(count)],text=True)) for count in (1,6)]
        print(json.dumps({'scope':'isolated synthetic 1600x2200 RGB pages; Pillow preprocessing and cache; recognizer mocked; no provider or model loaded',
                          'rows':rows,'benchmarkSha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                          'limits':['Whole-process peak includes imports/page creation; cold and cached timing each one sample, not p95',
                                    'RSS snapshots do not establish engine inference cost, long-term memory or school handwritten accuracy']},indent=2))
