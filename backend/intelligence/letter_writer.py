from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from backend.schemas.domain import CoverLetterDraft, JobPosting

_CONTACT_CLOSING = (
    "Буду рад продолжить общение с вами в этом чате или в мессенджерах -"
)
_GREETING = "Здравствуйте!"
_COVER_LETTER_WORD_LIMIT = 110


def _messenger_links(profile_payload: Any) -> list[str]:
    """Return only the messenger values explicitly present in the profile."""
    contacts = profile_payload.get("contacts", {}) if isinstance(profile_payload, dict) else {}
    values = contacts.get("messengers", []) if isinstance(contacts, dict) else []
    return [str(value).strip() for value in values if str(value).strip()]


def _finish_cover_letter(text: str, profile_payload: Any) -> str:
    links = _messenger_links(profile_payload)
    closing = f"{_CONTACT_CLOSING} {', '.join(links)}" if links else _CONTACT_CLOSING
    # Keep the contractual closing intact even when the model exceeds its limit.
    body_text = text.strip()
    if body_text.casefold().startswith(_GREETING.casefold()):
        body_text = body_text[len(_GREETING):].lstrip()
    available = max(0, _COVER_LETTER_WORD_LIMIT - len(_GREETING.split()) - len(closing.split()))
    body_words = body_text.split()[:available]
    body = " ".join(body_words).rstrip(" ,;:")
    return f"{_GREETING}\n\n{body}\n\n{closing}" if body else f"{_GREETING}\n\n{closing}"


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
    preference_policy=None,
) -> str:
    profile_payload = _payload(profile)
    resume_payloads = [_payload(resume) for resume in resumes]
    if not resume_payloads:
        raise ValueError("Для сопроводительного письма не выбрано ни одного резюме")

    ai_payload = {
            "vacancy": {
                "title": job.title,
                "company": job.company,
                "description": job.description[:8_000],
            },
            "profile": profile_payload,
            "resumes": resume_payloads,
            "requirements": (
                "Напиши основной текст готового сопроводительного письма на русском языке от первого лица, не более 80 слов, без приветствия. "
                "В нём обязательно должны быть три коротких смысловых блока: почему понравилась вакансия, почему понравилась "
                "компания и каковы преимущества кандидата для этой вакансии. Используй официальный, но живой стиль. Опирайся только на описание вакансии, "
                "profile и resumes; не выдумывай опыт, контакты, навыки, цифры или достижения. Не добавляй финальную "
                "фразу с мессенджерами — она будет добавлена автоматически. Если называешь кандидата, используй "
                "только profile.full_name. Верни только поле text."
            ),
        }
    if preference_policy:
        ai_payload["preference_policy"] = preference_policy.model_dump(mode="json") if hasattr(preference_policy, "model_dump") else preference_policy
    draft = await gateway.structured(
        "writer",
        ai_payload,
        CoverLetterDraft,
    )
    return _finish_cover_letter(draft.text, profile_payload)
