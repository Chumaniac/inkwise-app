"""Evaluate original relevance labels against local BGE-M3 and real graph/vector services."""
import asyncio
import json
from pathlib import Path

from inkwise.benchmarks import retrieval_metrics
from inkwise.knowledge import atomic_json
from inkwise.reference import validate_reference_hits
from inkwise.retrieval import HybridRetriever,Scope
from inkwise.rubrics import RubricRepository
from inkwise.settings import Settings


async def main():
    root=Path('artifacts/reference-library-check')
    prior=json.loads((root/'receipt.json').read_text())
    settings=Settings(data_dir=root,rubric_dir=root/'rules',neo4j_uri='bolt://127.0.0.1:17687',chroma_host='127.0.0.1',chroma_port=18080,
        embedding_base_url='http://127.0.0.1:11434/v1',embedding_model='bge-m3',embedding_revision=prior['realModelRevision'])
    definitions=json.loads(Path('teaching/reference-evaluation.json').read_text())
    output=root/'relevance-results.json'
    rows=json.loads(output.read_text()) if output.exists() else []
    for query in definitions['queries']:
        if any(row['id']==query['id'] for row in rows):continue
        rubric=RubricRepository(settings.rubric_dir).get(prior['tenantId'],query['rubricId'])
        scope=Scope(prior['tenantId'],rubric.release,query['language'],query['genre'] if 'genre' in query else rubric.genre,query['grade'],'original-query',rubric.id)
        async def validate(scope,hits):return validate_reference_hits(settings.reference_pack,scope,hits)
        bundle=await HybridRetriever(settings,case_validator=validate).retrieve(scope,rubric,query['query'])
        if bundle.warnings:raise RuntimeError('Real retrieval path degraded; do not record as a successful metric')
        rows.append({'id':query['id'],'relevance':query['relevance'],'retrieved':[item['source'] for item in bundle.examples],
            'mandatory':[rule.id for rule in rubric.rules],'appliedRules':[rule.id for rule in bundle.rubric.rules]})
        atomic_json(output,rows)
    summary={'provenance':definitions['provenance'],'modelRevision':prior['realModelRevision'],'metrics':retrieval_metrics(rows,definitions['k'])}
    atomic_json(root/'relevance-summary.json',summary);print(json.dumps(summary,indent=2))


if __name__=='__main__':asyncio.run(main())
