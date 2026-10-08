"""Privacy-safe, append-only measurements for search experiments.

The HireHi adapter writes immutable ``BrowserEvent`` rows. This module only
aggregates those rows; it never reads cookies, browser storage, or resume PII.
The old HH measurements are intentionally kept in the public report shape.
"""
from __future__ import annotations

import hashlib
import json
import math
import subprocess
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

from sqlalchemy import select

from backend.config import settings
from backend.orchestrator.search_version import HH_SEARCH_VERSION
from backend.persistence.models import AIModelSettings, BrowserEvent, JobSession, Vacancy

_pending: ContextVar[list | None] = ContextVar("search_measurements", default=None)


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


def begin():
    return _pending.set([])


def end(token):
    _pending.reset(token)


def record(kind: str, data: dict) -> None:
    queue = _pending.get()
    if queue is not None:
        queue.append((kind, data, datetime.now(timezone.utc)))


def flush(db, session_id: int) -> bool:
    queue = _pending.get()
    if queue:
        overlap_ids: list[str] = []
        overlap_raw = 0
        for kind, data, timestamp in queue:
            if kind == "overlap":
                ids = _event_ids(data)
                if not ids and data.get("external_id") is not None:
                    ids = [str(data["external_id"])]
                overlap_ids.extend(ids)
                count = data.get("raw_count")
                overlap_raw += (
                    int(count) if isinstance(count, (int, float)) and not isinstance(count, bool)
                    else len(ids)
                )
                continue
            db.add(BrowserEvent(session_id=session_id, event_type=f"metric_{kind}",
                                message="Search measurement", data=data, created_at=timestamp))
        if overlap_ids:
            db.add(BrowserEvent(
                session_id=session_id,
                event_type="metric_overlap",
                message="Search measurement",
                data={"ids": list(dict.fromkeys(overlap_ids)), "raw_count": overlap_raw},
                created_at=queue[0][2],
            ))
        queue.clear()
        return True
    return False


@contextmanager
def measure(stage: str):
    start = perf_counter()
    outcome = "ok"
    try:
        yield
    except BaseException as exc:
        outcome = type(exc).__name__
        raise
    finally:
        record("stage", {"stage": stage, "seconds": perf_counter() - start, "outcome": outcome})


def initialize(db, item, profile, resumes, algorithm: str | None = None,
               algorithm_version: str | None = None, criteria_hash: str | None = None) -> None:
    """Persist an experiment identity once at launch.

    ``algorithm`` is explicit for HireHi adaptive runs. Omitting it retains
    the legacy values and is therefore safe for existing HH callers.
    """
    if (item.recovery or {}).get("measurement_identity"):
        return
    root = Path(__file__).resolve().parents[2]
    git = {}
    for name, args in (("revision", ["rev-parse", "HEAD"]), ("branch", ["branch", "--show-current"]),
                       ("dirty", ["status", "--porcelain", "--untracked-files=no"])):
        try:
            value = subprocess.check_output(["git", *args], cwd=root, timeout=3, text=True,
                                            stderr=subprocess.DEVNULL).strip()
            git[name] = bool(value) if name == "dirty" else value
        except (OSError, subprocess.SubprocessError):
            git[name] = None
    config = db.get(AIModelSettings, 1)
    resolved_algorithm = (algorithm or algorithm_version
                          or (HH_SEARCH_VERSION if item.adapter_id == "hh" else "existing_v1"))
    if criteria_hash is not None:
        if not isinstance(criteria_hash, str) or not 1 <= len(criteria_hash) <= 256:
            raise ValueError("criteria_hash must be a non-empty string of at most 256 characters")
        resolved_criteria_hash = criteria_hash
    else:
        resolved_criteria_hash = fingerprint({"profile": profile, "resumes": resumes,
                                              "scores": item.minimum_scores,
                                              "preferences": item.desired_job_description})
    identity = {
        "schema": 2, "algorithm": resolved_algorithm, "algorithm_version": resolved_algorithm,
        "git": git, "adapter": item.adapter_id, "criteria_hash": resolved_criteria_hash,
        "model": config.model if config else settings.llm_provider,
        "model_endpoint_hash": fingerprint(config.base_url) if config else None,
        "application_limit": item.application_limit,
    }
    item.recovery = {**(item.recovery or {}), "measurement_identity": identity}
    db.commit()


def discovery(adapter, refs) -> None:
    batch = getattr(adapter, "last_discovery_batch", None)
    if batch is None:
        batch = {"ids": [r.external_id for r in refs], "source": "listing"}
    record("discovery", batch)


def _utc(value):
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _event_data(event) -> dict:
    return event.data if isinstance(event.data, dict) else {}


def _event_ids(data: dict) -> list[str]:
    values = data.get("ids", data.get("external_ids", data.get("job_ids", [])))
    if isinstance(values, str):
        values = [values]
    return [str(value) for value in values or [] if value is not None]


def _source(data: dict) -> str:
    return str(data.get("source_id", data.get("source", data.get("query", "listing"))))


def _mode(data: dict) -> str:
    value = data.get("mode", data.get("strategy", data.get("arm_type")))
    if isinstance(value, str):
        lower = value.lower()
        if "explor" in lower:
            return "exploration"
        if "exploit" in lower:
            return "exploitation"
    return "exploration" if data.get("exploration") is True else "exploitation"


def _is_complete(data: dict) -> bool:
    for key in ("complete", "evaluation_complete", "full"):
        if key in data and data[key] is False:
            return False
    status = str(data.get("status", data.get("evaluation_status", "complete"))).lower()
    return status not in {"partial", "incomplete", "pending", "started"}


def _relevant(data: dict) -> bool:
    if "relevant" in data:
        return bool(data["relevant"])
    if "is_relevant" in data:
        return bool(data["is_relevant"])
    return str(data.get("decision", "")).lower() in {"apply", "relevant", "selected", "accept", "accepted"}


def _ratio(numerator: int | float, denominator: int | float):
    return numerator / denominator if denominator else None


def _stat_int(value) -> int:
    if isinstance(value, (list, tuple, set)):
        return len(value)
    return int(value or 0)


def _metric_seconds(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return max(0.0, float(value))


_MODEL_REPAIR_CATEGORIES = {
    "safety", "requirements", "special_conditions", "formatting",
    "unexpected_tool_call", "unsafe_model_output", "schema_validation",
}


def _identity(item) -> dict:
    identity = (item.recovery or {}).get("measurement_identity") or {}
    return identity if isinstance(identity, dict) else {}


def _events(db, session_id: int):
    return list(db.scalars(select(BrowserEvent).where(BrowserEvent.session_id == session_id)
                           .order_by(BrowserEvent.id)))


def _evaluations(events, start, vacancy_ids=None):
    """Return the first complete evaluation per external id, in event order."""
    result = {}
    for event in events:
        if event.event_type not in {"evaluation", "metric_evaluation", "metric_hirehi_evaluation"}:
            continue
        data = _event_data(event)
        if not _is_complete(data):
            continue
        key = data.get("external_id") or data.get("job_id")
        if key is None and vacancy_ids:
            key = vacancy_ids.get(data.get("vacancy_id"))
        if key is None or str(key) in result:
            continue
        key = str(key)
        timestamp = _utc(event.created_at)
        elapsed = max(0, (timestamp - start).total_seconds()) if start and timestamp else 0
        result[key] = {"id": key, "data": data, "event": event, "elapsed": elapsed,
                       "relevant": _relevant(data), "source": _source(data),
                       "rank": data.get("rank", data.get("position"))}
    return result


def _snapshot_maps(events):
    snapshots = {}
    semantic = set()
    overall = None
    for event in events:
        data = _event_data(event)
        if event.event_type in {"metric_hirehi_snapshot", "hirehi_snapshot"}:
            if isinstance(data.get("sources"), dict):
                overall = (event.id, data)
                for source, stats in data["sources"].items():
                    if isinstance(stats, dict):
                        snapshots[str(source)] = (event.id, stats)
            else:
                snapshots[_source(data)] = (event.id, data)
        elif (event.event_type in {"metric_semantic_reuse", "semantic_reuse"}
              and data.get("near_duplicate", data.get("reused", True))):
            semantic.add(str(data.get("external_id", data.get("job_id", data.get("canonical_external_id", "")))))
    return snapshots, {value for value in semantic if value}, overall


def summary(db, item: JobSession) -> dict:
    events = _events(db, item.id)
    vacancy_ids = {v.id: v.external_id for v in db.scalars(
        select(Vacancy).where(Vacancy.session_id == item.id)
    )}
    start = _utc(item.started_at) if item.started_at else None
    finish = _utc(item.finished_at) if item.finished_at else datetime.now(timezone.utc)
    elapsed = max(0, (finish - start).total_seconds()) if start else 0
    found, overlap = set(), set()
    overlap_raw = 0
    first_source, first_mode, per_source_ids = {}, {}, defaultdict(set)
    raw = 0
    sources = {}
    mode_stats = {"exploration": {"raw": 0, "unique": 0, "analyzed": 0, "relevant": 0},
                  "exploitation": {"raw": 0, "unique": 0, "analyzed": 0, "relevant": 0}}
    stages, retries = {}, 0
    token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "reported_calls": 0}
    model_metrics = {
        "queue": {"count": 0, "seconds": 0.0},
        "provider": {"count": 0, "seconds": 0.0},
        "repair": {"count": 0, "by_category": {}, "by_ordinal": {}},
        "recovery": {"count": 0},
    }
    seen_model_events = {name: set() for name in model_metrics}

    def model_event_key(bucket: str, event, data: dict):
        diagnostic_id = data.get("diagnostic_id")
        attempt = data.get("attempt")
        ordinal = data.get(
            "ordinal", data.get("generation", data.get("correction_generation", data.get("attempt")))
        )
        if isinstance(diagnostic_id, str) and diagnostic_id:
            discriminator = ordinal if bucket == "repair" else attempt
            return (diagnostic_id, discriminator)
        return ("event", event.id)

    for event in events:
        data = _event_data(event)
        if event.event_type == "metric_discovery":
            source, mode, ids = _source(data), _mode(data), _event_ids(data)
            row = sources.setdefault(source, {"pages": 0, "raw": 0, "unique": 0, "unique_first_seen": 0,
                                               "analyzed": 0, "relevant": 0, "precision": None,
                                               "novelty": None, "cost": 0, "failures": 0})
            row["pages"] += 1
            count = data.get("raw_count", data.get("raw"))
            count = int(count) if isinstance(count, (int, float)) else len(ids)
            raw += count
            row["raw"] += count
            mode_stats[mode]["raw"] += count
            for key in ids:
                per_source_ids[source].add(key)
                row["unique"] = len(per_source_ids[source])
                if key not in found:
                    first_source[key], first_mode[key] = source, mode
                    row["unique_first_seen"] += 1
                    mode_stats[mode]["unique"] += 1
                found.add(key)
            row["failures"] += int(data.get("failures", data.get("failed", 0)) or 0)
            cost = data.get("cost", data.get("cost_usd", 0))
            if isinstance(cost, (int, float)):
                row["cost"] += cost
        elif event.event_type in {"metric_overlap", "overlap"}:
            ids = _event_ids(data)
            if not ids and data.get("external_id") is not None:
                ids = [str(data["external_id"])]
            overlap.update(ids)
            count = data.get("raw_count")
            overlap_raw += (
                int(count) if isinstance(count, (int, float)) and not isinstance(count, bool)
                else len(ids)
            )
        elif event.event_type == "metric_stage":
            stage = str(data.get("stage", "unknown"))
            row = stages.setdefault(stage, {"calls": 0, "seconds": 0, "errors": 0})
            row["calls"] += 1
            row["seconds"] += float(data.get("seconds", 0) or 0)
            row["errors"] += data.get("outcome") != "ok"
        elif event.event_type == "recovery_retry":
            retries += 1
        elif event.event_type == "metric_tokens":
            token_usage["reported_calls"] += 1
            for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
                token_usage[name] += int(data.get(name, 0) or 0)
        elif event.event_type in {"metric_model_queue", "metric_model_provider"}:
            bucket = "queue" if event.event_type == "metric_model_queue" else "provider"
            key = model_event_key(bucket, event, data)
            if key in seen_model_events[bucket]:
                continue
            seen_model_events[bucket].add(key)
            model_metrics[bucket]["count"] += 1
            duration_key = "queue_seconds" if bucket == "queue" else "provider_seconds"
            seconds = _metric_seconds(data.get(duration_key))
            if seconds is not None:
                model_metrics[bucket]["seconds"] += seconds
        elif event.event_type == "metric_model_recovery":
            bucket = "recovery"
            key = model_event_key(bucket, event, data)
            if key in seen_model_events[bucket]:
                continue
            seen_model_events[bucket].add(key)
            model_metrics[bucket]["count"] += 1
        elif event.event_type == "metric_model_repair":
            bucket = "repair"
            category = data.get("category", data.get("correction_category", data.get("repair_kind")))
            if category not in _MODEL_REPAIR_CATEGORIES:
                continue
            key = model_event_key(bucket, event, data)
            if key in seen_model_events[bucket]:
                continue
            seen_model_events[bucket].add(key)
            model_metrics[bucket]["count"] += 1
            by_category = model_metrics[bucket]["by_category"]
            by_category[category] = by_category.get(category, 0) + 1
            ordinal = data.get(
                "ordinal", data.get("generation", data.get("correction_generation", data.get("attempt")))
            )
            if isinstance(ordinal, int) and not isinstance(ordinal, bool) and 0 <= ordinal <= 20:
                by_ordinal = model_metrics[bucket]["by_ordinal"]
                key = str(ordinal)
                by_ordinal[key] = by_ordinal.get(key, 0) + 1

    snapshots, semantic_near, overall_snapshot = _snapshot_maps(events)
    # A snapshot is a durable checkpoint emitted by the adaptive planner. The
    # last snapshot for each source is exposed verbatim, while its aggregate
    # fields are used only when no discovery rows supplied that statistic.
    authoritative = defaultdict(set)
    snapshot_fields = {
        "raw": ("raw_count", "raw"), "unique": ("unique_count", "unique"),
        "analyzed": ("analyzed_count", "analyzed"), "relevant": ("relevant_count", "relevant"),
        "cost": ("cost", "cost_usd"), "failures": ("failures", "failed"),
        "precision": ("precision",), "novelty": ("novelty",),
    }
    for source, (_event_id, data) in snapshots.items():
        row = sources.setdefault(source, {"pages": 0, "raw": 0, "unique": 0, "unique_first_seen": 0,
                                           "analyzed": 0, "relevant": 0, "precision": None,
                                           "novelty": None, "cost": 0, "failures": 0})
        for field, names in snapshot_fields.items():
            value = next((data[name] for name in names if name in data), None)
            if value is not None:
                if field in {"raw", "unique", "analyzed", "relevant", "failures"}:
                    value = _stat_int(value)
                row[field] = value
                authoritative[source].add(field)
    # An overall snapshot may carry metrics/audit/rejection summaries in
    # addition to its per-source authoritative rows.
    overall_data = overall_snapshot[1] if overall_snapshot else {}
    overall_metrics = overall_data.get("metrics") if isinstance(overall_data.get("metrics"), dict) else {}
    # Some deployments emit only a checkpoint snapshot. It can still provide
    # discovery IDs, but never overrides discovery rows already observed.
    if not found:
        for source, (_event_id, data) in snapshots.items():
            ids = data.get("ids", data.get("external_ids", data.get("unique_ids", [])))
            if isinstance(ids, str):
                ids = [ids]
            for key in ids or []:
                key = str(key)
                if key not in found:
                    found.add(key)
                    first_source[key] = source
                    first_mode[key] = _mode(data)
            raw += _stat_int(data.get("raw_count", data.get("raw", len(ids or []))))
    if not found and isinstance(overall_data.get("metrics"), dict):
        raw = _stat_int(overall_data["metrics"].get("raw", overall_data["metrics"].get("raw_discoveries", raw)))
    snapshot_audit = overall_data.get("audit") if isinstance(overall_data.get("audit"), dict) else None
    snapshot_reasons = overall_data.get("rejection_reasons")
    if isinstance(snapshot_reasons, dict):
        snapshot_reasons = {str(key): int(value or 0) for key, value in snapshot_reasons.items()}
    else:
        snapshot_reasons = None
    evaluations = _evaluations(events, start, vacancy_ids)
    evaluated_ids = set(evaluations)
    relevant_ids = {key for key, value in evaluations.items() if value["relevant"]}
    judged_ids = evaluated_ids
    for key, value in evaluations.items():
        source = first_source.get(key, value["source"])
        row = sources.setdefault(source, {"pages": 0, "raw": 0, "unique": 0, "unique_first_seen": 0,
                                           "analyzed": 0, "relevant": 0, "precision": None,
                                           "novelty": None, "cost": 0, "failures": 0})
        if "analyzed" not in authoritative[source]:
            row["analyzed"] += 1
        if "relevant" not in authoritative[source]:
            row["relevant"] += value["relevant"]
        mode_stats[first_mode.get(key, "exploitation")]["analyzed"] += 1
        mode_stats[first_mode.get(key, "exploitation")]["relevant"] += value["relevant"]
    for source, row in sources.items():
        if "precision" not in authoritative[source]:
            row["precision"] = _ratio(row["relevant"], row["analyzed"])
        if "novelty" not in authoritative[source]:
            row["novelty"] = _ratio(row["unique_first_seen"], row["raw"])

    ordered = list(evaluations.values())
    r_at, n_at, rn_at = {}, {}, {}
    for checkpoint in (50, 100, 200):
        prefix = ordered[:checkpoint]
        r_at[str(checkpoint)] = sum(value["relevant"] for value in prefix)
        n_at[str(checkpoint)] = len(prefix)
        rn_at[str(checkpoint)] = _ratio(r_at[str(checkpoint)], n_at[str(checkpoint)])
    if isinstance(overall_metrics.get("R_at"), dict):
        r_at.update({str(key): _stat_int(value) for key, value in overall_metrics["R_at"].items()})
    if isinstance(overall_metrics.get("N_at"), dict):
        n_at.update({str(key): _stat_int(value) for key, value in overall_metrics["N_at"].items()})
    for checkpoint in (50, 100, 200):
        if f"R@{checkpoint}" in overall_metrics:
            r_at[str(checkpoint)] = _stat_int(overall_metrics[f"R@{checkpoint}"])
        if f"N@{checkpoint}" in overall_metrics:
            n_at[str(checkpoint)] = _stat_int(overall_metrics[f"N@{checkpoint}"])
        rn_at[str(checkpoint)] = _ratio(r_at[str(checkpoint)], n_at[str(checkpoint)])
    reached = sorted(value["elapsed"] for value in evaluations.values() if value["relevant"])
    audit_selected_ids, audit_relevant_ids, audit_false_negative_ids = set(), set(), set()
    audit_selected_incremental = 0
    audit_false_negative_incremental = 0
    audit_relevant_incremental = 0
    audit_snapshot_selected = 0
    audit_snapshot_false_negative = 0
    audit_snapshot_relevant = 0
    audit_fnr_values = []

    def consume_audit(data: dict, *, snapshot: bool = False):
        nonlocal audit_selected_incremental, audit_false_negative_incremental
        nonlocal audit_relevant_incremental
        nonlocal audit_snapshot_selected, audit_snapshot_false_negative, audit_snapshot_relevant
        selected_value = data.get("selected_ids", data.get("selected", []))
        relevant_value = data.get("relevant_ids", data.get("relevant", []))
        false_negative_value = data.get("false_negative_ids", data.get("false_negatives", []))
        if isinstance(selected_value, (list, tuple, set)):
            audit_selected_ids.update(map(str, selected_value))
        elif selected_value is not None:
            if snapshot:
                audit_snapshot_selected += int(selected_value or 0)
            else:
                audit_selected_incremental += int(selected_value or 0)
        if isinstance(relevant_value, (list, tuple, set)):
            audit_relevant_ids.update(map(str, relevant_value))
        elif relevant_value is not None:
            if snapshot:
                audit_snapshot_relevant += int(relevant_value or 0)
            else:
                audit_relevant_incremental += int(relevant_value or 0)
        if isinstance(false_negative_value, (list, tuple, set)):
            audit_false_negative_ids.update(map(str, false_negative_value))
        elif false_negative_value is not None:
            if snapshot:
                audit_snapshot_false_negative += int(false_negative_value or 0)
            else:
                audit_false_negative_incremental += int(false_negative_value or 0)
        if data.get("fnr") is not None:
            audit_fnr_values.append(float(data["fnr"]))

    for event in events:
        if event.event_type in {"metric_audit", "audit"}:
            consume_audit(_event_data(event))
    # Flat snapshots are cumulative checkpoints: only the latest snapshot for
    # each source is consumed, while ID fields are unioned with audit events.
    for _source_id, (_event_id, data) in snapshots.items():
        if any(key in data for key in ("selected_ids", "selected", "false_negative_ids", "false_negatives")):
            consume_audit(data, snapshot=True)
    if snapshot_audit:
        consume_audit(snapshot_audit, snapshot=True)
    audit_selected = len(audit_selected_ids) + audit_selected_incremental + audit_snapshot_selected
    audit_relevant = len(audit_relevant_ids) + audit_relevant_incremental + audit_snapshot_relevant
    audit_false_negative = (len(audit_false_negative_ids) + audit_false_negative_incremental
                            + audit_snapshot_false_negative)
    rejection_reasons = defaultdict(int)
    rejected = 0
    for value in evaluations.values():
        if not value["relevant"]:
            rejected += 1
            reason = value["data"].get("rejection_reason", value["data"].get("reason"))
            if reason:
                rejection_reasons[str(reason)] += 1
    for mode in mode_stats.values():
        mode["precision"] = _ratio(mode["relevant"], mode["analyzed"])
    audit_fnr = (audit_false_negative / audit_selected if audit_selected
                 else (sum(audit_fnr_values) / len(audit_fnr_values) if audit_fnr_values else None))
    D = len(found)
    N = len(evaluated_ids)
    if overall_metrics:
        D = _stat_int(overall_metrics.get("D", overall_metrics.get("unique_discovered", D)))
        N = _stat_int(overall_metrics.get("N", overall_metrics.get("analyzed", N)))
        reported_R = _stat_int(overall_metrics.get("R", overall_metrics.get("relevant", len(relevant_ids))))
    else:
        reported_R = len(relevant_ids)
    hirehi_semantics = item.adapter_id == "hirehi"
    result = {
        "session_id": item.id, "identity": _identity(item), "status": item.status,
        "started_at": start.isoformat() if start else None,
        "finished_at": _utc(item.finished_at).isoformat() if item.finished_at else None,
        "elapsed_seconds": elapsed, "D": D, "N": N, "R": reported_R,
        "R/N": _ratio(reported_R, N), "N/D": _ratio(N, D),
        "raw_discoveries": raw, "unique_discovered": D,
        "duplicate_discoveries": max(raw - D, 0), "exact_duplicates": max(raw - D, 0),
        "semantic_near_duplicates": len(semantic_near), "historical_overlap": len(overlap),
        "historical_overlap_raw": overlap_raw,
        "judged": N, "relevant": reported_R,
        "unjudged": max(D - N, 0) if hirehi_semantics else len(found - judged_ids),
        "relevant_per_discovered": _ratio(reported_R, D),
        "relevant_per_judged": _ratio(reported_R, N),
        "judged_coverage": _ratio(N, D),
        "relevant_per_hour": _ratio(reported_R * 3600, elapsed),
        "R_at": r_at, "N_at": n_at, "R/N_at": rn_at,
        "R@50": r_at["50"], "R@100": r_at["100"], "R@200": r_at["200"],
        "R/N@50": rn_at["50"], "R/N@100": rn_at["100"], "R/N@200": rn_at["200"],
        "time_to_relevant_seconds": {str(k): reached[k - 1] if len(reached) >= k else None for k in (1, 20, 50, 100)},
        "time_to_R": {str(k): reached[k - 1] if len(reached) >= k else None for k in (1, 20, 50, 100)},
        "exploration": mode_stats["exploration"], "exploitation": mode_stats["exploitation"],
        "audit": {"selected": audit_selected, "relevant": audit_relevant,
                   "false_negatives": audit_false_negative,
                   "fnr": audit_fnr},
        "rejection_reasons": snapshot_reasons or dict(rejection_reasons),
        "rejection_reason_coverage": _ratio(sum((snapshot_reasons or rejection_reasons).values()), rejected),
        "recovery_retries": retries, "stages": stages, "sources": sources,
        "model_metrics": model_metrics,
        "snapshots": {source: data for source, (_id, data) in snapshots.items()},
        "latest_snapshots": {source: data for source, (_id, data) in snapshots.items()},
        "token_usage": token_usage if token_usage["reported_calls"] else None,
        "applications": {key: (item.counters or {}).get(key, 0) for key in ("submitted", "already_applied", "errors")},
        "limitations": ["Relevance is the configured model decision unless an audit event supplies ground truth.",
                        "Historical overlaps and unjudged vacancies are not negative labels.",
                        "Durations include waiting; monetary cost is reported only when an event supplies it."],
    }
    return result


def load_hirehi_weak_prior(db, current_session: JobSession, *, cap: int = 5) -> dict:
    """Load bounded, compatible source priors from previous HireHi sessions."""
    identity = _identity(current_session)
    criteria_hash = identity.get("criteria_hash")
    algorithm = identity.get("algorithm_version", identity.get("algorithm"))
    if not criteria_hash or not algorithm:
        return {}
    aggregate = defaultdict(lambda: [0, 0, set()])
    sessions = db.scalars(select(JobSession).where(JobSession.adapter_id == "hirehi")).all()
    for previous in sessions:
        if previous.id == current_session.id:
            continue
        previous_identity = _identity(previous)
        if previous_identity.get("criteria_hash") != criteria_hash:
            continue
        if previous_identity.get("algorithm_version", previous_identity.get("algorithm")) != algorithm:
            continue
        events = _events(db, previous.id)
        per_source = defaultdict(lambda: [0, 0])
        # V3 emits cumulative overall snapshots. Use only the newest one for
        # this session; otherwise repeated checkpoints would amplify priors.
        overall_snapshots = [
            event for event in events
            if event.event_type in {"metric_hirehi_snapshot", "hirehi_snapshot"}
            and isinstance(_event_data(event).get("sources"), dict)
        ]
        if overall_snapshots:
            latest = max(overall_snapshots, key=lambda event: event.id)
            for source in sorted(_event_data(latest)["sources"]):
                stats = _event_data(latest)["sources"][source]
                if not isinstance(stats, dict):
                    continue
                analyzed = _stat_int(stats.get("analyzed", stats.get("analyzed_count", 0)))
                relevant = _stat_int(stats.get("relevant", stats.get("relevant_count", 0)))
                per_source[str(source)][0] += min(relevant, analyzed)
                per_source[str(source)][1] += max(analyzed - relevant, 0)
        else:
            # Legacy sessions had only evaluation events. Recover their source
            # from the first discovery containing the ID when possible.
            evaluations = _evaluations(events, _utc(previous.started_at) if previous.started_at else None)
            for key, value in evaluations.items():
                source = _source(value["data"])
                if source == "listing":
                    source = next((str(_source(_event_data(e))) for e in events
                                   if e.event_type == "metric_discovery" and key in _event_ids(_event_data(e))), source)
                per_source[source][0 if value["relevant"] else 1] += 1
        for source in sorted(per_source):
            successes, failures = per_source[source]
            aggregate[source][0] += successes
            aggregate[source][1] += failures
            aggregate[source][2].add(previous.id)
    result = {}
    for source in sorted(aggregate):
        successes, failures, prior_sessions = aggregate[source]
        total = successes + failures
        if not total:
            continue
        bounded_total = min(total, max(0, int(cap)))
        success_share = bounded_total * successes / total
        failure_share = bounded_total * failures / total
        row = {"successes": success_share, "failures": failure_share,
               "total": success_share + failure_share, "sessions": len(prior_sessions)}
        if bounded_total == total:
            row["successes"], row["failures"] = successes, failures
        result[source] = row
    return result


load_weak_prior = load_hirehi_weak_prior
weak_prior = load_hirehi_weak_prior


def freeze(db, item) -> dict:
    result = summary(db, item)
    item.recovery = {**(item.recovery or {}), "measurement_report": result}
    db.commit()
    return result
