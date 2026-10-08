"""Semantic review of candidate-specific factual claims in cover letters."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, Field

LETTER_CLAIMS_VERSION = "candidate-claims-v1"


class CandidateClaimEvidence(BaseModel):
    claim_span: str = Field(min_length=1, strict=True)
    resume_index: int = Field(ge=0, strict=True)
    source_quote: str = Field(min_length=1, strict=True)


class CandidateClaimCheck(BaseModel):
    all_candidate_claims_supported: bool = Field(strict=True)
    confidence: float = Field(ge=0, le=1, strict=True)
    unsupported_claims: list[str]
    evidence: list[CandidateClaimEvidence]


class CandidateClaimValidationError(ValueError):
    """The checker completed, but did not validate the candidate facts."""


_ASPIRATION_OR_COURTESY_RE = re.compile(
    r"(?iu)\b(?:я\s+(?:хочу|готов[а]?|буду\s+рад[а]?|рад[а]?|надеюсь|"
    r"считаю|уверен[а]?|заинтересован[а]?|открыт[а]?|приглашаю)|"
    r"буду\s+рад[а]?|мне\s+интересн\w*|готов[а]?\s+обсудить|"
    r"считаю\s+себя\s+подходящ\w*|уверен[а]?,?\s+что\s+стану\s+"
    r"отличн\w+\s+кандидат\w*)\b"
)
_DIRECT_SELF_REFERENCE_RE = re.compile(
    r"(?iu)\b(?:я|i)\s+[\wА-ЯЁа-яё-]+|"
    r"\b(?:мой|моя|моё|мои)\s+(?:опыт|стаж|проект|результат|вклад|навык|"
    r"образован\w*|достижени\w*|компетенци\w*|знани\w*|карьер\w*|"
    r"портфолио|работ\w*)\b|"
    r"\bу\s+меня\s+(?!есть\s+(?:желание|интерес|намерение|возможность))"
    r"(?:есть\s+)?[\wА-ЯЁа-яё-]+"
)
_DEFAULT_FACT_FORMAT_RE = re.compile(
    r"(?iu)(?:\bв\s+работе\s+(?:использую|применяю|использовал[а]?)\b|"
    r"\bхорошо\s+знакома?\s+с\b|\bобразование\s*:|\bуровень\s+образования\b|"
    r"\b(?:высшее|среднее|средне-специальное)\s+образование\b|"
    r"\b(?:бакалавр|магистр|специалист|университет|институт)\b|"
    r"\b(?:опыт|стаж)\s+работы\b)"
)


def _semantic_strings(value: Any) -> list[str]:
    """Collect resume text while excluding object keys and metadata labels."""
    result: list[str] = []
    metadata_keys = {
        "availability", "source", "source_id", "source_site", "source_section",
        "source_system", "schema_version", "created_at", "updated_at", "metadata",
        "field_id", "record_id", "resume_id", "id",
    }

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            # Availability and schema labels are not evidence. Read only the
            # semantic value of normalized field wrappers.
            if "value" in item and "availability" in item:
                if item.get("availability") == "present":
                    visit(item.get("value"))
                return
            for key, child in item.items():
                if str(key).casefold() not in metadata_keys:
                    visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)
        elif isinstance(item, str) and item.strip():
            result.append(item.strip())

    visit(value)
    return result


def _normalized_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _has_candidate_fact_claim(text: str) -> bool:
    # Names represented by private placeholders and statements of intent,
    # courtesy, or subjective fit are not professional-history evidence.
    if re.search(r"(?iu)\b(?:я|i)\s*\{\{\s*(?:full_name|name)\s*\}\}", text):
        return False
    without_aspiration = _ASPIRATION_OR_COURTESY_RE.sub(" ", text)
    return bool(
        _DIRECT_SELF_REFERENCE_RE.search(without_aspiration)
        or _DEFAULT_FACT_FORMAT_RE.search(without_aspiration)
    )


async def validate_candidate_claims(
    gateway: Any,
    letter: str,
    resumes: Sequence[Any],
) -> None:
    """Review factual claims against professional resume text only.

    The vacancy, template, preferences, and employer instructions are
    intentionally absent from this review payload. The semantic decision is
    model-assisted rather than a formal proof; local checks independently
    validate each returned span, resume index, and source quote.
    """
    payload = {"letter": str(letter), "resumes": list(resumes)}
    result = await gateway.structured("letter_claim_check", payload, CandidateClaimCheck)
    if not isinstance(result, CandidateClaimCheck):
        result = CandidateClaimCheck.model_validate(result)

    valid = (
        result.all_candidate_claims_supported is True
        and result.confidence >= 0.9
        and not result.unsupported_claims
    )
    source_texts = [_semantic_strings(resume) for resume in resumes]
    for evidence in result.evidence:
        if evidence.claim_span not in letter:
            valid = False
            break
        if evidence.resume_index >= len(source_texts):
            valid = False
            break
        quote = _normalized_text(evidence.source_quote)
        if not quote or not any(quote in _normalized_text(source) for source in source_texts[evidence.resume_index]):
            valid = False
            break
    fact_sentences = [
        sentence for sentence in re.split(r"(?<=[.!?])\s+|\n+", letter)
        if _has_candidate_fact_claim(sentence)
    ]
    if any(
        not any(evidence.claim_span in sentence for evidence in result.evidence)
        for sentence in fact_sentences
    ):
        valid = False
    if not valid:
        raise CandidateClaimValidationError("Кандидатские факты не подтверждены выбранным резюме")
