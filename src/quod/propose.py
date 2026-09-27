"""Asymmetric branching: one frontier call proposes many distinct approaches, each with a discriminating
experiment the engines can run; deterministic computation kills the losers; only survivors go back to the model.

An experiment must be machine-runnable: a FormalClaim (instances/CAS/SMT) or a Lean statement (statement check).
Approaches without a falsifiable intermediate are kept as `untested`, never counted as survivors.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile

from . import config as config_mod, dossier, engines, expand, formal
from .models import Claim, FormalClaim, utcnow
from .state import StateStore

SYSTEM = """You are the PROPOSER's planning pass in a mathematics research harness. Given a branch dossier, produce
N mutually DISTINCT approaches to the objective. For each approach give the cheapest experiment that could kill it,
in a form a machine can run without you:
  - "claim": a SymPy formal block {"variables": {...}, "hypotheses": [...], "conclusion": "..."} that must be TRUE
    if the approach is viable (instances/CAS/SMT will test it), or
  - "lean": a Lean 4 statement ending in `sorry` that must COMPILE against Mathlib if the approach is viable
    (e.g. the key lemma the approach needs, stated precisely), or
  - null if the approach has no falsifiable intermediate (it will then be marked untested, which is a cost).
Respond with ONLY a JSON array of objects:
{"name": "kebab-case", "idea": "<two sentences>", "kills_if": "<what result would kill it>",
 "experiment": {"claim": {...}} | {"lean": "..."} | null}
Distinctness matters more than count: do not list variants of one idea."""


def _model_call(system: str, prompt: str, model: str, timeout: int) -> tuple[str | None, str | None]:
    cli = os.environ.get("ANTHROPIC_CLI", "claude")
    cmd = [cli, "-p", "--no-session-persistence", "--model", model, "--output-format", "json", "--tools", "",
           "--max-turns", "1", "--disable-slash-commands", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--setting-sources", "", "--system-prompt", system]
    env = {**os.environ, "QUOD_ROLE": "planner"}
    with tempfile.TemporaryDirectory(prefix="quod-propose-") as cwd:
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env)
        except subprocess.TimeoutExpired:
            return None, f"timeout after {timeout}s"
    if proc.returncode != 0:
        return None, proc.stderr[-1500:]
    try:
        return json.loads(proc.stdout).get("result", ""), None
    except json.JSONDecodeError:
        return proc.stdout, None


def run_experiment(exp: dict | None, use_hammer: bool = False, hammer_timeout: int = 120) -> dict:
    """Deterministic verdict on one approach's experiment: survived | killed | untested | inconclusive."""
    if not exp:
        return {"verdict": "untested", "details": "no falsifiable intermediate"}
    if exp.get("claim"):
        try:
            fc = FormalClaim.model_validate(exp["claim"])
            inst = expand.instances(fc)
            if inst.counterexamples:
                return {"verdict": "killed", "details": f"instance counterexample {inst.counterexamples[0]}", "by": "engine:expand"}
            sym = expand.symbolic(fc)
            if sym.verdict == "false":
                return {"verdict": "killed", "details": sym.details, "by": "engine:sympy"}
            smt = engines.smt(fc)
            if smt.verdict == "counterexample":
                return {"verdict": "killed", "details": f"z3 model {smt.model}", "by": "engine:z3"}
            if sym.verdict == "verified" or smt.verdict == "verified":
                return {"verdict": "survived", "strength": "strong", "details": f"cas {sym.verdict}; smt {smt.verdict}"}
            if inst.outcome == "survived":
                return {"verdict": "survived", "strength": "medium", "details": f"instances {inst.summary()}"}
            return {"verdict": "inconclusive", "details": f"instances {inst.summary()}; cas {sym.verdict}; smt {smt.verdict}"}
        except Exception as e:  # noqa: BLE001
            return {"verdict": "malformed", "details": f"experiment unreadable: {e}"}
    if exp.get("lean"):
        rep = formal.check(exp["lean"], name_hint="propose")
        if rep.verdict == "proved":
            return {"verdict": "survived", "strength": "strong", "details": "key lemma kernel-proved", "path": rep.path}
        if rep.verdict in ("statement_ok", "compiled"):
            # well-formed is necessary, not sufficient. If the hammer is available, let deterministic automation try
            # to close the key lemma: a proof upgrades the survivor to strong at no model cost.
            if use_hammer and formal.hammer_dylib() is not None:
                body = exp["lean"].replace(":= by\n  sorry", ":= by hammer").replace(":= by sorry", ":= by hammer").replace(":=\n  sorry", ":= by hammer")
                if "hammer" in body:
                    h = formal.hammer(body, name_hint="propose_hammer", timeout=hammer_timeout)
                    if h.get("verdict") == "suggested":
                        return {"verdict": "survived", "strength": "strong", "details": f"key lemma closed by hammer: {h['suggestions'][0]}", "path": rep.path}
            return {"verdict": "survived", "strength": "weak", "details": "key lemma statement compiles (not proved)", "path": rep.path}
        if rep.verdict == "statement_error":
            # a Lean elaboration error is a badly *stated* experiment, never a refutation of the approach
            return {"verdict": "malformed", "details": rep.details[:400], "path": rep.path}
        return {"verdict": "inconclusive", "details": f"{rep.verdict}: {rep.details[:300]}"}
    return {"verdict": "untested", "details": "experiment has neither claim nor lean"}


def run(store: StateStore, branch_id: str, n: int = 12, model: str | None = None, timeout: int = 900,
        record: bool = True, dry_run: bool = False, use_hammer: bool = True) -> dict:
    cfg = config_mod.resolve()
    model = model or cfg.proposer
    d = dossier.render(store, branch_id, max_chars=9000)
    prompt = f"N = {n}\n\n{d}"
    if dry_run:
        return {"model": model, "prompt": prompt}
    text, err = _model_call(SYSTEM, prompt, model, timeout)
    if err:
        return {"error": err}
    start, end = text.find("["), text.rfind("]")
    try:
        approaches = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return {"error": "planner did not return a JSON array", "raw": text[:800]}
    results = []
    for i, a in enumerate(approaches[:n], 1):
        name = str(a.get("name", f"approach-{i}"))[:60]
        verdict = run_experiment(a.get("experiment"), use_hammer=use_hammer)
        results.append({"name": name, "idea": a.get("idea", ""), "kills_if": a.get("kills_if", ""), **verdict})
        if record and verdict["verdict"] in ("survived", "killed", "inconclusive", "malformed"):
            cid = f"{branch_id}-approach-{i}"
            if not store.get_claim(cid):
                exp = a.get("experiment") or {}
                fc = None
                try:
                    fc = FormalClaim.model_validate(exp["claim"]) if exp.get("claim") else (FormalClaim(lean=exp["lean"]) if exp.get("lean") else None)
                except Exception:  # noqa: BLE001
                    fc = None
                store.add_claim(Claim(id=cid, branch_id=branch_id,
                                      statement=f"[approach:{name}] {a.get('idea','')} | experiment: {verdict['details'][:200]}",
                                      formal=fc, tags=["approach", verdict["verdict"], verdict.get("strength", "")]))
                if verdict["verdict"] == "killed":
                    store.refute(cid, verdict["details"], verdict.get("by", "engine"))
    tally = {k: sum(1 for r in results if r["verdict"] == k) for k in ("survived", "killed", "inconclusive", "malformed", "untested")}
    tally["survived_strong"] = sum(1 for r in results if r["verdict"] == "survived" and r.get("strength") == "strong")
    store.event(branch_id, "propose.ran", {"model": model, "n": len(results), "tally": tally, "ts": utcnow()})
    survivors = sorted((r for r in results if r["verdict"] == "survived"),
                       key=lambda r: {"strong": 0, "medium": 1, "weak": 2}.get(r.get("strength"), 3))
    return {"model": model, "tally": tally, "survivors": survivors,
            "killed": [{"name": r["name"], "details": r["details"]} for r in results if r["verdict"] == "killed"],
            "inconclusive": [{"name": r["name"], "details": r["details"][:160]} for r in results if r["verdict"] == "inconclusive"],
            "malformed": [{"name": r["name"], "details": r["details"][:160]} for r in results if r["verdict"] == "malformed"],
            "untested": [r["name"] for r in results if r["verdict"] == "untested"]}
