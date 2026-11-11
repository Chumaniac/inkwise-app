import io

import pytest
from PIL import Image

from inkwise.errors import DomainError
from inkwise.ocr import OCRResult, normalize_image
from inkwise.settings import Settings


def image_bytes(size=(100, 100), format="PNG"):
    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, format=format)
    return buffer.getvalue()


def test_image_must_decode_and_have_bounded_pixels():
    assert normalize_image(image_bytes(), Settings()).startswith(b"\x89PNG")
    for data in (b"<script>fake png</script>", image_bytes(format="GIF")):
        with pytest.raises(DomainError):
            normalize_image(data, Settings())
    with pytest.raises(DomainError):
        normalize_image(image_bytes(), Settings(max_image_pixels=100))


def test_uncertain_lines_are_preserved_and_never_auto_confirmed():
    result = OCRResult(engine="synthetic", revision=1, requiresReview=False,
        lines=[{"text": "I go yesterday", "confidence": .5, "box": [0, 0, 1, .1]}]).confirmed_payload()
    assert result["text"] == "I go yesterday"
    assert result["requiresReview"] is True
    assert result["uncertainLines"] == [0]


def test_non_finite_or_unbounded_ocr_boxes_are_rejected():
    from pydantic import ValidationError
    for box in ([float('nan'),0,1,.1],[0,0,20,.1]):
        with pytest.raises(ValidationError):
            OCRResult(engine='synthetic',revision=1,lines=[{'text':'Test','confidence':.5,'box':box}])


@pytest.mark.asyncio
async def test_native_ocr_output_is_bounded_before_process_exit(tmp_path):
    import sys
    from inkwise.ocr import recognize_image
    binary=tmp_path/'synthetic-ocr'
    binary.write_text('#!'+sys.executable+'\nimport os,time\nos.write(1,b"x"*300000)\ntime.sleep(5)\n')
    binary.chmod(0o700)
    with pytest.raises(DomainError,match='OCR_FAILED'):
        await recognize_image(tmp_path/'unused.png',Settings(ocr_backend='vision',vision_executable=binary,ocr_timeout_seconds=2))

@pytest.mark.asyncio
async def test_optional_lexicon_failure_category_preserves_transcript(tmp_path, monkeypatch, caplog):
    import json
    from inkwise.ocr import enrich_lexicon
    from inkwise.retrieval import Embeddings
    active=tmp_path/'lexicons'/'tenant'/'active.json';active.parent.mkdir(parents=True)
    active.write_text(json.dumps({'modelRevision':'synthetic-v1'}))
    async def fail(*args, **kwargs): raise TypeError('SYNTHETIC_PRIVATE_LEXICON_ERROR')
    monkeypatch.setattr(Embeddings,'embed',fail)
    primary={'text':'Synthetic transcript','lines':[{'text':'Synthetic transcript'}],'uncertainLines':[0]}
    result=await enrich_lexicon(primary,Settings(data_dir=tmp_path,lexicon_model_revision='synthetic-v1'),tenant='tenant',language_policy='en')
    assert result['text']==primary['text'] and result['lexiconSuggestions']==[]
    assert result['lexiconFailureCategory']=='internal'
    assert 'SYNTHETIC_PRIVATE_LEXICON_ERROR' not in caplog.text
