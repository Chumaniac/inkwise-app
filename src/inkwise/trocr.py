"""Offline, process-isolated TrOCR adapter. Optional dependencies load only here."""
import argparse
import json
import os
import sys


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--model',required=True)
    args=parser.parse_args()
    os.environ['HF_HUB_OFFLINE']='1';os.environ['TRANSFORMERS_OFFLINE']='1'
    os.environ['HF_HUB_DISABLE_TELEMETRY']='1';os.environ['TOKENIZERS_PARALLELISM']='false'
    request=json.loads(sys.stdin.buffer.read(32769))
    if not 1<=len(request.get('lines',[]))<=64:raise ValueError('Invalid line batch')
    import torch
    from PIL import Image
    from transformers import TrOCRProcessor,VisionEncoderDecoderModel
    torch.set_num_threads(2)
    processor=TrOCRProcessor.from_pretrained(args.model,local_files_only=True,token=False)
    model=VisionEncoderDecoderModel.from_pretrained(args.model,local_files_only=True,use_safetensors=True,token=False)
    model.eval();output=[]
    with torch.inference_mode():
        for item in request['lines']:
            with Image.open(item['path']) as source:
                image=source.convert('RGB')
                values=processor(images=image,return_tensors='pt').pixel_values
            ids=model.generate(values,max_new_tokens=128,num_beams=1)
            text=processor.batch_decode(ids,skip_special_tokens=True)[0]
            output.append({'text':text,'confidence':0.0,'box':item['box'],'alternatives':[]})
    print(json.dumps({'engine':'trocr-handwritten','revision':os.path.basename(args.model),
                      'lines':output,'requiresReview':True},ensure_ascii=False))


if __name__=='__main__':
    try:main()
    except Exception:
        sys.stderr.write('OCR_SECONDARY_FAILED\n');raise SystemExit(1) from None
