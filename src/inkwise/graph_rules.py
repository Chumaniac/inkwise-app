"""Immutable graph rule releases and normalized weight paths."""

from inkwise.contracts import Rubric
from inkwise.errors import DomainError
from inkwise.rubrics import canonical,digest


async def store_rules(session,tenant: str,rubric: Rubric):
    payload=canonical(rubric.model_dump(mode="json",by_alias=True))
    checksum=digest(payload)
    async def write(tx):
        result=await tx.run("MERGE (v:RubricRelease {tenantId:$tenant,release:$release,id:$id}) "
            "ON CREATE SET v.payload=$payload,v.checksum=$checksum "
            "RETURN v.checksum AS checksum",tenant=tenant,release=rubric.release,id=rubric.id,payload=payload,checksum=checksum)
        row=await result.single()
        if row["checksum"]!=checksum:raise DomainError("IMMUTABLE_RULE_CONFLICT")
        for dimension,weight in rubric.weights.items():
            await tx.run("MATCH (v:RubricRelease {tenantId:$tenant,release:$release,id:$rubric}) "
                "MERGE (d:Dimension {tenantId:$tenant,release:$release,rubricId:$rubric,id:$dimension}) "
                "SET d.weight=$weight MERGE (v)-[p:HAS_DIMENSION]->(d) SET p.weight=$weight/100.0",
                tenant=tenant,release=rubric.release,rubric=rubric.id,dimension=dimension,weight=weight)
        sums={d:sum(r.weight for r in rubric.rules if r.dimension==d) for d in rubric.weights}
        for rule in rubric.rules:
            await tx.run("MATCH (d:Dimension {tenantId:$tenant,release:$release,rubricId:$rubric,id:$dimension}) "
                "MERGE (r:Criterion {tenantId:$tenant,release:$release,rubricId:$rubric,id:$id}) "
                "SET r.description=$description,r.levels=$levels,r.dimension=$dimension "
                "MERGE (d)-[p:HAS_CRITERION]->(r) SET p.weight=$weight",
                tenant=tenant,release=rubric.release,rubric=rubric.id,dimension=rule.dimension,id=rule.id,
                description=rule.description,levels=rule.levels,weight=rule.weight/sums[rule.dimension])
        for rule in rubric.rules:
            for required in rule.requires:
                await tx.run("MATCH (a:Criterion {tenantId:$tenant,release:$release,rubricId:$rubric,id:$id}), "
                    "(b:Criterion {tenantId:$tenant,release:$release,rubricId:$rubric,id:$required}) MERGE (a)-[:REQUIRES]->(b)",
                    tenant=tenant,release=rubric.release,rubric=rubric.id,id=rule.id,required=required)
    await session.execute_write(write)


async def verify_graph_rules(session,tenant: str,expected: Rubric):
    result=await session.run("MATCH (v:RubricRelease {tenantId:$tenant,release:$release,id:$id}) "
        "RETURN v.payload AS payload,v.checksum AS checksum",tenant=tenant,release=expected.release,id=expected.id)
    row=await result.single()
    if row is None:raise DomainError("GRAPH_RULESET_UNAVAILABLE")
    serialized=canonical(expected.model_dump(mode="json",by_alias=True))
    if row["checksum"]!=digest(serialized) or row["payload"]!=serialized:
        raise DomainError("GRAPH_RULESET_CONFLICT")
