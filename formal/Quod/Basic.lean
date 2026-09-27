import Mathlib

/-!
Smoke theorems for the formal layer. `quod demo ladder` re-proves both through `formal.check`
from the claims' `lean` blocks; this file exists so `lake build` has a fixed target.
-/

namespace Quod

/-- The normalized harmonic-oscillator spectrum Eₙ = n + 1/2 has unit spacing. -/
theorem harmonicOscillatorSpacing (n : ℕ) :
    ((n + 1 : ℕ) : ℝ) + (1 / 2 : ℝ) - ((n : ℝ) + (1 / 2 : ℝ)) = 1 := by
  norm_num

/-- Algebraic smoke test that Mathlib tactics are operational. -/
theorem squareExpansion (a b : ℝ) : (a + b)^2 = a^2 + 2*a*b + b^2 := by
  ring

end Quod
