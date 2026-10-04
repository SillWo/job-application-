"""Durable descriptive reports and a conservative HireHi rollout gate.

This module assesses evidence only. It never changes a feature flag or starts
another search. Example: ``python -m backend.services.compare_search 12 13``.
"""
from __future__ import annotations

import argparse
import json
from statistics import median

from backend.persistence.database import SessionLocal
from backend.persistence.models import JobSession
from backend.services.search_metrics import summary


def _identity(report):
    value = report.get("identity") or {}
    return value if isinstance(value, dict) else {}


def _algorithm(report):
    return str(_identity(report).get("algorithm_version", _identity(report).get("algorithm", "unmeasured")))


def _is_adaptive(report):
    return _algorithm(report).lower() in {"hirehi_adaptive_v3", "hirehi_v3", "adaptive_v3"}


def _is_baseline(report):
    value = _algorithm(report).lower()
    return value in {"hirehi_v1", "hirehi_baseline", "existing_v1", "baseline", "v1"}


def _checkpoint(report, name, checkpoint):
    nested = report.get(name) or {}
    value = nested.get(str(checkpoint), nested.get(checkpoint)) if isinstance(nested, dict) else None
    if value is None:
        value = report.get(f"{name}@{checkpoint}")
    if value is None:
        aliases = {"R_at": "R", "N_at": "N"}
        value = report.get(f"{aliases.get(name, name)}@{checkpoint}")
    return value


def _available_checkpoints(report_a, report_b):
    result = []
    for checkpoint in (50, 100, 200):
        n_a = _checkpoint(report_a, "N_at", checkpoint)
        n_b = _checkpoint(report_b, "N_at", checkpoint)
        # Older reports used the evaluation count implicitly; use N when the
        # checkpoint itself was reached, otherwise do not invent evidence.
        if n_a is None and report_a.get("N", 0) >= checkpoint:
            n_a = checkpoint
        if n_b is None and report_b.get("N", 0) >= checkpoint:
            n_b = checkpoint
        if n_a is not None and n_b is not None and n_a >= checkpoint and n_b >= checkpoint:
            result.append(checkpoint)
    return result


def _matched_pairs(reports):
    groups = {}
    for report in sorted(reports, key=lambda row: (row.get("session_id") is None, row.get("session_id") or 0)):
        identity = _identity(report)
        key = (identity.get("criteria_hash"), identity.get("model"))
        algorithm = _algorithm(report).lower()
        if None in key or (algorithm not in {"hirehi_v1", "hirehi_baseline", "existing_v1", "baseline", "v1",
                                             "hirehi_adaptive_v3", "hirehi_v3", "adaptive_v3"}):
            continue
        bucket = groups.setdefault(key, {"baseline": [], "adaptive": []})
        bucket["adaptive" if _is_adaptive(report) else "baseline"].append(report)
    pairs = []
    for key, bucket in groups.items():
        for baseline, adaptive in zip(bucket["baseline"], bucket["adaptive"], strict=False):
            checkpoints = _available_checkpoints(baseline, adaptive)
            pairs.append({"criteria_hash": key[0], "model": key[1], "baseline": baseline,
                          "adaptive": adaptive, "checkpoints": checkpoints})
    return pairs


def assess_rollout(reports, *, min_pairs: int = 10) -> dict:
    """Assess paired HireHi baseline/adaptive evidence without side effects."""
    pairs = _matched_pairs(reports)
    reasons = []
    if len(pairs) < min_pairs:
        reasons.append(f"need at least {min_pairs} matched pairs; found {len(pairs)}")
    early_lifts = []
    final_values = []
    audit_false_negative_total = 0.0
    audit_selected_total = 0.0
    incomplete_pairs = 0
    for pair in pairs:
        baseline, adaptive = pair["baseline"], pair["adaptive"]
        b_early = _checkpoint(baseline, "R_at", 100)
        a_early = _checkpoint(adaptive, "R_at", 100)
        b_n100 = _checkpoint(baseline, "N_at", 100)
        a_n100 = _checkpoint(adaptive, "N_at", 100)
        if b_n100 is None:
            b_n100 = 100 if (baseline.get("N") or 0) >= 100 else None
        if a_n100 is None:
            a_n100 = 100 if (adaptive.get("N") or 0) >= 100 else None
        audit = adaptive.get("audit") or {}
        selected_value = audit.get("selected_ids", audit.get("selected", 0))
        selected = (len(selected_value) if isinstance(selected_value, (list, tuple, set))
                    else float(selected_value or 0))
        false_value = audit.get("false_negative_ids", audit.get("false_negatives"))
        fn = ((len(false_value) if isinstance(false_value, (list, tuple, set)) else float(false_value))
              if false_value is not None else None)
        fnr = audit.get("fnr")
        if fnr is None and fn is not None and selected:
            fnr = fn / selected
        complete = (b_n100 is not None and a_n100 is not None and
                    b_early is not None and b_early > 0 and a_early is not None and
                    selected > 0 and fnr is not None)
        pair["complete"] = complete
        pair["audit_selected"] = selected
        pair["audit_false_negatives"] = fn
        if not complete:
            incomplete_pairs += 1
            pair["early_lift"] = None
            pair["final_ratio"] = None
            continue
        pair["checkpoints"] = _available_checkpoints(baseline, adaptive)
        pair["checkpoints"] = sorted(set(pair["checkpoints"]) | {100})
        pair["early_lift"] = ((a_early - b_early) / b_early) if b_early else None
        if pair["early_lift"] is not None:
            early_lifts.append(pair["early_lift"])
        audit_selected_total += selected
        audit_false_negative_total += fn if fn is not None else float(fnr) * selected
        common = max(pair["checkpoints"])
        b_final = _checkpoint(baseline, "R_at", common)
        a_final = _checkpoint(adaptive, "R_at", common)
        if b_final in (None, 0) or a_final is None:
            pair["final_ratio"] = None
        else:
            pair["final_ratio"] = a_final / b_final
            final_values.append(pair["final_ratio"])
    evidence_pairs = [pair for pair in pairs if pair.get("complete")]
    median_lift = median(early_lifts) if early_lifts else None
    aggregate_baseline = sum((_checkpoint(p["baseline"], "R_at", 100) or 0) for p in evidence_pairs)
    aggregate_adaptive = sum((_checkpoint(p["adaptive"], "R_at", 100) or 0) for p in evidence_pairs)
    aggregate_lift = ((aggregate_adaptive - aggregate_baseline) / aggregate_baseline
                      if aggregate_baseline else None)
    aggregate_final_b = sum((_checkpoint(p["baseline"], "R_at", max(p["checkpoints"])) or 0) for p in evidence_pairs)
    aggregate_final_a = sum((_checkpoint(p["adaptive"], "R_at", max(p["checkpoints"])) or 0) for p in evidence_pairs)
    final_ratio = aggregate_final_a / aggregate_final_b if aggregate_final_b else None
    fnr = (audit_false_negative_total / audit_selected_total
           if audit_selected_total else None)
    gates = {
        "audit_fnr_le_5_percent": fnr is not None and fnr <= 0.05,
        "median_early_R@100_lift_ge_10_percent": median_lift is not None and median_lift >= 0.10,
        "aggregate_early_R@100_lift_ge_10_percent": aggregate_lift is not None and aggregate_lift >= 0.10,
        "matched_final_R_not_below_5_percent": final_ratio is not None and final_ratio >= 0.95,
    }
    if len(pairs) < min_pairs or incomplete_pairs:
        verdict = "insufficient_evidence"
    elif all(gates.values()):
        verdict = "pass"
    else:
        verdict = "fail"
        reasons.extend(name for name, passed in gates.items() if not passed)
    if incomplete_pairs:
        reasons.append(f"{incomplete_pairs} matched pairs lack N>=100, R@100, or audit sample")
    return {
        "verdict": verdict, "matched_pairs": len(pairs), "complete_pairs": len(pairs) - incomplete_pairs,
        "minimum_pairs": min_pairs,
        "metrics": {"audit_fnr": fnr, "median_early_R@100_lift": median_lift,
                     "aggregate_early_R@100_lift": aggregate_lift,
                     "matched_final_R_ratio": final_ratio,
                     "pair_final_ratios": final_values},
        "gates": gates, "reasons": reasons,
        "pairs": [{"criteria_hash": p["criteria_hash"], "model": p["model"],
                    "baseline_session_id": p["baseline"].get("session_id"),
                    "adaptive_session_id": p["adaptive"].get("session_id"),
                    "checkpoints": p["checkpoints"], "early_lift": p.get("early_lift"),
                    "final_ratio": p.get("final_ratio")} for p in pairs],
    }


rollout_assessment = assess_rollout


def compare(reports):
    groups = {}
    for report in reports:
        algorithm = _algorithm(report)
        group = groups.setdefault(algorithm, {"sessions": [], "relevant": 0, "unique_discovered": 0,
                                               "judged": 0, "elapsed_seconds": 0, "historical_overlap": 0})
        group["sessions"].append(report.get("session_id"))
        for key in ("relevant", "unique_discovered", "judged", "elapsed_seconds", "historical_overlap"):
            group[key] += report.get(key, 0) or 0
    for group in groups.values():
        group["relevant_per_discovered"] = group["relevant"] / group["unique_discovered"] if group["unique_discovered"] else None
        group["relevant_per_hour"] = group["relevant"] * 3600 / group["elapsed_seconds"] if group["elapsed_seconds"] else None
    return {"groups": groups, "sessions": reports, "rollout_assessment": assess_rollout(reports),
            "interpretation": "Descriptive totals only; rollout gates require matched criteria/model pairs. No automatic winner or flag change."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_ids", nargs="+", type=int)
    args = parser.parse_args()
    reports = []
    with SessionLocal() as db:
        for ident in dict.fromkeys(args.session_ids):
            item = db.get(JobSession, ident)
            if item is None:
                parser.error(f"Session {ident} does not exist")
            reports.append((item.recovery or {}).get("measurement_report") or summary(db, item))
    print(json.dumps(compare(reports), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
