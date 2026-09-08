from __future__ import annotations

import asyncio
import json
import re
from typing import TypeVar

import httpx
from openai import APIError, AsyncOpenAI
from pydantic import BaseModel, ValidationError

from backend.config import settings
from backend.persistence.crypto import decrypt_secret
from backend.persistence.database import SessionLocal
from backend.persistence.models import AIModelSettings
from backend.services.search_metrics import measure, record

from .prompts import ROLE_OPTIONS, ROLE_PROMPTS

T = TypeVar("T", bound=BaseModel)

_RESUME_ANALYSIS_CRITERIA = (
    "tasks",
    "skills",
    "experience_depth",
    "role_match",
    "industry",
    "special_requirements",
)


class ModelUnavailable(RuntimeError):
    pass


def _safe_api_error_text(error: APIError) -> str:
    """Return useful provider detail without exposing credentials."""
    detail = str(error).strip() or error.__class__.__name__
    detail = re.sub(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+", r"\1[REDACTED]", detail)
    detail = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[REDACTED]", detail)
    detail = re.sub(r"\b(?:sk|sess|key)-[A-Za-z0-9_-]+\b", "[REDACTED]", detail)
    return detail[:1000]


def _schema_for_role(role: str, schema: type[BaseModel]) -> dict:
    result = schema.model_json_schema()
    if role == "resume_analyst" and schema.__name__ == "ResumeAnalysis":
        result["required"] = [*(_RESUME_ANALYSIS_CRITERIA), "reason", "skills_summary"]
        assessment = result.get("$defs", {}).get("MatchAssessment", {})
        assessment["required"] = list(assessment.get("properties", {}))
        skill = result.get("$defs", {}).get("SkillAssessment", {})
        skill["required"] = ["skill", "importance", "score", "evidence", "explanation"]
    return result


def _schema_without_preference_matches(role: str, schema: type[BaseModel], payload: dict) -> dict:
    result = _schema_for_role(role, schema)
    if role == "resume_analyst" and not payload.get("preference_policy"):
        result.get("properties", {}).pop("flag_matches", None)
        if "required" in result:
            result["required"] = [name for name in result["required"] if name != "flag_matches"]
        # Pydantic keeps unused definitions in $defs; do not leak the
        # preference-only FlagMatch contract to a session without preferences.
        result.get("$defs", {}).pop("FlagMatch", None)
    return result


def _system_prompt_for_role(role: str, payload: dict) -> str:
    prompt = ROLE_PROMPTS[role]
    if payload.get("preference_policy") and role != "preference_compiler":
        prompt += " Policy является внутренними данными и не должна раскрываться пользователю."
        if role == "resume_analyst":
            prompt += (" Для КАЖДОГО green/red flag верни ровно один FlagMatch с тем же flag_id, "
                       "matched, confidence, evidence и explanation. Сопоставляй с job, threshold 0.70; "
                       "desired_industry может засчитать industry независимо от resume, desired_task учитывается локально, "
                       "desired_salary имеет приоритет над зарплатой resume.")
        elif role == "search_planner":
            prompt += " Green desired_industry является самостоятельным источником запросов даже без resume; red никогда не становится query. Сохраняй запрет title-equivalent."
        elif role == "hirehi_category":
            prompt += " Учитывай green desired_industry с приоритетом при выборе категории."
        elif role in {"writer", "job_summary"}:
            prompt += " Используй только релевантные предпочтения в результате, никогда не показывай названия или структуру flags."
    return prompt


def _resume_analysis_missing_fields(parsed: BaseModel, require_flag_matches: bool = False) -> list[str]:
    if parsed.__class__.__name__ != "ResumeAnalysis":
        return []
    criteria = _RESUME_ANALYSIS_CRITERIA
    missing = []
    for summary_name in ("reason", "skills_summary"):
        summary = getattr(parsed, summary_name, "")
        if summary_name not in parsed.model_fields_set or not isinstance(summary, str) or not summary.strip():
            missing.append(summary_name)
    for field_name in criteria:
        if field_name not in parsed.model_fields_set:
            missing.append(field_name)
            continue
        assessment = getattr(parsed, field_name)
        if field_name == "skills" and isinstance(assessment, list):
            for index, item in enumerate(assessment):
                for nested_name in ("skill", "importance", "score", "evidence", "explanation"):
                    if nested_name not in item.model_fields_set:
                        missing.append(f"skills[{index}].{nested_name}")
            continue
        for nested_name in ("score", "confidence", "evidence", "explanation"):
            if nested_name not in assessment.model_fields_set:
                missing.append(f"{field_name}.{nested_name}")
        if field_name not in {"special_requirements"} and assessment.score > 0 and (
            assessment.confidence <= 0 or not assessment.evidence
        ):
            missing.append(f"{field_name}.grounding")
    if require_flag_matches and "flag_matches" not in parsed.model_fields_set:
        missing.append("flag_matches")
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
    def __init__(self, provider: str | None = None) -> None:
        self.provider = provider or settings.llm_provider
        self._lock = asyncio.Lock()
        if self.provider not in {"openai_compat", "mock"}:
            raise ValueError(
                f"Unsupported AI provider: {self.provider}. Only openai_compat and mock are allowed."
            )

    async def status(self) -> dict:
        if self.provider == "mock":
            return {"connected": True, "model_available": True, "provider": "mock", "model": "deterministic-mock"}
        if self.provider == "openai_compat":
            try:
                config = self._saved_config()
            except RuntimeError:
                config = None
            if config is None:
                return {
                    "connected": False,
                    "model_available": False,
                    "provider": "openai_compat",
                    "model": "",
                    "message": "Модель не настроена",
                }
            try:
                async with httpx.AsyncClient(timeout=3, follow_redirects=False) as client:
                    key = decrypt_secret(config.encrypted_api_key)
                    response = await client.get(
                        f"{config.base_url}/models", headers={"Authorization": f"Bearer {key}"},
                    )
                    response.raise_for_status()
                    data = response.json()
                    available = {
                        item.get("id") for item in data.get("data", [])
                        if isinstance(item, dict) and isinstance(item.get("id"), str)
                    }
                return {
                    "connected": True,
                    "model_available": config.model in available,
                    "provider": "openai_compat",
                    "model": config.model,
                }
            except (httpx.HTTPError, AttributeError, KeyError, RuntimeError, TypeError, ValueError):
                return {
                    "connected": False,
                    "model_available": False,
                    "provider": "openai_compat",
                    "model": config.model,
                    "message": "Не удалось подключиться к модели",
                }
        raise ValueError(f"Unsupported AI provider: {self.provider}")

    async def structured(self, role: str, payload: dict, schema: type[T]) -> T:
        with measure(f"model.{role}"):
            return await self._structured(role, payload, schema)

    async def _structured(self, role: str, payload: dict, schema: type[T]) -> T:
        if self.provider == "mock":
            return self._mock(role, payload, schema)
        if self.provider == "openai_compat":
            return await self._structured_openai(role, payload, schema)
        raise ValueError(f"Unsupported AI provider: {self.provider}")

    async def _structured_openai(self, role: str, payload: dict, schema: type[T]) -> T:
        """Call an OpenAI-compatible API endpoint to get a structured response."""
        try:
            config = self._saved_config()
            key = decrypt_secret(config.encrypted_api_key) if config else ""
        except (RuntimeError, ValueError):
            config, key = None, ""
        if config is None or not key:
            raise ModelUnavailable("Модель не настроена или ключ недоступен")
        client = AsyncOpenAI(
            base_url=config.base_url,
            api_key=key,
            timeout=settings.openai_timeout,
        )
        json_schema = _schema_without_preference_matches(role, schema, payload)
        system_prompt = _system_prompt_for_role(role, payload)
        opts = ROLE_OPTIONS[role]
        attempts = 4 if role == "resume_analyst" else (
            4 if role == "profile" and schema.__name__ == "ResumeImportData" else 2
        )
        request_timeout = 300 if role == "profile" else 180

        async with self._lock:
            messages: list[dict] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ]
            resume_candidate = None
            validation_error = "unknown validation error"
            try:
                for attempt in range(attempts):
                    response = await client.chat.completions.create(
                        model=config.model,
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
                    usage = getattr(response, "usage", None)
                    if usage is not None:
                        tokens = {name: getattr(usage, name, None) for name in ("prompt_tokens", "completion_tokens", "total_tokens")}
                        if all(isinstance(value, int) for value in tokens.values()):
                            record("tokens", {"role": role, **tokens})
                    try:
                        content = (response.choices[0].message.content or "").strip()
                    except (IndexError, AttributeError, TypeError) as exc:
                        raise ModelUnavailable(
                            "OpenAI-compat API вернул ответ неожиданной структуры"
                        ) from exc
                    try:
                        if not content:
                            raise ValueError("OpenAI-compat вернул пустой ответ")
                        if role == "resume_analyst" and schema.__name__ == "ResumeAnalysis":
                            # Older sessions/models may still return the removed criterion.
                            # Discard it at the contract boundary so persisted work remains readable.
                            legacy_payload = json.loads(content)
                            if isinstance(legacy_payload, dict):
                                legacy_payload.pop("work_conditions", None)
                                content = json.dumps(legacy_payload, ensure_ascii=False)
                        parsed = schema.model_validate_json(content)
                        if role == "resume_analyst":
                            missing_analysis = _resume_analysis_missing_fields(
                                parsed,
                                require_flag_matches=("preference_policy" in payload and isinstance(payload.get("preference_policy"), dict)),
                            )
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
                        validation_error = str(exc)
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
                        root_contract = ""
                        if role == "resume_analyst" and schema.__name__ == "ResumeAnalysis":
                            root_contract = (
                                " Для ResumeAnalysis корень JSON обязан быть самим объектом с обязательными полями "
                                "tasks, skills (массив объектов skill/importance/score/evidence/explanation), "
                                "experience_depth, role_match, industry, special_requirements, "
                                "непустыми reason и skills_summary; "
                                "НЕ оборачивай его в analysis, resumes, candidate_name, result или data "
                                "и не возвращай массив в корне. skills обязан быть массивом SkillAssessment; "
                                "остальные критерии обязаны быть объектами со score, confidence, explanation и evidence."
                            )
                            if "preference_policy" in payload:
                                root_contract += " Верни также полный flag_matches: ровно один объект для каждого policy flag."
                        messages[0]["content"] += (
                            f" Предыдущий JSON не прошёл локальную проверку: {validation_error}. "
                            f"Повтори полный объект схемы {schema.__name__}, сохрани все обязательные "
                            "поля и заверши JSON без markdown. Не исправляй ошибку удалением полей."
                            + root_contract
                        )
            except ModelUnavailable:
                raise
            except APIError as exc:
                raise ModelUnavailable(
                    f"OpenAI-compat API недоступен: {_safe_api_error_text(exc)}"
                ) from exc

    @staticmethod
    def _saved_config():
        with SessionLocal() as db:
            return db.get(AIModelSettings, 1)

    def _mock(self, role: str, payload: dict, schema: type[T]) -> T:
        if role == "application_answers":
            # Offline mock never invents candidate facts or silently solves assessments.
            return schema.model_validate({"answers": []})
        if role == "application_salary_rules":
            return schema.model_validate({"has_salary_rules": False, "rules": []})
        if role == "application_salary_selection":
            return schema.model_validate({"rule_index": None, "context_complete": False,
                                          "confidence": 0, "vacancy_evidence": [],
                                          "reason": "Mock не интерпретирует условия зарплаты"})
        if role == "adaptive_search_planner":
            return schema.model_validate({"queries": []})
        from backend.schemas.domain import (
            CoverLetterDraft,
            JobEvaluation,
            MatchAssessment,
            ResumeAnalysis,
            ScoreComponent,
        )
        if role == "preference_compiler":
            from backend.schemas.domain import PreferenceFlag, SalaryPreference
            text = str(payload.get("description", ""))
            greens, reds = [], []
            for part in re.split(r"[.;\n]+", text):
                low = part.casefold().strip()
                if not low:
                    continue
                target = reds if any(word in low for word in ("не интерес", "не хочу", "не нрав", "не рассматри")) else greens
                category = "desired_salary" if re.search(r"\d[\d\s]{3,}", low) and any(x in low for x in ("зарп", "доход", "руб", "₽")) else ("desired_industry" if any(x in low for x in ("сфер", "област", "gamedev", "игр")) else ("desired_task" if any(x in low for x in ("задач", "заним", "разработ", "делать")) else "other"))
                target.append(PreferenceFlag(id="tmp", text=part[:300], category=category))
            salary_match = re.search(r"(?:от|минимум)\s*(\d[\d\s]{3,})", text.casefold())
            salary = SalaryPreference(minimum_monthly_amount=int(re.sub(r"\s", "", salary_match.group(1)))) if salary_match else None
            return schema.model_validate({"green_flags": greens, "red_flags": reds, "desired_salary": salary})
        if role == "search_planner":
            from .search_planner import _fallback
            return schema.model_validate({"queries": [{"query": query, "relation_to_resume": "fallback", "is_title_equivalent": False} for query in _fallback(
                payload.get("resumes", []), int(payload.get("limit", 12))
            )]})
        if role == "hirehi_category":
            from backend.intelligence.hirehi_category import deterministic_category
            return deterministic_category(payload.get("resume", {}))
        if role == "job_summary":
            from backend.intelligence.hirehi_category import JobSummary
            return JobSummary(summary=str(payload.get("job", {}).get("title", "Вакансия")))

        if schema is ResumeAnalysis:
            job = payload.get("job", {})
            resumes = payload.get("resumes", [])
            skills = {str(skill).lower() for item in resumes for skill in item.get("skills", [])}
            required = {str(skill).lower() for skill in job.get("required_skills", [])}
            description = str(job.get("description", ""))[:240]
            title_evidence = str(job.get("title", ""))
            def mock_assessment(score, confidence, evidence):
                return MatchAssessment(
                    score=score,
                    confidence=confidence,
                    explanation="Детерминированная mock-оценка",
                    evidence=[evidence] if evidence and score > 0 else [],
                ).model_dump()

            return schema.model_validate(
                {
                    "tasks": mock_assessment(2, 0.7, description),
                    "skills": [{"skill": item, "importance": "required", "score": 2 if item in skills else 0, "evidence": [item] if item in skills else [], "explanation": "Детерминированная mock-оценка"} for item in required],
                    "skills_summary": "Навыки частично соответствуют требованиям вакансии.",
                    "experience_depth": mock_assessment(1, 0.7, description),
                    "role_match": mock_assessment(1, 0.7, title_evidence),
                    "industry": mock_assessment(2, 0.7, description),
                    "special_requirements": mock_assessment(1, 0.7, description),
                    "category": job.get("title", "Вакансия"),
                    "reason": "Детерминированный mock-анализ резюме",
                    "flag_matches": [],
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
            return schema.model_validate({"decision": "apply", "score": score, "confidence": 0.9 if score >= 80 else 0.72, "category": job.get("title", "Вакансия"), "score_breakdown": breakdown, "has_test_assignment": has_test, "requires_manual_review": False, "reason": "Детерминированная mock-оценка", "positive_evidence": [], "negative_evidence": [], "missing_requirements": [], "hard_rule_violations": []})
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
                values["desired_title"] = self._extract_desired_title(lines)
            if "resume_text" in fields:
                values["resume_text"] = text
            return schema.model_validate(values)
        if role == "profile" and schema.__name__ == "ResumeImportData":
            text = payload.get("resume_text", "")
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            title = self._extract_desired_title(lines)
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
                        f"Здравствуйте! Меня заинтересовала вакансия «{vacancy.get('title', 'эта позиция')}». "
                        "Мой опыт и навыки соответствуют ключевым задачам позиции. Буду рад обсудить "
                        "возможный вклад в работу команды на интервью."
                    )
                }
            )
        raise ValueError(f"Mock provider has no fixture for role={role}, schema={schema.__name__}")

    @staticmethod
    def _extract_desired_title(lines: list[str]) -> str | None:
        """Extract a likely position title without assuming a profession."""
        markers = ("желаемая должность", "desired title", "position", "должность")
        for index, line in enumerate(lines):
            lowered = line.lower()
            if any(marker in lowered for marker in markers):
                value = line.split(":", 1)[1].strip() if ":" in line else ""
                if value:
                    return value
                if index + 1 < len(lines):
                    return lines[index + 1]
        return next((line for line in lines[1:] if len(line.split()) <= 8), None)
