from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


EducationType = Literal["higher", "secondary_vocational", "school"]


class ContactData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone: str | None = None
    email: str | None = None
    messengers: list[str] = Field(default_factory=list)


class EducationEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: EducationType
    institution: str = Field(min_length=1)
    faculty: str | None = None
    specialty: str | None = None
    degree: Literal["bachelor", "master", "postgraduate", "specialist"] | None = None
    start_date: str | None = None
    end_date: str | None = None

    @model_validator(mode="after")
    def validate_type_specific_fields(self) -> EducationEntry:
        if self.type == "higher" and self.degree is None:
            raise ValueError("higher education requires degree")
        if self.type in {"higher", "secondary_vocational"} and not self.specialty:
            raise ValueError(f"{self.type} education requires specialty")
        if self.type in {"higher", "secondary_vocational"} and not self.faculty:
            raise ValueError(f"{self.type} education requires faculty")
        if self.type == "school" and any((self.faculty, self.specialty, self.degree)):
            raise ValueError("school education does not accept faculty, specialty or degree")
        return self


class LanguageEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: str = Field(min_length=1)
    proficiency: str = Field(min_length=1)


class PersonalProfileData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    full_name: str | None = None
    residence: str | None = None
    job_search_locations: list[str] = Field(default_factory=list)
    contacts: ContactData = Field(default_factory=ContactData)
    education: list[EducationEntry] = Field(default_factory=list)
    languages: list[LanguageEntry] = Field(default_factory=list)
    driver_license: bool | None = None


class Experience(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company: str = Field(min_length=1)
    position: str = Field(min_length=1)
    start_date: str | None = None
    end_date: str | None = None
    duties: str = Field(default="")


class ResumeData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="Резюме", min_length=1, max_length=255)
    desired_title: str | None = None
    desired_salary: str | None = None
    employment_types: list[str] = Field(default_factory=list)
    work_formats: list[str] = Field(default_factory=list)
    business_trips: bool | Literal["can", "cannot"] | None = None
    experiences: list[Experience] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    about: str = ""
    selected_for_matching: bool = True
    original_filename: str | None = None
    original_path: str | None = None

    @field_validator("skills", mode="before")
    @classmethod
    def normalize_skills(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            value = value.split(",")
        if value is None:
            return []
        return list(dict.fromkeys(item.strip() for item in value if str(item).strip()))

    @field_validator("desired_salary", mode="before")
    @classmethod
    def normalize_salary(cls, value: Any) -> str | None:
        return None if value is None else str(value)

    @field_validator("about")
    @classmethod
    def validate_about_words(cls, value: str) -> str:
        if len(value.split()) > 500:
            raise ValueError("about must contain at most 500 words")
        return value


class CandidateProfileData(PersonalProfileData):
    """The personal profile payload; resumes are separate resources."""


class ResumeImportData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile: PersonalProfileData = Field(default_factory=PersonalProfileData)
    resume: ResumeData


class SearchFilters(BaseModel):
    viewed_limit: int = Field(default=30, ge=1, le=500)
    application_limit: int = Field(default=5, ge=0, le=50)
    duration_minutes: int = Field(default=60, ge=1, le=720)
    mode: Literal["analysis_only", "review_before_submit", "autopilot"] = "analysis_only"


class ScoringCriterion(BaseModel):
    key: str
    title: str
    description: str
    max_points: int = Field(ge=0, le=100)


class FlagMatch(BaseModel):
    """A model's evidence-backed match for one policy flag."""

    flag: str = Field(min_length=1)
    confidence: float = Field(default=0, ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)
    matched: bool = True
    # ``None`` preserves the pre-verdict API contract for stored evaluations;
    # new model responses must use this explicit semantic verdict.
    verdict: Literal["present", "absent", "uncertain"] | None = None

    @field_validator("confidence", mode="before")
    @classmethod
    def normalize_percentage_confidence(cls, value: Any) -> Any:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 1 < value <= 100:
            return value / 100
        return value


class WorkFormatAssessment(BaseModel):
    compatible: bool | None = None
    confidence: float = Field(default=0, ge=0, le=1)
    vacancy_format: str | None = None
    candidate_formats: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)

    @field_validator("confidence", mode="before")
    @classmethod
    def normalize_percentage_confidence(cls, value: Any) -> Any:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 1 < value <= 100:
            return value / 100
        return value


class PolicyFilterResult(BaseModel):
    green_flags: list[FlagMatch] = Field(default_factory=list)
    red_flags: list[FlagMatch] = Field(default_factory=list)
    work_format: WorkFormatAssessment = Field(default_factory=WorkFormatAssessment)
    reason: str = ""


class PolicyCompilation(BaseModel):
    """Dedicated schema returned by the policy compiler role."""

    green_flags: list[str] = Field(default_factory=list)
    red_flags: list[str] = Field(default_factory=list)
    flag_confidence_threshold: float = Field(default=0.70, ge=0, le=1)

    @field_validator("flag_confidence_threshold", mode="before")
    @classmethod
    def normalize_percentage_threshold(cls, value: Any) -> Any:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 1 < value <= 100:
            return value / 100
        return value


class MatchAssessment(BaseModel):
    """Normalized [0, 1] match supplied by the model; arithmetic is local."""

    match: float = Field(default=0, ge=0, le=1)
    confidence: float = Field(default=0, ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)
    explanation: str = ""

    @field_validator("match", "confidence", mode="before")
    @classmethod
    def normalize_percentage(cls, value: Any) -> Any:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 1 < value <= 100:
            return value / 100
        return value


class ResumeAnalysis(BaseModel):
    vacancy_seniority: Literal["junior", "middle", "senior"] | None = None
    title: MatchAssessment = Field(default_factory=MatchAssessment)
    tasks: MatchAssessment = Field(default_factory=MatchAssessment)
    industry: MatchAssessment = Field(default_factory=MatchAssessment)
    required_years: MatchAssessment = Field(default_factory=MatchAssessment)
    seniority: MatchAssessment = Field(default_factory=MatchAssessment)
    languages: MatchAssessment = Field(default_factory=MatchAssessment)
    skills: MatchAssessment = Field(default_factory=MatchAssessment)
    category: str = ""
    reason: str = ""


def default_scoring_criteria() -> list[ScoringCriterion]:
    return [
        ScoringCriterion(key="title", title="Название должности", description="Совпадение фактического названия должности с желаемой должностью в резюме.", max_points=5),
        ScoringCriterion(key="tasks", title="Задачи", description="Соответствие задач вакансии задачам из опыта пользователя.", max_points=30),
        ScoringCriterion(key="industry", title="Сфера", description="Соответствие сферы вакансии сфере предыдущего опыта.", max_points=25),
        ScoringCriterion(key="required_years", title="Годы опыта", description="Соответствие требуемых лет опыта подтверждённому опыту пользователя; 10 баллов при указанном уровне, иначе 20.", max_points=10),
        ScoringCriterion(key="seniority", title="Уровень позиции", description="Соответствие junior/middle/senior уровню позиции и опыту пользователя; применяется только при явно указанном уровне.", max_points=10),
        ScoringCriterion(key="languages", title="Языки", description="Соответствие требуемого уровня языка уровню языка пользователя.", max_points=10),
        ScoringCriterion(key="skills", title="Навыки", description="Совпадение требуемых навыков с навыками пользователя.", max_points=10),
    ]


class SearchPolicy(BaseModel):
    request_text: str = Field(min_length=10)
    score_threshold: int = Field(default=70, ge=0, le=100)
    green_flags: list[str] = Field(default_factory=list)
    red_flags: list[str] = Field(default_factory=list)
    flag_confidence_threshold: float = Field(default=0.70, ge=0, le=1)
    scoring_criteria: list[ScoringCriterion] = Field(default_factory=default_scoring_criteria)


class Salary(BaseModel):
    minimum: int | None = None
    maximum: int | None = None
    currency: str = "RUB"
    gross: bool | None = None


class ApplicationQuestion(BaseModel):
    label: str
    required: bool = False
    sensitive: bool = False


class JobPosting(BaseModel):
    source: str
    external_id: str | None = None
    url: str
    title: str
    company: str | None = None
    description: str
    responsibilities: list[str] = Field(default_factory=list)
    required_skills: list[str] = Field(default_factory=list)
    optional_skills: list[str] = Field(default_factory=list)
    location: str | None = None
    work_format: str | None = None
    employment_type: str | None = None
    salary: Salary | None = None
    requires_cover_letter: bool | None = None
    has_test_assignment: bool | None = None
    application_questions: list[ApplicationQuestion] = Field(default_factory=list)
    published_at: datetime | None = None
    extracted_at: datetime = Field(default_factory=utcnow)


class Evidence(BaseModel):
    vacancy_excerpt: str
    profile_path: str | None = None
    explanation: str


class ScoreComponent(BaseModel):
    key: str
    title: str
    points: int = Field(ge=0, le=100)
    max_points: int = Field(ge=0, le=100)
    explanation: str
    evidence: list[str] = Field(default_factory=list)


class JobEvaluation(BaseModel):
    decision: Literal["apply", "skip", "manual_review"]
    score: int = Field(ge=0, le=100)
    confidence: float = Field(ge=0, le=1)
    category: str
    score_breakdown: list[ScoreComponent] = Field(default_factory=list)
    hard_rule_violations: list[str] = Field(default_factory=list)
    positive_evidence: list[Evidence] = Field(default_factory=list)
    negative_evidence: list[Evidence] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)
    has_test_assignment: bool = False
    requires_manual_review: bool = False
    reason: str
    flag_filter: PolicyFilterResult | None = None

    @field_validator("confidence", mode="before")
    @classmethod
    def normalize_percentage_confidence(cls, value: Any) -> Any:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 1 < value <= 100:
            return value / 100
        return value


class ApplicationPlan(BaseModel):
    vacancy_id: int
    resume_file: str
    cover_letter: str | None = None
    known_answers: dict[str, str] = Field(default_factory=dict)
    unknown_question_policy: Literal["manual_review", "skip"] = "manual_review"
    submission_allowed: bool = False


class CoverLetterDraft(BaseModel):
    text: str = Field(min_length=40, max_length=3000)


class VacancyState(StrEnum):
    DISCOVERED = "DISCOVERED"
    EXTRACTED = "EXTRACTED"
    FILTERED_OUT = "FILTERED_OUT"
    EVALUATING = "EVALUATING"
    REJECTED_BY_MODEL = "REJECTED_BY_MODEL"
    SKIPPED_TEST = "SKIPPED_TEST"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    LETTER_GENERATED = "LETTER_GENERATED"
    READY_TO_SUBMIT = "READY_TO_SUBMIT"
    FILLING_FORM = "FILLING_FORM"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    ALREADY_APPLIED = "ALREADY_APPLIED"
    FAILED = "FAILED"
    UNKNOWN_RESULT = "UNKNOWN_RESULT"


class SessionStatus(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    WAITING_FOR_LOGIN = "WAITING_FOR_LOGIN"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    COMPLETED = "COMPLETED"
    STOPPED = "STOPPED"
    FAILED = "FAILED"
