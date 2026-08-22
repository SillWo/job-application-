from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from backend.schemas.domain import CoverLetterDraft, JobPosting

_CONTACT_CLOSING = (
    "Буду рад продолжить общение с вами в этом чате или в мессенджерах -"
)


def _messenger_links(profile_payload: Any) -> list[str]:
    """Return only the messenger values explicitly present in the profile."""
    contacts = profile_payload.get("contacts", {}) if isinstance(profile_payload, dict) else {}
    values = contacts.get("messengers", []) if isinstance(contacts, dict) else []
    return [str(value).strip() for value in values if str(value).strip()]


def _finish_cover_letter(text: str, profile_payload: Any) -> str:
    links = _messenger_links(profile_payload)
    closing = f"{_CONTACT_CLOSING} {', '.join(links)}" if links else _CONTACT_CLOSING
    # Keep the contractual closing intact even when the model exceeds its limit.
    available = max(0, 100 - len(closing.split()))
    body_words = text.strip().split()[:available]
    body = " ".join(body_words).rstrip(" ,;:")
    return f"{body}\n\n{closing}" if body else closing


def _payload(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    elif isinstance(value, dict):
        value = dict(value)
    else:
        value = {
            key: getattr(value, key)
            for key in dir(value)
            if not key.startswith("_") and not callable(getattr(value, key, None))
        }
    return value


async def write_cover_letter(
    job: JobPosting,
    profile: Any,
    resumes: Sequence[Any],
    gateway,
) -> str:
    profile_payload = _payload(profile)
    resume_payloads = [_payload(resume) for resume in resumes]
    if not resume_payloads:
        raise ValueError("Для сопроводительного письма не выбрано ни одного резюме")

    draft = await gateway.structured(
        "writer",
        {
            "vacancy": {
                "title": job.title,
                "company": job.company,
                "description": job.description[:8_000],
            },
            "profile": profile_payload,
            "resumes": resume_payloads,
            "requirements": (
                "Напиши основной текст готового сопроводительного письма на русском языке от первого лица, не более 70 слов. "
                "В нём обязательно должны быть три коротких смысловых блока: почему понравилась вакансия, почему понравилась "
                "компания и каковы преимущества кандидата для этой вакансии. Используй официальный, но живой стиль. Опирайся только на описание вакансии, "
                "profile и resumes; не выдумывай опыт, контакты, навыки, цифры или достижения. Не добавляй финальную "
                "фразу с мессенджерами — она будет добавлена автоматически. Если называешь кандидата, используй "
                "только profile.full_name. Верни только поле text."
            ),
        },
        CoverLetterDraft,
    )
    return _finish_cover_letter(draft.text, profile_payload)
