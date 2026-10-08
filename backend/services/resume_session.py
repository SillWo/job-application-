"""Session-scoped resume import, privacy views, and safe local rendering.

This module is deliberately independent of site navigation.  Adapters may
provide a ``resume_import`` capability; this service owns token lifetime,
normalization, persistence, and the boundary between model and private data.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import importlib
import json
import re
import secrets
import sys
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlparse, urlunparse

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from backend.adapters import adapter_registry
from backend.adapters.base.resume_import import (
    coverage_for,
    open_resume_page,
)
from backend.adapters.base.resume_import import (
    snapshot_hash as semantic_snapshot_hash,
)
from backend.persistence.crypto import encrypt_secret
from backend.persistence.models import (
    JobSession,
    ResumePreviewToken,
    SavedResumeSource,
    SessionResumeSnapshot,
)
from backend.schemas.domain import (
    FieldAvailability,
    ResumeContacts,
    ResumeIdentity,
    ResumePrivateView,
    ResumeProfessionalView,
    SiteResumeSnapshot,
    SourceField,
    utcnow,
)
from backend.services.private_text import (
    _CONTROL,
    _DIRECT_URL,
    _EMAIL,
    _PHONE,
    ResumeImportError,
    _unseal_private,
)
from backend.services.private_text import (
    _flat_private as _flat_private,
)
from backend.services.private_text import (
    redact_private_text as redact_private_text,
)
from backend.services.private_text import (
    render_local_private as render_local_private,
)
from backend.services.private_text import (
    render_private_placeholders as render_private_placeholders,
)

PREVIEW_TTL = timedelta(minutes=10)
SNAPSHOT_TTL = timedelta(hours=24)
SAVED_SOURCE_VALID = "valid"
SAVED_SOURCE_CHANGED = "changed"
SAVED_SOURCE_UNAVAILABLE = "unavailable"
RESUME_EXTRACT_DEADLINE_SECONDS = 180
RESUME_CLOSE_DEADLINE_SECONDS = 15
_HTML_BLOCK = re.compile(r"(?is)<(script|style)\b[^>]*>.*?</\1\s*>")
_HTML_TAG = re.compile(r"(?is)</?[a-z][^>]*>")
_MESSENGER_URL = re.compile(
    r"(?i)https?://(?:t\.me|telegram\.me|wa\.me|whatsapp\.com|vk\.com)/[^\s<>]+"
)
class ResumeImportUnavailable(ResumeImportError):
    pass


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _seal_private(value: dict) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    try:
        return "dpapi:" + encrypt_secret(payload)
    except (RuntimeError, OSError) as exc:
        # Windows must fail closed: an unavailable DPAPI is not permission to
        # persist a base64-encoded copy of private candidate data.
        if sys.platform == "win32":
            raise ResumeImportError("Не удалось защитить приватные данные снимка") from exc
        # Non-Windows test/development hosts get an explicit test-only
        # envelope; deployments using real candidate data must run on Windows.
        return "sealed-test:" + base64.urlsafe_b64encode(payload.encode()).decode()


def _utc(value: datetime) -> datetime:
    """Normalize SQLite's timezone-naive round-trip to UTC for comparisons."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def prune_expired_preview_tokens(db: Session) -> int:
    """Remove expired/consumed preview payloads and return deleted count.

    Preview rows contain encrypted private data and are therefore deleted as
    soon as an import path touches the token store, not merely rejected at
    validation time.  The caller owns the transaction.
    """
    cutoff = datetime.now(timezone.utc)
    result = db.execute(
        delete(ResumePreviewToken)
        .where(
            (ResumePreviewToken.expires_at <= cutoff)
            | ResumePreviewToken.consumed_at.is_not(None)
        )
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


def prune_expired_session_snapshots(db: Session) -> int:
    """Delete only abandoned CREATED-session snapshots past their grace TTL."""
    cutoff = datetime.now(timezone.utc)
    created_ids = select(JobSession.id).where(JobSession.status == "CREATED")
    result = db.execute(
        delete(SessionResumeSnapshot)
        .where(
            SessionResumeSnapshot.expires_at.is_not(None),
            SessionResumeSnapshot.expires_at <= cutoff,
            SessionResumeSnapshot.session_id.in_(created_ids),
        )
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


def canonical_resume_url(adapter_id: str, url: str) -> str:
    """Apply transport-safe URL normalization.

    The resume path and host are deliberately *not* hardcoded here.  Each
    adapter owns that policy (including regional hosts and opaque IDs).  This
    helper is retained for callers that need a safe generic URL copy; the
    authoritative validation is performed by :func:`validate_adapter_resume_url`.
    """
    value = str(url or "").strip()
    # The site policy is the single source of truth for accepted tracking and
    # print parameters. It returns the queryless identity URL while retaining
    # the import URL on the ResumeRef.
    module = _site_resume_module(adapter_id)
    policy = getattr(module, "POLICY", None) if module is not None else None
    if policy is not None:
        try:
            return policy.validate(value).url
        except ValueError as exc:
            raise ResumeImportError("Ссылка не прошла проверку выбранного сайта") from exc
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ResumeImportError("Ссылка содержит недопустимый порт") from exc
    if parsed.scheme != "https" or parsed.username or parsed.password or port not in (None, 443):
        raise ResumeImportError("Ссылка должна использовать HTTPS без учётных данных и порта")
    host = (parsed.hostname or "").casefold().rstrip(".")
    if not host or not parsed.path or not parsed.path.startswith("/"):
        raise ResumeImportError("Укажите прямую ссылку на резюме")
    if any(ord(char) < 0x20 for char in parsed.path):
        raise ResumeImportError("Ссылка содержит недопустимые символы")
    # Fragments and well-known tracking parameters never reach the browser;
    # unknown query keys are rejected rather than treated as redirect state.
    if re.search(r"%(?![0-9A-Fa-f]{2})", parsed.query):
        raise ResumeImportError("Ссылка содержит некорректное URL-кодирование")
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    query_keys = {key.casefold() for key, _ in query_pairs}
    if any(not key.startswith("utm_") and key not in {"from", "hhtmfrom", "print"} for key in query_keys):
        raise ResumeImportError("Ссылка содержит недопустимые параметры")
    print_values = [value for key, value in query_pairs if key == "print"]
    if len(print_values) > 1 or (print_values and print_values[0] != "true"):
        raise ResumeImportError("Параметр print в ссылке на резюме недействителен")
    return urlunparse(("https", host, parsed.path.rstrip("/"), "", "", ""))


def _source_field(value: Any, *, section: str | None = None) -> SourceField:
    if isinstance(value, SourceField):
        return value
    if isinstance(value, dict) and "availability" in value:
        return SourceField.model_validate(value)
    if value is None:
        return SourceField(availability=FieldAvailability.NOT_PROVIDED, source_section=section)
    return SourceField(value=value, availability=FieldAvailability.PRESENT, source_section=section)


def _as_list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _normalize_extracted(
    extracted: Any, *, adapter_id: str, source_url: str, source_ref: Any = None
) -> SiteResumeSnapshot:
    try:
        raw = extracted.model_dump(mode="json") if hasattr(extracted, "model_dump") else dict(extracted or {})
    except Exception as exc:
        raise ResumeImportError("Сайт вернул некорректные данные резюме") from exc
    if not isinstance(raw, dict):
        raise ResumeImportError("Сайт вернул некорректные данные резюме")
    if "snapshot" in raw and isinstance(raw["snapshot"], dict):
        raw = raw["snapshot"]
    # An extractor may return a pre-built canonical model.  It is still
    # untrusted adapter output: do not let it spoof the selected site, URL or
    # resume reference, and never trust its supplied content hash.
    supplied_site = raw.get("source_site")
    if supplied_site is not None and str(supplied_site) != adapter_id:
        raise ResumeImportError("Сайт снимка резюме не совпадает с выбранным адаптером")
    expected_url_hash = _sha256(source_url)
    supplied_url_hash = raw.get("source_url_hash")
    if supplied_url_hash is not None and str(supplied_url_hash) != expected_url_hash:
        raise ResumeImportError("Ссылка снимка резюме не совпадает с проверенной ссылкой")
    ref_id = str(
        raw.get("source_resume_id")
        or raw.get("external_id")
        or getattr(source_ref, "external_id", None)
        or getattr(source_ref, "id", None)
        or "imported"
    ).strip()
    if not ref_id:
        raise ResumeImportError("Сайт не вернул идентификатор резюме")
    expected_ref_id = (
        source_ref.get("external_id") if isinstance(source_ref, dict)
        else getattr(source_ref, "external_id", None)
    )
    if expected_ref_id is None:
        path_parts = [part for part in urlparse(source_url).path.split("/") if part]
        if len(path_parts) >= 2 and path_parts[-2].casefold() == "resume":
            expected_ref_id = path_parts[-1]
    if expected_ref_id is not None and str(expected_ref_id) != ref_id:
        raise ResumeImportError("Идентификатор снимка резюме не совпадает с выбранным резюме")

    # Accept the compact extractor contract as well as the expanded schema.
    def mapping(name: str) -> dict:
        value = raw.get(name)
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ResumeImportError("Сайт вернул некорректные данные резюме")
        return dict(value)

    target = mapping("target")
    for key in ("desired_title", "specializations", "grade", "desired_salary", "employment_types", "work_formats"):
        if key not in target and key in raw:
            target[key] = raw[key]
    location = mapping("location")
    for key in ("residence", "relocation", "business_trips", "citizenship", "work_permit", "commute_time"):
        if key not in location and key in raw:
            location[key] = raw[key]
    identity = mapping("identity")
    contacts = mapping("contacts")
    if "full_name" in raw and "full_name" not in identity:
        identity["full_name"] = raw["full_name"]
    for key in ("gender", "age", "birth_date", "has_photo", "photo_url"):
        if key in raw and key not in identity:
            identity[key] = raw[key]
    for key in ("phone", "email", "messengers", "links", "preferred_contact", "contact_comment"):
        if key in raw and key not in contacts:
            contacts[key] = raw[key]

    for group in (target, location, identity, contacts):
        for key, value in list(group.items()):
            group[key] = _source_field(value)
    payload = {
        "schema_version": 2,
        "extractor_version": str(raw.get("extractor_version") or "site-resume-v1"),
        "source_site": adapter_id,
        "source_resume_id": ref_id,
        "source_url_hash": expected_url_hash,
        "content_hash": "0" * 64,
        "source_updated_at": raw.get("source_updated_at"),
        "source_updated_text": _source_field(raw.get("source_updated_text"), section="metadata"),
        "imported_at": raw.get("imported_at") or utcnow(),
        "identity": identity,
        "contacts": contacts,
        "self_employment": _source_field(raw.get("self_employment"), section="additional_info"),
        "job_search_status": _source_field(raw.get("job_search_status"), section="metadata"),
        "source_badges": _source_field(raw.get("source_badges"), section="metadata"),
        "target": target,
        "location": location,
        "total_experience": _source_field(raw.get("total_experience"), section="experience"),
        "experience": _as_list(raw.get("experience", raw.get("experiences"))),
        "projects": _as_list(raw.get("projects")),
        "skills": _as_list(raw.get("skills")),
        "education": _as_list(raw.get("education")),
        "languages": _as_list(raw.get("languages")),
        "courses": _as_list(raw.get("courses")),
        "certifications": _as_list(raw.get("certifications")),
        "awards": _as_list(raw.get("awards")),
        "portfolio": _as_list(raw.get("portfolio")),
        "about": _source_field(raw.get("about"), section="about"),
        "additional_sections": _as_list(raw.get("additional_sections")),
        "coverage": raw.get("coverage") or {},
    }
    try:
        snapshot = SiteResumeSnapshot.model_validate(payload)
    except Exception as exc:
        raise ResumeImportError("Сайт вернул неполные или несовместимые данные резюме") from exc
    title = snapshot.target.desired_title
    if title.availability != FieldAvailability.PRESENT or not str(title.value or "").strip():
        raise ResumeImportError("Сайт не вернул целевую роль резюме")
    meaningful = (
        bool(snapshot.experience or snapshot.skills or snapshot.projects
             or snapshot.education or snapshot.languages or snapshot.courses
             or snapshot.certifications or snapshot.awards or snapshot.portfolio
             or snapshot.additional_sections)
        or snapshot.about.availability == FieldAvailability.PRESENT
    )
    if not meaningful:
        raise ResumeImportError("Сайт не вернул профессиональные разделы резюме")
    # A parser error is never a successful normalized import. Unsupported
    # fields are acceptable only when they are genuinely absent (no value was
    # captured); owner-layout fallbacks with data are rejected by the print DOM
    # gate and cannot be counted as complete imports.
    for field in _iter_source_fields(snapshot):
        if field.availability is FieldAvailability.PARSE_ERROR:
            raise ResumeImportError("Сайт вернул ошибку разбора данных резюме")
        if field.availability is FieldAvailability.UNSUPPORTED and field.value not in (None, "", []):
            raise ResumeImportError("Сайт вернул неподдержанные данные резюме")
    if snapshot.coverage.parse_errors:
        raise ResumeImportError("Сайт вернул ошибку разбора данных резюме")
    for path in snapshot.coverage.unsupported_fields:
        field = _source_field_at_path(snapshot, path)
        if field is not None and field.value not in (None, "", []):
            raise ResumeImportError("Сайт вернул неподдержанные данные резюме")
    if snapshot.coverage.hidden_fields:
        # Hidden DOM content is not a complete normalized snapshot. Reject it
        # before it can be persisted or used by the workflow.
        raise ResumeImportError("РЎРЅРёРјРѕРє СЂРµР·СЋРјРµ СЃРѕРґРµСЂР¶РёС‚ hidden_fields")
    # Import timestamps describe when this copy was read, not its contents.
    # Excluding them makes a revalidation of an unchanged page deterministic.
    digest = _snapshot_hash(snapshot)
    return snapshot.model_copy(update={"content_hash": digest})


def _snapshot_hash(snapshot: SiteResumeSnapshot) -> str:
    return semantic_snapshot_hash(snapshot)


def _iter_source_fields(value: Any):
    if isinstance(value, SourceField):
        yield value
        return
    if hasattr(value, "model_dump"):
        yield from _iter_source_fields(value.model_dump(mode="python"))
        return
    if isinstance(value, dict):
        if "availability" in value and "value" in value:
            try:
                yield SourceField.model_validate(value)
            except Exception:
                return
            return
        for child in value.values():
            yield from _iter_source_fields(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_source_fields(child)


def _source_field_at_path(snapshot: SiteResumeSnapshot, path: str) -> SourceField | None:
    """Resolve coverage paths such as ``experience[0].company`` safely."""
    node: Any = snapshot
    for name, index in re.findall(r"([A-Za-z_]\w*)(?:\[(\d+)\])?", path):
        if hasattr(node, name):
            node = getattr(node, name)
        elif isinstance(node, dict):
            node = node.get(name)
        else:
            return None
        if index:
            if not isinstance(node, list) or int(index) >= len(node):
                return None
            node = node[int(index)]
    return node if isinstance(node, SourceField) else None


def professional_view(snapshot: SiteResumeSnapshot | dict) -> ResumeProfessionalView:
    item = snapshot if isinstance(snapshot, SiteResumeSnapshot) else SiteResumeSnapshot.model_validate(snapshot)
    view = ResumeProfessionalView(
        target=item.target, self_employment=item.self_employment,
        job_search_status=item.job_search_status, source_badges=item.source_badges,
        location=item.location, experience=item.experience,
        projects=item.projects, skills=item.skills, education=item.education,
        languages=item.languages, courses=item.courses, certifications=item.certifications,
        awards=item.awards, portfolio=item.portfolio, about=item.about,
        additional_sections=item.additional_sections, total_experience=item.total_experience,
    )
    # Availability is useful to the model, while CSS/semantic extraction
    # provenance is an implementation detail and must not cross the boundary.
    data = view.model_dump(mode="json")
    def strip_internal(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: strip_internal(item) for key, item in value.items()
                    if key not in {"source_locator", "source_section"}}
        if isinstance(value, list):
            return [strip_internal(item) for item in value]
        return value
    data = strip_internal(data)
    private = private_view(item).model_dump(mode="json")
    private_values: list[str] = []

    def collect_private(value: Any) -> None:
        if isinstance(value, dict):
            if "value" in value and "availability" in value:
                raw = value.get("value")
                if isinstance(raw, str) and value.get("availability") == "present":
                    private_values.append(raw.strip())
                elif isinstance(raw, list):
                    private_values.extend(
                        str(item).strip() for item in raw if str(item).strip()
                    )
            else:
                for child in value.values():
                    collect_private(child)
        elif isinstance(value, list):
            for child in value:
                collect_private(child)

    collect_private(private)
    exact = {value.casefold() for value in private_values if len(value) >= 2}
    # A full name can be repeated with punctuation or embedded in a sentence;
    # redact sufficiently distinctive components too, while avoiding ordinary
    # short words becoming blanket replacements.
    full_name = item.identity.full_name.value
    if item.identity.full_name.availability == FieldAvailability.PRESENT and isinstance(full_name, str):
        exact.update(token.casefold() for token in re.findall(r"[\wА-Яа-яЁё-]+", full_name)
                    if len(token) >= 4)

    def redact(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: redact(child) for key, child in value.items()}
        if isinstance(value, list):
            return [redact(child) for child in value]
        if not isinstance(value, str):
            return value
        result = value
        if result.casefold() in exact:
            return "[private value omitted]"
        for secret in sorted(exact, key=len, reverse=True):
            result = re.sub(re.escape(secret), "[private value omitted]", result, flags=re.I)
        result = _EMAIL.sub("[email omitted]", result)
        result = _PHONE.sub("[phone omitted]", result)
        return _DIRECT_URL.sub("[link omitted]", result)

    return ResumeProfessionalView.model_validate(redact(data))


def _redacted_snapshot(snapshot: SiteResumeSnapshot) -> tuple[SiteResumeSnapshot, ResumeProfessionalView]:
    """Build the persisted privacy-safe snapshot and hash that exact copy."""
    view = professional_view(snapshot)
    redacted = snapshot.model_copy(
        update={
            "identity": ResumeIdentity(),
            "contacts": ResumeContacts(),
            "self_employment": view.self_employment,
            "job_search_status": view.job_search_status,
            "source_badges": view.source_badges,
            "target": view.target,
            "location": view.location,
            "experience": view.experience,
            "projects": view.projects,
            "skills": view.skills,
            "education": view.education,
            "languages": view.languages,
            "courses": view.courses,
            "certifications": view.certifications,
            "awards": view.awards,
            "portfolio": view.portfolio,
            "about": view.about,
            "additional_sections": view.additional_sections,
            "total_experience": view.total_experience,
            "content_hash": "0" * 64,
        }
    )
    return redacted.model_copy(update={"content_hash": _snapshot_hash(redacted)}), view


def private_view(snapshot: SiteResumeSnapshot | dict) -> ResumePrivateView:
    item = snapshot if isinstance(snapshot, SiteResumeSnapshot) else SiteResumeSnapshot.model_validate(snapshot)
    return ResumePrivateView(identity=item.identity, contacts=item.contacts)


def professional_model_payload(snapshot_or_view: SiteResumeSnapshot | ResumeProfessionalView | dict) -> dict:
    """Serialize professional data as plain semantic values for model consumers."""
    view = professional_view(snapshot_or_view) if isinstance(snapshot_or_view, SiteResumeSnapshot) else snapshot_or_view
    data = view.model_dump(mode="json") if hasattr(view, "model_dump") else dict(view or {})

    def semantic_values(value: Any) -> Any:
        if isinstance(value, dict):
            # SourceField is the only wrapper in this schema. Requiring the
            # wrapper's metadata shape avoids mistaking an ordinary semantic
            # object that happens to contain a `value` member for a field.
            if (
                "value" in value
                and "availability" in value
                and set(value).issubset({"value", "availability", "source_section", "source_locator"})
            ):
                return semantic_values(value.get("value"))
            return {
                key: semantic_values(item)
                for key, item in value.items()
                if key not in {"availability", "source_section", "source_locator"}
            }
        if isinstance(value, list):
            return [semantic_values(item) for item in value]
        return value

    flattened = semantic_values(data)
    # The normalized contract uses explicit ``target``/``experience`` blocks,
    # while existing intelligence consumers accept the legacy compact names.
    # Keep both views professional-only so the migration cannot silently drop
    # role, salary, work-format, or experience data.
    if isinstance(flattened, dict):
        target = flattened.get("target")
        if isinstance(target, dict):
            for key in (
                "desired_title", "specializations", "grade", "desired_salary",
                "employment_types", "work_formats",
            ):
                if key in target:
                    flattened.setdefault(key, target[key])
        if "experience" in flattened:
            flattened.setdefault("experiences", flattened["experience"])
    return flattened


def full_resume_model_payload(value: SiteResumeSnapshot | dict) -> dict:
    """Serialize the complete normalized resume for model consumers.

    ``professional_model_payload`` intentionally omits identity and contact
    fields for the old privacy boundary.  A session started from an explicitly
    imported resume has a different contract: the local workflow may use the
    complete normalized source, including those fields.  Keep this function
    deliberately allowlisted so adapter metadata, bearer identifiers,
    locators, tokens, and raw page markup cannot cross the model boundary.
    """
    if isinstance(value, SiteResumeSnapshot):
        item = value
    else:
        raw = value.get("full_snapshot") if isinstance(value, dict) and isinstance(value.get("full_snapshot"), dict) else value
        item = SiteResumeSnapshot.model_validate(raw)

    fields = (
        "identity", "contacts", "self_employment", "job_search_status", "source_badges",
        "target", "location", "experience", "skills",
        "education", "projects", "languages", "courses", "certifications",
        "awards", "portfolio", "about", "additional_sections", "total_experience",
    )
    data = item.model_dump(mode="json", include=set(fields))

    def semantic_values(node: Any) -> Any:
        if isinstance(node, dict):
            if (
                "value" in node
                and "availability" in node
                and set(node).issubset({"value", "availability", "source_section", "source_locator"})
            ):
                return semantic_values(node.get("value"))
            return {
                key: semantic_values(child)
                for key, child in node.items()
                if key not in {"availability", "source_locator", "source_section"}
            }
        if isinstance(node, list):
            return [semantic_values(child) for child in node]
        if isinstance(node, str):
            # Adapter contracts are text-oriented, but a site can still
            # accidentally pass markup from a rich-text section.  Keep the
            # model boundary plain text and discard executable/style blocks.
            return _CONTROL.sub("", html.unescape(_HTML_TAG.sub("", _HTML_BLOCK.sub("", node))))
        return node

    return semantic_values(data)


def public_preview(snapshot: SiteResumeSnapshot, *, source_url: str | None = None) -> dict[str, Any]:
    """Return safe preview metadata; values of identity/contact fields stay local."""
    questions: list[dict[str, Any]] = []
    gender = snapshot.identity.gender
    if gender.availability != FieldAvailability.PRESENT or gender.value not in {"male", "female"}:
        questions.append({
            "id": "grammatical_gender",
            "question": "Какой род использовать в сопроводительных письмах?",
            "options": ["male", "female"],
            "required": True,
            "session_only": True,
        })
    result = {
        "source_site": snapshot.source_site,
        "target_title": snapshot.target.desired_title.value,
        "source_updated_at": snapshot.source_updated_at.isoformat() if snapshot.source_updated_at else None,
        # Only an explicitly provided, schema-valid value is exposed.  Gender
        # is never inferred from a name or other resume text.
        "grammatical_gender": gender.value if gender.value in {"male", "female"} else None,
        "grammatical_gender_source": "resume" if gender.value in {"male", "female"} else None,
        "sections": snapshot.coverage.present_sections,
        "coverage": snapshot.coverage.model_dump(mode="json"),
        "private_fields_found": {
            "full_name": snapshot.identity.full_name.availability == FieldAvailability.PRESENT,
            "phone": snapshot.contacts.phone.availability == FieldAvailability.PRESENT,
            "email": snapshot.contacts.email.availability == FieldAvailability.PRESENT,
        },
        "questions": questions,
    }
    if source_url is not None:
        result["import_url"] = _safe_import_url(snapshot.source_site, source_url)
    return result


def _safe_import_url(adapter_id: str, source_url: str | None) -> str | None:
    if not source_url:
        return None
    module = _site_resume_module(adapter_id)
    policy = getattr(module, "POLICY", None) if module is not None else None
    if policy is None:
        return None
    try:
        return policy.validate(source_url).import_url
    except (TypeError, ValueError):
        return None


def _saved_source_preview(
    snapshot: SiteResumeSnapshot, *, gender_known: bool | None = None,
    private_fields_found: dict[str, bool] | None = None,
    source_url: str | None = None,
) -> dict[str, Any]:
    """Return metadata safe for durable storage and saved-source responses.

    ``public_preview`` is intentionally useful during the launch flow and may
    contain a session-only question (including its answer options).  Saved
    sources must not retain an answer value or gender, so strip only those
    values at this separate persistence boundary.  Generic question metadata
    remains useful for asking the user again after a restart.
    """
    result = dict(public_preview(snapshot, source_url=source_url))
    result["import_url"] = _safe_import_url(snapshot.source_site, source_url)

    # Extractors are untrusted.  A malformed title/section can repeat the
    # external resume id even though the normal preview shape does not expose
    # ``source_resume_id`` itself.  Keep that bearer-adjacent identifier out of
    # the durable metadata and its API representation as a final boundary.
    external_id = snapshot.source_resume_id.strip()

    def scrub(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: scrub(child) for key, child in value.items()}
        if isinstance(value, list):
            return [scrub(child) for child in value]
        if isinstance(value, str) and external_id:
            return re.sub(re.escape(external_id), "[identifier omitted]", value, flags=re.IGNORECASE)
        return value

    result = scrub(result)
    result.pop("grammatical_gender", None)
    result.pop("grammatical_gender_source", None)
    if gender_known is True:
        result.pop("questions", None)
    if private_fields_found is not None:
        result["private_fields_found"] = {
            key: bool(private_fields_found.get(key, False))
            for key in ("full_name", "phone", "email")
        }
    if not result.get("questions"):
        result.pop("questions", None)
    return result


def _private_gender_is_known(private_payload: str) -> bool:
    """Return only whether a sealed preview had an explicit gender value."""
    return _private_gender_value(private_payload) is not None


def _private_gender_value(private_payload: str) -> str | None:
    """Return a validated gender enum from a sealed preview, if present."""
    private = _unseal_private(private_payload)
    identity = private.get("identity")
    gender = identity.get("gender") if isinstance(identity, dict) else None
    value = gender.get("value") if isinstance(gender, dict) else None
    availability = gender.get("availability") if isinstance(gender, dict) else None
    return value if availability == FieldAvailability.PRESENT.value and value in {"male", "female"} else None


def _private_fields_found(private_payload: str) -> dict[str, bool]:
    private = _unseal_private(private_payload)
    identity = private.get("identity") if isinstance(private.get("identity"), dict) else {}
    contacts = private.get("contacts") if isinstance(private.get("contacts"), dict) else {}

    def present(group: dict, key: str) -> bool:
        value = group.get(key)
        return isinstance(value, dict) and value.get("availability") == FieldAvailability.PRESENT.value

    return {
        "full_name": present(identity, "full_name"),
        "phone": present(contacts, "phone"),
        "email": present(contacts, "email"),
    }


def _snapshot_gender_is_known(snapshot: SiteResumeSnapshot) -> bool:
    gender = snapshot.identity.gender
    return gender.availability == FieldAvailability.PRESENT and gender.value in {"male", "female"}


def _apply_saved_gender(
    snapshot: SiteResumeSnapshot, grammatical_gender: str | None
) -> SiteResumeSnapshot:
    """Make a saved-source gender preference authoritative in every view."""
    if grammatical_gender not in {"male", "female"}:
        return snapshot
    data = snapshot.model_dump(mode="json")
    data.setdefault("identity", {})["gender"] = {
        "value": grammatical_gender,
        "availability": FieldAvailability.PRESENT.value,
        "source_section": "saved_source",
    }
    return SiteResumeSnapshot.model_validate(data)


_SAVED_RESUME_DATA_ERROR = "Обновите данные резюме во вкладке «Профиль»"
_MAIN_RESUME_SECTIONS = frozenset({
    "identity", "contacts", "target", "location", "experience", "skills",
    "education", "languages", "about", "total_experience",
})


def uses_saved_resume_data(adapter_id: str) -> bool:
    """Whether this site uses an explicitly saved local resume snapshot."""
    return adapter_id in {"hh", "zarplata", "hirehi"}


def _validated_saved_snapshot(row: SavedResumeSource) -> SiteResumeSnapshot:
    """Validate a locally protected snapshot and all durable row bindings."""
    payload = getattr(row, "resume_snapshot_payload", None)
    if not payload:
        raise ResumeImportError(_SAVED_RESUME_DATA_ERROR)
    try:
        raw = _unseal_private(payload)
        snapshot = SiteResumeSnapshot.model_validate(raw)
        if snapshot.content_hash != _snapshot_hash(snapshot):
            raise ValueError("snapshot hash mismatch")
        if snapshot.source_site != row.adapter_id:
            raise ValueError("snapshot site mismatch")
        if row.grammatical_gender in {"male", "female"}:
            saved_gender = snapshot.identity.gender
            if (
                saved_gender.availability is not FieldAvailability.PRESENT
                or saved_gender.value != row.grammatical_gender
            ):
                raise ValueError("snapshot gender does not match saved preference")
        source_url = _saved_source_url(row)
        if not source_url or _sha256(source_url) != row.source_url_hash:
            raise ValueError("saved URL hash mismatch")
        if snapshot.source_url_hash != row.source_url_hash:
            raise ValueError("snapshot URL binding mismatch")
        if _sha256(snapshot.source_resume_id) != row.resume_id_hash:
            raise ValueError("snapshot resume binding mismatch")
        if snapshot.coverage.parse_errors or snapshot.coverage.hidden_fields:
            raise ValueError("snapshot coverage is incomplete")
        for field in _iter_source_fields(snapshot):
            if field.availability in {FieldAvailability.HIDDEN, FieldAvailability.PARSE_ERROR}:
                raise ValueError("snapshot field is hidden or failed to parse")
            if field.availability is FieldAvailability.UNSUPPORTED and field.value not in (None, "", []):
                raise ValueError("snapshot has unsupported data")
        for path in snapshot.coverage.unsupported_fields:
            field = _source_field_at_path(snapshot, path)
            if field is not None and field.value not in (None, "", []):
                raise ValueError("snapshot coverage has unsupported data")
        title = snapshot.target.desired_title
        if title.availability is not FieldAvailability.PRESENT or not str(title.value or "").strip():
            raise ValueError("snapshot target role is missing")
        meaningful = bool(
            snapshot.experience or snapshot.skills or snapshot.projects or snapshot.education
            or snapshot.languages or snapshot.courses or snapshot.certifications
            or snapshot.awards or snapshot.portfolio or snapshot.additional_sections
        ) or snapshot.about.availability is FieldAvailability.PRESENT
        if not meaningful:
            raise ValueError("snapshot professional sections are missing")
        public_snapshot, _ = _redacted_snapshot(snapshot)
        if public_snapshot.content_hash != row.content_hash:
            raise ValueError("saved projection hash mismatch")
        return snapshot
    except ResumeImportError as exc:
        # Preserve the actionable contract without exposing decryption/schema
        # details or any value from the resume.
        if _SAVED_RESUME_DATA_ERROR in str(exc):
            raise
        raise ResumeImportError(_SAVED_RESUME_DATA_ERROR) from exc
    except Exception as exc:
        raise ResumeImportError(_SAVED_RESUME_DATA_ERROR) from exc


def _saved_snapshot_completion(snapshot: SiteResumeSnapshot) -> str:
    """Classify main-section coverage from the full validated snapshot."""
    # Coverage stored by older extractors may be stale or omit newer sections.
    # Recompute it on a copy, using the canonical schema's field availability.
    measured = snapshot.model_copy(deep=True)
    coverage_for(measured)
    present = set(measured.coverage.present_sections)
    return "complete" if present >= _MAIN_RESUME_SECTIONS else "partial"


def load_saved_resume_data(row: SavedResumeSource) -> SiteResumeSnapshot:
    """Load a complete saved resume snapshot using local data only."""
    if not uses_saved_resume_data(row.adapter_id):
        raise ResumeImportError(_SAVED_RESUME_DATA_ERROR)
    return _validated_saved_snapshot(row)


def _store_saved_snapshot(
    row: SavedResumeSource,
    snapshot: SiteResumeSnapshot,
    *,
    update_saved_at: bool = True,
) -> None:
    """Atomically prepare the full sealed copy and its redacted projection."""
    snapshot = _apply_saved_gender(snapshot, row.grammatical_gender)
    snapshot = snapshot.model_copy(update={"content_hash": _snapshot_hash(snapshot)})
    public_snapshot, _ = _redacted_snapshot(snapshot)
    row.resume_snapshot_payload = _seal_private(snapshot.model_dump(mode="json"))
    if update_saved_at:
        row.resume_data_saved_at = datetime.now(timezone.utc)
    row.content_hash = public_snapshot.content_hash


def _snapshot_fields_found(snapshot: SiteResumeSnapshot) -> dict[str, bool]:
    return {
        "full_name": snapshot.identity.full_name.availability == FieldAvailability.PRESENT,
        "phone": snapshot.contacts.phone.availability == FieldAvailability.PRESENT,
        "email": snapshot.contacts.email.availability == FieldAvailability.PRESENT,
    }


def _capability(adapter: Any) -> Any:
    return getattr(adapter, "resume_import", adapter)


def _site_resume_module(adapter_id: str) -> Any | None:
    try:
        return importlib.import_module(f"backend.adapters.{adapter_id}.resume")
    except (ImportError, ModuleNotFoundError):
        return None


def validate_adapter_resume_url(adapter_id: str, url: str, adapter: Any = None) -> tuple[str, Any]:
    adapter = adapter or adapter_registry.get(adapter_id)
    # Generic checks run before site code and are intentionally limited to
    # transport/credential smuggling.  The adapter remains authoritative for
    # host, path and ID semantics.
    generic = canonical_resume_url(adapter_id, url)
    validator = getattr(_capability(adapter), "validate_resume_url", None)
    module = _site_resume_module(adapter_id)
    if validator is None and module is not None:
        validator = getattr(module, "validate_resume_url", None)
    if validator:
        try:
            ref = validator(generic)
        except (ValueError, TypeError) as exc:
            raise ResumeImportError("Ссылка не прошла проверку выбранного сайта") from exc
        canonical = str(getattr(ref, "url", None) or "").strip()
        if not canonical:
            raise ResumeImportError("Сайт не вернул проверенную ссылку на резюме")
        try:
            canonical = canonical_resume_url(adapter_id, canonical)
        except ResumeImportError:
            raise ResumeImportError("Сайт вернул небезопасную ссылку на резюме") from None
        ref_site = getattr(ref, "source_site", adapter_id)
        if ref_site != adapter_id:
            raise ResumeImportError("Сайт снимка резюме не совпадает с выбранным адаптером")
        return canonical, ref
    # A capability is optional for adapters that do not support resume import;
    # if one is supplied without a validator, fail closed rather than guessing
    # a path/ID contract in this shared service.
    raise ResumeImportUnavailable("Для выбранного сайта импорт резюме пока недоступен")


async def extract_resume(
    adapter_id: str, url: str, *, page: Any = None,
    validated: tuple[str, Any] | None = None,
) -> SiteResumeSnapshot:
    executor = None
    try:
        adapter = adapter_registry.get(adapter_id)
    except KeyError as exc:
        raise ResumeImportError("Неизвестный сайт вакансий") from exc
    canonical, ref = validated or validate_adapter_resume_url(adapter_id, url, adapter)
    capability = _capability(adapter)
    module = _site_resume_module(adapter_id)
    extractor = next(
        (getattr(capability, name, None) for name in ("extract_resume", "extract_resume_from_url", "import_resume") if getattr(capability, name, None)),
        None,
    )
    if extractor is None:
        extractor_object = getattr(module, "extractor", None) if module is not None else None
        policy = getattr(module, "POLICY", None) if module is not None else None
        if extractor_object is not None and policy is not None:
            async def module_extractor(browser_page, resume_ref):
                return await extractor_object.extract(browser_page, resume_ref, policy)
            extractor = module_extractor
        else:
            raise ResumeImportUnavailable("Для выбранного сайта импорт резюме пока недоступен")
    async def _read() -> Any:
        nonlocal page, executor
        # Site extractors may use a browser page (preferred) or implement a
        # complete URL-based flow for tests/controlled browser adapters.
        if page is None:
            # The browser layer owns Playwright lifecycle. No generic HTTP
            # client or hidden site endpoint is used for resume extraction.
            from backend.browser.executor import BrowserExecutor

            allowed_domains = set(getattr(adapter, "allowed_domains", ()))
            canonical_host = urlparse(canonical).hostname
            if canonical_host:
                # The host was already validated by the site policy. Include
                # that exact regional host for the browser route guard without
                # broadening navigation to arbitrary subdomains.
                allowed_domains.add(canonical_host)
            executor = BrowserExecutor(
                adapter_id,
                tuple(sorted(allowed_domains)),
                headless=True,
                navigation_hop_limit=8,
            )
            page = await executor.start()
        opener = getattr(capability, "open_resume", None)
        if opener is None and module is not None:
            policy = getattr(module, "POLICY", None)
            if policy is not None:
                async def module_opener(browser_page, resume_ref):
                    await open_resume_page(browser_page, resume_ref, policy)
                opener = module_opener
        if opener is not None:
            result = opener(page, ref)
            if hasattr(result, "__await__"):
                await result
        try:
            result = extractor(page, ref)
        except TypeError:
            # Controlled fake capabilities often expose a URL-only extractor.
            result = extractor(canonical)
        if hasattr(result, "__await__"):
            result = await result
        return result

    try:
        result = await asyncio.wait_for(_read(), timeout=RESUME_EXTRACT_DEADLINE_SECONDS)
    except TimeoutError as exc:
        raise ResumeImportError("Импорт резюме превысил общий лимит времени") from exc
    except ResumeImportError:
        raise
    except Exception as exc:
        raise ResumeImportError("Не удалось прочитать выбранное резюме") from exc
    finally:
        if executor is not None:
            with suppress(Exception):
                await asyncio.wait_for(executor.close(), timeout=RESUME_CLOSE_DEADLINE_SECONDS)
    return _normalize_extracted(result, adapter_id=adapter_id, source_url=canonical, source_ref=ref)


def issue_preview_token(
    db: Session,
    adapter_id: str,
    snapshot: SiteResumeSnapshot,
    *,
    source_url: str | None = None,
) -> str:
    prune_expired_preview_tokens(db)
    prune_expired_session_snapshots(db)
    token = secrets.token_urlsafe(32)
    # Keep PII only in the protected payload.  The persisted normalized copy
    # and its content hash describe the exact redacted data used by the
    # session, rather than a stale hash of the pre-redaction source.
    public_snapshot, safe_professional = _redacted_snapshot(snapshot)
    db.add(ResumePreviewToken(
        token_hash=_sha256(token), adapter_id=adapter_id,
        snapshot=public_snapshot.model_dump(mode="json"),
        professional_view=safe_professional.model_dump(mode="json"),
        full_snapshot=snapshot.model_dump(mode="json"),
        private_view=_seal_private(private_view(snapshot).model_dump(mode="json")),
        source_url=source_url,
        expires_at=datetime.now(timezone.utc) + PREVIEW_TTL,
    ))
    db.commit()
    return token


class SavedResumeSourceNotFound(ResumeImportError):
    """Raised when a requested durable source does not exist."""


def _saved_source_url(row: SavedResumeSource) -> str | None:
    """Return the canonical public URL from a durable source."""
    value = getattr(row, "source_url", None)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _preview_source_url(row: ResumePreviewToken) -> str | None:
    """Return the canonical public URL from a preview token."""
    value = getattr(row, "source_url", None)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _saved_source_token(
    db: Session, token: str, adapter_id: str
) -> tuple[ResumePreviewToken, SiteResumeSnapshot, str]:
    """Load and validate a preview token without consuming it.

    New preview rows carry the public URL directly. The returned URL is copied
    to the explicitly user-visible durable profile field after confirmation. Live
    page revalidation remains owned by SessionCreate/refresh; confirming a
    token must not consume or refresh it.
    """
    prune_expired_preview_tokens(db)
    if not token or len(token) > 256:
        raise ResumeImportError("Предпросмотр резюме истёк, проверьте ссылку ещё раз")
    item = db.scalar(
        select(ResumePreviewToken).where(ResumePreviewToken.token_hash == _sha256(token))
    )
    now = datetime.now(timezone.utc)
    if (
        item is None
        or item.adapter_id != adapter_id
        or item.consumed_at is not None
        or _utc(item.expires_at) <= now
        or not item.source_url
    ):
        raise ResumeImportError("Предпросмотр резюме истёк или уже использован")
    snapshot = SiteResumeSnapshot.model_validate(item.snapshot)
    try:
        canonical = _preview_source_url(item)
        if not canonical:
            raise ValueError
    except ResumeImportError:
        raise
    except Exception as exc:
        raise ResumeImportError("Предпросмотр требует повторной проверки ссылки") from exc
    if _sha256(canonical) != snapshot.source_url_hash:
        raise ResumeImportError("Предпросмотр содержит недействительную ссылку")
    return item, snapshot, canonical


def confirm_saved_resume_source(
    db: Session, *, adapter_id: str, preview_token: str, consent: bool,
    grammatical_gender: str | None = None,
) -> tuple[SavedResumeSource, str]:
    """Persist a confirmed source while leaving its preview token usable."""
    if consent is not True:
        raise ResumeImportError("Требуется явное согласие на сохранение ссылки")
    if grammatical_gender is not None and grammatical_gender not in {"male", "female"}:
        raise ResumeImportError("Выберите мужской или женский род")
    try:
        item, public_preview_snapshot, source_url = _saved_source_token(db, preview_token, adapter_id)
        # Preview state may hold the URL directly (legacy rows use the sealed
        # fallback); durable profile rows use the canonical public URL.
        resume_gender = _private_gender_value(item.private_view)
        if resume_gender is None and grammatical_gender is None:
            raise ResumeImportError(
                "Выберите мужской или женский род для сохраненного резюме"
            )
        selected_gender = grammatical_gender or resume_gender
        if uses_saved_resume_data(adapter_id):
            if not isinstance(item.full_snapshot, dict):
                raise ResumeImportError("Предпросмотр резюме требует повторного импорта")
            snapshot = SiteResumeSnapshot.model_validate(item.full_snapshot)
            if snapshot.content_hash != _snapshot_hash(snapshot):
                raise ResumeImportError("Предпросмотр резюме требует повторного импорта")
            if (
                snapshot.source_site != adapter_id
                or snapshot.source_url_hash != _sha256(source_url)
                or _sha256(snapshot.source_resume_id) != _sha256(public_preview_snapshot.source_resume_id)
            ):
                raise ResumeImportError("Предпросмотр резюме требует повторного импорта")
            snapshot = _apply_saved_gender(snapshot, selected_gender)
            snapshot = snapshot.model_copy(update={"content_hash": _snapshot_hash(snapshot)})
        else:
            snapshot = public_preview_snapshot
        public_snapshot, _ = _redacted_snapshot(snapshot)
        # ``public_snapshot`` redacts identity/contact values and their
        # repetitions from professional prose.  Reconstruct only the
        # presence/absence of the generic question from the protected preview;
        # The validated gender preference is stored separately from this
        # redacted preview; resume identity/contact values remain excluded.
        safe_preview = _saved_source_preview(
            public_snapshot,
            gender_known=selected_gender is not None,
            private_fields_found=_private_fields_found(item.private_view),
            source_url=source_url,
        )
        row = db.scalar(
            select(SavedResumeSource).where(SavedResumeSource.adapter_id == adapter_id)
        )
        values = {
            "adapter_id": adapter_id,
            "grammatical_gender": selected_gender,
            "source_url": source_url,
            "source_url_hash": _sha256(source_url),
            "resume_id_hash": _sha256(snapshot.source_resume_id),
            "content_hash": public_snapshot.content_hash,
            "resume_snapshot_payload": (
                _seal_private(snapshot.model_dump(mode="json"))
                if uses_saved_resume_data(adapter_id) else None
            ),
            "resume_data_saved_at": datetime.now(timezone.utc) if uses_saved_resume_data(adapter_id) else None,
            "preview": safe_preview,
            "status": SAVED_SOURCE_VALID,
            "checked_at": datetime.now(timezone.utc),
            "changed": False,
            "error_code": None,
        }
        if row is None:
            row = SavedResumeSource(**values)
            db.add(row)
        else:
            for key, value in values.items():
                setattr(row, key, value)
        db.commit()
        db.refresh(row)
        return row, preview_token
    except ResumeImportError:
        db.rollback()
        raise


def saved_resume_source_record(
    row: SavedResumeSource, *, preview_token: str | None = None
) -> dict[str, Any]:
    """Serialize the public durable-source contract.

    ``source_url`` is the canonical public link entered by the user.  Keep
    ``masked_url`` for older clients, but expose the real URL as the primary
    profile value so it remains useful after a restart or failed revalidation.
    """

    source_url: str | None = None
    try:
        source_url = _saved_source_url(row)
    except Exception:
        # A damaged legacy envelope must not make the rest of the profile
        # list unavailable.
        source_url = None

    def masked_url() -> str | None:
        try:
            if not source_url:
                return None
            parsed = urlparse(source_url)
            host = (parsed.hostname or "").casefold().rstrip(".")
            parts = [part for part in parsed.path.split("/") if part]
            if parts:
                parts[-1] = f"{parts[-1][:3]}•••"
            return urlunparse(("https", host, "/" + "/".join(parts), "", "", ""))
        except Exception:
            # A damaged envelope is still represented by its durable status;
            # never make an error while rendering a list hide the row.
            return None

    def safe_preview(value: Any) -> Any:
        if isinstance(value, dict):
            forbidden = {"source_resume_id", "external_id", "source_url", "resume_url", "url"}
            return {
                key: safe_preview(child)
                for key, child in value.items()
                if key not in forbidden
            }
        if isinstance(value, list):
            return [safe_preview(child) for child in value]
        if isinstance(value, str):
            return _DIRECT_URL.sub("[link omitted]", value)
        return value

    result: dict[str, Any] = {
        "adapter_id": row.adapter_id,
        "grammatical_gender": row.grammatical_gender,
        "status": row.status,
        "checked_at": row.checked_at.isoformat() if row.checked_at else None,
        "masked_url": masked_url(),
        "source_url": source_url,
        "import_url": _safe_import_url(row.adapter_id, source_url),
        "preview": safe_preview(dict(row.preview or {})),
        "changed": bool(row.changed),
    }
    if uses_saved_resume_data(row.adapter_id):
        try:
            snapshot = _validated_saved_snapshot(row)
            result.update({
                "uses_saved_data": True,
                "resume_data_status": "ready",
                "resume_data_saved_at": row.resume_data_saved_at.isoformat() if row.resume_data_saved_at else None,
                "resume_data_error_message": None,
                "completion_status": _saved_snapshot_completion(snapshot),
                "completion_error_message": None,
            })
        except ResumeImportError:
            state = "missing" if not getattr(row, "resume_snapshot_payload", None) else "corrupt"
            result.update({
                "uses_saved_data": True,
                "resume_data_status": state,
                "resume_data_saved_at": row.resume_data_saved_at.isoformat() if row.resume_data_saved_at else None,
                "resume_data_error_message": _SAVED_RESUME_DATA_ERROR,
                "completion_status": "error",
                "completion_error_message": _SAVED_RESUME_DATA_ERROR,
            })
    else:
        result.update({
            "uses_saved_data": False,
            "resume_data_status": None,
            "resume_data_saved_at": None,
            "resume_data_error_message": None,
            "completion_status": "empty",
            "completion_error_message": None,
        })
    if row.error_code:
        result["error_code"] = row.error_code
        if row.error_code == "unavailable":
            result["error_message"] = "Не удалось временно проверить ссылку; сохраненный источник оставлен"
    if preview_token is not None:
        result["preview_token"] = preview_token
    return result


async def _read_saved_resume_source(
    db: Session, adapter_id: str
) -> tuple[SavedResumeSource, str, SiteResumeSnapshot]:
    """Read and revalidate a durable source, without changing its row."""
    row = db.scalar(
        select(SavedResumeSource).where(SavedResumeSource.adapter_id == adapter_id)
    )
    if row is None:
        raise SavedResumeSourceNotFound("Сохраненный источник не найден")
    source_url = _saved_source_url(row)
    if not source_url:
        raise ResumeImportError("Ссылка сохраненного резюме отсутствует или повреждена")
    canonical, ref = validate_adapter_resume_url(adapter_id, source_url)
    if _sha256(canonical) != row.source_url_hash:
        raise ResumeImportError("Ссылка источника изменилась")
    fresh = await extract_resume(adapter_id, canonical, validated=(canonical, ref))
    if fresh.source_url_hash != row.source_url_hash:
        raise ResumeImportError("Ссылка источника изменилась")
    return row, canonical, fresh


async def revalidate_saved_resume_source(
    db: Session, adapter_id: str
) -> tuple[SavedResumeSource, SiteResumeSnapshot]:
    """Re-read a saved URL during explicit refresh and update its local copy.

    Errors are deliberately converted to ``unavailable`` while retaining the
    source row and last good resume snapshot. On success, the refreshed full
    snapshot replaces the local cache while the confirmed source URL remains.
    """
    checked_at = datetime.now(timezone.utc)
    try:
        row, _canonical, fresh = await _read_saved_resume_source(db, adapter_id)
        # The user's explicit source preference wins over a missing or changed
        # extractor value and is embedded before all snapshots are persisted.
        fresh = _apply_saved_gender(fresh, row.grammatical_gender)
        fresh = fresh.model_copy(update={"content_hash": _snapshot_hash(fresh)})
        public_snapshot, _ = _redacted_snapshot(fresh)
        changed = row.content_hash != public_snapshot.content_hash
        row.preview = _saved_source_preview(
            public_snapshot,
            gender_known=(
                _snapshot_gender_is_known(fresh)
                or row.grammatical_gender in {"male", "female"}
            ),
            private_fields_found=_snapshot_fields_found(fresh),
            source_url=_canonical,
        )
        row.content_hash = public_snapshot.content_hash
        row.resume_id_hash = _sha256(fresh.source_resume_id)
        if uses_saved_resume_data(adapter_id):
            _store_saved_snapshot(row, fresh)
        row.status = SAVED_SOURCE_CHANGED if changed else SAVED_SOURCE_VALID
        row.changed = changed
        row.checked_at = checked_at
        row.error_code = None
        if uses_saved_resume_data(adapter_id):
            db.commit()
        else:
            db.flush()
        return row, fresh
    except SavedResumeSourceNotFound:
        raise
    except Exception as exc:
        db.rollback()
        row = db.scalar(
            select(SavedResumeSource).where(SavedResumeSource.adapter_id == adapter_id)
        )
        if row is None:
            raise SavedResumeSourceNotFound("Сохраненный источник не найден") from exc
        # Preserve the lazy legacy upgrade even when the external site is
        # temporarily unavailable.  The URL is local profile state and must
        # not disappear merely because revalidation failed.
        with suppress(Exception):
            _saved_source_url(row)
        row.status = SAVED_SOURCE_UNAVAILABLE
        row.changed = False
        row.checked_at = checked_at
        row.error_code = "unavailable"
        db.commit()
        raise ResumeImportError("Не удалось повторно проверить сохраненное резюме") from exc


async def refresh_saved_resume_source(
    db: Session, adapter_id: str, *, issue_token: bool = True
) -> tuple[SavedResumeSource, str | None]:
    """Refresh one durable source and optionally issue a compatibility token.

    Any decryption, allowlist, browser, or extraction failure is represented
    by a generic unavailable status.  The durable row is retained and no page
    text or exception detail crosses the API boundary.
    """
    try:
        row, fresh = await revalidate_saved_resume_source(db, adapter_id)
        token = None
        if issue_token:
            source_url = _saved_source_url(row)
            token = issue_preview_token(db, adapter_id, fresh, source_url=source_url)
        return row, token
    except ResumeImportError:
        # ``revalidate_saved_resume_source`` has already retained and marked
        # the row.  Preserve the historical refresh API's non-raising result.
        row = db.scalar(select(SavedResumeSource).where(SavedResumeSource.adapter_id == adapter_id))
        if row is None:
            raise
        return row, None


async def list_saved_resume_sources(
    db: Session,
) -> list[tuple[SavedResumeSource, str | None]]:
    """List durable sources without touching the network or token store.

    Site checking belongs to explicit refresh. Session launch reads only the
    locally saved copy. A transient browser failure must never turn a normal
    GET into a disappearing source or replace its token behind the caller's
    back.
    """
    rows = list(db.scalars(select(SavedResumeSource).order_by(SavedResumeSource.adapter_id)))
    return [(row, None) for row in rows]


def delete_saved_resume_source(db: Session, adapter_id: str) -> None:
    """Delete a durable source and revoke its outstanding preview tokens."""
    row = db.scalar(
        select(SavedResumeSource).where(SavedResumeSource.adapter_id == adapter_id)
    )
    if row is None:
        raise SavedResumeSourceNotFound("Сохраненный источник не найден")
    _saved_source_url(row)
    db.delete(row)
    # Explicit deletion revokes temporary preview tokens for the same adapter.
    # Session-owned snapshots are intentionally unaffected.
    db.execute(
        delete(ResumePreviewToken)
        .where(ResumePreviewToken.adapter_id == adapter_id)
        .execution_options(synchronize_session=False)
    )
    db.commit()


def update_saved_resume_gender(
    db: Session, adapter_id: str, grammatical_gender: str
) -> SavedResumeSource:
    """Update only the local preference attached to a saved source."""
    if grammatical_gender not in {"male", "female"}:
        raise ResumeImportError("Выберите мужской или женский род")
    row = db.scalar(
        select(SavedResumeSource).where(SavedResumeSource.adapter_id == adapter_id)
    )
    if row is None:
        raise SavedResumeSourceNotFound("Сохраненный источник не найден")
    _saved_source_url(row)
    if uses_saved_resume_data(adapter_id) and row.resume_snapshot_payload:
        snapshot = _validated_saved_snapshot(row)
        row.grammatical_gender = grammatical_gender
        _store_saved_snapshot(row, snapshot, update_saved_at=False)
        # Keep the privacy-safe preview and its hash aligned with the newly
        # selected local preference without replacing any resume content.
        public_snapshot, _ = _redacted_snapshot(_apply_saved_gender(snapshot, grammatical_gender))
        row.preview = _saved_source_preview(
            public_snapshot,
            gender_known=True,
            private_fields_found=_snapshot_fields_found(snapshot),
            source_url=_saved_source_url(row),
        )
    else:
        row.grammatical_gender = grammatical_gender
    preview = dict(row.preview or {})
    preview.pop("questions", None)
    preview.pop("grammatical_gender", None)
    preview.pop("grammatical_gender_source", None)
    row.preview = preview
    db.commit()
    db.refresh(row)
    return row


def persist_session_snapshot(
    db: Session, session_id: int, snapshot: SiteResumeSnapshot
) -> SessionResumeSnapshot:
    """Persist an immutable session copy without creating a bearer token.

    Confirmed saved sources are durable records; the normalized private data
    needed by one running session is a separate, replaceable snapshot.  This
    helper is used after the source is re-read at create/start time and keeps
    that lifecycle independent from preview-token expiry/consumption.
    """
    public_snapshot, safe_professional = _redacted_snapshot(snapshot)
    row = db.scalar(
        select(SessionResumeSnapshot).where(SessionResumeSnapshot.session_id == session_id)
    )
    values = {
        "source_site": snapshot.source_site,
        "source_resume_id": snapshot.source_resume_id,
        "source_url_hash": snapshot.source_url_hash,
        # The JSON snapshot is the redacted public copy.  Store its hash in
        # the row-level integrity column as well; the workflow validator then
        # rebuilds the private projection and accepts the matching public
        # representation without comparing it to the pre-redaction hash.
        "content_hash": public_snapshot.content_hash,
        "snapshot": public_snapshot.model_dump(mode="json"),
        "professional_view": safe_professional.model_dump(mode="json"),
        "full_snapshot": snapshot.model_dump(mode="json"),
        "private_view": _seal_private(private_view(snapshot).model_dump(mode="json")),
        # A CREATED session may be abandoned, but a fresh launch snapshot is
        # valid for the same bounded recovery window as token-created copies.
        "expires_at": datetime.now(timezone.utc) + SNAPSHOT_TTL,
    }
    if row is None:
        row = SessionResumeSnapshot(session_id=session_id, **values)
        db.add(row)
    else:
        for key, value in values.items():
            setattr(row, key, value)
    db.flush()
    return row


def delete_snapshot(db: Session, session_id: int) -> bool:
    scalar = getattr(db, "scalar", None)
    if scalar is None:
        return False
    item = scalar(select(SessionResumeSnapshot).where(SessionResumeSnapshot.session_id == session_id))
    if item is None:
        return False
    db.delete(item)
    return True
