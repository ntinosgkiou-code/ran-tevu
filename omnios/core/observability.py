"""
observability.py — interpretable decision tracing (self-interpretable pipeline).
Each request yields an ordered set of spans (one per cognitive stage) recording
what was seen (tokenised only), what was decided, confidence and latency.
Retrieve at GET /trace/{id}. Stores only tokenised data — never raw PII.
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Span:
    actor: str
    summary: str
    detail: Dict[str, Any] = field(default_factory=dict)
    confidence: Optional[float] = None
    duration_ms: float = 0.0


class DecisionTrace:
    def __init__(self, trace_id: str) -> None:
        self.trace_id = trace_id
        self.spans: List[Span] = []

    def add(self, actor: str, summary: str, detail: Optional[Dict[str, Any]] = None,
            confidence: Optional[float] = None, duration_ms: float = 0.0) -> None:
        self.spans.append(Span(actor, summary, detail or {}, confidence, round(duration_ms, 2)))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "total_ms": round(sum(s.duration_ms for s in self.spans), 2),
            "stages": len(self.spans),
            "spans": [asdict(s) for s in self.spans],
        }


class TraceStore:
    def __init__(self, cap: int = 200) -> None:
        self._d: Dict[str, DecisionTrace] = {}
        self._order: List[str] = []
        self._cap = cap

    def put(self, trace: DecisionTrace) -> None:
        self._d[trace.trace_id] = trace
        self._order.append(trace.trace_id)
        while len(self._order) > self._cap:
            self._d.pop(self._order.pop(0), None)

    def get(self, trace_id: str) -> Optional[DecisionTrace]:
        return self._d.get(trace_id)


STORE = TraceStore()


def new_trace() -> DecisionTrace:
    return DecisionTrace(uuid.uuid4().hex[:12])
