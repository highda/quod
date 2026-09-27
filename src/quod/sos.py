"""Sum-of-squares certificates: guessed numerically, rounded to rationals, verified exactly, handed to Lean.

The pattern this module exists for is the one the ladder likes: a floating-point search proposes a certificate,
exact arithmetic checks it, and what the kernel finally sees is a rational identity with no numerics in it.

For a polynomial `p` over ℝ in the given variables, find a positive semidefinite Gram matrix `Q` with
`p = z^T Q z` where `z` is the monomial basis up to half the degree. Projected gradient descent on the array
backend produces a floating-point `Q`; that `Q` is rounded to rationals, factored by rational LDL^T into
`p = Σ cᵢ · sᵢ²` with `cᵢ ≥ 0`, and the identity is confirmed by SymPy over ℚ. Only then is anything recorded:
the certificate is `cas` evidence (an exact symbolic identity), and the emitted Lean hint list is what makes
`nlinarith` close the inequality in one step instead of searching.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from itertools import combinations_with_replacement

import sympy as sp

from . import gpu
from .claims import parse
from .models import Evidence, FormalClaim
from .paths import artifacts_dir


@dataclass
class SosReport:
    verdict: str                                  # certified | not_found | unsupported
    details: str = ""
    backend: str = ""
    squares: list[tuple[sp.Rational, sp.Expr]] = field(default_factory=list)
    residual: sp.Expr | None = None
    lean_hint: str = ""
    seconds: float = 0.0
    artifact: str | None = None


def monomials(vars_: list[sp.Symbol], half_degree: int) -> list[sp.Expr]:
    out = [sp.Integer(1)]
    for d in range(1, half_degree + 1):
        for combo in combinations_with_replacement(vars_, d):
            out.append(sp.prod(combo))
    return out


def _gram_target(p: sp.Expr, vars_: list[sp.Symbol], basis: list[sp.Expr]) -> tuple[list[sp.Expr], dict]:
    """Linear constraints: each coefficient of p must equal the sum of Q entries whose monomial product matches."""
    poly = sp.Poly(sp.expand(p), *vars_)
    buckets: dict[tuple[int, ...], list[tuple[int, int]]] = {}
    for i, mi in enumerate(basis):
        for j, mj in enumerate(basis):
            key = sp.Poly(sp.expand(mi * mj), *vars_).monoms()[0]
            buckets.setdefault(key, []).append((i, j))
    coeffs = dict(zip(poly.monoms(), poly.coeffs()))
    return [], {"buckets": buckets, "coeffs": coeffs, "n": len(basis)}


def _solve_numeric(spec: dict, b: gpu.Array, rounds: int = 400, seed: int = 0):
    """Alternating projections between the affine set the coefficients impose and the PSD cone.

    Both projections are array operations (`matmul`, `eigh`), so this runs on Metal when MLX is present and on
    numpy otherwise. It is a guess generator: convergence is not required for correctness, because the rational
    rounding downstream either produces an exact identity or is discarded.
    """
    xp = b.mod
    n = spec["n"]
    buckets, coeffs = spec["buckets"], spec["coeffs"]
    keys = sorted(buckets)
    rows = []
    rhs = []
    for key in keys:
        row = [0.0] * (n * n)
        for i, j in buckets[key]:
            row[i * n + j] += 1.0
        rows.append(row)
        rhs.append(float(coeffs.get(key, 0)))
    A = xp.array(rows)
    target = xp.array(rhs)
    pinvA = xp.linalg.pinv(A, stream=xp.cpu) if b.name.startswith("mlx") else xp.linalg.pinv(A)

    # start from the least-squares solution of the affine constraints, then alternate
    q = pinvA @ target
    if seed:
        import numpy as np  # noqa: PLC0415 - a different starting point per restart

        rng = np.random.default_rng(seed)
        q = q + xp.array(rng.normal(scale=0.05, size=(n * n,)).tolist())
    loss = float("inf")
    for _ in range(rounds):
        Q = xp.reshape(q, (n, n))
        Q = (Q + xp.transpose(Q)) / 2
        w, V = xp.linalg.eigh(Q, stream=xp.cpu) if b.name.startswith("mlx") else xp.linalg.eigh(Q)
        w = xp.maximum(w, 0.0)
        Q = (V * w) @ xp.transpose(V)
        q = xp.reshape(Q, (n * n,))
        resid = A @ q - target
        loss = float(xp.sum(resid * resid))
        q = q - pinvA @ resid                      # project back onto the affine set
        if loss < 1e-20:
            break
    Q = xp.reshape(q, (n, n))
    Q = (Q + xp.transpose(Q)) / 2
    return [[float(Q[i, j]) for j in range(n)] for i in range(n)], loss


def _rational_ldl(Q: list[list[float]], basis: list[sp.Expr], denom: int) -> list[tuple[sp.Rational, sp.Expr]]:
    """Round Q to rationals with the given denominator and factor it as Σ cᵢ (Σ Lᵢⱼ mⱼ)² over ℚ."""
    n = len(basis)
    M = sp.Matrix(n, n, lambda i, j: sp.Rational(round(float(Q[i][j]) * denom), denom))
    M = (M + M.T) / 2
    squares: list[tuple[sp.Rational, sp.Expr]] = []
    # symmetric Gaussian elimination; a non-positive pivot means this rounding is not a PSD matrix
    for k in range(n):
        pivot = M[k, k]
        if pivot == 0:
            if any(M[k, j] != 0 for j in range(k, n)):
                return []
            continue
        if pivot < 0:
            return []
        row = [M[k, j] / pivot for j in range(n)]
        squares.append((pivot, sp.expand(sum(row[j] * basis[j] for j in range(n)))))
        for i in range(k, n):
            for j in range(k, n):
                M[i, j] = M[i, j] - pivot * row[i] * row[j]
    return squares


def certify(fc: FormalClaim, half_degree: int | None = None, denominators=(1, 2, 4, 8, 12, 24, 48, 120, 720),
            restarts: int = 3, prefer_gpu: bool = True) -> SosReport:
    """Try to certify `conclusion` (a `p ≥ 0`-shaped inequality) as a rational sum of squares."""
    t0 = time.perf_counter()
    b = gpu.backend(prefer_gpu)
    syms, hyps, concl = parse(fc)
    if hyps:
        return SosReport("unsupported", "hypotheses are not handled: an SOS certificate here would need a "
                                        "Positivstellensatz multiplier for each hypothesis", b.name)
    try:
        kind, diff = gpu.relation_parts(concl)
    except gpu.Unsupported as e:
        return SosReport("unsupported", str(e), b.name)
    if kind not in ("ge", "gt", "le", "lt"):
        return SosReport("unsupported", f"not an inequality: {concl}", b.name)
    # `relation_parts` gives lhs - rhs; for lhs >= rhs that is already the nonnegative side.
    p = sp.expand(diff if kind in ("ge", "gt") else -diff)
    vars_ = sorted(p.free_symbols, key=lambda s: s.name)
    if not vars_:
        return SosReport("unsupported", "no variables: this is arithmetic, not an SOS problem", b.name)
    try:
        poly = sp.Poly(p, *vars_)
    except sp.PolynomialError as e:
        return SosReport("unsupported", f"not polynomial: {e}", b.name)
    deg = poly.total_degree()
    if deg % 2:
        return SosReport("unsupported", f"odd total degree {deg}: no SOS decomposition exists", b.name)
    hd = half_degree or deg // 2
    basis = monomials(vars_, hd)
    _, spec = _gram_target(p, vars_, basis)

    best_loss = None
    for seed in range(restarts):
        Q, loss = _solve_numeric(spec, b, seed=seed)
        best_loss = loss if best_loss is None else min(best_loss, loss)
        if loss > 1e-6:
            continue
        for denom in denominators:
            squares = _rational_ldl(Q, basis, denom)
            if not squares:
                continue
            total = sp.expand(sum(c * sp.expand(s ** 2) for c, s in squares))
            residual = sp.expand(p - total)
            if residual == 0:
                rep = SosReport("certified", f"p = Σ cᵢ sᵢ² exactly over ℚ with {len(squares)} squares "
                                             f"(denominator {denom}, numeric loss {loss:.2e})", b.name,
                                squares=squares, residual=sp.Integer(0),
                                seconds=time.perf_counter() - t0)
                rep.lean_hint = lean_hint(squares)
                return rep
            # a nonnegative remainder is still a valid certificate if it is itself obviously nonnegative
            if residual.is_number and residual >= 0:
                rep = SosReport("certified", f"p = Σ cᵢ sᵢ² + {residual} over ℚ (constant slack)", b.name,
                                squares=squares, residual=residual, seconds=time.perf_counter() - t0)
                rep.lean_hint = lean_hint(squares)
                return rep
    return SosReport("not_found", f"no rational SOS found (best numeric constraint loss {best_loss:.2e}); "
                                  f"try --half-degree {hd + 1} or a finer denominator", b.name,
                     seconds=time.perf_counter() - t0)


def lean_hint(squares: list[tuple[sp.Rational, sp.Expr]]) -> str:
    """The `nlinarith` hint list that turns a search into a check."""
    terms = ", ".join(f"sq_nonneg ({sp.printing.str.sstr(s)})" for _, s in squares)
    return f"nlinarith [{terms}]"


def attack(store, claim_id: str, **kw) -> dict:
    """Certify a claim's inequality and record the exact identity as `cas` evidence, plus the Lean hint."""
    c = store.require_claim(claim_id)
    if not (c.formal and c.formal.engine_readable):
        return {"claim": claim_id, "skipped": "claim has no engine-readable formal block"}
    rep = certify(c.formal, **kw)
    out = {"claim": claim_id, "verdict": rep.verdict, "backend": rep.backend, "details": rep.details,
           "seconds": round(rep.seconds, 2)}
    if rep.verdict != "certified":
        return out
    art = artifacts_dir() / c.branch_id / f"{c.id}.sos.json"
    art.parent.mkdir(parents=True, exist_ok=True)
    art.write_text(json.dumps({"squares": [[str(cc), sp.printing.str.sstr(s)] for cc, s in rep.squares],
                               "residual": str(rep.residual), "lean_hint": rep.lean_hint,
                               "details": rep.details}, indent=2))
    rel = str(art.relative_to(artifacts_dir().parent))
    store.add_evidence(c.id, Evidence(kind="cas", summary=rep.details, produced_by="engine:sos",
                                      artifact=rel, metadata={"lean_hint": rep.lean_hint}))
    out.update({"squares": len(rep.squares), "lean_hint": rep.lean_hint, "artifact": rel,
                "evidence": "cas (the identity is exact over ℚ; the numeric stage only proposed it)"})
    return out
