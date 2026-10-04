"""Deterministic HireHi public-resume extractor."""

from __future__ import annotations

import hashlib
import re
from contextlib import suppress

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
    now_utc,
    updated_at,
)

from . import resume_locators as L

POLICY = ResumeURLPolicy(
    "hirehi", "hirehi.ru", r"/resume/(?P<id>[A-Za-z0-9_-]{6,128})/?",
    r"[A-Za-z0-9_-]{6,128}", r"^(?:www\.)?hirehi\.ru$",
)


def _field(value, section: str, *, required: bool = False):
    value = " ".join(str(value or "").split()).strip()
    value = re.sub(r"^[·•|]+\s*", "", value)
    if value:
        return SourceField(value=value, availability=FieldAvailability.PRESENT,
                           source_section=section)
    return SourceField(
        availability=FieldAvailability.PARSE_ERROR if required else FieldAvailability.NOT_PROVIDED,
        source_section=section,
    )


def _split_meta(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"\s*[·•|]\s*", value or "") if part.strip()]


def _looks_like_grade(value: str) -> bool:
    return bool(re.search(
        r"\b(?:intern|junior|middle|senior|lead|head|стажер|младш|средн|старш|ведущ|руковод)\w*\b",
        value, re.I,
    ))


def _looks_like_employment(value: str) -> bool:
    return bool(re.search(
        r"занятост|employment|full[- ]?time|part[- ]?time|проект|стажиров|contract|freelance|полная|частичная",
        value, re.I,
    ))


def _looks_like_format(value: str) -> bool:
    return bool(re.search(r"удал|remote|офис|office|гибрид|hybrid|on[- ]?site|вахт", value, re.I))


async def _visible(node) -> bool:
    method = getattr(node, "is_visible", None)
    if method is None:
        return True
    return bool(await method())


async def _inner_text(node) -> str:
    try:
        return " ".join((await node.inner_text(timeout=8_000)).split()).strip()
    except TypeError:
        return " ".join((await node.inner_text()).split()).strip()


async def _values(root, selector: str, *, attribute: str | None = None) -> tuple[list[str], bool, bool, bool]:
    try:
        locator = root.locator(selector)
        count = await locator.count()
    except Exception:
        return [], False, False, True
    values: list[str] = []
    visible = False
    read_error = False
    for index in range(count):
        node = locator.nth(index)
        if not await _visible(node):
            continue
        visible = True
        try:
            if attribute:
                value = await node.get_attribute(attribute)
            else:
                try:
                    value = await node.inner_text(timeout=8_000)
                except TypeError:
                    value = await node.inner_text()
        except Exception:
            value = ""
            read_error = True
        value = " ".join(str(value or "").split()).strip()
        if value and value not in values:
            values.append(value)
    return values, count > 0, visible, read_error


async def _first(root, selector: str, section: str, *, attribute: str | None = None,
                 required: bool = False):
    values, matched, visible, read_error = await _values(root, selector, attribute=attribute)
    if read_error:
        return SourceField(availability=FieldAvailability.PARSE_ERROR, source_section=section)
    if values:
        return _field(values[0], section, required=required)
    return SourceField(
        availability=FieldAvailability.PARSE_ERROR if required else (
            FieldAvailability.HIDDEN if matched and not visible else FieldAvailability.NOT_PROVIDED
        ), source_section=section,
    )


async def _list(root, selector: str, section: str, *, attribute: str | None = None,
                required: bool = False):
    values, matched, visible, read_error = await _values(root, selector, attribute=attribute)
    if read_error:
        return SourceField(availability=FieldAvailability.PARSE_ERROR, source_section=section)
    if values:
        return SourceField(value=values, availability=FieldAvailability.PRESENT,
                           source_section=section)
    return SourceField(
        availability=FieldAvailability.PARSE_ERROR if required else (
            FieldAvailability.HIDDEN if matched and not visible else FieldAvailability.NOT_PROVIDED
        ), source_section=section,
    )


async def _attribute_or_text(root, selector: str, section: str):
    for attr in ("href", "src"):
        values, _, _, _ = await _values(root, selector, attribute=attr)
        if values:
            return _field(values[0], section)
    return await _first(root, selector, section)


async def _contact_field(root, selector: str, section: str, prefix: str):
    values, _, _, read_error = await _values(root, selector, attribute="href")
    if read_error:
        return SourceField(availability=FieldAvailability.PARSE_ERROR, source_section=section)
    if values:
        return _field(re.sub(rf"^{re.escape(prefix)}", "", values[0], flags=re.I), section)
    return await _first(root, selector, section)


def _date_values(values: list[str]) -> tuple[str | None, str | None]:
    values = [re.sub(r"\s+[—–-]\s*$", "", value).strip() for value in values]
    return (values[0], values[1] if len(values) > 1 else None) if values else (None, None)


def _section_kind(title: str, *, has_work_markers: bool = False) -> str:
    lowered = title.casefold()
    rules = (
        ("experience", r"опыт|работ|experience|employment|work history"),
        ("education", r"образован|education|университет|school|college"),
        ("skills", r"навык|skills|компетенц"),
        ("languages", r"язык|language"),
        ("awards", r"наград|award|grant|грант|достижен"),
        ("courses", r"курс|course|обучен"),
        ("certifications", r"сертифик|certif"),
        ("projects", r"проект|project"),
        ("portfolio", r"портфолио|portfolio"),
        ("additional", r"дополн|additional|прочее|other"),
    )
    for kind, pattern in rules:
        if re.search(pattern, lowered):
            return kind
    return "experience" if has_work_markers else "unknown"


def _section_title_text(title: str) -> str:
    return re.sub(r"\s*[·•|]\s*.*$", "", title or "").strip() or title.strip()


class HireHiResumeExtractor:
    version = "hirehi-resume-v2"
    policy = POLICY
    critical_selectors = (L.NAME, L.TITLE)

    async def list_resume_refs(self, page, policy):
        refs = []
        links = page.locator(L.ACCOUNT_RESUME_LINKS)
        for index in range(await links.count()):
            link = links.nth(index)
            if not await _visible(link):
                continue
            href = await link.get_attribute("href")
            try:
                absolute = href if href and href.startswith("http") else f"https://hirehi.ru{href or ''}"
                ref = policy.validate(absolute)
            except (ValueError, TypeError):
                continue
            if all(existing.external_id != ref.external_id for existing in refs):
                if "import_url" in getattr(ResumeRef, "model_fields", {}):
                    ref = ref.model_copy(update={"import_url": absolute})
                refs.append(ref)
        return refs

    async def _target(self, page):
        meta = await _first(page, L.HEADER_META, "target")
        parts = _split_meta(meta.value or "")
        grade = await _first(page, L.GRADE, "target")
        employment = await _list(page, L.EMPLOYMENT, "target")
        formats = await _list(page, L.WORK_FORMAT, "target")
        if not grade.value:
            value = next((part for part in parts if _looks_like_grade(part)), "")
            if value:
                grade = _field(value, "target")
        if not employment.value:
            value = next((part for part in parts if _looks_like_employment(part)), "")
            if value:
                employment = SourceField(value=[value], availability=FieldAvailability.PRESENT,
                                          source_section="target")
        if not formats.value:
            value = next((part for part in parts if _looks_like_format(part)), "")
            values = [item.strip() for item in value.split(",") if item.strip()]
            if values:
                formats = SourceField(value=values, availability=FieldAvailability.PRESENT,
                                      source_section="target")
        return ResumeTarget(
            desired_title=await _first(page, L.TITLE, "target", required=True),
            specializations=await _list(page, L.SPECIALIZATION, "target"),
            grade=grade,
            desired_salary=empty_field("target", "desired_salary"),
            employment_types=employment,
            work_formats=formats,
        )

    async def _experience_item(self, item):
        section = "experience"
        company = await _first(item, L.EXPERIENCE_COMPANY, section, required=True)
        company_url = await _first(item, L.EXPERIENCE_COMPANY_LINK, section, attribute="href")
        title = await _first(item, L.EXPERIENCE_TITLE, section, required=True)
        subtitle_values, _, _, _ = await _values(
            item, ".resume-public-list-subtitle, .resume-public-position"
        )
        if subtitle_values:
            title = _field(subtitle_values[0].split(" · ", 1)[0], section, required=True)
        dates, _, _, _ = await _values(item, L.EXPERIENCE_DATE_LINE)
        start, end = _date_values(dates)
        duties = await _first(item, L.EXPERIENCE_DESCRIPTION, section)
        achievements = await _list(item, L.EXPERIENCE_ACHIEVEMENT, section)
        if duties.availability is FieldAvailability.NOT_PROVIDED and not achievements.value:
            duties = await _first(item, L.EXPERIENCE_DESCRIPTION_CONTAINER, section)
        return ResumeExperience(
            company=company, position=title,
            start_date=_field(start, section) if start else SourceField(source_section=section),
            end_date=_field(end, section) if end else SourceField(source_section=section),
            duties=duties, achievements=achievements, source_section=section,
            location=await _first(item, L.EXPERIENCE_LOCATION, section),
            company_url=company_url,
            industries=await _list(item, L.EXPERIENCE_INDUSTRIES, section),
            employment_type=await _first(item, L.EXPERIENCE_EMPLOYMENT, section),
            work_format=await _first(item, L.EXPERIENCE_WORK_FORMAT, section),
            grade=await _first(item, L.EXPERIENCE_GRADE, section),
            duration=await _first(item, L.EXPERIENCE_DURATION, section),
        )

    async def _education_item(self, item):
        section = "education"
        institution = await _first(item, L.EXPERIENCE_COMPANY, section, required=True)
        title = await _first(item, ".resume-public-list-title", section)
        subtitle = await _first(item, ".resume-public-list-subtitle", section)
        parts = _split_meta(subtitle.value or "")
        degree = _field(parts[0], section) if parts else subtitle
        specialty = _field(parts[1], section) if len(parts) > 1 else title
        faculty = await _first(item, L.EDUCATION_FACULTY, section)
        if faculty.availability is not FieldAvailability.PRESENT and len(parts) > 2:
            faculty = _field(parts[2], section)
        dates, _, _, _ = await _values(item, L.EXPERIENCE_DATE_LINE)
        start, end = _date_values(dates)
        description = await _first(item, L.EXPERIENCE_DESCRIPTION_CONTAINER, section)
        duration = await _first(item, L.EXPERIENCE_DURATION, section)
        return ResumeEducation(
            institution=institution, specialty=specialty, degree=degree,
            start_date=_field(start, section) if start else SourceField(source_section=section),
            end_date=_field(end, section) if end else SourceField(source_section=section),
            faculty=faculty,
            duration=duration,
            description=description,
        )

    async def _simple_item(self, item, section: str, model):
        name = await _first(item, ".resume-public-list-title, .resume-public-list-company, [data-field='name']", section, required=True)
        dates, _, _, _ = await _values(item, L.EXPERIENCE_DATE_LINE)
        start, end = _date_values(dates)
        year = _field(end or start, section) if (end or start) else SourceField(source_section=section)
        issuer = await _first(item, L.EXPERIENCE_LOCATION, section)
        description = await _first(item, L.EXPERIENCE_DESCRIPTION_CONTAINER, section)
        if model is ResumeCourse:
            return model(name=name, institution=issuer, year=year, description=description)
        return model(name=name, issuer=issuer, year=year, description=description)

    async def _language_items(self, root):
        result = []
        items = root.locator(L.LANGUAGE)
        for index in range(await items.count()):
            item = items.nth(index)
            if not await _visible(item):
                continue
            language = await _first(item, L.LANGUAGE_NAME, "languages", required=True)
            levels, _, _, _ = await _values(item, L.LANGUAGE_LEVEL)
            proficiency = _field(" — ".join(levels), "languages") if levels else SourceField(source_section="languages")
            result.append(ResumeLanguage(language=language, proficiency=proficiency))
        return result

    async def _skill_items(self, root):
        result = []
        rows = root.locator(L.SKILL_ROW)
        row_count = await rows.count()
        if row_count:
            for index in range(row_count):
                row = rows.nth(index)
                if not await _visible(row):
                    continue
                category = await _first(row, L.SKILL_LABEL, "skills")
                names = await _list(row, L.SKILL, "skills")
                result.extend(ResumeSkill(name=_field(name, "skills"), category=category)
                              for name in names.value or [])
            return result
        names = await _list(root, L.SKILL, "skills")
        return [ResumeSkill(name=_field(name, "skills")) for name in names.value or []]

    async def _section_records(self, section_root):
        records = section_root.locator(L.ITEMS)
        result = []
        seen_keys: set[str] = set()
        for index in range(await records.count()):
            item = records.nth(index)
            if await _visible(item):
                key = ""
                for attr in ("data-record-id", "data-item-id", "data-id", "id"):
                    with suppress(Exception):
                        key = str(await item.get_attribute(attr) or "")
                    if key:
                        break
                if key and key in seen_keys:
                    continue
                if key:
                    seen_keys.add(key)
                result.append(item)
        return result

    async def extract(self, page, ref, policy):
        identity = ResumeIdentity(
            full_name=await _first(page, L.NAME, "identity", required=True),
            gender=empty_field("identity", "gender"),
            age=empty_field("identity", "age"),
            has_photo=await bool_field(page, L.PHOTO, "identity", label="has_photo"),
            photo_url=await _attribute_or_text(page, L.PHOTO, "identity"),
        )
        contacts = ResumeContacts(
            phone=await _contact_field(page, L.PHONE, "contacts", "tel:"),
            email=await _contact_field(page, L.EMAIL, "contacts", "mailto:"),
            messengers=await _list(page, L.MESSENGER, "contacts", attribute="href"),
            links=await _list(page, L.LINK, "contacts", attribute="href"),
            preferred_contact=await _attribute_or_text(page, L.PREFERRED_CONTACT, "contacts"),
        )
        target = await self._target(page)
        residence = await _first(page, L.CITY, "location")
        relocation = await _first(page, L.RELOCATION, "location")
        if residence.availability is FieldAvailability.PRESENT and residence.value:
            match = re.search(r"\(([^)]*(?:релокац|relocat)[^)]*)\)", residence.value, re.I)
            if match:
                residence = _field(residence.value[:match.start()].rstrip(" ,"), "location")
                if relocation.availability is not FieldAvailability.PRESENT:
                    relocation = _field(match.group(1), "location")
        location = ResumeLocation(
            residence=residence,
            relocation=relocation,
            business_trips=await _first(page, L.TRIPS, "location"),
            citizenship=empty_field("location", "citizenship"),
            work_permit=empty_field("location", "work_permit"),
            commute_time=await _first(page, L.COMMUTE_TIME, "location"),
        )
        about = await _first(page, L.ABOUT, "about")
        experience, education, languages, courses, awards = [], [], [], [], []
        certifications, projects, portfolio, skills, additional_sections = [], [], [], [], []
        total_experience = SourceField(source_section="experience")
        sections = page.locator(L.SECTIONS)
        for index in range(await sections.count()):
            root = sections.nth(index)
            if not await _visible(root):
                marker = ""
                with suppress(Exception):
                    marker = str(await root.get_attribute("data-section") or "")
                additional_sections.append(AdditionalResumeSection(
                    name=marker or f"hidden_section_{index + 1}",
                    content=SourceField(
                        availability=FieldAvailability.PARSE_ERROR,
                        source_section="additional",
                    ),
                ))
                continue
            title_field = await _first(root, L.SECTION_TITLE, "metadata")
            title = str(title_field.value or "")
            section_marker = ""
            with suppress(Exception):
                section_marker = str(await root.get_attribute("data-section") or "")
            items = await self._section_records(root)
            # The header is a resume card too, but it is not a content section.
            if not title and not items:
                continue
            marker_values = await _values(items[0], L.EXPERIENCE_WORK_FORMAT) if items else ([], False, False, False)
            kind = _section_kind(section_marker or title, has_work_markers=bool(marker_values[0]))
            if kind == "experience":
                if "·" in title:
                    total_experience = _field(title.split("·", 1)[1], "experience")
                experience.extend([await self._experience_item(item) for item in items])
            elif kind == "education":
                education.extend([await self._education_item(item) for item in items])
            elif kind in {"languages", "skills"}:
                languages.extend(await self._language_items(root))
                skills.extend(await self._skill_items(root))
            elif kind == "courses":
                courses.extend([await self._simple_item(item, "courses", ResumeCourse) for item in items])
            elif kind == "awards":
                awards.extend([await self._simple_item(item, "awards", ResumeAward) for item in items])
            elif kind == "certifications":
                certifications.extend([await self._simple_item(item, "certifications", ResumeCertification) for item in items])
            elif kind == "projects":
                projects.extend([ResumeProject(name=await _first(item, L.EXPERIENCE_COMPANY, "projects", required=True)) for item in items])
            elif kind == "portfolio":
                portfolio.extend([ResumePortfolioItem(title=await _first(item, L.EXPERIENCE_COMPANY, "portfolio", required=True)) for item in items])
            else:
                content = await _inner_text(root)
                if content:
                    additional_sections.append(AdditionalResumeSection(
                        name=_section_title_text(title) or f"unknown_section_{index + 1}",
                        content=_field(content, "additional"),
                    ))
        if not languages and not skills:
            languages = await self._language_items(page)
            skills = await self._skill_items(page)
        snapshot = SiteResumeSnapshot(
            extractor_version=self.version, source_site=ref.source_site,
            source_resume_id=ref.external_id,
            source_url_hash=hashlib.sha256(ref.url.encode()).hexdigest(),
            content_hash="0" * 64, imported_at=now_utc(),
            source_updated_at=await updated_at(page, L.UPDATED),
            source_updated_text=await _first(page, L.UPDATED, "metadata"),
            identity=identity, contacts=contacts, target=target, location=location,
            experience=experience, skills=skills, projects=projects, education=education,
            languages=languages, courses=courses, certifications=certifications,
            awards=awards, portfolio=portfolio, about=about,
            additional_sections=additional_sections, total_experience=total_experience,
            coverage=ResumeCoverage(),
        )
        coverage_for(snapshot)
        return finalize_snapshot(snapshot)


extractor = HireHiResumeExtractor()


def validate_resume_url(url: str) -> ResumeRef:
    ref = POLICY.validate(url)
    return ref.model_copy(update={"import_url": ref.url})
