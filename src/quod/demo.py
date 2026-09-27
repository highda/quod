"""Smoke test that walks the whole ladder honestly.

1. Numerical: finite-difference spectrum of the 1D harmonic oscillator -> EMPIRICAL (no error bound, so
   it is *not* numerically certified; the old starter mislabelled this).
2. Formal claims that the engine can read: level spacing and a polynomial identity -> instance check,
   CAS, SMT and, when Lean is installed, the kernel.
"""
from __future__ import annotations

import json

import numpy as np
from scipy.sparse import diags
from scipy.sparse.linalg import eigsh

from . import attack
from .models import Branch, BranchCheckpoint, Claim, Epoch, Evidence, FormalClaim, Rigor
from .paths import artifacts_dir
from .state import StateStore

BRANCH = "demo-ladder"

LEAN_SPACING = """theorem harmonicOscillatorSpacing (n : ℕ) :
    ((n + 1 : ℕ) : ℝ) + (1 / 2 : ℝ) - ((n : ℝ) + (1 / 2 : ℝ)) = 1 := by
  norm_num"""

LEAN_SQUARE = """theorem squareExpansion (a b : ℝ) : (a + b)^2 = a^2 + 2*a*b + b^2 := by
  ring"""


def qho_spectrum(n: int = 1600, xmax: float = 9.0, k: int = 4) -> dict:
    x = np.linspace(-xmax, xmax, n)
    dx = x[1] - x[0]
    interior = x[1:-1]
    m = interior.size
    kinetic = diags([np.full(m - 1, -0.5 / dx**2), np.full(m, 1.0 / dx**2), np.full(m - 1, -0.5 / dx**2)],
                    [-1, 0, 1], format="csr")
    H = kinetic + diags(0.5 * interior**2, 0, format="csr")
    vals, _ = eigsh(H, k=k, which="SA", tol=1e-12)
    vals = np.sort(vals)
    expected = np.arange(k, dtype=float) + 0.5
    err = np.abs(vals - expected)
    return {"grid_points": n, "xmax": xmax, "energies": vals.tolist(), "exact": expected.tolist(),
            "absolute_errors": err.tolist(), "max_abs_error": float(err.max()),
            "note": "second-order finite differences; no rigorous error bound -> empirical only"}


def run(store: StateStore | None = None, n: int = 1600, xmax: float = 9.0, lean: bool = True) -> dict:
    store = store or StateStore()
    if not store.get_branch(BRANCH):
        store.create_branch(Branch(id=BRANCH, problem="Smoke test: 1D quantum harmonic oscillator spectrum and two identities.",
                                   objective="Exercise every rung of the rigor ladder with real engines.", tags=["demo"]))
    store.start_epoch(Epoch(branch_id=BRANCH, model="engine", role="demo"))

    res = qho_spectrum(n=n, xmax=xmax)
    art = artifacts_dir() / "qho_result.json"
    art.write_text(json.dumps(res, indent=2))
    art_rel = f"artifacts/{art.name}"
    if not store.get_claim("C-QHO-001"):
        store.add_claim(Claim(
            id="C-QHO-001", branch_id=BRANCH,
            statement="For H = -(1/2) d²/dx² + x²/2, the four lowest eigenvalues are 1/2, 3/2, 5/2, 7/2.",
            assumptions=["Dirichlet boundary at |x| = xmax approximates the L² problem"],
            evidence=[Evidence(kind="finite_difference", summary=f"max |E_num - (n+1/2)| = {res['max_abs_error']:.3e} at n={n}",
                               produced_by="engine:scipy.eigsh", artifact=art_rel, metadata=res)],
            rigor=Rigor.EMPIRICAL, tags=["spectral"],
        ))
    if not store.get_claim("C-SPACING-001"):
        store.add_claim(Claim(
            id="C-SPACING-001", branch_id=BRANCH,
            statement="With E_n = n + 1/2, consecutive levels are spaced by exactly 1.",
            formal=FormalClaim(variables={"n": "N"}, conclusion="Eq((n + 1) + Rational(1, 2) - (n + Rational(1, 2)), 1)",
                               lean=LEAN_SPACING),
            tags=["algebra"],
        ))
    if not store.get_claim("C-SQUARE-001"):
        store.add_claim(Claim(
            id="C-SQUARE-001", branch_id=BRANCH,
            statement="(a + b)² = a² + 2ab + b² for real a, b.",
            formal=FormalClaim(variables={"a": "R", "b": "R"}, conclusion="Eq((a + b)**2, a**2 + 2*a*b + b**2)",
                               lean=LEAN_SQUARE),
            tags=["algebra"],
        ))
    if not store.get_claim("C-WRONG-001"):
        store.add_claim(Claim(
            id="C-WRONG-001", branch_id=BRANCH,
            statement="(a + b)² = a² + b² for real a, b. (Deliberately false: the engine must refute it.)",
            formal=FormalClaim(variables={"a": "R", "b": "R"}, conclusion="Eq((a + b)**2, a**2 + b**2)"),
            tags=["algebra", "negative-control"],
        ))

    reports = {cid: attack.run(store, cid, lean=lean) for cid in ("C-SPACING-001", "C-SQUARE-001", "C-WRONG-001")}
    final = {cid: store.require_claim(cid).rigor for cid in ("C-QHO-001", "C-SPACING-001", "C-SQUARE-001", "C-WRONG-001")}
    store.checkpoint(BranchCheckpoint(
        branch_id=BRANCH, objective="Exercise every rung of the rigor ladder with real engines.",
        established_claims=[c for c, r in final.items() if r not in (Rigor.SPECULATIVE, Rigor.REFUTED)],
        open_goals=["Certify the QHO eigenvalues with a rigorous bound (Temple/Kato or interval Rayleigh quotients)."],
        next_actions=["Replace finite differences with a Hermite spectral basis and interval-certify E_0.",
                      "If Lean is installed, confirm C-SPACING-001 and C-SQUARE-001 reach formally_proved."],
        artifact_ids=[art_rel],
    ))
    store.end_epoch(BRANCH, "demo complete")
    store.set_branch_status(BRANCH, "closed", "smoke test; not a research branch", force=True)
    return {"qho": res, "attacks": reports, "final_rigor": final}


def expected_ok(result: dict, lean_present: bool) -> list[str]:
    """Return a list of problems (empty == green)."""
    problems = []
    f = result["final_rigor"]
    if result["qho"]["max_abs_error"] > 2e-3:
        problems.append(f"QHO error too large: {result['qho']['max_abs_error']}")
    if f["C-WRONG-001"] != Rigor.REFUTED:
        problems.append("negative control was not refuted")
    top = Rigor.FORMALLY_PROVED if lean_present else Rigor.SOLVER_VERIFIED
    for cid in ("C-SPACING-001", "C-SQUARE-001"):
        if f[cid] not in (Rigor.SOLVER_VERIFIED, Rigor.SYMBOLICALLY_VERIFIED, Rigor.FORMALLY_PROVED):
            problems.append(f"{cid} stalled at {f[cid]}")
        if lean_present and f[cid] != top:
            problems.append(f"{cid} did not reach {top} although Lean is installed")
    return problems
