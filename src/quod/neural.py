"""A local model as a hammer: candidate Lean proofs generated on Metal, decided by the kernel.

This is an *extra* — idle compute put to work, not a replacement for anything. A local quantised prover will
not out-reason a frontier model, and on hard goals it mostly will not succeed; what it is good for is partials
and routine steps, where a candidate costs GPU seconds and no tokens and the kernel rejects a wrong one for
free. So the metric is kernel-accepted proofs per wall hour, not pass@1: a model that fails most attempts still
pays for itself if some of its hits would otherwise have cost paid reasoning.

Weights are linked, never shipped: `PRESETS` names a few known-good targets and any Hugging Face repo id or
local directory works as well (`quod neural pull`). The feature stays off until a model is configured.

The constraint is memory, and the guard is derived from the model rather than fixed: required space is the
measured (or declared) weight footprint times a load factor, plus a KV allowance scaled to the requested
generation length, plus headroom. This module loads, generates, unloads, and only then compiles — on a 16 GB
machine a resident model and a Lean build do not coexist, and swapping the compiler out is worse than not
trying, so a running `lake` also blocks the pass.

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

# Models are linked, never shipped: these are download targets with the sizes the guard needs to reason about.
# `format: mlx` runs as-is through mlx-lm / mlx-embeddings; `needs-convert` is a base checkpoint that has to go
# through `mlx_lm.convert -q` first. Sizes are the on-disk footprint of the quantised weights.
PRESETS: dict[str, dict] = {
    "prover-7b": {
        "repo": "mlx-community/DeepSeek-Prover-V2-7B-4bit", "role": "prover", "approx_gb": 4.2, "format": "mlx",
        "note": "Lean 4 prover, 4-bit MLX conversion of deepseek-ai/DeepSeek-Prover-V2-7B. The default choice "
                "for drafting tactic scripts: small enough to load between compiles.",
    },
    "prover-7b-base": {
        "repo": "deepseek-ai/DeepSeek-Prover-V2-7B", "role": "prover", "approx_gb": 14.0, "format": "needs-convert",
        "note": "Full-precision source checkpoint. Convert before use: `mlx_lm.convert --hf-path "
                "deepseek-ai/DeepSeek-Prover-V2-7B -q --q-bits 4`.",
    },
    "reasoner-27b-ternary": {
        "repo": "prism-ml/Ternary-Bonsai-2-27B-mlx-2bit", "role": "reasoner", "approx_gb": 5.9, "format": "mlx",
        "note": "Ternary-weight 27B general reasoner (~1.76 bits/weight). Not Lean-specialised, so weaker at "
                "tactics than the 7B prover per gigabyte, but a candidate for cheap local critique.",
    },
    "encoder-bge-small": {
        "repo": "mlx-community/bge-small-en-v1.5-8bit", "role": "encoder", "approx_gb": 0.04, "format": "mlx",
        "note": "Text embedding model for `quod premise index --encoder <repo>`: turns lexical retrieval into "
                "semantic retrieval. Tiny, and coexists with a Lean build.",
    },
}
KV_CACHE_GB = 0.6          # measured order of magnitude for a 7B at a few thousand tokens of context
LOAD_OVERHEAD = 1.15       # weights are not the whole resident set
OS_HEADROOM_GB = 1.0       # leave the machine usable
MIN_FLOOR_GB = 1.5         # even a tiny encoder should not be loaded into a nearly-full machine
DEFAULT_MIN_FREE_GB = None  # derived from the model's footprint; an explicit number overrides it
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


def resolve_model(name: str) -> tuple[str, dict | None]:
    """A preset name or a bare Hugging Face repo id. Custom repos are first-class: no allowlist."""
    if name in PRESETS:
        return PRESETS[name]["repo"], PRESETS[name]
    return name, next((v for v in PRESETS.values() if v["repo"] == name), None)


def local_path(repo: str) -> "Path | None":
    """Where the weights are, if they have been fetched: an explicit path, else the Hugging Face cache."""
    from pathlib import Path  # noqa: PLC0415

    p = Path(repo).expanduser()
    if p.exists() and p.is_dir():
        return p
    cache = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    d = cache / ("models--" + repo.replace("/", "--"))
    if d.exists():
        snaps = sorted((d / "snapshots").glob("*")) if (d / "snapshots").exists() else []
        return snaps[-1] if snaps else d
    return None


def footprint_gb(repo: str) -> tuple[float, str]:
    """(gigabytes, how it was determined). Measured on disk when the weights are present, else the preset size."""
    p = local_path(repo)
    if p is not None:
        total = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        if total:
            return total / 1e9, "measured on disk"
    _, preset = resolve_model(repo)
    if preset:
        return float(preset["approx_gb"]), "preset estimate"
    return 5.0, "unknown model: assumed 5 GB"


def requirement_gb(repo: str, max_tokens: int = 512) -> dict:
    """What a generation pass actually needs, rather than one fixed floor for every model.

    weights × load overhead + KV cache scaled to the requested generation + headroom for the machine.
    """
    weights, how = footprint_gb(repo)
    kv = KV_CACHE_GB * max(0.25, max_tokens / 2048)
    need = max(MIN_FLOOR_GB, weights * LOAD_OVERHEAD + kv + OS_HEADROOM_GB)
    return {"weights_gb": round(weights, 3), "weights_source": how, "kv_gb": round(kv, 2),
            "required_gb": round(need, 2)}


def pull(name: str, dry_run: bool = False) -> dict:
    """Fetch weights from Hugging Face. Nothing is installed and nothing is vendored: the repository links
    models, and this downloads one into the local cache when asked."""
    repo, preset = resolve_model(name)
    existing = local_path(repo)
    info = {"model": name, "repo": repo, "preset": bool(preset),
            "format": (preset or {}).get("format", "unknown"), "note": (preset or {}).get("note"),
            "already_present": existing is not None,
            "url": f"https://huggingface.co/{repo}" if "/" in repo else None}
    if existing is not None:
        info["path"] = str(existing)
        info["size_gb"] = round(footprint_gb(repo)[0], 2)
        return info
    if dry_run:
        info["would_download"] = (preset or {}).get("approx_gb")
        return info
    if importlib.util.find_spec("huggingface_hub") is None:
        info["error"] = "huggingface_hub is not in this environment"
        info["manual"] = [f"pixi add huggingface-hub    # between runs, never during one",
                          f"hf download {repo} --local-dir models/{repo.split('/')[-1]}",
                          f"then set [neural] prover_model = \"models/{repo.split('/')[-1]}\" in quod.toml"]
        return info
    from huggingface_hub import snapshot_download  # noqa: PLC0415

    t0 = time.perf_counter()
    path = snapshot_download(repo_id=repo)
    info.update({"path": path, "seconds": round(time.perf_counter() - t0, 1),
                 "size_gb": round(footprint_gb(repo)[0], 2),
                 "next": f'set [neural] prover_model = "{repo}" in quod.toml (or export QUOD_PROVER_MODEL)'})
    return info


def availability(min_free_gb: float | None = DEFAULT_MIN_FREE_GB, max_tokens: int = 512) -> HammerReport:
    """Whether a generation pass may run right now, and if not, exactly what is in the way."""
    model = configured_model()
    if not model:
        return HammerReport(False, reason="no [neural] prover_model configured in quod.toml; the local hammer "
                                         "is opt-in because it needs multi-gigabyte weights")
    if importlib.util.find_spec("mlx_lm") is None:
        return HammerReport(False, model, "mlx_lm is not installed in this environment; ledger it rather than "
                                          "installing during a run")
    repo, _ = resolve_model(model)
    if local_path(repo) is None:
        return HammerReport(False, model, f"weights for {repo} are not present locally; fetch them with "
                                          f"`quod neural pull {model}` (they are linked, not shipped)")
    if lake_running():
        return HammerReport(False, model, "a `lake` build is running: loading a model now would swap out the "
                                          "compiler. Run the hammer between compiles.")
    need = requirement_gb(repo, max_tokens)
    floor = need["required_gb"] if min_free_gb is None else min_free_gb
    free = free_memory_gb()
    if free < floor:
        return HammerReport(False, model,
                            f"only {free:.1f} GB free; this pass needs about {floor:.1f} GB "
                            f"({need['weights_gb']} GB of weights [{need['weights_source']}] × {LOAD_OVERHEAD} "
                            f"+ {need['kv_gb']} GB KV + {OS_HEADROOM_GB} GB headroom)")
    return HammerReport(True, model)


def generate(statement: str, n: int = 4, max_tokens: int = 512, temperature: float = 0.7,
             min_free_gb: float | None = DEFAULT_MIN_FREE_GB) -> HammerReport:
    """Load, generate `n` candidates, unload. No compilation happens while the model is resident."""
    rep = availability(min_free_gb, max_tokens=max_tokens)
    if not rep.available:
        return rep
    t0 = time.perf_counter()
    try:  # pragma: no cover - requires weights on disk
        from mlx_lm import generate as mlx_generate, load as mlx_load  # noqa: PLC0415

        model, tokenizer = mlx_load(resolve_model(rep.model)[0])
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
           min_free_gb: float | None = DEFAULT_MIN_FREE_GB) -> dict:
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
