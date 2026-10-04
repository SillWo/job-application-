from backend.persistence.models import BrowserEvent, JobSession
from backend.services import search_metrics
from backend.services.compare_search import _matched_pairs, assess_rollout

pytest_plugins = ("test_workflow_non_captcha_continuation",)


def _report(session_id, algorithm, *, early, final=None, fn=0, truth=100):
    final = early if final is None else final
    return {
        "session_id": session_id,
        "identity": {"algorithm": algorithm, "criteria_hash": "same", "model": "test-model"},
        "N": 200,
        "N_at": {"50": 50, "100": 100, "200": 200},
        "R_at": {"50": early // 2, "100": early, "200": final},
        "audit": {"selected": truth, "relevant": truth, "false_negatives": fn,
                   "fnr": fn / truth if truth else None},
    }


def test_rollout_gate_passes_only_with_ten_compatible_pairs():
    reports = []
    for index in range(10):
        reports.extend([_report(index * 2, "hirehi_v1", early=10, final=20),
                        _report(index * 2 + 1, "hirehi_adaptive_v3", early=12, final=24)])
    result = assess_rollout(reports)
    assert result["verdict"] == "pass"
    assert result["matched_pairs"] == 10
    assert result["metrics"]["aggregate_early_R@100_lift"] == 0.2


def test_rollout_gate_is_insufficient_before_minimum_pairs():
    result = assess_rollout([_report(1, "hirehi_v1", early=10),
                             _report(2, "hirehi_adaptive_v3", early=20)])
    assert result["verdict"] == "insufficient_evidence"


def test_rollout_gate_fails_audit_or_quality_gate():
    reports = []
    for index in range(10):
        reports.extend([_report(index * 2, "hirehi_v1", early=10, final=20),
                        _report(index * 2 + 1, "hirehi_adaptive_v3", early=10, final=18, fn=6)])
    result = assess_rollout(reports)
    assert result["verdict"] == "fail"
    assert result["gates"]["audit_fnr_le_5_percent"] is False


def test_rollout_with_empty_checkpoints_is_insufficient_not_an_exception():
    baseline = _report(1, "hirehi_v1", early=10)
    adaptive = _report(2, "hirehi_adaptive_v3", early=12)
    baseline["N"] = adaptive["N"] = 10
    baseline["N_at"] = adaptive["N_at"] = {}
    result = assess_rollout([baseline, adaptive])
    assert result["verdict"] == "insufficient_evidence"
    assert result["complete_pairs"] == 0


def test_matched_pairs_accept_existing_v1_but_ignore_unknown_and_hh():
    reports = []
    for index in range(10):
        reports.extend([_report(index * 2, "existing_v1", early=10),
                        _report(index * 2 + 1, "hirehi_adaptive_v3", early=12)])
    reports.extend([_report(100, "hh_adaptive_v1", early=12),
                    _report(101, "future_algorithm", early=12)])
    pairs = _matched_pairs(reports)
    assert len(pairs) == 10
    assert all(pair["baseline"]["identity"]["algorithm"] == "existing_v1" for pair in pairs)


def test_hirehi_report_uses_unique_discovered_and_complete_evaluation_denominators(runtime):
    factory, _ = runtime
    with factory() as db:
        item = JobSession(adapter_id="hirehi", application_limit=None, status="STOPPED", counters={})
        db.add(item)
        db.flush()
        db.add_all([
            BrowserEvent(session_id=item.id, event_type="metric_discovery", message="fixture",
                         data={"source_id": "q", "ids": ["a", "b", "b", "c"], "raw_count": 4}),
            BrowserEvent(session_id=item.id, event_type="metric_discovery", message="fixture",
                         data={"source_id": "q", "ids": ["c"], "raw_count": 1}),
            BrowserEvent(session_id=item.id, event_type="metric_evaluation", message="fixture",
                         data={"external_id": "a", "decision": "apply"}),
            BrowserEvent(session_id=item.id, event_type="metric_evaluation", message="fixture",
                         data={"external_id": "b", "decision": "skip"}),
            BrowserEvent(session_id=item.id, event_type="metric_evaluation", message="fixture",
                         data={"external_id": "b", "decision": "apply"}),
        ])
        db.commit()
        report = search_metrics.summary(db, item)
        assert (report["raw_discoveries"], report["D"], report["N"], report["R"]) == (5, 3, 2, 1)
        assert report["R/N"] == 0.5
        assert report["N/D"] == 2 / 3


def test_hirehi_weak_prior_caps_each_source_and_preserves_rate(runtime):
    factory, _ = runtime
    with factory() as db:
        previous = JobSession(adapter_id="hirehi", application_limit=None, status="STOPPED", counters={},
                              recovery={"measurement_identity": {"criteria_hash": "same", "algorithm_version": "v3"}})
        current = JobSession(adapter_id="hirehi", application_limit=None, status="CREATED", counters={},
                             recovery={"measurement_identity": {"criteria_hash": "same", "algorithm_version": "v3"}})
        db.add_all([previous, current])
        db.flush()
        events = []
        for source in ("a", "b"):
            for index in range(20):
                key = f"{source}-{index}"
                events.extend([
                    BrowserEvent(session_id=previous.id, event_type="metric_discovery", message="fixture",
                                 data={"source_id": source, "ids": [key]}),
                    BrowserEvent(session_id=previous.id, event_type="metric_evaluation", message="fixture",
                                 data={"external_id": key, "source_id": source,
                                       "decision": "apply" if index < 10 else "skip"}),
                ])
        db.add_all(events)
        db.commit()
        prior = search_metrics.load_hirehi_weak_prior(db, current)
        for source in ("a", "b"):
            assert prior[source]["total"] <= 5
            assert prior[source]["successes"] == prior[source]["failures"] == 2.5


def test_hirehi_audit_fnr_uses_selected_sample_and_snapshot_is_authoritative(runtime):
    factory, _ = runtime
    with factory() as db:
        item = JobSession(adapter_id="hirehi", application_limit=None, status="STOPPED", counters={})
        db.add(item)
        db.flush()
        db.add_all([
            BrowserEvent(session_id=item.id, event_type="metric_discovery", message="fixture",
                         data={"source_id": "q", "ids": ["a", "b"]}),
            BrowserEvent(session_id=item.id, event_type="metric_evaluation", message="fixture",
                         data={"external_id": "a", "source_id": "q", "decision": "apply"}),
            BrowserEvent(session_id=item.id, event_type="metric_hirehi_snapshot", message="fixture",
                         data={"source_id": "q", "raw": 2, "unique": 2, "analyzed": 0,
                               "relevant": 0, "cost": 0,
                               "selected_ids": ["x", "y"], "false_negative_ids": ["x"]}),
            BrowserEvent(session_id=item.id, event_type="metric_audit", message="fixture",
                         data={"selected_ids": ["x", "y"], "relevant_ids": ["x"],
                               "false_negative_ids": ["x"]}),
            BrowserEvent(session_id=item.id, event_type="metric_audit", message="fixture",
                         data={"selected_ids": ["x", "y"], "relevant_ids": ["x"],
                               "false_negative_ids": ["x"]}),
        ])
        db.commit()
        report = search_metrics.summary(db, item)
        assert report["audit"]["fnr"] == 0.5
        assert report["sources"]["q"]["analyzed"] == 0
        assert report["sources"]["q"]["relevant"] == 0


def test_hirehi_prior_prefers_latest_overall_snapshot_per_source(runtime):
    factory, _ = runtime
    with factory() as db:
        previous = JobSession(adapter_id="hirehi", application_limit=None, status="STOPPED", counters={},
                              recovery={"measurement_identity": {"criteria_hash": "same", "algorithm_version": "v3"}})
        current = JobSession(adapter_id="hirehi", application_limit=None, status="CREATED", counters={},
                             recovery={"measurement_identity": {"criteria_hash": "same", "algorithm_version": "v3"}})
        db.add_all([previous, current])
        db.flush()
        db.add_all([
            BrowserEvent(session_id=previous.id, event_type="metric_hirehi_snapshot", message="fixture",
                         data={"sources": {"query:one": {"analyzed": 2, "relevant": 2}}}),
            BrowserEvent(session_id=previous.id, event_type="metric_hirehi_snapshot", message="fixture",
                         data={"sources": {"query:one": {"raw": 7, "analyzed": 4, "relevant": 1},
                                             "query:two": {"analyzed": 10, "relevant": 5}}}),
        ])
        db.commit()
        prior = search_metrics.load_hirehi_weak_prior(db, current)
        assert prior["query:one"]["successes"] == 1
        assert prior["query:one"]["failures"] == 3
        assert prior["query:two"]["successes"] == 2.5
        assert prior["query:two"]["failures"] == 2.5


def test_hirehi_summary_accepts_overall_snapshot_integration_shape(runtime):
    factory, _ = runtime
    with factory() as db:
        item = JobSession(adapter_id="hirehi", application_limit=None, status="STOPPED", counters={})
        db.add(item)
        db.flush()
        db.add(BrowserEvent(
            session_id=item.id, event_type="metric_hirehi_snapshot", message="fixture",
            data={"sources": {"query:one": {"raw": 7, "unique": 5, "analyzed": 2, "relevant": 1},
                                "query:two": {"raw": 5, "unique": 4, "analyzed": 1, "relevant": 0}},
                  "metrics": {"D": 9, "N": 3, "R": 1},
                  "audit": {"selected_ids": ["audit-1"], "false_negative_ids": []},
                  "rejection_reasons": {"missing_skill": 2}},
        ))
        db.commit()
        report = search_metrics.summary(db, item)
        assert report["raw_discoveries"] == 12
        assert report["D"] == 9
        assert report["exact_duplicates"] == 3
        assert report["sources"]["query:one"]["analyzed"] == 2
        assert report["audit"]["selected"] == 1
