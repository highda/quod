# The claim language

A claim is a sentence plus, whenever possible, a `formal` block the engine can read:

```json
{
  "variables": {"n": "N", "k": "N+", "x": "R"},
  "hypotheses": ["Gt(x, 1)"],
  "conclusion": "Eq(Sum(2*k - 1, (k, 1, n)), n**2)",
  "lean": "theorem sumOdd (n : ℕ) : ∑ k ∈ Finset.range n, (2*k+1) = n^2 := by\n  induction n <;> simp_all [Finset.sum_range_succ]; ring"
}
```

## Fields
- `variables` — every symbol that appears anywhere, **bound indices included**. Domains:
  `N` (ℕ, ≥0), `N+` (ℕ, ≥1), `Z`, `Q`, `R`, `R+` (>0), `C`. Domains become SymPy assumptions
  (`integer`, `positive`, `real`, …) and Z3 sorts/constraints.
- `hypotheses` — SymPy boolean expressions. Instances where a hypothesis is false are skipped, not counted.
- `conclusion` — a SymPy relation (`Eq`, `Ne`, `Lt`, `Le`, `Gt`, `Ge`) or boolean combination (`And`, `Or`,
  `Not`, `Implies`). Use function-call syntax, not `==` or `<`.
- `lean` — optional Lean 4 statement *with proof attempt*. With `sorry` it is compiled as a *statement* check
  (`statement_ok`/`statement_error`), never a proof. A formal block may be Lean-only (imported targets).
  Write for the *pinned* Mathlib (`formal/lean-toolchain`), not from memory: e.g. big operators bind with
  `∑ k ∈ s, f k`, not the older `∑ k in s, f k`. The kernel, not recall, decides what compiles.

Available names besides your variables: `Eq Ne Lt Le Gt Ge And Or Not Implies Sum Product Integral
Derivative Limit oo sqrt exp log sin cos tan sinh cosh tanh Abs floor ceiling factorial binomial gcd lcm Mod
pi E I Rational Integer S Max Min sign zeta gamma primepi isprime totient fibonacci re im conjugate
Piecewise`. `isprime(p)` may be used as a hypothesis on a symbolic variable; it is decided per instance. Undeclared names are a parse error.

## What each engine does with it
| step | tool | evidence kind | verdicts |
|---|---|---|---|
| instances | SymPy exact + 50-digit numeric fallback over a small grid of each domain | `instance_check` | survived / **refuted** (counterexample recorded) / inconclusive |
| CAS | `simplify`, `expand`, `trigsimp`, `doit`, `nsimplify` on lhs−rhs; `simplify` on relations | `cas` | verified / false / inconclusive |
| SMT | Z3, polynomial arithmetic over Z/Q/R; checks `hyps ∧ ¬concl` UNSAT | `smt` | verified / counterexample / unknown / unsupported / trivial (SymPy decided it first) |
| interval | `mpmath.iv` at a *ground* point (`quod` API `engines.interval_eq`) | `interval` | verified / false / inconclusive |
| Lean | `lake env lean` on the wrapped statement | `lean` | proved / failed / statement_ok / statement_error / unavailable |

Unsupported shapes are reported as such. Nothing is approximated silently.

## Draft, then add
```sh
quod claim check --formal '{...}'          # dry run, nothing stored
quod claim add C-001 -b BR --statement "…" --formal '{...}'
quod attack C-001                          # engine attacks + evidence + promotion
quod critic C-001                          # isolated adversary (model)
```
