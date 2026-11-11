"""Conservative script spans; teacher policy, not guessed dialect, selects rules."""

import unicodedata


def language_spans(text: str, policy: str) -> dict:
    spans=[]
    for index,char in enumerate(text):
        name=unicodedata.name(char,"")
        if "CJK" in name or "IDEOGRAPH" in name:
            language="zh"
        elif "LATIN" in name:
            language="latin"
        else:
            language=spans[-1]["language"] if spans else "unknown"
        if spans and spans[-1]["language"]==language:
            spans[-1]["end"]=index+1
        else:
            spans.append({"start":index,"end":index+1,"language":language})
    warnings=[]
    if policy=="dialect":warnings.append("DIALECT_REQUIRES_TEACHER_RULESET")
    return {"policy":policy,"spans":spans,"method":"unicode-script-hints", "confidence":None,
            "warnings":warnings,"note":"Latin script is not proof of English. Teacher task policy is authoritative."}


from functools import lru_cache
import re


@lru_cache(maxsize=1)
def _detector():
    try:
        from lingua import Language,LanguageDetectorBuilder
        languages={Language.CHINESE:'zh',Language.ENGLISH:'en',Language.FRENCH:'fr',Language.SPANISH:'es',
            Language.GERMAN:'de',Language.JAPANESE:'ja',Language.KOREAN:'ko',Language.THAI:'th',Language.VIETNAMESE:'vi'}
        return LanguageDetectorBuilder.from_languages(*languages).with_low_accuracy_mode().build(),languages
    except ImportError:return None,{}


def detect_language_spans(text,policy):
    """Offsets are Python code points; classifier scores are relative, not calibrated."""
    base=language_spans(text,policy);detector,codes=_detector();spans=[];warnings=list(base['warnings'])
    if detector is None:warnings.append('LANGUAGE_DETECTOR_UNAVAILABLE')
    for raw in base['spans']:
        source=text[raw['start']:raw['end']]
        for part in re.finditer(r'[^.!?。！？\n]+[.!?。！？\n]*|[.!?。！？\n]+',source):
            for offset in range(part.start(),part.end(),512):
                start=raw['start']+offset;end=min(raw['start']+part.end(),start+512)
                piece=text[start:end];language='unknown' if raw['language']=='latin' else raw['language'];confidence=None
                if detector and sum(character.isalpha() for character in piece)>=3 and (raw['language']!='latin' or len(piece.strip())>=10):
                    values=detector.compute_language_confidence_values(piece)
                    if values and values[0].value>=.65 and (len(values)==1 or values[0].value-values[1].value>=.15):
                        language=codes[values[0].language];confidence=float(values[0].value)
                if len(spans)>=199:
                    spans.append({'start':start,'end':len(text),'language':'unknown','scriptHint':'unknown','confidence':None})
                    return {**base,'spans':spans,'warnings':sorted(set(warnings+['LANGUAGE_SPAN_LIMIT'])),
                        'method':'lingua-local' if detector else 'unicode-script-hints'}
                spans.append({'start':start,'end':end,'language':language,'scriptHint':raw['language'],'confidence':confidence})
    if any(span['language']=='unknown' and any(c.isalpha() for c in text[span['start']:span['end']]) for span in spans):
        warnings.append('LANGUAGE_DETECTION_UNCERTAIN')
    return {**base,'spans':spans,'method':'lingua-local' if detector else 'unicode-script-hints',
            'warnings':sorted(set(warnings)),'confidenceKind':'relative-language-score-not-a-student-or-grading-confidence'}


def language_context(text,rubric):
    from inkwise.contracts import LanguageRules
    from inkwise.errors import DomainError
    from inkwise.rubrics import canonical,digest
    policy=rubric.language_rules
    if policy is None:
        if rubric.language_policy=='dialect':raise DomainError('DIALECT_POLICY_REQUIRED',422)
        if not rubric.synthetic:raise DomainError('LANGUAGE_POLICY_REQUIRED',422)
        allowed=['zh','en'] if rubric.language_policy=='mixed' else [rubric.language_policy]
        policy=LanguageRules(id='reference-'+rubric.language_policy,release='v1',mode=rubric.language_policy,
            allowedLanguages=allowed,languageWeights={key:1/len(allowed) for key in allowed},
            source='Framework default; school rules remain authoritative',approved=False,
            allowForeignQuotes=rubric.synthetic,spellingVariants=['en-US','en-GB'] if rubric.synthetic else [])
    if not rubric.synthetic and not policy.approved:raise DomainError('APPROVED_LANGUAGE_POLICY_REQUIRED',422)
    result=detect_language_spans(text,rubric.language_policy)
    if policy.mode=='dialect':result['warnings']=[value for value in result['warnings'] if value!='DIALECT_REQUIRES_TEACHER_RULESET']
    protected=[];quoted=[]
    for term in policy.protected_terms:
        expression=re.escape(term)
        if term.isascii() and term.isalpha():expression=r'(?<!\w)'+expression+r'(?!\w)'
        for match in re.finditer(expression,text,re.I):protected.append({'start':match.start(),'end':match.end(),'kind':'teacher-protected-term'})
    if policy.allow_foreign_quotes:
        for match in re.finditer(r'“[^”]{1,1000}”|「[^」]{1,1000}」|『[^』]{1,1000}』|"[^"\n]{1,1000}"',text):
            quoted.append((match.start(),match.end()))
    if set(policy.spelling_variants)=={'en-US','en-GB'}:
        variants='colour color favourite favorite centre center organise organize travelling traveling'.split()
        for match in re.finditer(r'\b(?:'+'|'.join(variants)+r')\b',text,re.I):
            protected.append({'start':match.start(),'end':match.end(),'kind':'accepted-spelling-variant'})
    units={};eligible=set()
    for span in result['spans']:
        language=span['language'];piece=text[span['start']:span['end']]
        if language in ('zh','en'):eligible.add(language)
        if language=='unknown' and span.get('scriptHint')=='latin' and 'en' in policy.allowed_languages:eligible.add('en')
        if language not in policy.allowed_languages and language!='unknown':
            exempt=any(start<=span['start'] and span['end']<=end for start,end in quoted)
            protected.append({'start':span['start'],'end':span['end'],'kind':'allowed-foreign-quotation' if exempt else 'outside-rule-language'})
            if not exempt:result['warnings'].append('OUTSIDE_LANGUAGE_REQUIRES_TEACHER')
        count=len(re.findall(r'[\u3400-\u9fff]',piece)) if language=='zh' else len(re.findall(r'\b\w+\b',piece))
        units[language]=units.get(language,0)+count
    if len(protected)>200:raise DomainError('LANGUAGE_POLICY_MATCH_LIMIT',422)
    result.update({'policyId':policy.id,'policyHash':digest(canonical(policy.model_dump(by_alias=True))),
        'protectedSpans':protected,'eligibleLanguages':sorted(eligible&set(policy.language_weights)),
        'languageWeights':policy.language_weights,'units':units,'schoolPolicyApproved':policy.approved,
        'normalization':'Chinese characters and other-language words reported separately; score bands use declared policy weights'})
    result['warnings']=sorted(set(result['warnings']))
    return result


def evidence_protected(evidence,context):
    return any(evidence.start<span['end'] and evidence.end>span['start'] for span in context.get('protectedSpans',[]))


def attach_language_policy(settings,tenant,rubric_id,policy_path,release,submitted_by,approved_by,source):
    import json,tempfile
    from pathlib import Path
    from inkwise.contracts import LanguageRules
    from inkwise.knowledge import import_rubric,read_bundle_text
    from inkwise.rubrics import RubricRepository,canonical,safe_segment
    from inkwise.errors import DomainError
    policy=LanguageRules.model_validate_json(read_bundle_text(policy_path))
    rubric=RubricRepository(settings.rubric_dir).get(tenant,rubric_id)
    if not policy.approved or rubric.synthetic:raise DomainError('APPROVED_LANGUAGE_POLICY_REQUIRED')
    if policy.mode!=rubric.language_policy:raise DomainError('LANGUAGE_POLICY_SCOPE_MISMATCH')
    revised=rubric.model_copy(update={'language_rules':policy,'release':safe_segment(release)})
    with tempfile.TemporaryDirectory(prefix='inkwise-language-') as folder:
        path=Path(folder)/'rubric.json';path.write_text(canonical(revised.model_dump(by_alias=True)))
        return import_rubric(settings,tenant,path,submitted_by=submitted_by,approved_by=approved_by,source=source)


def model_language_context(context):
    """Coalesce adjacent language hints; confidence diagnostics stay in the teacher report."""
    result={key:context[key] for key in ('policyId','policyHash','protectedSpans','eligibleLanguages','languageWeights','warnings') if key in context}
    spans=[]
    for row in context.get('spans',[]):
        item={key:row[key] for key in ('start','end','language','scriptHint') if key in row}
        if spans and spans[-1]['end']==item['start'] and spans[-1]['language']==item['language'] and spans[-1].get('scriptHint')==item.get('scriptHint'):
            spans[-1]['end']=item['end']
        else:spans.append(item)
    result['spans']=spans
    return result
