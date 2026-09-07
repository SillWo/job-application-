"""Append-only search measurements shared by both experimental branches.

Reuse the event schema so switching branches never requires a database downgrade.
Only identifiers, hashes, durations and decisions enter exported measurements.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
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
        for kind, data, timestamp in queue:
            db.add(BrowserEvent(session_id=session_id, event_type=f"metric_{kind}",
                                message="Search measurement", data=data, created_at=timestamp))
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


def initialize(db, item, profile, resumes) -> None:
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
    identity = {
        "schema": 1, "algorithm": HH_SEARCH_VERSION if item.adapter_id == "hh" else "existing_v1",
        "git": git, "adapter": item.adapter_id,
        "criteria_hash": fingerprint({"profile": profile, "resumes": resumes,
                                      "scores": item.minimum_scores, "preferences": item.desired_job_description}),
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
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def summary(db, item: JobSession) -> dict:
    events = list(db.scalars(select(BrowserEvent).where(BrowserEvent.session_id == item.id)
                             .order_by(BrowserEvent.id)))
    start = _utc(item.started_at) if item.started_at else None
    finish = _utc(item.finished_at) if item.finished_at else datetime.now(timezone.utc)
    elapsed = max(0, (finish - start).total_seconds()) if start else 0
    found, overlap, decisions, times, sources, stages = set(), set(), {}, {}, {}, {}
    first_source = {}
    raw = retries = 0
    token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "reported_calls": 0}
    # Evaluation events are immutable even if a vacancy is later removed from history.
    vacancy_ids = {v.id: v.external_id for v in db.scalars(select(Vacancy).where(Vacancy.session_id == item.id))}
    for event in events:
        data = event.data or {}
        if event.event_type == "metric_discovery":
            source = str(data.get("source", "listing"))
            row = sources.setdefault(source, {"pages": 0, "raw": 0, "unique_first_seen": 0, "relevant": 0})
            ids = data.get("ids", [])
            row["pages"] += 1
            row["raw"] += len(ids)
            raw += len(ids)
            for key in ids:
                if key not in found:
                    first_source[key] = source
                    row["unique_first_seen"] += 1
                found.add(key)
        elif event.event_type == "metric_overlap":
            overlap.add(data["external_id"])
        elif event.event_type == "evaluation":
            key = data.get("external_id") or vacancy_ids.get(data.get("vacancy_id"))
            if key is not None:
                decisions[key] = data.get("decision")
                times.setdefault(key, max(0, (_utc(event.created_at) - start).total_seconds()) if start else 0)
        elif event.event_type == "metric_stage":
            row = stages.setdefault(data["stage"], {"calls": 0, "seconds": 0, "errors": 0})
            row["calls"] += 1
            row["seconds"] += data["seconds"]
            row["errors"] += data["outcome"] != "ok"
        elif event.event_type == "recovery_retry":
            retries += 1
        elif event.event_type == "metric_tokens":
            token_usage["reported_calls"] += 1
            for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
                token_usage[name] += data[name]
    relevant = {key for key, decision in decisions.items() if decision == "apply"}
    judged = {key for key, decision in decisions.items() if decision in {"apply", "skip"}}
    for key in relevant:
        if key in first_source:
            sources[first_source[key]]["relevant"] += 1
    reached = sorted(times[key] for key in relevant)
    return {
        "session_id": item.id, "identity": (item.recovery or {}).get("measurement_identity"),
        "status": item.status, "started_at": start.isoformat() if start else None,
        "finished_at": _utc(item.finished_at).isoformat() if item.finished_at else None,
        "elapsed_seconds": elapsed, "raw_discoveries": raw, "unique_discovered": len(found),
        "duplicate_discoveries": raw - len(found), "historical_overlap": len(overlap),
        "judged": len(judged), "relevant": len(relevant), "unjudged": len(found - judged),
        "relevant_per_discovered": len(relevant) / len(found) if found else None,
        "relevant_per_judged": len(relevant) / len(judged) if judged else None,
        "judged_coverage": len(judged) / len(found) if found else None,
        "relevant_per_hour": len(relevant) * 3600 / elapsed if elapsed else None,
        "time_to_relevant_seconds": {str(k): reached[k - 1] if len(reached) >= k else None for k in (1, 20, 50, 100)},
        "recovery_retries": retries, "stages": stages, "sources": sources,
        "token_usage": token_usage if token_usage["reported_calls"] else None,
        "applications": {key: (item.counters or {}).get(key, 0) for key in ("submitted", "already_applied", "errors", "skipped_test")},
        "limitations": ["Relevance is the configured model decision, not independent human ground truth.",
                         "Historical overlaps and unjudged vacancies are not negative labels.",
                         "HH recommendations change after activity; run order can affect results.",
                         "Durations include waiting; stage times are measured separately. Tokens are available only when reported by the provider; monetary cost is not estimated."],
    }


def freeze(db, item) -> dict:
    result = summary(db, item)
    item.recovery = {**(item.recovery or {}), "measurement_report": result}
    db.commit()
    return result
