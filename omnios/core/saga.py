"""
saga.py — Saga orchestration with compensation (distributed-transaction pattern).
=================================================================================
A booking spans multiple systems (reserve in the ERP/PMS, then issue a
confirmation). If a later step fails, every completed step is compensated in
reverse order — the system never ends in a half-committed state. This is the
Saga pattern (Garcia-Molina & Salem, 1987): no global lock, eventual consistency
via explicit rollback.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, List, Tuple


@dataclass
class Step:
    name: str
    action: Callable[[], Any]
    compensate: Callable[[Any], None]


class SagaError(Exception):
    def __init__(self, failed_step: str, original: Exception):
        self.failed_step = failed_step
        self.original = original
        super().__init__(f"saga failed at '{failed_step}': {original!r}")


def run_saga(steps: List[Step]) -> List[Any]:
    """Execute steps in order; on failure compensate completed steps in reverse."""
    completed: List[Tuple[Step, Any]] = []
    for step in steps:
        try:
            result = step.action()
            completed.append((step, result))
            print(f"[SAGA] step '{step.name}' OK")
        except Exception as exc:
            for done_step, done_result in reversed(completed):
                try:
                    done_step.compensate(done_result)
                    print(f"[SAGA COMPENSATE] '{done_step.name}' rolled back")
                except Exception as comp_exc:
                    print(f"[SAGA] compensation error in '{done_step.name}': {comp_exc!r}")
            raise SagaError(step.name, exc) from exc
    return [r for _, r in completed]
