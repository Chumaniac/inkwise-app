"""Read-only, version-bound navigation over existing report and OCR evidence."""
import math
import re

from pydantic import Field, StrictInt

from inkwise.contracts import Contract
from inkwise.errors import DomainError


class CitationLookup(Contract):
    version: StrictInt = Field(ge=1)
    text_version: StrictInt = Field(ge=1)
    text_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    quote: str = Field(min_length=1, max_length=2000)
    start: StrictInt = Field(ge=0, le=15000)
    end: StrictInt = Field(ge=1, le=15000)


def locate_citation(job, request: CitationLookup):
    if (job.version != request.version or job.text_version != request.text_version
            or job.text_hash != request.text_hash):
        raise DomainError("VERSION_CONFLICT")
    report = job.review or job.result or {}
    evidence = [item.get('evidence', {}) for item in report.get('findings', [])]
    scores = report.get('feedback', {}).values() if job.review else report.get('scores', [])
    evidence.extend(item for score in scores if score.get('include', True) for item in score.get('evidence', []))
    if not any((item.get('start'), item.get('end'), item.get('quote')) ==
               (request.start, request.end, request.quote) for item in evidence):
        raise DomainError("CITATION_NOT_IN_REPORT", 422)
    if job.text[request.start:request.end] != request.quote:
        raise DomainError("CITATION_TEXT_CHANGED", 409)
    result = {'schema': 'inkwise.citation-location/v1', 'jobVersion': job.version,
              'textVersion': job.text_version, 'textHash': job.text_hash,
              'startUtf16': len(job.text[:request.start].encode('utf-16-le')) // 2,
              'endUtf16': len(job.text[:request.end].encode('utf-16-le')) // 2,
              'status': 'text-only', 'reason': 'coordinates-unavailable', 'pages': []}
    # Older reports may locate literal text, but cannot establish image provenance.
    if report.get('textHash') != job.text_hash:
        result['reason'] = 'report-binding-unrecorded'
        return result
    if job.text.find(request.quote, job.text.find(request.quote) + 1) != -1:
        result['reason'] = 'repeated-quote'
        return result
    ocr = job.ocr or {}
    if ocr.get('text') != job.text:
        result['reason'] = 'transcript-edited'
        return result
    pages = {row['page']: row for row in ocr.get('pages', [])[:6]
             if isinstance(row.get('page'), int) and isinstance(row.get('sha256'), str)
             and re.fullmatch(r'[a-f0-9]{64}', row['sha256'])}
    cursor = 0
    located = {}
    covered = []
    for line in ocr.get('lines', [])[:600]:
        text = line.get('text', '')
        if not text:
            continue
        start = job.text.find(text, cursor)
        if start == -1:
            result['reason'] = 'line-order-uncertain'
            return result
        cursor = start + len(text)
        if start >= request.end or cursor <= request.start:
            continue
        page = line.get('page', 1)
        if page not in pages:
            return result
        metadata = pages[page]
        entry = located.setdefault(page, {'page': page, 'imageId': metadata.get('imageId'),
                                         'imageSha256': metadata['sha256'], 'boxes': []})
        box = line.get('box', [])
        if (len(box) != 4 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in box)
                or min(box) < 0 or box[2] <= 0 or box[3] <= 0
                or box[0] + box[2] > 1.000001 or box[1] + box[3] > 1.000001):
            result['pages'] = [{**entry, 'boxes': []}]
            return result
        if len(entry['boxes']) >= 40:
            result['reason'] = 'region-limit'
            return result
        entry['boxes'].append(box)
        covered.append((max(start, request.start), min(cursor, request.end)))
    # Every non-whitespace character must be accounted for; no guessed alignment.
    if any(not char.isspace() and not any(start <= i < end for start, end in covered)
           for i, char in enumerate(job.text[request.start:request.end], request.start)):
        result['reason'] = 'line-order-uncertain'
        return result
    if located:
        result.update(status='region', reason='exact-text-and-source', pages=list(located.values()))
    return result
