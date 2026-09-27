"""Expand a formal claim into testable instances and run cheap, deterministic attacks.

This is the layer that spends CPU instead of tokens. It never proves an unrestricted theorem;
it kills wrong claims early and produces evidence for the ladder.
"""
from __future__ import annotations

import itertools
import random
from dataclasses import dataclass, field

import mpmath
import sympy as sp

from .claims import free_names, parse
from .models import FormalClaim

SAMPLE_VALUES: dict[str, list[sp.Basic]] = {
    "N": [sp.Integer(i) for i in range(0, 8)],
    "N+": [sp.Integer(i) for i in range(1, 9)],
    "Z": [sp.Integer(i) for i in range(-5, 6)],
    "Q": [sp.Rational(p, q) for p in (-3, -1, 0, 1, 2, 5) for q in (1, 2, 3)],
    "R": [sp.Integer(-2), sp.Rational(-1, 2), sp.Integer(0), sp.Rational(1, 3), sp.Integer(1),
          sp.sqrt(2), sp.pi / 4, sp.Integer(3), sp.E],
    "R+": [sp.Rational(1, 10), sp.Rational(1, 2), sp.Integer(1), sp.sqrt(2), sp.Integer(2), sp.pi, sp.Integer(10)],
    "C": [sp.Integer(0), sp.Integer(1), sp.I, 1 + sp.I, sp.Rational(1, 2) - 2 * sp.I, sp.exp(sp.I * sp.pi / 3)],
}


@dataclass
class InstanceReport:
    checked: int = 0
    passed: int = 0
    skipped: int = 0  # hypotheses not satisfied
    undecided: int = 0
    counterexamples: list[dict] = field(default_factory=list)

    @property
    def outcome(self) -> str:
        if self.counterexamples:
            return "refuted"
        if self.passed and not self.undecided:
            return "survived"
        return "inconclusive"

    def summary(self) -> str:
        return (f"{self.passed}/{self.checked} instances passed, {self.skipped} skipped (hypotheses false), "
                f"{self.undecided} undecided, {len(self.counterexamples)} counterexamples")


MAX_EXPLICIT_TERMS = 200_000  # a ground sum/product longer than this is left to SymPy's own machinery


def expand_finite(expr):
    """Evaluate Sum/Product with concrete integer limits term by term.

    `Sum.doit()` reaches for Euler-Maclaurin on summands it cannot antidifferentiate, which turns a ground
    integer identity such as Sum(floor((k-1)/4), (k, 1, 2019)) into an expression with Derivative(floor(...))
    and no truth value. Explicit summation is exact and, at these lengths, fast.
    """
    if not isinstance(expr, sp.Basic):
        return expr
    for node in sorted(expr.atoms(sp.Sum, sp.Product), key=lambda n: -len(n.args)):
        if len(node.limits) != 1:
            continue
        var, lo, hi = node.limits[0]
        lo, hi = sp.sympify(lo), sp.sympify(hi)
        if not (lo.is_Integer and hi.is_Integer):
            continue
        n = int(hi) - int(lo) + 1
        if n < 0 or n > MAX_EXPLICIT_TERMS:
            continue
        body = expand_finite(node.function)
        terms = [body.subs(var, sp.Integer(i)) for i in range(int(lo), int(hi) + 1)]
        value = (sp.Add(*terms) if isinstance(node, sp.Sum) else sp.Mul(*terms)) if terms else (sp.Integer(0) if isinstance(node, sp.Sum) else sp.Integer(1))
        expr = expr.xreplace({node: value})
    return expr


def _truth(expr: sp.Basic, dps: int = 50) -> bool | None:
    """Decide a substituted relation. Exact first, then high-precision numeric with a tolerance."""
    if isinstance(expr, sp.Basic):
        expr = expand_finite(expr).doit()
    if expr is sp.true or expr == True:  # noqa: E712
        return True
    if expr is sp.false or expr == False:  # noqa: E712
        return False
    try:
        simplified = sp.simplify(expr)
    except Exception:  # noqa: BLE001
        simplified = expr
    if simplified in (sp.true, sp.false):
        return bool(simplified)
    if isinstance(simplified, sp.Rel):
        try:
            lhs, rhs = simplified.lhs, simplified.rhs
            if lhs.has(sp.Integral) or rhs.has(sp.Integral) or lhs.has(sp.Sum) or rhs.has(sp.Sum):
                # ground integrals/sums: explicit where the limits are integers, numeric quadrature otherwise
                lhs, rhs = expand_finite(lhs).doit(), expand_finite(rhs).doit()
            lhs = sp.N(lhs, dps)
            rhs = sp.N(rhs, dps)
            if not (lhs.is_number and rhs.is_number):
                return None
            with mpmath.workdps(dps):
                a, b = mpmath.mpmathify(complex(lhs)), mpmath.mpmathify(complex(rhs))
                tol = mpmath.mpf(10) ** (-(dps - 10))
                if isinstance(simplified, sp.Eq):
                    return bool(abs(a - b) <= tol * (1 + abs(a) + abs(b)))
                if isinstance(simplified, sp.Ne):
                    return bool(abs(a - b) > tol * (1 + abs(a) + abs(b)))
                a, b = a.real, b.real
                if isinstance(simplified, sp.Lt):
                    return bool(a < b - tol)
                if isinstance(simplified, sp.Le):
                    return bool(a <= b + tol)
                if isinstance(simplified, sp.Gt):
                    return bool(a > b + tol)
                if isinstance(simplified, sp.Ge):
                    return bool(a >= b - tol)
        except Exception:  # noqa: BLE001
            return None
    return None


def instances(fc: FormalClaim, max_instances: int = 400, seed: int = 0) -> InstanceReport:
    syms, hyps, concl = parse(fc)
    names = free_names(fc)
    grids = [SAMPLE_VALUES[fc.variables[n]] for n in names]
    combos = list(itertools.product(*grids)) if names else [()]
    if len(combos) > max_instances:
        random.Random(seed).shuffle(combos)
        combos = combos[:max_instances]
    rep = InstanceReport()
    for combo in combos:
        subs = {syms[n]: v for n, v in zip(names, combo)}
        hyp_ok = True
        for h in hyps:
            t = _truth(h.subs(subs))
            if t is not True:
                hyp_ok = False
                break
        if not hyp_ok:
            rep.skipped += 1
            continue
        rep.checked += 1
        t = _truth(concl.subs(subs))
        if t is True:
            rep.passed += 1
        elif t is False:
            rep.counterexamples.append({n: str(v) for n, v in zip(names, combo)})
        else:
            rep.undecided += 1
    return rep


@dataclass
class SymbolicReport:
    verdict: str  # verified | false | inconclusive
    method: str
    details: str


def symbolic(fc: FormalClaim) -> SymbolicReport:
    """Try to establish the conclusion with CAS identities. Hypotheses are used only via symbol
    assumptions (domains); a claim whose truth depends on a non-domain hypothesis will come back
    inconclusive rather than wrongly verified."""
    syms, hyps, concl = parse(fc)
    if isinstance(concl, sp.Eq):
        diff = concl.lhs - concl.rhs
        for name, fn in (("simplify", sp.simplify), ("expand", sp.expand), ("trigsimp", sp.trigsimp),
                         ("doit+simplify", lambda e: sp.simplify(e.doit())),
                         ("finite+simplify", lambda e: sp.simplify(expand_finite(e).doit())), ("nsimplify", sp.nsimplify)):
            try:
                r = fn(diff)
            except Exception:  # noqa: BLE001
                continue
            if r == 0:
                return SymbolicReport("verified", f"cas:{name}", f"lhs - rhs reduces to 0 via {name}")
            if r.is_number and r.is_nonzero:
                return SymbolicReport("false", f"cas:{name}", f"lhs - rhs reduces to nonzero constant {r}")
        return SymbolicReport("inconclusive", "cas", "no rewrite reduced lhs - rhs to 0")
    if isinstance(concl, sp.Rel):
        try:
            r = sp.simplify(expand_finite(concl).doit())
        except Exception as e:  # noqa: BLE001
            return SymbolicReport("inconclusive", "cas", f"simplify raised {e}")
        if r is sp.true:
            return SymbolicReport("verified", "cas:simplify", "relation simplifies to True under domain assumptions")
        if r is sp.false:
            return SymbolicReport("false", "cas:simplify", "relation simplifies to False under domain assumptions")
        return SymbolicReport("inconclusive", "cas", f"relation simplifies to {r}, not a truth value")
    try:
        r = sp.simplify(concl)
        if r is sp.true:
            return SymbolicReport("verified", "cas:simplify", "boolean simplifies to True")
        if r is sp.false:
            return SymbolicReport("false", "cas:simplify", "boolean simplifies to False")
    except Exception:  # noqa: BLE001
        pass
    return SymbolicReport("inconclusive", "cas", "unsupported conclusion shape for CAS check")


def identify_constant(value: str | float, dps: int = 30) -> dict:
    """Guess a closed form for a numeric constant. Output is a *speculative* hint, never evidence."""
    with mpmath.workdps(dps):
        x = mpmath.mpf(str(value))
        guesses = mpmath.identify(x, ["pi", "e", "sqrt(2)", "sqrt(3)", "log(2)", "euler", "catalan"])
        ns = sp.nsimplify(sp.Float(str(x), dps), [sp.pi, sp.E, sp.EulerGamma, sp.Catalan], tolerance=10 ** -(dps - 5))
    return {"value": str(x), "mpmath_identify": guesses, "sympy_nsimplify": str(ns), "rigor": "speculative"}
