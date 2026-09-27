from quod import formal
from quod.models import FormalClaim


def test_forbidden_constructs_rejected(root):
    for bad in ("theorem t : 1 = 1 := by native_decide", "axiom foo : False\ntheorem t : False := foo",
                "unsafe def x : Nat := 1\ntheorem t : 1 = 1 := rfl"):
        rep = formal.check(bad)
        assert rep.verdict in ("failed", "unavailable"), bad
        if rep.verdict == "failed":
            assert "forbidden" in rep.details


def test_signatures_and_frozen_violations():
    src = "abbrev sol : ℕ := sorry\n\ntheorem main (n : ℕ) (h : 0 < n) :\n  n ≥ 1 := by\n  sorry\n"
    sig = formal.signatures(src)
    assert set(sig) == {"sol", "main"}
    frozen = list(sig.values())
    filled = src.replace("abbrev sol : ℕ := sorry", "abbrev sol : ℕ := 7").replace("by\n  sorry", "by omega")
    assert formal.frozen_violations(frozen, filled) == []
    weakened = filled.replace("(h : 0 < n)", "(h : 1 < n)")
    assert any("main" in v for v in formal.frozen_violations(frozen, weakened))
    assert any("missing" in v for v in formal.frozen_violations(frozen, "theorem other : True := trivial"))
    fc = FormalClaim(lean=src, frozen=frozen)
    assert fc.frozen == frozen


def test_axiom_check_parsing():
    import re
    out = "'main' depends on axioms: [propext, Classical.choice, Quot.sound]\n'x' depends on axioms: [propext, myAxiom]"
    used = set()
    for m in re.finditer(r"depends on axioms: \[([^\]]*)\]", out):
        used |= {a.strip() for a in m.group(1).split(",")}
    assert used - formal.ALLOWED_AXIOMS == {"myAxiom"}


def test_hammer_suggestion_parsing():
    import re
    out = "x.lean:4:59: warning: redundant\nTry these:\n  [apply] grind only [sq_le_sq]\n  [apply] grind => instantiate only [sq_le_sq]\n"
    sugg = []
    for block in formal.TRY_THIS_RE.findall(out):
        for line in block.splitlines():
            line = re.sub(r"^\s*\[\w+\]\s*", "", line).strip()
            if line:
                sugg.append(line)
    assert sugg[0] == "grind only [sq_le_sq]" and len(sugg) == 2


def test_qualified_theorem_names_track_namespaces():
    src = "namespace Foo\ntheorem a : 1 = 1 := rfl\nnamespace Bar\nlemma b : 2 = 2 := rfl\nend Bar\nend Foo\ntheorem c : 3 = 3 := rfl\n"
    assert formal.qualified_theorem_names(src) == ["Foo.a", "Foo.Bar.b", "c"]


def test_errors_first_keeps_first_error():
    out = "x.lean:1:0: warning: w1\nx.lean:50:2: error: FIRST\n  detail\nx.lean:900:2: error: LAST\n"
    d = formal._errors_first(out, limit=60)
    assert d.startswith("x.lean:50:2: error: FIRST")
