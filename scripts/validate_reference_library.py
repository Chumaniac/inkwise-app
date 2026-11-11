"""Loopback-only real BGE-M3 plus Neo4j/Chroma reference verification."""
import asyncio
import json
from pathlib import Path

import httpx

from inkwise.cli import seed_demo
from inkwise.database import initialize_database
from inkwise.knowledge import atomic_json
from inkwise.reference import index_references,validate_reference_hits
from inkwise.retrieval import HybridRetriever,Scope
from inkwise.rubrics import RubricRepository
from inkwise.settings import Settings


async def main():
    root=Path('artifacts/reference-library-check');root.mkdir(parents=True,exist_ok=True)
    async with httpx.AsyncClient(timeout=5,trust_env=False) as client:
        response=await client.get('http://127.0.0.1:11434/api/tags');response.raise_for_status()
        revision=next(row['digest'] for row in response.json()['models'] if row['name'] in ('bge-m3','bge-m3:latest'))
    settings=Settings(database_url=f'sqlite:///{root}/db.sqlite',data_dir=root,rubric_dir=root/'rules',
        neo4j_uri='bolt://127.0.0.1:17687',chroma_host='127.0.0.1',chroma_port=18080,
        embedding_base_url='http://127.0.0.1:11434/v1',embedding_model='bge-m3',embedding_revision=revision)
    tenant='inkwise-reference-proof';initialize_database(settings.database_url)
    if not (settings.rubric_dir/tenant/'active.json').exists():seed_demo(settings,tenant)
    result=await index_references(settings,tenant)
    rubric=RubricRepository(settings.rubric_dir).get(tenant,'english-demo')
    scope=Scope(tenant,rubric.release,'en','short-text',7,'new-synthetic-query',rubric.id)
    async def validate(scope,hits):return validate_reference_hits(settings.reference_pack,scope,hits)
    bundle=await HybridRetriever(settings,case_validator=validate).retrieve(scope,rubric,'How can a student improve paragraph order and transitions?')
    assert bundle.examples and not bundle.warnings
    result.update({'realModelRevision':revision,'retrievedReferences':len(bundle.examples),'dualRoute':all(len(row['ranks'])==2 and all(rank for rank in row['ranks']) for row in bundle.provenance),
        'scope':'public curriculum reference indexing and retrieval, not scored student gold'})
    atomic_json(root/'receipt.json',result)
    print(json.dumps(result,indent=2))


if __name__=='__main__':asyncio.run(main())
