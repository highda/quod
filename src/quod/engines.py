"""Exact/solver engines behind the ladder: SMT via Z3, interval evaluation via mpmath.iv."""
from __future__ import annotations

import importlib.util
from dataclasses import dataclass

import mpmath
import sympy as sp

from .claims import decided_at_parse, parse
from .models import FormalClaim


@dataclass
class SmtReport:
    verdict: str  # verified | counterexample | unknown | unavailable | unsupported | trivial
    details: str
    model: dict | None = None


def z3_available() -> bool:
    return importlib.util.find_spec("z3") is not None


class _Unsupported(Exception):
    pass


def _to_z3(expr: sp.Basic, env: dict, z3):
    if expr in env:
        return env[expr]
    if isinstance(expr, sp.Integer):
        return z3.IntVal(int(expr)) if env.get("__int_mode__") else z3.RealVal(int(expr))
    if isinstance(expr, sp.Rational):
        return z3.RealVal(f"{expr.p}/{expr.q}")
    if isinstance(expr, sp.Add):
        args = [_to_z3(a, env, z3) for a in expr.args]
        r = args[0]
        for a in args[1:]:
            r = r + a
        return r
    if isinstance(expr, sp.Mul):
        args = [_to_z3(a, env, z3) for a in expr.args]
        r = args[0]
        for a in args[1:]:
            r = r * a
        return r
    if isinstance(expr, sp.Pow):
        base, exp = expr.args
        if isinstance(exp, sp.Integer):
            n = int(exp)
            b = _to_z3(base, env, z3)
            if n >= 0:
                r = z3.RealVal(1) if not env.get("__int_mode__") else z3.IntVal(1)
                for _ in range(n):
                    r = r * b
                return r
            r = z3.RealVal(1)
            for _ in range(-n):
                r = r * b
            return z3.RealVal(1) / r
        raise _Unsupported(f"non-integer exponent {exp}")
    if isinstance(expr, sp.Eq):
        return _to_z3(expr.lhs, env, z3) == _to_z3(expr.rhs, env, z3)
    if isinstance(expr, sp.Ne):
        return _to_z3(expr.lhs, env, z3) != _to_z3(expr.rhs, env, z3)
    if isinstance(expr, sp.Lt):
        return _to_z3(expr.lhs, env, z3) < _to_z3(expr.rhs, env, z3)
    if isinstance(expr, sp.Le):
        return _to_z3(expr.lhs, env, z3) <= _to_z3(expr.rhs, env, z3)
    if isinstance(expr, sp.Gt):
        return _to_z3(expr.lhs, env, z3) > _to_z3(expr.rhs, env, z3)
    if isinstance(expr, sp.Ge):
        return _to_z3(expr.lhs, env, z3) >= _to_z3(expr.rhs, env, z3)
    if isinstance(expr, sp.And):
        return z3.And(*[_to_z3(a, env, z3) for a in expr.args])
    if isinstance(expr, sp.Or):
        return z3.Or(*[_to_z3(a, env, z3) for a in expr.args])
    if isinstance(expr, sp.Not):
        return z3.Not(_to_z3(expr.args[0], env, z3))
    if isinstance(expr, sp.Implies):
        return z3.Implies(_to_z3(expr.args[0], env, z3), _to_z3(expr.args[1], env, z3))
    if isinstance(expr, sp.Abs):
        a = _to_z3(expr.args[0], env, z3)
        return z3.If(a >= 0, a, -a)
    if isinstance(expr, sp.Piecewise):
        # Piecewise((v1, c1), ..., (vn, True)) -> nested If; a missing default is unsupported.
        pairs = list(expr.args)
        if pairs[-1].cond is not sp.true:
            raise _Unsupported("Piecewise without a final `True` branch")
        result = _to_z3(pairs[-1].expr, env, z3)
        for piece in reversed(pairs[:-1]):
            result = z3.If(_to_z3(piece.cond, env, z3), _to_z3(piece.expr, env, z3), result)
        return result
    if isinstance(expr, (sp.Max, sp.Min)):
        args = [_to_z3(a, env, z3) for a in expr.args]
        r = args[0]
        for a in args[1:]:
            r = z3.If(r >= a, r, a) if isinstance(expr, sp.Max) else z3.If(r <= a, r, a)
        return r
    if expr is sp.true:
        return z3.BoolVal(True)
    if expr is sp.false:
        return z3.BoolVal(False)
    raise _Unsupported(f"{type(expr).__name__}: {expr}")


def smt(fc: FormalClaim, timeout_ms: int = 10_000) -> SmtReport:
    """Check hypotheses ∧ domain-constraints ∧ ¬conclusion is UNSAT. Polynomial arithmetic over
    Z/Q/R only; anything else is reported `unsupported`, never silently approximated."""
    if not z3_available():
        return SmtReport("unavailable", "z3 not importable in this environment")
    import z3  # noqa: PLC0415

    syms, hyps, concl = parse(fc)
    decided = concl if concl in (sp.true, sp.false) else decided_at_parse(fc)
    if decided in (sp.true, sp.false):
        return SmtReport("trivial", f"conclusion reduced to {decided} by SymPy domain assumptions before reaching the "
                                    "solver; this is CAS evidence, not SMT evidence")
    if "C" in fc.variables.values():
        return SmtReport("unsupported", "complex domain not supported by the SMT translation")
    has_fraction = any(isinstance(a, sp.Rational) and not isinstance(a, sp.Integer)
                       for e in [*hyps, concl] for a in e.atoms(sp.Rational))
    all_int = all(d in ("N", "N+", "Z") for d in fc.variables.values()) and not has_fraction
    env: dict = {"__int_mode__": all_int}
    constraints = []
    for name, dom in fc.variables.items():
        s = syms[name]
        v = z3.Int(name) if all_int else z3.Real(name)
        env[s] = v
        if dom in ("N", "N+", "Z") and not all_int:
            constraints.append(z3.ToInt(v) == v)  # integrality inside a real problem
        if dom in ("N",):
            constraints.append(v >= 0)
        if dom in ("N+", "R+"):
            constraints.append(v > 0)
    try:
        for h in hyps:
            constraints.append(_to_z3(h, env, z3))
        neg = z3.Not(_to_z3(concl, env, z3))
    except _Unsupported as e:
        return SmtReport("unsupported", f"translation: {e}")
    s = z3.Solver()
    s.set("timeout", timeout_ms)
    s.add(*constraints, neg)
    r = s.check()
    if r == z3.unsat:
        return SmtReport("verified", f"hypotheses ∧ ¬conclusion UNSAT (z3 {z3.get_version_string()})")
    if r == z3.sat:
        m = s.model()
        model = {str(d): str(m[d]) for d in m.decls()}
        return SmtReport("counterexample", "z3 found a satisfying assignment for ¬conclusion", model)
    return SmtReport("unknown", f"z3 returned unknown: {s.reason_unknown()}")


@dataclass
class IntervalReport:
    verdict: str  # verified | false | inconclusive
    details: str
    interval: str


def interval_eq(fc: FormalClaim, subs: dict[str, str], dps: int = 60) -> IntervalReport:
    """Certified evaluation of a *ground* equality/inequality at a point using interval arithmetic.
    Certifies the instance, not the theorem; evidence kind is `interval`."""
    syms, _hyps, concl = parse(fc)
    if not isinstance(concl, sp.Rel):
        return IntervalReport("inconclusive", "conclusion is not a relation", "")
    point = {syms[k]: sp.sympify(v) for k, v in subs.items()}
    lhs, rhs = concl.lhs.subs(point), concl.rhs.subs(point)
    if lhs.free_symbols or rhs.free_symbols:
        return IntervalReport("inconclusive", f"unbound symbols after substitution: {lhs.free_symbols | rhs.free_symbols}", "")
    with mpmath.workdps(dps):
        try:
            f = sp.lambdify((), lhs - rhs, modules=[{"pi": mpmath.iv.pi, "E": mpmath.iv.e, "sqrt": mpmath.iv.sqrt,
                                                     "exp": mpmath.iv.exp, "log": mpmath.iv.log,
                                                     "sin": mpmath.iv.sin, "cos": mpmath.iv.cos}, "mpmath"])
            d = mpmath.iv.mpf(f()) if not isinstance(f(), mpmath.iv.mpf) else f()
        except Exception as e:  # noqa: BLE001
            return IntervalReport("inconclusive", f"interval evaluation failed: {e}", "")
        s = str(d)
        if isinstance(concl, sp.Eq):
            return IntervalReport("inconclusive" if 0 in d else "false",
                                  "0 lies in the enclosure of lhs-rhs (consistent, not certified equal)" if 0 in d
                                  else "enclosure of lhs-rhs excludes 0", s)
        lo, hi = d.a, d.b
        if isinstance(concl, (sp.Lt, sp.Le)) and hi < 0:
            return IntervalReport("verified", "enclosure of lhs-rhs is strictly negative", s)
        if isinstance(concl, (sp.Gt, sp.Ge)) and lo > 0:
            return IntervalReport("verified", "enclosure of lhs-rhs is strictly positive", s)
        if isinstance(concl, (sp.Lt, sp.Le)) and lo > 0 or isinstance(concl, (sp.Gt, sp.Ge)) and hi < 0:
            return IntervalReport("false", "enclosure of lhs-rhs has the wrong sign everywhere", s)
        return IntervalReport("inconclusive", "enclosure straddles 0; raise dps", s)
