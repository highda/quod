"""Delegated formalization: hand one claim to a model whose job is only to make the Lean kernel accept it.

Why this exists: deciding *what* to prove and getting Lean to agree are different skills, and a model can be
strong at the first while losing most of an epoch to the compiler on the second. Splitting the roles lets each
model do what it is good at, measured rather than assumed (see docs/EVALUATION.md).

The split is a no-op when `formalizer` and `proposer` are the same model: the same weights then do the work,
in a fresh context, with the same tools and the same kernel deciding the outcome. Nothing about the ladder,
the evidence rules or the frozen statements changes — this module never promotes a claim itself; it produces a
file, and `formal.check`/`quod formal attach` decide whether that file is a proof.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time

from . import config as config_mod, formal
from .paths import formal_dir, harness_root
from .state import StateStore

SYSTEM_PROMPT = """You are the FORMALIZER in a mathematics research harness. You are given one claim and one job:
produce a Lean 4 file that the kernel accepts, proving exactly that claim against the pinned Mathlib.

Rules:
- `sorry` is never a proof. Neither is `axiom`, `native_decide`, `unsafe`, or any `@[implemented_by]`/`extern`.
  The checker rejects them and reports the file as failed.
- Frozen signatures must appear verbatim. If the claim carries them, you may fill proofs and add lemmas, but
  you may not restate, weaken or generalise the frozen declaration.
- Prove the claim as stated. A narrower theorem that happens to compile is a failure, not a partial success.
- Work by compiling: write the file, run the checker, read the FIRST error, fix that. Do not rewrite from
  scratch on each error. Search Mathlib before inventing a lemma (`exact?`, `apply?`, `simp?`, Loogle).
- Prefer short, robust proofs: `simp`, `norm_num`, `decide`, `omega`, `ring`, `linarith`, `aesop`, and explicit
  term-mode where it is shorter. Large `decide` certificates need a raised timeout, not a smaller statement.
- If you cannot close it, say so plainly and report the exact obstruction: the goal you could not discharge and
  the lemma that would discharge it. Do not report success you did not get from the kernel.

Finish your last message with a single line:
  RESULT: <proved|failed> <path to the Lean file, relative to the harness root>
"""

PROMPT = """Harness root: {root}
Lean project: formal/ (pinned Lean 4 + Mathlib; `formal/Quod/` is yours to write in)

Check a file with:
  pixi run --quiet quod formal check --file <path> --raw [--timeout 1800]

## Claim to prove
id: {claim_id}
statement: {statement}
assumptions: {assumptions}

## Formal block
{formal}

## Frozen signatures (must appear verbatim, proofs filled in)
{frozen}

## Notes from the branch
{notes}

Write your file to `formal/Quod/{suggested}.lean` (any name under formal/Quod/ is fine) and iterate until the
checker reports `proved`. Then print the RESULT line.
"""


def build_command(cli: str, model: str, max_turns: int, system: str = SYSTEM_PROMPT) -> list[str]:
    """Tool-enabled, unlike the adversary: this role has to compile files. MCP servers stay off."""
    return [
        cli, "-p", "--no-session-persistence", "--model", model, "--output-format", "json",
        "--max-turns", str(max_turns), "--disable-slash-commands",
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--allowed-tools", "Bash,Read,Write,Edit,Glob,Grep",
        "--permission-mode", "acceptEdits",
        "--system-prompt", system,
    ]


def _suggested_name(claim_id: str) -> str:
    parts = [p.capitalize() for p in re.split(r"[^A-Za-z0-9]+", claim_id) if p]
    return ("".join(parts) or "Claim")[:40]


def parse_result(stdout: str) -> tuple[str | None, str | None]:
    """(verdict, path) from the RESULT line of the session's final message."""
    try:
        text = json.loads(stdout).get("result", "") or ""
    except Exception:  # noqa: BLE001 - a crashed session has no envelope
        text = stdout
    m = None
    for m in re.finditer(r"^RESULT:\s*(proved|failed)\s+(\S+)\s*$", text, re.M | re.I):
        pass
    if not m:
        return None, None
    return m.group(1).lower(), m.group(2)


def run(store: StateStore, claim_id: str, model: str | None = None, max_turns: int = 40,
        timeout: int | None = None, dry_run: bool = False) -> dict:
    c = store.require_claim(claim_id)
    cfg = config_mod.resolve()
    model = model or cfg.formalizer
    timeout = timeout or max(cfg.critic_timeout, 1800)
    cp = store.latest_checkpoint(c.branch_id)
    notes = "\n".join(f"- {x}" for x in ((cp.obstructions if cp else []) + (cp.open_goals if cp else []))[:6]) or "_none_"
    prompt = PROMPT.format(
        root=harness_root(), claim_id=c.id, statement=c.statement, assumptions=json.dumps(c.assumptions),
        formal=json.dumps(c.formal.model_dump(exclude_none=True), indent=2) if c.formal else "none",
        frozen="\n".join(c.formal.frozen) if (c.formal and c.formal.frozen) else "_none_",
        notes=notes, suggested=_suggested_name(c.id),
    )
    cli = os.environ.get("ANTHROPIC_CLI", "claude")
    same = model == cfg.proposer
    if dry_run:
        return {"claim": c.id, "model": model, "same_model_as_proposer": same,
                "command": build_command(cli, model, max_turns)[:-1] + ["<SYSTEM_PROMPT>"], "prompt": prompt}
    if not shutil.which(cli):
        return {"error": f"{cli} not on PATH; install/login the Claude CLI"}
    # QUOD_ROLE keeps the harness hooks out of this session: its Stop must not end the proposer's epoch.
    env = {**os.environ, "QUOD_ROLE": "formalizer"}
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(build_command(cli, model, max_turns), input=prompt, capture_output=True, text=True,
                              timeout=timeout, cwd=harness_root(), env=env)
    except subprocess.TimeoutExpired:
        store.event(c.branch_id, "formalize.timeout", {"claim": c.id, "model": model, "timeout": timeout})
        return {"claim": c.id, "model": model, "verdict": "timeout", "seconds": timeout}
    verdict, path = parse_result(proc.stdout)
    seconds = time.perf_counter() - t0
    res: dict = {"claim": c.id, "model": model, "same_model_as_proposer": same, "reported": verdict,
                 "file": path, "seconds": round(seconds, 1)}
    if proc.returncode != 0 and not verdict:
        res["verdict"] = "session_failed"
        res["details"] = (proc.stderr or proc.stdout)[-600:]
        store.event(c.branch_id, "formalize.failed", {"claim": c.id, "model": model})
        return res
    # The session's own word is not evidence: the kernel is re-run here, on the file it produced.
    candidate = (harness_root() / path) if path else None
    if candidate is None or not candidate.exists():
        res["verdict"] = "no_file"
        store.event(c.branch_id, "formalize.no_file", {"claim": c.id, "model": model, "reported": verdict})
        return res
    rep = formal.check_file(candidate)
    res["verdict"] = rep.verdict
    res["details"] = rep.details[:400]
    res["scratch"] = rep.path
    if rep.verdict == "proved" and c.formal and c.formal.frozen:
        bad = formal.frozen_violations(c.formal.frozen, candidate.read_text())
        if bad:
            res["verdict"] = "frozen_violated"
            res["details"] = "; ".join(bad)[:400]
    store.event(c.branch_id, "formalize.checked", {"claim": c.id, "model": model, "verdict": res["verdict"],
                                                   "file": str(candidate.relative_to(harness_root()))})
    store.tool_event(c.branch_id, "formalize", "lean.formalize", seconds, f"{c.id}: {res['verdict']}",
                     res["verdict"] == "proved")
    res["attach"] = (f"quod formal attach {c.id} {path}" if res["verdict"] == "proved"
                     else "not attachable: the kernel did not accept this file as a proof of the claim")
    return res


def available() -> bool:
    return shutil.which(os.environ.get("ANTHROPIC_CLI", "claude")) is not None and formal.lean_available() and formal_dir().exists()
