"""Compare cached local engines on a small licensed public word-crop pack."""
import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path

from inkwise.benchmarks import ocr_metrics
from inkwise.knowledge import atomic_json
from inkwise.ocr import OCRResult,run_ocr_process
from inkwise.settings import Settings


async def main():
    folder=Path('artifacts/handwriting-public');manifest=json.loads((folder/'manifest.json').read_text())
    output=Path('artifacts/handwriting-check');output.mkdir(parents=True,exist_ok=True)
    settings=Settings()
    rows=manifest['rows'][:12]
    for row in rows:
        if hashlib.sha256((folder/row['image']).read_bytes()).hexdigest()!=row['sha256']:
            raise ValueError('Preserved image checksum mismatch')
    vision_file=output/'vision.json'
    if vision_file.exists():vision=json.loads(vision_file.read_text())
    else:vision=[]
    saved={row['id'] for row in vision}
    for row in rows:
        if row['id'] in saved:continue
        start=time.perf_counter()
        try:
            result=await run_ocr_process([str(settings.vision_executable.resolve()),str((folder/row['image']).resolve())],60)
            text=' '.join(line.text for line in OCRResult.model_validate(result).lines)
            status='succeeded'
        except Exception:text='';status='failed'
        vision.append({'id':row['id'],'reference':row['reference'],'prediction':text,'status':status,
                       'durationMs':(time.perf_counter()-start)*1000,'inputHash':row['sha256']})
        atomic_json(vision_file,vision)
    trocr_file=output/'trocr.json'
    if trocr_file.exists():trocr=json.loads(trocr_file.read_text())
    else:
        start=time.perf_counter()
        result=await run_ocr_process([sys.executable,'-m','inkwise.trocr','--model',str(settings.ocr_model_path.resolve())],180,
            json.dumps({'lines':[{'path':str((folder/row['image']).resolve()),'box':[0,0,1,1]} for row in rows]}).encode())
        result=OCRResult.model_validate(result)
        if len(result.lines)!=len(rows):raise ValueError('Incomplete model output')
        trocr=[{'id':row['id'],'reference':row['reference'],'prediction':line.text,'inputHash':row['sha256']} for row,line in zip(rows,result.lines)]
        atomic_json(trocr_file,trocr)
        atomic_json(output/'trocr-timing.json',{'batchMs':(time.perf_counter()-start)*1000,'rows':len(rows)})
    summary={'source':manifest['source'],'mirrorRevision':manifest['mirrorRevision'],'scope':manifest['scope'],
             'vision':ocr_metrics(vision),'trocr':ocr_metrics(trocr),
             'visionCaseFolded':ocr_metrics([{**row,'reference':row['reference'].casefold(),'prediction':row['prediction'].casefold()} for row in vision]),
             'trocrCaseFolded':ocr_metrics([{**row,'reference':row['reference'].casefold(),'prediction':row['prediction'].casefold()} for row in trocr]),'modelRevision':settings.ocr_model_path.name,
             'acceptance':'real engine execution and measured errors on selected public words; not K12 handwriting accuracy'}
    atomic_json(output/'summary.json',summary)
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=='__main__':asyncio.run(main())
