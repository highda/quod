import json

from quod import sources
from quod.state import StateStore

PUTNAM = """import Mathlib

open Nat

/--
Show that every positive integer is a sum of distinct powers of two.
-/
abbrev putnam_1900_a1_solution : ℕ := sorry
-- 42
theorem putnam_1900_a1
(n : ℕ) (hn : 0 < n)
: ∃ s : Finset ℕ, n = ∑ k ∈ s, 2 ^ k :=
sorry
"""


def test_parse_lean_problem():
    doc, stmt = sources.parse_lean_problem(PUTNAM)
    assert doc.startswith("Show that every positive integer")
    assert stmt.startswith("theorem putnam_1900_a1") and stmt.endswith("sorry")


def test_oeis_conjecture_lines():
    e = {"formula": ["a(n) = 2*a(n-1).", "Conjecture: a(n) = n^2 + 1 for n > 3."], "comment": ["It is conjectured that ..."]}
    assert len(sources.oeis_conjecture_lines(e)) == 2


def test_open_branch_with_lean_target(root, monkeypatch):
    monkeypatch.setattr(sources, "fetch", lambda url, ttl_ok=True: PUTNAM)
    p = sources.putnam_problem("putnam_1900_a1")
    assert p.lean and "open Nat" in p.lean and "import" not in p.lean
    assert "42" not in p.lean and "abbrev putnam_1900_a1_solution" in p.lean  # answer comment stripped, abbrev kept
    with StateStore(root / "state" / "s.sqlite3") as s:
        b = sources.open_branch(s, p)
        assert b.id == "putnam-putnam-1900-a1"
        target = s.get_claim(f"{b.id}-target")
        assert target and target.formal.lean.endswith("sorry")
        assert any(ev["type"] == "source.imported" for ev in s.events(b.id))


def test_oeis_problem_from_cached_json(root, monkeypatch):
    entry = {"number": 45, "name": "Fibonacci numbers", "data": "0,1,1,2,3,5", "keyword": "nonn,core",
             "formula": ["Conjecture: a(n) ~ phi^n/sqrt(5)."], "comment": []}
    monkeypatch.setattr(sources, "fetch", lambda url, ttl_ok=True: json.dumps(entry))
    p = sources.oeis_problem("A000045")
    assert p.lean is None and "Conjecture lines" in p.statement and p.extra["conjectures"]


def test_fc_module_name_and_restatement(monkeypatch):
    assert sources.fc_module("ErdosProblems/1.lean") == "FormalConjectures.ErdosProblems.«1»"
    assert sources.fc_module("OEIS/100434.lean") == "FormalConjectures.OEIS.«100434»"
    fc_text = """/-! # Erdős 1 -/
open Finset
namespace Erdos1
def IsSumDistinctSet (A : Finset ℕ) (N : ℕ) : Prop := sorry
@[category research open, AMS 5 11]
theorem erdos_1 : ¬ ∃ C > 0, ∀ N : ℕ, N ≠ 0 → C < N := by
  sorry
end Erdos1
"""
    monkeypatch.setattr(sources, "fetch", lambda url, ttl_ok=True: fc_text)
    p = sources.fc_problem("ErdosProblems/1.lean")
    assert p.lean.startswith("import FormalConjectures.ErdosProblems.«1»")
    assert "open Erdos1" in p.lean and "theorem erdos_1_quod" in p.lean and "@[category" not in p.lean
