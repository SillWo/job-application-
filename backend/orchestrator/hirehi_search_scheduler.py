"""Pure adaptive scheduling state for HireHi (V1--V3)."""
from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass, field
from math import isfinite, sqrt
from statistics import median
from typing import Any

from backend.intelligence.hirehi_adaptive_planner import SourceStats


def _encode_rng(state: object) -> list[Any]:
    """Convert random's tuple state into JSON-safe primitives."""
    if isinstance(state, tuple):
        return [_encode_rng(value) if isinstance(value, tuple) else value for value in state]
    return state  # type: ignore[return-value]


def _decode_rng(value: Any) -> tuple[Any, ...]:
    """Strictly validate and convert a JSON random state back to tuples."""
    if not isinstance(value, list) or len(value) != 3 or not isinstance(value[0], int) or value[0] != 3:
        raise ValueError("invalid random state version/shape")
    vector = value[1]
    if not isinstance(vector, list) or len(vector) != 625 or any(not isinstance(x, int) or isinstance(x, bool) for x in vector):
        raise ValueError("invalid random state vector")
    if value[2] is not None and not isinstance(value[2], (int, float)):
        raise ValueError("invalid random state gaussian cache")
    return (value[0], tuple(vector), value[2])


@dataclass
class Source:
    key: str
    family: str
    spec: dict[str, Any]
    cluster: str = ""
    cursor: Any = 0
    exhausted: bool = False
    raw_discovered: int = 0
    unique_discovered: int = 0
    analyzed: int = 0
    relevant: int = 0
    duplicates: int = 0
    failures: int = 0
    elapsed_seconds: float = 0.0
    novelty: float = 1.0
    availability: float = 1.0
    cost: float = 0.0
    cooldown_until: int | None = None
    next_refresh: int = 0
    last_used_at: int | None = None
    batch_count: int = 0
    dynamic: bool = False
    signatures: list[str] = field(default_factory=list)
    consecutive_repeats: int = 0
    prior_successes: float = 0.0
    prior_failures: float = 0.0
    origin: str = "planned"
    protected: bool = False

    @property
    def kind(self) -> str:
        return self.family

    @property
    def page(self) -> Any:
        return self.cursor

    @page.setter
    def page(self, value: Any) -> None:
        self.cursor = value

    @property
    def visits(self) -> int:
        return self.batch_count

    @visits.setter
    def visits(self, value: int) -> None:
        self.batch_count = value

    @property
    def precision(self) -> float:
        return self.relevant / self.analyzed if self.analyzed else 0.0

    @property
    def posterior(self) -> tuple[float, float]:
        return self.relevant + max(0.0, self.prior_successes) + 1, self.analyzed - self.relevant + max(0.0, self.prior_failures) + 1

    def stats(self) -> SourceStats:
        return SourceStats(
            source_id=self.key, family=self.family, raw_discovered=self.raw_discovered,
            unique_discovered=self.unique_discovered, analyzed=self.analyzed,
            relevant=self.relevant, duplicates=self.duplicates, failures=self.failures,
            elapsed_seconds=self.elapsed_seconds, cursor=self.cursor, exhausted=self.exhausted,
            cooldown_until=self.cooldown_until, last_used_at=self.last_used_at,
            novelty=self.novelty, availability=self.availability, cost=self.cost,
        )

    def sample_score(self, rng: random.Random, cost_lambda: float = 0.25, typical_cost: float | None = None) -> float:
        alpha, beta = self.posterior
        theta = rng.betavariate(alpha, beta)
        ratio = self.cost / typical_cost if typical_cost and self.cost > 0 else 1.0
        return theta * max(0.01, self.novelty) * max(0.01, self.availability) / (1 + cost_lambda * ratio)


class HireHiSearchScheduler:
    """80/20 Thompson scheduler with bounded, reversible cooldowns."""

    CALIBRATION_ANALYSES = 10
    NORMAL_BATCH = 8
    EXPLOIT_BATCH = 15
    EXPLORE_BATCH = 5
    MAX_ACTIVE = 12

    def __init__(self, *, seed: int = 0, rng: random.Random | None = None, cost_lambda: float = 0.25,
                 quality_target: float = 0.20):
        self.sources: dict[str, Source] = {}
        self.turn = 0
        self.rng = rng or random.Random(seed)
        self.cost_lambda = cost_lambda
        self.quality_target = quality_target
        self.exploitation_rounds = 0
        self.exploration_rounds = 0

    def add(self, source: Source | dict[str, Any], *, key: str | None = None) -> Source:
        if isinstance(source, dict):
            source = Source(key=key or str(source.get("key") or source.get("source_id")),
                            family=source.get("family", source.get("kind", "query")), spec=source.get("spec", source))
        if source.key not in self.sources and len(self.sources) < self.MAX_ACTIVE:
            self.sources[source.key] = source
        return self.sources.get(source.key, source)

    def _ready(self) -> list[Source]:
        ready = []
        for source in self.sources.values():
            if source.cooldown_until is not None and self.turn >= source.cooldown_until:
                source.cooldown_until = None
                source.availability = 1.0
            if source.exhausted and self.turn >= source.next_refresh:
                source.exhausted = False
                source.cursor = 0
                source.signatures.clear()
                source.availability = 1.0
            if not source.exhausted and (source.cooldown_until is None or self.turn >= source.cooldown_until):
                ready.append(source)
        return ready

    def choose(self) -> Source | None:
        available = self._ready()
        if not available:
            # The wall-clock wait belongs to the workflow.  Do not jump the
            # logical turn here: doing so can spin a source during cooldown.
            available = []
        if not available:
            return None
        explore = self.turn % 5 == 0
        if explore:
            self.exploration_rounds += 1
            chosen = min(available, key=lambda s: (s.analyzed, s.batch_count, s.last_used_at if s.last_used_at is not None else -1, s.key))
        else:
            self.exploitation_rounds += 1
            uncalibrated = [s for s in available if s.analyzed < self.CALIBRATION_ANALYSES]
            candidates = uncalibrated or available
            measured = [s.cost for s in candidates if s.cost > 0]
            typical_cost = median(measured) if measured else None
            chosen = max(candidates, key=lambda s: (s.sample_score(self.rng, self.cost_lambda, typical_cost), -s.analyzed, s.key))
        chosen.last_used_at = self.turn
        self.turn += 1
        return chosen

    def advance_to_wakeup(self) -> bool:
        """Advance logical time once, after the caller's wall-clock wait."""
        wakeups = [x for source in self.sources.values() for x in (source.cooldown_until, source.next_refresh)
                   if x is not None and x > self.turn]
        if not wakeups:
            return False
        self.turn = min(wakeups)
        return True

    def batch_size(self, source: Source, *, exploration: bool | None = None) -> int:
        if exploration is None:
            exploration = (max(0, self.turn - 1) % 5) == 0
        if source.analyzed < self.CALIBRATION_ANALYSES:
            return self.CALIBRATION_ANALYSES - source.analyzed
        if exploration:
            return self.EXPLORE_BATCH
        if source.precision >= self.quality_target:
            return self.EXPLOIT_BATCH
        return self.NORMAL_BATCH

    def update(self, source: Source, *, raw: int = 0, unique: int = 0, analyzed: int = 0,
               relevant: int = 0, seconds: float = 0.0, failures: int = 0,
               count_batch: bool = True) -> None:
        if count_batch:
            source.batch_count += 1
        source.raw_discovered += max(0, raw)
        source.unique_discovered += max(0, unique)
        source.analyzed += max(0, analyzed)
        source.relevant += min(max(0, relevant), max(0, analyzed))
        source.elapsed_seconds += max(0.0, seconds)
        source.failures += max(0, failures)
        source.novelty = (source.unique_discovered + 1) / (source.raw_discovered + 2)
        source.availability = 1.0 if not source.exhausted else 0.15
        source.cost = source.elapsed_seconds / max(1, source.analyzed)
        if source.analyzed >= 25 and self.should_cooldown(source):
            source.cooldown_until = self.turn + 3
            source.availability = 0.25
            source.next_refresh = max(source.next_refresh, self.turn + 6)

    def observe(self, source: Source, relevant: bool, *, analyzed: bool = True) -> None:
        self.update(source, analyzed=int(analyzed), relevant=int(bool(relevant)) if analyzed else 0)

    def apply_prior(self, source: Source, successes: float = 0.0, failures: float = 0.0) -> None:
        source.prior_successes = min(100.0, max(0.0, float(successes)))
        source.prior_failures = min(100.0, max(0.0, float(failures)))

    def should_cooldown(self, source: Source) -> bool:
        if source.analyzed < 25:
            return False
        n = source.analyzed
        mean = (source.relevant + 1) / (n + 2)
        upper = min(1.0, mean + 1.96 * sqrt(max(0.0, mean * (1 - mean) / (n + 3))))
        return upper < self.quality_target

    def mark_exhausted(self, source: Source, *, refresh_after: int = 1) -> None:
        source.exhausted = True
        source.availability = 0.15
        source.next_refresh = self.turn + max(1, refresh_after)

    def checkpoint(self) -> dict[str, Any]:
        result = {
            "turn": self.turn, "cost_lambda": self.cost_lambda, "quality_target": self.quality_target,
            "exploration_rounds": self.exploration_rounds, "exploitation_rounds": self.exploitation_rounds,
            "rng_state": _encode_rng(self.rng.getstate()), "sources": [asdict(s) for s in self.sources.values()],
        }
        try:
            json.dumps(result)
        except (TypeError, ValueError) as exc:
            raise ValueError("scheduler checkpoint contains non-JSON state") from exc
        return result

    @classmethod
    def restore(cls, data: dict[str, Any]) -> HireHiSearchScheduler:
        result = cls(cost_lambda=float(data.get("cost_lambda", 0.25)), quality_target=float(data.get("quality_target", 0.20)))
        result.turn = int(data.get("turn", 0))
        result.exploration_rounds = int(data.get("exploration_rounds", 0))
        result.exploitation_rounds = int(data.get("exploitation_rounds", 0))
        if "rng_state" not in data:
            raise ValueError("checkpoint missing random state")
        try:
            result.rng.setstate(_decode_rng(data["rng_state"]))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("invalid random state") from exc
        for raw in data.get("sources", []):
            for prior_key in ("prior_successes", "prior_failures"):
                prior = raw.get(prior_key, 0.0)
                if isinstance(prior, bool) or not isinstance(prior, (int, float)) or not isfinite(float(prior)) or prior < 0:
                    raise ValueError("invalid source prior")
            result.add(Source(**raw))
        return result

    def next_retry_delay(self, *, cap: float = 128.0) -> float:
        """Bounded seconds until a cooldown/refresh may be attempted."""
        wakeups = [x for source in self.sources.values() for x in (source.cooldown_until, source.next_refresh)
                   if x is not None and x > self.turn]
        return min(float(cap), float(max(1, min(wakeups) - self.turn))) if wakeups else min(float(cap), 1.0)


Scheduler = HireHiSearchScheduler
__all__ = ["Source", "SourceStats", "HireHiSearchScheduler", "Scheduler"]
