import pytest
from pydantic import ValidationError

from backend.api.router import SessionCreate
from backend.persistence.models import JobSession


def payload(**overrides: object) -> dict[str, object]:
    return {"profile_id": 1, "adapter_id": "hh", **overrides}

def test_session_has_canonical_gate_map() -> None:
    assert SessionCreate.model_validate(payload()).minimum_scores == {"title": 0, "tasks": 2, "industry": 2, "skills": 2, "required_years": 1, "languages": 1}

def test_fixed_gates_cannot_be_overridden() -> None:
    result = SessionCreate.model_validate(payload(minimum_scores={"tasks": 3, "title": 2, "required_years": 0}))
    assert result.minimum_scores["title"] == 0 and result.minimum_scores["required_years"] == 1 and result.minimum_scores["languages"] == 1

@pytest.mark.parametrize("value", [0, 4, True, False])
def test_configurable_gate_values_are_strict(value: object) -> None:
    with pytest.raises(ValidationError):
        SessionCreate.model_validate(payload(minimum_scores={"tasks": value}))

def test_unknown_gate_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SessionCreate.model_validate(payload(minimum_scores={"unknown": 1}))


def test_aggregate_threshold_is_rejected_and_absent_from_sessions() -> None:
    with pytest.raises(ValidationError):
        SessionCreate.model_validate(payload(score_threshold=70))

    assert "score_threshold" not in JobSession.__table__.c
