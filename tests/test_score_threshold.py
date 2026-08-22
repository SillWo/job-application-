import pytest
from pydantic import ValidationError

from backend.api.router import SessionCreate
from backend.persistence.models import JobSession
from backend.schemas.domain import RELEVANCE_SCORE_THRESHOLD


def _session_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "profile_id": 1,
        "adapter_id": "hh",
    }
    payload.update(overrides)
    return payload


def test_new_session_payload_uses_default_relevance_threshold() -> None:
    assert SessionCreate.model_validate(_session_payload()).score_threshold == 70
    assert SessionCreate.model_validate(_session_payload(score_threshold=70)).score_threshold == 70
    assert RELEVANCE_SCORE_THRESHOLD == 70


@pytest.mark.parametrize("override", [0, 69, 71, 100])
def test_new_session_payload_accepts_client_threshold_override(override: int) -> None:
    assert SessionCreate.model_validate(_session_payload(score_threshold=override)).score_threshold == override


@pytest.mark.parametrize("override", [-1, 101])
def test_new_session_payload_rejects_out_of_range_threshold(override: int) -> None:
    with pytest.raises(ValidationError):
        SessionCreate.model_validate(_session_payload(score_threshold=override))


def test_minimum_scores_are_optional_and_validated() -> None:
    assert SessionCreate.model_validate(_session_payload()).minimum_scores is None
    assert SessionCreate.model_validate(_session_payload(minimum_scores={})).minimum_scores == {}
    assert SessionCreate.model_validate(_session_payload(minimum_scores={"tasks": 2})).minimum_scores == {"tasks": 2}
    with pytest.raises(ValidationError):
        SessionCreate.model_validate(_session_payload(minimum_scores={"tasks": 4}))


def test_session_payload_rejects_removed_mode() -> None:
    with pytest.raises(ValidationError):
        SessionCreate.model_validate(_session_payload(mode="autopilot"))


def test_orm_default_relevance_threshold_is_70() -> None:
    assert str(JobSession.__table__.c.score_threshold.server_default.arg) == "70"
