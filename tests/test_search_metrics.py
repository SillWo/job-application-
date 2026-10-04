import json

import pytest
from test_workflow_non_captcha_continuation import FakeAdapter, run_workflow
from test_workflow_non_captcha_continuation import runtime as metric_runtime

from backend.adapters.base.protocol import JobRef
from backend.persistence.models import BrowserEvent, JobSession, Vacancy
from backend.services import search_metrics as metrics

runtime = metric_runtime


@pytest.mark.asyncio
async def test_overlap_is_unjudged_and_report_survives_version_switch(runtime, monkeypatch):
    factory, ident = runtime
    with factory() as db:
        db.add(Vacancy(source="fake", external_id="old", url="https://fake/old", title="Old", state="REJECTED_BY_MODEL"))
        db.commit()
    refs = [JobRef(external_id=key, url=f"https://fake/{key}") for key in ("old", "new", "new")]
    await run_workflow(runtime, monkeypatch, FakeAdapter(refs))
    with factory() as db:
        item = db.get(JobSession, ident)
        report = item.recovery["measurement_report"]
        assert report["raw_discoveries"] == 3
        assert report["unique_discovered"] == 2
        assert report["duplicate_discoveries"] == 1
        assert report["historical_overlap"] == report["unjudged"] == 1
        assert report["judged"] == 1
        assert report["relevant"] == 0
        identity = report["identity"]
        monkeypatch.setattr(metrics, "HH_SEARCH_VERSION", "changed")
        metrics.initialize(db, item, {}, [])
        assert item.recovery["measurement_identity"] == identity
        assert report["stages"]["browser.extract_job"]["calls"] == 1


def test_relevance_counts_before_application_and_duplicates_do_not_inflate(runtime):
    factory, ident = runtime
    with factory() as db:
        for kind, data in [
            ("metric_discovery", {"source": "rec", "ids": ["1", "2"]}),
            ("metric_discovery", {"source": "query", "ids": ["1", "3"]}),
            ("evaluation", {"external_id": "1", "decision": "apply"}),
            ("evaluation", {"external_id": "1", "decision": "apply"}),
            ("evaluation", {"external_id": "2", "decision": "skip"}),
        ]:
            db.add(BrowserEvent(session_id=ident, event_type=kind, message="fixture", data=data))
        db.commit()
        report = metrics.summary(db, db.get(JobSession, ident))
        assert report["relevant"] == 1
        assert report["judged"] == 2
        assert report["unjudged"] == 1
        assert report["relevant_per_discovered"] == 1 / 3
        assert report["relevant_per_judged"] == 1 / 2
        assert report["sources"]["rec"]["relevant"] == 1
        assert report["applications"]["submitted"] == 0


def test_model_event_summary_aggregates_durations_repairs_and_recovery_without_ids(runtime):
    factory, ident = runtime
    diagnostic_id = "private-diagnostic@example.test"
    base = {"stage": "application_answers", "role": "application_answers",
            "vacancy_id": 987, "diagnostic_id": diagnostic_id, "attempt": 2, "retry_count": 1}
    events = [
        ("metric_model_queue", {**base, "queue_seconds": 1.25}),
        # Duplicate persistence of the same logical attempt must count once.
        ("metric_model_queue", {**base, "queue_seconds": 1.25}),
        ("metric_model_provider", {**base, "provider_seconds": 2.5}),
        ("metric_model_provider", {**base, "provider_seconds": 2.5}),
        ("metric_model_recovery", base),
        ("metric_model_recovery", base),
        ("metric_model_repair", {**base, "category": "safety", "generation": 1}),
        ("metric_model_repair", {**base, "category": "safety", "generation": 1}),
        ("metric_model_repair", {**base, "category": "private-diagnostic@example.test", "generation": 2}),
        ("metric_tokens", {**base, "prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15}),
    ]
    with factory() as db:
        for kind, data in events:
            db.add(BrowserEvent(session_id=ident, event_type=kind, message="fixture", data=data))
        db.commit()

        report = metrics.summary(db, db.get(JobSession, ident))

        assert report["model_metrics"] == {
            "queue": {"count": 1, "seconds": 1.25},
            "provider": {"count": 1, "seconds": 2.5},
            "repair": {"count": 1, "by_category": {"safety": 1}, "by_ordinal": {"1": 1}},
            "recovery": {"count": 1},
        }
        assert report["token_usage"] == {
            "prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15, "reported_calls": 1,
        }
        serialized = json.dumps(report)
        assert diagnostic_id not in serialized
        assert "application_answers" not in serialized
        assert "987" not in serialized


def test_model_metrics_count_events_without_duration_and_filter_unknown_repair_categories(runtime):
    factory, ident = runtime
    with factory() as db:
        db.add_all([
            BrowserEvent(session_id=ident, event_type="metric_model_queue", message="fixture",
                         data={"stage": "resume", "attempt": 1}),
            BrowserEvent(session_id=ident, event_type="metric_model_provider", message="fixture",
                         data={"stage": "resume"}),
            BrowserEvent(session_id=ident, event_type="metric_model_repair", message="fixture",
                         data={"category": "unknown-user-value", "generation": 1}),
        ])
        db.commit()

        report = metrics.summary(db, db.get(JobSession, ident))

        assert report["model_metrics"]["queue"] == {"count": 1, "seconds": 0.0}
        assert report["model_metrics"]["provider"] == {"count": 1, "seconds": 0.0}
        assert report["model_metrics"]["repair"] == {
            "count": 0, "by_category": {}, "by_ordinal": {},
        }


def test_provider_repair_kinds_are_aggregated_by_attempt_without_diagnostic_ids(runtime):
    factory, ident = runtime
    base = {"diagnostic_id": "provider-diagnostic", "stage": "evaluation"}
    repairs = [
        {**base, "repair_kind": "unexpected_tool_call", "attempt": 2},
        {**base, "repair_kind": "unexpected_tool_call", "attempt": 2},
        {**base, "repair_kind": "unsafe_model_output", "attempt": 3},
        {**base, "repair_kind": "schema_validation", "attempt": 4},
    ]
    with factory() as db:
        db.add_all([
            BrowserEvent(session_id=ident, event_type="metric_model_repair", message="fixture", data=data)
            for data in repairs
        ])
        db.commit()

        report = metrics.summary(db, db.get(JobSession, ident))

        assert report["model_metrics"]["repair"] == {
            "count": 3,
            "by_category": {
                "unexpected_tool_call": 1,
                "unsafe_model_output": 1,
                "schema_validation": 1,
            },
            "by_ordinal": {"2": 1, "3": 1, "4": 1},
        }
        assert "provider-diagnostic" not in json.dumps(report)


@pytest.mark.asyncio
async def test_stopping_paused_session_freezes_measurements_without_a_running_task(runtime, monkeypatch):
    from backend.api import router

    async def close(_):
        pass

    monkeypatch.setattr(router, "close_browser", close)
    factory, ident = runtime
    with factory() as db:
        item = db.get(JobSession, ident)
        metrics.initialize(db, item, {}, [])
        item.status = "PAUSED"
        db.commit()
        await router.stop_session(ident, db)
        db.refresh(item)
        assert item.recovery["measurement_report"]["status"] == "CANCELLED"
        assert item.recovery["measurement_report"]["finished_at"] is not None
