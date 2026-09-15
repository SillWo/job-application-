from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Generic, Literal, TypeVar

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Resume data imported from a job site is deliberately modelled separately.
# The availability marker is important: ``None``
# cannot tell a missing field from a field hidden by an anonymous/public view.
class FieldAvailability(StrEnum):
    PRESENT = "present"
    NOT_PROVIDED = "not_provided"
    HIDDEN = "hidden"
    UNSUPPORTED = "unsupported"
    PARSE_ERROR = "parse_error"


SourceValue = TypeVar("SourceValue")


class SourceField(BaseModel, Generic[SourceValue]):
    model_config = ConfigDict(extra="forbid")
    value: SourceValue | None = None
    availability: FieldAvailability = FieldAvailability.NOT_PROVIDED
    source_section: str | None = None
    # Locator is useful only inside the adapter while reading the page; it is
    # excluded from every serialized model payload sent to persistence/models.
    source_locator: str | None = Field(default=None, exclude=True)


class ResumeIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    full_name: SourceField[str] = Field(default_factory=SourceField)
    gender: SourceField[str] = Field(default_factory=SourceField)
    age: SourceField[int | str] = Field(default_factory=SourceField)
    birth_date: SourceField[str] = Field(default_factory=SourceField)
    has_photo: SourceField[bool] = Field(
        default_factory=SourceField, validation_alias=AliasChoices("has_photo", "photo_available")
    )


class ResumeContacts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone: SourceField[str] = Field(default_factory=SourceField)
    email: SourceField[str] = Field(default_factory=SourceField)
    messengers: SourceField[list[str]] = Field(default_factory=SourceField)
    links: SourceField[list[str]] = Field(
        default_factory=SourceField,
        validation_alias=AliasChoices("links", "professional_links"),
    )


class ResumeTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    desired_title: SourceField[str] = Field(
        default_factory=SourceField, validation_alias=AliasChoices("desired_title", "title")
    )
    specializations: SourceField[list[str]] = Field(default_factory=SourceField)
    grade: SourceField[str] = Field(default_factory=SourceField)
    desired_salary: SourceField[str] = Field(
        default_factory=SourceField, validation_alias=AliasChoices("desired_salary", "salary")
    )
    employment_types: SourceField[list[str]] = Field(default_factory=SourceField)
    work_formats: SourceField[list[str]] = Field(default_factory=SourceField)


class ResumeLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    residence: SourceField[str] = Field(
        default_factory=SourceField, validation_alias=AliasChoices("residence", "city")
    )
    relocation: SourceField[str] = Field(default_factory=SourceField)
    business_trips: SourceField[str | bool] = Field(default_factory=SourceField)
    citizenship: SourceField[list[str]] = Field(default_factory=SourceField)
    work_permit: SourceField[str] = Field(default_factory=SourceField)


class ResumeExperience(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company: SourceField[str] | str = ""
    position: SourceField[str] | str = Field(
        default="", validation_alias=AliasChoices("position", "title")
    )
    start_date: SourceField[str] | str | None = Field(
        default=None, validation_alias=AliasChoices("start_date", "period")
    )
    end_date: SourceField[str] | str | None = None
    duties: SourceField[str] | str = Field(
        default="", validation_alias=AliasChoices("duties", "description")
    )
    achievements: SourceField[list[str]] | list[str] = Field(default_factory=list)
    source_section: str | None = None


class ResumeProject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: SourceField[str] | str = ""
    description: SourceField[str] | str = ""
    role: SourceField[str] | str | None = None
    links: SourceField[list[str]] | list[str] = Field(
        default_factory=list, validation_alias=AliasChoices("links", "url")
    )


class ResumeSkill(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: SourceField[str] | str
    level: SourceField[str] | str | None = None
    category: SourceField[str] | str | None = None


class ResumeEducation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    institution: SourceField[str] | str = ""
    specialty: SourceField[str] | str | None = Field(
        default=None, validation_alias=AliasChoices("specialty", "program")
    )
    degree: SourceField[str] | str | None = None
    start_date: SourceField[str] | str | None = Field(
        default=None, validation_alias=AliasChoices("start_date", "period")
    )
    end_date: SourceField[str] | str | None = None


class ResumeLanguage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: SourceField[str] | str
    proficiency: SourceField[str] | str | None = Field(
        default=None, validation_alias=AliasChoices("proficiency", "level")
    )


class ResumeCourse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: SourceField[str] | str
    institution: SourceField[str] | str | None = None
    year: SourceField[str] | str | None = Field(
        default=None, validation_alias=AliasChoices("year", "period")
    )


class ResumeCertification(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: SourceField[str] | str
    issuer: SourceField[str] | str | None = None
    year: SourceField[str] | str | None = Field(
        default=None, validation_alias=AliasChoices("year", "period")
    )


class ResumeAward(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: SourceField[str] | str
    issuer: SourceField[str] | str | None = None
    year: SourceField[str] | str | None = Field(
        default=None, validation_alias=AliasChoices("year", "period")
    )


class ResumePortfolioItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: SourceField[str] | str = Field(
        default="", validation_alias=AliasChoices("title", "name")
    )
    url: SourceField[str] | str | None = None
    description: SourceField[str] | str | None = None


class AdditionalResumeSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    content: SourceField[str] | str = ""
    availability: FieldAvailability = FieldAvailability.PRESENT


class ResumeCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    present_sections: list[str] = Field(default_factory=list)
    missing_sections: list[str] = Field(default_factory=list)
    hidden_fields: list[str] = Field(default_factory=list)
    unsupported_fields: list[str] = Field(default_factory=list)
    parse_errors: list[str] = Field(default_factory=list)


class SiteResumeSnapshot(BaseModel):
    """Immutable, normalized data captured for exactly one job session."""
    model_config = ConfigDict(extra="forbid")
    schema_version: int = Field(default=1, ge=1)
    extractor_version: str = Field(min_length=1, max_length=80)
    source_site: str = Field(min_length=1, max_length=50)
    source_resume_id: str = Field(min_length=1, max_length=255)
    source_url_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_updated_at: datetime | None = None
    imported_at: datetime = Field(default_factory=utcnow)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    identity: ResumeIdentity = Field(default_factory=ResumeIdentity)
    contacts: ResumeContacts = Field(default_factory=ResumeContacts)
    target: ResumeTarget = Field(default_factory=ResumeTarget)
    location: ResumeLocation = Field(default_factory=ResumeLocation)
    experience: list[ResumeExperience] = Field(default_factory=list)
    projects: list[ResumeProject] = Field(default_factory=list)
    skills: list[ResumeSkill] = Field(default_factory=list)
    education: list[ResumeEducation] = Field(default_factory=list)
    languages: list[ResumeLanguage] = Field(default_factory=list)
    courses: list[ResumeCourse] = Field(default_factory=list)
    certifications: list[ResumeCertification] = Field(default_factory=list)
    awards: list[ResumeAward] = Field(default_factory=list)
    portfolio: list[ResumePortfolioItem] = Field(default_factory=list)
    about: SourceField[str] = Field(default_factory=SourceField)
    additional_sections: list[AdditionalResumeSection] = Field(default_factory=list)
    coverage: ResumeCoverage = Field(default_factory=ResumeCoverage)


class ResumeRef(BaseModel):
    """A validated, user-selected public resume reference (not a snapshot)."""
    model_config = ConfigDict(extra="forbid")
    source_site: str
    external_id: str
    url: str
    title: str | None = None
    language: str | None = None


class ResumeProfessionalView(BaseModel):
    """The allowlisted candidate context sent to search/evaluation models."""
    model_config = ConfigDict(extra="forbid")
    target: ResumeTarget = Field(default_factory=ResumeTarget)
    location: ResumeLocation = Field(default_factory=ResumeLocation)
    experience: list[ResumeExperience] = Field(default_factory=list)
    projects: list[ResumeProject] = Field(default_factory=list)
    skills: list[ResumeSkill] = Field(default_factory=list)
    education: list[ResumeEducation] = Field(default_factory=list)
    languages: list[ResumeLanguage] = Field(default_factory=list)
    courses: list[ResumeCourse] = Field(default_factory=list)
    certifications: list[ResumeCertification] = Field(default_factory=list)
    awards: list[ResumeAward] = Field(default_factory=list)
    portfolio: list[ResumePortfolioItem] = Field(default_factory=list)
    about: SourceField[str] = Field(default_factory=SourceField)
    additional_sections: list[AdditionalResumeSection] = Field(default_factory=list)


class ResumePrivateView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    identity: ResumeIdentity = Field(default_factory=ResumeIdentity)
    contacts: ResumeContacts = Field(default_factory=ResumeContacts)


class ScoringCriterion(BaseModel):
    key: str
    title: str
    description: str
    max_points: int = Field(ge=0, le=100)


class MatchAssessment(BaseModel):
    """Discrete rubric score supplied by the model; weighted arithmetic is local."""

    score: int = Field(default=0, ge=0)
    confidence: float = Field(default=0, ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)
    explanation: str = ""

    @field_validator("confidence", mode="before")
    @classmethod
    def normalize_percentage(cls, value: Any) -> Any:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 1 < value <= 100:
            return value / 100
        return value


class SkillAssessment(BaseModel):
    """Assessment of one vacancy skill; the model never computes totals."""
    skill: str = Field(min_length=1)
    importance: Literal["required", "preferred"]
    score: int = Field(ge=0, le=2)
    evidence: list[str]
    explanation: str
    model_config = ConfigDict(extra="forbid")

class PreferenceFlag(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default="", max_length=80)
    text: str = Field(min_length=1, max_length=500)
    category: Literal["desired_industry", "desired_task", "desired_salary", "other"]

class SalaryPreference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    minimum_monthly_amount: int = Field(gt=0)
    currency: str = "RUB"

class DesiredJobPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    green_flags: list[PreferenceFlag] = Field(default_factory=list)
    red_flags: list[PreferenceFlag] = Field(default_factory=list)
    desired_salary: SalaryPreference | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_compiler_shape(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        aliases = {"desired_industry": "desired_industry", "desired_industries": "desired_industry", "desired_task": "desired_task", "desired_tasks": "desired_task", "tasks": "desired_task", "desired_salary": "desired_salary", "salary": "desired_salary", "other": "other"}
        def flags(raw: Any) -> list[dict[str, Any]]:
            groups = raw.items() if isinstance(raw, dict) else [(None, raw)]
            result = []
            for category, items in groups:
                category = aliases.get(str(category), "other") if category is not None else None
                if not isinstance(items, list): items = [items]
                for item in items:
                    if hasattr(item, "model_dump"):
                        item = item.model_dump()
                    item_category = aliases.get(str(item.get("category")), "other") if isinstance(item, dict) and category is None else category or "other"
                    if isinstance(item, dict) and category is None and item.get("category") in aliases:
                        item_category = aliases[item["category"]]
                    if isinstance(item, str): result.append({"id": "", "text": item, "category": item_category})
                    elif isinstance(item, dict) and isinstance(item.get("text"), str): result.append({"id": str(item.get("id", "")), "text": item["text"], "category": item_category})
            return result[:100]
        output = {"green_flags": flags(value.get("green_flags", [])), "red_flags": flags(value.get("red_flags", [])), "desired_salary": value.get("desired_salary")}
        salary = output["desired_salary"]
        if isinstance(salary, dict):
            amount = salary.get("minimum_monthly_amount", salary.get("minimum", salary.get("amount")))
            output["desired_salary"] = {"minimum_monthly_amount": amount, "currency": salary.get("currency", "RUB")} if isinstance(amount, int) and not isinstance(amount, bool) and amount > 0 else None
        return output

class FlagMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    flag_id: str
    matched: bool = False
    confidence: float = Field(default=0, ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)
    explanation: str = ""


class ResumeAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tasks: MatchAssessment
    skills: list[SkillAssessment]
    skills_summary: str = ""
    experience_depth: MatchAssessment
    role_match: MatchAssessment
    industry: MatchAssessment
    special_requirements: MatchAssessment
    category: str = ""
    reason: str = ""
    flag_matches: list[FlagMatch] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_discrete_scores(self) -> ResumeAnalysis:
        maxima = {"tasks": 4, "industry": 4, "experience_depth": 4, "role_match": 4,
                  "special_requirements": 2}
        for key, maximum in maxima.items():
            value = getattr(self, key)
            if value is not None and value.score > maximum:
                raise ValueError(f"{key}.score must be between 0 and {maximum}")
        return self


def default_scoring_criteria() -> list[ScoringCriterion]:
    return [
        ScoringCriterion(key="tasks", title="Задачи", description="Соответствие задач вакансии задачам из опыта пользователя.", max_points=35),
        ScoringCriterion(key="skills", title="Навыки", description="Hard skills и инструменты.", max_points=20),
        ScoringCriterion(key="experience_depth", title="Годы опыта", description="Уровень и глубина ответственности.", max_points=15),
        ScoringCriterion(key="role_match", title="Роль", description="Соответствие роли по фактическим обязанностям.", max_points=10),
        ScoringCriterion(key="industry", title="Сфера", description="Соответствие домена/индустрии.", max_points=10),
        ScoringCriterion(key="special_requirements", title="Особые требования", description="Образование и дополнительные требования.", max_points=10),
    ]


class Salary(BaseModel):
    minimum: int | None = None
    maximum: int | None = None
    currency: str = "RUB"
    gross: bool | None = None


class ApplicationQuestion(BaseModel):
    label: str
    required: bool = False
    sensitive: bool = False


class ApplicationField(BaseModel):
    """Visible form metadata. IDs are assigned by the adapter, never by the model."""
    id: str
    label: str
    kind: Literal["text", "number", "radio", "checkbox", "select", "multiselect", "unsupported"] = "text"
    options: list[str] = Field(default_factory=list)
    required: bool = True
    max_length: int | None = None


class FormAnswer(BaseModel):
    values: list[str] = Field(default_factory=list)
    # Binds an answer to a particular question/options, including after reloads.
    field: ApplicationField
    source: str = ""
    explanation: str = ""


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
    payment_frequency: str | None = None
    required_experience: str | None = None
    hiring_format: str | None = None
    work_schedule: str | None = None
    working_hours: str | None = None
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
    raw_points: int = Field(default=0, ge=0)
    raw_max_points: int = Field(default=0, ge=0)
    minimum_points: int | None = Field(default=None, ge=0)
    minimum_failed: bool = False
    explanation: str
    evidence: list[str] = Field(default_factory=list)


class JobEvaluation(BaseModel):
    decision: Literal["apply", "skip"]
    score: int = Field(ge=0, le=100)
    confidence: float = Field(ge=0, le=1)
    category: str
    score_breakdown: list[ScoreComponent] = Field(default_factory=list)
    hard_rule_violations: list[str] = Field(default_factory=list)
    minimum_score_violations: list[str] = Field(default_factory=list)
    positive_evidence: list[Evidence] = Field(default_factory=list)
    negative_evidence: list[Evidence] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)
    flag_matches: list[FlagMatch] = Field(default_factory=list)
    # Persisted evaluations created before strict preference validation omit
    # this field and therefore cannot authorize a cached apply decision.
    preference_flags_verified: bool = False
    has_test_assignment: bool = False
    reason: str

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
    form_answers: dict[str, FormAnswer] = Field(default_factory=dict)
    unanswered_fields: dict[str, str] = Field(default_factory=dict)
    form_fields: dict[str, ApplicationField] = Field(default_factory=dict)
    allow_foreign_application: bool = False
    submission_allowed: bool = False


class CoverLetterDraft(BaseModel):
    text: str = Field(min_length=40, max_length=3000)


class VacancyState(StrEnum):
    EXTRACTED = "EXTRACTED"
    EVALUATING = "EVALUATING"
    REJECTED_BY_MODEL = "REJECTED_BY_MODEL"
    READY_TO_SUBMIT = "READY_TO_SUBMIT"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    ALREADY_APPLIED = "ALREADY_APPLIED"
    READY_TO_REPORT = "READY_TO_REPORT"
    REPORTED = "REPORTED"
    UNCONFIRMED = "UNCONFIRMED"
    ERROR = "ERROR"


class SessionStatus(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    STOPPED = "STOPPED"
    FAILED = "FAILED"
