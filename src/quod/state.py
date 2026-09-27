"""SQLite event/claim store. Canonical research memory; model context is disposable."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from .models import (
    AttackReport,
    Branch,
    BranchCheckpoint,
    Claim,
    Contribution,
    Epoch,
    Evidence,
    FailedStrategy,
    FormalClaim,
    Obstruction,
    Rigor,
    ToolboxProposal,
    utcnow,
)
from .paths import state_db

SCHEMA = """
CREATE TABLE IF NOT EXISTS branches(
  id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS epochs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, branch_id TEXT NOT NULL, payload TEXT NOT NULL,
  started_at TEXT NOT NULL, ended_at TEXT);
CREATE TABLE IF NOT EXISTS claims(
  id TEXT PRIMARY KEY, branch_id TEXT NOT NULL, rigor TEXT NOT NULL, payload TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS checkpoints(
  id INTEGER PRIMARY KEY AUTOINCREMENT, branch_id TEXT NOT NULL, ts TEXT NOT NULL,
  auto INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS attacks(
  id INTEGER PRIMARY KEY AUTOINCREMENT, branch_id TEXT NOT NULL, claim_id TEXT NOT NULL,
  outcome TEXT NOT NULL, ts TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ledger(
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, status TEXT NOT NULL,
  ts TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS contributions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, target TEXT NOT NULL, status TEXT NOT NULL,
  ts TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, branch_id TEXT NOT NULL,
  event_type TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS obstructions(
  id TEXT PRIMARY KEY, branch_id TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
  ts TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS turns(
  id INTEGER PRIMARY KEY AUTOINCREMENT, epoch_id INTEGER NOT NULL, idx INTEGER NOT NULL, ts TEXT NOT NULL,
  input_tokens INTEGER, cache_read INTEGER, cache_create INTEGER, output_tokens INTEGER, context_total INTEGER);
CREATE TABLE IF NOT EXISTS tool_calls(
  id INTEGER PRIMARY KEY AUTOINCREMENT, epoch_id INTEGER NOT NULL, branch_id TEXT NOT NULL, ts TEXT NOT NULL,
  seconds REAL, tool TEXT NOT NULL, kind TEXT NOT NULL, summary TEXT, ok INTEGER);
CREATE INDEX IF NOT EXISTS idx_turns_epoch ON turns(epoch_id);
CREATE INDEX IF NOT EXISTS idx_tool_calls_epoch ON tool_calls(epoch_id);
CREATE INDEX IF NOT EXISTS idx_claims_branch ON claims(branch_id);
CREATE INDEX IF NOT EXISTS idx_events_branch ON events(branch_id);
CREATE INDEX IF NOT EXISTS idx_obstructions_branch ON obstructions(branch_id);
CREATE INDEX IF NOT EXISTS idx_checkpoints_branch ON checkpoints(branch_id, id);
"""


class StateError(RuntimeError):
    pass


class StateStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else state_db()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)

    # -- lifecycle -------------------------------------------------------------------------
    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- events ----------------------------------------------------------------------------
    def event(self, branch_id: str, event_type: str, payload: dict) -> None:
        self.db.execute(
            "INSERT INTO events(ts,branch_id,event_type,payload) VALUES(?,?,?,?)",
            (utcnow(), branch_id, event_type, json.dumps(payload, sort_keys=True, default=str)),
        )

    def events(self, branch_id: str, limit: int = 50) -> list[dict]:
        rows = self.db.execute(
            "SELECT ts,event_type,payload FROM events WHERE branch_id=? ORDER BY id DESC LIMIT ?",
            (branch_id, limit),
        ).fetchall()
        return [{"ts": t, "type": e, "payload": json.loads(p)} for t, e, p in rows]

    # -- branches --------------------------------------------------------------------------
    def create_branch(self, branch: Branch) -> Branch:
        if self.get_branch(branch.id):
            raise StateError(f"branch {branch.id} already exists")
        if branch.parent and not self.get_branch(branch.parent):
            raise StateError(f"parent branch {branch.parent} does not exist")
        self.db.execute(
            "INSERT INTO branches(id,payload,status,created_at) VALUES(?,?,?,?)",
            (branch.id, branch.model_dump_json(), branch.status, branch.created_at),
        )
        self.event(branch.id, "branch.created", branch.model_dump(mode="json"))
        return branch

    def get_branch(self, branch_id: str) -> Branch | None:
        row = self.db.execute("SELECT payload FROM branches WHERE id=?", (branch_id,)).fetchone()
        return Branch.model_validate_json(row[0]) if row else None

    def require_branch(self, branch_id: str) -> Branch:
        b = self.get_branch(branch_id)
        if not b:
            raise StateError(f"unknown branch {branch_id!r}; create it with `quod branch new`")
        return b

    def branches(self, status: str | None = None) -> list[Branch]:
        q = "SELECT payload FROM branches" + (" WHERE status=?" if status else "") + " ORDER BY created_at"
        rows = self.db.execute(q, (status,) if status else ()).fetchall()
        return [Branch.model_validate_json(r[0]) for r in rows]

    def set_branch_status(self, branch_id: str, status: str, reason: str, force: bool = False) -> Branch:
        b = self.require_branch(branch_id)
        allowed = ("open", "proved", "refuted", "closed", "abandoned")
        if status not in allowed:
            raise StateError(f"invalid branch status {status!r}; one of {allowed}")
        if status in ("closed", "abandoned") and not force:
            # Giving up is a recorded result or it is not a result. A branch may only stop when the work
            # it did is legible to whoever reads it next: distinct failed strategies and a named obstruction.
            fails = self.failed_strategies(branch_id)
            obs = self.obstructions(branch_id)
            missing = []
            if len({f.strategy.strip().lower() for f in fails}) < 2:
                missing.append(f"at least 2 distinct failed strategies with exact reasons (have {len(fails)}) — `quod fail-strategy`")
            if not obs:
                missing.append("at least 1 obstruction naming the blocked step and what would remove it — `quod obstruction add`")
            if missing:
                raise StateError(
                    f"refusing to mark branch {branch_id} {status}: " + "; ".join(missing)
                    + ". A target is never closed for being hard — only for being proved, refuted, or blocked by "
                      "something you can name. If the epoch budget ran out, leave the branch `open` and checkpoint."
                )
        b.status = status  # type: ignore[assignment]
        self.db.execute("UPDATE branches SET payload=?, status=? WHERE id=?", (b.model_dump_json(), status, branch_id))
        self.event(branch_id, "branch.status", {"status": status, "reason": reason})
        return b

    # -- epochs ----------------------------------------------------------------------------
    def start_epoch(self, epoch: Epoch) -> Epoch:
        self.require_branch(epoch.branch_id)
        cur = self.db.execute(
            "INSERT INTO epochs(branch_id,payload,started_at) VALUES(?,?,?)",
            (epoch.branch_id, epoch.model_dump_json(), epoch.started_at),
        )
        epoch.id = cur.lastrowid
        self.db.execute("UPDATE epochs SET payload=? WHERE id=?", (epoch.model_dump_json(), epoch.id))
        self.event(epoch.branch_id, "epoch.started", epoch.model_dump(mode="json"))
        return epoch

    def current_epoch(self, branch_id: str) -> Epoch | None:
        row = self.db.execute(
            "SELECT payload FROM epochs WHERE branch_id=? AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
            (branch_id,),
        ).fetchone()
        return Epoch.model_validate_json(row[0]) if row else None

    def end_epoch(self, branch_id: str, reason: str, tokens_in: int = 0, tokens_out: int = 0) -> Epoch | None:
        ep = self.current_epoch(branch_id)
        if not ep:
            return None
        ep.ended_at = utcnow()
        ep.end_reason = reason
        ep.tokens_in += tokens_in
        ep.tokens_out += tokens_out
        self.db.execute("UPDATE epochs SET payload=?, ended_at=? WHERE id=?", (ep.model_dump_json(), ep.ended_at, ep.id))
        self.event(branch_id, "epoch.ended", ep.model_dump(mode="json"))
        return ep

    def set_epoch_usage(self, branch_id: str, tokens_in: int, tokens_out: int, epoch_id: int | None = None) -> Epoch:
        """Overwrite the token counts of one epoch (default: the latest, open or closed) with measured numbers."""
        eps = self.epochs(branch_id)
        if not eps:
            raise StateError(f"branch {branch_id} has no epochs")
        ep = next((e for e in eps if e.id == epoch_id), None) if epoch_id else eps[-1]
        if ep is None:
            raise StateError(f"no epoch {epoch_id} on branch {branch_id}")
        ep.tokens_in, ep.tokens_out = tokens_in, tokens_out
        self.db.execute("UPDATE epochs SET payload=? WHERE id=?", (ep.model_dump_json(), ep.id))
        self.event(branch_id, "epoch.usage_measured", {"epoch": ep.id, "tokens_in": tokens_in, "tokens_out": tokens_out})
        return ep

    def epochs(self, branch_id: str) -> list[Epoch]:
        rows = self.db.execute("SELECT payload FROM epochs WHERE branch_id=? ORDER BY id", (branch_id,)).fetchall()
        return [Epoch.model_validate_json(r[0]) for r in rows]

    # -- claims ----------------------------------------------------------------------------
    def add_claim(self, claim: Claim) -> Claim:
        self.require_branch(claim.branch_id)
        if self.get_claim(claim.id):
            raise StateError(f"claim {claim.id} already exists")
        if claim.rigor != Rigor.SPECULATIVE:
            err = Claim(id=claim.id, branch_id=claim.branch_id, statement=claim.statement,
                        evidence=claim.evidence).promotion_error(claim.rigor)
            if err:
                raise StateError(f"cannot create claim at rigor {claim.rigor}: {err}")
        if claim.supersedes and not self.get_claim(claim.supersedes):
            raise StateError(f"superseded claim {claim.supersedes} does not exist")
        for dep in claim.depends_on:
            if not self.get_claim(dep):
                raise StateError(f"dependency {dep} does not exist")
        self._write_claim(claim)
        self.event(claim.branch_id, "claim.created", claim.model_dump(mode="json"))
        return claim

    def _write_claim(self, claim: Claim) -> None:
        claim.updated_at = utcnow()
        self.db.execute(
            "INSERT OR REPLACE INTO claims(id,branch_id,rigor,payload,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (claim.id, claim.branch_id, claim.rigor, claim.model_dump_json(), claim.created_at, claim.updated_at),
        )

    def get_claim(self, claim_id: str) -> Claim | None:
        row = self.db.execute("SELECT payload FROM claims WHERE id=?", (claim_id,)).fetchone()
        return Claim.model_validate_json(row[0]) if row else None

    def require_claim(self, claim_id: str) -> Claim:
        c = self.get_claim(claim_id)
        if not c:
            raise StateError(f"unknown claim {claim_id!r}")
        return c

    def claims(self, branch_id: str | None = None, rigor: Rigor | None = None) -> list[Claim]:
        q, args = "SELECT payload FROM claims", []
        conds = []
        if branch_id:
            conds.append("branch_id=?"); args.append(branch_id)
        if rigor:
            conds.append("rigor=?"); args.append(rigor)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY created_at, id"
        return [Claim.model_validate_json(r[0]) for r in self.db.execute(q, args).fetchall()]

    def add_evidence(self, claim_id: str, ev: Evidence) -> Claim:
        c = self.require_claim(claim_id)
        c.evidence.append(ev)
        self._write_claim(c)
        self.event(c.branch_id, "evidence.added", {"claim": claim_id, **ev.model_dump(mode="json")})
        return c

    def set_lean(self, claim_id: str, lean_source: str) -> Claim:
        """Replace the claim's formal.lean with the text the kernel actually accepted."""
        c = self.require_claim(claim_id)
        if c.formal is None:
            raise StateError(f"claim {claim_id} has no formal block to attach Lean text to")
        c.formal.lean = lean_source
        self._write_claim(c)
        self.event(c.branch_id, "claim.lean_updated", {"claim": claim_id, "chars": len(lean_source)})
        return c

    def promote(self, claim_id: str, target: Rigor) -> Claim:
        c = self.require_claim(claim_id)
        err = c.promotion_error(target)
        if err:
            raise StateError(f"promotion {c.rigor} -> {target} rejected: {err}")
        prev = c.rigor
        c.rigor = target
        self._write_claim(c)
        self.event(c.branch_id, "claim.promoted", {"claim": claim_id, "from": prev, "to": target})
        return c

    def refute(self, claim_id: str, counterexample: str, produced_by: str, artifact: str | None = None) -> Claim:
        self.add_evidence(claim_id, Evidence(kind="counterexample", summary=counterexample,
                                             produced_by=produced_by, artifact=artifact))
        return self.promote(claim_id, Rigor.REFUTED)

    def register_declarations(self, target_claim_id: str, accepted_source: str, first_seen: dict[str, str] | None = None) -> list[str]:
        """After the kernel accepts a target proof, make every helper theorem/lemma in it a claim of its own
        (rigor formally_proved, lean evidence), so dependency-closure attribution reads the artifact rather than
        relying on the agent to have registered lemmas. Idempotent; existing claims are left alone."""
        from .formal import declaration_blocks, qualified_theorem_names  # noqa: PLC0415

        target = self.require_claim(target_claim_id)
        # the target's own declaration(s): from the frozen statement when imported, else the last declaration
        target_short: set[str] = set()
        if target.formal and target.formal.frozen:
            for sig in target.formal.frozen:
                parts = sig.split()
                if len(parts) > 1 and parts[0] in ("theorem", "lemma"):
                    target_short.add(parts[1].split(".")[-1])
        if not target_short:
            names = qualified_theorem_names(accepted_source)
            if names:
                target_short.add(names[-1].split(".")[-1])
        created = []
        for name, block in declaration_blocks(accepted_source).items():
            if name.split(".")[-1] in target_short:
                continue  # the target itself
            cid = f"{target_claim_id}-decl-{name.split('.')[-1]}"[:120]
            if self.get_claim(cid):
                continue
            ev = Evidence(kind="lean", summary=f"helper declaration accepted with {target_claim_id}", produced_by="engine:lean",
                          metadata={"auto": True, "first_seen": (first_seen or {}).get(name.split(".")[-1])})
            c = Claim(id=cid, branch_id=target.branch_id, statement=f"[helper] {name}", formal=FormalClaim(lean=block),
                      evidence=[ev], rigor=Rigor.FORMALLY_PROVED, depends_on=[], tags=["lemma", "auto"])
            if (first_seen or {}).get(name.split(".")[-1]):
                c.created_at = first_seen[name.split(".")[-1]]
            self._write_claim(c)
            self.event(target.branch_id, "claim.auto_registered", {"claim": cid, "from": target_claim_id, "first_seen": c.created_at})
            created.append(cid)
        return created

    def first_seen_declarations(self, branch_id: str) -> dict[str, str]:
        """Declaration short name -> timestamp of the first *successful* in-process Lean check that contained it."""
        rows = self.db.execute("SELECT ts, summary FROM tool_calls WHERE branch_id=? AND kind='lean.check' AND tool NOT LIKE 'stream:%' "
                               "AND ok=1 ORDER BY ts", (branch_id,)).fetchall()
        seen: dict[str, str] = {}
        for ts, summary in rows:
            m = re.search(r"\[([^\]]*)\]\s*$", summary or "")
            for n in (m.group(1).split(",") if m else []):
                short = n.strip().split(".")[-1]
                if short and short not in seen:
                    seen[short] = ts
        return seen

    # -- attacks ---------------------------------------------------------------------------
    def record_attack(self, report: AttackReport) -> AttackReport:
        self.require_claim(report.claim_id)
        self.db.execute(
            "INSERT INTO attacks(branch_id,claim_id,outcome,ts,payload) VALUES(?,?,?,?,?)",
            (report.branch_id, report.claim_id, report.outcome, report.created_at, report.model_dump_json()),
        )
        self.event(report.branch_id, "attack.recorded", report.model_dump(mode="json"))
        return report

    def attacks(self, branch_id: str | None = None, claim_id: str | None = None) -> list[AttackReport]:
        q, args, conds = "SELECT payload FROM attacks", [], []
        if branch_id:
            conds.append("branch_id=?"); args.append(branch_id)
        if claim_id:
            conds.append("claim_id=?"); args.append(claim_id)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY id"
        return [AttackReport.model_validate_json(r[0]) for r in self.db.execute(q, args).fetchall()]

    def attack_stats(self) -> dict[str, dict[str, int]]:
        """Hit rate per attacker: how often each attacker actually found something."""
        rows = self.db.execute("SELECT payload FROM attacks").fetchall()
        stats: dict[str, dict[str, int]] = {}
        for (p,) in rows:
            r = AttackReport.model_validate_json(p)
            s = stats.setdefault(r.attacker, {"total": 0, "refuted": 0, "weakened": 0, "survived": 0, "inconclusive": 0})
            s["total"] += 1
            s[r.outcome] += 1
        return stats

    # -- failed strategies -----------------------------------------------------------------
    def record_failure(self, branch_id: str, fs: FailedStrategy) -> FailedStrategy:
        self.require_branch(branch_id)
        self.event(branch_id, "strategy.failed", fs.model_dump(mode="json"))
        return fs

    def failed_strategies(self, branch_id: str) -> list[FailedStrategy]:
        rows = self.db.execute(
            "SELECT payload FROM events WHERE branch_id=? AND event_type='strategy.failed' ORDER BY id", (branch_id,)
        ).fetchall()
        return [FailedStrategy.model_validate_json(r[0]) for r in rows]

    # -- obstructions ----------------------------------------------------------------------
    def add_obstruction(self, ob: Obstruction) -> Obstruction:
        self.require_branch(ob.branch_id)
        if self.db.execute("SELECT 1 FROM obstructions WHERE id=?", (ob.id,)).fetchone():
            raise StateError(f"obstruction {ob.id} already exists")
        self.db.execute("INSERT INTO obstructions(id,branch_id,kind,status,ts,payload) VALUES(?,?,?,?,?,?)",
                        (ob.id, ob.branch_id, ob.kind, ob.status, ob.created_at, ob.model_dump_json()))
        self.event(ob.branch_id, "obstruction.added", ob.model_dump(mode="json"))
        return ob

    def obstructions(self, branch_id: str | None = None, status: str | None = None) -> list[Obstruction]:
        where, args = [], []
        if branch_id:
            where.append("branch_id=?")
            args.append(branch_id)
        if status:
            where.append("status=?")
            args.append(status)
        q = "SELECT payload FROM obstructions" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY ts"
        return [Obstruction.model_validate_json(r[0]) for r in self.db.execute(q, tuple(args)).fetchall()]

    def clear_obstruction(self, ob_id: str, how: str) -> Obstruction:
        row = self.db.execute("SELECT payload FROM obstructions WHERE id=?", (ob_id,)).fetchone()
        if not row:
            raise StateError(f"unknown obstruction {ob_id}")
        ob = Obstruction.model_validate_json(row[0])
        if len(how.strip()) < 15:
            raise StateError("say how it was cleared (>= 15 chars); a cleared obstruction is a result")
        ob.status, ob.cleared_how = "cleared", how
        self.db.execute("UPDATE obstructions SET status=?, payload=? WHERE id=?", (ob.status, ob.model_dump_json(), ob_id))
        self.event(ob.branch_id, "obstruction.cleared", {"id": ob_id, "how": how})
        return ob

    # -- effort signals --------------------------------------------------------------------
    def difficulty(self, branch_id: str) -> dict:
        """Evidence that this target is actually resisting. Machinery that costs tokens (strategy cards,
        `quod propose`) is gated on this so it is not paid for on problems that fall to a first attempt."""
        b = self.require_branch(branch_id)
        calls = self.tool_calls(branch_id)
        lean_failed = sum(1 for c in calls if c["kind"].startswith("lean") and not c["ok"])
        claims = self.claims(branch_id)
        target = next((c for c in claims if "target" in c.tags), None)
        signals = {
            "failed_strategies": len(self.failed_strategies(branch_id)),
            "lean_rejections": lean_failed,
            "epochs": len(self.epochs(branch_id)),
            "open_obstructions": len(self.obstructions(branch_id, status="open")),
            "refuted_claims": sum(1 for c in claims if c.rigor == Rigor.REFUTED),
            "declared_hard": bool({"hard", "open-problem", "programme"} & set(b.tags)),
            "target_proved": bool(target and target.rigor == Rigor.FORMALLY_PROVED),
        }
        signals["hard"] = bool(
            signals["declared_hard"]
            or signals["failed_strategies"] >= 1
            or signals["lean_rejections"] >= 2
            or signals["epochs"] >= 2
            or signals["refuted_claims"] >= 1
            or signals["open_obstructions"] >= 1
        )
        reasons = [k for k in ("declared_hard", "failed_strategies", "lean_rejections", "epochs", "refuted_claims",
                               "open_obstructions") if signals[k]]
        signals["why"] = reasons
        return signals

    def epoch_activity(self, branch_id: str, epoch_id: int | None = None) -> dict:
        """What the current (or given) epoch has actually recorded. An epoch that recorded nothing is a
        bug or a performance, not a result — `quod epoch end` refuses it."""
        eps = self.epochs(branch_id)
        ep = next((e for e in eps if e.id == epoch_id), None) if epoch_id else (self.current_epoch(branch_id) or (eps[-1] if eps else None))
        if ep is None:
            return {"epoch": None, "total": 0}
        since = ep.started_at
        rows = self.db.execute(
            "SELECT event_type, COUNT(*) FROM events WHERE branch_id=? AND ts>=? GROUP BY event_type", (branch_id, since)
        ).fetchall()
        counted = {"claim.created", "claim.promoted", "claim.auto_registered", "evidence.added", "strategy.failed",
                   "obstruction.added", "obstruction.cleared", "attack.recorded", "ledger.proposed"}
        by = {t: n for t, n in rows}
        total = sum(n for t, n in by.items() if t in counted)
        return {"epoch": ep.id, "since": since, "by_type": by, "total": total,
                "recorded": sorted(t for t in by if t in counted)}

    # -- checkpoints -----------------------------------------------------------------------
    def checkpoint(self, cp: BranchCheckpoint) -> BranchCheckpoint:
        self.require_branch(cp.branch_id)
        ep = self.current_epoch(cp.branch_id)
        if cp.epoch_id is None and ep:
            cp.epoch_id = ep.id
        self.db.execute(
            "INSERT INTO checkpoints(branch_id,ts,auto,payload) VALUES(?,?,?,?)",
            (cp.branch_id, cp.created_at, int(cp.auto), cp.model_dump_json()),
        )
        self.event(cp.branch_id, "checkpoint.created", {"auto": cp.auto, "next_actions": cp.next_actions})
        return cp

    def latest_checkpoint(self, branch_id: str, include_auto: bool = True) -> BranchCheckpoint | None:
        q = "SELECT payload FROM checkpoints WHERE branch_id=?" + ("" if include_auto else " AND auto=0")
        row = self.db.execute(q + " ORDER BY id DESC LIMIT 1", (branch_id,)).fetchone()
        return BranchCheckpoint.model_validate_json(row[0]) if row else None

    def auto_checkpoint(self, branch_id: str, reason: str) -> BranchCheckpoint:
        """Snapshot taken by a hook (compaction/stop). Derived from state, never from the transcript,
        so it is a floor, not a substitute for the agent's own checkpoint."""
        b = self.require_branch(branch_id)
        last = self.latest_checkpoint(branch_id)
        established = [c.id for c in self.claims(branch_id)
                       if c.rigor not in (Rigor.SPECULATIVE, Rigor.REFUTED)]
        cp = BranchCheckpoint(
            branch_id=branch_id,
            objective=b.objective,
            established_claims=established,
            open_goals=last.open_goals if last else [],
            active_assumptions=last.active_assumptions if last else [],
            obstructions=last.obstructions if last else [],
            failed_strategies=self.failed_strategies(branch_id),
            next_actions=last.next_actions if last else [],
            artifact_ids=last.artifact_ids if last else [],
            notes=f"AUTO checkpoint ({reason}). Open goals/next actions copied from the last manual "
                  f"checkpoint at {last.created_at if last else 'never'}; verify they are not stale.",
            auto=True,
        )
        return self.checkpoint(cp)

    # -- instrumentation -------------------------------------------------------------------
    def tool_event(self, branch_id: str, tool: str, kind: str, seconds: float, summary: str, ok: bool) -> None:
        """In-process record of a harness tool call (formal check, attack, hammer, ...) with its cost."""
        ep = self.current_epoch(branch_id)
        self.db.execute(
            "INSERT INTO tool_calls(epoch_id,branch_id,ts,seconds,tool,kind,summary,ok) VALUES(?,?,?,?,?,?,?,?)",
            (ep.id if ep else 0, branch_id, utcnow(), seconds, tool, kind, summary[:300], int(ok)),
        )

    def replace_turns(self, epoch_id: int, turns: list[dict]) -> None:
        self.db.execute("DELETE FROM turns WHERE epoch_id=?", (epoch_id,))
        self.db.executemany(
            "INSERT INTO turns(epoch_id,idx,ts,input_tokens,cache_read,cache_create,output_tokens,context_total) "
            "VALUES(?,?,?,?,?,?,?,?)",
            [(epoch_id, t["idx"], t["ts"], t["input"], t["cache_read"], t["cache_create"], t["output"], t["context"]) for t in turns],
        )

    def add_stream_tool_calls(self, epoch_id: int, branch_id: str, calls: list[dict]) -> None:
        self.db.execute("DELETE FROM tool_calls WHERE epoch_id=? AND tool LIKE 'stream:%'", (epoch_id,))
        self.db.executemany(
            "INSERT INTO tool_calls(epoch_id,branch_id,ts,seconds,tool,kind,summary,ok) VALUES(?,?,?,?,?,?,?,?)",
            [(epoch_id, branch_id, c["ts"], c["seconds"], "stream:" + c["tool"], c["kind"], c["summary"][:300], int(c["ok"])) for c in calls],
        )

    def turns(self, epoch_id: int) -> list[dict]:
        rows = self.db.execute("SELECT idx,ts,input_tokens,cache_read,cache_create,output_tokens,context_total FROM turns "
                               "WHERE epoch_id=? ORDER BY idx", (epoch_id,)).fetchall()
        return [dict(zip(("idx", "ts", "input", "cache_read", "cache_create", "output", "context"), r)) for r in rows]

    def tool_calls(self, branch_id: str) -> list[dict]:
        rows = self.db.execute("SELECT epoch_id,ts,seconds,tool,kind,summary,ok FROM tool_calls WHERE branch_id=? ORDER BY ts",
                               (branch_id,)).fetchall()
        return [dict(zip(("epoch_id", "ts", "seconds", "tool", "kind", "summary", "ok"), r)) for r in rows]

    # -- ledger ----------------------------------------------------------------------------
    def similar_proposals(self, title: str, threshold: float = 0.8) -> list[ToolboxProposal]:
        """Open ledger entries whose title is close to the given one. Twelve copies of one bug were filed across a
        campaign because an agent cannot see what is already recorded."""
        import difflib  # noqa: PLC0415

        def norm(t: str) -> str:
            return " ".join(re.sub(r"[^a-z0-9 ]", " ", t.lower()).split())

        want = norm(title)
        hits = []
        for p in self.proposals(status="open"):
            if difflib.SequenceMatcher(None, want, norm(p.title)).ratio() >= threshold:
                hits.append(p)
        return hits

    def propose(self, p: ToolboxProposal) -> ToolboxProposal:
        cur = self.db.execute(
            "INSERT INTO ledger(kind,status,ts,payload) VALUES(?,?,?,?)",
            (p.kind, p.status, p.created_at, p.model_dump_json()),
        )
        p.id = cur.lastrowid
        self.db.execute("UPDATE ledger SET payload=? WHERE id=?", (p.model_dump_json(), p.id))
        self.event(p.branch_id or "-", "ledger.proposed", p.model_dump(mode="json"))
        return p

    def proposals(self, status: str | None = None) -> list[ToolboxProposal]:
        q = "SELECT payload FROM ledger" + (" WHERE status=?" if status else "") + " ORDER BY id"
        rows = self.db.execute(q, (status,) if status else ()).fetchall()
        return [ToolboxProposal.model_validate_json(r[0]) for r in rows]

    # -- upstream contributions ------------------------------------------------------------
    def add_contribution(self, c: Contribution) -> Contribution:
        """A gap in another project, with what this run already has for it. Not a toolbox proposal: nothing
        here asks the harness to change."""
        if c.branch_id:
            self.require_branch(c.branch_id)
        cur = self.db.execute("INSERT INTO contributions(target,status,ts,payload) VALUES(?,?,?,?)",
                              (c.target, c.status, c.created_at, c.model_dump_json()))
        c.id = cur.lastrowid
        self.db.execute("UPDATE contributions SET payload=? WHERE id=?", (c.model_dump_json(), c.id))
        self.event(c.branch_id or "-", "contribution.recorded", c.model_dump(mode="json"))
        return c

    def contributions(self, target: str | None = None, status: str | None = None) -> list[Contribution]:
        where, args = [], []
        if target:
            where.append("target=?")
            args.append(target)
        if status:
            where.append("status=?")
            args.append(status)
        q = "SELECT payload FROM contributions" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id"
        return [Contribution.model_validate_json(r[0]) for r in self.db.execute(q, tuple(args)).fetchall()]

    def set_contribution_status(self, cid: int, status: str, url: str | None = None) -> Contribution:
        row = self.db.execute("SELECT payload FROM contributions WHERE id=?", (cid,)).fetchone()
        if not row:
            raise StateError(f"no contribution {cid}")
        c = Contribution.model_validate_json(row[0])
        allowed = ("candidate", "drafted", "submitted", "merged", "declined", "superseded")
        if status not in allowed:
            raise StateError(f"invalid status {status!r}; one of {allowed}")
        if status in ("submitted", "merged") and not (url or c.url):
            raise StateError(f"{status} needs the pull request or issue url (--url)")
        c.status, c.url = status, (url or c.url)  # type: ignore[assignment]
        self.db.execute("UPDATE contributions SET status=?, payload=? WHERE id=?", (status, c.model_dump_json(), cid))
        self.event(c.branch_id or "-", "contribution.status", {"id": cid, "status": status, "url": c.url})
        return c

    def append_proposal_evidence(self, pid: int, evidence: str) -> ToolboxProposal:
        row = self.db.execute("SELECT payload FROM ledger WHERE id=?", (pid,)).fetchone()
        if not row:
            raise StateError(f"no ledger entry {pid}")
        p = ToolboxProposal.model_validate_json(row[0])
        p.evidence = (p.evidence + "\n---\n" + evidence).strip("\n") if p.evidence else evidence
        self.db.execute("UPDATE ledger SET payload=? WHERE id=?", (p.model_dump_json(), pid))
        self.event(p.branch_id or "-", "ledger.evidence_appended", {"id": pid})
        return p

    def set_proposal_status(self, pid: int, status: str) -> ToolboxProposal:
        row = self.db.execute("SELECT payload FROM ledger WHERE id=?", (pid,)).fetchone()
        if not row:
            raise StateError(f"no ledger entry {pid}")
        p = ToolboxProposal.model_validate_json(row[0])
        p.status = status  # type: ignore[assignment]
        self.db.execute("UPDATE ledger SET payload=?, status=? WHERE id=?", (p.model_dump_json(), status, pid))
        return p
