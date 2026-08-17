from __future__ import annotations

import asyncio
import json
from typing import TypeVar

import httpx
from openai import APIError, AsyncOpenAI
from pydantic import BaseModel, ValidationError

from backend.config import settings

from .prompts import ROLE_OPTIONS, ROLE_PROMPTS

T = TypeVar("T", bound=BaseModel)

_RESUME_ANALYSIS_CRITERIA = (
    "title",
    "tasks",
    "industry",
    "required_years",
    "seniority",
    "languages",
    "skills",
)


class ModelUnavailable(RuntimeError):
    pass


def _schema_for_role(role: str, schema: type[BaseModel]) -> dict:
    result = schema.model_json_schema()
    if role == "resume_analyst" and schema.__name__ == "ResumeAnalysis":
        result["required"] = list(result.get("properties", {}))
        assessment = result.get("$defs", {}).get("MatchAssessment", {})
        assessment["required"] = list(assessment.get("properties", {}))
    elif role == "policy_filter" and schema.__name__ == "PolicyFilterResult":
        result["required"] = list(result.get("properties", {}))
        for definition_name in ("FlagMatch", "WorkFormatAssessment"):
            definition = result.get("$defs", {}).get(definition_name, {})
            definition["required"] = list(definition.get("properties", {}))
    return result


def _system_prompt_for_role(role: str, payload: dict) -> str:
    prompt = ROLE_PROMPTS[role]
    if role != "policy_filter":
        return prompt
    policy = payload.get("policy", {})
    green_count = len(policy.get("green_flags", []))
    red_count = len(policy.get("red_flags", []))
    return (
        f"{prompt} Верни ровно {green_count} объектов green_flags и ровно {red_count} объектов "
        "red_flags: по одному объекту для каждого переданного policy flag, с точной строкой flag "
        "и в исходном порядке. Даже absent или uncertain flag обязан быть отдельным объектом."
    )


def _resume_analysis_missing_fields(parsed: BaseModel) -> list[str]:
    if parsed.__class__.__name__ != "ResumeAnalysis":
        return []
    missing = []
    if "vacancy_seniority" not in parsed.model_fields_set:
        missing.append("vacancy_seniority")
    for field_name in _RESUME_ANALYSIS_CRITERIA:
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


def _resume_import_missing_fields(source: str, parsed: BaseModel) -> list[str]:
    """Find schema fields that were defaulted although their HH section exists."""
    if parsed.__class__.__name__ != "ResumeImportData":
        return []
    profile = parsed.profile
    resume = parsed.resume
    text = source.lower()
    requirements = (
        (("образован",), bool(profile.education)),
        (("язык",), bool(profile.languages)),
        (("опыт работы",), bool(resume.experiences)),
        (("навык", "стек", "стэк"), bool(resume.skills)),
        (("желаемая должность",), bool(resume.desired_title)),
        (("тип занятости",), bool(resume.employment_types)),
        (("формат работы",), bool(resume.work_formats)),
        (("командиров",), resume.business_trips is not None),
    )
    return [
        ", ".join(markers)
        for markers, populated in requirements
        if any(marker in text for marker in markers) and not populated
    ]


def _resume_import_is_incomplete(source: str, parsed: BaseModel) -> bool:
    return bool(_resume_import_missing_fields(source, parsed))


def _merge_resume_import(base: BaseModel, candidate: BaseModel) -> BaseModel:
    """Merge non-default parser blocks while keeping the first complete values."""
    profile = base.profile.model_dump()
    candidate_profile = candidate.profile.model_dump()
    for key, value in candidate_profile.items():
        if value not in (None, [], {}):
            profile[key] = value
    resume = base.resume.model_dump()
    candidate_resume = candidate.resume.model_dump()
    for key, value in candidate_resume.items():
        if value not in (None, [], ""):
            resume[key] = value
    return base.__class__.model_validate({"profile": profile, "resume": resume})


class ModelGateway:
    _lock = asyncio.Lock()

    def __init__(self, provider: str | None = None) -> None:
        self.provider = provider or settings.llm_provider

    async def status(self) -> dict:
        if self.provider == "mock":
            return {"connected": True, "model_available": True, "provider": "mock", "model": "deterministic-mock"}
        if self.provider == "openai_compat":
            try:
                async with httpx.AsyncClient(timeout=3) as client:
                    response = await client.get(
                        f"{settings.openai_base_url.rstrip('/')}/models",
                        headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                    )
                    response.raise_for_status()
                return {
                    "connected": True,
                    "model_available": True,
                    "provider": "openai_compat",
                    "model": settings.openai_model,
                }
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                return {
                    "connected": False,
                    "model_available": False,
                    "provider": "openai_compat",
                    "model": settings.openai_model,
                    "message": str(exc),
                }
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                response = await client.get(f"{settings.ollama_base_url}/api/tags")
                response.raise_for_status()
            names = {item["name"] for item in response.json().get("models", [])}
            wanted = settings.ollama_model
            available = wanted in names or any(name.startswith(f"{wanted}:") for name in names)
            return {"connected": True, "model_available": available, "provider": "ollama", "model": wanted}
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            return {"connected": False, "model_available": False, "provider": "ollama", "model": settings.ollama_model, "message": str(exc)}

    async def structured(self, role: str, payload: dict, schema: type[T]) -> T:
        if self.provider == "mock":
            return self._mock(role, payload, schema)
        if self.provider == "openai_compat":
            return await self._structured_openai(role, payload, schema)
        async with self._lock:
            body = {
                "model": settings.ollama_model,
                "stream": False,
                "format": _schema_for_role(role, schema),
                "messages": [
                    {"role": "system", "content": _system_prompt_for_role(role, payload)},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                "options": {k: v for k, v in ROLE_OPTIONS[role].items() if k != "think"},
                "think": ROLE_OPTIONS[role]["think"],
            }
            try:
                request_timeout = 300 if role == "profile" else 180
                async with httpx.AsyncClient(timeout=request_timeout) as client:
                    attempts = 4 if role == "profile" and schema.__name__ == "ResumeImportData" else 2
                    resume_candidate = None
                    for attempt in range(attempts):
                        response = await client.post(
                            f"{settings.ollama_base_url}/api/chat", json=body
                        )
                        response.raise_for_status()
                        content = response.json()["message"]["content"]
                        try:
                            if not content.strip():
                                raise ValueError("Ollama returned an empty response")
                            parsed = schema.model_validate_json(content)
                            if role == "resume_analyst":
                                missing_analysis = _resume_analysis_missing_fields(parsed)
                                if missing_analysis:
                                    raise ValueError(
                                        "ResumeAnalysis contains defaulted or incomplete fields: "
                                        + ", ".join(missing_analysis)
                                    )
                            if role == "profile" and _resume_import_is_incomplete(
                                payload.get("resume_text", ""), parsed
                            ):
                                if resume_candidate is not None:
                                    parsed = _merge_resume_import(resume_candidate, parsed)
                                resume_candidate = parsed
                                missing = _resume_import_missing_fields(
                                    payload.get("resume_text", ""), parsed
                                )
                                if not missing:
                                    return parsed
                                if attempt == attempts - 1:
                                    raise ModelUnavailable(
                                        "Ollama вернула неполный ResumeImportData после repair-pass"
                                    )
                                body["messages"][0]["content"] += (
                                    f" Обязательный targeted repair: пропущены поля/разделы {missing}. "
                                    "Найди их в исходном тексте и заполни явно. Особенно проверь заголовки "
                                    "Образование и Языки: верни все записи, даже если они находятся после "
                                    "опыта работы. Не оставляй эти поля пустыми при наличии текста раздела."
                                )
                                raise ValueError("ResumeImportData contains defaulted fields")
                            if (
                                role == "profile"
                                and schema.__name__ == "ResumeImportData"
                                and resume_candidate is not None
                            ):
                                parsed = _merge_resume_import(resume_candidate, parsed)
                            return parsed
                        except (ValidationError, ValueError) as exc:
                            if attempt == attempts - 1:
                                raise ModelUnavailable(
                                    "Ollama вернула неполный или некорректный JSON "
                                    f"после повторной попытки: {exc}"
                                ) from exc
                        # Retry truncated or otherwise invalid structured output once with
                        # a larger deterministic non-thinking response budget.
                        body["think"] = False
                        body["options"]["num_ctx"] = max(
                            int(body["options"].get("num_ctx", 0)), 32768
                        )
                        body["options"]["num_predict"] = max(
                            int(body["options"].get("num_predict", 0)), 12000
                        )
                        if role == "profile" and schema.__name__ == "ResumeImportData":
                            body["messages"][0]["content"] += (
                                " Предыдущий JSON не прошёл проверку. Повтори полный ResumeImportData: "
                                "извлеки каждый явно присутствующий элемент из разделов education, languages, "
                                "experiences, skills, desired_title, employment_types, work_formats и "
                                "business_trips. Пустой массив запрещён, если соответствующий раздел есть в "
                                "исходном тексте. Не выбирай значения по умолчанию и обязательно заверши JSON."
                            )
                        else:
                            body["messages"][0]["content"] += (
                                f" Предыдущий JSON не прошёл проверку. Повтори полный объект схемы "
                                f"{schema.__name__}, сохрани все обязательные поля и заверши JSON без markdown."
                            )
            except ModelUnavailable:
                raise
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                raise ModelUnavailable(f"Ollama временно недоступна: {exc}") from exc

    async def _structured_openai(self, role: str, payload: dict, schema: type[T]) -> T:
        """Call an OpenAI-compatible API endpoint to get a structured response."""
        client = AsyncOpenAI(
            base_url=settings.openai_base_url,
            api_key=settings.openai_api_key,
        )
        json_schema = _schema_for_role(role, schema)
        system_prompt = _system_prompt_for_role(role, payload)
        opts = ROLE_OPTIONS[role]
        attempts = 4 if role == "profile" and schema.__name__ == "ResumeImportData" else 2
        request_timeout = 300 if role == "profile" else 180

        async with self._lock:
            messages: list[dict] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ]
            resume_candidate = None
            try:
                for attempt in range(attempts):
                    response = await client.chat.completions.create(
                        model=settings.openai_model,
                        messages=messages,  # type: ignore[arg-type]
                        temperature=opts.get("temperature", 0.1),
                        max_tokens=opts.get("num_predict", 4096),
                        timeout=request_timeout,
                        response_format={  # type: ignore[arg-type]
                            "type": "json_schema",
                            "json_schema": {
                                "name": schema.__name__,
                                "schema": json_schema,
                                "strict": False,
                            },
                        },
                    )
                    content = (response.choices[0].message.content or "").strip()
                    try:
                        if not content:
                            raise ValueError("OpenAI-compat вернул пустой ответ")
                        parsed = schema.model_validate_json(content)
                        if role == "resume_analyst":
                            missing_analysis = _resume_analysis_missing_fields(parsed)
                            if missing_analysis:
                                raise ValueError(
                                    "ResumeAnalysis contains defaulted or incomplete fields: "
                                    + ", ".join(missing_analysis)
                                )
                        if role == "profile" and _resume_import_is_incomplete(
                            payload.get("resume_text", ""), parsed
                        ):
                            if resume_candidate is not None:
                                parsed = _merge_resume_import(resume_candidate, parsed)
                            resume_candidate = parsed
                            missing = _resume_import_missing_fields(
                                payload.get("resume_text", ""), parsed
                            )
                            if not missing:
                                return parsed
                            if attempt == attempts - 1:
                                raise ModelUnavailable(
                                    "OpenAI-compat вернул неполный ResumeImportData после repair-pass"
                                )
                            messages[0]["content"] += (
                                f" Обязательный targeted repair: пропущены поля/разделы {missing}. "
                                "Найди их в исходном тексте и заполни явно. Особенно проверь заголовки "
                                "Образование и Языки: верни все записи, даже если они находятся после "
                                "опыта работы. Не оставляй эти поля пустыми при наличии текста раздела."
                            )
                            raise ValueError("ResumeImportData contains defaulted fields")
                        if (
                            role == "profile"
                            and schema.__name__ == "ResumeImportData"
                            and resume_candidate is not None
                        ):
                            parsed = _merge_resume_import(resume_candidate, parsed)
                        return parsed
                    except (ValidationError, ValueError) as exc:
                        if attempt == attempts - 1:
                            raise ModelUnavailable(
                                "OpenAI-compat вернул неполный или некорректный JSON "
                                f"после повторной попытки: {exc}"
                            ) from exc
                    # Retry with a larger explicit repair prompt
                    if role == "profile" and schema.__name__ == "ResumeImportData":
                        messages[0]["content"] += (
                            " Предыдущий JSON не прошёл проверку. Повтори полный ResumeImportData: "
                            "извлеки каждый явно присутствующий элемент из разделов education, languages, "
                            "experiences, skills, desired_title, employment_types, work_formats и "
                            "business_trips. Пустой массив запрещён, если соответствующий раздел есть в "
                            "исходном тексте. Не выбирай значения по умолчанию и обязательно заверши JSON."
                        )
                    else:
                        messages[0]["content"] += (
                            f" Предыдущий JSON не прошёл проверку. Повтори полный объект схемы "
                            f"{schema.__name__}, сохрани все обязательные поля и заверши JSON без markdown."
                        )
            except ModelUnavailable:
                raise
            except APIError as exc:
                raise ModelUnavailable(f"OpenAI-compat API недоступен: {exc}") from exc

    def _mock(self, role: str, payload: dict, schema: type[T]) -> T:
        from backend.schemas.domain import (
            CoverLetterDraft,
            FlagMatch,
            JobEvaluation,
            MatchAssessment,
            PolicyCompilation,
            PolicyFilterResult,
            ResumeAnalysis,
            ScoreComponent,
            WorkFormatAssessment,
        )

        if schema is PolicyCompilation:
            request = str(payload.get("request_text", ""))
            lower = request.lower()
            green = []
            red = []
            if "gamedev" in lower or "game dev" in lower:
                green.append("Вакансия связана с GameDev")
            if "продаж" in lower or "sales" in lower:
                red.append("Вакансия связана с продажами")
            return schema.model_validate(
                {
                    "green_flags": green,
                    "red_flags": red,
                    "flag_confidence_threshold": 0.70,
                }
            )
        if schema is PolicyFilterResult:
            job = payload.get("job", {})
            text = " ".join(
                str(job.get(key, ""))
                for key in ("title", "description", "responsibilities", "required_skills")
            ).lower()
            policy = payload.get("policy", {})
            greens = [
                FlagMatch(
                    flag=flag,
                    confidence=1 if any(part in text for part in flag.lower().split()[-1:]) else 0,
                    evidence=[str(job.get("description", ""))[:240]] if any(part in text for part in flag.lower().split()[-1:]) else [],
                    matched=any(part in text for part in flag.lower().split()[-1:]),
                    verdict="present" if any(part in text for part in flag.lower().split()[-1:]) else "absent",
                )
                for flag in policy.get("green_flags", [])
            ]
            reds = [
                FlagMatch(
                    flag=flag,
                    confidence=1 if "продаж" in text and "продаж" in flag.lower() else 0,
                    evidence=[str(job.get("description", ""))[:240]] if "продаж" in text and "продаж" in flag.lower() else [],
                    matched="продаж" in text and "продаж" in flag.lower(),
                    verdict="present" if "продаж" in text and "продаж" in flag.lower() else "absent",
                )
                for flag in policy.get("red_flags", [])
            ]
            formats = list(payload.get("candidate_work_formats", []))
            vacancy_format = job.get("work_format")
            if not vacancy_format:
                format_patterns = {
                    "remote": ("удален", "удалён", "remote"),
                    "hybrid": ("гибрид", "hybrid"),
                    "office": ("офис", "очно", "на месте", "office"),
                    "mobile": ("разъезд", "мобильн", "mobile"),
                    "rotational": ("вахт", "rotational"),
                }
                vacancy_format = next(
                    (
                        format_name
                        for format_name, patterns in format_patterns.items()
                        if any(pattern in text for pattern in patterns)
                    ),
                    None,
                )
            compatible = None if not vacancy_format or not formats else vacancy_format in formats
            return schema.model_validate(
                {
                    "green_flags": [item.model_dump() for item in greens],
                    "red_flags": [item.model_dump() for item in reds],
                    "work_format": WorkFormatAssessment(
                        compatible=compatible,
                        confidence=1 if compatible is not None else 0,
                        vacancy_format=vacancy_format,
                        candidate_formats=formats,
                    ).model_dump(),
                    "reason": "Детерминированный mock-фильтр",
                }
            )
        if schema is ResumeAnalysis:
            job = payload.get("job", {})
            resumes = payload.get("resumes", [])
            titles = [str(item.get("desired_title") or "").lower() for item in resumes]
            title = str(job.get("title", "")).lower()
            title_match = 1 if title and any(title in item or item in title for item in titles if item) else 0
            skills = {str(skill).lower() for item in resumes for skill in item.get("skills", [])}
            required = {str(skill).lower() for skill in job.get("required_skills", [])}
            skill_match = len(required & skills) / len(required) if required else 0
            description = str(job.get("description", ""))[:240]
            title_evidence = str(job.get("title", ""))
            level = next(
                (
                    name
                    for name, markers in {
                        "junior": ("junior", "джун", "младш"),
                        "middle": ("middle", "мидл"),
                        "senior": ("senior", "сеньор", "старш"),
                    }.items()
                    if any(marker in f"{title} {description.lower()}" for marker in markers)
                ),
                None,
            )

            def mock_assessment(match, confidence, evidence):
                return MatchAssessment(
                    match=match,
                    confidence=confidence,
                    explanation="Детерминированная mock-оценка",
                    evidence=[evidence] if evidence and match > 0 else [],
                ).model_dump()

            return schema.model_validate(
                {
                    "vacancy_seniority": level,
                    "title": mock_assessment(title_match, 0.9, title_evidence),
                    "tasks": mock_assessment(0.5, 0.7, description),
                    "industry": mock_assessment(0.5, 0.7, description),
                    "required_years": mock_assessment(0.5, 0.7, description),
                    "seniority": mock_assessment(0.5 if level else 0, 0.7, title_evidence),
                    "languages": mock_assessment(0.5, 0.7, description),
                    "skills": mock_assessment(
                        skill_match,
                        0.9,
                        next(iter(required & skills), ""),
                    ),
                    "category": job.get("title", "Вакансия"),
                    "reason": "Детерминированный mock-анализ резюме",
                }
            )

        if schema is JobEvaluation:
            job = payload["job"]
            has_test = bool(job.get("has_test_assignment"))
            criteria = payload["policy"]["scoring_criteria"]
            ratio = 0.35 if has_test else (0.88 if "Python" in job.get("description", "") else 0.68)
            breakdown = [
                ScoreComponent(
                    key=item["key"],
                    title=item["title"],
                    points=round(item["max_points"] * ratio),
                    max_points=item["max_points"],
                    explanation="Детерминированная mock-оценка",
                    evidence=[],
                ).model_dump()
                for item in criteria
            ]
            score = sum(item["points"] for item in breakdown)
            return schema.model_validate({"decision": "apply" if score >= payload["policy"]["score_threshold"] else "skip", "score": score, "confidence": 0.9 if score >= 80 else 0.72, "category": job.get("title", "Вакансия"), "score_breakdown": breakdown, "has_test_assignment": has_test, "requires_manual_review": False, "reason": "Детерминированная mock-оценка", "positive_evidence": [], "negative_evidence": [], "missing_requirements": [], "hard_rule_violations": []})
        if role == "profile" and schema.__name__ in {
            "CandidateProfileData",
            "PersonalProfileData",
            "ResumeData",
        }:
            text = payload.get("resume_text", "")
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            fields = getattr(schema, "model_fields", {})
            values = {}
            if "full_name" in fields:
                values["full_name"] = lines[0] if lines else None
            if "summary" in fields:
                values["summary"] = " ".join(lines[1:3]) or None
            if "desired_title" in fields:
                values["desired_title"] = next(
                    (line for line in lines if "product manager" in line.lower()),
                    None,
                )
            if "resume_text" in fields:
                values["resume_text"] = text
            return schema.model_validate(values)
        if role == "profile" and schema.__name__ == "ResumeImportData":
            text = payload.get("resume_text", "")
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            title = next(
                (line for line in lines if "product manager" in line.lower()),
                None,
            )
            return schema.model_validate(
                {
                    "profile": {"full_name": lines[0] if lines else None},
                    "resume": {
                        "name": "Импортированное резюме",
                        "desired_title": title,
                        "about": " ".join(lines[1:3]),
                    },
                }
            )
        if schema is CoverLetterDraft:
            vacancy = payload.get("vacancy", {})
            return schema.model_validate(
                {
                    "text": (
                        f"Здравствуйте! Меня заинтересовала вакансия «{vacancy.get('title', 'Product Manager')}». "
                        "Мой опыт управления продуктом, проверки гипотез и работы с командой "
                        "разработки соответствует ключевым задачам позиции. Буду рад обсудить "
                        "возможный вклад в развитие продукта на интервью."
                    )
                }
            )
        raise ValueError(f"Mock provider has no fixture for role={role}, schema={schema.__name__}")
