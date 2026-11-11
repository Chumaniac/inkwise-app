from inkwise.language import language_spans


def test_language_spans_preserve_unicode_and_do_not_infer_dialect():
    text='😀他说 Hello，侬好！'
    result=language_spans(text,'mixed')
    assert ''.join(text[s['start']:s['end']] for s in result['spans'])==text
    assert {s['language'] for s in result['spans']}=={'unknown','zh','latin'}
    assert result['confidence'] is None
    assert language_spans('侬好','dialect')['warnings']==['DIALECT_REQUIRES_TEACHER_RULESET']


def test_trained_local_detection_and_ambiguous_short_text():
    import pytest
    pytest.importorskip('lingua')
    from inkwise.language import detect_language_spans
    text='今天我们一起学习。 We wrote a short story about our school.'
    result=detect_language_spans(text,'mixed')
    assert {'zh','en'}<={row['language'] for row in result['spans']}
    assert ''.join(text[row['start']:row['end']] for row in result['spans'])==text
    assert all(row['language']=='fr' for row in detect_language_spans('Bonjour tout le monde. Je suis content de vous voir.','en')['spans'])
    assert detect_language_spans('Tom','en')['spans'][0]['language']=='unknown'


def test_dialect_policy_is_explicit_and_protects_declared_terms(tmp_path):
    import pytest
    from inkwise.cli import seed_demo
    from inkwise.contracts import LanguageRules
    from inkwise.language import language_context
    from inkwise.errors import DomainError
    from inkwise.rubrics import RubricRepository
    from inkwise.settings import Settings
    settings=Settings(rubric_dir=tmp_path);seed_demo(settings,'a')
    rubric=RubricRepository(tmp_path).get('a','chinese-demo').model_copy(update={'language_policy':'dialect'})
    with pytest.raises(DomainError,match='DIALECT_POLICY_REQUIRED'):language_context('侬好！',rubric)
    rules=LanguageRules(id='synthetic-dialect',release='v1',mode='dialect',allowedLanguages=['zh'],
        languageWeights={'zh':1},protectedTerms=['侬'],source='Original synthetic policy, not a regional classification')
    context=language_context('侬好！',rubric.model_copy(update={'language_rules':rules}))
    assert context['protectedSpans'][0]['start']==0 and context['protectedSpans'][0]['end']==1
    assert 'DIALECT_REQUIRES_TEACHER_RULESET' not in context['warnings']
    assert context['schoolPolicyApproved'] is False


def test_formal_policy_required_and_protected_overlap(tmp_path):
    import pytest
    from types import SimpleNamespace
    from inkwise.cli import seed_demo
    from inkwise.settings import Settings
    from inkwise.rubrics import RubricRepository
    from inkwise.language import language_context,evidence_protected
    from inkwise.errors import DomainError
    settings=Settings(rubric_dir=tmp_path);seed_demo(settings,'a')
    rubric=RubricRepository(tmp_path).get('a','english-demo').model_copy(update={'synthetic':False})
    with pytest.raises(DomainError,match='LANGUAGE_POLICY_REQUIRED'):language_context('A complete English sentence.',rubric)
    assert evidence_protected(SimpleNamespace(start=8,end=12),{'protectedSpans':[{'start':10,'end':15}]})
    assert not evidence_protected(SimpleNamespace(start=5,end=10),{'protectedSpans':[{'start':10,'end':15}]})
