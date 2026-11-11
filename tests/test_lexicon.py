import json

import pytest

from inkwise.database import Base,Identity,create_session_factory
from inkwise.errors import DomainError
from inkwise.lexicon import build_index,build_reviewed_lexicon,nearest
from inkwise.settings import Settings


def test_lexicon_runtime_rejects_other_tenant_and_revision(tmp_path):
    path=tmp_path/'words.sqlite';vector=[1.0]+[0.0]*1023
    build_index(path,[{'incorrect':'teh','correct':'the','language':'en'}],[vector],'source','model-v1',tenant='a')
    with pytest.raises(DomainError):nearest(path,vector,model_revision='model-v1',tenant='b')
    with pytest.raises(DomainError):nearest(path,vector,model_revision='wrong',tenant='a')
    with pytest.raises(DomainError):nearest(path,vector,model_revision='model-v1',tenant='a',source_hash='wrong')


@pytest.mark.asyncio
async def test_incremental_build_reuses_successful_embeddings(tmp_path):
    settings=Settings(database_url=f'sqlite:///{tmp_path}/db.sqlite',data_dir=tmp_path/'data')
    engine,sessions=create_session_factory(settings.database_url);Base.metadata.create_all(engine)
    with sessions.begin() as db:
        db.add(Identity(id='author',tenant_id='a',role='teacher',classroom_ids=['c'],token_hash='synthetic-author'))
        db.add(Identity(id='reviewer',tenant_id='a',role='reviewer',classroom_ids=['c'],token_hash='synthetic-reviewer'))
    class Encoder:
        calls=0
        async def embed(self,texts):
            self.calls+=len(texts)
            return [[1.0]+[0.0]*1023 for _ in texts]
    encoder=Encoder();source=tmp_path/'source.json'
    entries=[{'incorrect':'teh','correct':'the','language':'en','explanation':'Synthetic reviewed example'}]
    source.write_text(json.dumps({'provenance':'Original synthetic vocabulary','entries':entries}))
    first=await build_reviewed_lexicon(settings,'a',source,'model-v1','author','reviewer',encoder)
    entries.append({'incorrect':'recieve','correct':'receive','language':'en','explanation':'Synthetic reviewed example'})
    source.write_text(json.dumps({'provenance':'Original synthetic vocabulary','entries':entries}))
    second=await build_reviewed_lexicon(settings,'a',source,'model-v1','author','reviewer',encoder)
    third=await build_reviewed_lexicon(settings,'a',source,'model-v1','author','reviewer',encoder)
    assert first['newEmbeddings']==1
    assert second['newEmbeddings']==1 and second['reusedEmbeddings']==1
    assert third['newEmbeddings']==0 and encoder.calls==2
    engine.dispose()


@pytest.mark.asyncio
async def test_external_candidates_cannot_be_published_as_reviewed(tmp_path):
    from inkwise.cli import provision
    from inkwise.database import initialize_database
    settings=Settings(database_url=f'sqlite:///{tmp_path}/db.sqlite',data_dir=tmp_path/'data')
    initialize_database(settings.database_url)
    author=provision(settings,'a',['c'],'teacher',tmp_path/'teacher')
    reviewer=provision(settings,'a',['c'],'reviewer',tmp_path/'reviewer')
    source=tmp_path/'candidates.json'
    source.write_text(json.dumps({'reviewStatus':'candidate','provenance':'External source',
        'entries':[{'incorrect':'teh','correct':'the','language':'en'}]}))
    class NeverEmbed:
        async def embed(self, texts):
            pytest.fail('Unapproved data must not trigger embedding')
    with pytest.raises(DomainError,match='LEXICON_CANDIDATE_NOT_APPROVED'):
        await build_reviewed_lexicon(settings,'a',source,'v1',author,reviewer,NeverEmbed())
    assert not (settings.data_dir/'lexicons'/'a'/'active.json').exists()
