"""Strategy cards: research methodology conditioned on problem structure, learned from runs.

A card is a trigger (structural features + prose), a procedure, and *automatically* accounted outcomes:
attempted when a run cites it, useful when lemmas it produced were promoted, decisive when they are in the
target's dependency closure. Anti-strategies carry the observed failure and an escape.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

from .models import Rigor, utcnow
from .paths import library_db
from .state import StateStore

# structural features derivable from a branch's claims (formal blocks) and its problem text
FEATURE_PATTERNS = {
    "sum": r"\bSum\(|∑", "integral": r"\bIntegral\(|∫", "product": r"\bProduct\(|∏", "mod": r"\bMod\(|\bZMod\b|%",
    "prime": r"isprime|\bPrime\b|prime", "polynomial": r"[Pp]olynomial|\bcoeff\b|\*\*\s*[3-9]", "matrix": r"Matrix|matrix",
    "inequality": r"\b(Le|Lt|Ge|Gt)\(|[≤≥<>]", "equation": r"\bEq\(|=", "trig": r"\b(sin|cos|tan)\b", "exp-log": r"\b(exp|log)\b",
    "permutation": r"[Pp]ermutation|Equiv\.Perm|bijection", "counting": r"\bcard\b|ncard|number of|count", "game": r"\b[Gg]ame\b|Alice|Bob",
    "real-analysis": r"deriv|Continuous|Tendsto|ContDiff|limit", "complex": r"\bℂ\b|Complex", "sequence": r"a_n|a \(n|sequence|recurrence",
    "existential": r"∃|exists|there exist", "universal-forall": r"∀|for all|every", "find-answer": r"putnam_\d+_[ab]\d_solution|Determine|Find all",
    "symmetry": r"symmetric|symmetry|invariant", "measure": r"Measure|μH|volume|Hausdorff", "functional-equation": r"f\(x\s*\+\s*y\)|f\(f\(|functional",
}


class StrategyCard(BaseModel):
    id: str
    name: str
    kind: str = "strategy"  # strategy | anti-strategy
    trigger_features: list[str] = Field(default_factory=list)
    trigger: str = ""
    procedure: list[str] = Field(default_factory=list)
    observed_failure: str = ""
    escape: str = ""
    attempted: int = 0
    useful: int = 0
    decisive: int = 0
    sources: list[str] = Field(default_factory=list)  # branch ids
    created_at: str = Field(default_factory=utcnow)


SCHEMA = """
CREATE TABLE IF NOT EXISTS strategies(id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS strategy_uses(id INTEGER PRIMARY KEY AUTOINCREMENT, strategy_id TEXT NOT NULL, branch_id TEXT NOT NULL,
  outcome TEXT NOT NULL, lemmas TEXT, ts TEXT NOT NULL);
"""


class StrategyStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else library_db()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def add(self, card: StrategyCard) -> StrategyCard:
        self.db.execute("INSERT OR REPLACE INTO strategies(id,kind,payload,created_at) VALUES(?,?,?,?)",
                        (card.id, card.kind, card.model_dump_json(), card.created_at))
        return card

    def get(self, sid: str) -> StrategyCard | None:
        row = self.db.execute("SELECT payload FROM strategies WHERE id=?", (sid,)).fetchone()
        if row:
            return StrategyCard.model_validate_json(row[0])
        # `strategy match` used to print only the name, so agents passed the name here.
        # Accept it when it identifies exactly one card.
        hits = [c for c in self.all() if c.name == sid]
        return hits[0] if len(hits) == 1 else None

    def all(self) -> list[StrategyCard]:
        return [StrategyCard.model_validate_json(r[0]) for r in self.db.execute("SELECT payload FROM strategies ORDER BY created_at")]

    def record_use(self, sid: str, branch_id: str, outcome: str, lemmas: list[str]) -> StrategyCard:
        card = self.get(sid)
        if not card:
            raise KeyError(sid)
        self.db.execute("INSERT INTO strategy_uses(strategy_id,branch_id,outcome,lemmas,ts) VALUES(?,?,?,?,?)",
                        (sid, branch_id, outcome, json.dumps(lemmas), utcnow()))
        card.attempted += 1
        if outcome in ("useful", "decisive"):
            card.useful += 1
        if outcome == "decisive":
            card.decisive += 1
        if branch_id not in card.sources:
            card.sources.append(branch_id)
        return self.add(card)


def features_of(store: StateStore, branch_id: str) -> list[str]:
    b = store.require_branch(branch_id)
    text = b.problem + " " + b.objective + " "
    for c in store.claims(branch_id):
        text += c.statement + " "
        if c.formal:
            text += " ".join(c.formal.hypotheses) + " " + c.formal.conclusion + " " + (c.formal.lean or "") + " "
    return [f for f, pat in FEATURE_PATTERNS.items() if re.search(pat, text)]


import math


def feature_idf(store: StateStore | None) -> dict[str, float]:
    """Rarity weight per feature across all branches: generic features (equation, ∀) weigh little,
    specific ones (measure, game, permutation) decide a match. Uniform when no store is given."""
    if store is None:
        return {f: 1.0 for f in FEATURE_PATTERNS}
    branches = store.branches()
    n = max(1, len(branches))
    counts = {f: 0 for f in FEATURE_PATTERNS}
    for b in branches:
        for f in features_of(store, b.id):
            counts[f] += 1
    return {f: math.log((n + 1) / (counts[f] + 1)) + 0.1 for f in FEATURE_PATTERNS}


def match(cards: list[StrategyCard], features: list[str], limit: int = 8, idf: dict[str, float] | None = None,
          min_score: float = 0.35) -> list[tuple[StrategyCard, float, list[str]]]:
    """Rank cards by rarity-weighted trigger overlap, scaled by historical evidence. Returns (card, score, shared)."""
    fs = set(features)
    idf = idf or {f: 1.0 for f in FEATURE_PATTERNS}
    scored = []
    for c in cards:
        trig = set(c.trigger_features)
        if not trig:
            continue
        shared = sorted(fs & trig, key=lambda f: -idf.get(f, 0))
        if not shared:
            continue
        overlap = sum(idf.get(f, 0) for f in shared) / sum(idf.get(f, 0) for f in trig)
        # evidence-weighted: decisive counts most, attempted-but-useless counts against
        evidence = (2 * c.decisive + c.useful + 0.5) / (c.attempted + 1)
        score = overlap * (0.5 + evidence)
        if overlap >= min_score:
            scored.append((c, score, shared))
    scored.sort(key=lambda x: -x[1])
    return scored[:limit]


def outcome_for(store: StateStore, branch_id: str, lemma_names: list[str]) -> str:
    """Automatic accounting: decisive if any named lemma is in the target's closure, useful if promoted, else attempted."""
    from .trace import closure  # noqa: PLC0415

    from .formal import qualified_theorem_names  # noqa: PLC0415

    cl = closure(store, branch_id)
    closure_text = cl["proof"]
    proof_decls = set(qualified_theorem_names(closure_text)) | {n.split(".")[-1] for n in qualified_theorem_names(closure_text)}
    claims = {c.id: c for c in store.claims(branch_id)}
    names = [n for n in lemma_names if n]
    # a claim id resolves to the Lean declarations it carries; a bare name is checked as a declaration
    for n in names:
        c = claims.get(n)
        decls = qualified_theorem_names(c.formal.lean) if (c and c.formal and c.formal.lean) else [n]
        if any(d in proof_decls or d.split(".")[-1] in proof_decls for d in decls):
            return "decisive"
    for n in names:
        c = claims.get(n)
        if c and c.rigor not in (Rigor.SPECULATIVE, Rigor.REFUTED):
            return "useful"
        if any(n in (cc.formal.lean or "") for cc in claims.values() if cc.formal and cc.rigor == Rigor.FORMALLY_PROVED):
            return "useful"
    return "attempted"


EXTRACT_SYSTEM = """You distill research methodology from a finished mathematics run. You are given the branch dossier
(claims, failed strategies with exact reasons, checkpoint) and the final Lean proof outline. Produce strategy cards:
reusable procedures conditioned on problem STRUCTURE, not on this problem's content. Also produce anti-strategies for
each recorded failed strategy (what was tried, why it failed, the escape that worked).

Respond with ONLY a JSON array of objects:
{"name": "kebab-case-name", "kind": "strategy"|"anti-strategy",
 "trigger_features": [subset of: %s],
 "trigger": "<one sentence, structural>", "procedure": ["step", ...],
 "observed_failure": "<anti only>", "escape": "<anti only>",
 "lemmas": ["<claim ids or Lean declaration names this strategy produced in THIS run>"]}
At most 4 cards. Prefer one sharp card over several vague ones. Never mention the problem's name or year."""


def extract(store: StateStore, branch_id: str, model: str = "sonnet", timeout: int = 600, dry_run: bool = False) -> dict:
    """Post-run extraction pass with a cheap model and minimal context. Outcomes are then computed, not self-reported."""
    from . import dossier  # noqa: PLC0415

    cl_features = features_of(store, branch_id)
    d = dossier.render(store, branch_id, max_chars=10000)
    claims = store.claims(branch_id)
    target = next((c for c in claims if "target" in c.tags), None)
    outline = ""
    if target and target.formal and target.formal.lean:
        outline = "\n".join(l for l in target.formal.lean.splitlines() if re.match(r"^\s*(theorem|lemma|def|abbrev|noncomputable def)\b", l))[:4000]
    prompt = f"## Structural features\n{cl_features}\n\n## Dossier\n{d}\n\n## Final proof outline (declarations only)\n{outline}\n"
    system = EXTRACT_SYSTEM % ", ".join(FEATURE_PATTERNS)
    if dry_run:
        return {"model": model, "features": cl_features, "prompt": prompt}
    cli = os.environ.get("ANTHROPIC_CLI", "claude")
    cmd = [cli, "-p", "--no-session-persistence", "--model", model, "--output-format", "json", "--tools", "",
           "--max-turns", "1", "--disable-slash-commands", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--setting-sources", "", "--system-prompt", system]
    env = {**os.environ, "QUOD_ROLE": "extractor"}
    with tempfile.TemporaryDirectory(prefix="quod-extract-") as cwd:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env)
    if proc.returncode != 0:
        return {"error": proc.stderr[-1500:]}
    try:
        text = json.loads(proc.stdout).get("result", "")
    except json.JSONDecodeError:
        text = proc.stdout
    start, end = text.find("["), text.rfind("]")
    try:
        cards_raw = json.loads(text[start:end + 1]) if start != -1 else []
    except json.JSONDecodeError:
        return {"error": "extractor did not return JSON", "raw": text[:1000]}
    ss = StrategyStore()
    stored = []
    try:
        for raw in cards_raw[:4]:
            name = re.sub(r"[^a-z0-9-]", "-", str(raw.get("name", "card")).lower()).strip("-")[:60]
            kind = "anti-strategy" if str(raw.get("kind", "")).startswith("anti") else "strategy"
            sid = f"{'A' if kind == 'anti-strategy' else 'S'}-{name}"
            card = ss.get(sid) or StrategyCard(id=sid, name=name, kind=kind)
            card.trigger_features = [f for f in raw.get("trigger_features", []) if f in FEATURE_PATTERNS] or card.trigger_features
            card.trigger = raw.get("trigger", card.trigger)
            card.procedure = raw.get("procedure", card.procedure) or card.procedure
            card.observed_failure = raw.get("observed_failure", card.observed_failure)
            card.escape = raw.get("escape", card.escape)
            ss.add(card)
            outcome = "attempted" if kind == "anti-strategy" else outcome_for(store, branch_id, raw.get("lemmas", []))
            ss.record_use(sid, branch_id, outcome, raw.get("lemmas", []))
            stored.append({"id": sid, "kind": kind, "outcome": outcome, "features": card.trigger_features})
    finally:
        ss.close()
    store.event(branch_id, "strategy.extracted", {"model": model, "cards": stored})
    return {"model": model, "features": cl_features, "cards": stored}


def render_cards(cards: list[tuple[StrategyCard, float, list[str]]]) -> str:
    L = []
    for c, score, shared in cards:
        # the id, not just the name: `quod strategy record` takes the id
        head = f"[{c.kind}] {c.id}  ({c.name}; attempted {c.attempted}, useful {c.useful}, decisive {c.decisive})"
        L.append(head)
        L.append(f"  matched on: {shared}   trigger: {c.trigger}")
        if c.kind == "strategy":
            L += [f"  {i + 1}. {s}" for i, s in enumerate(c.procedure)]
        else:
            L += [f"  failure: {c.observed_failure}", f"  escape:  {c.escape}"]
    return "\n".join(L) or "_no matching strategy cards_"


CONSOLIDATE_SYSTEM = """You curate a library of mathematical research strategy cards. You are given cards as one line each:
`id | kind | trigger_features | trigger`. Group cards that describe the SAME reusable procedure (same trigger
structure and same core move) even if worded differently. Cards that merely share features are NOT the same.
Respond with ONLY a JSON array of clusters: [{"canonical": "<id to keep>", "merge": ["<id>", ...]}, ...]
Include only clusters with at least two cards. Prefer the most general, best-worded card as canonical. Never merge a
strategy with an anti-strategy."""


def consolidate(model: str = "sonnet", timeout: int = 600, dry_run: bool = False) -> dict:
    """Merge duplicate cards: a cheap model proposes clusters, the merge itself is deterministic (counters summed,
    sources and features unioned, uses re-pointed). Without this, every card stays at attempted=1 forever."""
    ss = StrategyStore()
    try:
        cards = ss.all()
        lines = "\n".join(f"{c.id} | {c.kind} | {','.join(c.trigger_features)} | {c.trigger[:220]}" for c in cards)
        if dry_run:
            return {"n_cards": len(cards), "prompt_chars": len(lines)}
        cli = os.environ.get("ANTHROPIC_CLI", "claude")
        cmd = [cli, "-p", "--no-session-persistence", "--model", model, "--output-format", "json", "--tools", "",
               "--max-turns", "1", "--disable-slash-commands", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
               "--setting-sources", "", "--system-prompt", CONSOLIDATE_SYSTEM]
        with tempfile.TemporaryDirectory(prefix="quod-consolidate-") as cwd:
            proc = subprocess.run(cmd, input=lines, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                                  env={**os.environ, "QUOD_ROLE": "curator"})
        if proc.returncode != 0:
            return {"error": proc.stderr[-1200:]}
        try:
            text = json.loads(proc.stdout).get("result", "")
        except json.JSONDecodeError:
            text = proc.stdout
        start, end = text.find("["), text.rfind("]")
        try:
            clusters = json.loads(text[start:end + 1]) if start != -1 else []
        except json.JSONDecodeError:
            return {"error": "curator did not return JSON", "raw": text[:800]}
        by_id = {c.id: c for c in cards}
        merged = []
        for cl in clusters:
            canon = by_id.get(cl.get("canonical"))
            members = [by_id[m] for m in cl.get("merge", []) if m in by_id and m != cl.get("canonical")]
            if not canon or not members or any(m.kind != canon.kind for m in members):
                continue
            for m in members:
                canon.attempted += m.attempted
                canon.useful += m.useful
                canon.decisive += m.decisive
                canon.sources = sorted(set(canon.sources) | set(m.sources))
                canon.trigger_features = sorted(set(canon.trigger_features) | set(m.trigger_features))
                if m.kind == "anti-strategy" and m.escape and m.escape not in canon.escape:
                    canon.escape = (canon.escape + " | " + m.escape).strip(" |")
                ss.db.execute("UPDATE strategy_uses SET strategy_id=? WHERE strategy_id=?", (canon.id, m.id))
                ss.db.execute("DELETE FROM strategies WHERE id=?", (m.id,))
            ss.add(canon)
            merged.append({"canonical": canon.id, "absorbed": [m.id for m in members], "attempted": canon.attempted, "decisive": canon.decisive})
        return {"model": model, "before": len(cards), "after": len(ss.all()), "merged": merged}
    finally:
        ss.close()
