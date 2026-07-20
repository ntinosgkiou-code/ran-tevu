"""
causal_graph.py — the Clinical Causal DAG the Auditor reasons over.
====================================================================
Hierarchy (per manifest): [diagnosis] → [pathophysiological vector] → [clinical decision].
Two capabilities, both REAL (no branding):

  • Multi-hop justification — `justification(dx, cpt)` returns the explicit causal chain
    (e.g. I10 → cardiovascular_strain → 93000) that explains WHY a procedure follows from a
    diagnosis. The Auditor attaches these chains to the claim as human-readable evidence.

  • Pearl-style intervention — `do(node)` performs graph surgery (removes ALL incoming edges
    of the node), the formal semantics of an intervention in Pearl's do-calculus: once we SET
    a variable, its natural causes no longer explain it. Used to reason about what a decision
    would look like under intervention vs. observation (and honestly scoped: full do-calculus
    identification needs observational data we don't claim to have).

Deterministic, dependency-free DAG (no NetworkX needed for graphs this size — and the
implementation is trivially auditable). Tests: python core/test_causal_graph.py
"""
from __future__ import annotations

from typing import Optional

from omnios_manifest_loader import MANIFEST


class CausalGraph:
    def __init__(self) -> None:
        self._children: dict[str, list[str]] = {}
        self._parents: dict[str, list[str]] = {}
        self._mechanism: dict[tuple[str, str], str] = {}

    # ---- construction ----
    def add_edge(self, cause: str, effect: str, mechanism: str = "") -> None:
        if effect in self._children.get(cause, []):
            return
        if self._creates_cycle(cause, effect):
            raise ValueError(f"edge {cause}→{effect} would create a cycle — a causal DAG must stay acyclic")
        self._children.setdefault(cause, []).append(effect)
        self._parents.setdefault(effect, []).append(cause)
        self._mechanism[(cause, effect)] = mechanism

    def _creates_cycle(self, cause: str, effect: str) -> bool:
        stack, seen = [effect], set()
        while stack:
            node = stack.pop()
            if node == cause:
                return True
            if node in seen:
                continue
            seen.add(node)
            stack.extend(self._children.get(node, []))
        return False

    @classmethod
    def from_manifest(cls, m: dict = MANIFEST) -> "CausalGraph":
        g = cls()
        for cause, effect, mech in m.get("causal_seeds", {}).get("edges", []):
            g.add_edge(cause, effect, mech)
        return g

    # ---- structure ----
    def parents(self, node: str) -> list[str]:
        return list(self._parents.get(node, []))

    def children(self, node: str) -> list[str]:
        return list(self._children.get(node, []))

    def nodes(self) -> list[str]:
        return sorted(set(self._children) | set(self._parents))

    # ---- Pearl intervention: do(X) = graph surgery, remove X's incoming edges ----
    def do(self, node: str) -> "CausalGraph":
        g = CausalGraph()
        for (cause, effect), mech in self._mechanism.items():
            if effect == node:
                continue                     # the intervention severs the node's natural causes
            g.add_edge(cause, effect, mech)
        return g

    # ---- multi-hop reasoning ----
    def paths(self, source: str, target: str, max_hops: int = 3) -> list[list[str]]:
        """All simple directed paths source→target within max_hops (deterministic order)."""
        out: list[list[str]] = []

        def dfs(node: str, trail: list[str]) -> None:
            if len(trail) - 1 > max_hops:
                return
            if node == target:
                out.append(list(trail))
                return
            for child in sorted(self._children.get(node, [])):
                if child not in trail:                      # simple paths only
                    trail.append(child)
                    dfs(child, trail)
                    trail.pop()

        dfs(source, [source])
        return out

    def justification(self, dx_code: str, cpt_code: str,
                      max_hops: int = 3) -> Optional[dict]:
        """The explicit causal chain dx → … → cpt, with per-hop mechanisms — or None."""
        found = self.paths(dx_code, cpt_code, max_hops)
        if not found:
            return None
        path = found[0]                                     # deterministic: first in sorted order
        hops = [{"from": a, "to": b, "mechanism": self._mechanism.get((a, b), "")}
                for a, b in zip(path, path[1:])]
        return {"path": path, "chain": " → ".join(path), "hops": hops}


CLINICAL_GRAPH = CausalGraph.from_manifest()


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    g = CLINICAL_GRAPH
    j = g.justification("I10", "93000")
    print("I10 → 93000:", j["chain"] if j else None)
    print("J02.9 → 93000:", g.justification("J02.9", "93000"))     # None — no causal route
    surged = g.do("cardiovascular_strain")
    print("after do(cardiovascular_strain), I10 still reaches 93000?:",
          bool(surged.justification("I10", "93000")))               # False — intervention severed it
