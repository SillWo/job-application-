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
            ("evaluation", {"external_id": "2", "decision": "manual_review"}),
        ]:
            db.add(BrowserEvent(session_id=ident, event_type=kind, message="fixture", data=data))
        db.commit()
        report = metrics.summary(db, db.get(JobSession, ident))
        assert report["relevant"] == report["judged"] == 1
        assert report["unjudged"] == 2
        assert report["relevant_per_discovered"] == 1 / 3
        assert report["relevant_per_judged"] == 1
        assert report["sources"]["rec"]["relevant"] == 1
        assert report["applications"]["submitted"] == 0


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
        assert item.recovery["measurement_report"]["status"] == "STOPPED"
        assert item.recovery["measurement_report"]["finished_at"] is not None
