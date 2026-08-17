from __future__ import annotations

import json
from typing import TypeVar

from openai import APIError, APITimeoutError, AsyncOpenAI
from pydantic import BaseModel, ValidationError

from backend.config import settings

from .gateway import ModelUnavailable
from .prompts import ROLE_OPTIONS, ROLE_PROMPTS

T = TypeVar("T", bound=BaseModel)


def _system_prompt(role: str, schema: type[BaseModel]) -> str:
    """Build a strict JSON prompt for an OpenAI-compatible cloud endpoint."""
    schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    return (
        f"{ROLE_PROMPTS[role]}\n\n"
        "ВАЖНО: верни ТОЛЬКО один корректный JSON-объект. "
        "Не используй Markdown, ```json, комментарии или текст до/после JSON. "
        "Ответ обязан соответствовать этой JSON Schema:\n"
        f"{schema_json}"
    )


def _missing_resume_analysis_fields(parsed: BaseModel) -> list[str]:
    if parsed.__class__.__name__ != "ResumeAnalysis":
        return []

    criteria = (
        "title",
        "tasks",
        "industry",
        "required_years",
        "seniority",
        "languages",
        "skills",
    )
    missing: list[str] = []

    if "vacancy_seniority" not in parsed.model_fields_set:
        missing.append("vacancy_seniority")

    for field_name in criteria:
        if field_name not in parsed.model_fields_set:
            missing.append(field_name)
            continue

        assessment = getattr(parsed, field_name)
        for nested_name in ("match", "confidence", "evidence"):
            if nested_name not in assessment.model_fields_set:
                missing.append(f"{field_name}.{nested_name}")

        if assessment.match > 0 and (
            assessment.confidence <= 0 or not assessment.evidence
        ):
            missing.append(f"{field_name}.grounding")

    return missing


async def analyze_relevance(
    payload: dict,
    schema: type[T],
) -> T:
    """Run vacancy relevance analysis through the shared cloud API settings."""
    client = AsyncOpenAI(
        base_url=settings.openai_base_url,
        api_key=settings.openai_api_key,
        timeout=settings.openai_timeout,
        max_retries=1,
    )

    opts = ROLE_OPTIONS["resume_analyst"]
    messages: list[dict] = [
        {
            "role": "system",
            "content": _system_prompt("resume_analyst", schema),
        },
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False),
        },
    ]

    last_error: Exception | None = None

    for attempt in range(2):
        try:
            response = await client.chat.completions.create(
                model=settings.openai_model,
                messages=messages,  # type: ignore[arg-type]
                temperature=opts.get("temperature", 0.1),
                max_tokens=opts.get("num_predict", 4096),
                response_format={"type": "json_object"},
            )

            content = (response.choices[0].message.content or "").strip()
            if not content:
                raise ValueError("Cloud LLM вернула пустой ответ")

            parsed = schema.model_validate_json(content)

            missing = _missing_resume_analysis_fields(parsed)
            if missing:
                raise ValueError(
                    "ResumeAnalysis содержит неполные поля: " + ", ".join(missing)
                )

            return parsed

        except (ValidationError, ValueError) as exc:
            last_error = exc
            if attempt == 0:
                messages[0]["content"] += (
                    "\n\nПредыдущий ответ не прошёл валидацию. "
                    "Повтори ПОЛНЫЙ JSON-объект. Для каждого критерия обязательно верни "
                    "match, confidence, explanation и evidence. "
                    "Если match > 0, evidence должен содержать подтверждение из вакансии, "
                    "профиля или резюме. Не добавляй никакого текста вне JSON."
                )
                continue
        except (APITimeoutError, APIError) as exc:
            raise ModelUnavailable(
                f"Cloud LLM API недоступен: {exc}"
            ) from exc

    raise ModelUnavailable(
        "Cloud LLM вернула неполный или некорректный JSON "
        "после повторной попытки: "
        f"{last_error}"
    )
