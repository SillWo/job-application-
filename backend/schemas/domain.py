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
    decision: Literal["apply", "skip", "manual_review"]
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
    requires_manual_review: bool = False
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
