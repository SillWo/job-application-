from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from backend.schemas.domain import CoverLetterDraft, JobPosting


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
                "Напиши готовое сопроводительное письмо на русском языке от первого лица, "
                "120–180 слов. Используй только данные из profile и resumes, свяжи их с "
                "задачами вакансии и не выдумывай опыт, контакты, навыки, цифры или достижения. "
                "Если называешь кандидата, используй только profile.full_name. Верни только поле text."
            ),
        },
        CoverLetterDraft,
    )
    return draft.text.strip()
