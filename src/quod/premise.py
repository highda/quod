"""Premise retrieval over the pinned Mathlib: an index that belongs to the pin, not to the calendar.

Measured on the pinned checkout: 185,690 theorem/lemma declarations in 8,311 files, 109 MB of source. Embedded
at 256 dimensions in float16 that is ~95 MB, which loads onto Metal in one piece, so a query is a single
matmul against the whole library.

The index is keyed by `(encoder id, Mathlib revision)`. Because Mathlib is pinned here, the index is pinned
too: it is rebuilt only when the pin is deliberately bumped, and then only for files whose contents changed.
A default encoder that needs no downloaded weights is included (signed character-n-gram hashing, which is a
strong lexical baseline for identifier-heavy text); a neural encoder can be dropped in without touching
callers, and `quod doctor` reports which one the index was built with.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import struct
import time
from dataclasses import dataclass
from pathlib import Path

from .paths import formal_dir, state_dir

DIM = 256
DECL_RE = re.compile(
    r"^(?:@\[[^\]]*\]\s*)?(?:private\s+|protected\s+|nonrec\s+|noncomputable\s+)*"
    r"(theorem|lemma)\s+([A-Za-z_][A-Za-z0-9_.'!?]*)\s*(.*?)(?::=|\bwhere\b)",
    re.S | re.M,
)
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files(module TEXT PRIMARY KEY, fhash TEXT NOT NULL, decls INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS decls(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, module TEXT NOT NULL, statement TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_decls_module ON decls(module);
CREATE INDEX IF NOT EXISTS idx_decls_name ON decls(name);
"""
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS decls_fts USING fts5(name, statement, content='');
"""


# -- encoders ---------------------------------------------------------------------------------
class HashEncoder:
    """Signed hashing of identifier tokens and character 4-grams into DIM dimensions, L2-normalised.

    No weights, no download, deterministic across machines — so an index built here is reproducible from the
    same Mathlib revision alone.
    """

    id = f"hash-ngram-v1-d{DIM}"

    @staticmethod
    def _features(text: str) -> list[str]:
        toks = re.findall(r"[A-Za-z][A-Za-z0-9_']*|[∀∃→↔≤≥≠∑∏∫√⁻¹∈∉⊆∩∪]", text)
        feats = [t.lower() for t in toks]
        feats += [f"{a}|{b}" for a, b in zip(feats, feats[1:])]           # token bigrams
        squashed = re.sub(r"\s+", " ", text)[:400]
        feats += [squashed[i:i + 4] for i in range(0, max(0, len(squashed) - 3), 2)]
        return feats

    def encode(self, texts: list[str]):
        import numpy as np  # noqa: PLC0415

        out = np.zeros((len(texts), DIM), dtype=np.float32)
        for r, text in enumerate(texts):
            for f in self._features(text):
                h = hashlib.blake2b(f.encode(), digest_size=8).digest()
                idx = int.from_bytes(h[:4], "little") % DIM
                sign = 1.0 if h[4] & 1 else -1.0
                out[r, idx] += sign
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        return out / np.maximum(norms, 1e-9)


def encoder(name: str = "hash"):
    """`hash` always works. A neural encoder is used only if its runtime and weights are actually present, so a
    missing model degrades to the lexical baseline instead of failing a run."""
    if name in ("hash", "default"):
        return HashEncoder()
    try:  # pragma: no cover - exercised only where weights exist
        from mlx_embeddings import load  # type: ignore  # noqa: PLC0415

        model, tokenizer = load(name)

        class MlxEncoder:
            id = f"mlx:{name}-d{DIM}"

            def encode(self, texts: list[str]):
                import numpy as np  # noqa: PLC0415

                vecs = [np.asarray(model.encode(t)[:DIM], dtype=np.float32) for t in texts]
                m = np.stack(vecs)
                return m / np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-9)

        return MlxEncoder()
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"encoder {name!r} unavailable ({e}); `hash` needs no weights") from e


# -- index ------------------------------------------------------------------------------------
def mathlib_root() -> Path:
    return formal_dir() / ".lake" / "packages" / "mathlib" / "Mathlib"


def mathlib_rev() -> str:
    p = formal_dir() / "lake-manifest.json"
    if not p.exists():
        return "unpinned"
    try:
        for pkg in json.loads(p.read_text()).get("packages", []):
            if pkg.get("name") == "mathlib":
                return (pkg.get("rev") or pkg.get("inputRev") or "unknown")[:12]
    except json.JSONDecodeError:
        pass
    return "unknown"


@dataclass(frozen=True)
class IndexPaths:
    db: Path
    vecs: Path

    @staticmethod
    def for_index(encoder_id: str, rev: str) -> "IndexPaths":
        d = state_dir() / "premise"
        d.mkdir(parents=True, exist_ok=True)
        key = re.sub(r"[^A-Za-z0-9._-]", "_", f"{encoder_id}@{rev}")
        return IndexPaths(d / f"{key}.sqlite3", d / f"{key}.f16")


def declarations(path: Path) -> list[tuple[str, str]]:
    """(name, statement) for each theorem/lemma in a Lean file."""
    text = path.read_text(errors="ignore")
    out = []
    for _, name, sig in DECL_RE.findall(text):
        statement = " ".join(sig.split())[:600]
        if statement:
            out.append((name, statement))
    return out


def build(encoder_name: str = "hash", limit_files: int | None = None, progress=None) -> dict:
    """Build or refresh the index. Only files whose contents changed are re-read and re-embedded."""
    root = mathlib_root()
    if not root.exists():
        return {"error": f"{root} not found: run scripts/formal-setup.sh first"}
    import numpy as np  # noqa: PLC0415

    enc = encoder(encoder_name)
    rev = mathlib_rev()
    paths = IndexPaths.for_index(enc.id, rev)
    db = sqlite3.connect(paths.db, isolation_level=None)
    db.executescript(SCHEMA)
    has_fts = True
    try:
        db.executescript(FTS_SCHEMA)
    except sqlite3.OperationalError:  # pragma: no cover - SQLite built without FTS5
        has_fts = False
    db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('encoder',?)", (enc.id,))
    db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('mathlib_rev',?)", (rev,))

    files = sorted(root.rglob("*.lean"))[:limit_files]
    known = {m: h for m, h in db.execute("SELECT module, fhash FROM files")}
    t0 = time.perf_counter()
    changed = 0
    for i, f in enumerate(files):
        module = str(f.relative_to(root.parent)).removesuffix(".lean").replace("/", ".")
        fhash = hashlib.blake2b(f.read_bytes(), digest_size=8).hexdigest()
        if known.get(module) == fhash:
            continue
        decls = declarations(f)
        db.execute("BEGIN")
        old_ids = [r[0] for r in db.execute("SELECT id FROM decls WHERE module=?", (module,))]
        db.execute("DELETE FROM decls WHERE module=?", (module,))
        if has_fts and old_ids:
            db.executemany("INSERT INTO decls_fts(decls_fts, rowid, name, statement) VALUES('delete', ?, '', '')",
                           [(i,) for i in old_ids])
        for name_, stmt_ in decls:
            cur = db.execute("INSERT INTO decls(name,module,statement) VALUES(?,?,?)", (name_, module, stmt_))
            if has_fts:
                db.execute("INSERT INTO decls_fts(rowid, name, statement) VALUES(?,?,?)",
                           (cur.lastrowid, name_, stmt_))
        db.execute("INSERT OR REPLACE INTO files(module,fhash,decls) VALUES(?,?,?)", (module, fhash, len(decls)))
        db.execute("COMMIT")
        changed += 1
        if progress and changed % 500 == 0:
            progress(f"{changed} files re-indexed ({i + 1}/{len(files)} scanned)")

    rows = db.execute("SELECT id, name, statement FROM decls ORDER BY id").fetchall()
    # Vectors are rewritten whenever anything changed: 185k x 256 float16 is ~95 MB, seconds to write.
    if changed or not paths.vecs.exists():
        with paths.vecs.open("wb") as fh:
            fh.write(struct.pack("<III", len(rows), DIM, 1))
            batch: list[str] = []
            ids: list[int] = []
            for did, name, statement in rows:
                batch.append(f"{name} : {statement}")
                ids.append(did)
                if len(batch) >= 2048:
                    fh.write(enc.encode(batch).astype(np.float16).tobytes())
                    batch, ids = [], []
            if batch:
                fh.write(enc.encode(batch).astype(np.float16).tobytes())
        db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('rows',?)", (str(len(rows)),))
    db.close()
    return {"encoder": enc.id, "mathlib_rev": rev, "files_scanned": len(files), "files_reindexed": changed,
            "declarations": len(rows), "vectors": str(paths.vecs), "megabytes": round(paths.vecs.stat().st_size / 1e6, 1),
            "seconds": round(time.perf_counter() - t0, 1)}


def status(encoder_name: str = "hash") -> dict:
    enc = encoder(encoder_name)
    rev = mathlib_rev()
    paths = IndexPaths.for_index(enc.id, rev)
    if not paths.db.exists() or not paths.vecs.exists():
        return {"built": False, "encoder": enc.id, "mathlib_rev": rev,
                "hint": "run `quod premise index` once per Mathlib revision"}
    db = sqlite3.connect(paths.db)
    rows = db.execute("SELECT COUNT(*) FROM decls").fetchone()[0]
    files = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    db.close()
    return {"built": True, "encoder": enc.id, "mathlib_rev": rev, "declarations": rows, "files": files,
            "megabytes": round(paths.vecs.stat().st_size / 1e6, 1)}


def search(query: str, k: int = 12, encoder_name: str = "hash", prefer_gpu: bool = True,
           mode: str = "auto") -> dict:
    """Retrieve candidate premises.

    `mode="lexical"` uses the FTS5 index, which is what the default (weightless) encoder can honestly support:
    measured on this corpus it ranks identifier- and symbol-heavy queries well and natural-language queries
    poorly. `mode="vector"` uses the embedded matrix and is worth it only with a neural encoder, where the
    query need not share vocabulary with the statement. `auto` picks vector for a neural encoder, else lexical.
    """
    if mode == "auto":
        mode = "lexical" if encoder_name in ("hash", "default", "lexical") else "vector"
    if mode == "lexical":
        return search_lexical(query, k, encoder_name)
    return search_vector(query, k, encoder_name, prefer_gpu)


def _fts_query(query: str) -> str:
    """FTS5 MATCH string: identifier tokens OR-ed, quoted so Lean punctuation cannot be read as syntax."""
    toks = [t for t in re.findall(r"[A-Za-z][A-Za-z0-9_']{2,}", query)]
    return " OR ".join(f'"{t}"' for t in dict.fromkeys(toks)) or f'"{query[:40]}"'


def search_lexical(query: str, k: int = 12, encoder_name: str = "hash") -> dict:
    enc_id = encoder(encoder_name).id
    rev = mathlib_rev()
    paths = IndexPaths.for_index(enc_id, rev)
    if not paths.db.exists():
        return {"error": "no index for this Mathlib revision; run `quod premise index`"}
    db = sqlite3.connect(paths.db)
    t0 = time.perf_counter()
    try:
        rows = db.execute(
            "SELECT d.name, d.module, d.statement, bm25(decls_fts) AS score FROM decls_fts "
            "JOIN decls d ON d.id = decls_fts.rowid WHERE decls_fts MATCH ? ORDER BY score LIMIT ?",
            (_fts_query(query), k)).fetchall()
    except sqlite3.OperationalError as e:
        db.close()
        return {"error": f"lexical search unavailable ({e}); rebuild the index or use --mode vector"}
    total = db.execute("SELECT COUNT(*) FROM decls").fetchone()[0]
    db.close()
    return {"query": query, "encoder": enc_id, "mathlib_rev": rev, "backend": "sqlite-fts5", "mode": "lexical",
            "searched": total, "seconds": round(time.perf_counter() - t0, 3),
            "hits": [{"rank": i, "score": round(-r[3], 3), "name": r[0], "module": r[1], "statement": r[2][:300]}
                     for i, r in enumerate(rows, 1)]}


def search_vector(query: str, k: int = 12, encoder_name: str = "hash", prefer_gpu: bool = True) -> dict:
    """Nearest declarations by embedding. One matmul against the whole library."""
    from . import gpu  # noqa: PLC0415 - optional Metal backend

    enc = encoder(encoder_name)
    rev = mathlib_rev()
    paths = IndexPaths.for_index(enc.id, rev)
    if not paths.db.exists() or not paths.vecs.exists():
        return {"error": "no index for this Mathlib revision; run `quod premise index`"}
    import numpy as np  # noqa: PLC0415

    with paths.vecs.open("rb") as fh:
        rows, dim, _ = struct.unpack("<III", fh.read(12))
        mat = np.frombuffer(fh.read(), dtype=np.float16, count=rows * dim).reshape(rows, dim)
    q = enc.encode([query])[0].astype(np.float32)
    b = gpu.backend(prefer_gpu)
    t0 = time.perf_counter()
    if b.name.startswith("mlx"):
        mx = b.mod
        scores = mx.matmul(mx.array(mat.astype(np.float32)), mx.array(q))
        order = [int(i) for i in mx.argsort(-scores)[:k].tolist()]
        vals = [float(scores[i]) for i in order]
    else:
        scores = mat.astype(np.float32) @ q
        order = list(np.argsort(-scores)[:k])
        vals = [float(scores[i]) for i in order]
    db = sqlite3.connect(paths.db)
    hits = []
    for rank, (i, sc) in enumerate(zip(order, vals), 1):
        row = db.execute("SELECT name, module, statement FROM decls WHERE id=?", (int(i) + 1,)).fetchone()
        if row:
            hits.append({"rank": rank, "score": round(sc, 4), "name": row[0], "module": row[1],
                         "statement": row[2][:300]})
    db.close()
    return {"query": query, "encoder": enc.id, "mathlib_rev": rev, "backend": b.name,
            "searched": rows, "seconds": round(time.perf_counter() - t0, 3), "hits": hits}
