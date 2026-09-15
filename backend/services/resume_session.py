"""Session-scoped resume import, privacy views, and safe local rendering.

This module is deliberately independent of site navigation.  Adapters may
provide a ``resume_import`` capability; this service owns token lifetime,
normalization, persistence, and the boundary between model and private data.
"""

from __future__ import annotations

import base64
import hashlib
import html
import importlib
import json
import re
import secrets
import sys
import unicodedata
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlparse, urlunparse

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from backend.adapters import adapter_registry
from backend.persistence.crypto import decrypt_secret, encrypt_secret
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

PREVIEW_TTL = timedelta(minutes=10)
SNAPSHOT_TTL = timedelta(hours=24)
SAVED_SOURCE_VALID = "valid"
SAVED_SOURCE_CHANGED = "changed"
SAVED_SOURCE_UNAVAILABLE = "unavailable"
_MARKER = re.compile(
    r"(?:\{\{.*?\}\}|\{%.*?%\}|<%.*?%>|\$\{.*?\}|\[[^\]\r\n]{1,160}\])",
    re.DOTALL,
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HTML_BLOCK = re.compile(r"(?is)<(script|style)\b[^>]*>.*?</\1\s*>")
_HTML_TAG = re.compile(r"(?is)</?[a-z][^>]*>")
_EMAIL = re.compile(r"(?i)(?<![\w.+-])[\w.!#$%&'*+/=?^`{|}~-]+@[\w-]+(?:\.[\w-]+)+(?![\w.-])")
_PHONE = re.compile(
    r"(?<!\w)(?:\+?7|8)[\s().-]*(?:\d[\s().-]*){9,10}(?!\w)"
)
_MESSENGER_URL = re.compile(
    r"(?i)https?://(?:t\.me|telegram\.me|wa\.me|whatsapp\.com|vk\.com)/[^\s<>]+"
)
_DIRECT_URL = re.compile(
    r'(?i)(?:https?://|ftp://|mailto:|tel:|www\.)[^\s<>"\']+'
    r"|(?<![\w@])(?:github\.com|gitlab\.com|linkedin\.com|facebook\.com|"
    r'instagram\.com|behance\.net|dribbble\.com)(?:/[^\s<>"\']*)?'
)


class ResumeImportError(ValueError):
    """Safe user-facing import/validation error (never contains page text)."""


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


def _unseal_private(value: str) -> dict:
    try:
        if value.startswith("dpapi:"):
            payload = decrypt_secret(value[6:])
        elif value.startswith("sealed-test:"):
            payload = base64.urlsafe_b64decode(value[11:]).decode("utf-8")
        else:
            raise ValueError
        data = json.loads(payload)
    except Exception as exc:
        raise ResumeImportError("Повреждён защищённый снимок резюме") from exc
    if not isinstance(data, dict):
        raise ResumeImportError("Повреждён защищённый снимок резюме")
    return data


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
    query_keys = {key.casefold() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
    if any(not key.startswith("utm_") and key not in {"from", "hhtmfrom"} for key in query_keys):
        raise ResumeImportError("Ссылка содержит недопустимые параметры")
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
    for key in ("residence", "relocation", "business_trips", "citizenship", "work_permit"):
        if key not in location and key in raw:
            location[key] = raw[key]
    identity = mapping("identity")
    contacts = mapping("contacts")
    if "full_name" in raw and "full_name" not in identity:
        identity["full_name"] = raw["full_name"]
    for key in ("gender", "age", "birth_date", "has_photo"):
        if key in raw and key not in identity:
            identity[key] = raw[key]
    for key in ("phone", "email", "messengers", "links"):
        if key in raw and key not in contacts:
            contacts[key] = raw[key]

    for group in (target, location, identity, contacts):
        for key, value in list(group.items()):
            group[key] = _source_field(value)
    payload = {
        "schema_version": 1,
        "extractor_version": str(raw.get("extractor_version") or "site-resume-v1"),
        "source_site": adapter_id,
        "source_resume_id": ref_id,
        "source_url_hash": expected_url_hash,
        "content_hash": "0" * 64,
        "source_updated_at": raw.get("source_updated_at"),
        "imported_at": raw.get("imported_at") or utcnow(),
        "identity": identity,
        "contacts": contacts,
        "target": target,
        "location": location,
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
    # Import timestamps describe when this copy was read, not its contents.
    # Excluding them makes a revalidation of an unchanged page deterministic.
    digest = _snapshot_hash(snapshot)
    return snapshot.model_copy(update={"content_hash": digest})


def _snapshot_hash(snapshot: SiteResumeSnapshot) -> str:
    content = snapshot.model_dump(
        mode="json", exclude={"content_hash", "imported_at", "source_updated_at"}
    )
    return _sha256(json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def professional_view(snapshot: SiteResumeSnapshot | dict) -> ResumeProfessionalView:
    item = snapshot if isinstance(snapshot, SiteResumeSnapshot) else SiteResumeSnapshot.model_validate(snapshot)
    view = ResumeProfessionalView(
        target=item.target, location=item.location, experience=item.experience,
        projects=item.projects, skills=item.skills, education=item.education,
        languages=item.languages, courses=item.courses, certifications=item.certifications,
        awards=item.awards, portfolio=item.portfolio, about=item.about,
        additional_sections=item.additional_sections,
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

    def flatten(value: Any) -> Any:
        if isinstance(value, dict):
            if "value" in value and "availability" in value:
                return flatten(value.get("value"))
            return {key: flatten(item) for key, item in value.items()}
        if isinstance(value, list):
            return [flatten(item) for item in value]
        return value

    flattened = flatten(data)
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
        "identity", "contacts", "target", "location", "experience", "skills",
        "education", "projects", "languages", "courses", "certifications",
        "awards", "portfolio", "about", "additional_sections", "coverage",
    )
    data = item.model_dump(mode="json", include=set(fields))

    def flatten(node: Any) -> Any:
        if isinstance(node, dict):
            if "value" in node and "availability" in node:
                return flatten(node.get("value"))
            return {
                key: flatten(child)
                for key, child in node.items()
                if key not in {"source_locator", "source_section"}
            }
        if isinstance(node, list):
            return [flatten(child) for child in node]
        if isinstance(node, str):
            # Adapter contracts are text-oriented, but a site can still
            # accidentally pass markup from a rich-text section.  Keep the
            # model boundary plain text and discard executable/style blocks.
            return _CONTROL.sub("", html.unescape(_HTML_TAG.sub("", _HTML_BLOCK.sub("", node))))
        return node

    return flatten(data)


def public_preview(snapshot: SiteResumeSnapshot) -> dict[str, Any]:
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
    return {
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


def _saved_source_preview(
    snapshot: SiteResumeSnapshot, *, gender_known: bool | None = None,
    private_fields_found: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """Return metadata safe for durable storage and saved-source responses.

    ``public_preview`` is intentionally useful during the launch flow and may
    contain a session-only question (including its answer options).  Saved
    sources must not retain an answer value or gender, so strip only those
    values at this separate persistence boundary.  Generic question metadata
    remains useful for asking the user again after a restart.
    """
    result = dict(public_preview(snapshot))

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
    try:
        # Site extractors may use a browser page (preferred) or implement a
        # complete URL-based flow for tests/controlled browser adapters.
        if page is None:
            # The browser layer owns Playwright lifecycle. No generic HTTP
            # client or hidden site endpoint is used for resume extraction.
            from backend.browser.executor import BrowserExecutor

            executor = BrowserExecutor(
                adapter_id,
                tuple(getattr(adapter, "allowed_domains", ())),
                headless=True,
                navigation_hop_limit=8,
            )
            page = await executor.start()
        opener = getattr(capability, "open_resume", None)
        if opener is None and module is not None:
            policy = getattr(module, "POLICY", None)
            if policy is not None:
                async def module_opener(browser_page, resume_ref):
                    await browser_page.goto(resume_ref.url, wait_until="domcontentloaded", timeout=60_000)
                    final_url = getattr(browser_page, "url", resume_ref.url)
                    if callable(final_url):
                        final_url = final_url()
                    policy.validate_final(final_url or resume_ref.url, resume_ref.external_id)
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
    except ResumeImportError:
        raise
    except Exception as exc:
        raise ResumeImportError("Не удалось прочитать выбранное резюме") from exc
    finally:
        if executor is not None:
            with suppress(Exception):
                await executor.close()
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
        item, snapshot, source_url = _saved_source_token(db, preview_token, adapter_id)
        # Preview state may hold the URL directly (legacy rows use the sealed
        # fallback); durable profile rows use the canonical public URL.
        resume_gender = _private_gender_value(item.private_view)
        if resume_gender is None and grammatical_gender is None:
            raise ResumeImportError(
                "Выберите мужской или женский род для сохраненного резюме"
            )
        selected_gender = grammatical_gender or resume_gender
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
        "preview": safe_preview(dict(row.preview or {})),
        "changed": bool(row.changed),
    }
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
    """Re-read a saved URL and update its durable status/preview.

    Errors are deliberately converted to ``unavailable`` while retaining the
    source row.  The fresh snapshot is returned only for the caller's current
    session and is never used as a replacement for the durable URL.
    """
    checked_at = datetime.now(timezone.utc)
    try:
        row, _canonical, fresh = await _read_saved_resume_source(db, adapter_id)
        # The user's explicit source preference wins over a missing or changed
        # extractor value and is embedded before all snapshots are persisted.
        fresh = _apply_saved_gender(fresh, row.grammatical_gender)
        public_snapshot, _ = _redacted_snapshot(fresh)
        changed = row.content_hash != public_snapshot.content_hash
        row.preview = _saved_source_preview(
            public_snapshot,
            gender_known=(
                _snapshot_gender_is_known(fresh)
                or row.grammatical_gender in {"male", "female"}
            ),
            private_fields_found=_snapshot_fields_found(fresh),
        )
        row.content_hash = public_snapshot.content_hash
        row.resume_id_hash = _sha256(fresh.source_resume_id)
        row.status = SAVED_SOURCE_CHANGED if changed else SAVED_SOURCE_VALID
        row.changed = changed
        row.checked_at = checked_at
        row.error_code = None
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

    Source checking belongs to session launch or an explicit refresh action.
    A transient browser failure must therefore never turn a normal GET into a
    disappearing source or replace its token behind the caller's back.
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


def _flat_private(private: ResumePrivateView | dict) -> dict[str, str]:
    if isinstance(private, str):
        private = _unseal_private(private)
    data = private.model_dump(mode="json") if isinstance(private, ResumePrivateView) else dict(private or {})
    identity = data.get("identity", {}) if isinstance(data.get("identity"), dict) else {}
    contacts = data.get("contacts", {}) if isinstance(data.get("contacts"), dict) else {}
    result: dict[str, str] = {}
    for _name, group in (("", identity), ("", contacts)):
        for key, value in group.items():
            if isinstance(value, dict) and "value" in value:
                value = value.get("value")
            if isinstance(value, list):
                value = ", ".join(str(item).strip() for item in value if str(item).strip())
            if value is not None and str(value).strip():
                result[key.casefold()] = str(value).strip()
    result.update({"fio": result.get("full_name", ""), "name": result.get("full_name", "")})
    return result


def redact_private_text(text: str, private: ResumePrivateView | dict | None = None) -> str:
    """Remove private literals before arbitrary text enters a model payload."""
    values: list[str] = []
    if private is not None:
        values.extend(value for value in _flat_private(private).values() if len(value) >= 2)
    result = str(text or "")
    for value in sorted({item.casefold() for item in values}, key=len, reverse=True):
        result = re.sub(re.escape(value), "[private value omitted]", result, flags=re.I)
    result = _EMAIL.sub("[email omitted]", result)
    result = _PHONE.sub("[phone omitted]", result)
    return _DIRECT_URL.sub("[link omitted]", result)


def render_local_private(text: str, private: ResumePrivateView | dict) -> str:
    """Replace private placeholders locally and reject all leftovers.

    Missing values remove the marker and adjacent list punctuation.  Control
    and zero-width format characters are removed before the final invariant,
    so neither template syntax nor service markers can reach a form or letter.
    """
    values = _flat_private(private)
    aliases = {
        "full_name": "full_name", "фио": "full_name", "имя": "full_name",
        "phone": "phone", "телефон": "phone", "email": "email", "почта": "email",
        "messengers": "messengers", "мессенджеры": "messengers",
    }
    pattern = re.compile(r"\{\{\s*([\wа-яё.-]+)\s*\}\}|\[\s*([^\]\r\n]{1,100})\s*\]")

    def marker_key(match: re.Match[str]) -> str:
        key = (match.group(1) or match.group(2) or "").casefold().strip()
        return aliases.get(key, key)

    def is_contact_line(line: str) -> bool:
        """Identify a whole contact/signature line, without touching prose."""
        plain = pattern.sub("", line).casefold()
        return bool(re.search(
            r"(?:\b(?:телефон|phone|мобильн\w*|email|e[- ]?mail|почт\w*|"
            r"мессенджер\w*|messenger\w*|telegram|whatsapp|телеграм)\b|"
            r"(?:^|\s)(?:фио|full\s+name|с\s+уважением)(?:\s|:|,|$))",
            plain,
        ))

    # Drop an entire contact/signature line when its value is unavailable.
    # A generic prose line keeps its surrounding text (for example,
    # ``Здравствуйте, {{full_name}}`` becomes ``Здравствуйте,``).
    prepared_lines: list[str] = []
    for line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        markers = list(pattern.finditer(line))
        if markers and is_contact_line(line) and any(marker_key(marker) not in values for marker in markers):
            continue
        prepared_lines.append(line)

    def replace(match: re.Match[str]) -> str:
        return values.get(marker_key(match), "")

    rendered = pattern.sub(replace, "\n".join(prepared_lines))
    # Collapse malformed/nested bracket markers as well (e.g. ``[[email]]``)
    # before the generic marker scrubber can leave one delimiter behind.
    rendered = re.sub(r"\[+[^\]\r\n]*\]+", "", rendered)
    rendered = re.sub(r"\{+[^}\r\n]*\}+", "", rendered)
    # Fail closed for unmatched service-marker openers too.  The normal
    # marker regex intentionally requires a closing delimiter, so malformed
    # input must be scrubbed separately rather than reaching a form/report.
    rendered = re.sub(r"\{\{[^{}\r\n]*(?:\}\}|$)", "", rendered, flags=re.MULTILINE)
    rendered = re.sub(r"\[\[[^\[\]\r\n]*(?:\]\]|$)", "", rendered, flags=re.MULTILINE)
    rendered = re.sub(r"<%[^<>\r\n]*(?:%>|$)", "", rendered, flags=re.MULTILINE)
    rendered = re.sub(r"\$\{[^{}\r\n]*(?:\}|$)", "", rendered, flags=re.MULTILINE)
    rendered = _MARKER.sub("", rendered)
    rendered = _CONTROL.sub("", rendered)
    rendered = "".join(char for char in rendered if unicodedata.category(char) != "Cf" or char in "\n\t")
    # A missing placeholder must not leave an empty bullet or dangling colon.
    rendered = re.sub(r"(?m)^\s*[-*•]\s*$\n?", "", rendered)
    rendered = re.sub(r"[ \t]{2,}", " ", rendered)
    rendered = re.sub(r"[ \t]+([,:;])", r"\1", rendered)
    rendered = rendered.replace("[", "").replace("]", "")
    rendered = "\n".join(line.rstrip() for line in rendered.splitlines()).strip()
    if _MARKER.search(rendered) or any(token in rendered for token in ("{{", "}}", "[[", "]]")):
        raise ResumeImportError("Не удалось безопасно собрать текст письма или анкеты")
    return rendered


# Explicit aliases make the security boundary discoverable to callers.
render_private_placeholders = render_local_private
