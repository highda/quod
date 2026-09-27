"""A local model as a hammer: candidate Lean proofs generated on Metal, decided by the kernel.

The economics, not the quality, are the point. A candidate costs GPU seconds and no tokens, and the kernel
rejects a wrong one for free, so the metric that matters is kernel-accepted proofs per wall hour rather than
pass@1 — a model that fails most attempts is still worth running before a paid attempt.

The constraint is memory. On a 16 GB machine a resident model and a Lean build do not coexist: this module
loads, generates, unloads, and only then compiles. It refuses to load at all when free memory is below a floor
or a `lake` build is already running, because swapping the compiler out is worse than not trying.

Nothing here decides anything. A candidate is a string until `formal.check` accepts it; the evidence recorded
on success is `lean` (the kernel's), and the model's involvement is recorded as provenance only.
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import time
from dataclasses import dataclass, field

from . import formal
from .models import Evidence
from .paths import harness_root

DEFAULT_MIN_FREE_GB = 5.0
PROMPT = """Prove this Lean 4 theorem against Mathlib. Reply with ONLY the complete theorem, no prose, no
explanation, in a single ```lean code block. Do not use `sorry`, `axiom`, `native_decide` or `unsafe`.

{statement}
"""


@dataclass
class HammerReport:
    available: bool
    model: str = ""
    reason: str = ""                       # why it did not run, when it did not
    candidates: list[str] = field(default_factory=list)
    accepted: str | None = None
    verdicts: list[str] = field(default_factory=list)
    generate_seconds: float = 0.0
    check_seconds: float = 0.0


def free_memory_gb() -> float:
    """Free + inactive pages, which is what a model load can actually claim on macOS."""
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=10).stdout
        page = os.sysconf("SC_PAGE_SIZE")
        free = inactive = 0
        for line in out.splitlines():
            if "Pages free" in line:
                free = int(re.sub(r"\D", "", line))
            elif "Pages inactive" in line:
                inactive = int(re.sub(r"\D", "", line))
        return (free + inactive) * page / 1e9
    except Exception:  # noqa: BLE001 - a missing vm_stat must not block the caller
        return float("inf")


def lake_running() -> bool:
    """One-shot check, not a poll: a Lean build in flight means the memory is already committed."""
    try:
        return subprocess.run(["pgrep", "-x", "lake"], capture_output=True, timeout=10).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def configured_model() -> str:
    from . import config as config_mod  # noqa: PLC0415

    try:
        return config_mod.resolve().prover_model
    except Exception:  # noqa: BLE001
        return ""


def availability(min_free_gb: float = DEFAULT_MIN_FREE_GB) -> HammerReport:
    """Whether a generation pass may run right now, and if not, exactly what is in the way."""
    model = configured_model()
    if not model:
        return HammerReport(False, reason="no [neural] prover_model configured in quod.toml; the local hammer "
                                         "is opt-in because it needs multi-gigabyte weights")
    if importlib.util.find_spec("mlx_lm") is None:
        return HammerReport(False, model, "mlx_lm is not installed in this environment; ledger it rather than "
                                          "installing during a run")
    if lake_running():
        return HammerReport(False, model, "a `lake` build is running: loading a model now would swap out the "
                                          "compiler. Run the hammer between compiles.")
    free = free_memory_gb()
    if free < min_free_gb:
        return HammerReport(False, model, f"only {free:.1f} GB free, floor is {min_free_gb:.1f} GB")
    return HammerReport(True, model)


def generate(statement: str, n: int = 4, max_tokens: int = 512, temperature: float = 0.7,
             min_free_gb: float = DEFAULT_MIN_FREE_GB) -> HammerReport:
    """Load, generate `n` candidates, unload. No compilation happens while the model is resident."""
    rep = availability(min_free_gb)
    if not rep.available:
        return rep
    t0 = time.perf_counter()
    try:  # pragma: no cover - requires weights on disk
        from mlx_lm import generate as mlx_generate, load as mlx_load  # noqa: PLC0415

        model, tokenizer = mlx_load(rep.model)
        prompt = PROMPT.format(statement=statement.strip())
        for i in range(n):
            text = mlx_generate(model, tokenizer, prompt=prompt, max_tokens=max_tokens, verbose=False,
                                temp=temperature if i else 0.0)
            code = extract_lean(text)
            if code:
                rep.candidates.append(code)
    except Exception as e:  # noqa: BLE001 - a broken local model is a non-event for the run
        rep.available = False
        rep.reason = f"generation failed: {e}"
        return rep
    finally:
        try:  # release the weights before anything touches the compiler
            del model, tokenizer
        except Exception:  # noqa: BLE001
            pass
        import gc  # noqa: PLC0415

        gc.collect()
        try:
            import mlx.core as mx  # noqa: PLC0415

            mx.clear_cache()
        except Exception:  # noqa: BLE001
            pass
    rep.generate_seconds = time.perf_counter() - t0
    return rep


def extract_lean(text: str) -> str | None:
    """The first ```lean block, or a bare `theorem …` body if the model skipped the fence."""
    m = re.search(r"```(?:lean4?|)\s*(.+?)```", text, re.S)
    body = m.group(1) if m else text
    if "theorem" not in body and "lemma" not in body:
        return None
    body = body.strip()
    return body if body else None


def hammer(store, claim_id: str, n: int = 4, max_tokens: int = 512, attach: bool = False,
           min_free_gb: float = DEFAULT_MIN_FREE_GB) -> dict:
    """Generate candidates for a claim's Lean statement and let the kernel decide."""
    c = store.require_claim(claim_id)
    if not (c.formal and c.formal.lean):
        return {"claim": claim_id, "skipped": "claim has no Lean statement to prove"}
    rep = generate(c.formal.lean, n=n, max_tokens=max_tokens, min_free_gb=min_free_gb)
    out: dict = {"claim": claim_id, "model": rep.model, "available": rep.available,
                 "candidates": len(rep.candidates), "generate_seconds": round(rep.generate_seconds, 1)}
    if not rep.available:
        out["reason"] = rep.reason
        return out
    t0 = time.perf_counter()
    for cand in rep.candidates:
        if c.formal.frozen and formal.frozen_violations(c.formal.frozen, cand):
            rep.verdicts.append("frozen_violated")
            continue
        r = formal.check(cand, name_hint=f"{c.id}_neural")
        rep.verdicts.append(r.verdict)
        if r.verdict == "proved":
            rep.accepted = cand
            path = harness_root() / "artifacts" / c.branch_id / f"{c.id}.neural.lean"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(cand)
            store.add_evidence(c.id, Evidence(
                kind="lean", summary=f"kernel accepted a locally generated proof: {r.details[:160]}",
                produced_by="engine:lean", artifact=r.path, metadata={"generated_by": f"local:{rep.model}"}))
            store.event(c.branch_id, "neural.hammer_hit", {"claim": c.id, "model": rep.model})
            out["artifact"] = str(path.relative_to(harness_root()))
            break
    rep.check_seconds = time.perf_counter() - t0
    store.tool_event(c.branch_id, "neural-hammer", "lean.neural", rep.generate_seconds + rep.check_seconds,
                     f"{c.id}: {'proved' if rep.accepted else 'no candidate accepted'}", bool(rep.accepted))
    out.update({"verdicts": rep.verdicts, "check_seconds": round(rep.check_seconds, 1),
                "accepted": bool(rep.accepted),
                "note": ("the kernel accepted one of the candidates, so the evidence is `lean`; the model is "
                         "recorded as provenance only" if rep.accepted else
                         "no candidate survived the kernel; nothing was recorded as evidence")})
    if rep.accepted and attach:
        out["attach"] = f"quod formal attach {c.id} {out.get('artifact')}"
    return out
