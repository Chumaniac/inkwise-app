"""Bounded multi-page OCR, image geometry, uncertain-line comparison and durable reuse."""
import asyncio
import logging
import hashlib
import io
import math
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
from PIL import Image,ImageOps

from inkwise.errors import DomainError, failure_category, scope_denied
from inkwise.cache_storage import ensure_cache_capacity, read_cache_json, OCR_WRITE_RESERVE, MAX_CACHE_INPUT_BYTES
from inkwise.cache_storage import atomic_json
from inkwise.rubrics import canonical,digest,safe_segment


def prepare_page(source: Path,target: Path,deskew=False):
    with Image.open(source) as original:
        width,height=original.size
        gray=ImageOps.autocontrast(original.convert('L'),cutoff=1)
        angle=0.0
        if deskew:
            small=gray.copy();small.thumbnail((640,640))
            def quality(degrees):
                rotated=small.rotate(degrees,expand=True,fillcolor=255)
                mask=np.asarray(rotated)<160
                return float(np.var(mask.sum(axis=1)))
            baseline=quality(0);options=[(quality(value),value) for value in (-4,-3,-2,-1,0,1,2,3,4)]
            score,proposed=max(options)
            if baseline>0 and score>baseline*1.15:angle=float(proposed)
        processed=gray.rotate(angle,expand=True,fillcolor=255) if angle else gray
        processed.save(target,format='PNG')
        return {'originalSize':[width,height],'processedSize':list(processed.size),'rotationDegrees':angle,
                'operations':['grayscale','autocontrast']+(['deskew'] if angle else [])}


def original_box(box,geometry):
    w,h=geometry['originalSize'];nw,nh=geometry['processedSize']
    # Pillow forward rotation uses y-down image coordinates; this matrix is its inverse.
    radians=math.radians(geometry['rotationDegrees']);c,s=math.cos(radians),math.sin(radians)
    x,y,bw,bh=box;points=[]
    for px,py in ((x,y),(x+bw,y),(x,y+bh),(x+bw,y+bh)):
        px,py=px*nw-nw/2,py*nh-nh/2
        points.append((c*px-s*py+w/2,s*px+c*py+h/2))
    left=max(0,min(px for px,_ in points)/w);top=max(0,min(py for _,py in points)/h)
    right=min(1,max(px for px,_ in points)/w);bottom=min(1,max(py for _,py in points)/h)
    return [left,top,max(0,right-left),max(0,bottom-top)]


def line_crops(path: Path,folder: Path,max_lines=60):
    """Projection fallback for single-column pages; complex layouts require review."""
    with Image.open(path) as image:
        gray=ImageOps.autocontrast(image.convert('L'))
        small=gray.copy();small.thumbnail((1600,2400))
        mask=np.asarray(small)<170
        occupied=mask.mean(axis=1)>.003
        intervals=[];start=None;last=None
        for y,value in enumerate(occupied):
            if value:
                if start is None:start=y
                last=y
            elif start is not None and y-last>3:
                if last-start>=3:intervals.append((start,last+1))
                start=last=None
        if start is not None and last-start>=3:intervals.append((start,last+1))
        if len(intervals)>max_lines:raise DomainError('OCR_LINE_LIMIT',422)
        rows=[]
        for index,(top,bottom) in enumerate(intervals):
            columns=np.where(mask[top:bottom].any(axis=0))[0]
            if not len(columns):continue
            x0=max(0,int(columns[0])-3);x1=min(small.width,int(columns[-1])+4)
            y0=max(0,top-3);y1=min(small.height,bottom+3)
            box=[x0/small.width,y0/small.height,(x1-x0)/small.width,(y1-y0)/small.height]
            target=folder/f'line-{index}.png'
            image.crop((box[0]*image.width,box[1]*image.height,(box[0]+box[2])*image.width,(box[1]+box[3])*image.height)).save(target)
            rows.append({'path':str(target),'box':box})
        return rows


def layout_warnings(lines):
    # Geometry hints are conservative; no automatic column/strike-through reconstruction.
    warnings=set()
    if len(lines)>200:warnings.add('LAYOUT_ANALYSIS_LIMIT_REQUIRES_IMAGE_CHECK')
    for i,first in enumerate(lines[:200]):
        x,y,w,h=first['box']
        for second in lines[i+1:200]:
            sx,sy,sw,sh=second['box']
            vertical=max(0,min(y+h,sy+sh)-max(y,sy))
            horizontal=max(0,min(x+w,sx+sw)-max(x,sx))
            if vertical>min(h,sh)*.6 and max(sx-(x+w),x-(sx+sw))>.08:
                warnings.add('MULTICOLUMN_REQUIRES_IMAGE_CHECK')
            if min(w*h,sw*sh)>0 and vertical*horizontal/min(w*h,sw*sh)>.3:
                warnings.add('OVERLAPPING_TEXT_REQUIRES_IMAGE_CHECK')
    return sorted(warnings)


def file_signature(path):
    # Trusted operator-owned model files; changes invalidate only the cache identity.
    try:
        info=path.stat();return [str(path.resolve()),info.st_size,info.st_mtime_ns]
    except FileNotFoundError:return [str(path),'missing']


async def run_trocr(settings,rows):
    from inkwise.ocr import run_ocr_process
    from inkwise.routing import model_capacity
    weights=settings.ocr_model_path/'model.safetensors'
    if not weights.is_file() or weights.stat().st_size>1_600_000_000:
        raise DomainError('OCR_MODEL_SIZE_OR_FILE_INVALID')
    capacity=settings.model_copy(update={'model_base_url':'local-trocr','model_global_concurrency':1})
    async with model_capacity(capacity):
        return await run_ocr_process([sys.executable,'-m','inkwise.trocr','--model',str(settings.ocr_model_path.resolve())],
            settings.ocr_timeout_seconds,canonical({'lines':rows}).encode())


async def trocr_page(path,settings):
    from inkwise.ocr import run_ocr_process
    if not settings.ocr_model_path.is_dir():raise DomainError('OCR_SECONDARY_NOT_CONFIGURED',503)
    with TemporaryDirectory(prefix='inkwise-lines-') as temporary:
        rows=await asyncio.to_thread(line_crops,path,Path(temporary),settings.ocr_max_lines)
        if not rows:raise DomainError('OCR_NO_TEXT',422)
        return await run_trocr(settings,rows)


async def compare_uncertain_lines(path,result,settings,language):
    from inkwise.ocr import run_ocr_process,OCRResult
    from inkwise.benchmarks import edit_distance
    if settings.ocr_secondary_backend=='disabled' or language=='zh' or language=='dialect':
        return {**result,'secondaryStatus':'not-configured-for-language','disagreements':[]}
    indices=sorted(range(len(result['lines'])),key=lambda i:result['lines'][i]['confidence'])[:settings.ocr_secondary_max_lines]
    indices=[i for i in indices if not any('\u3400'<=c<='\u9fff' for c in result['lines'][i]['text'])]
    if not indices:return {**result,'secondaryStatus':'no-eligible-lines','disagreements':[]}
    try:
        if settings.ocr_secondary_backend!='trocr' or not settings.ocr_model_path.is_dir():
            raise DomainError('OCR_SECONDARY_NOT_CONFIGURED')
        with TemporaryDirectory(prefix='inkwise-comparison-') as temporary:
            rows=[]
            with Image.open(path) as image:
                for index in indices:
                    x,y,w,h=result['lines'][index]['box']
                    box=(max(0,x*image.width-3),max(0,y*image.height-3),min(image.width,(x+w)*image.width+3),min(image.height,(y+h)*image.height+3))
                    if box[2]<=box[0] or box[3]<=box[1]:continue
                    target=Path(temporary)/f'{index}.png';image.crop(box).save(target)
                    rows.append({'path':str(target),'box':[x,y,w,h],'index':index})
            if not rows:return {**result,'secondaryStatus':'no-eligible-lines','disagreements':[]}
            raw=await run_trocr(settings,rows)
            secondary=OCRResult.model_validate(raw)
            if len(secondary.lines)!=len(rows):raise DomainError('OCR_INVALID_RESPONSE')
            disagreements=[]
            for source,line in zip(rows,secondary.lines):
                index=source['index'];primary=result['lines'][index]['text']
                distance=edit_distance(primary,line.text)/max(1,len(primary),len(line.text))
                if distance>settings.ocr_disagreement_threshold:
                    disagreements.append({'line':index,'primary':primary,'secondary':line.text,'distance':distance,'requiresImageCheck':True})
            # Keep the primary transcript immutable; alternatives are never silently substituted.
            return {**result,'secondaryStatus':'compared','secondaryEngine':secondary.engine,
                'comparedLines':len(rows),'disagreements':disagreements,
                'uncertainLines':sorted(set(result['uncertainLines'])|{row['line'] for row in disagreements})}
    except asyncio.CancelledError:raise
    except Exception as error:
        if scope_denied(error): raise
        category=failure_category(error)
        logging.getLogger(__name__).warning('ocr.secondary degraded category=%s', category)
        return {**result,'secondaryStatus':'unavailable-requires-teacher','disagreements':[],
                'secondaryFailureCategory':category}


def missing_pages(text,count):
    import re
    totals=[]
    for line in text.splitlines():
        match=re.fullmatch(r'\s*(?:第\s*\d{1,2}\s*页[,，/ ]*)?共\s*(\d{1,2})\s*页\s*',line)
        english=re.fullmatch(r'\s*page\s+\d{1,2}\s+of\s+(\d{1,2})\s*',line,re.I)
        if match or english:totals.append(int((match or english).group(1)))
    return bool(totals and max(totals)>count)


def existing_pages(job,settings):
    pages=(job.ocr or {}).get('pages')
    if pages:return pages
    if not job.image_id:return []
    source=settings.data_dir/'images'/(safe_segment(job.image_id)+'.png')
    with source.open('rb') as stream:
        checksum=hashlib.file_digest(stream,'sha256').hexdigest()
    pages=[{'imageId':job.image_id,'sha256':checksum,'page':1}]
    # Preserve successful legacy single-page recognition when appending a page.
    if job.ocr and job.ocr.get('text'):
        cache=settings.data_dir/'ocr-cache'/safe_segment(job.id)/(safe_segment(job.image_id)+'.json')
        if not cache.exists():
            ensure_cache_capacity(cache.parent, settings, MAX_CACHE_INPUT_BYTES)
            with Image.open(source) as image:size=list(image.size)
            atomic_json(cache,{'sourceHash':checksum,'result':job.ocr,
                'geometry':{'originalSize':size,'processedSize':size,'rotationDegrees':0,'operations':[]}})
    return pages


async def recognize_document(pages,settings,*,tenant,job_id,language_policy):
    from inkwise.ocr import recognize_image
    if not 1<=len(pages)<=settings.ocr_max_pages:raise DomainError('OCR_PAGE_LIMIT')
    if len({page['sha256'] for page in pages})!=len(pages):raise DomainError('DUPLICATE_PAGE')
    folder=settings.data_dir/'ocr-cache'/safe_segment(job_id)
    folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    combined=[];texts=[];metadata=[];engines=set();uncertain=[];disagreements=[];suggestions=[]
    for ordinal,page in enumerate(pages,1):
        image_id=safe_segment(page['imageId']);source=settings.data_dir/'images'/(image_id+'.png')
        fingerprint=digest(canonical({'source':page['sha256'],'engine':settings.ocr_backend,'secondary':settings.ocr_secondary_backend,
            'model':file_signature(settings.ocr_model_path/'model.safetensors'),'vision':file_signature(settings.vision_executable),
            'secondaryLines':settings.ocr_secondary_max_lines,'lineLimit':settings.ocr_max_lines,'disagreementThreshold':settings.ocr_disagreement_threshold,
            'endpoint':settings.ocr_base_url,'lexiconRevision':settings.lexicon_model_revision,'deskew':settings.ocr_deskew,'language':language_policy}))
        with source.open('rb') as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest()!=page['sha256']:raise DomainError('OCR_SOURCE_INTEGRITY_FAILED')
        cached=folder/(image_id+'.json')
        reused=cached.exists()
        if reused:
            saved=read_cache_json(cached)
            if saved.get('sourceHash')!=page['sha256']:raise DomainError('OCR_CACHE_INTEGRITY_FAILED')
            if saved.get('configurationFingerprint') and saved['configurationFingerprint']!=fingerprint:
                raise DomainError('OCR_CACHED_CONFIGURATION_CHANGED')
            result,geometry=saved['result'],saved['geometry']
        else:
            ensure_cache_capacity(folder, settings, OCR_WRITE_RESERVE, reserve_files=2)
            processed=folder/(fingerprint+'.png')
            geometry=await asyncio.to_thread(prepare_page,source,processed,settings.ocr_deskew)
            result=await recognize_image(processed,settings,tenant=tenant,language_policy=language_policy)
            atomic_json(cached,{'result':result,'geometry':geometry,'sourceHash':page['sha256'],'configurationFingerprint':fingerprint})
        result={**result,'layoutWarnings':layout_warnings(result['lines'])}
        offset=len(combined)
        for line in result['lines']:
            combined.append({**line,'page':ordinal,'box':original_box(line['box'],geometry)})
        uncertain.extend(offset+i for i in result['uncertainLines'])
        disagreements.extend({**row,'line':row['line']+offset,'page':ordinal} for row in result.get('disagreements',[]))
        suggestions.extend({**row,'line':row['line']+offset,'page':ordinal} for row in result.get('lexiconSuggestions',[]))
        texts.append(result['text']);engines.add(result['engine'])
        metadata.append({**page,'page':ordinal,'geometry':geometry,'secondaryStatus':result.get('secondaryStatus'),'layoutWarnings':result['layoutWarnings'],'cached':reused,
                         'secondaryFailureCategory':result.get('secondaryFailureCategory'),
                         'lexiconStatus':result.get('lexiconStatus'),'lexiconFailureCategory':result.get('lexiconFailureCategory')})
    text='\n\n'.join(texts)
    if len(text)>15000:raise DomainError('OCR_TEXT_TOO_LONG',422)
    missing=missing_pages(text,len(pages))
    return {'engine':'+'.join(sorted(engines)),'revision':'document-v1','text':text,'lines':combined,'pages':metadata,
            'uncertainLines':uncertain,'disagreements':disagreements,'requiresReview':True,
            'layoutWarnings':sorted({warning for page in metadata for warning in page['layoutWarnings']}),
            'pageOrderRequiresConfirmation':True,'missingPageSuspected':missing,'lexiconSuggestions':suggestions}
