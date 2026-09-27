"""Repository-anchored paths. Every store/artefact path resolves against the harness root,
never the current working directory, so hooks, subagents and the CLI agree on one state."""
from __future__ import annotations

import os
from pathlib import Path


def harness_root() -> Path:
    env = os.environ.get("QUOD_ROOT")
    if env:
        return Path(env).resolve()
    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        if (candidate / "pixi.toml").exists() and (candidate / "CLAUDE.md").exists():
            return candidate
    return here


def state_dir() -> Path:
    p = harness_root() / "state"
    p.mkdir(parents=True, exist_ok=True)
    return p


def artifacts_dir() -> Path:
    p = harness_root() / "artifacts"
    p.mkdir(parents=True, exist_ok=True)
    return p


def formal_dir() -> Path:
    return harness_root() / "formal"


def bench_dir() -> Path:
    return harness_root() / "bench" / "problems"


def state_db() -> Path:
    return state_dir() / "harness.sqlite3"


def library_db() -> Path:
    return state_dir() / "library.sqlite3"
