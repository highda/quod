from quod import attack, demo, formal
from quod.library import Library, LibraryError
from quod.models import Branch, Claim, FormalClaim, Rigor
from quod.state import StateStore
import pytest


def test_attack_climbs_ladder_and_refutes(root):
    with StateStore(root / "state" / "a.sqlite3") as s:
        s.create_branch(Branch(id="B", problem="p", objective="o"))
        s.add_claim(Claim(id="GOOD", branch_id="B", statement="s",
                          formal=FormalClaim(variables={"x": "R"}, conclusion="Eq((x+1)**2, x**2+2*x+1)")))
        s.add_claim(Claim(id="BAD", branch_id="B", statement="s",
                          formal=FormalClaim(variables={"x": "R"}, conclusion="Eq((x+1)**2, x**2+1)")))
        s.add_claim(Claim(id="PROSE", branch_id="B", statement="no formal block"))
        good = attack.run(s, "GOOD", lean=False)
        assert good["final_rigor"] in (Rigor.SYMBOLICALLY_VERIFIED, Rigor.SOLVER_VERIFIED)
        assert attack.run(s, "BAD", lean=False)["final_rigor"] == Rigor.REFUTED
        assert "skipped" in attack.run(s, "PROSE")
        assert (root / "artifacts" / "B" / "BAD.instances.json").exists()
        stats = s.attack_stats()
        assert stats["engine:expand"]["refuted"] == 1


def test_library_accepts_only_verified(root):
    with StateStore(root / "state" / "l.sqlite3") as s:
        s.create_branch(Branch(id="B", problem="p", objective="o"))
        s.add_claim(Claim(id="C", branch_id="B", statement="binomial square identity",
                          formal=FormalClaim(variables={"x": "R"}, conclusion="Eq((x+1)**2, x**2+2*x+1)"), tags=["algebra"]))
        lib = Library(root / "state" / "lib.sqlite3")
        with pytest.raises(LibraryError):
            lib.from_claim(s.require_claim("C"))
        attack.run(s, "C", lean=False)
        e = lib.from_claim(s.require_claim("C"))
        assert lib.search("binomial")[0].id == e.id
        assert "binomial" in lib.export_markdown()
        lib.close()


def test_demo_ladder_is_green(root):
    with StateStore(root / "state" / "d.sqlite3") as s:
        res = demo.run(s, n=900, xmax=8.0, lean=False)
        assert demo.expected_ok(res, lean_present=False) == []
        assert res["final_rigor"]["C-QHO-001"] == Rigor.EMPIRICAL  # honest label: no error bound
        assert res["final_rigor"]["C-WRONG-001"] == Rigor.REFUTED
        # idempotent: a second run must not trip on existing claims
        res2 = demo.run(s, n=900, xmax=8.0, lean=False)
        assert demo.expected_ok(res2, lean_present=False) == []


def test_lean_sorry_never_counts(root):
    rep = formal.check("theorem t : 1 = 1 := by sorry")
    assert rep.verdict in ("statement_ok", "statement_error", "unavailable")
    assert rep.verdict != "proved"


def test_lean_only_formal_block(root):
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        FormalClaim()
    fc = FormalClaim(lean="theorem t : 1 = 1 := by sorry")
    assert not fc.engine_readable
    with StateStore(root / "state" / "lo.sqlite3") as s:
        s.create_branch(Branch(id="B", problem="p", objective="o"))
        s.add_claim(Claim(id="T", branch_id="B", statement="target", formal=fc))
        rep = attack.run(s, "T", lean=False)
        assert rep["final_rigor"] == Rigor.SPECULATIVE and "skipped" in rep["steps"][0]


def test_formal_unwrap_strips_scratch_wrapper():
    src = "import Mathlib\n\nnamespace Quod.Scratch\n\ntheorem t : 1 = 1 := by rfl\n\nend Quod.Scratch\n"
    assert formal.unwrap(src) == "theorem t : 1 = 1 := by rfl"
    # opens/set_options between the imports and the namespace are part of the checked file: keep them
    src2 = "import Mathlib\n\nopen Polynomial\n\nnamespace Quod.Scratch\n\ntheorem t : 1 = 1 := by rfl\n\nend Quod.Scratch\n"
    assert formal.unwrap(src2) == "open Polynomial\n\ntheorem t : 1 = 1 := by rfl"


def test_set_lean_replaces_claim_text(root):
    with StateStore(root / "state" / "s.sqlite3") as s:
        s.create_branch(Branch(id="B", problem="p", objective="o"))
        s.add_claim(Claim(id="C", branch_id="B", statement="s",
                          formal=FormalClaim(variables={"x": "R"}, conclusion="Eq(x, x)", lean="theorem old : 1 = 1 := by sorry")))
        s.set_lean("C", "theorem new : 1 = 1 := by rfl")
        assert s.require_claim("C").formal.lean.startswith("theorem new")
