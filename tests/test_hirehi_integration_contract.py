from types import SimpleNamespace

from backend.orchestrator import workflow
from backend.schemas.domain import JobEvaluation


def _evaluation(**kwargs):
    return JobEvaluation(
        decision=kwargs.pop("decision", "skip"),
        score=20,
        confidence=kwargs.pop("confidence", 0.9),
        category="fixture",
        reason=kwargs.pop("reason", "fixture"),
        **kwargs,
    )


def test_hirehi_reason_mapper_uses_bounded_known_categories():
    assert workflow._hirehi_evaluation_reason(_evaluation(minimum_score_violations=["skills"])) == "hard_skill_missing"
    assert workflow._hirehi_evaluation_reason(_evaluation(minimum_score_violations=["role_match"])) == "wrong_role"
    assert workflow._hirehi_evaluation_reason(_evaluation(reason="arbitrary vacancy text")) == "other"


def test_hirehi_snapshot_has_actual_metrics_contract_without_ids():
    source = SimpleNamespace(
        family="query", raw_discovered=4, unique_discovered=2,
        analyzed=2, relevant=1, failures=0, exhausted=False,
    )
    engine = SimpleNamespace(
        algorithm_version="hirehi_adaptive_v3",
        scheduler=SimpleNamespace(sources={"source-key": source}),
        rejection_reasons={"other": 1},
        metrics=lambda: {"D": 4, "N": 2, "R": 1, "R/N": 0.5, "audit": {"selected": ["secret-id"]}},
        audit_metrics=lambda: {"eligible": 2, "selected": 1, "audited_relevant": 0, "fnr": 0.0},
    )
    payload = workflow._hirehi_snapshot_data(engine)
    assert set(payload) == {"sources", "metrics", "audit", "rejection_reasons"}
    assert payload["sources"]["source-key"]["analyzed"] == 2
    assert payload["metrics"]["N"] == 2
    assert "secret-id" not in str(payload)


def test_hirehi_prior_changes_posterior_without_session_n_r():
    source = SimpleNamespace(prior_successes=0.0, prior_failures=0.0, spec={"query": "role"})
    class Engine:
        scheduler = SimpleNamespace(sources={"source-key": source})
        def search_checkpoint(self):
            return {"algorithm_version": "hirehi_adaptive_v3", "criteria_hash": "same"}
    workflow._apply_hirehi_weak_prior(Engine(), {"role": {"successes": 3, "failures": 2}})
    assert source.prior_successes == 1.0
    assert source.prior_failures == 1.0
    assert source.analyzed == 0 if hasattr(source, "analyzed") else True
