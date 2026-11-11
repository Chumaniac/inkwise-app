import hashlib
from pathlib import Path

import pytest
from PIL import Image,ImageDraw

from inkwise.document_ocr import missing_pages,original_box,prepare_page,recognize_document
from inkwise.errors import DomainError
from inkwise.settings import Settings


def test_page_markers_do_not_treat_prose_numbers_as_pagination():
    assert missing_pages('Page 1 of 3',2)
    assert missing_pages('第1页 共3页',2)
    assert not missing_pages('A group of 30 students.\nPage 2 of 2',2)
    assert original_box([.1,.2,.3,.4],{'originalSize':[100,200],'processedSize':[100,200],'rotationDegrees':0})==pytest.approx([.1,.2,.3,.4])


@pytest.mark.asyncio
async def test_multi_page_order_and_successful_ocr_reuse(tmp_path,monkeypatch):
    import inkwise.ocr
    settings=Settings(data_dir=tmp_path,ocr_backend='vision')
    folder=tmp_path/'images';folder.mkdir();pages=[];calls=[]
    for number in (1,2):
        path=folder/f'image-{number}.png';image=Image.new('RGB',(100,100),'white');ImageDraw.Draw(image).text((10,10),str(number),fill='black');image.save(path)
        pages.append({'imageId':f'image-{number}','sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'page':number})
    async def recognize(path,*args,**kwargs):
        calls.append(path);text='Synthetic page '+str(len(calls))
        return {'engine':'synthetic','revision':1,'text':text,'lines':[{'text':text,'box':[0,0,1,.2],'confidence':.5,'alternatives':[]}],
                'uncertainLines':[0],'lexiconSuggestions':[],'requiresReview':True}
    monkeypatch.setattr(inkwise.ocr,'recognize_image',recognize)
    first=await recognize_document(pages,settings,tenant='a',job_id='job',language_policy='en')
    second=await recognize_document(pages[::-1],settings,tenant='a',job_id='job',language_policy='en')
    assert len(calls)==2 and first['text']=='Synthetic page 1\n\nSynthetic page 2'
    assert second['text']=='Synthetic page 2\n\nSynthetic page 1'
    # Storage pressure must not cause successful OCR to be regenerated or erased.
    from types import SimpleNamespace
    from inkwise import cache_storage
    monkeypatch.setattr(cache_storage, '_disk_usage', lambda _: SimpleNamespace(free=1))
    reused = await recognize_document(pages,settings,tenant='a',job_id='job',language_policy='en')
    assert all(row['cached'] for row in reused['pages']) and len(calls)==2
    with pytest.raises(DomainError,match='STORAGE_CAPACITY_EXCEEDED'):
        await recognize_document(pages,settings,tenant='a',job_id='new-job',language_policy='en')
    assert len(calls)==2 and all((folder/(page['imageId']+'.png')).exists() for page in pages)
    settings.ocr_deskew=True
    with pytest.raises(DomainError,match='OCR_CACHED_CONFIGURATION_CHANGED'):
        await recognize_document(pages,settings,tenant='a',job_id='job',language_policy='en')
    assert len(calls)==2
    assert all(row['cached'] for row in second['pages'])
    with pytest.raises(DomainError,match='DUPLICATE_PAGE'):
        await recognize_document([pages[0],pages[0]],settings,tenant='a',job_id='job',language_policy='en')


@pytest.mark.asyncio
async def test_second_engine_disagreement_never_rewrites_primary(tmp_path,monkeypatch):
    import inkwise.ocr
    from inkwise.document_ocr import compare_uncertain_lines
    path=tmp_path/'image.png';Image.new('RGB',(300,100),'white').save(path)
    (tmp_path/'model.safetensors').touch()
    settings=Settings(data_dir=tmp_path,ocr_secondary_backend='trocr',ocr_model_path=tmp_path)
    primary={'engine':'primary','text':'I go yesterday','lines':[{'text':'I go yesterday','confidence':.5,'box':[0,0,1,1],'alternatives':[]}],'uncertainLines':[0]}
    async def secondary(*args,**kwargs):return {'engine':'secondary','revision':'synthetic','lines':[{'text':'I went yesterday','confidence':0,'box':[0,0,1,1],'alternatives':[]}]}
    monkeypatch.setattr(inkwise.ocr,'run_ocr_process',secondary)
    result=await compare_uncertain_lines(path,primary,settings,'en')
    assert result['text']=='I go yesterday' and result['lines'][0]['text']=='I go yesterday'
    assert result['disagreements'][0]['secondary']=='I went yesterday'


@pytest.mark.asyncio
async def test_empty_primary_uses_local_handwriting_fallback_only_for_english(tmp_path,monkeypatch):
    import inkwise.ocr,inkwise.document_ocr
    binary=tmp_path/'vision';binary.touch()
    settings=Settings(ocr_backend='vision',vision_executable=binary,ocr_secondary_backend='trocr')
    async def empty(*args,**kwargs):return {'engine':'vision','revision':1,'lines':[]}
    async def fallback(*args,**kwargs):return {'engine':'trocr-handwritten','revision':'synthetic','lines':[{'text':'word','confidence':0,'box':[0,0,1,1],'alternatives':[]}]}
    monkeypatch.setattr(inkwise.ocr,'run_ocr_process',empty);monkeypatch.setattr(inkwise.document_ocr,'trocr_page',fallback)
    result=await inkwise.ocr.recognize_image(tmp_path/'unused',settings,language_policy='en')
    assert result['text']=='word' and result['requiresReview'] and result['fallbackReason']=='PRIMARY_OCR_NO_TEXT'
    with pytest.raises(DomainError,match='OCR_NO_TEXT'):
        await inkwise.ocr.recognize_image(tmp_path/'unused',settings,language_policy='zh')


def test_rotated_box_uses_actual_pillow_inverse_coordinates():
    import numpy as np
    image=Image.new('L',(200,100),255);ImageDraw.Draw(image).rectangle((145,24,151,30),fill=0)
    rotated=image.rotate(4,expand=True,fillcolor=255)
    ys,xs=np.where(np.asarray(rotated)<100)
    box=[float(xs.mean()/rotated.width),float(ys.mean()/rotated.height),0,0]
    original=original_box(box,{'originalSize':[200,100],'processedSize':list(rotated.size),'rotationDegrees':4})
    assert original[:2]==pytest.approx([148/200,27/100],abs=.01)


@pytest.mark.asyncio
async def test_remote_ocr_and_embedding_blocked_before_transmitting(tmp_path):
    from inkwise.ocr import recognize_image
    from inkwise.retrieval import Embeddings
    settings=Settings(ocr_backend='http',ocr_base_url='https://remote.invalid',embedding_base_url='https://remote.invalid')
    with pytest.raises(DomainError,match='LOCAL_OCR_ENDPOINT_REQUIRED'):await recognize_image(tmp_path/'absent',settings)
    with pytest.raises(DomainError,match='LOCAL_EMBEDDING_ENDPOINT_REQUIRED'):await Embeddings(settings).embed(['synthetic'])


def test_complex_geometry_requires_original_image_check():
    from inkwise.document_ocr import layout_warnings
    assert 'MULTICOLUMN_REQUIRES_IMAGE_CHECK' in layout_warnings([{'box':[.05,.1,.3,.05]},{'box':[.6,.1,.3,.05]}])
    assert 'OVERLAPPING_TEXT_REQUIRES_IMAGE_CHECK' in layout_warnings([{'box':[.1,.1,.5,.05]},{'box':[.2,.1,.4,.05]}])
    assert layout_warnings([{'box':[.1,.1,.8,.05]},{'box':[.1,.2,.8,.05]}])==[]

@pytest.mark.asyncio
async def test_optional_secondary_timeout_is_safe_and_cancellation_propagates(tmp_path,monkeypatch,caplog):
    import asyncio
    import inkwise.document_ocr
    path=tmp_path/'synthetic.png';Image.new('RGB',(100,100),'white').save(path)
    settings=Settings(ocr_secondary_backend='trocr',ocr_model_path=tmp_path)
    primary={'text':'Synthetic primary','lines':[{'text':'Synthetic primary','confidence':.5,'box':[0,0,1,1]}],'uncertainLines':[0]}
    async def timeout(*args):raise TimeoutError('SYNTHETIC_PRIVATE_SECONDARY')
    monkeypatch.setattr(inkwise.document_ocr,'run_trocr',timeout)
    result=await inkwise.document_ocr.compare_uncertain_lines(path,primary,settings,'en')
    assert result['text']==primary['text'] and result['secondaryFailureCategory']=='timeout'
    assert 'SYNTHETIC_PRIVATE_SECONDARY' not in caplog.text
    async def cancel(*args):raise asyncio.CancelledError()
    monkeypatch.setattr(inkwise.document_ocr,'run_trocr',cancel)
    with pytest.raises(asyncio.CancelledError):
        await inkwise.document_ocr.compare_uncertain_lines(path,primary,settings,'en')
