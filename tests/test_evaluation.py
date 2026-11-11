import pytest

from inkwise.evaluation import cluster_bootstrap_mae, cohort, metrics, propose_weights, qwk


def test_qwk_uses_fixed_scale_and_does_not_claim_constant_scores_are_valid():
    assert qwk([0,1,2,3,4],[0,1,2,3,4]) == 1
    assert qwk([3,3],[3,3]) is None
    assert qwk([0,4],[4,0]) < 0


def test_eval_rejects_student_leakage():
    rows=[{"essayId":str(i),"studentId":"same-student","split":split,"teacherBand":3,"modelBand":2,
           "language":"en","grade":7} for i,split in enumerate(["train","test"])]
    with pytest.raises(ValueError): metrics(rows)


def test_classroom_assignment_is_stable_and_weight_changes_are_bounded():
    assert cohort("a","class1","v1","synthetic-salt") == cohort("a","class1","v1","synthetic-salt")
    baseline=[.3,.25,.2,.15,.1]
    result=propose_weights([[1,0,0,0,0],[0,1,1,1,1]],[0,1],baseline)
    assert abs(sum(result)-1)<1e-8
    assert all(abs(a-b)<=.0200001 for a,b in zip(result,baseline))


def test_metrics_and_cluster_interval_are_reproducible():
    rows=[{"essayId":str(i),"studentId":str(i),"split":"test","teacherBand":i,"modelBand":i,
           "language":"en","grade":7} for i in range(5)]
    assert metrics(rows)["overall"]["qwk"] == 1
    assert cluster_bootstrap_mae(rows,iterations=100)==(0,0)


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -1, 100000000])
def test_weight_proposal_rejects_unsafe_regularization(value):
    from inkwise.evaluation import propose_weights
    with pytest.raises(ValueError):
        propose_weights([[0,1],[1,0]], [0,1], [.5,.5], regularization=value)
