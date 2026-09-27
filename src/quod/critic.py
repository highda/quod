"""Isolated model adversary. Independence comes from context isolation and a different model:
the critic receives the dossier and the candidate claim, never the proposer's transcript.

Context hygiene (measured 2026-09-19 on a one-word reply through the Claude CLI):
    default CLI context incl. the user's MCP servers   ~171k input tokens
    --strict-mcp-config with no servers                  ~9k
    --system-prompt replacing the CLI's own              ~530
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

from . import config as config_mod, dossier
from .models import AttackReport
from .state import StateStore

# Replaces the Claude Code system prompt entirely.
SYSTEM_PROMPT = """You are the ADVERSARY in a two-agent mathematics research harness. You did not produce the claim
you are given and you must not trust it. Your only job is to break it or to find what it silently assumes.

Rules:
- Attack the *statement*: hunt for counterexamples, degenerate cases, boundary cases, missing hypotheses,
  quantifier slips, domain errors, circular dependencies on other claims, and results already known in the
  literature that contradict or subsume it.
- Prefer concrete, checkable objections: an explicit counterexample, a specific missing hypothesis, a named
  theorem (Mathlib names welcome). Vague doubt is worthless.
- Do not write a proof. Do not propose a fix unless it is a strictly weaker statement that survives your attack.
- If the claim carries a Lean statement, check FAITHFULNESS: does the Lean statement say what the sentence says,
  with the same quantifiers, domain and strength? A narrower or merely related Lean statement means the claim is
  NOT proved as stated — report `weakened` and say exactly what was proved instead.
- If you cannot break it, say what you tried and what the weakest point still is.

Respond with ONLY a JSON object:
{"outcome": "refuted"|"weakened"|"survived"|"inconclusive", "details": "<concise, concrete>",
 "counterexample": <object or null>, "missing_hypotheses": [...], "known_results": [...]}"""

MODES = {
    "logical": "MODE logical adversary: locate the unsupported implication. For each step of the argument behind the claim, ask what "
               "licenses it; name the first step that is asserted rather than derived, and state exactly what would be needed.",
    "assumption": "MODE assumption adversary: expose hidden hypotheses. List every property the claim silently uses (finiteness, "
                  "non-degeneracy, domain, ordering, continuity, integrality) that is not in its hypotheses; give a case where dropping one breaks it.",
    "counterexample": "MODE counterexample hunter: your only goal is a concrete object that violates the claim. Try degenerate values, "
                      "boundary cases, small cases, and structured families; report the smallest one you find, or the family you searched.",
    "representation": "MODE representation critic: challenge the formulation. Is the formal (SymPy/Lean) statement the same as the sentence "
                      "(same quantifiers, domain, strength)? Is there a formulation in which the claim is easier or in which it is obviously false? "
                      "A narrower Lean statement than the sentence is a `weakened` verdict.",
    "compressor": "MODE proof compressor: given the claim and its proof structure, name machinery that is unnecessary, lemmas that are "
                  "special cases of Mathlib results (name them), and the shortest route you see. Report `survived` with the compression plan in details.",
    "fresh": "MODE fresh mathematician: solve the problem independently from the verified facts in the dossier only, without looking at the "
             "proposer's approach. If your route differs, say where; if you reach a different answer, that is a `refuted` verdict.",
}


def choose_mode(claim, attacks) -> str:
    """Rule-table scheduler: pick the attack mode with the highest expected value for the claim's state."""
    from .models import Rigor  # noqa: PLC0415

    prior = {a.kind for a in attacks if a.claim_id == claim.id and a.attacker.startswith("model")}
    if claim.rigor == Rigor.FORMALLY_PROVED:
        # The kernel is the stronger adversary. A representation pass only pays when the Lean statement is
        # *not* the frozen target: restatements and supersessions. Measured 2026-09-21: 0 hits in 47 passes
        # over frozen, kernel-checked targets, so compression is the default there.
        restated = bool(claim.supersedes) or bool({"restated", "narrowed"} & set(claim.tags)) or not (claim.formal and claim.formal.frozen)
        if restated and "llm:representation" not in prior:
            return "representation"
        return "compressor"
    if claim.rigor in (Rigor.SPECULATIVE, Rigor.EMPIRICAL):
        return "counterexample" if "llm:counterexample" not in prior else "assumption"
    if claim.formal and claim.formal.lean and "llm:representation" not in prior:
        return "representation"
    return "logical" if "llm:logical" not in prior else "fresh"


PROMPT = """## Branch dossier (verified facts and current state)
{dossier}

## Candidate claim under attack
id: {claim_id}
rigor: {rigor}
statement: {statement}
assumptions: {assumptions}
formal: {formal}
"""


def build_command(cli: str, model: str, system: str = SYSTEM_PROMPT) -> list[str]:
    """`--tools` is variadic, so the prompt must travel on stdin. `--bare` is NOT used: it drops the auth context."""
    return [
        cli, "-p", "--no-session-persistence", "--model", model, "--output-format", "json",
        "--tools", "", "--max-turns", "1", "--disable-slash-commands",
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--setting-sources", "", "--system-prompt", system,
    ]


def run(store: StateStore, claim_id: str, model: str | None = None, timeout: int | None = None,
        dry_run: bool = False, mode: str | None = None) -> dict:
    c = store.require_claim(claim_id)
    cfg = config_mod.resolve(adversary=model)
    model = cfg.adversary
    timeout = timeout or cfg.critic_timeout
    if mode in (None, "auto"):
        mode = choose_mode(c, store.attacks(branch_id=c.branch_id))
    if mode not in MODES:
        return {"error": f"unknown mode {mode}; one of {sorted(MODES)} or auto"}
    system = SYSTEM_PROMPT + "\n\n" + MODES[mode]
    prompt = PROMPT.format(
        dossier=dossier.render(store, c.branch_id, max_chars=8000), claim_id=c.id, rigor=c.rigor,
        statement=c.statement, assumptions=json.dumps(c.assumptions),
        formal=json.dumps(c.formal.model_dump(exclude_none=True)) if c.formal else "none",
    )
    cli = os.environ.get("ANTHROPIC_CLI", "claude")
    if dry_run:
        return {"model": model, "mode": mode, "model_source": cfg.source["adversary"], "warnings": cfg.warnings(),
                "command": build_command(cli, model, system)[:-1] + ["<SYSTEM_PROMPT>"], "prompt": prompt}
    if not shutil.which(cli):
        return {"error": f"{cli} not on PATH; install/login the Claude CLI"}
    # cwd is a scratch dir so no CLAUDE.md/memory is picked up. QUOD_ROLE=adversary makes the harness hooks
    # stand down, so the critic's Stop cannot end the proposer's epoch.
    env = {**os.environ, "QUOD_ROLE": "adversary"}
    with tempfile.TemporaryDirectory(prefix="quod-critic-") as cwd:
        try:
            proc = subprocess.run(build_command(cli, model, system), input=prompt, capture_output=True, text=True,
                                  timeout=timeout, cwd=cwd, env=env)
        except subprocess.TimeoutExpired:
            return {"error": f"critic timed out after {timeout}s"}
    if proc.returncode != 0:
        return {"error": proc.stderr[-2000:] or f"exit {proc.returncode}"}
    raw = proc.stdout
    try:
        envelope = json.loads(raw)
        text = envelope.get("result", raw) if isinstance(envelope, dict) else raw
    except json.JSONDecodeError:
        envelope, text = {}, raw
    if isinstance(envelope, dict) and envelope.get("is_error"):
        return {"error": str(envelope.get("result"))[:2000]}
    verdict = _extract_json(text)
    usage = envelope.get("usage", {}) if isinstance(envelope, dict) else {}
    outcome = verdict.get("outcome", "inconclusive")
    if outcome not in ("refuted", "weakened", "survived", "inconclusive"):
        outcome = "inconclusive"
    report = store.record_attack(AttackReport(
        claim_id=c.id, branch_id=c.branch_id, attacker=f"model:{model}", kind=f"llm:{mode}", outcome=outcome,
        details=json.dumps({k: verdict.get(k) for k in ("details", "counterexample", "missing_hypotheses", "known_results")}),
    ))
    store.event(c.branch_id, "critic.usage", {"model": model, "usage": usage})
    total_in = sum(usage.get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    return {"model": model, "mode": mode, "outcome": outcome, "verdict": verdict,
            "tokens": {"input_total": total_in, "output": usage.get("output_tokens", 0)},
            "recorded": report.created_at, "warnings": cfg.warnings()}


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return {"outcome": "inconclusive", "details": text[:2000]}
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {"outcome": "inconclusive", "details": text[:2000]}
