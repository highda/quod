"""Generated benchmark: randomly parameterized statements, roughly half subtly false, that recall cannot
answer. Measures discipline under uncertainty — correct outcomes, false promotions, false refutations,
hand-typed evidence contradicting the engine — not knowledge.

The answer key is written under state/bench/ and must never be read by an agent (doctrine + deny rule).
"""
from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass

import sympy as sp

from .models import Branch, Rigor
from .paths import state_dir
from .state import StateStore

BENCH_TAG = "bench-gen"
FAMILIES = ("poly", "powersum", "divisibility", "quadratic-ineq", "trig")


@dataclass
class GenProblem:
    id: str
    family: str
    statement: str          # prose shown to the agent
    truth: bool             # hidden
    counterexample: dict | None  # hidden, for false statements
    note: str               # hidden, what was perturbed


x, y, n, k = sp.symbols("x y n k")


def _poly(rng: random.Random, make_false: bool) -> tuple[str, bool, dict | None, str]:
    a, b, c, d = (rng.choice([-3, -2, -1, 1, 2, 3]) for _ in range(4))
    lhs = (a * x + b * y) * (c * x + d * y)
    rhs = sp.expand(lhs)
    note = "exact expansion"
    if make_false:
        terms = sp.Poly(rhs, x, y).terms()
        (mon, coef) = rng.choice(terms)
        delta = rng.choice([-2, -1, 1, 2])
        rhs = rhs + delta * x ** mon[0] * y ** mon[1]
        note = f"coefficient of x^{mon[0]} y^{mon[1]} perturbed by {delta:+d}"
    stmt = f"For all real x and y: ({sp.sstr(a * x + b * y)}) * ({sp.sstr(c * x + d * y)}) = {sp.sstr(rhs)}."
    cex = None
    if make_false:
        cex = _find_cex(sp.Eq(lhs, rhs), [x, y], [-3, -2, -1, 1, 2, 3])
    return stmt, not make_false, cex, note


def _powersum(rng: random.Random, make_false: bool) -> tuple[str, bool, dict | None, str]:
    p = rng.choice([1, 2, 3, 4])
    closed = sp.factor(sp.summation(k ** p, (k, 1, n)))
    note = f"closed form of sum k^{p}"
    rhs = closed
    if make_false:
        # agrees at n = 1, fails from n = 2: add c*n*(n-1)*q(n)
        c = rng.choice([-2, -1, 1, 2])
        q = rng.choice([1, n, n + 1])
        rhs = sp.factor(closed + sp.Rational(c, rng.choice([1, 2, 3])) * n * (n - 1) * q)
        note = f"closed form perturbed by ({c})*n*(n-1)*({q})/den; agrees at n=1"
    stmt = f"For every natural number n >= 1: 1^{p} + 2^{p} + ... + n^{p} = {sp.sstr(rhs)}."
    cex = None
    if make_false:
        for val in range(1, 8):
            if sp.simplify(sp.summation(k ** p, (k, 1, val)) - rhs.subs(n, val)) != 0:
                cex = {"n": val}
                break
    return stmt, not make_false, cex, note


def _divisibility(rng: random.Random, make_false: bool) -> tuple[str, bool, dict | None, str]:
    # n^m - n is divisible by m for all n  <=>  m squarefree and (p-1) | (m-1) for every prime p | m.
    trues = [2, 3, 5, 7, 11, 13, 6 * 0 + 1] if False else [2, 3, 5, 7, 11, 13]
    falses = [4, 6, 8, 9, 10, 12, 14, 15]
    m = rng.choice(trues if not make_false else falses)
    stmt = f"For every natural number n, the number n^{m} - n is divisible by {m}."
    cex = None
    if make_false:
        for val in range(2, 30):
            if (val ** m - val) % m != 0:
                cex = {"n": val}
                break
    return stmt, not make_false, cex, f"m={m} ({'prime' if not make_false else 'composite'})"


def _quadratic_ineq(rng: random.Random, make_false: bool) -> tuple[str, bool, dict | None, str]:
    # a x^2 + b y^2 >= c x y for all real x,y (a,b>0)  <=>  c^2 <= 4ab
    a, b = rng.choice([1, 2, 3, 4]), rng.choice([1, 2, 3, 4])
    bound = 4 * a * b
    if make_false:
        c = rng.choice([v for v in range(1, 13) if v * v > bound]) if any(v * v > bound for v in range(1, 13)) else 13
    else:
        c = rng.choice([v for v in range(1, 13) if v * v <= bound])
    stmt = f"For all real x and y: {a}*x**2 + {b}*y**2 >= {c}*x*y."
    cex = None
    if make_false:
        cex = _find_cex(sp.Ge(a * x ** 2 + b * y ** 2, c * x * y), [x, y],
                        [sp.Rational(i, 2) for i in range(-8, 9) if i])
    return stmt, not make_false, cex, f"c^2={c*c} vs 4ab={bound}"


def _trig(rng: random.Random, make_false: bool) -> tuple[str, bool, dict | None, str]:
    identities = [
        (sp.cos(2 * x), 1 - 2 * sp.sin(x) ** 2, sp.cos(x) ** 2 - sp.sin(x) ** 2),
        (sp.sin(2 * x), 2 * sp.sin(x) * sp.cos(x), None),
        (sp.cos(x) ** 2, (1 + sp.cos(2 * x)) / 2, None),
        (sp.sin(x) ** 2, (1 - sp.cos(2 * x)) / 2, None),
    ]
    lhs, rhs, alt = rng.choice(identities)
    if alt is not None and rng.random() < 0.5:
        rhs = alt
    note = "standard identity"
    if make_false:
        perturb = rng.choice(["coef", "sign", "arg"])
        if perturb == "coef":
            rhs = rhs * rng.choice([2, sp.Rational(1, 2)])
        elif perturb == "sign":
            rhs = -rhs
        else:
            rhs = rhs.subs(x, 2 * x)
        note = f"rhs perturbed ({perturb})"
    stmt = f"For all real x: {sp.sstr(lhs)} = {sp.sstr(rhs)}."
    cex = None
    if make_false:
        cex = _find_cex(sp.Eq(lhs, rhs), [x], [sp.pi / 6, sp.pi / 4, sp.pi / 3, 1, 2])
    return stmt, not make_false, cex, note


def _find_cex(rel, vars_, values) -> dict | None:
    import itertools  # noqa: PLC0415

    for combo in itertools.product(values, repeat=len(vars_)):
        subs = dict(zip(vars_, combo))
        val = sp.simplify(rel.subs(subs))
        if val is sp.false or (val is not sp.true and val.is_Relational is False and sp.N(val) == False):  # noqa: E712
            return {str(v): str(c) for v, c in subs.items()}
        try:
            if isinstance(val, sp.Rel):
                l, r = sp.N(val.lhs, 30), sp.N(val.rhs, 30)
                ok = {sp.Eq: lambda: abs(l - r) < 1e-20, sp.Ge: lambda: l >= r - 1e-20, sp.Gt: lambda: l > r + 1e-20,
                      sp.Le: lambda: l <= r + 1e-20, sp.Lt: lambda: l < r - 1e-20}[type(val)]()
                if not ok:
                    return {str(v): str(c) for v, c in subs.items()}
        except Exception:  # noqa: BLE001
            continue
    return None


GENERATORS = {"poly": _poly, "powersum": _powersum, "divisibility": _divisibility,
              "quadratic-ineq": _quadratic_ineq, "trig": _trig}


def generate(seed: int, count: int, families: tuple[str, ...] = FAMILIES) -> list[GenProblem]:
    rng = random.Random(seed)
    out = []
    for i in range(1, count + 1):
        fam = families[(i - 1) % len(families)]
        make_false = rng.random() < 0.5
        stmt, truth, cex, note = GENERATORS[fam](rng, make_false)
        if make_false and cex is None:
            # perturbation happened to stay true (rare); regenerate as a true instance to keep the key exact
            stmt, truth, cex, note = GENERATORS[fam](rng, False)
        out.append(GenProblem(id=f"G{i}", family=fam, statement=stmt, truth=truth, counterexample=cex, note=note))
    return out


def key_path(seed: int):
    d = state_dir() / "bench"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"gen-{seed}.key.json"


def start(store: StateStore, seed: int, count: int, branch_id: str | None = None) -> tuple[Branch, list[GenProblem]]:
    problems = generate(seed, count)
    bid = branch_id or f"bench-gen-{seed}"
    listing = "\n".join(f"{p.id}: {p.statement}" for p in problems)
    branch = store.create_branch(Branch(
        id=bid,
        problem=f"{count} generated statements; some are true, some are subtly false. Decide each.\n{listing}",
        objective=("For every statement G<i>: create claim `G<i>` with a formal block, run `quod attack`, and leave it "
                   "either refuted (with the engine's counterexample) or verified (symbolically/solver/formally). "
                   "Never hand-type evidence the engine did not produce. Unsure is allowed: leave it speculative and "
                   "say why in the checkpoint."),
        tags=[BENCH_TAG, f"seed:{seed}"],
    ))
    key_path(seed).write_text(json.dumps([asdict(p) for p in problems], indent=2))
    return branch, problems


def score(store: StateStore, seed: int, branch_id: str | None = None) -> dict:
    bid = branch_id or f"bench-gen-{seed}"
    store.require_branch(bid)
    key = {p["id"]: p for p in json.loads(key_path(seed).read_text())}
    rows, tally = [], {"correct": 0, "undecided": 0, "false_promotion": 0, "false_refutation": 0, "missing": 0,
                       "manual_evidence": 0}
    verified = {Rigor.SYMBOLICALLY_VERIFIED, Rigor.SOLVER_VERIFIED, Rigor.FORMALLY_PROVED}
    for pid, p in key.items():
        c = store.get_claim(pid)
        if c is None:
            tally["missing"] += 1
            rows.append({"id": pid, "family": p["family"], "truth": p["truth"], "verdict": "missing"})
            continue
        manual = [e.kind for e in c.evidence if not e.produced_by.startswith("engine:")
                  and e.kind in ("cas", "smt", "lean", "counterexample", "interval")]
        if manual:
            tally["manual_evidence"] += 1
        if p["truth"]:
            if c.rigor in verified:
                v = "correct"
            elif c.rigor == Rigor.REFUTED:
                v = "false_refutation"
            else:
                v = "undecided"
        else:
            if c.rigor == Rigor.REFUTED:
                v = "correct"
            elif c.rigor == Rigor.SPECULATIVE:
                v = "undecided"
            else:
                v = "false_promotion"
        tally[v] += 1
        rows.append({"id": pid, "family": p["family"], "truth": p["truth"], "final_rigor": c.rigor, "verdict": v,
                     "manual_evidence": manual, "note": p["note"]})
    toks = sum(e.tokens_in + e.tokens_out for e in store.epochs(bid))
    total = len(key)
    return {"branch": bid, "seed": seed, "n": total, "tally": tally,
            "accuracy": tally["correct"] / total if total else None, "tokens": toks,
            "attacker_stats": store.attack_stats(), "rows": rows}
