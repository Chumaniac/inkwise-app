"""Versioned public contracts and immutable evidence records."""

from enum import StrEnum
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator, model_serializer
from pydantic.alias_generators import to_camel

from inkwise.core import DIMENSIONS


class Contract(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


_QUOTED_CORRECTION = re.compile(
    r'''["'‘’“”`]([^"'‘’“”`\n]{1,120})["'‘’“”`]\s*'''
    r'''(?:(?:should|must)\s+be|(?:be\s+)?(?:changed|corrected)\s+to|'''
    r'''(?:be\s+)?replaced\s+with|改为|改成|替换为|→|->)\s*'''
    r'''["'‘’“”`]([^"'‘’“”`\n]{1,120})["'‘’“”`]''', re.IGNORECASE)


def feedback_requires_rewrite(reason: str, suggestion: str) -> bool:
    """Flag explicit identical replacements, without judging or changing a grade.

    Compare exact strings: capitalization and spacing can be valid corrections.
    This narrow check is not a language-quality or factual-accuracy certificate.
    """
    return any(match[1] == match[2] for value in (reason, suggestion) if isinstance(value, str) and len(value) <= 1200
               for match in _QUOTED_CORRECTION.finditer(value))


class Role(StrEnum):
    TEACHER = "teacher"
    REVIEWER = "reviewer"
    ADMIN = "admin"


class Principal(Contract):
    user_id: str
    tenant_id: str
    role: Role
    classroom_ids: list[str]


class JobCreate(Contract):
    intent: Literal["assess","proofread","auto"] = "assess"
    operator_request: str = Field(default="",max_length=500)
    student_ref: str = Field(default="",max_length=64,pattern=r"^[A-Za-z0-9_-]*$")
    experiment_consent: bool = False
    routing_preference: Literal['local','hybrid'] = 'local'
    cloud_consent: bool = False
    max_cost_microusd: Annotated[StrictInt,Field(ge=0,le=10000000)] = 0
    classroom_id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=100)
    prompt: str = Field(min_length=1, max_length=3000)
    text: str = Field(default="", max_length=15000)
    grade: Annotated[StrictInt, Field(ge=1, le=12)]
    language_policy: Literal["zh", "en", "mixed", "dialect"]
    genre: Literal["narrative", "argumentative", "short-text"]
    rubric_id: str = Field(min_length=1, max_length=120)

    @field_validator("title", "prompt", "classroom_id", "rubric_id")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Must not be blank")
        return value.strip()


class Versioned(Contract):
    version: Annotated[StrictInt, Field(ge=1)]


class TextConfirmation(Versioned):
    page_order_confirmed: bool = False
    text: str = Field(min_length=1, max_length=15000)

    @field_validator("text")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Text must not be blank")
        return value


class Evidence(Contract):
    start: Annotated[StrictInt, Field(ge=0)]
    end: Annotated[StrictInt, Field(ge=1)]
    quote: str = Field(min_length=1, max_length=1000)
    rule_id: str = Field(min_length=1, max_length=120)


Dimension = Literal["content", "coherence", "expression", "vocabulary", "grammar"]


class LanguageSubScore(Contract):
    language: Literal['zh','en']
    band: Annotated[StrictInt,Field(ge=0,le=4)]
    reason: str = Field(min_length=1,max_length=300)
    suggestion: str = Field(min_length=1,max_length=300)
    evidence: list[Evidence] = Field(min_length=1,max_length=4)


class MixedDimensionScore(Contract):
    dimension: Literal['vocabulary','grammar']
    language_scores: list[LanguageSubScore] = Field(min_length=2,max_length=2)


class DimensionScore(Contract):
    dimension: Dimension
    band: Annotated[StrictInt, Field(ge=0, le=4)]
    reason: str = Field(min_length=1, max_length=1200)
    evidence: list[Evidence] = Field(min_length=1, max_length=8)
    suggestion: str = Field(min_length=1, max_length=1200)


class MultilingualDimensionScore(DimensionScore):
    weighted_language_band: float = Field(ge=0,le=4,allow_inf_nan=False)
    language_aggregation: Literal['weighted-nearest-rubric-band'] = 'weighted-nearest-rubric-band'
    language_scores: list[LanguageSubScore] = Field(min_length=2,max_length=2)


class ScoringResult(Contract):
    scores: list[DimensionScore] = Field(min_length=5, max_length=5)
    model: str = Field(min_length=1, max_length=200)
    warnings: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def unique_dimensions(self):
        if {score.dimension for score in self.scores} != set(DIMENSIONS):
            raise ValueError("Each dimension must appear exactly once")
        return self


class TeacherFeedback(Contract):
    include: bool = True
    reason: str = Field(default="",max_length=1200)
    suggestion: str = Field(default="",max_length=1200)
    evidence: list[Evidence] = Field(default_factory=list,max_length=8)

    @model_validator(mode="after")
    def meaningful_included_feedback(self):
        if self.include and not self.reason.strip():
            raise ValueError("Included feedback requires a teacher-reviewed reason")
        return self


class Review(Versioned):
    scores: dict[str, Annotated[StrictInt, Field(ge=0, le=4)]]
    comment: str = Field(min_length=1, max_length=3000)
    feedback: dict[Dimension,TeacherFeedback] = Field(default_factory=dict,max_length=5)
    feedback_confirmed: bool = False

    @model_validator(mode="after")
    def explicit_feedback_approval(self):
        if any(item.include for item in self.feedback.values()) and not self.feedback_confirmed:
            raise ValueError("Selected feedback requires explicit teacher confirmation")
        return self

    @field_validator("scores")
    @classmethod
    def all_dimensions(cls, value: dict) -> dict:
        if set(value) != set(DIMENSIONS):
            raise ValueError("Each dimension must appear exactly once")
        return value

    @field_validator("comment")
    @classmethod
    def meaningful_comment(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Review explanation is required")
        return value


class CandidateCreate(Versioned):
    anonymized_comment: str = Field(default="", max_length=3000)
    reuse_review_feedback: bool = False
    anonymized_text: str = Field(min_length=1, max_length=15000)
    reuse_authorized: Literal[True]
    provenance: str = Field(min_length=1, max_length=1000)


    @field_validator('anonymized_text','provenance')
    @classmethod
    def candidate_nonblank(cls,value):
        if not value.strip():raise ValueError('Must not be blank')
        return value


class CandidateDecision(Versioned):
    decision: Literal["approve", "reject"]
    reason: str = Field(min_length=1, max_length=1500)


class Rule(Contract):
    id: str = Field(min_length=1, max_length=120)
    dimension: Dimension
    description: str = Field(min_length=1, max_length=2000)
    levels: list[str] = Field(min_length=5, max_length=5)
    weight: float = Field(default=1,gt=0,le=100,allow_inf_nan=False)
    requires: list[str] = Field(default_factory=list,max_length=20)


class LanguageRules(Contract):
    id: str = Field(min_length=1,max_length=100)
    release: str = Field(min_length=1,max_length=100)
    mode: Literal['zh','en','mixed','dialect']
    allowed_languages: list[str] = Field(min_length=1,max_length=10)
    protected_terms: list[str] = Field(default_factory=list,max_length=200)
    allow_foreign_quotes: bool = False
    spelling_variants: list[Literal['en-US','en-GB']] = Field(default_factory=list,max_length=2)
    language_weights: dict[str,float]
    source: str = Field(min_length=1,max_length=1000)
    approved: bool = False

    @model_validator(mode='after')
    def valid_policy(self):
        import math
        if any(not 1<=len(term)<=80 for term in self.protected_terms):raise ValueError('Invalid protected term')
        if not set(self.allowed_languages)<={'zh','en','fr','es','de','ja','ko','th','vi'}:raise ValueError('Unsupported language code')
        if (not self.language_weights or not set(self.language_weights)<={'zh','en'}
            or not set(self.language_weights)<=set(self.allowed_languages)
            or any(not math.isfinite(value) or value<0 for value in self.language_weights.values())
            or abs(sum(self.language_weights.values())-1)>1e-8):raise ValueError('Invalid language weights')
        if self.mode=='mixed' and set(self.language_weights)!={'zh','en'}:raise ValueError('Both language weights required')
        return self


class Rubric(Contract):
    weight_learning_allowed: bool = False
    language_rules: LanguageRules | None = None
    id: str
    release: str
    title: str
    language_policy: Literal["zh", "en", "mixed", "dialect"]
    grade_min: Annotated[StrictInt, Field(ge=1, le=12)]
    grade_max: Annotated[StrictInt, Field(ge=1, le=12)]
    genre: Literal["narrative", "argumentative", "short-text"]
    weights: dict[str, float]
    rules: list[Rule] = Field(min_length=5,max_length=100)
    synthetic: bool = True

    @model_serializer(mode='wrap')
    def compatible_serialization(self,handler):
        data=handler(self)
        if not self.weight_learning_allowed:
            data.pop('weight_learning_allowed',None);data.pop('weightLearningAllowed',None)
        if self.language_rules is None:
            data.pop('language_rules',None);data.pop('languageRules',None)
        return data

    @model_validator(mode="after")
    def consistent(self):
        import math

        if self.language_rules and (self.language_rules.mode!=self.language_policy or (not self.synthetic and not self.language_rules.approved)):
            raise ValueError('Language policy must match the rubric and formal approval status')
        if self.grade_min > self.grade_max:
            raise ValueError("Invalid grade range")
        if set(self.weights) != set(DIMENSIONS) or any(not math.isfinite(w) or w < 0 for w in self.weights.values()):
            raise ValueError("Invalid dimension weights")
        if abs(sum(self.weights.values()) - 100) > .00001:
            raise ValueError("Weights must sum to 100")
        if {rule.dimension for rule in self.rules} != set(DIMENSIONS):
            raise ValueError("Rules must cover every dimension")
        if len({rule.id for rule in self.rules}) != len(self.rules):
            raise ValueError("Duplicate rule ID")
        dependencies={rule.id:rule.requires for rule in self.rules}
        visited,active=set(),set()
        def visit(identifier):
            if identifier not in dependencies or identifier in active:
                raise ValueError("Missing or cyclic rule dependency")
            if identifier in visited:return
            active.add(identifier)
            for required in dependencies[identifier]:visit(required)
            active.remove(identifier);visited.add(identifier)
        for identifier in dependencies:visit(identifier)
        return self


class PageOrder(Versioned):
    image_ids: list[str] = Field(min_length=1,max_length=10)


class IntentRequest(Contract):
    mode: Literal["auto","assess","proofread","explain"] = "auto"
    request: str = Field(default="",max_length=500)


class FeedbackFinding(Contract):
    dimension: Dimension
    evidence: Evidence
    suggestion: str = Field(min_length=1,max_length=700)


class FeedbackApproval(Versioned):
    findings_confirmed: bool = False
    findings: list[FeedbackFinding] = Field(default_factory=list,max_length=12)
    comment: str = Field(min_length=1,max_length=3000)


    @model_validator(mode='after')
    def selected_feedback_confirmed(self):
        if self.findings and not self.findings_confirmed:raise ValueError('Explicit feedback confirmation required')
        return self


class LanguagePreview(Contract):
    text: str = Field(min_length=1,max_length=15000)
    policy: Literal['zh','en','mixed','dialect'] = 'mixed'


class ProposalAction(Versioned):
    action: Literal['submit','approve','publish']
    note: str = Field(min_length=1,max_length=1000)
    experiment_id: str | None = Field(default=None,max_length=60)


class ExperimentControl(Versioned):
    enabled: bool
