"""Export durable measurements: python -m backend.services.compare_search 12 13."""
import argparse
import json

from backend.persistence.database import SessionLocal
from backend.persistence.models import JobSession
from backend.services.search_metrics import summary


def compare(reports):
    groups = {}
    for report in reports:
        identity = report.get("identity") or {}
        algorithm = identity.get("algorithm", "unmeasured")
        group = groups.setdefault(algorithm, {"sessions": [], "relevant": 0, "unique_discovered": 0,
                                              "judged": 0, "elapsed_seconds": 0, "historical_overlap": 0})
        group["sessions"].append(report["session_id"])
        for key in ("relevant", "unique_discovered", "judged", "elapsed_seconds", "historical_overlap"):
            group[key] += report[key]
    for group in groups.values():
        group["relevant_per_discovered"] = group["relevant"] / group["unique_discovered"] if group["unique_discovered"] else None
        group["relevant_per_hour"] = group["relevant"] * 3600 / group["elapsed_seconds"] if group["elapsed_seconds"] else None
    return {"groups": groups, "sessions": reports, "interpretation": "Descriptive totals only; compare matched criteria/model and equal time windows. Historical overlap, stopping limits and account feedback confound sequential runs. No automatic winner."}


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
