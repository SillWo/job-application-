from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from backend.schemas.domain import ApplicationField, ApplicationPlan, JobPosting


class AdapterManifest(BaseModel):
    site_id: str
    display_name: str
    allowed_domains: tuple[str, ...]
    supports_submission: bool = True


class LoginState(BaseModel):
    authenticated: bool
    message: str


class JobRef(BaseModel):
    external_id: str
    url: str


class EmployerContact(BaseModel):
    email: str | None = None
    telegram: str | None = None
    linkedin: str | None = None
    exhausted: bool = False


class ApplicationRoute(BaseModel):
    kind: Literal["hirehi_chat", "direct_contact", "external_employer", "unknown"]
    target_url: str | None = None
    contact: EmployerContact | None = None


class ApplicationForm(BaseModel):
    requires_cover_letter: bool = False
    questions: list[str] = Field(default_factory=list)
    fields: list[ApplicationField] = Field(default_factory=list)
    confirmation: Literal["foreign_country"] | None = None
    route: ApplicationRoute | None = None
    employer_contact: EmployerContact | None = None
    target_url: str | None = None


class FillResult(BaseModel):
    success: bool
    unknown_questions: list[str] = Field(default_factory=list)
    answered_fields: list[str] = Field(default_factory=list)


class SubmissionResult(BaseModel):
    status: Literal["submitted", "already_applied", "unknown", "blocked", "needs_input"]
    message: str


class Blocker(BaseModel):
    kind: Literal["captcha", "mfa", "blocked", "test", "sensitive", "unknown_form"]
    message: str


class JobSiteAdapter(Protocol):
    site_id: str
    display_name: str
    allowed_domains: tuple[str, ...]
    manifest: AdapterManifest

    async def start(self, context: Any, settings: dict) -> None: ...
    async def get_login_state(self, page: Any) -> LoginState: ...
    async def open_search(self, page: Any, filters: dict) -> None: ...
    async def collect_job_refs(self, page: Any) -> list[JobRef]: ...
    async def collect_more_job_refs(self, page: Any) -> list[JobRef]: ...
    async def open_job(self, page: Any, ref: JobRef) -> None: ...
    async def extract_job(self, page: Any) -> JobPosting: ...
    async def open_application(self, page: Any) -> ApplicationForm: ...
    async def fill_application(self, page: Any, plan: ApplicationPlan) -> FillResult: ...
    async def submit_application(self, page: Any) -> SubmissionResult: ...
    async def verify_submission(self, page: Any) -> SubmissionResult: ...
    async def detect_blockers(self, page: Any) -> list[Blocker]: ...
