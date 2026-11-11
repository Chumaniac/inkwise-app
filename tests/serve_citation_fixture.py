"""Synthetic, loopback-only citation UI fixture. No OCR, model or worker calls."""
import hashlib
import io
import os
from pathlib import Path

import uvicorn
from PIL import Image, ImageDraw

from inkwise.api import create_app
from inkwise.cli import seed_demo
from inkwise.core import DIMENSIONS
from inkwise.database import Base, Identity, Job, TenantQuota, create_session_factory
from inkwise.ocr import store_image
from inkwise.rubrics import RubricRepository, digest
from inkwise.settings import Settings


def main():
    if os.getenv('INKWISE_SYNTHETIC_PREVIEW') != '1':
        raise SystemExit('Explicit synthetic flag required')
    root = Path(os.environ['INKWISE_CITATION_FIXTURE']).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    settings = Settings(database_url=f'sqlite:///{root}/fixture.sqlite', data_dir=root,
                        rubric_dir=root/'rules', model_base_url='', model_name='')
    engine, sessions = create_session_factory(settings.database_url)
    Base.metadata.create_all(engine)
    seed_demo(settings, 'synthetic-citations')
    rubric = RubricRepository(settings.rubric_dir).get('synthetic-citations', 'english-demo').model_dump(by_alias=True)
    text = 'First synthetic sentence.\n\nSecond synthetic sentence.'
    quote = 'Second synthetic sentence.'
    start = text.index(quote)
    pages = []
    for number, line in enumerate(text.split('\n\n'), 1):
        image = Image.new('RGB', (800, 400), 'white')
        ImageDraw.Draw(image).text((80, 80), line, fill='black')
        output = io.BytesIO()
        image.save(output, format='PNG')
        content = output.getvalue()
        identifier = f'synthetic-citation-page-{number}'
        store_image(root/'images'/(identifier+'.png'), content)
        pages.append({'page': number, 'imageId': identifier, 'sha256': hashlib.sha256(content).hexdigest()})
    with sessions.begin() as db:
        db.add(Identity(id='synthetic-teacher', tenant_id='synthetic-citations', classroom_ids=['demo-class'], role='teacher',
                        token_hash=hashlib.sha256(b'synthetic-citation-only').hexdigest()))
        db.add(TenantQuota(tenant_id='synthetic-citations'))
        db.add(Job(id='synthetic-citation', tenant_id='synthetic-citations', classroom_id='demo-class', owner_id='synthetic-teacher',
                   idempotency_key='synthetic-citation', request_hash='synthetic', text=text, text_hash=digest(text), text_version=1,
                   version=3, status='teacher_review', image_id=pages[0]['imageId'], rubric=rubric,
                   payload={'title': '合成引用定位', 'grade': 7, 'classroomId': 'demo-class', 'intent': 'assess'},
                   ocr={'engine': 'synthetic-coordinate-fixture', 'pages': pages, 'text': text, 'uncertainLines': [],
                        'lines': [{'page': n, 'text': line, 'box': [.1, .2, .8, .1]} for n, line in enumerate(text.split('\n\n'), 1)]},
                   result={'total': 75, 'model': 'synthetic-fixture', 'textHash': digest(text), 'scores': [
                       {'dimension': dimension, 'band': 3, 'reason': 'Synthetic reason', 'suggestion': 'Synthetic suggestion',
                        'evidence': [{'start': start, 'end': start+len(quote), 'quote': quote, 'ruleId': rubric['rules'][i]['id']}]}
                       for i, dimension in enumerate(DIMENSIONS)]}))
    try:
        uvicorn.run(create_app(settings), host='127.0.0.1', port=int(os.getenv('INKWISE_PREVIEW_PORT', '18766')), access_log=False)
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
