"""Backend policy and capability probe."""
from __future__ import annotations

import importlib.util
import platform
import shutil
from dataclasses import dataclass


@dataclass(frozen=True)
class BackendDecision:
    backend: str
    reason: str


# Workload shapes worth moving to Metal: anything wide, approximate and re-checkable afterwards.
GPU_KINDS = {"dense_batch", "optimization", "monte_carlo", "search", "sieve", "anneal", "embed", "neural"}
GPU_MIN_SIZE = 50_000


def choose_backend(kind: str, size: int = 1, exact: bool = False) -> BackendDecision:
    if exact:
        return BackendDecision("cpu-exact", "Exact arithmetic/proof work is routed to CPU engines.")
    if kind in GPU_KINDS and size >= GPU_MIN_SIZE and mlx_gpu_available():
        return BackendDecision("mlx-gpu", "Large batched approximate workload; MLX/Metal available. Survivors must be rechecked exactly.")
    return BackendDecision("cpu", "Small or irregular workload; CPU avoids accelerator overhead.")


def mlx_gpu_available() -> bool:
    if importlib.util.find_spec("mlx") is None:
        return False
    try:
        import mlx.core as mx  # noqa: PLC0415

        return bool(mx.metal.is_available())
    except Exception:  # noqa: BLE001
        return False


def workspace_trusted(root) -> bool | None:
    """Whether Claude Code has recorded trust for this directory. None if the record is unreadable.
    Until trusted, .claude/settings.json (allowlist, hooks) is ignored by Claude Code."""
    import json  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    cfg = Path.home() / ".claude.json"
    if not cfg.exists():
        return None
    try:
        projects = json.loads(cfg.read_text()).get("projects", {})
    except (OSError, ValueError):
        return None
    entry = projects.get(str(Path(root).resolve())) or projects.get(str(root))
    if entry is None:
        return False
    return bool(entry.get("hasTrustDialogAccepted", False))


def capabilities() -> dict[str, bool | str]:
    mods = ["numpy", "scipy", "sympy", "mpmath", "flint", "z3", "mlx", "sage"]
    found: dict[str, bool | str] = {}
    for m in mods:
        try:
            found[m] = importlib.util.find_spec(m) is not None
        except (ImportError, ModuleNotFoundError, ValueError):
            found[m] = False
    found["mlx_metal"] = mlx_gpu_available()
    found["machine"] = platform.machine()
    found["lean"] = shutil.which("lean") is not None
    found["lake"] = shutil.which("lake") is not None
    found["elan"] = shutil.which("elan") is not None
    found["claude"] = shutil.which("claude") is not None
    return found
