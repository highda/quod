# formal/

Pinned Lean 4 + Mathlib project. `lean-toolchain` pins Lean; `lakefile.toml` pins the Mathlib tag;
`lake-manifest.json` (commit it after the first `lake update`) pins everything transitive.

- `Quod/Basic.lean` — fixed smoke theorems.
- `Quod/Scratch/` — files written by `quod formal check` / `quod attack`; git-ignored.

Never run `lake build` without `lake exe cache get` first on a laptop.
