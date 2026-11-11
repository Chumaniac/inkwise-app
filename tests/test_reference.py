import json
from pathlib import Path

from inkwise.reference import load_reference_pack, search_references, card_text, validate_reference_hits
from inkwise.retrieval import Hit,Scope

PACK=Path('teaching/reference-pack.json')


def test_public_references_are_scoped_and_not_teacher_scores():
    pack,checksum=load_reference_pack(PACK)
    assert len(pack.cards)==30
    result=search_references(PACK,'zh',7,'标点')
    assert result['use']=='reference-only-not-school-approval'
    assert all(row['language']=='zh' and row['gradeMin']<=7<=row['gradeMax'] for row in result['items'])
    assert result['items'][0]['dimension']=='grammar'
    assert search_references(PACK,'en',1)['items']==[]


def test_disabled_source_invalidates_old_index(tmp_path):
    pack,checksum=load_reference_pack(PACK)
    card=next(card for card in pack.cards if card.language=='zh' and card.grade_max==9)
    hit=Hit('h',card_text(card,pack),{'source':card.id,'referenceHash':checksum},'graph')
    scope=Scope('school','v1','zh','narrative',7,'different')
    assert validate_reference_hits(PACK,scope,[hit])=={'h'}
    data=pack.model_dump(by_alias=True)
    data['sources'][0]['enabled']=False
    path=tmp_path/'pack.json';path.write_text(json.dumps(data))
    assert validate_reference_hits(path,scope,[hit])==set()
    assert validate_reference_hits(PACK,Scope('school','v1','en','narrative',7,'different'),[hit])==set()
