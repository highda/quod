"""Parse FormalClaim into SymPy objects with domain assumptions."""
from __future__ import annotations

import sympy as sp
from sympy.core.parameters import evaluate as sp_evaluate
from sympy.parsing.sympy_parser import (
    auto_number, factorial_notation, lambda_notation, parse_expr, repeated_decimals,
)

# No auto_symbol: an undeclared name is an error, not a fresh symbol.
TRANSFORMATIONS = (lambda_notation, repeated_decimals, auto_number, factorial_notation)

from .models import FormalClaim

DOMAIN_ASSUMPTIONS: dict[str, dict[str, bool]] = {
    "N": {"integer": True, "nonnegative": True},
    "N+": {"integer": True, "positive": True},
    "Z": {"integer": True},
    "Q": {"rational": True},
    "R": {"real": True},
    "R+": {"real": True, "positive": True},
    "C": {"complex": True},
}

# Names the claim language exposes besides the declared variables.
SAFE_NAMESPACE = {
    name: getattr(sp, name)
    for name in (
        "Eq", "Ne", "Lt", "Le", "Gt", "Ge", "And", "Or", "Not", "Implies",
        "Sum", "Product", "Integral", "Derivative", "Limit", "oo",
        "sqrt", "exp", "log", "sin", "cos", "tan", "sinh", "cosh", "tanh", "Abs", "floor", "ceiling",
        "factorial", "binomial", "gcd", "lcm", "Mod", "pi", "E", "I", "Rational", "Integer", "S",
        "Max", "Min", "sign", "zeta", "gamma", "primepi", "totient", "fibonacci",
        "re", "im", "conjugate", "Piecewise",
    )
}


class isprime(sp.Function):  # noqa: N801 - claim-language name
    """Symbolic-safe primality: stays unevaluated on symbols, decides on integers.
    SymPy's own `isprime` raises on a Symbol, which broke instance expansion."""

    @classmethod
    def eval(cls, n):
        if isinstance(n, sp.Integer):
            return sp.true if sp.isprime(int(n)) else sp.false
        if n.is_Number:
            return sp.false
        return None


class ClaimParseError(ValueError):
    pass


SAFE_NAMESPACE["isprime"] = isprime


def symbols_for(fc: FormalClaim) -> dict[str, sp.Symbol]:
    return {name: sp.Symbol(name, **DOMAIN_ASSUMPTIONS[dom]) for name, dom in fc.variables.items()}


def parse(fc: FormalClaim) -> tuple[dict[str, sp.Symbol], list[sp.Basic], sp.Basic]:
    syms = symbols_for(fc)
    ns = {**SAFE_NAMESPACE, **syms}
    try:
        # Global evaluation off while parsing: `Eq(Sum(...), n)` otherwise decides itself during construction,
        # and on a summand SymPy cannot antidifferentiate (e.g. floor) that decision reaches for
        # Euler-Maclaurin and raises instead of parsing. The engines evaluate afterwards.
        with sp_evaluate(False):
            hyps = [parse_expr(h, local_dict=ns, transformations=TRANSFORMATIONS, evaluate=False) for h in fc.hypotheses]
            concl = parse_expr(fc.conclusion, local_dict=ns, transformations=TRANSFORMATIONS, evaluate=False)
    except NameError as e:
        raise ClaimParseError(f"undeclared symbol ({e}); declare every symbol, bound ones included, in `variables`") from e
    except Exception as e:  # noqa: BLE001 - surface parser message verbatim
        raise ClaimParseError(f"cannot parse claim: {e}") from e
    return syms, hyps, concl


def decided_at_parse(fc: FormalClaim) -> sp.Basic | None:
    """True/False if SymPy settles the conclusion from domain assumptions alone, else None.

    Claims are parsed unevaluated so that a summand SymPy cannot antidifferentiate does not blow up parsing
   . The solver must still not take credit for what the assumption engine already decided, so
    this re-parses with evaluation on — cheaply, and without algebraic normalisation, which is the solver's
    own job. A conclusion that cannot be parsed that way is simply not decided here.
    """
    syms = symbols_for(fc)
    ns = {**SAFE_NAMESPACE, **syms}
    try:
        concl = parse_expr(fc.conclusion, local_dict=ns, transformations=TRANSFORMATIONS)
    except Exception:  # noqa: BLE001
        return None
    return concl if concl in (sp.true, sp.false) else None


def free_names(fc: FormalClaim) -> list[str]:
    """Declared variables that occur free in hypotheses or conclusion (bound summation indices excluded)."""
    syms, hyps, concl = parse(fc)
    free = set().union(*(x.free_symbols for x in [*hyps, concl]))
    return [n for n, s in syms.items() if s in free]
