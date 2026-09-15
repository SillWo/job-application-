"""HH.ru public resume importer (DOM-only, no API or raw HTML capture)."""

from __future__ import annotations

from backend.adapters.base.protocol import (
    AdditionalResumeSection,
    FieldAvailability,
    ResumeAward,
    ResumeCertification,
    ResumeContacts,
    ResumeCourse,
    ResumeCoverage,
    ResumeEducation,
    ResumeExperience,
    ResumeIdentity,
    ResumeLanguage,
    ResumeLocation,
    ResumePortfolioItem,
    ResumeProject,
    ResumeRef,
    ResumeSkill,
    ResumeTarget,
    SiteResumeSnapshot,
    SourceField,
)
from backend.adapters.base.resume_import import (
    ResumeURLPolicy,
    bool_field,
    coverage_for,
    empty_field,
    finalize_snapshot,
    integer_field,
    list_field,
    now_utc,
    text_field,
    updated_at,
)

from . import resume_locators as L

POLICY = ResumeURLPolicy(
    "hh", "hh.ru", r"/resume/(?P<id>[0-9a-fA-F]{16,64})/?",
    r"[0-9a-fA-F]{16,64}", r"^(?:hh\.ru|www\.hh\.ru|[a-z0-9-]+\.hh\.ru)$",
)


def _unsupported(section: str, name: str) -> SourceField:
    return empty_field(section, name, unsupported=True)


async def _child_text(item, selector: str, section: str, label: str, *, required: bool = True) -> SourceField[str]:
    # The shared reader accepts any Playwright-like object exposing locator.
    class PageLike:
        def locator(self, value):
            return item.locator(value)
    result = await text_field(PageLike(), selector, section, label=label)
    # An experience card exists, so an absent child hook indicates a DOM
    # change, not a legitimately omitted candidate field.
    if required and result.availability is FieldAvailability.NOT_PROVIDED:
        result.availability = FieldAvailability.PARSE_ERROR
    return result


async def _item_text(item) -> str:
    try:
        return " ".join((await item.inner_text(timeout=8_000)).split())
    except Exception:
        return ""


class HHResumeExtractor:
    version = "hh-resume-v1"
    policy = POLICY
    critical_selectors = (L.NAME, L.TITLE)

    async def list_resume_refs(self, page, policy: ResumeURLPolicy) -> list[ResumeRef]:
        refs: list[ResumeRef] = []
        links = page.locator(L.ACCOUNT_RESUME_LINKS)
        for index in range(await links.count()):
            link = links.nth(index)
            if not await link.is_visible():
                continue
            href = await link.get_attribute("href")
            if not href:
                continue
            try:
                ref = policy.validate(href if href.startswith("http") else f"https://hh.ru{href}")
            except ValueError:
                continue
            if all(old.external_id != ref.external_id for old in refs):
                refs.append(ref)
        return refs

    async def extract(self, page, ref: ResumeRef, policy: ResumeURLPolicy) -> SiteResumeSnapshot:
        section = "identity"
        identity = ResumeIdentity(
            full_name=await text_field(page, L.NAME, section, label="full_name"),
            gender=await text_field(page, L.GENDER, section, label="gender"),
            age=await integer_field(page, L.AGE, section),
            has_photo=await bool_field(page, L.PHOTO, section, label="has_photo"),
        )
        contacts = ResumeContacts(
            phone=await text_field(page, L.PHONE, "contacts", label="phone"),
            email=await text_field(page, L.EMAIL, "contacts", label="email"),
            messengers=await list_field(page, L.MESSENGER, "contacts", label="messengers"),
            links=await list_field(page, L.LINK, "contacts", label="links"),
        )
        target = ResumeTarget(
            desired_title=await text_field(page, L.TITLE, "target", label="desired_title"),
            specializations=await list_field(page, L.SPECIALIZATION, "target", label="specializations"),
            grade=await text_field(page, L.GRADE, "target", label="grade"),
            desired_salary=await text_field(page, L.SALARY, "target", label="desired_salary"),
            employment_types=await list_field(page, L.EMPLOYMENT, "target", label="employment_types"),
            work_formats=await list_field(page, L.WORK_FORMAT, "target", label="work_formats"),
        )
        location = ResumeLocation(
            residence=await text_field(page, L.CITY, "location", label="residence"),
            relocation=await text_field(page, L.RELOCATION, "location", label="relocation"),
            business_trips=await text_field(page, L.TRIPS, "location", label="business_trips"),
            citizenship=await text_field(page, L.CITIZENSHIP, "location", label="citizenship"),
            work_permit=await text_field(page, L.WORK_PERMIT, "location", label="work_permit"),
        )
        about = await text_field(page, L.ABOUT, "about", label="about")
        experience: list[ResumeExperience] = []
        if await page.locator(L.OWNER_EXPERIENCE_CARD).count():
            # The authenticated owner page renders complete cards without the
            # public child hooks. Preserve the visible card as duties and mark
            # fields that this owner layout does not expose as unsupported.
            items = page.locator(L.EXPERIENCE)
            for index in range(await items.count()):
                item = items.nth(index)
                if await item.is_visible():
                    experience.append(ResumeExperience(
                        company=_unsupported("experience", "company"),
                        position=_unsupported("experience", "position"),
                        duties=SourceField(
                            value=await _item_text(item),
                            availability=FieldAvailability.PRESENT,
                            source_section="experience",
                        ),
                        start_date=_unsupported("experience", "start_date"),
                        source_section="experience",
                    ))
        else:
            items = page.locator(L.EXPERIENCE)
            for index in range(await items.count()):
                item = items.nth(index)
                if not await item.is_visible():
                    continue
                company = await _child_text(item, L.EXPERIENCE_COMPANY, "experience", "company")
                position = await _child_text(item, L.EXPERIENCE_TITLE, "experience", "position")
                duties = await _child_text(item, L.EXPERIENCE_DESCRIPTION, "experience", "duties", required=False)
                period = await _child_text(item, L.EXPERIENCE_PERIOD, "experience", "period")
                # The first direct child on HH is a section header, not a job card.
                if (company.availability is FieldAvailability.PRESENT
                        and position.availability is FieldAvailability.PARSE_ERROR
                        and duties.availability is FieldAvailability.NOT_PROVIDED):
                    continue
                # HH's public view does not expose stable company/date hooks on all cards.
                if company.availability is FieldAvailability.PARSE_ERROR:
                    company = _unsupported("experience", "company")
                if period.availability is FieldAvailability.PARSE_ERROR:
                    period = _unsupported("experience", "period")
                experience.append(ResumeExperience(
                    company=company, position=position, duties=duties,
                    start_date=period,
                    source_section="experience",
                ))
        # Site-specific sections retain their own presence and provenance.
        skills = [ResumeSkill(name=x)
                  for x in (await list_field(page, L.SKILL, "skills", label="name")).value or []]
        projects = []
        project_items = page.locator(L.PROJECT)
        for index in range(await project_items.count()):
            item = project_items.nth(index)
            if await item.is_visible():
                projects.append(ResumeProject(name=await _item_text(item)))
        education = []
        education_items = page.locator(L.EDUCATION)
        for index in range(await education_items.count()):
            item = education_items.nth(index)
            if await item.is_visible():
                education.append(ResumeEducation(institution=await _item_text(item)))
        languages = []
        language_items = page.locator(L.LANGUAGE)
        for index in range(await language_items.count()):
            item = language_items.nth(index)
            if await item.is_visible():
                languages.append(ResumeLanguage(language=await _item_text(item)))
        courses = []
        course_items = page.locator(L.COURSE)
        for index in range(await course_items.count()):
            item = course_items.nth(index)
            if await item.is_visible():
                courses.append(ResumeCourse(name=await _item_text(item)))
        certifications = []
        cert_items = page.locator(L.CERTIFICATION)
        for index in range(await cert_items.count()):
            item = cert_items.nth(index)
            if await item.is_visible():
                certifications.append(ResumeCertification(name=await _item_text(item)))
        awards = []
        award_items = page.locator(L.AWARD)
        for index in range(await award_items.count()):
            item = award_items.nth(index)
            if await item.is_visible():
                awards.append(ResumeAward(name=await _item_text(item)))
        portfolio = []
        portfolio_items = page.locator(L.PORTFOLIO)
        for index in range(await portfolio_items.count()):
            item = portfolio_items.nth(index)
            if await item.is_visible():
                portfolio.append(ResumePortfolioItem(title=await _item_text(item)))
        additional_values = await list_field(page, L.ADDITIONAL, "additional", label="content")
        additional_sections = [AdditionalResumeSection(
            name="additional", content=SourceField(value=value,
            availability=FieldAvailability.PRESENT, source_section="additional"),
        ) for value in (additional_values.value or [])]
        snapshot = SiteResumeSnapshot(
            extractor_version=self.version, source_site=ref.source_site,
            source_resume_id=ref.external_id,
            source_url_hash=__import__("hashlib").sha256(ref.url.encode()).hexdigest(),
            content_hash="0" * 64,
            imported_at=now_utc(), source_updated_at=await updated_at(page, L.UPDATED), identity=identity, contacts=contacts,
            target=target, location=location, experience=experience, skills=skills,
            projects=projects, education=education, languages=languages, courses=courses,
            certifications=certifications, awards=awards, portfolio=portfolio, about=about,
            additional_sections=additional_sections, coverage=ResumeCoverage(),
        )
        coverage_for(snapshot)
        return finalize_snapshot(snapshot)


extractor = HHResumeExtractor()


def validate_resume_url(url: str) -> ResumeRef:
    return POLICY.validate(url)
