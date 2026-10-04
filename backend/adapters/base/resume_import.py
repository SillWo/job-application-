"""Shared, browser-only plumbing for site-specific resume importers.

The concrete extractors in each site package own their locators and mapping.
This module only provides URL policy, safe visible-text reads and snapshot
hashing.  It intentionally has no HTTP client and never reads raw HTML.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, unquote, urlparse, urlunparse

from backend.adapters.base.protocol import (
    FieldAvailability,
    ResumeRef,
    SiteResumeSnapshot,
    SourceField,
)


class ResumeURLPolicy:
    """Strict URL policy shared by the three public resume pages."""

    def __init__(self, site_id: str, base_domain: str, path_pattern: str, id_pattern: str,
                 host_pattern: str | None = None, *, requires_print: bool | None = None,
                 redirect_host_group: set[str] | frozenset[str] | tuple[str, ...] | None = None) -> None:
        self.site_id = site_id
        self.base_domain = base_domain
        self.path_re = re.compile(path_pattern)
        self.id_re = re.compile(id_pattern)
        self.host_re = re.compile(host_pattern or rf"^(?:www\.)?{re.escape(base_domain)}$", re.I)
        self.redirect_host_group = frozenset(
            host.casefold().rstrip(".") for host in (redirect_host_group or ())
        )
        # HH and Zarplata expose a distinct public print layout.  HireHi's
        # public page is already the canonical layout and rejects ``print``.
        self.requires_print = site_id in {"hh", "zarplata"} if requires_print is None else requires_print

    def _validate_query(self, parsed) -> tuple[bool, list[tuple[str, str]]]:
        if re.search(r"%(?![0-9A-Fa-f]{2})", parsed.query):
            raise ValueError("Ссылка на резюме содержит некорректное URL-кодирование")
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        print_values: list[str] = []
        for key, value in pairs:
            if key == "print":
                print_values.append(value)
                continue
            folded = key.casefold()
            if folded.startswith("utm_") or folded in {"from", "hhtmfrom"}:
                continue
            raise ValueError("Ссылка на резюме содержит недопустимые параметры")
        if len(print_values) > 1:
            raise ValueError("Параметр print в ссылке на резюме не должен дублироваться")
        if print_values and print_values[0] != "true":
            raise ValueError("Параметр print в ссылке на резюме должен быть true")
        if print_values and not self.requires_print:
            raise ValueError("Параметр print не поддерживается этим сайтом")
        return bool(print_values), pairs

    def import_url(self, canonical_url: str) -> str:
        """Return the browser URL for the site-specific public layout."""
        if not self.requires_print:
            return canonical_url
        parsed = urlparse(canonical_url)
        return urlunparse(parsed._replace(query="print=true", fragment=""))

    def _same_final_host(self, actual: str | None, expected: str) -> bool:
        actual = (actual or "").casefold().rstrip(".")
        expected = expected.casefold().rstrip(".")
        if actual == expected:
            return True
        # Trust only configured aliases; a shared domain suffix would also
        # accept attacker-controlled hosts such as evil.example.com.
        return bool(
            self.redirect_host_group
            and actual in self.redirect_host_group
            and expected in self.redirect_host_group
        )

    def validate(self, url: str) -> ResumeRef:
        if not isinstance(url, str) or len(url) > 2048:
            raise ValueError("Ссылка на резюме имеет недопустимый формат")
        parsed = urlparse(url.strip())
        host = (parsed.hostname or "").rstrip(".").lower()
        # Credentials, query strings, fragments and non-default ports are not
        # part of a public resume URL and can be used to smuggle a redirect.
        if parsed.scheme.lower() != "https" or not self.host_re.fullmatch(host):
            raise ValueError("Ссылка на резюме должна вести на разрешённый HTTPS-домен")
        if parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError("Ссылка на резюме не должна содержать учётные данные или порт")
        _has_print, _pairs = self._validate_query(parsed)
        if parsed.fragment or parsed.params:
            raise ValueError("Ссылка на резюме не должна содержать параметры")
        if not parsed.path or "%2f" in parsed.path.lower() or "%5c" in parsed.path.lower():
            raise ValueError("Ссылка на резюме имеет недопустимый путь")
        # Reject encoded characters rather than silently changing the resource
        # which the user approved.
        try:
            path = unquote(parsed.path)
        except Exception as exc:  # pragma: no cover - defensive parser guard
            raise ValueError("Ссылка на резюме имеет недопустимый путь") from exc
        if path != parsed.path:
            raise ValueError("Ссылка на резюме не должна содержать URL-кодирование")
        match = self.path_re.fullmatch(path)
        if not match:
            raise ValueError("Ожидалась прямая HTML-ссылка на резюме")
        external_id = match.group("id")
        if not self.id_re.fullmatch(external_id):
            raise ValueError("Идентификатор резюме имеет недопустимый формат")
        canonical = urlunparse(("https", host, path.rstrip("/"), "", "", ""))
        return ResumeRef(
            source_site=self.site_id,
            external_id=external_id,
            url=canonical,
            import_url=self.import_url(canonical),
        )

    def validate_final(self, url: str, expected_id: str, expected_host: str | None = None) -> ResumeRef:
        ref = self.validate(url)
        if ref.external_id != expected_id:
            raise ValueError("Переход изменил идентификатор резюме")
        if expected_host and not self._same_final_host(urlparse(ref.url).hostname, expected_host):
            raise ValueError("Переход изменил домен резюме")
        return ref


def clean_text(value: str | None) -> str:
    return " ".join((value or "").split()).strip()


async def _visible(locator: Any) -> bool:
    visible = getattr(locator, "is_visible", None)
    return bool(await visible()) if visible is not None else True


async def text_field(page: Any, selector: str, section: str, *, label: str | None = None,
                     optional: bool = True) -> SourceField[str]:
    """Read only visible text and retain hidden/not-provided provenance."""
    locator = page.locator(selector)
    count = await locator.count()
    if not count:
        return SourceField(availability=FieldAvailability.NOT_PROVIDED, source_section=section)
    for index in range(count):
        item = locator.nth(index)
        if not await _visible(item):
            continue
        try:
            text = clean_text(await item.inner_text(timeout=8_000))
        except Exception:
            return SourceField(availability=FieldAvailability.PARSE_ERROR, source_section=section)
        if text:
            return SourceField(value=text, availability=FieldAvailability.PRESENT, source_section=section)
    return SourceField(availability=FieldAvailability.HIDDEN, source_section=section)


async def attribute_field(page: Any, selector: str, attribute: str, section: str,
                          *, label: str | None = None) -> SourceField[str]:
    locator = page.locator(selector)
    count = await locator.count()
    if not count:
        return SourceField(availability=FieldAvailability.NOT_PROVIDED, source_section=section)
    for index in range(count):
        item = locator.nth(index)
        if not await _visible(item):
            continue
        value = clean_text(await item.get_attribute(attribute))
        if value:
            return SourceField(value=value, availability=FieldAvailability.PRESENT, source_section=section)
    return SourceField(availability=FieldAvailability.HIDDEN, source_section=section)


async def list_field(page: Any, selector: str, section: str, *, label: str | None = None
                     ) -> SourceField[list[str]]:
    locator = page.locator(selector)
    count = await locator.count()
    if not count:
        return SourceField(availability=FieldAvailability.NOT_PROVIDED, source_section=section)
    values: list[str] = []
    visible_count = 0
    for index in range(count):
        item = locator.nth(index)
        if not await _visible(item):
            continue
        visible_count += 1
        value = clean_text(await item.inner_text())
        if value and value not in values:
            values.append(value)
    if values:
        return SourceField(value=values, availability=FieldAvailability.PRESENT, source_section=section)
    status = FieldAvailability.HIDDEN if not visible_count else FieldAvailability.NOT_PROVIDED
    return SourceField(availability=status, source_section=section)


async def bool_field(page: Any, selector: str, section: str, *, label: str | None = None
                     ) -> SourceField[bool]:
    locator = page.locator(selector)
    count = await locator.count()
    if not count:
        return SourceField(availability=FieldAvailability.NOT_PROVIDED, source_section=section)
    if any([await _visible(locator.nth(index)) for index in range(count)]):
        return SourceField(value=True, availability=FieldAvailability.PRESENT, source_section=section)
    return SourceField(availability=FieldAvailability.HIDDEN, source_section=section)


async def integer_field(page: Any, selector: str, section: str) -> SourceField[int]:
    raw = await text_field(page, selector, section)
    if raw.value is None:
        return SourceField(availability=raw.availability, source_section=section)
    match = re.search(r"\d{1,3}", raw.value)
    if not match:
        return SourceField(availability=FieldAvailability.PARSE_ERROR, source_section=section)
    return SourceField(value=int(match.group()), availability=FieldAvailability.PRESENT,
                       source_section=section)


async def updated_at(page: Any, selector: str) -> datetime | None:
    """Read a machine timestamp from an approved ``time``/data hook."""
    raw = await attribute_field(page, selector, "datetime", "metadata")
    if raw.availability is not FieldAvailability.PRESENT or not raw.value:
        return None
    try:
        return datetime.fromisoformat(raw.value.replace("Z", "+00:00"))
    except ValueError:
        return None


def empty_field(section: str, label: str, *, unsupported: bool = False) -> SourceField[Any]:
    return SourceField(
        availability=FieldAvailability.UNSUPPORTED if unsupported else FieldAvailability.NOT_PROVIDED,
        source_section=section,
    )


def snapshot_hash(snapshot: SiteResumeSnapshot) -> str:
    data = snapshot.model_dump(mode="json", exclude={"content_hash", "imported_at", "source_updated_at"})

    def semantic(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: semantic(child)
                for key, child in value.items()
                if key not in {
                    "schema_version", "extractor_version", "source_site", "source_resume_id",
                    "source_url_hash", "source_updated_text", "coverage", "source_section", "source_locator",
                }
            }
        if isinstance(value, list):
            return [semantic(child) for child in value]
        return value

    data = semantic(data)
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def finalize_snapshot(snapshot: SiteResumeSnapshot) -> SiteResumeSnapshot:
    snapshot.content_hash = snapshot_hash(snapshot)
    return snapshot


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def coverage_for(snapshot: SiteResumeSnapshot) -> None:
    """Populate a compact coverage summary from typed field availability."""
    scalar_sections: dict[str, Any] = {
        "identity": snapshot.identity,
        "contacts": snapshot.contacts,
        "self_employment": snapshot.self_employment,
        "job_search_status": snapshot.job_search_status,
        "source_badges": snapshot.source_badges,
        "target": snapshot.target,
        "location": snapshot.location,
        "about": snapshot.about,
        "total_experience": snapshot.total_experience,
    }
    list_sections = (
        "experience", "projects", "skills", "education", "languages", "courses",
        "certifications", "awards", "portfolio", "additional_sections",
    )
    present: set[str] = set()
    missing: set[str] = set()
    hidden: list[str] = []
    unsupported: list[str] = []
    errors: list[str] = []
    def collect(value: Any, path: str, states: list[str]) -> None:
        if isinstance(value, SourceField):
            state = value.availability.value
            states.append(state)
            if state == "hidden":
                hidden.append(path)
            elif state == "unsupported":
                unsupported.append(path)
            elif state == "parse_error":
                errors.append(path)
            return
        if hasattr(value, "model_dump"):
            collect(value.model_dump(mode="python"), path, states)
            return
        if isinstance(value, dict):
            # This branch also handles test doubles and legacy extractor dicts.
            if "availability" in value:
                state = str(value.get("availability"))
                states.append(state)
                if state == "hidden":
                    hidden.append(path)
                elif state == "unsupported":
                    unsupported.append(path)
                elif state == "parse_error":
                    errors.append(path)
                return
            for name, child in value.items():
                collect(child, f"{path}.{name}" if path else str(name), states)
            return
        if isinstance(value, list):
            for index, child in enumerate(value):
                collect(child, f"{path}[{index}]", states)

    for section, model in scalar_sections.items():
        states: list[str] = []
        collect(model, section, states)
        if any(state == "present" for state in states):
            present.add(section)
        elif states and all(state == "not_provided" for state in states):
            missing.add(section)

    for section in list_sections:
        values = list(getattr(snapshot, section) or [])
        states: list[str] = []
        collect(values, section, states)
        # A non-empty list proves that the section exists even when individual
        # fields are hidden or unsupported.  Empty lists are simply missing;
        # they are not evidence that the site lacks the section.
        if values:
            present.add(section)
        elif not states:
            missing.add(section)
    snapshot.coverage.present_sections = sorted(present)
    if hasattr(snapshot.coverage, "missing_sections"):
        snapshot.coverage.missing_sections = sorted(missing)
    snapshot.coverage.unsupported_fields = sorted(set(unsupported))
    snapshot.coverage.hidden_fields = sorted(hidden)
    snapshot.coverage.parse_errors = sorted(errors)


def _page_url(page: Any, fallback: str) -> str:
    value = getattr(page, "url", fallback)
    if callable(value):
        value = value()
    return str(value or fallback)


async def _goto_checked(page: Any, url: str, *, max_bytes: int) -> Any:
    response = await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    headers = getattr(response, "headers", {}) if response is not None else {}
    raw_length = headers.get("content-length") if isinstance(headers, dict) else None
    if raw_length and str(raw_length).isdigit() and int(raw_length) > max_bytes:
        raise ValueError("Страница резюме слишком большая")
    return response


async def open_resume_page(page: Any, ref: ResumeRef, policy: ResumeURLPolicy) -> None:
    """Navigate to a validated import URL and verify redirects before retrying."""
    checked = policy.validate(ref.url)
    if checked.external_id != ref.external_id or checked.source_site != ref.source_site:
        raise ValueError("Ссылка и идентификатор резюме не совпадают")
    import_url = checked.import_url or policy.import_url(checked.url)
    await _goto_checked(page, import_url, max_bytes=8 * 1024 * 1024)
    final_url = _page_url(page, import_url)
    # Always validate the first redirect before deciding whether a retry is
    # safe. A hostile host or resume ID can never be retried.
    expected_host = urlparse(checked.url).hostname
    final_ref = policy.validate_final(final_url, checked.external_id, expected_host)
    if policy.requires_print:
        final_pairs = parse_qsl(urlparse(final_url).query, keep_blank_values=True)
        has_print = len([value for key, value in final_pairs if key == "print" and value == "true"]) == 1
        if not has_print:
            # The only permitted retry is the same validated resource after a
            # redirect that dropped the print query. The second result must
            # retain print=true; otherwise extraction fails closed.
            if final_ref.url != checked.url:
                raise ValueError("Переход со страницы резюме изменил ресурс")
            await _goto_checked(page, import_url, max_bytes=8 * 1024 * 1024)
            final_url = _page_url(page, import_url)
            policy.validate_final(final_url, checked.external_id, expected_host)
            final_pairs = parse_qsl(urlparse(final_url).query, keep_blank_values=True)
            has_print = len([value for key, value in final_pairs if key == "print" and value == "true"]) == 1
            if not has_print:
                raise ValueError("Страница резюме не сохранила print=true")
        await _wait_for_print_root(page)


async def _wait_for_print_root(page: Any) -> None:
    locator_factory = getattr(page, "locator", None)
    if locator_factory is None:
        return
    for selector in (
        "body.bloko-print",
        "[data-qa='resume-main-info__content-wrapper'], "
        "[data-testid='resume-main-info__content-wrapper'], .resume-main-info__content-wrapper",
    ):
        locator = locator_factory(selector)
        wait_for = getattr(locator, "wait_for", None)
        if callable(wait_for):
            try:
                await wait_for(state="visible", timeout=10_000)
            except Exception as exc:
                raise ValueError("Страница резюме не готова к извлечению print-данных") from exc


async def _visible_selector(page: Any, selector: str) -> bool:
    locator = page.locator(selector)
    count = await locator.count()
    for index in range(count):
        item = locator.nth(index) if hasattr(locator, "nth") else locator
        visible = getattr(item, "is_visible", None)
        if visible is None or await visible():
            return True
    return False


async def _require_print_layout(page: Any, ref: ResumeRef) -> None:
    """Require the public print DOM; owner/account cards are never a fallback."""
    if not await _visible_selector(page, "body.bloko-print"):
        raise ValueError("Страница резюме не содержит print-разметку")
    if not await _visible_selector(
        page,
        "[data-qa='resume-main-info__content-wrapper'], "
        "[data-testid='resume-main-info__content-wrapper'], .resume-main-info__content-wrapper",
    ):
        raise ValueError("Страница резюме не содержит публичный print-контейнер")
    if await _visible_selector(
        page,
        "[data-qa='profile-experience-company-card'], .profile-experience-company-card",
    ):
        raise ValueError("Обнаружена regular owner-разметка резюме")
    section_found = False
    for selector in (
        "[data-qa='resume-position'], [data-testid='resume-position'], .resume-position",
        "[data-qa='resume-experience-block'], [data-testid='resume-experience-block'], .resume-experience-block",
        "[data-qa='resume-specializations'], [data-testid='resume-specializations'], .resume-specializations",
    ):
        if await _visible_selector(page, selector):
            section_found = True
            break
    if not section_found:
        raise ValueError("Страница резюме не содержит print-разделов")


class ResumeImportMixin:
    """Opt-in methods delegated to each site's independent extractor."""

    resume_policy: ResumeURLPolicy
    resume_extractor: Any
    max_resume_response_bytes = 8 * 1024 * 1024

    def validate_resume_url(self, url: str) -> ResumeRef:
        return self.resume_policy.validate(url)

    async def list_resume_refs(self, page: Any) -> list[ResumeRef]:
        return await self.resume_extractor.list_resume_refs(page, self.resume_policy)

    async def open_resume(self, page: Any, ref: ResumeRef) -> None:
        await open_resume_page(page, ref, self.resume_policy)

    async def _require_print_layout(self, page: Any, ref: ResumeRef) -> None:
        if not self.resume_policy.requires_print:
            return
        await _require_print_layout(page, ref)

    async def extract_resume(self, page: Any, ref: ResumeRef) -> SiteResumeSnapshot:
        if ref.source_site != self.site_id:
            raise ValueError("Резюме принадлежит другому сайту")
        await self._require_print_layout(page, ref)
        # A public URL may resolve with HTTP 200 while rendering a removed or
        # not-found page. Use exact error UI hooks and critical resume roots;
        # never scan arbitrary resume prose for words such as "недоступно".
        # Mock pages without a locator are supported.
        locator_factory = getattr(page, "locator", None)
        if locator_factory is not None:
            error_selectors = (
                "[data-qa='resume-not-found'], [data-testid='resume-not-found'], "
                ".resume-not-found, .resume-error, [data-qa='error-page'], "
                "[data-testid='error-page']",
            )
            error_found = False
            for selector in error_selectors:
                if await locator_factory(selector).count():
                    error_found = True
                    break
            if error_found:
                raise ValueError("Страница резюме недоступна или снята с публикации")
            critical_roots = getattr(
                self.resume_extractor,
                "critical_selectors",
                (".resume-public-name", ".resume-public-position"),
            )
            critical_found = False
            for selector in critical_roots:
                if await locator_factory(selector).count():
                    critical_found = True
                    break
            if not critical_found:
                raise ValueError("Страница резюме недоступна или снята с публикации")
        snapshot = await self.resume_extractor.extract(page, ref, self.resume_policy)
        title = snapshot.target.desired_title
        if title.availability is not FieldAvailability.PRESENT or not str(title.value or "").strip():
            raise ValueError("Страница резюме не содержит целевой роли")
        if not (
            snapshot.experience or snapshot.skills or snapshot.projects
            or snapshot.education or snapshot.languages or snapshot.courses
            or snapshot.certifications or snapshot.awards or snapshot.portfolio
            or snapshot.additional_sections
            or snapshot.about.availability is FieldAvailability.PRESENT
        ):
            raise ValueError("Страница резюме не содержит профессиональных разделов")
        return snapshot
