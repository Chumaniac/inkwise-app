"""Bounded, content-free evaluation summaries for OCR, retrieval and feedback."""
import math
import statistics

from inkwise.errors import DomainError


def edit_distance(left,right):
    if len(left)*len(right)>4_000_000:raise DomainError('EVALUATION_UNIT_TOO_LARGE')
    if len(left)<len(right):left,right=right,left
    previous=list(range(len(right)+1))
    for i,a in enumerate(left,1):
        current=[i]
        for j,b in enumerate(right,1):current.append(min(current[-1]+1,previous[j]+1,previous[j-1]+(a!=b)))
        previous=current
    return previous[-1]


def ocr_metrics(rows):
    if not rows or len(rows)>10000:raise DomainError('EVALUATION_DATA_INVALID')
    characters=words=char_errors=word_errors=missing=0
    for row in rows:
        expected,predicted=row['reference'],row['prediction']
        if not isinstance(expected,str) or not isinstance(predicted,str) or not expected or max(len(expected),len(predicted))>2000:
            raise DomainError('EVALUATION_DATA_INVALID')
        characters+=len(expected);words+=len(expected.split());missing+=int(not predicted.strip())
        char_errors+=edit_distance(expected,predicted);word_errors+=edit_distance(expected.split(),predicted.split())
    return {'units':len(rows),'referenceCharacters':characters,'cer':char_errors/characters,
            'wer':word_errors/words if words else None,'emptyPredictionRate':missing/len(rows),
            'scope':'transcribed units up to 2000 characters; whitespace/case preserved, no text included in output'}


def retrieval_metrics(rows,k=10):
    if not rows or len(rows)>10000 or not 1<=k<=100:raise DomainError('EVALUATION_DATA_INVALID')
    recalls=[];ndcgs=[];rr=[];coverage=[]
    for row in rows:
        relevant=row['relevance'];ranked=row['retrieved'][:k]
        if not relevant or len(relevant)>1000 or len(ranked)!=len(set(ranked)) or any(type(v) is not int or not 0<=v<=3 for v in relevant.values()):
            raise DomainError('EVALUATION_DATA_INVALID')
        positive={key for key,value in relevant.items() if value>0}
        if not positive:raise DomainError('EVALUATION_DATA_INVALID')
        recalls.append(len(positive&set(ranked))/len(positive))
        dcg=sum((2**relevant.get(key,0)-1)/math.log2(i+2) for i,key in enumerate(ranked))
        ideal=sum((2**value-1)/math.log2(i+2) for i,value in enumerate(sorted(relevant.values(),reverse=True)[:k]))
        ndcgs.append(dcg/ideal)
        rr.append(next((1/(i+1) for i,key in enumerate(ranked) if key in positive),0))
        mandatory=set(row.get('mandatory',[]))
        if mandatory:coverage.append(len(mandatory&set(row.get('appliedRules',[])))/len(mandatory))
    return {'queries':len(rows),'k':k,'recallAtK':statistics.mean(recalls),'ndcgAtK':statistics.mean(ndcgs),
            'mrr':statistics.mean(rr),'mandatoryRuleCoverage':statistics.mean(coverage) if coverage else None}


def feedback_metrics(rows):
    if not rows or len(rows)>10000:raise DomainError('EVALUATION_DATA_INVALID')
    if any(type(row.get('usefulness')) is not int or not 1<=row['usefulness']<=5 or type(row.get('correct')) is not bool for row in rows):
        raise DomainError('EVALUATION_DATA_INVALID')
    return {'ratings':len(rows),'meanUsefulness':statistics.mean(row['usefulness'] for row in rows),
            'incorrectAdviceRate':statistics.mean(not row['correct'] for row in rows),'scope':'supplied reviewer judgments, not click acceptance'}


def latency_metrics(rows):
    if not rows or len(rows)>10000:raise DomainError('EVALUATION_DATA_INVALID')
    values=[row['durationMs'] for row in rows]
    if any(type(value) not in (int,float) or not math.isfinite(value) or value<0 for value in values):raise DomainError('EVALUATION_DATA_INVALID')
    values.sort()
    percentile=lambda p:values[min(len(values)-1,max(0,math.ceil(len(values)*p)-1))]
    return {'count':len(values),'p50Ms':percentile(.5),'p95Ms':percentile(.95),'p99Ms':percentile(.99),
            'method':'nearest-rank; report stage, machine and real/synthetic model separately'}


def evaluate_suite(document):
    allowed={'ocr':ocr_metrics,'retrieval':retrieval_metrics,'feedback':feedback_metrics,'latency':latency_metrics}
    if not document.get('provenance') or not any(name in document for name in allowed):raise DomainError('EVALUATION_PROVENANCE_REQUIRED')
    result={'provenance':document['provenance']}
    for name,function in allowed.items():
        if name in document:result[name]=function(document[name])
    return result
