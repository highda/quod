"""Closed-form and recurrence discovery from a sequence: candidate generation for the conjecture end of a run.

Every candidate is *fitted* on a prefix and *validated* on the terms it never saw, so a formula that merely
interpolates is discarded before it reaches a claim. Fits are exact over ℚ (no floating point), which is why
this stays on the CPU: the linear systems involved are a few dozen unknowns, and the exactness matters more
than the throughput. What comes out is `empirical` evidence and a claim the engines can then attack — a
candidate closed form is a conjecture, not a result.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import sympy as sp

from .models import FormalClaim

n = sp.Symbol("n", integer=True, nonnegative=True)


@dataclass
class Candidate:
    kind: str                       # polynomial | rational-ratio | linear-recurrence | holonomic | scaled
    expr: sp.Expr | None            # closed form in n, when there is one
    recurrence: sp.Expr | None      # expression that must vanish, in terms of a(n), a(n+1), ... when there is one
    fitted_on: int = 0
    validated_on: int = 0
    description: str = ""

    def formal(self, seq_name: str = "a") -> FormalClaim | None:
        """A formal block the engine can attack, when the candidate is an explicit closed form."""
        if self.expr is None:
            return None
        return FormalClaim(variables={"n": "N"}, conclusion=f"Eq({seq_name}(n), {sp.printing.str.sstr(self.expr)})")


@dataclass
class Report:
    terms: int
    candidates: list[Candidate] = field(default_factory=list)
    seconds: float = 0.0

    def best(self) -> Candidate | None:
        ranked = sorted(self.candidates, key=lambda c: (-c.validated_on, _complexity(c)))
        return ranked[0] if ranked else None


def _complexity(c: Candidate) -> int:
    return sp.count_ops(c.expr) if c.expr is not None else (sp.count_ops(c.recurrence) + 5 if c.recurrence is not None else 99)


def _rationals(seq: list) -> list[sp.Rational]:
    return [sp.Rational(str(x)) for x in seq]


def fit_polynomial(seq: list[sp.Rational], max_degree: int = 6) -> Candidate | None:
    """Exact polynomial in n through the first d+1 terms, validated on the rest."""
    for d in range(0, min(max_degree, len(seq) - 2) + 1):
        pts = list(range(d + 1))
        coeffs = sp.symbols(f"c0:{d + 1}", rational=True)
        poly = sum(coeffs[k] * n ** k for k in range(d + 1))
        sol = sp.solve([poly.subs(n, i) - seq[i] for i in pts], coeffs, dict=True)
        if not sol:
            continue
        expr = sp.expand(poly.subs(sol[0]))
        ok = sum(1 for i in range(len(seq)) if sp.simplify(expr.subs(n, i) - seq[i]) == 0)
        if ok == len(seq):
            return Candidate("polynomial", expr, None, d + 1, len(seq) - (d + 1),
                             f"degree-{d} polynomial in n, exact on all {len(seq)} terms")
    return None


def fit_ratio(seq: list[sp.Rational], max_degree: int = 2) -> Candidate | None:
    """Hypergeometric shape: a(n+1)/a(n) a rational function of n of small degree."""
    if any(x == 0 for x in seq[:-1]) or len(seq) < 5:
        return None
    ratios = [seq[i + 1] / seq[i] for i in range(len(seq) - 1)]
    num_c = sp.symbols(f"p0:{max_degree + 1}", rational=True)
    den_c = sp.symbols(f"q0:{max_degree + 1}", rational=True)
    num = sum(num_c[k] * n ** k for k in range(max_degree + 1))
    den = sum(den_c[k] * n ** k for k in range(max_degree + 1))
    eqs = [sp.expand(num.subs(n, i) - ratios[i] * den.subs(n, i)) for i in range(len(ratios))]
    sol = sp.solve([*eqs, den_c[0] - 1], [*num_c, *den_c], dict=True)
    for s in sol or []:
        r = sp.simplify((num / den).subs(s))
        if r.free_symbols - {n}:
            continue
        ok = sum(1 for i in range(len(ratios)) if sp.simplify(r.subs(n, i) - ratios[i]) == 0)
        if ok == len(ratios):
            return Candidate("rational-ratio", None, sp.Symbol("a_next") - r * sp.Symbol("a_n"),
                             len(ratios), 0, f"a(n+1)/a(n) = {r}, exact on all {len(ratios)} ratios")
    return None


def fit_linear_recurrence(seq: list[sp.Rational], max_order: int = 5) -> Candidate | None:
    """Constant-coefficient recurrence a(n+r) = Σ cᵢ a(n+i), fitted on the first rows and validated on the rest."""
    for r in range(1, min(max_order, (len(seq) - 1) // 2) + 1):
        rows = len(seq) - r
        if rows < r + 2:
            continue
        cs = sp.symbols(f"k0:{r}", rational=True)
        fit_rows = [sp.Eq(seq[i + r], sum(cs[j] * seq[i + j] for j in range(r))) for i in range(r)]
        sol = sp.solve(fit_rows, cs, dict=True)
        if not sol:
            continue
        vals = [sol[0].get(c, sp.Integer(0)) for c in cs]
        if any(v.free_symbols for v in vals):
            continue
        good = sum(1 for i in range(rows)
                   if sp.simplify(seq[i + r] - sum(vals[j] * seq[i + j] for j in range(r))) == 0)
        if good == rows:
            terms = " + ".join(f"({v})·a(n+{j})" for j, v in enumerate(vals) if v != 0)
            expr = sp.Symbol(f"a_n_plus_{r}") - sum(vals[j] * sp.Symbol(f"a_n_plus_{j}") for j in range(r))
            closed = _solve_recurrence(vals, seq[:r])
            return Candidate("linear-recurrence", closed, expr, r, rows - r,
                             f"a(n+{r}) = {terms}, exact on all {rows} rows"
                             + (f"; closed form {closed}" if closed is not None else ""))
    return None


def _solve_recurrence(coeffs: list[sp.Rational], initial: list[sp.Rational]) -> sp.Expr | None:
    """Closed form of a constant-coefficient recurrence via its characteristic roots, when SymPy finds one."""
    r = len(coeffs)
    try:
        a = sp.Function("a")
        rel = sp.Eq(a(n + r), sum(coeffs[j] * a(n + j) for j in range(r)))
        sol = sp.rsolve(rel, a(n), {a(i): initial[i] for i in range(r)})
        return sp.simplify(sol) if sol is not None else None
    except Exception:  # noqa: BLE001 - rsolve is best-effort
        return None


def fit_scaled(seq: list[sp.Rational], max_degree: int = 4) -> Candidate | None:
    """Divide out a standard growth factor and look for a polynomial in what remains."""
    factors = {"n!": lambda i: sp.factorial(i), "2**n": lambda i: 2 ** i, "3**n": lambda i: 3 ** i,
               "n**n": lambda i: sp.Integer(i) ** i if i > 0 else sp.Integer(1)}
    for name, f in factors.items():
        try:
            scaled = [seq[i] / f(i) for i in range(len(seq))]
        except Exception:  # noqa: BLE001
            continue
        cand = fit_polynomial(scaled, max_degree=max_degree)
        if cand and cand.expr is not None:
            expr = sp.simplify(cand.expr * sp.sympify(name.replace("!", "actorial(n)").replace("nf", "f") if name == "n!" else name))
            if name == "n!":
                expr = sp.simplify(cand.expr * sp.factorial(n))
            return Candidate("scaled", expr, None, cand.fitted_on, cand.validated_on,
                             f"{name} × ({cand.expr}), exact on all {len(seq)} terms")
    return None


def analyze(seq: list, max_degree: int = 6, max_order: int = 5) -> Report:
    """Run every fitter over a sequence and return the candidates that validated."""
    t0 = time.perf_counter()
    vals = _rationals(seq)
    rep = Report(terms=len(vals))
    for fitter in (lambda: fit_polynomial(vals, max_degree), lambda: fit_linear_recurrence(vals, max_order),
                   lambda: fit_scaled(vals, max_degree), lambda: fit_ratio(vals)):
        try:
            cand = fitter()
        except Exception:  # noqa: BLE001 - a fitter that blows up on a shape is not a failure of the run
            cand = None
        if cand:
            rep.candidates.append(cand)
    rep.seconds = time.perf_counter() - t0
    return rep


def render(rep: Report) -> dict:
    best = rep.best()
    return {
        "terms": rep.terms, "seconds": round(rep.seconds, 2),
        "candidates": [{"kind": c.kind, "closed_form": sp.printing.str.sstr(c.expr) if c.expr is not None else None,
                        "recurrence": sp.printing.str.sstr(c.recurrence) if c.recurrence is not None else None,
                        "fitted_on": c.fitted_on, "validated_on": c.validated_on, "description": c.description}
                       for c in rep.candidates],
        "best": (sp.printing.str.sstr(best.expr) if best and best.expr is not None else
                 (best.description if best else None)),
        "note": ("candidates are conjectures: fitted on a prefix, validated on the remaining terms, and "
                 "`empirical` at best until an engine or the kernel settles them"),
    }
