"""Prepare only published numeric ratings, never essays, from a CC BY study."""
import argparse
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import httpx

URL='https://data.mendeley.com/public-files/datasets/8fbzsg82p9/files/9562d278-d62b-47dd-a81c-d4ca49f65090/file_downloaded'
EXPECTED='4e2e377a5add54d26e8b759e9c4c2b1f560cc4aba330d4e23fba7b0ebf90cf1a'
NS={'x':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


def download_ratings():
    buffer=bytearray()
    with httpx.Client(timeout=45,follow_redirects=True) as client:
        with client.stream('GET',URL) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                buffer.extend(chunk)
                if len(buffer)>1_000_000:raise ValueError('Published archive exceeds pinned size limit')
    with zipfile.ZipFile(io.BytesIO(buffer)) as archive:
        name=next(name for name in archive.namelist() if name.endswith("Scores of Mannual and ChatGPT's Rating.xlsx"))
        if archive.getinfo(name).file_size>2_000_000:raise ValueError('Rating workbook exceeds limit')
        return archive.read(name)


def prepare(raw):
    if hashlib.sha256(raw).hexdigest()!=EXPECTED:raise ValueError('Published rating workbook checksum changed')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        strings=[''.join(row.itertext()) for row in ET.fromstring(archive.read('xl/sharedStrings.xml')).findall('x:si',NS)]
        sheet=ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
    rows=[]
    for row in sheet.findall('.//x:row',NS):
        cells={}
        for cell in row.findall('x:c',NS):
            value=cell.find('x:v',NS)
            if value is None:continue
            column=re.match(r'[A-Z]+',cell.attrib['r'])[0]
            cells[column]=strings[int(value.text)] if cell.attrib.get('t')=='s' else float(value.text)
        identifier=cells.get('A')
        if not isinstance(identifier,str) or not re.fullmatch(r'WEXP\d+',identifier):continue
        opaque=hashlib.sha256(('mendeley-8fbzsg82p9:'+identifier).encode()).hexdigest()[:20]
        rows.append({'id':opaque,'subject':opaque,'teacherScores':[cells['B'],cells['C'],cells['D']],
                     'prediction':cells['T'],'rawConfidence':1-abs(cells['L']-cells['P'])/100,'group':'adult-efl-en'})
    if len(rows)!=82:raise ValueError('Unexpected study record count')
    rows.sort(key=lambda row:row['id'])
    for index,row in enumerate(rows):row['split']='train' if index<40 else 'calibration' if index<60 else 'test'
    return {'source':{'id':'mendeley-8fbzsg82p9-v1','url':'https://data.mendeley.com/datasets/8fbzsg82p9/1',
        'license':'CC-BY-4.0','provenance':'Yang, Yap and Ali (2023), published human ratings and historical ChatGPT scores; numeric columns only',
        'domain':'adult EFL, not K12','model':'historical ChatGPT ratings from source, not Inkwise inference','workbookSha256':EXPECTED},
        'referenceOnly':True,'featureDefinition':'historical-two-runs-agreement-v1','scale':{'maximum':100,'tolerance':5},'rows':rows}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ratings',type=Path)
    parser.add_argument('--output',type=Path,default=Path('artifacts/public-evaluation/study-input.json'))
    args=parser.parse_args()
    document=prepare(args.ratings.read_bytes() if args.ratings else download_ratings())
    args.output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    content=json.dumps(document,ensure_ascii=False,indent=2)+'\n'
    if args.output.exists() and args.output.read_text()!=content:raise SystemExit('Refusing to replace a different successful input')
    args.output.write_text(content);args.output.chmod(0o600)
    print('Prepared 82 numeric-only reference records: train 40, calibration 20, test 22')
