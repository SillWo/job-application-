"""Bounded HH.ru print-resume importer (visible DOM locators only)."""

from __future__ import annotations

import hashlib
import re

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
    attribute_field,
    bool_field,
    coverage_for,
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
    redirect_host_group={"hh.ru", "www.hh.ru", "krasnoyarsk.hh.ru"},
)


class _PageLike:
    def __init__(self, locator):
        self._locator = locator

    def locator(self, selector):
        return self._locator.locator(selector)


async def _child_text(item, selector: str, section: str, label: str,
                      *, required: bool = False) -> SourceField:
    value = await text_field(_PageLike(item), selector, section, label=label)
    if required and value.availability is FieldAvailability.NOT_PROVIDED:
        value.availability = FieldAvailability.PARSE_ERROR
    return value


async def _child_attr(item, selector: str, attr: str, section: str) -> SourceField:
    return await attribute_field(_PageLike(item), selector, attr, section)


async def _child_list(item, selector: str, section: str) -> SourceField[list[str]]:
    return await list_field(_PageLike(item), selector, section)


async def _item_text(item) -> str:
    return " ".join((await item.inner_text(timeout=8_000)).split())


def _hidden_field(section: str) -> SourceField:
    """Keep a matched but hidden record in the coverage ledger."""
    return SourceField(availability=FieldAvailability.HIDDEN, source_section=section)


def _hidden_experience() -> ResumeExperience:
    hidden = _hidden_field("experience")
    return ResumeExperience(
        company=hidden, position=_hidden_field("experience"),
        start_date=_hidden_field("experience"), end_date=_hidden_field("experience"),
        duties=_hidden_field("experience"), achievements=_hidden_field("experience"),
        location=_hidden_field("experience"), company_url=_hidden_field("experience"),
        industries=_hidden_field("experience"), employment_type=_hidden_field("experience"),
        work_format=_hidden_field("experience"), grade=_hidden_field("experience"),
        duration=_hidden_field("experience"), source_section="experience",
    )


def _hidden_education() -> ResumeEducation:
    return ResumeEducation(
        institution=_hidden_field("education"), faculty=_hidden_field("education"),
        specialty=_hidden_field("education"), degree=_hidden_field("education"),
        start_date=_hidden_field("education"), end_date=_hidden_field("education"),
        duration=_hidden_field("education"), description=_hidden_field("education"),
    )


async def _validate_content_sections(page) -> None:
    """Reject newly introduced content blocks instead of importing partial data."""
    known = {
        "resume-experience-block", "resume-education-block",
        "resume-languages-block", "resume-education-courses-block",
        "resume-additional-info-block",
    }
    blocks = page.locator("section[data-qa]")
    for index in range(await blocks.count()):
        block = blocks.nth(index)
        data_qa = (await block.get_attribute("data-qa") or "").strip()
        if data_qa in known or not re.fullmatch(r"resume-[a-z0-9_-]+-block", data_qa):
            continue
        if await block.is_visible() and await _item_text(block):
            raise ValueError(f"HH print resume unknown content section parse error: {data_qa}")


async def _hidden_block(page, selector: str) -> bool:
    blocks = page.locator(selector)
    count = await blocks.count()
    if not count:
        return False
    for index in range(count):
        if await blocks.nth(index).is_visible():
            return False
    return True


async def _attribute_list(page, selector: str, attr: str, section: str) -> SourceField[list[str]]:
    locator = page.locator(selector)
    count = await locator.count()
    if not count:
        return SourceField(availability=FieldAvailability.NOT_PROVIDED, source_section=section)
    values: list[str] = []
    visible = 0
    for index in range(count):
        item = locator.nth(index)
        if not await item.is_visible():
            continue
        visible += 1
        raw = (await item.get_attribute(attr) or "").strip()
        if raw and raw not in values:
            values.append(raw)
    if values:
        return SourceField(value=values, availability=FieldAvailability.PRESENT, source_section=section)
    return SourceField(
        availability=FieldAvailability.HIDDEN if not visible else FieldAvailability.NOT_PROVIDED,
        source_section=section,
    )


async def _messenger_values(page) -> SourceField[list[str]]:
    locator = page.locator(L.MESSENGER)
    count = await locator.count()
    if not count:
        return SourceField(availability=FieldAvailability.NOT_PROVIDED, source_section="contacts")
    values = []
    visible = 0
    for index in range(count):
        item = locator.nth(index)
        if not await item.is_visible():
            continue
        visible += 1
        value = (await item.get_attribute("href") or "").strip()
        if not value:
            value = " ".join((await item.inner_text()).split())
            if value.casefold().rstrip(":：") in {
                "viber", "telegram", "whatsapp", "setka", "сетка",
                "телеграм", "вайбер", "вотсап",
            }:
                continue
        if value and value not in values:
            values.append(value)
    return SourceField(value=values, availability=FieldAvailability.PRESENT, source_section="contacts") if values else SourceField(availability=FieldAvailability.HIDDEN if not visible else FieldAvailability.NOT_PROVIDED, source_section="contacts")


async def _comma_list(page, selector: str, section: str, label: str) -> SourceField[list[str]]:
    raw = await text_field(page, selector, section, label=label)
    if raw.value is None:
        return SourceField(availability=raw.availability, source_section=section)
    values = [part.strip() for part in raw.value.split(",") if part.strip()]
    return SourceField(value=values, availability=FieldAvailability.PRESENT, source_section=section)


async def _relocation(page) -> SourceField[str]:
    marker = page.locator(L.RELOCATION)
    if not await marker.count():
        return SourceField(availability=FieldAvailability.NOT_PROVIDED, source_section="location")
    try:
        parent = marker.nth(0).locator("xpath=..")
        if await parent.get_attribute("data-qa") == "resume-main-info_area-and-relocation":
            value = " ".join((await marker.nth(0).inner_text(timeout=8_000)).split())
        else:
            value = " ".join((await parent.inner_text(timeout=8_000)).split())
    except Exception:
        return SourceField(availability=FieldAvailability.PARSE_ERROR, source_section="location")
    return SourceField(value=value, availability=FieldAvailability.PRESENT, source_section="location") if value else SourceField(availability=FieldAvailability.PARSE_ERROR, source_section="location")


async def _experience_items(page):
    explicit = page.locator(L.EXPERIENCE_ITEM)
    if await explicit.count():
        return [explicit.nth(i) for i in range(await explicit.count())]
    periods = page.locator(L.EXPERIENCE_PERIOD)
    items = []
    for index in range(await periods.count()):
        period = periods.nth(index)
        # The print DOM repeats one h-spacing container per job.  This keeps
        # optional company links from shifting fields between adjacent jobs.
        item = period.locator(
            "xpath=ancestor::*[contains(@class, 'magritte-h-spacing-container')][1]"
        )
        if await item.count():
            items.append(item)
    if await periods.count() != len(items):
        raise ValueError("HH print resume experience structure is incomplete")
    return items


async def _experience(page) -> list[ResumeExperience]:
    result = []
    for item in await _experience_items(page):
        if not await item.is_visible():
            result.append(_hidden_experience())
            continue
        company = await _child_text(item, L.EXPERIENCE_COMPANY, "experience", "company", required=True)
        position = await _child_text(item, L.EXPERIENCE_TITLE, "experience", "position", required=True)
        if company.availability is FieldAvailability.PARSE_ERROR or position.availability is FieldAvailability.PARSE_ERROR:
            raise ValueError("HH print resume experience item structure is unknown")
        duties = await _child_text(item, L.EXPERIENCE_DESCRIPTION, "experience", "duties")
        start = await _child_text(item, L.EXPERIENCE_FROM, "experience", "start_date", required=True)
        if start.value:
            start.value = re.sub(r"\s*[-–—]\s*$", "", start.value)
        end = await _child_text(item, L.EXPERIENCE_TO, "experience", "end_date")
        duration = await _child_text(item, L.EXPERIENCE_DURATION, "experience", "duration")
        location = await _child_text(item, L.EXPERIENCE_AREA, "experience", "location")
        company_url = await _child_attr(item, L.EXPERIENCE_COMPANY_URL, "href", "experience")
        industries = await _child_list(item, L.EXPERIENCE_INDUSTRY, "experience")
        achievements = await _child_list(item, L.EXPERIENCE_ACHIEVEMENT, "experience")
        experience = ResumeExperience(
            company=company, position=position, duties=duties,
            start_date=start, end_date=end, source_section="experience",
            achievements=achievements,
            location=location, company_url=company_url,
            industries=industries, duration=duration,
            employment_type=await _child_text(item, "[data-qa='resume-experience-employment-type']", "experience", "employment_type"),
            work_format=await _child_text(item, "[data-qa='resume-experience-work-format']", "experience", "work_format"),
            grade=await _child_text(item, "[data-qa='resume-experience-grade']", "experience", "grade"),
        )
        result.append(experience)
    if not result and await _hidden_block(page, L.EXPERIENCE_BLOCK):
        result.append(_hidden_experience())
    return result


async def _education(page) -> list[ResumeEducation]:
    locator = page.locator(L.EDUCATION)
    result = []
    for index in range(await locator.count()):
        item = locator.nth(index)
        if not await item.is_visible():
            result.append(_hidden_education())
            continue
        institution = await _child_text(item, L.EDUCATION_INSTITUTION, "education", "institution", required=True)
        if institution.availability is FieldAvailability.PARSE_ERROR:
            raise ValueError("HH print resume education item structure is unknown")
        faculty = await _child_text(item, L.EDUCATION_FACULTY, "education", "faculty")
        specialty = await _child_text(item, L.EDUCATION_SPECIALTY, "education", "specialty")
        secondary = await _child_text(item, "[class*='magritte-text_style-secondary']", "education", "faculty")
        secondary_parts = [part.strip() for part in re.split(r"\s*•\s*", secondary.value or "") if part.strip()]
        if secondary_parts:
            main = [part.strip() for part in secondary_parts[0].rsplit(",", 1)]
            if faculty.availability is FieldAvailability.NOT_PROVIDED and len(main) == 2:
                faculty = SourceField(value=main[0], availability=FieldAvailability.PRESENT, source_section="education")
            if specialty.availability is FieldAvailability.NOT_PROVIDED and len(main) == 2:
                specialty = SourceField(value=main[1], availability=FieldAvailability.PRESENT, source_section="education")
        year = await _child_text(item, L.EDUCATION_YEAR, "education", "year")
        degree = await _child_text(item, L.EDUCATION_DEGREE, "education", "degree")
        tertiary_values = await _child_list(item, "[class*='magritte-text_style-tertiary']", "education")
        details = tertiary_values.value or []
        if secondary_parts:
            details.extend(secondary_parts[1:])
        if details:
            if year.availability is FieldAvailability.NOT_PROVIDED:
                for detail in details:
                    found_year = re.search(r"(?<!\d)(?:19|20)\d{2}(?!\d)", detail)
                    if found_year:
                        year = SourceField(value=found_year.group(), availability=FieldAvailability.PRESENT, source_section="education")
                        break
            if degree.availability is FieldAvailability.NOT_PROVIDED:
                parts = [part for part in details if part.strip() != "•"]
                if parts:
                    degree = SourceField(value=parts[-1], availability=FieldAvailability.PRESENT, source_section="education")
        education = ResumeEducation(
            institution=institution, faculty=faculty, specialty=specialty, degree=degree,
            end_date=year,
        )
        result.append(education)
    if not result and await _hidden_block(page, L.EDUCATION_BLOCK):
        result.append(_hidden_education())
    return result


async def _languages(page) -> list[ResumeLanguage]:
    locator = page.locator(L.LANGUAGE)
    result = []
    for index in range(await locator.count()):
        item = locator.nth(index)
        if not await item.is_visible():
            result.append(ResumeLanguage(
                language=_hidden_field("languages"), proficiency=_hidden_field("languages")
            ))
            continue
        name = await _child_text(item, L.LANGUAGE_NAME, "languages", "language")
        level = await _child_text(item, L.LANGUAGE_LEVEL, "languages", "proficiency")
        raw = await _item_text(item)
        if name.availability is FieldAvailability.NOT_PROVIDED and raw:
            parts = [part.strip() for part in re.split(r"\s+[—–-]\s+", raw, maxsplit=2)]
            name = SourceField(value=parts[0], availability=FieldAvailability.PRESENT, source_section="languages")
            if level.availability is FieldAvailability.NOT_PROVIDED and len(parts) > 1:
                level = SourceField(value=" — ".join(parts[1:]), availability=FieldAvailability.PRESENT, source_section="languages")
        if level.availability is FieldAvailability.NOT_PROVIDED:
            group = item.locator(L.LANGUAGE_NATIVE_GROUP)
            if await group.count():
                markers = group.get_by_text(L.LANGUAGE_NATIVE_MARKER, exact=True)
                for marker_index in range(await markers.count()):
                    if await markers.nth(marker_index).is_visible():
                        level = SourceField(value="Родной", availability=FieldAvailability.PRESENT, source_section="languages")
                        break
        result.append(ResumeLanguage(language=name, proficiency=level))
    if not result and await _hidden_block(page, L.LANGUAGE_BLOCK):
        result.append(ResumeLanguage(
            language=_hidden_field("languages"), proficiency=_hidden_field("languages")
        ))
    return result


async def _skills(page) -> list[ResumeSkill]:
    locator = page.locator(L.SKILL)
    result = []
    seen = set()
    for index in range(await locator.count()):
        item = locator.nth(index)
        if not await item.is_visible():
            result.append(ResumeSkill(name=_hidden_field("skills"), level=_hidden_field("skills")))
            continue
        explicit_name = await _child_text(item, L.SKILL_NAME, "skills", "name")
        name = (explicit_name.value or await _item_text(item)).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        level = await _child_text(item, "[data-qa='resume-skill-level']", "skills", "level")
        if level.availability is FieldAvailability.NOT_PROVIDED:
            group = item.locator("xpath=ancestor::div[.//*[starts-with(@data-qa, 'skill-level-title-')]][1]")
            if await group.count():
                level = await text_field(_PageLike(group), L.SKILL_LEVEL, "skills", label="level")
        result.append(ResumeSkill(name=SourceField(value=name, availability=FieldAvailability.PRESENT, source_section="skills"), level=level))
    if not result and await _hidden_block(page, "[data-qa='skills-table']"):
        result.append(ResumeSkill(name=_hidden_field("skills"), level=_hidden_field("skills")))
    return result


async def _additional(page) -> list[AdditionalResumeSection]:
    result = []
    generic = page.locator("[data-qa='resume-additional-section']")
    for index in range(await generic.count()):
        item = generic.nth(index)
        if await item.is_visible():
            result.append(AdditionalResumeSection(name="additional", content=SourceField(value=await _item_text(item), availability=FieldAvailability.PRESENT, source_section="additional")))
        else:
            result.append(AdditionalResumeSection(name="additional", content=_hidden_field("additional"), availability=FieldAvailability.HIDDEN))
    return result


async def _named_values(page, item_selector: str, name_selector: str, section: str) -> list[SourceField[str]]:
    locator = page.locator(item_selector)
    values = []
    for index in range(await locator.count()):
        item = locator.nth(index)
        if await item.is_visible():
            value = await _child_text(item, name_selector, section, "name", required=True)
            if value.availability is FieldAvailability.PARSE_ERROR:
                raise ValueError(f"HH print resume {section} item structure is unknown")
            values.append(value)
        else:
            values.append(_hidden_field(section))
    return values


async def _courses(page) -> list[ResumeCourse]:
    """Read the structured HH print course cells.

    HH renders courses under the education-courses block as a primary title
    and a secondary line containing ``provider, qualification`` plus a year.
    Keep those values separate while retaining the historical normalized
    ``name``/``institution``/``description`` fields.
    """
    locator = page.locator(L.COURSE_CELL)
    result: list[ResumeCourse] = []
    for index in range(await locator.count()):
        item = locator.nth(index)
        if not await item.is_visible():
            result.append(ResumeCourse(
                name=_hidden_field("courses"), title=_hidden_field("courses"),
                institution=_hidden_field("courses"), provider=_hidden_field("courses"),
                description=_hidden_field("courses"), qualification=_hidden_field("courses"),
                year=_hidden_field("courses"),
            ))
            continue
        title = await _child_text(item, L.COURSE_TITLE, "courses", "title", required=True)
        if title.availability is FieldAvailability.PARSE_ERROR:
            raise ValueError("HH print resume courses structure is unknown")
        meta = await _child_text(item, L.COURSE_META, "courses", "metadata")
        raw_meta = meta.value or ""
        year_match = re.search(r"(?<!\d)(?:19|20)\d{2}(?!\d)", raw_meta)
        year = SourceField(
            value=year_match.group() if year_match else None,
            availability=FieldAvailability.PRESENT if year_match else FieldAvailability.NOT_PROVIDED,
            source_section="courses",
        )
        heading = re.sub(r"\s*[•·]\s*", " ", raw_meta)
        if year_match:
            heading = heading.replace(year_match.group(), " ")
        heading = " ".join(heading.split()).strip(" ,")
        provider_value, qualification_value = heading, ""
        if "," in heading:
            provider_value, qualification_value = [part.strip() for part in heading.split(",", 1)]
        provider = SourceField(
            value=provider_value or None,
            availability=FieldAvailability.PRESENT if provider_value else FieldAvailability.NOT_PROVIDED,
            source_section="courses",
        )
        qualification = SourceField(
            value=qualification_value or None,
            availability=FieldAvailability.PRESENT if qualification_value else FieldAvailability.NOT_PROVIDED,
            source_section="courses",
        )
        result.append(ResumeCourse(
            name=title, title=title, institution=provider, provider=provider,
            description=qualification, qualification=qualification, year=year,
        ))
    if result:
        return result
    if await _hidden_block(page, L.COURSE_BLOCK):
        return [ResumeCourse(
            name=_hidden_field("courses"), title=_hidden_field("courses"),
            institution=_hidden_field("courses"), provider=_hidden_field("courses"),
            description=_hidden_field("courses"), qualification=_hidden_field("courses"),
            year=_hidden_field("courses"),
        )]
    # Keep compatibility with older print fixtures that use the explicit
    # course item hook while still failing closed when an item is malformed.
    return [ResumeCourse(name=value, title=value) for value in await _named_values(
        page, L.COURSE, L.COURSE_NAME, "courses"
    )]


class HHResumeExtractor:
    version = "hh-print-resume-v2"
    policy = POLICY
    critical_selectors = (L.NAME, L.TITLE)

    async def list_resume_refs(self, page, policy: ResumeURLPolicy) -> list[ResumeRef]:
        refs = []
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
        if not await page.locator(L.PRINT_MARKER).count():
            raise ValueError("HH print resume DOM is missing")
        await _validate_content_sections(page)
        identity = ResumeIdentity(
            full_name=await text_field(page, L.NAME, "identity", label="full_name"),
            gender=await text_field(page, L.GENDER, "identity", label="gender"),
            age=await integer_field(page, L.AGE, "identity"),
            birth_date=await text_field(page, L.BIRTH_DATE, "identity", label="birth_date"),
            has_photo=await bool_field(page, L.PHOTO, "identity", label="has_photo"),
            photo_url=await _child_attr(page, L.PHOTO_URL, "src", "identity"),
        )
        contacts = ResumeContacts(
            phone=await text_field(page, L.PHONE, "contacts", label="phone"),
            email=await text_field(page, L.EMAIL, "contacts", label="email"),
            messengers=await _messenger_values(page),
            links=await _attribute_list(page, L.LINK, "href", "contacts"),
            preferred_contact=await text_field(page, L.PREFERRED_CONTACT, "contacts", label="preferred_contact"),
            contact_comment=await text_field(page, L.CONTACT_COMMENT, "contacts", label="contact_comment"),
        )
        target = ResumeTarget(
            desired_title=await text_field(page, L.TITLE, "target", label="desired_title"),
            specializations=await _comma_list(page, L.SPECIALIZATION, "target", "specializations"),
            grade=await text_field(page, L.GRADE, "target", label="grade"),
            desired_salary=await text_field(page, L.SALARY, "target", label="desired_salary"),
            employment_types=await _comma_list(page, L.EMPLOYMENT, "target", "employment_types"),
            work_formats=await _comma_list(page, L.WORK_FORMAT, "target", "work_formats"),
        )
        location = ResumeLocation(
            residence=await text_field(page, L.CITY, "location", label="residence"),
            relocation=await _relocation(page),
            business_trips=await text_field(page, L.TRIPS, "location", label="business_trips"),
            citizenship=await _comma_list(page, L.CITIZENSHIP, "location", "citizenship"),
            work_permit=await text_field(page, L.WORK_PERMIT, "location", label="work_permit"),
            commute_time=await text_field(page, L.COMMUTE_TIME, "location", label="commute_time"),
        )
        experience = await _experience(page)
        education, languages, skills = await _education(page), await _languages(page), await _skills(page)
        projects = [ResumeProject(name=value) for value in await _named_values(page, L.PROJECT, L.PROJECT_NAME, "projects")]
        courses = await _courses(page)
        certifications = [ResumeCertification(name=value) for value in await _named_values(page, L.CERTIFICATION, L.CERTIFICATION_NAME, "certifications")]
        awards = [ResumeAward(name=value) for value in await _named_values(page, L.AWARD, L.AWARD_NAME, "awards")]
        portfolio = [ResumePortfolioItem(title=value) for value in await _named_values(page, L.PORTFOLIO, L.PORTFOLIO_TITLE, "portfolio")]
        about = await text_field(page, L.ABOUT, "about", label="about")
        total = await text_field(page, L.EXPERIENCE_TOTAL, "metadata", label="total_experience")
        if total.value:
            total.value = re.sub(r"^[^:]+:\s*", "", total.value)
        source_updated_text = await text_field(page, L.UPDATED, "metadata", label="source_updated_text")
        snapshot = SiteResumeSnapshot(
            schema_version=2, extractor_version=self.version, source_site=ref.source_site,
            source_resume_id=ref.external_id, source_url_hash=hashlib.sha256(ref.url.encode()).hexdigest(),
            content_hash="0" * 64, imported_at=now_utc(), source_updated_at=await updated_at(page, L.UPDATED),
            source_updated_text=source_updated_text,
            job_search_status=await text_field(page, L.JOB_SEARCH_STATUS, "metadata", label="job_search_status"),
            source_badges=await list_field(page, L.SOURCE_BADGES, "metadata", label="source_badges"),
            identity=identity, contacts=contacts, target=target, location=location,
            self_employment=await text_field(page, L.SELF_EMPLOYMENT, "additional_info", label="self_employment"),
            experience=experience, projects=projects, skills=skills, education=education,
            languages=languages, courses=courses, certifications=certifications, awards=awards,
            portfolio=portfolio, about=about, additional_sections=await _additional(page),
            total_experience=total,
            coverage=ResumeCoverage(),
        )
        coverage_for(snapshot)
        return finalize_snapshot(snapshot)


extractor = HHResumeExtractor()


def validate_resume_url(url: str) -> ResumeRef:
    return POLICY.validate(url)
