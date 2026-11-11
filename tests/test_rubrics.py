import pytest
from pydantic import ValidationError

from inkwise.cli import seed_demo
from inkwise.contracts import Rubric
from inkwise.rubrics import RubricRepository
from inkwise.settings import Settings


def test_weight_graph_rejects_cycles_and_missing_dependencies(tmp_path):
    settings=Settings(rubric_dir=tmp_path);seed_demo(settings,"test")
    rubric=RubricRepository(tmp_path).get("test","english-demo")
    raw=rubric.model_dump(by_alias=True)
    raw["rules"][0]["requires"]=[raw["rules"][0]["id"]]
    with pytest.raises(ValidationError):Rubric.model_validate(raw)
    raw["rules"][0]["requires"]=["unknown-rule"]
    with pytest.raises(ValidationError):Rubric.model_validate(raw)
