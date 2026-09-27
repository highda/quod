"""Pull problems from the community into branches.

Sources:
  oeis    — sequences whose entries carry "Conjecture:" lines (unproved formulas/recurrences)
  putnam  — PutnamBench: Putnam problems formalized in Lean 4 (trishullab/PutnamBench)
  fc      — google-deepmind/formal-conjectures: open conjectures stated in Lean, by category

Fetching is an overseer action, not something a research epoch does; results are cached under state/sources/.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from .formal import signatures
from .models import Branch, Claim, FormalClaim
from .paths import state_dir
from .state import StateStore

UA = {"User-Agent": "quod-harness/0.2 (+https://github.com/highda/quod)"}
PUTNAM_RAW = "https://raw.githubusercontent.com/trishullab/PutnamBench/main/lean4/src/{id}.lean"
PUTNAM_LIST = "https://api.github.com/repos/trishullab/PutnamBench/contents/lean4/src"
FC_LIST = "https://api.github.com/repos/google-deepmind/formal-conjectures/contents/FormalConjectures/{cat}"
FC_RAW = "https://raw.githubusercontent.com/google-deepmind/formal-conjectures/main/FormalConjectures/{path}"
OEIS_SEARCH = "https://oeis.org/search?q={q}&fmt=json&start={start}"
OEIS_ENTRY = "https://oeis.org/{id}?fmt=json"


def _cache_dir() -> Path:
    d = state_dir() / "sources"
    d.mkdir(parents=True, exist_ok=True)
    return d


def fetch(url: str, ttl_ok: bool = True) -> str:
    key = re.sub(r"[^A-Za-z0-9]+", "_", url)[-150:]
    p = _cache_dir() / key
    if ttl_ok and p.exists():
        return p.read_text()
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - fixed hosts above
        text = r.read().decode()
    p.write_text(text)
    return text


@dataclass
class Problem:
    source: str
    id: str
    title: str
    statement: str            # prose for the branch
    lean: str | None          # formal statement (with sorry) when the source provides one
    url: str
    extra: dict


# -- OEIS ------------------------------------------------------------------------------------
CONJ_RE = re.compile(r"\b[Cc]onjectur")


def oeis_conjecture_lines(entry: dict) -> list[str]:
    lines = [*entry.get("formula", []), *entry.get("comment", [])]
    return [ln for ln in lines if CONJ_RE.search(ln)]


def oeis_search(query: str, start: int = 0) -> list[dict]:
    raw = fetch(OEIS_SEARCH.format(q=urllib.parse.quote(query), start=start))
    data = json.loads(raw)
    if not data:
        return []
    out = []
    for e in data:
        conj = oeis_conjecture_lines(e)
        out.append({"id": "A%06d" % e["number"], "name": e["name"], "keywords": e.get("keyword", ""),
                    "conjectures": conj[:3], "n_conjecture_lines": len(conj)})
    return out


def oeis_problem(aid: str) -> Problem:
    aid = aid.upper()
    if not re.fullmatch(r"A\d{6}", aid):
        raise ValueError("OEIS id must look like A000045")
    e = json.loads(fetch(OEIS_ENTRY.format(id=aid)))
    if isinstance(e, list):
        e = e[0]
    conj = oeis_conjecture_lines(e)
    data = e.get("data", "")
    terms = ",".join(data.split(",")[:20])
    stmt = (f"OEIS {aid}: {e['name']}\nFirst terms: {terms}\n"
            + ("Conjecture lines from the entry (unproved):\n" + "\n".join(f"  - {c}" for c in conj) if conj
               else "The entry carries no explicit 'Conjecture:' line; check formulas marked as empirical."))
    return Problem("oeis", aid, e["name"], stmt, None, f"https://oeis.org/{aid}",
                   {"keywords": e.get("keyword", ""), "formula": e.get("formula", [])[:10], "conjectures": conj,
                    "author": e.get("author", ""), "data": data})


# -- PutnamBench -----------------------------------------------------------------------------
def putnam_list() -> list[str]:
    data = json.loads(fetch(PUTNAM_LIST))
    return sorted(x["name"][:-5] for x in data if x["name"].endswith(".lean"))


def parse_lean_problem(text: str) -> tuple[str, str]:
    """Return (docstring prose, statement) from a Lean file whose theorem ends in `sorry`."""
    doc = ""
    m = re.search(r"/--(.*?)-/", text, re.S)
    if m:
        doc = " ".join(m.group(1).split())
    m2 = re.search(r"^(theorem|lemma)\b.*?:=\s*(by\s+)?sorry", text, re.S | re.M)
    stmt = m2.group(0).strip() if m2 else ""
    return doc, stmt


def putnam_problem(pid: str) -> Problem:
    pid = pid.lower()
    if not re.fullmatch(r"putnam_\d{4}_[ab]\d", pid):
        raise ValueError("PutnamBench id must look like putnam_1962_a1")
    text = fetch(PUTNAM_RAW.format(id=pid))
    doc, stmt = parse_lean_problem(text)
    # Keep everything after the imports: `open` lines and, for "find the answer" problems, the
    # `noncomputable abbrev putnam_*_solution := sorry` the theorem refers to. The harness wrapper adds `import Mathlib`.
    body = re.sub(r"^import .*\n", "", text, flags=re.M)
    # PutnamBench writes the reference answer as a `-- ...` comment under the solution abbrev. Strip all
    # line comments (docstrings `/-- -/` are kept: they carry the problem text) so the run stays blind.
    body = re.sub(r"^[ \t]*--.*\n?", "", body, flags=re.M)
    body = re.sub(r"[ \t]+--[^\n]*", "", body).strip()
    lean = body if stmt else None
    return Problem("putnam", pid, pid, doc or "(no docstring)", lean,
                   f"https://github.com/trishullab/PutnamBench/blob/main/lean4/src/{pid}.lean",
                   {"year": pid.split("_")[1], "problem": pid.split("_")[2].upper()})


# -- formal-conjectures ----------------------------------------------------------------------
def fc_list(category: str) -> list[str]:
    data = json.loads(fetch(FC_LIST.format(cat=category)))
    return sorted(x["name"] for x in data)


def fc_module(path: str) -> str:
    """FormalConjectures/ErdosProblems/1.lean -> FormalConjectures.ErdosProblems.«1»"""
    parts = path[:-5].split("/") if path.endswith(".lean") else path.split("/")
    return "FormalConjectures." + ".".join(f"«{x}»" if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", x) else x for x in parts)


def fc_problem(path: str) -> Problem:
    """formal-conjectures is a Lake dependency of formal/, so the target *imports* the module and restates the
    conjecture(s) with `sorry`; their definitions resolve natively."""
    text = fetch(FC_RAW.format(path=path))
    m = re.search(r"/-!(.*?)-/", text, re.S)
    doc = " ".join(m.group(1).split()) if m else ""
    stmts = re.findall(r"^(?:@\[[^\]]*\]\s*)?(?:theorem|lemma)\b.*?:=\s*(?:by\s+)?sorry", text, re.S | re.M)
    namespaces = re.findall(r"^namespace\s+([^\s]+)", text, re.M)
    opens = re.findall(r"^open .*$", text, re.M)
    lean = None
    if stmts:
        restated = []
        for st in stmts:
            st = re.sub(r"^@\[[^\]]*\]\s*", "", st.strip())
            st = re.sub(r"^(theorem|lemma)\s+([^\s:({]+)", lambda mm: f"{mm.group(1)} {mm.group(2).split('.')[-1]}_quod", st, count=1)
            restated.append(st)
        header = [f"import {fc_module(path)}", "", *opens, *(f"open {ns}" for ns in namespaces), ""]
        lean = "\n".join(header + ["\n\n".join(restated)])
    return Problem("fc", path, path, doc or "(no module docstring)", lean,
                   f"https://github.com/google-deepmind/formal-conjectures/blob/main/FormalConjectures/{path}",
                   {"n_statements": len(stmts), "module": fc_module(path), "namespaces": namespaces})


# -- open as a branch ------------------------------------------------------------------------
def load(source: str, ident: str) -> Problem:
    if source == "oeis":
        return oeis_problem(ident)
    if source == "putnam":
        return putnam_problem(ident)
    if source == "fc":
        return fc_problem(ident)
    raise ValueError(f"unknown source {source!r}; use oeis | putnam | fc")


def open_branch(store: StateStore, p: Problem, branch_id: str | None = None) -> Branch:
    bid = branch_id or f"{p.source}-{re.sub(r'[^A-Za-z0-9]+', '-', p.id).strip('-').lower()}"
    if p.source == "oeis":
        objective = ("Prove or refute the conjectured formula/recurrence: formalize it as a claim, attack it, and "
                     "reach a Lean-kernel proof or an explicit counterexample term. A proof is a contribution to OEIS.")
    elif p.source == "putnam":
        objective = "Prove the Lean statement below with the pinned Mathlib (kernel-accepted, no sorry)."
    else:
        objective = ("Advance the conjecture. The whole statement is open; the deliverables are kernel-proved "
                     "fragments and named obstructions: the strongest special case, the finite verification, the "
                     "weaker bound, any `textbook`/`solved` variant in the same module that Mathlib can reach, and "
                     "for every route you cannot close, `quod obstruction add` with the concrete thing that would "
                     "unblock it. Do not close the branch for difficulty.")
    problem = p.statement + (f"\n\nLean target:\n{p.lean}" if p.lean else "") + f"\n\nSource: {p.url}"
    tags = [f"source:{p.source}", p.id]
    if p.source == "fc":
        # An open conjecture is a programme, not a one-epoch target: it is past the difficulty gate from the
        # start, and `quod trace` scores it by proved lemmas and cleared obstructions.
        tags += ["open-problem", "programme"]
    b = store.create_branch(Branch(id=bid, problem=problem, objective=objective, tags=tags))
    if p.lean:
        # A statement carrying `answer(sorry)` has an undetermined answer: finding it is part of the problem,
        # so that signature is not frozen (the agent restates it with the determined answer and supersedes).
        frozen = [sig for sig in signatures(p.lean).values() if "answer(" not in sig]
        store.add_claim(Claim(id=f"{bid}-target", branch_id=bid, statement=f"Target statement from {p.source} {p.id}.",
                              formal=FormalClaim(lean=p.lean, frozen=frozen), tags=["target"]))
    store.event(bid, "source.imported", asdict(p))
    return b
