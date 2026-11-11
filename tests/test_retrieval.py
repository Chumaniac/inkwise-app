import pytest

from inkwise.cli import seed_demo
from inkwise.retrieval import Hit, HybridRetriever, Scope
from inkwise.rubrics import RubricRepository
from inkwise.settings import Settings


class Route:
    def __init__(self, hits=None, error=False):
        self.hits, self.error = hits, error

    async def query(self, *args):
        if self.error:
            raise TimeoutError("synthetic")
        return self.hits


def hit(identifier, text="Reviewed synthetic case", tenant="a"):
    return Hit(identifier, text, {"tenantId": tenant, "release": "synthetic-v1", "languagePolicy": "en",
        "genre": "short-text", "rubricId":"english-demo", "gradeMin": 5, "gradeMax": 12, "approved": True}, "test")


@pytest.fixture
def context(tmp_path):
    settings = Settings(rubric_dir=tmp_path)
    seed_demo(settings, "a")
    return settings, RubricRepository(tmp_path).get("a", "english-demo"), Scope("a", "synthetic-v1", "en", "short-text", 7, "essay-hash")


@pytest.mark.asyncio
async def test_cross_tenant_hits_are_removed_even_if_provider_returns_them(context):
    settings, rubric, scope = context
    retriever = HybridRetriever(settings, Route([hit("a"), hit("b", tenant="b")]), Route([hit("a")]))
    result = await retriever.retrieve(scope, rubric, "query")
    assert [e["id"] for e in result.examples] == ["a"]
    assert "OUT_OF_SCOPE_EVIDENCE_REJECTED" in result.warnings
    assert result.rubric is rubric


@pytest.mark.asyncio
async def test_both_routes_down_keep_exact_mandatory_rules(context):
    settings, rubric, scope = context
    result = await HybridRetriever(settings, Route(error=True), Route(error=True)).retrieve(scope, rubric, "query")
    assert result.examples == [] and result.rubric is rubric
    assert len(result.warnings) == 2


@pytest.mark.asyncio
async def test_conflicting_same_id_is_not_arbitrarily_selected(context):
    settings, rubric, scope = context
    result = await HybridRetriever(settings, Route([hit("x", "Version one")]), Route([hit("x", "Version two")])).retrieve(scope, rubric, "query")
    assert result.examples == []
    assert "EVIDENCE_VERSION_CONFLICT" in result.warnings


@pytest.mark.asyncio
async def test_teacher_case_requires_live_authorization_not_index_boolean(context):
    settings,rubric,scope=context
    record=hit("case")
    record.metadata["sourceKind"]="teacher-case"
    result=await HybridRetriever(settings,Route([record]),Route([])).retrieve(scope,rubric,"query")
    assert result.examples==[]
    assert "REVOKED_OR_UNVERIFIED_CASE_REJECTED" in result.warnings


@pytest.mark.asyncio
async def test_same_school_different_rubric_is_not_a_valid_anchor(context):
    settings,rubric,scope=context
    wrong=hit('wrong');wrong.metadata['rubricId']='different-teacher-standard'
    result=await HybridRetriever(settings,Route([wrong]),Route([])).retrieve(scope,rubric,'query')
    assert result.examples==[]


@pytest.mark.asyncio
async def test_one_case_cannot_fill_the_entire_few_shot_context(context):
    settings,rubric,scope=context
    rows=[hit(str(i)) for i in range(4)]+[hit('other')]
    for row in rows:row.metadata['source']='same-case'
    rows[-1].metadata['source']='other-case'
    result=await HybridRetriever(settings,Route(rows),Route([])).retrieve(scope,rubric,'query')
    assert len(result.examples)==3
    assert any(item['source']=='other-case' for item in result.examples)

@pytest.mark.asyncio
async def test_optional_route_failure_is_classified_without_raw_error(context, caplog):
    class Broken:
        async def query(self, *args):
            raise TypeError('SYNTHETIC_PRIVATE_ROUTE_EXCEPTION')
    settings, rubric, scope = context
    result = await HybridRetriever(settings, Broken(), Route([])).retrieve(scope, rubric, 'synthetic')
    assert result.rubric is rubric and result.examples == []
    assert 'category=internal' in caplog.text
    assert 'SYNTHETIC_PRIVATE_ROUTE_EXCEPTION' not in caplog.text

@pytest.mark.asyncio
async def test_scope_denial_is_never_silently_downgraded(context):
    from inkwise.errors import DomainError
    class Denied:
        async def query(self, *args):
            raise DomainError('RULESET_SCOPE_MISMATCH', 403)
    settings, rubric, scope = context
    with pytest.raises(DomainError, match='RULESET_SCOPE_MISMATCH'):
        await HybridRetriever(settings, Denied(), Route([])).retrieve(scope, rubric, 'synthetic')

@pytest.mark.asyncio
async def test_scope_denial_cancels_the_other_inflight_route(context):
    import asyncio
    from inkwise.errors import DomainError
    finished=[]
    class Slow:
        async def query(self,*args):
            try:await asyncio.sleep(10)
            finally:finished.append(True)
    class Denied:
        async def query(self,*args):
            await asyncio.sleep(0)
            raise DomainError('RULESET_SCOPE_MISMATCH',403)
    settings,rubric,scope=context
    with pytest.raises(DomainError):
        await HybridRetriever(settings,Denied(),Slow()).retrieve(scope,rubric,'synthetic')
    assert finished==[True]
