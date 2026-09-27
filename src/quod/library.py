"""Cross-problem library of verified results. Persists across branches and problems; this is where
the harness compounds. Only claims at rigor >= symbolically_verified/solver_verified are accepted."""
from __future__ import annotations

import re

import json
import sqlite3
from pathlib import Path

from .models import RIGOR_RANK, Claim, LibraryEntry, Rigor
from .paths import library_db

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries(
  id TEXT PRIMARY KEY, rigor TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
"""
FTS = "CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(id, statement, tags, notes)"

MIN_RANK = RIGOR_RANK[Rigor.SYMBOLICALLY_VERIFIED]


class LibraryError(RuntimeError):
    pass


class Library:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else library_db()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.executescript(SCHEMA)
        try:
            self.db.execute(FTS)
            self.fts = True
        except sqlite3.OperationalError:
            self.fts = False

    def close(self) -> None:
        self.db.close()

    def add(self, entry: LibraryEntry) -> LibraryEntry:
        if entry.rigor == Rigor.REFUTED or RIGOR_RANK[entry.rigor] < MIN_RANK:
            raise LibraryError(f"library accepts symbolically/solver verified or formally proved results, not {entry.rigor}")
        if self.get(entry.id):
            raise LibraryError(f"library entry {entry.id} exists")
        self.db.execute("INSERT INTO entries(id,rigor,payload,created_at) VALUES(?,?,?,?)",
                        (entry.id, entry.rigor, entry.model_dump_json(), entry.created_at))
        if self.fts:
            self.db.execute("INSERT INTO entries_fts(id,statement,tags,notes) VALUES(?,?,?,?)",
                            (entry.id, entry.statement, " ".join(entry.tags), entry.notes))
        return entry

    def from_claim(self, claim: Claim, entry_id: str | None = None, lean_path: str | None = None,
                   problem: str | None = None) -> LibraryEntry:
        lean = lean_path or next((e.artifact for e in claim.evidence if e.kind == "lean" and e.artifact), None)
        statement = claim.statement
        if problem and re.match(r"^Target statement from \w+", statement.strip()):
            # An imported target carries no mathematics in its statement, so a search on the subject matter
            # missed it entirely. Index the problem text with it.
            statement = f"{statement} — {' '.join(problem.split())[:600]}"
        return self.add(LibraryEntry(
            id=entry_id or f"L-{claim.id}", statement=statement, formal=claim.formal, lean_path=lean,
            rigor=claim.rigor, tags=claim.tags, source_branch=claim.branch_id, source_claim=claim.id,
        ))

    def get(self, entry_id: str) -> LibraryEntry | None:
        row = self.db.execute("SELECT payload FROM entries WHERE id=?", (entry_id,)).fetchone()
        return LibraryEntry.model_validate_json(row[0]) if row else None

    def search(self, query: str, limit: int = 20) -> list[LibraryEntry]:
        if self.fts and query.strip():
            try:
                rows = self.db.execute(
                    "SELECT e.payload FROM entries_fts f JOIN entries e ON e.id=f.id WHERE entries_fts MATCH ? LIMIT ?",
                    (query, limit)).fetchall()
                return [LibraryEntry.model_validate_json(r[0]) for r in rows]
            except sqlite3.OperationalError:
                pass
        like = f"%{query}%"
        rows = self.db.execute("SELECT payload FROM entries WHERE payload LIKE ? ORDER BY created_at DESC LIMIT ?",
                               (like, limit)).fetchall()
        return [LibraryEntry.model_validate_json(r[0]) for r in rows]

    def all(self) -> list[LibraryEntry]:
        rows = self.db.execute("SELECT payload FROM entries ORDER BY created_at").fetchall()
        return [LibraryEntry.model_validate_json(r[0]) for r in rows]

    def export_markdown(self) -> str:
        lines = ["# Library", "", "Verified results reusable across problems. Rigor is the floor, not a claim of novelty.", ""]
        for e in self.all():
            lines.append(f"## {e.id} — `{e.rigor}`")
            lines.append(e.statement)
            if e.formal:
                lines.append("")
                lines.append("```json")
                lines.append(json.dumps(e.formal.model_dump(exclude_none=True), indent=2))
                lines.append("```")
            if e.lean_path:
                lines.append(f"Lean: `{e.lean_path}`")
            if e.tags:
                lines.append("Tags: " + ", ".join(e.tags))
            if e.source_branch:
                lines.append(f"Source: branch `{e.source_branch}`, claim `{e.source_claim}`")
            lines.append("")
        return "\n".join(lines)
