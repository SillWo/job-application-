import pytest
from pydantic import ValidationError

from backend.api.router import SessionCreate
from backend.persistence.models import JobSession


def payload(**overrides: object) -> dict[str, object]:
    return {"profile_id": 1, "adapter_id": "hh", **overrides}


def test_session_has_canonical_gate_map() -> None:
    assert SessionCreate.model_validate(payload()).minimum_scores == {
        "tasks": 2,
        "skills": 1,
        "experience_depth": 1,
        "role_match": 1,
        "industry": 2,
        "special_requirements": 1,
    }


def test_all_configurable_gates_are_preserved() -> None:
    result = SessionCreate.model_validate(payload(minimum_scores={
        "tasks": 4,
        "skills": 2,
        "experience_depth": 3,
        "role_match": 4,
        "industry": 1,
        "special_requirements": 1,
    }))
    assert result.minimum_scores == {
        "tasks": 4,
        "skills": 2,
        "experience_depth": 3,
        "role_match": 4,
        "industry": 1,
        "special_requirements": 1,
    }


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("tasks", 0),
        ("tasks", 5),
        ("skills", 0),
        ("skills", 3),
        ("role_match", True),
        ("industry", False),
        ("special_requirements", 2),
    ],
)
def test_gate_values_are_strict(key: str, value: object) -> None:
    with pytest.raises(ValidationError):
        SessionCreate.model_validate(payload(minimum_scores={key: value}))


@pytest.mark.parametrize(
    "key", ["title", "required_years", "languages", "unknown"]
)
def test_unknown_or_non_configurable_gate_is_rejected(key: str) -> None:
    with pytest.raises(ValidationError):
        SessionCreate.model_validate(payload(minimum_scores={key: 1}))


def test_removed_work_conditions_is_named_when_special_requirements_is_valid() -> None:
    with pytest.raises(ValidationError, match="work_conditions"):
        SessionCreate.model_validate(payload(minimum_scores={
            "special_requirements": 1,
            "work_conditions": 1,
        }))


def test_aggregate_threshold_is_rejected_and_absent_from_sessions() -> None:
    with pytest.raises(ValidationError):
        SessionCreate.model_validate(payload(score_threshold=70))

    assert "score_threshold" not in JobSession.__table__.c
