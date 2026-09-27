# Blind rediscovery benchmark

Known results with names stripped. Construction-phase instrument only: run the harness on each,
compare `quod bench score` across toolbox/doctrine variants, freeze the toolbox, then do research.

```sh
quod bench list
quod bench start sum-odd        # opens branch bench-sum-odd
pixi run agent                    # work it with /orient
quod bench score
```
Each `*.toml`: `id`, `domain`, `difficulty`, `statement`, `acceptance` (Lean statement with `sorry`), `notes`.

## Generated statements (recall-proof)
```sh
quod bench generate --seed 7 --count 10 --preview   # look without opening a branch
quod bench generate --seed 7 --count 10             # opens bench-gen-7; key -> state/bench/gen-7.key.json
pixi run agent                                       # /orient bench-gen-7
quod bench score-gen --seed 7
```
Five families — polynomial identities, power-sum closed forms, divisibility `m | n^m − n`, weighted quadratic
inequalities, trig identities — randomly parameterized; about half are made subtly false (perturbations that
still hold at small cases). The agent sees prose only and must formalize. Score: correct, undecided,
**false promotions**, false refutations, and claims carrying hand-typed engine-kind evidence. Agents must never
read the key (doctrine rule 6; `.claude/settings.json` denies `Read(state/**)`).

## Community sources
```sh
quod source search oeis '"Conjecture:" keyword:nice'      # OEIS entries with unproved formula/recurrence lines
quod source search putnam 1985                             # PutnamBench ids
quod source search fc --category ErdosProblems             # formal-conjectures files
quod source show oeis A002375
quod source open putnam putnam_1962_a1                     # branch + target claim with the Lean statement
```
OEIS proofs are contributions (send them to the entry's editors); PutnamBench is the hard benchmark class; formal-conjectures
statements use a newer module system and must be restated against the pinned Mathlib.

## Campaigns
```sh
for id in putnam_1988_b1 putnam_2001_a1; do quod source open putnam $id --branch ${id//_/-}; done
for b in putnam-1988-b1 putnam-2001-a1; do scripts/run-epoch.sh $b; done   # sequential, measured
quod bench score --tag source:putnam
```
