"""
test_causal_graph.py — the clinical causal DAG + Pearl-style do() intervention.
Run: python core/test_causal_graph.py
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from causal_graph import CLINICAL_GRAPH, CausalGraph


def test_multi_hop_justification():
    j = CLINICAL_GRAPH.justification("I10", "93000")
    assert j and j["path"] == ["I10", "cardiovascular_strain", "93000"]
    assert "hypertension" in j["hops"][0]["mechanism"]
    assert CLINICAL_GRAPH.justification("J02.9", "93000") is None, \
        "pharyngitis must NOT causally justify an ECG"
    print(f"✅ multi-hop: {j['chain']} (with mechanisms) · no path J02.9→93000")


def test_do_intervention_is_graph_surgery():
    g = CLINICAL_GRAPH
    assert g.parents("cardiovascular_strain") == ["I10"]
    surged = g.do("cardiovascular_strain")                  # Pearl: sever incoming edges
    assert surged.parents("cardiovascular_strain") == []
    assert surged.justification("I10", "93000") is None, \
        "after do(strain), hypertension no longer explains the ECG through it"
    # the original graph is untouched (do() is non-destructive)
    assert g.justification("I10", "93000") is not None
    print("✅ do(): intervention severs incoming edges, original DAG untouched (non-destructive surgery)")


def test_acyclicity_enforced():
    g = CausalGraph()
    g.add_edge("A", "B")
    g.add_edge("B", "C")
    try:
        g.add_edge("C", "A")
        assert False, "cycle must be rejected"
    except ValueError:
        pass
    print("✅ DAG invariant: cycles rejected at construction (a causal graph must stay acyclic)")


def test_determinism_and_hop_bound():
    a = CLINICAL_GRAPH.paths("I10", "93000", max_hops=3)
    b = CLINICAL_GRAPH.paths("I10", "93000", max_hops=3)
    assert a == b
    assert CLINICAL_GRAPH.paths("I10", "93000", max_hops=1) == [], "2 hops needed — bound respected"
    print("✅ determinism + hop bound: identical queries → identical paths; max_hops enforced")


def main():
    test_multi_hop_justification()
    test_do_intervention_is_graph_surgery()
    test_acyclicity_enforced()
    test_determinism_and_hop_bound()
    print("\n🎯 CAUSAL GRAPH (clinical DAG · multi-hop justification · Pearl do()): ALL TESTS PASSED")


if __name__ == "__main__":
    main()
