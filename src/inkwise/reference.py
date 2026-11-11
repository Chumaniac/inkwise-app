"""Versioned public curriculum references, kept separate from teacher-rated anchors."""

import json
import re
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, model_validator

from inkwise.contracts import Contract, Dimension
from inkwise.errors import DomainError
from inkwise.rubrics import canonical, digest


class ReferenceSource(Contract):
    id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=300)
    publisher: str = Field(min_length=1, max_length=150)
    url: str = Field(max_length=1000)
    license: str = Field(min_length=1, max_length=200)
    use: str = Field(min_length=1, max_length=500)
    enabled: bool = True

    @model_validator(mode='after')
    def public_url(self):
        parsed=urlparse(self.url)
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Public HTTPS source URL required')
        return self


class ReferenceCard(Contract):
    id: str = Field(min_length=1, max_length=100, pattern=r'^[a-z0-9-]+$')
    title: str = Field(min_length=1, max_length=160)
    language: str = Field(pattern=r'^(zh|en|mixed)$')
    grade_min: int = Field(ge=1,le=12)
    grade_max: int = Field(ge=1,le=12)
    dimension: Dimension
    body: str = Field(min_length=1,max_length=1600)
    source_ids: list[str] = Field(min_length=1,max_length=5)
    enabled: bool = True

    @model_validator(mode='after')
    def ordered_grades(self):
        if self.grade_min>self.grade_max:raise ValueError('Invalid grade range')
        return self


class ReferencePack(Contract):
    version: str = Field(min_length=1,max_length=100)
    checked_on: str
    attribution: str
    sources: list[ReferenceSource] = Field(min_length=1,max_length=50)
    cards: list[ReferenceCard] = Field(min_length=1,max_length=400)

    @model_validator(mode='after')
    def valid_references(self):
        ids={source.id for source in self.sources}
        if len(ids)!=len(self.sources) or len({card.id for card in self.cards})!=len(self.cards):
            raise ValueError('Duplicate source/card')
        if any(not set(card.source_ids)<=ids for card in self.cards):raise ValueError('Unknown source')
        return self


def load_reference_pack(path: Path) -> tuple[ReferencePack,str]:
    try:
        with path.open('rb') as stream:raw=stream.read(2_000_001)
        if len(raw)>2_000_000:raise DomainError('REFERENCE_PACK_TOO_LARGE')
        pack=ReferencePack.model_validate_json(raw)
        return pack,digest(canonical(pack.model_dump(by_alias=True)))
    except (OSError,ValueError):
        raise DomainError('REFERENCE_PACK_INVALID') from None


def card_text(card,pack):
    sources={source.id:source for source in pack.sources}
    return canonical({'kind':'curriculum-reference-not-scored-example','title':card.title,'guidance':card.body,
        'sources':[{'id':key,'title':sources[key].title,'url':sources[key].url} for key in card.source_ids]})


def eligible_cards(pack, language, grade):
    enabled={source.id for source in pack.sources if source.enabled}
    return [card for card in pack.cards if card.enabled and card.language==language
            and card.grade_min<=grade<=card.grade_max and set(card.source_ids)<=enabled]


def search_references(path: Path, language: str, grade: int, query: str='', limit: int=12):
    if language not in ('zh','en','mixed') or not 1<=grade<=12 or not 1<=limit<=20:
        raise DomainError('REFERENCE_QUERY_INVALID',422)
    pack,checksum=load_reference_pack(path)
    terms=set(re.findall(r'[a-z]{2,}|[\u3400-\u9fff]',query.lower()[:1000]))
    cards=eligible_cards(pack,language,grade)
    cards.sort(key=lambda card:(-sum(term in (card.title+' '+card.body).lower() for term in terms),card.id))
    sources={source.id:source.model_dump(by_alias=True) for source in pack.sources}
    return {'version':pack.version,'packHash':checksum,'use':'reference-only-not-school-approval',
        'items':[{**card.model_dump(by_alias=True),'sources':[sources[key] for key in card.source_ids]} for card in cards[:limit]]}


def validate_reference_hits(path, scope, hits):
    """An old index cannot resurrect a disabled or modified public reference."""
    pack,checksum=load_reference_pack(path)
    cards={card.id:card for card in eligible_cards(pack,scope.language,scope.grade)}
    allowed=set()
    for hit in hits:
        card=cards.get(hit.metadata.get('source'))
        if card and hit.metadata.get('referenceHash')==checksum and hit.text==card_text(card,pack):allowed.add(hit.id)
    return allowed


async def index_references(settings, tenant, embeddings=None):
    """Bind approved-source references to each active tenant rubric namespace."""
    from neo4j import AsyncGraphDatabase
    from inkwise.chroma_store import ChromaStore
    from inkwise.graph_rules import store_rules
    from inkwise.retrieval import Embeddings
    from inkwise.rubrics import RubricRepository, safe_segment
    from inkwise.contracts import Rubric
    from inkwise.cache_storage import atomic_json
    safe_segment(tenant)
    pack,checksum=load_reference_pack(settings.reference_pack)
    rules=RubricRepository(settings.rubric_dir).list(tenant)
    if not rules or not settings.neo4j_uri or not settings.chroma_host:raise DomainError('RETRIEVAL_NOT_CONFIGURED')
    encoder=embeddings or Embeddings(settings)
    auth=(settings.neo4j_user,settings.neo4j_password.get_secret_value()) if settings.neo4j_user else None
    # Cache successful public-reference embeddings across rubric namespaces/restarts.
    import sqlite3, struct
    cache_dir=settings.data_dir/'reference-index'/tenant
    cache_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
    cache=sqlite3.connect(cache_dir/'vectors.sqlite')
    cache.execute('CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, vector BLOB)')
    revision=settings.embedding_revision or settings.embedding_model
    async def cached_embed(documents):
        vectors=[]
        for document in documents:
            key=digest(canonical({'revision':revision,'endpoint':settings.embedding_base_url,'text':document}))
            found=cache.execute('SELECT vector FROM vectors WHERE key=?',(key,)).fetchone()
            if found:
                vectors.append(list(struct.unpack('<1024f',found[0])))
                continue
            if cache.execute('SELECT count(*) FROM vectors').fetchone()[0]>=10000:
                raise DomainError('REFERENCE_CACHE_ARCHIVE_REQUIRED')
            vector=(await encoder.embed([document]))[0]
            with cache:cache.execute('INSERT INTO vectors VALUES (?,?)',(key,struct.pack('<1024f',*vector)))
            vectors.append(vector)
        return vectors
    count=0
    try:
        async with AsyncGraphDatabase.driver(settings.neo4j_uri,auth=auth) as driver:
            async with driver.session() as session, ChromaStore(settings.chroma_host,settings.chroma_port,settings.chroma_ssl) as chroma:
                for raw in rules:
                    rubric=Rubric.model_validate(raw)
                    await store_rules(session,tenant,rubric)
                    cards=[card for card in pack.cards if card.language==rubric.language_policy
                        and max(card.grade_min,rubric.grade_min)<=min(card.grade_max,rubric.grade_max)
                        and card in eligible_cards(pack,card.language,max(card.grade_min,rubric.grade_min))]
                    for start in range(0,len(cards),8):
                        batch=cards[start:start+8];documents=[card_text(card,pack) for card in batch]
                        vectors=await cached_embed(documents)
                        identifiers=[f'ref:{tenant}:{rubric.id}:{rubric.release}:{card.id}:{checksum[:12]}' for card in batch]
                        metadata=[{'tenantId':tenant,'release':rubric.release,'rubricId':rubric.id,
                            'languagePolicy':card.language,'genre':rubric.genre,'gradeMin':max(card.grade_min,rubric.grade_min),
                            'gradeMax':min(card.grade_max,rubric.grade_max),'approved':True,'sourceKind':'curriculum-reference',
                            'source':card.id,'referenceHash':checksum,'textHash':digest(document)} for card,document in zip(batch,documents)]
                        await chroma.add(ids=identifiers,embeddings=vectors,documents=documents,metadatas=metadata)
                        for card,identifier,document,meta in zip(batch,identifiers,documents,metadata):
                            await session.run('MERGE (e:Evidence {tenantId:$tenant,release:$release,id:$id}) SET e=$props '
                                'WITH e MATCH (r:Criterion {tenantId:$tenant,release:$release,rubricId:$rubric,dimension:$dimension}) '
                                'MERGE (r)-[:EXEMPLIFIED_BY]->(e)',tenant=tenant,release=rubric.release,id=identifier,
                                rubric=rubric.id,dimension=card.dimension,props={**meta,'id':identifier,'text':document})
                        count+=len(batch)
    finally:
        cache.close()
    receipt={'packHash':checksum,'version':pack.version,'tenantId':tenant,'indexedReferences':count,'schoolApproved':False}
    atomic_json(settings.data_dir/'reference-index'/tenant/'receipt.json',receipt)
    return receipt
