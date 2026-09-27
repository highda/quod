"""What produced a number, so a stale report cannot be mistaken for a current one.

Every generated report carries this header. Measurements are only comparable within one row of it: a different
Mathlib, a different model or a different harness version is a different instrument, and its numbers do not
belong in the same table as today's.
"""
from __future__ import annotations

import json
import platform
import sys
from datetime import datetime, timezone

from . import __version__
from .paths import formal_dir, harness_root


def _lean_toolchain() -> str | None:
    p = formal_dir() / "lean-toolchain"
    return p.read_text().strip() if p.exists() else None


def _mathlib_rev() -> str | None:
    p = formal_dir() / "lake-manifest.json"
    if not p.exists():
        return None
    try:
        manifest = json.loads(p.read_text())
    except json.JSONDecodeError:
        return None
    for pkg in manifest.get("packages", []):
        if pkg.get("name") == "mathlib":
            return pkg.get("inputRev") or (pkg.get("rev") or "")[:12] or None
    return None


def describe(models: dict[str, str] | None = None) -> dict:
    """Machine-readable provenance. `models` comes from the caller because a report may cover runs whose
    configuration differed from the one resolved right now."""
    if models is None:
        try:
            from . import config as config_mod  # noqa: PLC0415 - optional, and must not break a report

            cfg = config_mod.resolve()
            models = {"proposer": cfg.proposer, "adversary": cfg.adversary, "formalizer": cfg.formalizer}
        except Exception:  # noqa: BLE001
            models = {}
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "quod": __version__,
        "python": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.machine()}",
        "lean_toolchain": _lean_toolchain(),
        "mathlib": _mathlib_rev(),
        "models": models,
        "root": str(harness_root()),
    }


def header_markdown(what: str, models: dict[str, str] | None = None, extra: str = "") -> str:
    """One provenance block for the top of a generated report, plus the staleness caution."""
    d = describe(models)
    m = d["models"] or {}
    model_line = ", ".join(f"{k} {v}" for k, v in m.items() if v) or "not recorded"
    lean = d["lean_toolchain"] or "not installed"
    mathlib = d["mathlib"] or "unpinned"
    return "\n".join([
        f"# {what}",
        "",
        f"**Generated** {d['generated_at']} · **quod** {d['quod']} · **models** {model_line}",
        f"**Toolchain** {lean}, Mathlib {mathlib} · Python {d['python']} · {d['platform']}",
        "",
        "> These figures describe one configuration of the harness at one date. A different Mathlib, model or "
        "harness version is a different instrument: do not compare across headers, and regenerate before "
        "quoting. Older numbers in this file's history are kept for trend, not for comparison."
        + (f" {extra}" if extra else ""),
        "",
    ])
