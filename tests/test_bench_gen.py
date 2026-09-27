import json

from quod import attack, bench_gen
from quod.models import Claim, FormalClaim, Rigor
from quod.state import StateStore


def test_generation_is_deterministic_and_key_is_exact():
    a = bench_gen.generate(seed=3, count=10)
    b = bench_gen.generate(seed=3, count=10)
    assert [p.statement for p in a] == [p.statement for p in b]
    assert {p.family for p in a} == set(bench_gen.FAMILIES)
    for p in a:
        assert p.truth or p.counterexample, f"false statement without counterexample: {p}"


def test_start_and_score(root):
    with StateStore(root / "state" / "g.sqlite3") as s:
        b, probs = bench_gen.start(s, seed=5, count=5)
        assert (root / "state" / "bench" / "gen-5.key.json").exists()
        assert all(p.id in b.problem for p in probs)
        # an honest engine-driven answer for the first polynomial problem
        p0 = probs[0]
        assert p0.family == "poly"
        key = json.loads((root / "state" / "bench" / "gen-5.key.json").read_text())[0]
        sc = bench_gen.score(s, 5)
        assert sc["tally"]["missing"] == 5
        # fabricate: mark a claim verified by hand on a *false* statement -> flagged
        false_ids = [p.id for p in probs if not p.truth]
        true_ids = [p.id for p in probs if p.truth]
        if false_ids:
            fid = false_ids[0]
            s.add_claim(Claim(id=fid, branch_id=b.id, statement="hand"))
            from quod.models import Evidence
            s.add_evidence(fid, Evidence(kind="cas", summary="trust me", produced_by="agent"))
            s.promote(fid, Rigor.SYMBOLICALLY_VERIFIED)
            sc = bench_gen.score(s, 5)
            assert sc["tally"]["false_promotion"] == 1 and sc["tally"]["manual_evidence"] == 1
        if true_ids:
            tid = true_ids[0]
            s.add_claim(Claim(id=tid, branch_id=b.id, statement="x", formal=FormalClaim(variables={"x": "R"}, conclusion="Eq(x, x)")))
            attack.run(s, tid, lean=False)
            sc = bench_gen.score(s, 5)
            assert any(r["id"] == tid and r["verdict"] == "correct" for r in sc["rows"])
