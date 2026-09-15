"""HireHi HTML resume importer."""

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
    list_field,
    now_utc,
    text_field,
    updated_at,
)

from . import resume_locators as L

POLICY = ResumeURLPolicy(
    "hirehi", "hirehi.ru", r"/resume/(?P<id>[A-Za-z0-9_-]{6,128})/?",
    r"[A-Za-z0-9_-]{6,128}", r"^(?:www\.)?hirehi\.ru$",
)


async def _child_text(item, selector: str, label: str, *, required: bool = True):
    class PageLike:
        def locator(self, value):
            return item.locator(value)
    result = await text_field(PageLike(), selector, "experience", label=label)
    if required and result.availability is FieldAvailability.NOT_PROVIDED:
        result.availability = FieldAvailability.PARSE_ERROR
    return result


async def _item_text(item) -> str:
    try:
        return " ".join((await item.inner_text(timeout=8_000)).split())
    except Exception:
        return ""


class HireHiResumeExtractor:
    version = "hirehi-resume-v1"
    policy = POLICY
    critical_selectors = (L.NAME, L.TITLE)

    async def list_resume_refs(self, page, policy):
        refs = []
        links = page.locator(L.ACCOUNT_RESUME_LINKS)
        for index in range(await links.count()):
            link = links.nth(index)
            if not await link.is_visible():
                continue
            href = await link.get_attribute("href")
            try:
                ref = policy.validate(href if href and href.startswith("http") else f"https://hirehi.ru{href or ''}")
            except ValueError:
                continue
            if all(existing.external_id != ref.external_id for existing in refs):
                refs.append(ref)
        return refs

    async def extract(self, page, ref, policy):
        identity = ResumeIdentity(
            full_name=await text_field(page, L.NAME, "identity", label="full_name"),
            gender=empty_field("identity", "gender", unsupported=True),
            age=empty_field("identity", "age", unsupported=True),
            has_photo=await bool_field(page, ".resume-public-photo img", "identity", label="has_photo"),
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
            desired_salary=empty_field("target", "desired_salary", unsupported=True),
            employment_types=await list_field(page, L.EMPLOYMENT, "target", label="employment_types"),
            work_formats=await list_field(page, L.WORK_FORMAT, "target", label="work_formats"),
        )
        location = ResumeLocation(
            residence=await text_field(page, L.CITY, "location", label="residence"),
            relocation=await text_field(page, L.RELOCATION, "location", label="relocation"),
            business_trips=await text_field(page, L.TRIPS, "location", label="business_trips"),
            citizenship=empty_field("location", "citizenship", unsupported=True),
            work_permit=empty_field("location", "work_permit", unsupported=True),
        )
        about = await text_field(page, L.ABOUT, "about", label="about")
        experience = []
        items = page.locator(L.EXPERIENCE)
        for index in range(await items.count()):
            item = items.nth(index)
            if not await item.is_visible():
                continue
            experience.append(ResumeExperience(
                company=await _child_text(item, L.EXPERIENCE_COMPANY, "company"),
                position=await _child_text(item, L.EXPERIENCE_TITLE, "position"),
                duties=await _child_text(item, L.EXPERIENCE_DESCRIPTION, "duties", required=False),
                source_section="experience",
            ))
        skills_field = await list_field(page, L.SKILL, "skills", label="name")
        skills = [ResumeSkill(name=value) for value in (skills_field.value or [])]
        projects = []
        project_items = page.locator(L.PROJECT)
        for index in range(await project_items.count()):
            item = project_items.nth(index)
            if await item.is_visible():
                projects.append(ResumeProject(name=await _item_text(item)))
        education = []
        for index in range(await page.locator(L.EDUCATION).count()):
            item = page.locator(L.EDUCATION).nth(index)
            if await item.is_visible():
                education.append(ResumeEducation(institution=await _item_text(item)))
        languages = []
        for index in range(await page.locator(L.LANGUAGE).count()):
            item = page.locator(L.LANGUAGE).nth(index)
            if await item.is_visible():
                languages.append(ResumeLanguage(language=await _item_text(item)))
        courses = []
        for index in range(await page.locator(L.COURSE).count()):
            item = page.locator(L.COURSE).nth(index)
            if await item.is_visible():
                courses.append(ResumeCourse(name=await _item_text(item)))
        certifications = []
        for index in range(await page.locator(L.CERTIFICATION).count()):
            item = page.locator(L.CERTIFICATION).nth(index)
            if await item.is_visible():
                certifications.append(ResumeCertification(name=await _item_text(item)))
        awards = []
        for index in range(await page.locator(L.AWARD).count()):
            item = page.locator(L.AWARD).nth(index)
            if await item.is_visible():
                awards.append(ResumeAward(name=await _item_text(item)))
        portfolio = []
        for index in range(await page.locator(L.PORTFOLIO).count()):
            item = page.locator(L.PORTFOLIO).nth(index)
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
            certifications=certifications, awards=awards, portfolio=portfolio,
            about=about, additional_sections=additional_sections, coverage=ResumeCoverage(),
        )
        coverage_for(snapshot)
        return finalize_snapshot(snapshot)


extractor = HireHiResumeExtractor()


def validate_resume_url(url: str) -> ResumeRef:
    return POLICY.validate(url)
