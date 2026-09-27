"""Lean 4 bridge. The kernel is the only thing allowed to say `formally_proved`."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .paths import formal_dir

SCRATCH = "Quod/Scratch"
CACHE = "Quod/Scratch/.cache"

# Measured on the reference machine: `import Mathlib` costs ~3m45s of mostly-idle wall time (olean loading),
# a narrow import ~21s. Narrowing is therefore worth an occasional retry: a snippet is compiled against the
# smallest prelude whose modules cover the identifiers it mentions, and falls back to full Mathlib if that
# fails for a reason that looks like a missing import.
NARROW_HINTS: list[tuple[str, str]] = [
    (r"\b(norm_num|decide|omega|simp|rfl|trivial|linarith|nlinarith|positivity|ring|field_simp|gcongr)\b", "Mathlib.Tactic"),
    (r"\bFinset\b|\b∑\b|\b∏\b", "Mathlib.Algebra.BigOperators.Basic"),
    (r"\bNat\.|\bZMod\b|\bPrime\b|\bdvd\b|\b∣\b", "Mathlib.NumberTheory.Divisors"),
    (r"\bReal\.|\bℝ\b|\bContinuous\b|\bderiv\b|\bTendsto\b", "Mathlib.Analysis.SpecialFunctions.Pow.Real"),
    (r"\bComplex\.|\bℂ\b", "Mathlib.Analysis.SpecialFunctions.Complex.Log"),
    (r"\bMatrix\b|\bLinearMap\b|\bBasis\b", "Mathlib.LinearAlgebra.Matrix.Determinant.Basic"),
    (r"\bPolynomial\b", "Mathlib.Algebra.Polynomial.Basic"),
    (r"\bMeasure\b|\bμH\b|\bintegral\b|\b∫\b", "Mathlib.MeasureTheory.Integral.Bochner"),
    (r"\bEquiv\.Perm\b|\bFintype\b|\bcard\b", "Mathlib.Data.Fintype.Card"),
    (r"\bTopologicalSpace\b|\bIsOpen\b|\bcompact\b", "Mathlib.Topology.Basic"),
]
MISSING_IMPORT_RE = re.compile(r"unknown (identifier|constant|namespace)|unknown tactic|has not been imported", re.I)

# Anything that lets a "proof" bypass the kernel or smuggle in assumptions.
FORBIDDEN = re.compile(r"\b(axiom|native_decide|sorryAx|implemented_by|extern|unsafe|opaque\s+\w+\s*:[^=]*:=\s*sorry)\b")
ALLOWED_AXIOMS = {"propext", "Classical.choice", "Quot.sound"}
DECL_RE = re.compile(r"^\s*(?:@\[[^\]]*\]\s*)?(?:noncomputable\s+)?(theorem|lemma|abbrev|def)\s+([^\s:({]+)(.*?)(?::=|\bwhere\b)", re.S | re.M)


def qualified_theorem_names(src: str) -> list[str]:
    """Fully qualified names of theorem/lemma declarations, tracking `namespace X ... end X` blocks
    (the harness wrapper's own Quod.Scratch namespace is excluded, since #print axioms runs inside it)."""
    names, stack = [], []
    for line in src.splitlines():
        m = re.match(r"^\s*namespace\s+([^\s]+)", line)
        if m:
            if m.group(1) != "Quod.Scratch":
                stack.append(m.group(1))
            continue
        m = re.match(r"^\s*end\s+([^\s]+)\s*$", line)
        if m and stack and stack[-1] == m.group(1):
            stack.pop()
            continue
        m = re.match(r"^\s*(?:@\[[^\]]*\]\s*)?(?:private\s+|protected\s+)?(?:noncomputable\s+)?(?:theorem|lemma)\s+([^\s:({]+)", line)
        if m:
            names.append(".".join([*stack, m.group(1)]))
    return names


def signatures(src: str) -> dict[str, str]:
    """Declaration name -> whitespace-normalized statement/header (everything before `:=`).
    Used to freeze imported theorem statements so a proof cannot quietly change what is proved."""
    out = {}
    for kind, name, sig in DECL_RE.findall(src):
        out[name] = f"{kind} {name} " + " ".join(sig.split())
    return out


def frozen_violations(frozen: list[str], src: str) -> list[str]:
    """Which frozen signatures are missing or altered in `src`."""
    now = signatures(src)
    bad = []
    for f in frozen:
        m = re.match(r"(theorem|lemma|abbrev|def)\s+(\S+)", f)
        if not m:
            continue
        name = m.group(2)
        if name not in now:
            bad.append(f"declaration {name} missing")
        elif now[name] != f:
            bad.append(f"statement of {name} changed:\n  frozen: {f}\n  now:    {now[name]}")
    return bad


@dataclass
class LeanReport:
    verdict: str  # proved | compiled | failed | statement_ok | statement_error | unavailable
    details: str
    path: str | None = None


def lean_available() -> bool:
    return shutil.which("lake") is not None and (formal_dir() / "lakefile.toml").exists()


def narrow_imports(body: str) -> list[str]:
    """The smallest import list that plausibly covers a snippet. Always includes Mathlib.Tactic, because
    every proof uses tactics and it is the cheapest broad module."""
    mods = {"Mathlib.Tactic"}
    for pat, mod in NARROW_HINTS:
        if re.search(pat, body):
            mods.add(mod)
    return sorted(mods)


def _cache_path(src: str) -> Path:
    key = hashlib.sha256((_toolchain_key() + "\0" + src).encode()).hexdigest()[:32]
    return formal_dir() / CACHE / f"{key}.json"


def _toolchain_key() -> str:
    """Compiler identity: a cached verdict is only valid for the toolchain that produced it."""
    parts = []
    for name in ("lean-toolchain", "lake-manifest.json"):
        p = formal_dir() / name
        parts.append(hashlib.sha256(p.read_bytes()).hexdigest()[:16] if p.exists() else "-")
    return "|".join(parts)


def cached_verdict(src: str) -> LeanReport | None:
    p = _cache_path(src)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return None
    return LeanReport(d["verdict"], d["details"], d.get("path"))


def store_verdict(src: str, rep: LeanReport) -> None:
    """Only settled verdicts are cached: a timeout or an unavailable toolchain says nothing about the source."""
    if rep.verdict in ("unavailable",) or "timed out" in rep.details:
        return
    p = _cache_path(src)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"verdict": rep.verdict, "details": rep.details, "path": rep.path}))


def _wrap(statement: str, name_hint: str) -> str:
    body = statement.strip()
    if re.match(r"^import\s+Mathlib\b", body):
        return body  # a complete file the caller assembled itself
    head = ""
    if body.startswith("import"):
        # a stored form whose non-Mathlib imports were kept: keep them above the namespace
        lines = body.splitlines()
        n = 0
        while n < len(lines) and (lines[n].startswith("import") or not lines[n].strip()):
            n += 1
        head, body = "\n".join(lines[:n]).strip(), "\n".join(lines[n:]).strip()
    return ("import Mathlib\n" + (head + "\n" if head else "") + "\nnamespace Quod.Scratch\n\n"
            + body + "\n\nend Quod.Scratch\n")


def _errors_first(out: str, limit: int = 6000) -> str:
    """Lean diagnostics with the *first* errors kept: error blocks in file order, then warnings if room remains."""
    blocks, cur = [], []
    for line in out.splitlines():
        if re.match(r"^\S+\.lean:\d+:\d+: (error|warning|info)", line):
            if cur:
                blocks.append("\n".join(cur))
            cur = [line]
        elif cur:
            cur.append(line)
    if cur:
        blocks.append("\n".join(cur))
    errors = [b for b in blocks if ": error" in b.splitlines()[0]]
    others = [b for b in blocks if b not in errors]
    text = "\n\n".join(errors + others) if blocks else out
    return text[:limit] + (f"\n… [{len(text) - limit} more chars; use --raw]" if len(text) > limit else "")


def _extra_modules(src: str) -> list[str]:
    """Imports outside Mathlib (e.g. FormalConjectures.ErdosProblems.«1») that must be built on demand."""
    mods = re.findall(r"^import\s+([^\s]+)", src, re.M)
    return [m for m in mods if not m.startswith("Mathlib")]


def build_modules(mods: list[str], timeout: int = 1800) -> str | None:
    """`lake build` the given modules; return an error string on failure, else None."""
    if not mods:
        return None
    try:
        proc = subprocess.run(["lake", "build", *mods], cwd=formal_dir(), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"lake build {' '.join(mods)} timed out after {timeout}s"
    if proc.returncode != 0:
        return (proc.stdout + proc.stderr)[-3000:]
    return None


def default_timeout() -> int:
    """Kernel-check budget in seconds: [formal] timeout in quod.toml, QUOD_LEAN_TIMEOUT, else 900.
    Large `decide`-style certificates need far more than a fixed 600s."""
    from . import config as config_mod  # noqa: PLC0415 - avoid an import cycle at module load
    try:
        return config_mod.resolve().lean_timeout
    except Exception:  # noqa: BLE001 - a broken quod.toml must not make the kernel unreachable
        return 900


def check(statement: str, name_hint: str = "claim", timeout: int | None = None, narrow: bool = True,
          imports: list[str] | None = None, use_cache: bool = True) -> LeanReport:
    """Compile a snippet with the pinned toolchain; `sorry` is never a proof.

    Two costs are avoided here rather than paid every call:

    * **imports** — `import Mathlib` loads the whole library (measured ~3m45s of mostly-idle wall time against
      ~21s for a narrow import). A statement that does not already carry its own imports is first compiled
      against the smallest prelude that plausibly covers it, and only retried against full Mathlib if that
      fails for a reason that looks like a missing import.
    * **repetition** — a settled verdict is cached under `formal/Quod/Scratch/.cache`, keyed by the exact
      source and the toolchain identity, so re-checking the same text (which `attach` does by design) is free.
    """
    if imports is not None:
        narrow = False
    body = statement.strip()
    if not re.match(r"^import\b", body) and (narrow or imports is not None):
        mods = imports if imports is not None else narrow_imports(body)
        narrowed = "\n".join(f"import {m}" for m in mods) + "\n\nnamespace Quod.Scratch\n\n" + body + "\n\nend Quod.Scratch\n"
        rep = _check_source(narrowed, name_hint, timeout, use_cache)
        if rep.verdict not in ("failed", "statement_error") or not MISSING_IMPORT_RE.search(rep.details):
            return rep
        # the narrow prelude was not enough; pay for the full library once
    return _check_source(_wrap(statement, name_hint), name_hint, timeout, use_cache)


def _check_source(src_in: str, name_hint: str = "claim", timeout: int | None = None,
                  use_cache: bool = True) -> LeanReport:
    """Compile exactly this source (already wrapped). The single place that runs the compiler."""
    if not lean_available():
        return LeanReport("unavailable", "lake not on PATH or formal/lakefile.toml missing; run scripts/formal-setup.sh")
    timeout = default_timeout() if timeout is None else timeout
    src = src_in
    has_sorry = re.search(r"\bsorry\b", src) is not None
    forbidden = sorted({m.group(1) for m in FORBIDDEN.finditer(src)})
    if forbidden:
        return LeanReport("failed", f"forbidden constructs (bypass the kernel or add assumptions): {forbidden}")
    decls = qualified_theorem_names(src)
    if decls and not has_sorry:
        prints = "\n".join(f"#print axioms {n}" for n in decls)
        if "\nend Quod.Scratch" in src:
            src = src.replace("\nend Quod.Scratch", f"\n{prints}\n\nend Quod.Scratch", 1)
        else:
            src = src.rstrip() + "\n\n" + prints + "\n"
    digest = hashlib.sha256(src.encode()).hexdigest()[:12]
    safe = re.sub(r"[^A-Za-z0-9_]", "_", name_hint)[:40]
    path = formal_dir() / SCRATCH / f"{safe}_{digest}.lean"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(src)
    if use_cache and (hit := cached_verdict(src)) is not None:
        return LeanReport(hit.verdict, hit.details, str(path))
    build_err = build_modules(_extra_modules(src))
    if build_err:
        return LeanReport("failed", f"dependency build failed: {build_err}", str(path))
    try:
        proc = subprocess.run(
            ["lake", "env", "lean", str(path)], cwd=formal_dir(), capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return LeanReport("failed", f"lean timed out after {timeout}s", str(path))
    out = (proc.stdout + proc.stderr).strip()
    ok = proc.returncode == 0 and "error" not in out.lower()

    def settle(rep: LeanReport) -> LeanReport:
        if use_cache:
            store_verdict(src, rep)
        return rep

    if has_sorry:
        # Compiling a statement with `sorry` checks that the *statement* is well-formed on the pinned Mathlib.
        return settle(LeanReport("statement_ok" if ok else "statement_error",
                                 ("statement compiles; `sorry` means it is not a proof" if ok else _errors_first(out)), str(path)))
    if ok:
        if not decls:
            return settle(LeanReport("compiled", "file compiles but declares no theorem/lemma; nothing was proved", str(path)))
        used = set()
        for m in re.finditer(r"depends on axioms: \[([^\]]*)\]", out):
            used |= {a.strip() for a in m.group(1).split(",") if a.strip()}
        extra = sorted(used - ALLOWED_AXIOMS)
        if extra:
            return settle(LeanReport("failed", f"proof depends on non-standard axioms {extra} (#print axioms); allowed: {sorted(ALLOWED_AXIOMS)}", str(path)))
        return settle(LeanReport("proved", f"kernel accepted {', '.join(decls)}; axioms ⊆ {sorted(ALLOWED_AXIOMS)}", str(path)))
    return settle(LeanReport("failed", _errors_first(out), str(path)))


def _normalize(src: str) -> str:
    """Compare sources up to layout and the #print axioms block the checker appends itself."""
    src = re.sub(r"^#print axioms .*$", "", src, flags=re.M)
    return "\n".join(line.rstrip() for line in src.split("\n") if line.strip())


def same_source(compiled: str, stored: str, name_hint: str = "claim") -> bool:
    """True when re-wrapping the stored form would hand the compiler the text it just accepted, so the
    stored-form re-check cannot say anything new."""
    return _normalize(compiled) == _normalize(_wrap(stored, name_hint))


def check_file(path: str | Path, timeout: int | None = None) -> LeanReport:
    p = Path(path)
    if not p.exists():
        return LeanReport("failed", f"{p} does not exist")
    return check(p.read_text(), p.stem, timeout=timeout)


def unwrap(source: str) -> str:
    """Strip the scratch wrapper (the `import Mathlib` line and the namespace) so the claim stores a
    self-contained statement. Anything between the imports and the namespace (`open ...`, `set_option ...`,
    `noncomputable section`) is kept: it is part of what the kernel checked. Imports other than Mathlib are
    kept too — a proof over an imported conjecture module does not recompile without them."""
    keep = [ln for ln in re.findall(r"^import .*$", source, flags=re.M) if ln.strip() != "import Mathlib"]
    body = re.sub(r"^import .*\n", "", source, flags=re.M)
    if "namespace Quod.Scratch" in body:
        head, tail = body.split("namespace Quod.Scratch", 1)
        if "end Quod.Scratch" in tail:
            tail = tail.split("end Quod.Scratch", 1)[0]
        head = head.strip()
        body = (head + "\n\n" if head else "") + tail.strip()
    body = body.strip()
    return ("\n".join(keep) + "\n\n" + body) if keep else body


def search(query: str, limit: int = 10) -> dict:
    """Premise retrieval via Loogle (https://loogle.lean-lang.org). Accepts Loogle syntax:
    a constant name, `_ * _ ≤ _`, `Finset.sum, "range"`, `|- ∀ n, ?a ≤ ?b + n` etc."""
    import json  # noqa: PLC0415
    import urllib.parse  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    url = "https://loogle.lean-lang.org/json?q=" + urllib.parse.quote(query)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "quod-harness/0.2"}), timeout=30) as r:  # noqa: S310
            data = json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        return {"query": query, "error": str(e), "hint": "offline? use `exact?`/`apply?` inside Lean instead"}
    hits = [{"name": h.get("name"), "type": h.get("type"), "module": h.get("module")} for h in data.get("hits", [])[:limit]]
    return {"query": query, "count": data.get("count"), "hits": hits, "suggestions": data.get("suggestions", [])[:5],
            "error": data.get("error")}


# -- LeanHammer ------------------------------------------------------------------------------
TRY_THIS_RE = re.compile(r"Try (?:this|these):\s*\n((?:[ \t]+.*\n?)+)")


def hammer_dylib() -> Path | None:
    p = formal_dir() / ".lake" / "packages" / "cvc5" / ".lake" / "build" / "lib" / "libcvc5_cvc5.dylib"
    return p if p.exists() else None


def hammer(statement: str, name_hint: str = "hammer", timeout: int = 600) -> dict:
    """Run LeanHammer on a statement whose proof is `by hammer` (or contains `hammer` calls) and return the
    tactic scripts it suggests. Sledgehammer workflow: the suggestion replaces the hammer call, so the final
    proof needs neither the Hammer import nor its solvers. Requires `lake build Hammer` (scripts/formal-setup.sh)."""
    if not lean_available():
        return {"verdict": "unavailable", "details": "lake not available"}
    dylib = hammer_dylib()
    if dylib is None:
        return {"verdict": "unavailable", "details": "LeanHammer not built: run `lake build Hammer` in formal/ (see scripts/formal-setup.sh)"}
    body = statement.strip()
    if "import Hammer" not in body:
        body = "import Mathlib\nimport Hammer\n\n" + re.sub(r"^import .*\n", "", body, flags=re.M)
    if "hammer" not in body:
        return {"verdict": "unsupported", "details": "no `hammer` call in the statement; write the goal's proof as `by hammer`"}
    digest = hashlib.sha256(body.encode()).hexdigest()[:12]
    safe = re.sub(r"[^A-Za-z0-9_]", "_", name_hint)[:40]
    path = formal_dir() / SCRATCH / f"{safe}_{digest}.lean"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    try:
        proc = subprocess.run(["lake", "env", "lean", f"--load-dynlib={dylib}", str(path)], cwd=formal_dir(),
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"verdict": "timeout", "details": f"hammer exceeded {timeout}s", "path": str(path)}
    out = proc.stdout + proc.stderr
    suggestions = []
    for block in TRY_THIS_RE.findall(out):
        for line in block.splitlines():
            line = re.sub(r"^\s*\[\w+\]\s*", "", line).strip()
            if line:
                suggestions.append(line)
    if suggestions:
        return {"verdict": "suggested", "suggestions": suggestions, "path": str(path),
                "next": "replace the `hammer` call with a suggestion and run `quod formal check`"}
    return {"verdict": "no_proof", "details": out[-2500:], "path": str(path)}


DECL_BLOCK_RE = re.compile(r"^(?P<head>\s*(?:@\[[^\]]*\]\s*)?(?:private\s+|protected\s+)?(?:noncomputable\s+)?(?:theorem|lemma)\s+(?P<name>[^\s:({]+))", re.M)


def declaration_blocks(src: str) -> dict[str, str]:
    """Qualified name -> source text of each theorem/lemma (from its header to the next top-level declaration)."""
    lines = src.splitlines()
    starts = []
    stack: list[str] = []
    for i, line in enumerate(lines):
        m = re.match(r"^\s*namespace\s+([^\s]+)", line)
        if m:
            if m.group(1) != "Quod.Scratch":
                stack.append(m.group(1))
            continue
        m = re.match(r"^\s*end\s+([^\s]+)\s*$", line)
        if m and stack and stack[-1] == m.group(1):
            stack.pop()
            continue
        m = DECL_BLOCK_RE.match(line)
        if m and not line.startswith((" ", "\t")):
            starts.append((i, ".".join([*stack, m.group("name")])))
    out = {}
    for k, (i, name) in enumerate(starts):
        j = starts[k + 1][0] if k + 1 < len(starts) else len(lines)
        # trim trailing blank lines / doc comments belonging to the next declaration
        block = "\n".join(lines[i:j]).rstrip()
        out[name] = block
    return out
