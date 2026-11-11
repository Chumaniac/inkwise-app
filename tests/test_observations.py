from inkwise.grading import observable_checks


def test_single_paragraph_does_not_silently_support_multi_paragraph_reasoning():
    result=observable_checks('A single paragraph.',[{'dimension':'coherence','reason':'Each paragraph builds on the previous one.'}])
    assert result['paragraphsByBlankLines']==1
    assert result['paragraphReasoningToReview']==['coherence']
    assert observable_checks('One paragraph.\n\nAnother paragraph.',[{'dimension':'coherence','reason':'Each paragraph has a role.'}])['paragraphReasoningToReview']==[]
