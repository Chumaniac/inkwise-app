"""Image validation and local/HTTP OCR with mandatory text confirmation."""

import asyncio
import logging
import base64
import io
import json
import os
import re
from pathlib import Path
from typing import Annotated

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import Field, ValidationError

from inkwise.contracts import Contract
from inkwise.errors import DomainError, failure_category, scope_denied
from inkwise.provider_io import read_json_response, service_url
from inkwise.settings import Settings


class OCRLine(Contract):
    text: str = Field(max_length=2000)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    box: list[Annotated[float,Field(ge=0,le=1,allow_inf_nan=False)]] = Field(min_length=4, max_length=4)
    alternatives: list[str] = Field(default_factory=list, max_length=3)


class OCRResult(Contract):
    engine: str
    revision: str | int
    lines: list[OCRLine] = Field(max_length=1000)
    requires_review: bool = True

    def confirmed_payload(self) -> dict:
        text = "\n".join(line.text for line in self.lines)
        if not text.strip():
            raise DomainError("OCR_NO_TEXT", 422)
        if len(text) > 15000:
            raise DomainError("OCR_TEXT_TOO_LONG", 422)
        return {**self.model_dump(by_alias=True), "text": text, "requiresReview": True,
                "uncertainLines": [i for i, line in enumerate(self.lines) if line.confidence < .85]}


def normalize_image(data: bytes, settings: Settings) -> bytes:
    """Validate decoded dimensions before allocation and strip EXIF metadata."""
    if len(data) > settings.max_upload_bytes:
        raise DomainError("IMAGE_TOO_LARGE", 413)
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in ("PNG", "JPEG"):
                raise DomainError("UNSUPPORTED_IMAGE", 415)
            if image.width * image.height > settings.max_image_pixels or min(image.size) < 10:
                raise DomainError("IMAGE_DIMENSIONS_INVALID", 413)
            image.verify()
        with Image.open(io.BytesIO(data)) as source:
            normalized = ImageOps.exif_transpose(source).convert("RGB")
            buffer = io.BytesIO()
            normalized.save(buffer, format="PNG", optimize=False)
            result = buffer.getvalue()
            if len(result) > 32 * 1024 * 1024:
                raise DomainError("NORMALIZED_IMAGE_TOO_LARGE", 413)
            return result
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise DomainError("INVALID_IMAGE", 422) from None


def store_image(path: Path, content: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    owned = os.fstat(fd)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(content)
    except BaseException:
        # Only remove this write's inode, never an existing or replaced original.
        try:
            current = path.lstat()
            if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
                path.unlink()
        except FileNotFoundError:
            pass
        raise


async def run_ocr_process(arguments,timeout,input_bytes=None):
    process = await asyncio.create_subprocess_exec(*arguments,
        stdin=asyncio.subprocess.PIPE if input_bytes is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,limit=262144)
    async def read_bounded():
        if input_bytes is not None:
            process.stdin.write(input_bytes);await process.stdin.drain();process.stdin.close()
        output=bytearray()
        while chunk := await process.stdout.read(65536):
            output.extend(chunk)
            if len(output)>262144:raise DomainError("OCR_FAILED",502)
        await process.wait()
        return bytes(output)
    try:
        stdout=await asyncio.wait_for(read_bounded(),timeout)
        if process.returncode!=0:raise DomainError("OCR_FAILED",502)
        return json.loads(stdout)
    except asyncio.CancelledError:raise
    except TimeoutError:raise DomainError("OCR_TIMEOUT",504) from None
    except (ValueError,OSError):raise DomainError("OCR_FAILED",502) from None
    finally:
        if process.returncode is None:process.kill()
        await process.wait()


async def recognize_image(path: Path, settings: Settings, *, tenant: str | None = None, language_policy: str = 'mixed') -> dict:
    if settings.ocr_backend == "vision":
        if not settings.vision_executable.is_file():
            raise DomainError("OCR_NOT_CONFIGURED", 503)
        raw=await run_ocr_process([str(settings.vision_executable.resolve()),str(path.resolve())],settings.ocr_timeout_seconds)
    elif settings.ocr_backend == "trocr":
        if language_policy not in ('en','mixed'):raise DomainError('OCR_BACKEND_LANGUAGE_UNSUPPORTED',422)
        from inkwise.document_ocr import trocr_page
        raw=await trocr_page(path,settings)
    elif settings.ocr_backend == "http":
        from inkwise.provider_io import local_endpoint
        if not local_endpoint(settings.ocr_base_url):raise DomainError("LOCAL_OCR_ENDPOINT_REQUIRED",403)
        data = await asyncio.to_thread(path.read_bytes)
        async with httpx.AsyncClient(timeout=settings.ocr_timeout_seconds, follow_redirects=False, trust_env=False) as client:
            raw = await read_json_response(client, service_url(settings.ocr_base_url, "recognize"),
                        payload={"imageBase64": base64.b64encode(data).decode(), "languageCorrection": False})
    else:
        raise DomainError("OCR_NOT_CONFIGURED", 503)
    try:
        result=OCRResult.model_validate(raw).confirmed_payload()
    except ValidationError:
        raise DomainError("OCR_INVALID_RESPONSE", 502) from None
    except DomainError as error:
        if error.code!='OCR_NO_TEXT' or settings.ocr_secondary_backend!='trocr' or language_policy!='en':raise
        from inkwise.document_ocr import trocr_page
        raw=await trocr_page(path,settings)
        result=OCRResult.model_validate(raw).confirmed_payload()
        result['fallbackReason']='PRIMARY_OCR_NO_TEXT'
        return await enrich_lexicon(result,settings,tenant=tenant,language_policy=language_policy)
    if settings.ocr_backend!='trocr':
        from inkwise.document_ocr import compare_uncertain_lines
        result=await compare_uncertain_lines(path,result,settings,language_policy)
    return await enrich_lexicon(result,settings,tenant=tenant,language_policy=language_policy)


async def enrich_lexicon(result: dict,settings: Settings,*,tenant: str | None,language_policy: str) -> dict:
    """Suggest reviewed spelling candidates without changing the OCR transcript."""
    from inkwise.rubrics import safe_segment
    if not tenant or not settings.lexicon_model_revision:
        return {**result,'lexiconStatus':'not_configured','lexiconSuggestions':[]}
    if language_policy=='dialect':return {**result,'lexiconStatus':'teacher_only_dialect','lexiconSuggestions':[]}
    active=settings.data_dir/'lexicons'/safe_segment(tenant)/'active.json'
    if not active.is_file():return {**result,'lexiconStatus':'not_configured','lexiconSuggestions':[]}
    suggestions=[]
    try:
        from inkwise.lexicon import nearest
        from inkwise.retrieval import Embeddings
        release=json.loads(active.read_text())
        if release['modelRevision']!=settings.lexicon_model_revision:raise DomainError('EMBEDDING_VERSION_MISMATCH')
        indexes=result['uncertainLines'][:8]
        if indexes:
            vectors=await Embeddings(settings).embed([result['lines'][i]['text'][:256] for i in indexes])
            for index,vector in zip(indexes,vectors):
                matches=await asyncio.to_thread(nearest,active.parent/safe_segment(release['file']),vector,
                    model_revision=settings.lexicon_model_revision,tenant=tenant,source_hash=release['sourceHash'],limit=3)
                for match in matches:
                    entry=match['entry'];wrong=entry.get('incorrect','')
                    permitted={'zh','en','mixed'} if language_policy=='mixed' else {language_policy}
                    line=result['lines'][index]['text']
                    found=bool(re.search(r'(?<![A-Za-z])'+re.escape(wrong)+r'(?![A-Za-z])',line,re.I)) if wrong and entry.get('language')=='en' else bool(wrong and wrong in line)
                    if found and entry.get('language') in permitted:
                        suggestions.append({'line':index,'incorrect':wrong,'candidate':entry['correct'],
                            'explanation':entry.get('explanation',''),'sourceHash':release['sourceHash'],'requiresImageCheck':True})
        return {**result,'lexiconStatus':'available','lexiconSuggestions':suggestions}
    except asyncio.CancelledError:raise
    except Exception as error:
        if scope_denied(error): raise
        category=failure_category(error)
        logging.getLogger(__name__).warning('ocr.lexicon degraded category=%s', category)
        return {**result,'lexiconStatus':'unavailable_requires_teacher','lexiconSuggestions':[],
                'lexiconFailureCategory':category}


def ocr_configured(settings):
    from inkwise.provider_io import local_endpoint
    if settings.ocr_backend=='vision':return settings.vision_executable.is_file() and os.access(settings.vision_executable,os.X_OK)
    if settings.ocr_backend=='trocr':
        return all((settings.ocr_model_path/name).is_file() for name in ('model.safetensors','config.json','preprocessor_config.json'))
    return settings.ocr_backend=='http' and bool(settings.ocr_base_url) and local_endpoint(settings.ocr_base_url)
