"""Pure scheduling state: discounted yield per second with mandatory exploration."""
from dataclasses import asdict, dataclass, field
from math import log, sqrt


@dataclass
class Source:
    key: str
    kind: str
    spec: dict
    cluster: str = ""
    page: int = 0
    exhausted: bool = False
    visits: int = 0
    last_turn: int = -1
    raw: float = 0
    novel: float = 0
    judged: float = 0
    relevant: float = 0
    seconds: float = 0
    signatures: list = field(default_factory=list)
    next_refresh: int = 0
    stale_refreshes: int = 0
    retry_after: int = 0
    failures: int = 0

    def score(self, total):
        novelty = (self.novel + 1) / (self.raw + 2)
        relevance = (self.relevant + 1) / (self.judged + 2)
        batch_size = max(1, self.raw / max(1, self.visits))
        cost = max(1, self.seconds / max(1, self.visits))
        return batch_size * novelty * relevance / cost + 0.2 * sqrt(log(total + 2) / (self.visits + 1))


class Scheduler:
    def __init__(self):
        self.sources: dict[str, Source] = {}
        self.turn = 0
        self.recent_clusters: list[str] = []

    def add(self, source: Source):
        # Never discard low yield sources; exact duplicate specs share a cursor.
        if source.key not in self.sources:
            source.last_turn = max(source.last_turn, self.turn - 1)
            self.sources[source.key] = source

    def choose(self):
        available = [s for s in self.sources.values() if not s.exhausted]
        if not available:
            return None
        ready = [s for s in available if s.retry_after <= self.turn]
        if not ready:
            self.turn = min(s.retry_after for s in available)
            ready = [s for s in available if s.retry_after <= self.turn]
        available = ready
        # Ten-page cycle: 60% recommendation preference, 20% productive search,
        # 20% oldest-first exploration. Empty lanes donate their turns.
        slot = self.turn % 10
        if slot in (4, 9):
            candidates = [s for s in available if s.kind not in {"recommendations", "related", "resume", "resume_index"}] or available
            chosen = min(candidates, key=lambda s: (s.last_turn, s.visits))
        else:
            rec = slot in (0, 1, 3, 5, 6, 8)
            candidates = [s for s in available if (s.kind in {"recommendations", "related", "resume", "resume_index"}) == rec] or available
            # Periodic oldest-first recommendation turns cover individual resumes.
            if rec and slot == 8:
                chosen = min(candidates, key=lambda s: (s.last_turn, s.visits))
            else:
                chosen = max(candidates, key=lambda s: (
                    s.score(self.turn) / (1 + 0.2 * self.recent_clusters.count(s.cluster)) if s.cluster else s.score(self.turn),
                    -s.last_turn,
                ))
        chosen.last_turn = self.turn
        self.recent_clusters = [*self.recent_clusters[-19:], chosen.cluster]
        self.turn += 1
        return chosen

    def batch(self, source, raw, novel, seconds):
        source.visits += 1
        for name in ("raw", "novel", "judged", "relevant", "seconds"):
            setattr(source, name, getattr(source, name) * 0.9)
        source.raw += raw
        source.novel += novel
        source.seconds += max(0, seconds)

    def checkpoint(self):
        return {"turn": self.turn, "recent_clusters": self.recent_clusters, "sources": [asdict(s) for s in self.sources.values()]}

    @classmethod
    def restore(cls, data):
        result = cls()
        result.turn = data["turn"]
        result.recent_clusters = data.get("recent_clusters", [])
        for raw in data["sources"]:
            source = Source(**raw)
            result.sources[source.key] = source
        return result
