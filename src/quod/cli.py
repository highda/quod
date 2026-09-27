"""`quod` — the agent's interface to durable research state. Output is JSON or Markdown so a model
can read it back without guessing."""
from __future__ import annotations

import dataclasses
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

import typer

from . import __version__, attack as attack_mod, bench as bench_mod, bench_gen, critic as critic_mod, demo as demo_mod
from . import config as config_mod, dossier as dossier_mod, expand, formal as formal_mod, ingest, propose as propose_mod, sources
from . import conjecture as conjecture_mod, formalize as formalize_mod, neural as neural_mod
from . import premise as premise_mod, search as search_mod, sos as sos_mod, strategy as strategy_mod
from . import trace as trace_mod
from .compute import capabilities, choose_backend, workspace_trusted
from .library import Library, LibraryError
from .models import (
    Branch, BranchCheckpoint, Claim, Contribution, Epoch, Evidence, FailedStrategy, FormalClaim, Obstruction,
    Rigor, ToolboxProposal,
)
from .paths import harness_root, state_db
from .state import StateError, StateStore

app = typer.Typer(no_args_is_help=True, add_completion=False, pretty_exceptions_enable=False,
                  help="Quod: claims, engines, rigor ladder.")
branch_app = typer.Typer(no_args_is_help=True, help="Research branches.")
claim_app = typer.Typer(no_args_is_help=True, help="Claims and the rigor ladder.")
epoch_app = typer.Typer(no_args_is_help=True, help="Model epochs (sessions) on a branch.")
checkpoint_app = typer.Typer(no_args_is_help=True, help="Handoff checkpoints.")
library_app = typer.Typer(no_args_is_help=True, help="Cross-problem library of verified results.")
ledger_app = typer.Typer(no_args_is_help=True, help="Toolbox/doctrine change proposals.")
bench_app = typer.Typer(no_args_is_help=True, help="Blind rediscovery benchmark (construction phase).")
formal_app = typer.Typer(no_args_is_help=True, help="Lean bridge.")
demo_app = typer.Typer(no_args_is_help=True, help="Smoke tests.")
source_app = typer.Typer(no_args_is_help=True, help="Pull problems from OEIS, PutnamBench, formal-conjectures.")
strategy_app = typer.Typer(no_args_is_help=True, help="Strategy cards: methodology learned from runs.")
obstruction_app = typer.Typer(no_args_is_help=True, help="Named obstructions: what blocks a target, and what would remove it.")
premise_app = typer.Typer(no_args_is_help=True, help="Premise retrieval over the pinned Mathlib.")
neural_app = typer.Typer(no_args_is_help=True, help="Optional local models: presets, downloads, memory budget.")
contribute_app = typer.Typer(no_args_is_help=True, help="Upstream contributions: gaps that belong in another project (Mathlib, formal-conjectures, OEIS).")
for name, sub in (("branch", branch_app), ("claim", claim_app), ("epoch", epoch_app), ("checkpoint", checkpoint_app),
                  ("library", library_app), ("ledger", ledger_app), ("bench", bench_app), ("formal", formal_app),
                  ("demo", demo_app), ("source", source_app), ("strategy", strategy_app),
                  ("obstruction", obstruction_app), ("contribute", contribute_app),
                  ("premise", premise_app), ("neural", neural_app)):
    app.add_typer(sub, name=name)


def _plain(o):
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json", exclude_none=True)
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return dataclasses.asdict(o)
    if isinstance(o, list):
        return [_plain(x) for x in o]
    if isinstance(o, dict):
        return {k: _plain(v) for k, v in o.items()}
    return o


def out(obj) -> None:
    obj = _plain(obj)
    typer.echo(json.dumps(obj, indent=2, default=str))


def fail(msg: str, code: int = 1) -> None:
    typer.echo(json.dumps({"error": msg}), err=True)
    raise typer.Exit(code)


def store() -> StateStore:
    return StateStore()


def _ablated(feature: str) -> bool:
    """QUOD_ABLATE=engines,adversary,strategy disables harness features for controlled ablation runs."""
    return feature in {x.strip() for x in os.environ.get("QUOD_ABLATE", "").split(",") if x.strip()}


def _difficulty_gate(s_: StateStore, branch: str, what: str, force: bool) -> bool:
    """True when `what` may run. Machinery that costs a planning call or context is paid for only once a
    target has shown it resists; measured on mid-difficulty PutnamBench targets at 26-31% of total cost."""
    if force:
        return True
    d = s_.difficulty(branch)
    if d["hard"]:
        return True
    typer.echo(f"_{what} skipped: branch `{branch}` shows no difficulty signal yet "
               f"(no failed strategy, <2 Lean rejections, first epoch, not tagged hard/open-problem). "
               f"Attempt the target directly; re-run after the first failure, or pass --force._", err=True)
    return False


UPSTREAM_HINT = re.compile(
    r"(mathlib|formal[- ]conjectures|oeis)\b[^.]{0,80}?\b(lacks?|missing|absent|has no|does not (have|provide)|not in)"
    r"|\b(lacks?|missing|absent|no)\b[^.]{0,80}?\b(mathlib|formal[- ]conjectures)", re.I)


def _looks_upstream(text: str) -> bool:
    """A gap in another project is a contribution to that project, not a toolbox proposal. Advisory only:
    the author decides, but the CLI says so once."""
    return bool(UPSTREAM_HINT.search(text))


def _refuse_if_ablated(feature: str, what: str) -> None:
    if _ablated(feature):
        fail(f"{what} is disabled in this run (ablation: {feature}). Proceed without it.", 3)


def _record_tool(tool: str, kind: str, seconds: float, summary: str, ok: bool, branch: Optional[str] = None) -> None:
    """Instrumentation: log a harness tool call against the active branch (QUOD_BRANCH, set by the runner)."""
    b = branch or os.environ.get("QUOD_BRANCH")
    if not b:
        return
    try:
        with StateStore() as s:
            if s.get_branch(b):
                s.tool_event(b, tool, kind, seconds, summary, ok)
    except Exception:  # noqa: BLE001 - instrumentation must never break the command
        pass


def _load_formal(formal_json: Optional[str], formal_file: Optional[Path]) -> FormalClaim | None:
    if formal_file:
        return FormalClaim.model_validate_json(formal_file.read_text())
    if formal_json:
        return FormalClaim.model_validate_json(formal_json)
    return None


# -- top level -------------------------------------------------------------------------------
@app.command()
def version():
    out({"version": __version__, "root": str(harness_root()), "state": str(state_db())})


@app.command()
def init():
    """Create state/ and artifacts/ and the SQLite schema."""
    with store():
        pass
    Library().close()
    out({"initialized": str(harness_root()), "state": str(state_db())})


@app.command()
def doctor():
    """Report which engines are importable/installed. Green means the toolbox is usable, not that it is complete."""
    data = capabilities()
    data["python"] = sys.version.split()[0]
    data["root"] = str(harness_root())
    data["state_exists"] = state_db().exists()
    trusted = workspace_trusted(harness_root())
    data["claude_workspace_trusted"] = trusted
    try:
        data["premise_index"] = premise_mod.status()
    except Exception as e:  # noqa: BLE001 - doctor never fails on a subsystem
        data["premise_index"] = {"built": False, "error": str(e)}
    avail = neural_mod.availability()
    data["local_prover"] = {"available": avail.available, "model": avail.model or None, "reason": avail.reason or None,
                            "free_gb": round(neural_mod.free_memory_gb(), 1),
                            "presets": sorted(neural_mod.PRESETS)}
    hints = []
    if trusted is not True:
        hints.append("Claude Code has not trusted this directory yet: run `pixi run agent` once and accept the trust "
                     "dialog, otherwise .claude/settings.json (allowlist, hooks, skills) is ignored.")
    if not data["lean"]:
        hints.append("Lean not installed: the top rung (formally_proved) is unreachable. Run scripts/formal-setup.sh.")
    if not data["mlx_metal"]:
        hints.append("MLX cannot see Metal; GPU workloads will fall back to CPU.")
    if not data["premise_index"].get("built"):
        hints.append("No premise index for the pinned Mathlib: `quod premise index` (once per revision) makes "
                     "premise search available.")
    data["hints"] = hints
    out(data)


@app.command()
def config(key: Optional[str] = typer.Argument(None, help="e.g. models.proposer; omit for everything")):
    """Resolved model/role configuration and where each value came from."""
    try:
        cfg = config_mod.resolve()
    except RuntimeError as e:
        fail(str(e))
    data = {"models": {"proposer": cfg.proposer, "adversary": cfg.adversary, "overseer": cfg.overseer,
                       "formalizer": cfg.formalizer},
            "critic": {"timeout": cfg.critic_timeout}, "formal": {"timeout": cfg.lean_timeout},
            "neural": {"prover_model": cfg.prover_model or None},
            "source": cfg.source, "warnings": cfg.warnings()}
    if key:
        node = data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                fail(f"unknown config key {key}")
            node = node[part]
        typer.echo(node if isinstance(node, str) else json.dumps(node))
        return
    out(data)


@app.command()
def backend(kind: str, size: int = 1, exact: bool = False):
    """Which backend the policy would pick for a workload."""
    out(choose_backend(kind, size, exact).__dict__)


@app.command()
def dossier(branch: str = typer.Option(..., "--branch", "-b"), max_chars: int = 12000):
    """Compact branch dossier (Markdown) — what ORIENT reads instead of the transcript."""
    with store() as s:
        try:
            typer.echo(dossier_mod.render(s, branch, max_chars))
        except StateError as e:
            fail(str(e))


@app.command()
def identify(value: str, dps: int = 30):
    """Guess a closed form for a numeric constant (speculative hint only)."""
    out(expand.identify_constant(value, dps))


@app.command()
def fail_strategy(branch: str = typer.Option(..., "--branch", "-b"), strategy: str = typer.Option(...),
                  reason: str = typer.Option(..., help=">= 20 chars; exact failure reason"),
                  obstruction: Optional[str] = None, artifact: Optional[str] = None):
    """Record a failed strategy with its exact reason (rule 4)."""
    with store() as s:
        try:
            out(s.record_failure(branch, FailedStrategy(strategy=strategy, reason=reason, obstruction=obstruction, artifact=artifact)))
        except (StateError, ValueError) as e:
            fail(str(e))


@app.command()
def difficulty(branch: str = typer.Option(..., "--branch", "-b")):
    """Signals that this target is resisting. Token-expensive machinery is gated on these."""
    with store() as s:
        try:
            out(s.difficulty(branch))
        except StateError as e:
            fail(str(e))


# -- obstructions ----------------------------------------------------------------------------
@obstruction_app.command("add")
def obstruction_add(id: str, branch: str = typer.Option(..., "--branch", "-b"),
                    kind: str = typer.Option(..., help="missing_mathlib|missing_theory|complexity|dependency|formalization|statement"),
                    statement: str = typer.Option(..., help="what exactly is blocked (>= 25 chars)"),
                    unblock: str = typer.Option(..., help="the concrete thing that would remove it (>= 25 chars)"),
                    tried: list[str] = typer.Option([], help="what you already tried against it"),
                    claim: Optional[str] = None):
    """Record a named obstruction. This is the *only* permitted way to report that a target is not closing:
    a blocked step plus what would unblock it. Difficulty on its own is not a result."""
    with store() as s:
        try:
            ob = Obstruction(id=id, branch_id=branch, kind=kind, statement=statement, unblock=unblock,
                             tried=list(tried), claim_id=claim)
            res = s.add_obstruction(ob).model_dump(mode="json")
            if kind == "missing_mathlib":
                res["note"] = ("the upstream side of this is a contribution, not a toolbox change: "
                               "`quod contribute add mathlib --title ... --statement ... --why ...`, with "
                               "`--have` naming whatever you already proved for it.")
            out(res)
        except (StateError, ValueError) as e:
            fail(str(e))


@obstruction_app.command("list")
def obstruction_list(branch: Optional[str] = typer.Option(None, "--branch", "-b"), status: Optional[str] = None):
    with store() as s:
        out(s.obstructions(branch, status))


@obstruction_app.command("clear")
def obstruction_clear(id: str, how: str = typer.Option(..., help="how it was removed (>= 15 chars)")):
    """Mark an obstruction removed. A cleared obstruction counts as progress in `quod trace`."""
    with store() as s:
        try:
            out(s.clear_obstruction(id, how))
        except StateError as e:
            fail(str(e))


@app.command()
def attack(claim: str, no_promote: bool = False, no_lean: bool = False, max_instances: int = 400,
           engine_timeout: Optional[int] = typer.Option(None, help="seconds per engine step (default 180, "
                                                                   "QUOD_ENGINE_TIMEOUT); 0 disables the cap")):
    """Run every deterministic engine against a formal claim; records attacks and evidence, promotes if earned."""
    _refuse_if_ablated("engines", "quod attack")
    with store() as s:
        try:
            t0 = time.perf_counter()
            rep = attack_mod.run(s, claim, promote=not no_promote, lean=not no_lean, max_instances=max_instances,
                                 engine_timeout=engine_timeout)
            c = s.get_claim(claim)
            if c:
                s.tool_event(c.branch_id, "attack", "engine.attack", time.perf_counter() - t0,
                             f"{claim}: {rep.get('final_rigor', rep.get('skipped'))}", "skipped" not in rep)
            out(rep)
        except StateError as e:
            fail(str(e))


@app.command()
def critic(claim: str, model: Optional[str] = None, timeout: Optional[int] = None, dry_run: bool = False,
           mode: str = typer.Option("auto", help="auto | logical | assumption | counterexample | representation | compressor | fresh")):
    """Spawn the isolated model adversary on a claim (dossier + claim only; never the transcript)."""
    _refuse_if_ablated("adversary", "quod critic")
    with store() as s:
        try:
            out(critic_mod.run(s, claim, model=model, timeout=timeout, dry_run=dry_run, mode=mode))
        except StateError as e:
            fail(str(e))


@app.command()
def trace(branch: str = typer.Option(..., "--branch", "-b"), as_json: bool = typer.Option(False, "--json")):
    """Where the minutes and tokens went: phases, closure attribution, tool costs, η."""
    with store() as s:
        try:
            a = trace_mod.analyze(s, branch)
        except StateError as e:
            fail(str(e))
    out(a) if as_json else typer.echo(trace_mod.render(a))


@app.command()
def propose(branch: str = typer.Option(..., "--branch", "-b"), n: int = 12, model: Optional[str] = None,
            timeout: int = 900, no_record: bool = False, dry_run: bool = False,
            no_hammer: bool = typer.Option(False, help="do not try LeanHammer on statement-only survivors"),
            force: bool = typer.Option(False, help="run even when the target shows no difficulty signal yet")):
    """Asymmetric branching: one planning call proposes N distinct approaches with machine-runnable experiments;
    the engines kill the losers; survivors are returned (and recorded as approach claims)."""
    _refuse_if_ablated("engines", "quod propose")
    with store() as s:
        try:
            s.require_branch(branch)
            if not (dry_run or _difficulty_gate(s, branch, "quod propose", force)):
                raise typer.Exit(0)
            t0 = time.perf_counter()
            rep = propose_mod.run(s, branch, n=n, model=model, timeout=timeout, record=not no_record, dry_run=dry_run, use_hammer=not no_hammer)
            if not dry_run and "tally" in rep:
                s.tool_event(branch, "propose", "propose", time.perf_counter() - t0, json.dumps(rep["tally"]), True)
            out(rep)
        except StateError as e:
            fail(str(e))


@app.command()
def formalize(claim: str, model: Optional[str] = None, max_turns: int = 40,
              timeout: Optional[int] = None, dry_run: bool = False,
              attach: bool = typer.Option(False, help="attach and promote when the kernel accepts the file")):
    """Delegate one claim to the formalizer model: it writes Lean until the kernel accepts, or reports the
    obstruction. The kernel is re-run here on the file it produced — the session's own verdict is not evidence.

    With `formalizer` unset in quod.toml this runs the proposer's own model, so the behaviour is unchanged
    except for the fresh context."""
    with store() as s:
        try:
            rep = formalize_mod.run(s, claim, model=model, max_turns=max_turns, timeout=timeout, dry_run=dry_run)
        except StateError as e:
            fail(str(e))
        if attach and rep.get("verdict") == "proved" and rep.get("file"):
            rep["attached"] = True
            out(rep)
            # explicit arguments: calling a typer command in-process would otherwise pass its OptionInfo defaults
            formal_attach(claim, Path(rep["file"]), promote=True, timeout=timeout)
            return
        out(rep)
        if not dry_run and rep.get("verdict") != "proved":
            raise typer.Exit(2)


@app.command()
def search(claim: str, mode: str = typer.Option("counterexample", help="counterexample | witness"),
           box: Optional[str] = typer.Option(None, help='per-variable ranges, e.g. "n=0:10000,k=1:50"'),
           lo: int = -64, hi: int = 64, max_candidates: int = 4_000_000, want: int = 8,
           cpu: bool = typer.Option(False, help="force the numpy fallback instead of Metal")):
    """Wide array search over an integer box for counterexamples or witnesses; survivors are rechecked exactly.

    A refutation is recorded as `counterexample` evidence (the claim is refuted); anything else is recorded as
    `gpu_search`, which is `empirical` and cannot climb — an exhausted finite box is not a proof."""
    _refuse_if_ablated("engines", "quod search")
    ranges = None
    if box:
        ranges = {}
        for part in box.split(","):
            name, _, rng = part.partition("=")
            a, _, b = rng.partition(":")
            ranges[name.strip()] = (int(a), int(b))
    with store() as s:
        try:
            out(search_mod.attack(s, claim, mode=mode, box=ranges, default_range=(lo, hi),
                                  max_candidates=max_candidates, want=want, prefer_gpu=not cpu))
        except StateError as e:
            fail(str(e))


@app.command()
def certify(claim: str, half_degree: Optional[int] = None, restarts: int = 3,
            cpu: bool = typer.Option(False, help="force the numpy fallback instead of Metal")):
    """Find a sum-of-squares certificate for a polynomial inequality: guessed numerically, rounded to rationals,
    verified exactly, and returned with the `nlinarith` hint list that lets the kernel check it in one step."""
    _refuse_if_ablated("engines", "quod certify")
    with store() as s:
        try:
            rep = sos_mod.attack(s, claim, half_degree=half_degree, restarts=restarts, prefer_gpu=not cpu)
        except StateError as e:
            fail(str(e))
        out(rep)
        if rep.get("verdict") not in ("certified", None):
            raise typer.Exit(2)


@app.command()
def conjecture(values: str = typer.Argument(..., help="comma-separated terms, e.g. 1,1,2,5,14,42"),
               max_degree: int = 6, max_order: int = 5,
               claim: Optional[str] = typer.Option(None, help="record the best candidate as this claim id"),
               branch: Optional[str] = typer.Option(None, "--branch", "-b")):
    """Fit closed forms and recurrences to a sequence, exactly over ℚ, validated on terms the fit never saw.

    Candidates are conjectures: with --claim the best one is stored as a `speculative` claim for the engines to
    attack, never as evidence."""
    seq = [v.strip() for v in values.split(",") if v.strip()]
    rep = conjecture_mod.analyze(seq, max_degree=max_degree, max_order=max_order)
    res = conjecture_mod.render(rep)
    best = rep.best()
    if claim and best is not None:
        if not branch:
            fail("--claim needs --branch")
        with store() as s:
            try:
                fc = best.formal()
                s.add_claim(Claim(id=claim, branch_id=branch, statement=f"Conjectured from {len(seq)} terms: "
                                                                       f"{best.description}",
                                  formal=fc, tags=["conjecture"]))
                res["recorded_claim"] = claim
            except (StateError, ValueError) as e:
                res["record_failed"] = str(e)
    out(res)


@app.command("neural-hammer")
def neural_hammer(claim: str, n: int = 4, max_tokens: int = 512,
                  min_free_gb: Optional[float] = typer.Option(None, help="override the footprint-derived floor")):
    """Generate candidate Lean proofs with the configured local model and let the kernel decide.

    Loads, generates, unloads, then compiles — never both at once. Refuses when free memory is below the floor
    or a `lake` build is running."""
    _refuse_if_ablated("engines", "quod neural-hammer")
    with store() as s:
        try:
            rep = neural_mod.hammer(s, claim, n=n, max_tokens=max_tokens, min_free_gb=min_free_gb)
        except StateError as e:
            fail(str(e))
        out(rep)


@neural_app.command("presets")
def neural_presets():
    """Known-good local models. Weights are linked, not shipped; any Hugging Face repo id works too."""
    out({"presets": {name: {**spec, "url": f"https://huggingface.co/{spec['repo']}"}
                     for name, spec in neural_mod.PRESETS.items()},
         "custom": "quod neural pull <owner/repo>  ·  or point [neural] prover_model at a local directory",
         "note": "a prover is an extra: it drafts candidates the kernel then accepts or rejects, and is most "
                 "useful on routine steps and partials"})


@neural_app.command("pull")
def neural_pull(model: str = typer.Argument(..., help="preset name, Hugging Face repo id, or local path"),
                dry_run: bool = False):
    """Download weights into the local Hugging Face cache (or print the exact commands if the hub client is absent)."""
    res = neural_mod.pull(model, dry_run=dry_run)
    out(res)
    if res.get("error"):
        raise typer.Exit(2)


@neural_app.command("budget")
def neural_budget(model: Optional[str] = typer.Argument(None, help="preset, repo id or path; default: configured"),
                  max_tokens: int = 512):
    """What a generation pass would need, and whether the machine has it right now."""
    name = model or neural_mod.configured_model()
    if not name:
        fail("no model given and none configured in [neural] prover_model", 1)
    repo, _ = neural_mod.resolve_model(name)
    need = neural_mod.requirement_gb(repo, max_tokens)
    free = neural_mod.free_memory_gb()
    out({"model": name, "repo": repo, "present_locally": neural_mod.local_path(repo) is not None,
         **need, "free_gb": round(free, 2), "lake_running": neural_mod.lake_running(),
         "would_run": free >= need["required_gb"] and not neural_mod.lake_running()})


@premise_app.command("index")
def premise_index(encoder: str = "hash", limit_files: Optional[int] = None):
    """Build or refresh the premise index for the pinned Mathlib revision. Only changed files are re-read."""
    res = premise_mod.build(encoder_name=encoder, limit_files=limit_files,
                            progress=lambda m: typer.echo(m, err=True))
    out(res)


@premise_app.command("search")
def premise_search(query: str, k: int = 12, encoder: str = "hash",
                   mode: str = typer.Option("auto", help="auto | lexical | vector"),
                   cpu: bool = typer.Option(False, help="force the numpy fallback for vector mode")):
    """Candidate Mathlib premises for a goal. Cheaper than rediscovering a lemma, and far cheaper than proving it."""
    out(premise_mod.search(query, k=k, encoder_name=encoder, mode=mode, prefer_gpu=not cpu))


@premise_app.command("status")
def premise_status(encoder: str = "hash"):
    out(premise_mod.status(encoder_name=encoder))


@app.command()
def attackers():
    """Hit-rate ledger per attacker (engine or model)."""
    with store() as s:
        out(s.attack_stats())


# -- branch ----------------------------------------------------------------------------------
@branch_app.command("new")
def branch_new(id: str, problem: str = typer.Option(...), objective: str = typer.Option(...),
               parent: Optional[str] = None, budget_tokens: Optional[int] = None, tag: list[str] = typer.Option([])):
    with store() as s:
        try:
            out(s.create_branch(Branch(id=id, problem=problem, objective=objective, parent=parent,
                                       budget_tokens=budget_tokens, tags=list(tag))))
        except StateError as e:
            fail(str(e))


@branch_app.command("list")
def branch_list(status: Optional[str] = None):
    with store() as s:
        out(s.branches(status))


@branch_app.command("show")
def branch_show(id: str):
    with store() as s:
        b = s.get_branch(id)
        if not b:
            fail(f"unknown branch {id}")
        out({"branch": b.model_dump(mode="json"), "claims": [c.model_dump(mode="json", exclude_none=True) for c in s.claims(id)],
             "epochs": [e.model_dump(mode="json") for e in s.epochs(id)],
             "latest_checkpoint": (cp.model_dump(mode="json") if (cp := s.latest_checkpoint(id)) else None)})


@branch_app.command("tag")
def branch_tag(id: str, tag: list[str] = typer.Argument(...)):
    """Add tags to a branch (e.g. ablation:no-engines)."""
    with store() as s:
        b = s.get_branch(id)
        if not b:
            fail(f"unknown branch {id}")
        b.tags = sorted(set(b.tags) | set(tag))
        s.db.execute("UPDATE branches SET payload=? WHERE id=?", (b.model_dump_json(), id))
        out(b)


@branch_app.command("status")
def branch_status(id: str, status: str, reason: str = typer.Option(..., help="why the status changed"),
                  force: bool = typer.Option(False, help="close without the recorded failed strategies and obstruction (human override)")):
    with store() as s:
        try:
            out(s.set_branch_status(id, status, reason, force=force))
        except StateError as e:
            fail(str(e))


# -- claim -----------------------------------------------------------------------------------
@claim_app.command("add")
def claim_add(id: str, branch: str = typer.Option(..., "--branch", "-b"), statement: str = typer.Option(...),
              formal: Optional[str] = typer.Option(None, help="FormalClaim JSON"),
              formal_file: Optional[Path] = typer.Option(None, help="path to FormalClaim JSON"),
              assume: list[str] = typer.Option([]), depends_on: list[str] = typer.Option([]),
              supersedes: Optional[str] = None, tag: list[str] = typer.Option([])):
    """Add a claim. Starts speculative; earn rigor through `quod attack` or `quod claim evidence` + `promote`."""
    with store() as s:
        try:
            fc = _load_formal(formal, formal_file)
            out(s.add_claim(Claim(id=id, branch_id=branch, statement=statement, formal=fc, assumptions=list(assume),
                                  depends_on=list(depends_on), supersedes=supersedes, tags=list(tag))))
        except (StateError, ValueError) as e:
            fail(str(e))


@claim_app.command("list")
def claim_list(branch: Optional[str] = typer.Option(None, "--branch", "-b"), rigor: Optional[Rigor] = None):
    with store() as s:
        out([{"id": c.id, "rigor": c.rigor, "statement": c.statement, "formal": c.formal is not None,
              "evidence": sorted(c.evidence_kinds())} for c in s.claims(branch, rigor)])


@claim_app.command("show")
def claim_show(id: str):
    with store() as s:
        c = s.get_claim(id)
        if not c:
            fail(f"unknown claim {id}")
        out({"claim": c.model_dump(mode="json", exclude_none=True),
             "attacks": [a.model_dump(mode="json", exclude_none=True) for a in s.attacks(claim_id=id)]})


@claim_app.command("evidence")
def claim_evidence(id: str, kind: str = typer.Option(..., help="numeric|instance_check|interval|cas|smt|lean|counterexample|..."),
                   summary: str = typer.Option(...), produced_by: str = typer.Option("agent"),
                   artifact: Optional[str] = None):
    with store() as s:
        try:
            out(s.add_evidence(id, Evidence(kind=kind, summary=summary, produced_by=produced_by, artifact=artifact)))
        except StateError as e:
            fail(str(e))


@claim_app.command("promote")
def claim_promote(id: str, to: Rigor):
    """Move a claim up the ladder. Rejected unless evidence of the required kind is attached."""
    with store() as s:
        try:
            out(s.promote(id, to))
        except StateError as e:
            fail(str(e))


@claim_app.command("refute")
def claim_refute(id: str, counterexample: str = typer.Option(...), produced_by: str = typer.Option("agent"),
                 artifact: Optional[str] = None):
    with store() as s:
        try:
            out(s.refute(id, counterexample, produced_by, artifact))
        except StateError as e:
            fail(str(e))


@claim_app.command("check")
def claim_check(formal: Optional[str] = None, formal_file: Optional[Path] = None, max_instances: int = 400):
    """Dry-run the engines on a FormalClaim without storing anything. Use while drafting a claim."""
    _refuse_if_ablated("engines", "quod claim check")
    fc = _load_formal(formal, formal_file)
    if not fc:
        fail("give --formal JSON or --formal-file")
    try:
        rep = expand.instances(fc, max_instances=max_instances)
        sym = expand.symbolic(fc)
        from . import engines  # noqa: PLC0415

        smt = engines.smt(fc)
    except Exception as e:  # noqa: BLE001
        fail(str(e))
    out({"instances": {"outcome": rep.outcome, "summary": rep.summary(), "counterexamples": rep.counterexamples[:5]},
         "cas": sym.__dict__, "smt": {"verdict": smt.verdict, "details": smt.details, "model": smt.model}})


# -- epoch -----------------------------------------------------------------------------------
@epoch_app.command("start")
def epoch_start(branch: str = typer.Option(..., "--branch", "-b"), model: str = "unknown", role: str = "proposer"):
    with store() as s:
        try:
            if (cur := s.current_epoch(branch)):
                s.end_epoch(branch, "superseded by new epoch start")
                typer.echo(json.dumps({"ended_previous": cur.id}), err=True)
            out(s.start_epoch(Epoch(branch_id=branch, model=model, role=role)))
        except StateError as e:
            fail(str(e))


@epoch_app.command("end")
def epoch_end(branch: str = typer.Option(..., "--branch", "-b"), reason: str = typer.Option(...),
              tokens_in: int = 0, tokens_out: int = 0,
              usage_file: Optional[Path] = typer.Option(None, help="Claude CLI JSON result envelope (or stream-json log); "
                                                                   "overrides the token flags with measured numbers"),
              force: bool = typer.Option(False, help="end an epoch that recorded nothing (logged as a defect)")):
    with store() as s:
        try:
            act = s.epoch_activity(branch)
        except StateError as e:
            fail(str(e))
        if act["epoch"] is not None and act["total"] == 0:
            if not force:
                fail(f"refusing to end epoch {act['epoch']} on {branch}: it recorded nothing — no claim, no evidence, "
                     "no attack, no failed strategy, no obstruction, no ledger entry. An epoch with nothing in state is "
                     "a defect, not a result. Record what you tried (`quod fail-strategy`), what blocks the target "
                     "(`quod obstruction add`) or what the toolbox lacks (`quod ledger add`) — then end it. "
                     "Use --force only if the run was broken before any work was possible.", 3)
            s.event(branch, "epoch.empty", {"epoch": act["epoch"], "reason": reason})
    if usage_file:
        u = _usage_from_envelope(usage_file)
        tokens_in, tokens_out = u["input_total"], u["output"]
    with store() as s:
        ep = s.end_epoch(branch, reason, tokens_in, tokens_out)
        out(ep or {"warning": "no open epoch"})


def _usage_from_envelope(path: Path) -> dict:
    """Sum measured usage from a `claude -p --output-format json` result, or over *every* result line of a
    stream-json log: one log can carry several envelopes, and taking the last understates the epoch."""
    text = path.read_text().strip()
    env = None
    try:
        env = json.loads(text)
    except json.JSONDecodeError:
        total: dict = {"type": "result", "usage": {}}
        found = False
        for line in text.splitlines():
            try:
                cand = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(cand, dict) and cand.get("type") == "result":
                found = True
                for k, v in (cand.get("usage") or {}).items():
                    if isinstance(v, (int, float)):
                        total["usage"][k] = total["usage"].get(k, 0) + v
        env = total if found else None
    if not isinstance(env, dict) or "usage" not in env:
        fail(f"{path} does not contain a Claude CLI result envelope with `usage`")
    u = env["usage"]
    return {"input_total": sum(u.get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")),
            "output": u.get("output_tokens", 0), "cost_usd": env.get("total_cost_usd")}


@epoch_app.command("usage")
def epoch_usage(branch: str = typer.Option(..., "--branch", "-b"), usage_file: Path = typer.Option(...),
                epoch_id: Optional[int] = None):
    """Runner entry point: overwrite an epoch's token counts (default: latest) with the Claude CLI's measured usage."""
    u = _usage_from_envelope(usage_file)
    with store() as s:
        try:
            out({"epoch": s.set_epoch_usage(branch, u["input_total"], u["output"], epoch_id), "cost_usd": u["cost_usd"]})
        except StateError as e:
            fail(str(e))


@epoch_app.command("ingest")
def epoch_ingest(branch: str = typer.Option(..., "--branch", "-b"), usage_file: Path = typer.Option(...),
                 epoch_id: Optional[int] = None):
    """Runner entry point: measured usage + per-turn context + per-tool-call timings from a stream-json log."""
    parsed = ingest.parse_stream(usage_file)
    with store() as s:
        try:
            ep = s.set_epoch_usage(branch, parsed["result"]["input_total"], parsed["result"]["output"], epoch_id)
            s.replace_turns(ep.id, parsed["turns"])
            s.add_stream_tool_calls(ep.id, branch, parsed["tool_calls"])
            out({"epoch": ep.id, "turns": len(parsed["turns"]), "tool_calls": len(parsed["tool_calls"]),
                 "tokens_in": ep.tokens_in, "tokens_out": ep.tokens_out, "cost_usd": parsed["result"]["cost_usd"],
                 "measured_from": parsed["result"]["measured_from"]})
        except StateError as e:
            fail(str(e))


@epoch_app.command("mark")
def epoch_mark(reason: str = typer.Option(...), branch: Optional[str] = typer.Option(None, "--branch", "-b")):
    """Hook entry point: auto-checkpoint every open branch (or one) from state. Never fails the caller."""
    try:
        with store() as s:
            targets = [branch] if branch else [b.id for b in s.branches("open") if s.current_epoch(b.id)]
            done = []
            for bid in targets:
                s.auto_checkpoint(bid, reason)
                if reason in ("stop", "session_end"):
                    s.end_epoch(bid, reason)
                done.append(bid)
            out({"auto_checkpointed": done, "reason": reason})
    except Exception as e:  # noqa: BLE001
        out({"warning": str(e), "reason": reason})


# -- checkpoint ------------------------------------------------------------------------------
@checkpoint_app.command("create")
def checkpoint_create(branch: str = typer.Option(..., "--branch", "-b"), objective: Optional[str] = None,
                      goal: list[str] = typer.Option([], help="open goal (repeatable)"),
                      assume: list[str] = typer.Option([]), obstruction: list[str] = typer.Option([]),
                      next: list[str] = typer.Option([], help="next action (repeatable, give three)"),
                      artifact: list[str] = typer.Option([]), notes: str = "",
                      file: Optional[Path] = typer.Option(None, help="BranchCheckpoint JSON instead of flags")):
    with store() as s:
        try:
            if file:
                cp = BranchCheckpoint.model_validate_json(file.read_text())
                cp.branch_id = branch
            else:
                b = s.require_branch(branch)
                cp = BranchCheckpoint(branch_id=branch, objective=objective or b.objective, open_goals=list(goal),
                                      active_assumptions=list(assume), obstructions=list(obstruction),
                                      next_actions=list(next), artifact_ids=list(artifact), notes=notes)
            cp.established_claims = [c.id for c in s.claims(branch) if c.rigor not in (Rigor.SPECULATIVE, Rigor.REFUTED)]
            cp.failed_strategies = s.failed_strategies(branch)
            out(s.checkpoint(cp))
        except (StateError, ValueError) as e:
            fail(str(e))


@checkpoint_app.command("latest")
def checkpoint_latest(branch: str = typer.Option(..., "--branch", "-b"), manual_only: bool = False):
    with store() as s:
        cp = s.latest_checkpoint(branch, include_auto=not manual_only)
        out(cp or {"warning": "no checkpoint"})


# -- library ---------------------------------------------------------------------------------
@library_app.command("add")
def library_add(claim: str, id: Optional[str] = None, lean_path: Optional[str] = None):
    """Promote a verified claim into the cross-problem library."""
    with store() as s:
        lib = Library()
        try:
            c = s.require_claim(claim)
            b = s.get_branch(c.branch_id)
            out(lib.from_claim(c, entry_id=id, lean_path=lean_path, problem=b.problem if b else None))
        except (StateError, LibraryError) as e:
            fail(str(e))
        finally:
            lib.close()


@library_app.command("search")
def library_search(query: str, limit: int = 20):
    lib = Library()
    try:
        out(lib.search(query, limit))
    finally:
        lib.close()


@library_app.command("export")
def library_export(path: Path = Path("local/reports/LIBRARY.md")):
    lib = Library()
    try:
        p = harness_root() / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(lib.export_markdown())
        out({"written": str(p), "entries": len(lib.all())})
    finally:
        lib.close()


# -- ledger ----------------------------------------------------------------------------------
@ledger_app.command("add")
def ledger_add(kind: str, title: str = typer.Option(...), rationale: str = typer.Option(...),
               evidence: str = "", branch: Optional[str] = typer.Option(None, "--branch", "-b"),
               force: bool = typer.Option(False, help="file even when an open entry already says this")):
    """Propose a toolbox/prompt/doctrine change. Acted on by humans between research runs, never mid-run.

    A near-duplicate of an open entry is refused (exit 3) with that entry's id: add your evidence to it with
    `quod ledger append <id> --evidence ...` instead of filing the same defect again."""
    with store() as s:
        try:
            if not force and (dups := s.similar_proposals(title)):
                fail("an open ledger entry already reports this: "
                     + "; ".join(f"#{d.id} {d.title!r}" for d in dups)
                     + f". Add what you saw with `quod ledger append {dups[0].id} --evidence '...'`, or pass --force "
                       "if it is genuinely a different defect.", 3)
            prop = s.propose(ToolboxProposal(kind=kind, title=title, rationale=rationale, evidence=evidence, branch_id=branch))  # type: ignore[arg-type]
            res = prop.model_dump(mode="json")
            if _looks_upstream(f"{title} {rationale}"):
                res["note"] = ("this reads like a gap in another project, not in this harness. A lemma Mathlib "
                               "lacks, or a `sorry` a conjecture repository still carries, is a pull request "
                               "there: record it with `quod contribute add mathlib --title ... --statement ... "
                               "--why ...` and keep the ledger for changes to the toolbox itself.")
            out(res)
        except ValueError as e:
            fail(str(e))


@contribute_app.command("add")
def contribute_add(target: str = typer.Argument(..., help="mathlib | formal-conjectures | oeis | other"),
                   title: str = typer.Option(...),
                   statement: str = typer.Option(..., help="what should exist upstream (>= 25 chars)"),
                   why: str = typer.Option(..., help="why it is missing / what its absence blocked (>= 25 chars)"),
                   have: str = typer.Option("", help="what this run already produced: claim ids, artifact paths"),
                   branch: Optional[str] = typer.Option(None, "--branch", "-b"), claim: Optional[str] = None):
    """Record a gap that belongs in another project. This is *not* the toolbox ledger: a lemma Mathlib lacks is
    a pull request to Mathlib, not a change to this harness."""
    with store() as s:
        try:
            out(s.add_contribution(Contribution(target=target, title=title, statement=statement, why=why,
                                                have=have, branch_id=branch, claim_id=claim)))
        except (StateError, ValueError) as e:
            fail(str(e))


@contribute_app.command("list")
def contribute_list(target: Optional[str] = None, status: Optional[str] = None):
    with store() as s:
        out(s.contributions(target, status))


@contribute_app.command("set")
def contribute_set(id: int, status: str = typer.Argument(..., help="candidate|drafted|submitted|merged|declined|superseded"),
                   url: Optional[str] = typer.Option(None, help="pull request or issue url; required for submitted/merged")):
    with store() as s:
        try:
            out(s.set_contribution_status(id, status, url))
        except StateError as e:
            fail(str(e))


@contribute_app.command("export")
def contribute_export(path: Path = Path("local/reports/CONTRIBUTIONS.md")):
    """Write the upstream catalog: what each project is missing, what we already have for it, and its PR state."""
    with store() as s:
        rows = s.contributions()
        lines = ["# Upstream contributions", "",
                 "Gaps that belong to other projects, with what a run here already produced for them. Separate "
                 "from the toolbox ledger by design: nothing on this page is a change to the harness.", ""]
        for target in sorted({c.target for c in rows}):
            lines += [f"## {target}", ""]
            for c in [x for x in rows if x.target == target]:
                lines += [f"### #{c.id} [{c.status}] {c.title}", "", f"**Should exist:** {c.statement}", "",
                          f"**Why it is missing / what it blocked:** {c.why}", ""]
                if c.have:
                    lines += [f"**Already have:** {c.have}", ""]
                if c.url:
                    lines += [f"**Pull request:** {c.url}", ""]
                if c.branch_id or c.claim_id:
                    lines += [f"_found on branch `{c.branch_id or '-'}`"
                              + (f", claim `{c.claim_id}`" if c.claim_id else "") + f", {c.created_at}_", ""]
        p2 = harness_root() / path
        p2.parent.mkdir(parents=True, exist_ok=True)
        p2.write_text("\n".join(lines) + "\n")
        out({"written": str(p2), "contributions": len(rows)})


@ledger_app.command("append")
def ledger_append(id: int, evidence: str = typer.Option(..., help="another observation of the same defect")):
    """Add evidence to an existing ledger entry (what a second run should do instead of filing a copy)."""
    with store() as s:
        try:
            out(s.append_proposal_evidence(id, evidence))
        except StateError as e:
            fail(str(e))


@ledger_app.command("list")
def ledger_list(status: Optional[str] = None):
    with store() as s:
        out(s.proposals(status))


@ledger_app.command("set")
def ledger_set(id: int, status: str):
    with store() as s:
        try:
            out(s.set_proposal_status(id, status))
        except StateError as e:
            fail(str(e))


@ledger_app.command("export")
def ledger_export(path: Path = Path("local/reports/LEDGER.md")):
    with store() as s:
        rows = s.proposals()
        lines = ["# Toolbox ledger", "", "Proposals raised by the running agent. Reviewed by humans between runs.", ""]
        for p in rows:
            lines += [f"## #{p.id} [{p.status}] ({p.kind}) {p.title}", "", p.rationale, ""]
            if p.evidence:
                lines += [f"Evidence: {p.evidence}", ""]
            if p.branch_id:
                lines += [f"Branch: `{p.branch_id}` · {p.created_at}", ""]
        target = harness_root() / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines))
        out({"written": str(target), "entries": len(rows)})


# -- bench -----------------------------------------------------------------------------------
@bench_app.command("list")
def bench_list():
    out([{"id": p.id, "domain": p.domain, "difficulty": p.difficulty, "statement": p.statement} for p in bench_mod.load()])


@bench_app.command("start")
def bench_start(problem: str, branch: Optional[str] = None):
    probs = {p.id: p for p in bench_mod.load()}
    if problem not in probs:
        fail(f"unknown bench problem {problem}; known: {sorted(probs)}")
    with store() as s:
        try:
            out(bench_mod.start(s, probs[problem], branch))
        except StateError as e:
            fail(str(e))


@bench_app.command("score")
def bench_score(tag: str = typer.Option("bench", help="branch tag to score, e.g. bench, source:putnam, source:fc")):
    with store() as s:
        out(bench_mod.score(s, tag))


@bench_app.command("generate")
def bench_generate(seed: int = 1, count: int = 10, branch: Optional[str] = None, preview: bool = False):
    """Open a branch of randomly generated true/false statements (recall cannot help). Key goes to state/bench/."""
    if preview:
        out([{"id": p.id, "family": p.family, "statement": p.statement} for p in bench_gen.generate(seed, count)])
        return
    with store() as s:
        try:
            b, probs = bench_gen.start(s, seed, count, branch)
        except StateError as e:
            fail(str(e))
        out({"branch": b.id, "count": len(probs), "key": str(bench_gen.key_path(seed)),
             "statements": [{"id": p.id, "statement": p.statement} for p in probs]})


@bench_app.command("score-gen")
def bench_score_gen(seed: int = 1, branch: Optional[str] = None):
    """Score a generated branch against its hidden key: correct / undecided / false promotion / false refutation."""
    with store() as s:
        try:
            out(bench_gen.score(s, seed, branch))
        except (StateError, FileNotFoundError) as e:
            fail(str(e))


# -- formal ----------------------------------------------------------------------------------
@formal_app.command("check")
def formal_check(file: Optional[Path] = None, statement: Optional[str] = None, name: str = "claim",
                 timeout: Optional[int] = typer.Option(None, help="seconds; default from [formal] timeout in quod.toml (900)"),
                 raw: bool = typer.Option(False, help="print verdict and Lean diagnostics as plain text, errors first")):
    """Compile a Lean statement with the pinned toolchain. `sorry` never counts."""
    t0 = time.perf_counter()
    if file:
        rep = formal_mod.check_file(file, timeout)
    elif statement:
        rep = formal_mod.check(statement, name, timeout)
    else:
        fail("give --file or --statement")
    try:
        src_text = file.read_text() if file else (statement or "")
        decl_names = ",".join(formal_mod.qualified_theorem_names(src_text)[:12])
    except Exception:  # noqa: BLE001
        decl_names = ""
    _record_tool("formal.check", "lean.check", time.perf_counter() - t0,
                 f"{rep.verdict}: {name if statement else file} [{decl_names}]", rep.verdict in ("proved", "statement_ok", "compiled"))
    if raw:
        typer.echo(f"{rep.verdict}  {rep.path or ''}\n{rep.details}")
    else:
        out(rep)


@formal_app.command("search")
def formal_search(query: str, limit: int = 10):
    """Find Mathlib lemmas by name or shape via Loogle, e.g. 'Finset.sum_range_succ' or '_ ^ 2 ≤ _ ^ 2 ↔ _'."""
    out(formal_mod.search(query, limit))


@formal_app.command("hammer")
def formal_hammer(statement: Optional[str] = None, file: Optional[Path] = None, name: str = "hammer", timeout: int = 600):
    """Ask LeanHammer (premise selection + aesop/grind/Duper/SMT) for a tactic script closing a `by hammer` goal."""
    _refuse_if_ablated("engines", "quod formal hammer")
    src = file.read_text() if file else statement
    if not src:
        fail("give --statement or --file")
    t0 = time.perf_counter()
    rep = formal_mod.hammer(src, name, timeout)
    _record_tool("formal.hammer", "lean.hammer", time.perf_counter() - t0, rep.get("verdict", "?"), rep.get("verdict") == "suggested")
    out(rep)


@formal_app.command("attach")
def formal_attach(claim: str, file: Path, promote: bool = True,
                  timeout: Optional[int] = typer.Option(None, help="seconds; default from [formal] timeout in quod.toml (900)")):
    """Check a Lean file and, if the kernel accepts it, attach `lean` evidence and promote the claim."""
    rep = formal_mod.check_file(file, timeout)
    with store() as s:
        try:
            if rep.verdict != "proved":
                out(rep)
                raise typer.Exit(2)
            c = s.require_claim(claim)
            if c.formal and c.formal.frozen:
                bad = formal_mod.frozen_violations(c.formal.frozen, Path(file).read_text())
                if bad:
                    fail("frozen statement violated — the file proves something other than the imported target:\n" + "\n".join(bad), 2)
            stored = formal_mod.unwrap(Path(rep.path).read_text())
            # The claim stores this text, not the file: it must be reproducible on its own. Re-wrapping the
            # stored form often reproduces the very source just compiled, and compiling it again cost a second
            # full Mathlib load; when the text is identical, the first verdict already answers the question.
            if formal_mod.same_source(Path(rep.path).read_text(), stored, f"{claim}_stored"):
                rep2 = rep
            else:
                rep2 = formal_mod.check(stored, name_hint=f"{claim}_stored", timeout=timeout)
            if rep2.verdict != "proved":
                fail(f"accepted file does not recompile in stored form ({rep2.verdict}): {rep2.details[:400]}", 2)
            s.add_evidence(claim, Evidence(kind="lean", summary="kernel accepted (file and stored form)", produced_by="engine:lean", artifact=rep.path))
            if s.require_claim(claim).formal is not None:
                s.set_lean(claim, stored)
            if promote:
                s.promote(claim, Rigor.FORMALLY_PROVED)
            registered = s.register_declarations(claim, stored, s.first_seen_declarations(s.require_claim(claim).branch_id))
            out({"claim": claim, "lean": rep, "rigor": s.require_claim(claim).rigor, "helper_claims_registered": registered})
        except StateError as e:
            fail(str(e))


# -- source ----------------------------------------------------------------------------------
@source_app.command("search")
def source_search(source: str, query: str = typer.Argument(""), start: int = 0, category: str = "OEIS"):
    """oeis: full-text search (10 per page, --start N). putnam: list ids (query filters by substring).
    fc: list files in a category (--category, e.g. OEIS, ErdosProblems, Mathoverflow)."""
    try:
        if source == "oeis":
            out(sources.oeis_search(query or '"Conjecture:"', start))
        elif source == "putnam":
            out([p for p in sources.putnam_list() if query in p])
        elif source == "fc":
            out([f for f in sources.fc_list(category) if query in f])
        else:
            fail("source must be oeis | putnam | fc")
    except Exception as e:  # noqa: BLE001
        fail(f"fetch failed: {e}")


@source_app.command("show")
def source_show(source: str, id: str):
    try:
        out(sources.load(source, id))
    except Exception as e:  # noqa: BLE001
        fail(str(e))


@source_app.command("open")
def source_open(source: str, id: str, branch: Optional[str] = None):
    """Create a branch for the problem (and a target claim carrying its Lean statement when the source has one)."""
    try:
        p = sources.load(source, id)
    except Exception as e:  # noqa: BLE001
        fail(str(e))
    with store() as s:
        try:
            b = sources.open_branch(s, p, branch)
        except StateError as e:
            fail(str(e))
        out({"branch": b.id, "tags": b.tags, "lean_target": bool(p.lean), "url": p.url})


# -- strategy --------------------------------------------------------------------------------
@strategy_app.command("match")
def strategy_match(branch: str = typer.Option(..., "--branch", "-b"), limit: int = 6,
                   force: bool = typer.Option(False, help="read cards even on a target that has not resisted yet")):
    """Strategy cards whose structural trigger matches this branch (read at ORIENT)."""
    if _ablated("strategy"):
        typer.echo("_strategy cards disabled in this run (ablation)_")
        return
    with store() as s:
        if not _difficulty_gate(s, branch, "quod strategy match", force):
            return
        feats = strategy_mod.features_of(s, branch)
        idf = strategy_mod.feature_idf(s)
    ss = strategy_mod.StrategyStore()
    try:
        cards = strategy_mod.match(ss.all(), feats, limit, idf=idf)
    finally:
        ss.close()
    typer.echo(f"features: {feats}\n")
    typer.echo(strategy_mod.render_cards(cards))


@strategy_app.command("extract")
def strategy_extract(branch: str = typer.Option(..., "--branch", "-b"), model: str = "sonnet", dry_run: bool = False):
    """Post-run: distill strategy/anti-strategy cards from a finished branch (cheap model; outcomes computed from closure)."""
    with store() as s:
        try:
            out(strategy_mod.extract(s, branch, model=model, dry_run=dry_run))
        except StateError as e:
            fail(str(e))


@strategy_app.command("consolidate")
def strategy_consolidate(model: str = "sonnet", dry_run: bool = False):
    """Merge duplicate cards so outcome counters accumulate across runs (cheap model proposes clusters; merge is deterministic)."""
    out(strategy_mod.consolidate(model=model, dry_run=dry_run))


@strategy_app.command("list")
def strategy_list():
    ss = strategy_mod.StrategyStore()
    try:
        out(ss.all())
    finally:
        ss.close()


@strategy_app.command("add")
def strategy_add(id: str, name: str = typer.Option(...), kind: str = "strategy", trigger: str = "",
                 feature: list[str] = typer.Option([]), step: list[str] = typer.Option([]),
                 failure: str = "", escape: str = ""):
    ss = strategy_mod.StrategyStore()
    try:
        out(ss.add(strategy_mod.StrategyCard(id=id, name=name, kind=kind, trigger=trigger, trigger_features=list(feature),
                                             procedure=list(step), observed_failure=failure, escape=escape)))
    finally:
        ss.close()


@strategy_app.command("record")
def strategy_record(id: str, branch: str = typer.Option(..., "--branch", "-b"), lemma: list[str] = typer.Option([])):
    """Record that a run used a card; the outcome (attempted/useful/decisive) is computed from the branch's closure."""
    with store() as s:
        outcome = strategy_mod.outcome_for(s, branch, list(lemma))
    ss = strategy_mod.StrategyStore()
    try:
        out({"outcome": outcome, "card": ss.record_use(id, branch, outcome, list(lemma))})
    except KeyError:
        fail(f"unknown strategy {id}")
    finally:
        ss.close()


# -- demo ------------------------------------------------------------------------------------
@demo_app.command("ladder")
def demo_ladder(n: int = 1600, xmax: float = 9.0, no_lean: bool = False):
    """Walk the whole rigor ladder with real engines. Exit 2 if any rung misbehaves."""
    with store() as s:
        res = demo_mod.run(s, n=n, xmax=xmax, lean=not no_lean)
        problems = demo_mod.expected_ok(res, lean_present=(not no_lean) and formal_mod.lean_available())
        out({"final_rigor": res["final_rigor"], "qho_max_abs_error": res["qho"]["max_abs_error"],
             "steps": {k: [st for st in v.get("steps", [])] for k, v in res["attacks"].items()}, "problems": problems})
        if problems:
            raise typer.Exit(2)


@demo_app.command("oscillator")
def demo_oscillator(n: int = 1600, xmax: float = 9.0):
    """Numerics only (kept for quick hardware checks)."""
    out(demo_mod.qho_spectrum(n=n, xmax=xmax))
