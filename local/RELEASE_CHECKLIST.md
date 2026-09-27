# Release checklist

1. On an Apple-silicon Mac: install Pixi (recipe validated with 0.79.0; 0.81.x works), run `pixi lock`, commit `pixi.lock`.
2. `pixi install --locked` from a clean clone; `pixi run doctor` must show `z3`, `sympy`, `mpmath`, `flint`, `sage`, `mlx_metal` true.
3. `pixi run test` and `pixi run demo` green. The demo must show the negative control refuted and the two
   identities at `solver_verified` (or `formally_proved` if Lean is set up).
4. Lean: install `elan`, run `scripts/formal-setup.sh`, commit `formal/lake-manifest.json`. Re-run
   `pixi run demo`; both identities must reach `formally_proved`.
5. `quod ledger export` and `quod library export`; review `local/reports/LEDGER.md` and act on open
   items. Neither export belongs in the repository: `local/` is git-ignored on purpose.
6. Tag only after all of the above pass on ARM64 hardware.
7. Check `git status` and `git ls-files` for research output before tagging: `state/`, `artifacts/`,
   `local/` and run-generated `formal/Quod/*.lean` are ignored, and nothing under them should ever be staged.
