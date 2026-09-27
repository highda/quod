import pytest

from quod import engines, expand
from quod.claims import ClaimParseError, parse
from quod.models import FormalClaim


def test_undeclared_symbol_rejected():
    with pytest.raises(ClaimParseError):
        parse(FormalClaim(variables={"a": "R"}, conclusion="Eq(a + b, b + a)"))


def test_bound_symbols_must_be_declared_but_are_not_sampled():
    with pytest.raises(ClaimParseError):
        parse(FormalClaim(variables={"n": "N"}, conclusion="Eq(Sum(k, (k, 1, n)), n*(n+1)/2)"))
    fc = FormalClaim(variables={"n": "N", "k": "N+"}, conclusion="Eq(Sum(k, (k, 1, n)), n*(n+1)/2)")
    from quod.claims import free_names
    assert free_names(fc) == ["n"]
    rep = expand.instances(fc)
    assert rep.outcome == "survived" and rep.checked == 8


def test_instances_refute_false_claim():
    rep = expand.instances(FormalClaim(variables={"a": "R", "b": "R"}, conclusion="Eq((a+b)**2, a**2+b**2)"))
    assert rep.outcome == "refuted" and rep.counterexamples


def test_instances_respect_hypotheses():
    rep = expand.instances(FormalClaim(variables={"x": "R"}, hypotheses=["Gt(x, 1)"], conclusion="Gt(x**2, x)"))
    assert rep.outcome == "survived" and rep.skipped > 0


def test_symbolic_identity_and_summation():
    assert expand.symbolic(FormalClaim(variables={"a": "R", "b": "R"}, conclusion="Eq((a+b)**2, a**2+2*a*b+b**2)")).verdict == "verified"
    assert expand.symbolic(FormalClaim(variables={"n": "N", "k": "N+"}, conclusion="Eq(Sum(2*k-1, (k, 1, n)), n**2)")).verdict == "verified"
    assert expand.symbolic(FormalClaim(variables={"a": "R"}, conclusion="Eq(a**2, a)")).verdict == "inconclusive"


def test_identify_constant_is_speculative():
    r = expand.identify_constant("3.14159265358979323846264338327950288")
    assert r["rigor"] == "speculative" and "pi" in r["sympy_nsimplify"]


@pytest.mark.skipif(not engines.z3_available(), reason="z3 not installed")
def test_smt_verifies_and_refutes():
    ok = engines.smt(FormalClaim(variables={"a": "R", "b": "R"}, conclusion="Eq((a+b)**2, a**2+2*a*b+b**2)"))
    assert ok.verdict == "verified"
    bad = engines.smt(FormalClaim(variables={"n": "N"}, conclusion="Gt(n**2, n)"))
    assert bad.verdict == "counterexample" and bad.model
    # integer variable + non-integer rational literal must fall back to Real sort with integrality constraint
    mixed = engines.smt(FormalClaim(variables={"n": "N"}, conclusion="Eq((n + Rational(1, 2))**2, n**2 + n + Rational(1, 4))"))
    assert mixed.verdict == "verified"
    trivial = engines.smt(FormalClaim(variables={"n": "N"}, conclusion="Eq((n + 1) + Rational(1, 2) - (n + Rational(1, 2)), 1)"))
    assert trivial.verdict == "trivial"


def test_smt_reports_unsupported_instead_of_guessing():
    transcendental = FormalClaim(variables={"x": "R"}, conclusion="Gt(sin(x), -2)")
    expected = "unsupported" if engines.z3_available() else "unavailable"
    assert engines.smt(transcendental).verdict == expected


def test_smt_does_not_take_credit_for_sympy_assumptions():
    # exp(x) > 0 is decided by SymPy's assumption engine at parse time; the solver never runs.
    rep = engines.smt(FormalClaim(variables={"x": "R"}, conclusion="Gt(exp(x), 0)"))
    assert rep.verdict in ("trivial", "unavailable")
    assert expand.symbolic(FormalClaim(variables={"x": "R"}, conclusion="Gt(exp(x), 0)")).verdict == "verified"


def test_isprime_hypothesis_on_symbolic_variable():
    # Fermat's little theorem instances: p prime -> p | 2^p - 2 ; and a false variant that must be refuted
    ok = expand.instances(FormalClaim(variables={"p": "N+"}, hypotheses=["isprime(p)"], conclusion="Eq(Mod(2**p - 2, p), 0)"))
    assert ok.outcome == "survived" and ok.checked >= 3 and ok.skipped >= 1
    bad = expand.instances(FormalClaim(variables={"p": "N+"}, hypotheses=["isprime(p)"], conclusion="Eq(Mod(2**p - 1, p), 0)"))
    assert bad.outcome == "refuted"


def test_ground_integral_conclusion_is_decided_numerically():
    rep = expand.instances(FormalClaim(variables={"c": "R", "t": "R"}, conclusion="Eq(Integral(sin(pi*(c + t)), (t, -1, 1)), 0)"))
    assert rep.outcome == "survived" and rep.checked >= 5


@pytest.mark.skipif(not engines.z3_available(), reason="z3 not installed")
def test_smt_piecewise_and_max():
    ok = engines.smt(FormalClaim(variables={"x": "R"}, conclusion="Ge(Piecewise((x, Ge(x, 0)), (-x, True)), 0)"))
    assert ok.verdict == "verified"
    ok2 = engines.smt(FormalClaim(variables={"x": "R", "y": "R"}, conclusion="Ge(Max(x, y), x)"))
    assert ok2.verdict == "verified"
