"""Synthetic HTTP tests exercise protocol failures, not model quality."""

import json

import httpx
import pytest

from inkwise.cli import seed_demo
from inkwise.contracts import DimensionScore
from inkwise.errors import DomainError
from inkwise.providers import CallBudget, ModelGateway, score_dimension
from inkwise.rubrics import RubricRepository
from inkwise.settings import Settings


@pytest.fixture
def rubric(tmp_path):
    settings = Settings(rubric_dir=tmp_path)
    seed_demo(settings, "test")
    return RubricRepository(tmp_path).get("test", "english-demo")


def result(quote="A synthetic essay", rule="english-demo-R1", finish="stop"):
    data = {"dimension": "content", "band": 3, "reason": "Specific event is described.",
            "evidence": [{"start": 0, "end": len(quote), "quote": quote, "ruleId": rule}],
            "suggestion": "Add one concrete detail."}
    return {"choices": [{"finish_reason": finish, "message": {"content": json.dumps(data)}}]}


@pytest.mark.asyncio
async def test_valid_model_response_preserves_evidence(rubric):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=result())))
    gateway = ModelGateway(Settings(model_name="synthetic-test", model_base_url="http://provider.invalid/v1"), client)
    async with client:
        score = await score_dimension(gateway, text="A synthetic essay", prompt="Story", rubric=rubric,
                                        dimension="content", examples=[], budget=CallBudget(1))
    assert score.band == 3 and score.evidence[0].quote == "A synthetic essay"


@pytest.mark.asyncio
@pytest.mark.parametrize("response,code", [(result(quote="Invented quote"), "EVIDENCE_QUOTE_INVALID"),
    (result(rule="another-tenant-rule"), "EVIDENCE_RULE_INVALID"), (result(finish="length"), "MODEL_OUTPUT_INCOMPLETE")])
async def test_untrusted_model_output_cannot_pass_checks(rubric, response, code):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=response))) as client:
        gateway = ModelGateway(Settings(model_name="synthetic", model_base_url="http://provider.invalid/v1"), client)
        with pytest.raises(DomainError) as error:
            await score_dimension(gateway, text="A synthetic essay", prompt="Story", rubric=rubric,
                                  dimension="content", examples=[], budget=CallBudget(1))
        assert error.value.code == code


@pytest.mark.asyncio
async def test_timeout_is_not_automatically_retried_or_mocked(rubric):
    calls = []
    def fail(request):
        calls.append(request)
        raise httpx.ReadTimeout("sensitive provider details must not escape")
    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        gateway = ModelGateway(Settings(model_name="synthetic", model_base_url="http://provider.invalid/v1"), client)
        with pytest.raises(DomainError) as error:
            await score_dimension(gateway, text="A synthetic essay", prompt="Story", rubric=rubric,
                                  dimension="content", examples=[], budget=CallBudget(1))
    assert error.value.code == "PROVIDER_TIMEOUT_UNKNOWN"
    assert len(calls) == 1


def test_budget_is_a_hard_limit():
    budget = CallBudget(1)
    budget.consume()
    with pytest.raises(DomainError):
        budget.consume()


@pytest.mark.asyncio
@pytest.mark.parametrize("broken_rule",[False,True])
async def test_quote_repair_selects_immutable_source_anchor(rubric,broken_rule):
    calls=[]
    def provider(request):
        calls.append(request)
        if len(calls)==1:
            payload=result(quote="A synthetic essay" if broken_rule else "A paraphrase that was never written")
            if broken_rule:
                content=json.loads(payload['choices'][0]['message']['content']);content['evidence'][0]['ruleId']='invented-rule'
                payload['choices'][0]['message']['content']=json.dumps(content)
            return httpx.Response(200,json=payload)
        draft={"dimension":"content","band":3,"reason":"Evidence-grounded re-evaluation.",
               "evidence":[{"anchorId":"a0","ruleId":"english-demo-R1"}],"suggestion":"Add detail."}
        return httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{"content":json.dumps(draft)}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        gateway=ModelGateway(Settings(model_name="synthetic",model_base_url="http://provider.invalid/v1"),client)
        score=await score_dimension(gateway,text="A synthetic essay",prompt="Story",rubric=rubric,
                                    dimension="content",examples=[],budget=CallBudget(2))
    assert len(calls)==2
    assert score.evidence[0].quote=="A synthetic essay" and score.evidence[0].start==0


@pytest.mark.asyncio
async def test_mixed_language_dimension_uses_declared_weights_and_scoped_evidence(tmp_path):
    from inkwise.cli import seed_demo
    from inkwise.contracts import MixedDimensionScore,LanguageRules
    from inkwise.language import language_context
    from inkwise.providers import score_dimension,CallBudget
    from inkwise.rubrics import RubricRepository
    from inkwise.settings import Settings
    settings=Settings(rubric_dir=tmp_path);seed_demo(settings,'a')
    rubric=RubricRepository(tmp_path).get('a','mixed-demo')
    rules=LanguageRules(id='synthetic-mixed',release='v1',mode='mixed',allowedLanguages=['zh','en'],
        languageWeights={'zh':.8,'en':.2},source='Synthetic explicit language weights')
    rubric=rubric.model_copy(update={'language_rules':rules})
    text='今天阳光很好。 We walked together after school.'
    rule=next(r.id for r in rubric.rules if r.dimension=='grammar')
    routing=language_context(text,rubric)
    class Model:
        language_context=routing
        async def complete(self,schema,*args):
            assert schema is MixedDimensionScore
            return schema(dimension='grammar',languageScores=[
                {'language':'zh','band':4,'reason':'Synthetic Chinese result','suggestion':'Keep it',
                 'evidence':[{'start':0,'end':6,'quote':text[:6],'ruleId':rule}]},
                {'language':'en','band':2,'reason':'Synthetic English result','suggestion':'Check it',
                 'evidence':[{'start':text.index('We'),'end':len(text),'quote':text[text.index('We'):],'ruleId':rule}]}])
    score=await score_dimension(Model(),text=text,prompt='Synthetic',rubric=rubric,dimension='grammar',examples=[],budget=CallBudget(1))
    assert score.band==4 and len(score.language_scores)==2


@pytest.mark.asyncio
async def test_protected_term_cannot_be_only_negative_grammar_evidence(tmp_path):
    from inkwise.cli import seed_demo
    from inkwise.contracts import DimensionScore,LanguageRules
    from inkwise.errors import DomainError
    from inkwise.language import language_context
    from inkwise.providers import score_dimension,CallBudget
    from inkwise.rubrics import RubricRepository
    from inkwise.settings import Settings
    settings=Settings(rubric_dir=tmp_path);seed_demo(settings,'a')
    rubric=RubricRepository(tmp_path).get('a','chinese-demo')
    policy=LanguageRules(id='dialect-example',release='v1',mode='dialect',allowedLanguages=['zh'],
        languageWeights={'zh':1},protectedTerms=['侬'],source='Synthetic policy')
    rubric=rubric.model_copy(update={'language_policy':'dialect','language_rules':policy})
    routing=language_context('侬好！',rubric)
    rule=next(row.id for row in rubric.rules if row.dimension=='grammar')
    class Model:
        language_context=routing
        async def complete(self,*args):return DimensionScore(dimension='grammar',band=1,reason='Synthetic prohibited deduction',
            suggestion='Synthetic replacement',evidence=[{'start':0,'end':1,'quote':'侬','ruleId':rule}])
    with pytest.raises(DomainError,match='PROTECTED_LANGUAGE_EVIDENCE'):
        await score_dimension(Model(),text='侬好！',prompt='test',rubric=rubric,dimension='grammar',examples=[],budget=CallBudget(1))


@pytest.mark.asyncio
async def test_wrong_dimension_in_repair_gets_one_schema_narrowed_request(rubric):
    calls=[]
    def provider(request):
        calls.append(json.loads(request.content))
        if len(calls)==1:return httpx.Response(200,json=result(quote='Not in the original'))
        draft={'dimension':'grammar' if len(calls)==2 else 'content','band':3,'reason':'Synthetic reason',
            'evidence':[{'anchorId':'a0','ruleId':'english-demo-R1'}],'suggestion':'Add detail.'}
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(draft)}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        gateway=ModelGateway(Settings(model_name='synthetic',model_base_url='http://provider.invalid/v1',model_structured_output='json_schema'),client)
        score=await score_dimension(gateway,text='A synthetic essay',prompt='Story',rubric=rubric,dimension='content',examples=[],budget=CallBudget(3))
    assert len(calls)==3 and score.dimension=='content'
    assert calls[-1]['response_format']['json_schema']['schema']['properties']['dimension']['const']=='content'
