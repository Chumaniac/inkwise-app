"""Reproducible metrics and constrained weight proposals; never auto-publish."""

import hashlib
import math
import random
from collections import defaultdict


def qwk(expected: list[int], predicted: list[int], low=0, high=4) -> float | None:
    if len(expected) != len(predicted) or not expected or high <= low:
        raise ValueError("Paired ratings with a fixed non-empty scale are required")
    if any(type(value) is not int or not low <= value <= high for value in expected + predicted):
        raise ValueError("Scores must be integer labels within the fixed scale")
    bins = high - low + 1
    row, col = [0] * bins, [0] * bins
    observed = 0.0
    for first, second in zip(expected, predicted):
        row[first-low] += 1
        col[second-low] += 1
        observed += ((first-second)/(high-low))**2
    chance = sum(row[i] * col[j] / len(expected) * ((i-j)/(high-low))**2
                 for i in range(bins) for j in range(bins))
    # No variation is not evidence of high agreement.
    return None if chance == 0 else 1-observed/chance


def metrics(rows: list[dict]) -> dict:
    if not rows:
        raise ValueError("Evaluation data is empty")
    required = {"essayId", "studentId", "split", "teacherBand", "modelBand", "language", "grade"}
    if any(not required.issubset(row) for row in rows):
        raise ValueError("Evaluation provenance is incomplete")
    seen, students = set(), defaultdict(set)
    for row in rows:
        key = (row["essayId"], row.get("dimension", "overall"))
        if key in seen:
            raise ValueError("Duplicate evaluation row")
        seen.add(key)
        students[row["studentId"]].add(row["split"])
    if any(len(splits) > 1 for splits in students.values()):
        raise ValueError("Student leakage across data splits")
    test = [row for row in rows if row["split"] == "test"]
    if not test:
        raise ValueError("A held-out test partition is required")
    def summarize(part):
        expected, predicted = [r["teacherBand"] for r in part], [r["modelBand"] for r in part]
        return {"count":len(part), "qwk":qwk(expected,predicted),
                "mae":sum(abs(a-b) for a,b in zip(expected,predicted))/len(part),
                "adjacentAgreement":sum(abs(a-b)<=1 for a,b in zip(expected,predicted))/len(part)}
    groups = defaultdict(list)
    for row in test:
        groups[f"{row['language']}:g{row['grade']}"] .append(row)
    return {"overall":summarize(test), "groups":{key:summarize(part) for key,part in sorted(groups.items())},
            "scope":"held-out supplied labels; teacher validity and data authorization require separate evidence"}


def cluster_bootstrap_mae(rows: list[dict], iterations=1000, seed=42) -> tuple[float,float]:
    """Resample students rather than correlated rows from the same student."""
    if not 100 <= iterations <= 10000:
        raise ValueError("Bounded bootstrap iterations required")
    groups = defaultdict(list)
    for row in rows:
        groups[row["studentId"]].append(abs(row["teacherBand"]-row["modelBand"]))
    if len(groups)<2:
        raise ValueError("At least two independent students are needed")
    keys = list(groups)
    rng, samples = random.Random(seed), []
    for _ in range(iterations):
        errors = [error for key in rng.choices(keys,k=len(keys)) for error in groups[key]]
        samples.append(sum(errors)/len(errors))
    samples.sort()
    return samples[int(iterations*.025)], samples[min(iterations-1,int(iterations*.975))]


def cohort(tenant: str, classroom: str, experiment: str, secret_salt: str) -> str:
    """Stable classroom-level randomization; not student-level experimentation."""
    import hmac
    digest = hmac.new(secret_salt.encode(), f"{tenant}:{classroom}:{experiment}".encode(), hashlib.sha256).digest()
    return "B" if int.from_bytes(digest[:8], "big") % 100 < 50 else "A"


def propose_weights(features: list[list[float]], labels: list[float], baseline: list[float],
                     max_change=.02, regularization=1.0, iterations=500) -> list[float]:
    """Huber regression with box-constrained simplex projection, for review only.

    Features/labels are normalized to [0,1]. Teacher-fixed weights must not be
    passed here; this tool only proposes changes where policy permits them.
    """
    import numpy as np
    x,y,w0 = np.asarray(features,dtype=float),np.asarray(labels,dtype=float),np.asarray(baseline,dtype=float)
    if x.ndim!=2 or len(x)<2 or x.shape!=(len(y),len(w0)) or not np.all(np.isfinite(x)) or not np.all(np.isfinite(y)):
        raise ValueError("Invalid calibration data")
    if np.any(x<0) or np.any(x>1) or np.any(y<0) or np.any(y>1) or not np.all(np.isfinite(w0)):
        raise ValueError("Use normalized finite labels and features")
    if abs(float(w0.sum())-1)>1e-8 or np.any(w0<0) or not 0<max_change<=.02 or not 1<=iterations<=5000:
        raise ValueError("Invalid baseline or unsafe update bound")
    # Fixed 0.1 step supports this bounded regularization range.
    if not math.isfinite(regularization) or not 0 <= regularization <= 5:
        raise ValueError("Regularization must be finite and within [0,5]")
    lower,upper=np.maximum(0,w0-max_change),np.minimum(1,w0+max_change)
    w=w0.copy()
    for _ in range(iterations):
        gradient=x.T @ np.clip(x@w-y,-.1,.1)/len(x)+2*regularization*(w-w0)
        proposal=w-.1*gradient
        lo,hi=float(np.min(proposal-upper)),float(np.max(proposal-lower))
        for _ in range(60):
            mid=(lo+hi)/2
            if np.clip(proposal-mid,lower,upper).sum()>1: lo=mid
            else: hi=mid
        w=np.clip(proposal-(lo+hi)/2,lower,upper)
    return w.tolist()
