"""Model/role configuration. Precedence: explicit argument > environment > quod.toml > default.

Roles:
  proposer  — the main Claude Code session (strategy, formalization, tool use)
  adversary — isolated critic spawned per claim (`quod critic`, tools off)
  overseer  — reserved for a future branch/budget scheduler; unused today
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass

from .paths import harness_root

DEFAULT_MODEL = "opus"
ROLES = ("proposer", "adversary", "overseer", "formalizer")
ALIASES = {"opus": "opus", "fable": "fable", "sonnet": "sonnet", "haiku": "haiku"}
# Full ids resolve too (`claude-fable-5-1`, `claude-opus-5-5`); an alias tracks the latest of its family.


@dataclass(frozen=True)
class ModelConfig:
    proposer: str
    adversary: str
    overseer: str
    formalizer: str
    critic_timeout: int
    lean_timeout: int
    prover_model: str
    source: dict[str, str]  # role -> where the value came from

    def warnings(self) -> list[str]:
        w = []
        if self.formalizer == self.proposer and not self.source["formalizer"].startswith("default"):
            w.append("formalizer and proposer are the same model: `quod formalize` still works (fresh context, "
                     "same tools, same kernel) but the role split buys nothing.")
        if self.proposer == self.adversary:
            w.append("proposer and adversary use the same model; independence rests on context isolation only. "
                     "Prefer a different model for the adversary (see README: ideal configuration).")
        return w


def _file() -> dict:
    p = harness_root() / "quod.toml"
    if not p.exists():
        return {}
    try:
        return tomllib.loads(p.read_text())
    except tomllib.TOMLDecodeError as e:
        raise RuntimeError(f"quod.toml is not valid TOML: {e}") from e


def resolve(proposer: str | None = None, adversary: str | None = None,
            formalizer: str | None = None) -> ModelConfig:
    cfg = _file()
    models = cfg.get("models", {})
    out: dict[str, str] = {}
    src: dict[str, str] = {}
    explicit = {"proposer": proposer, "adversary": adversary, "overseer": None, "formalizer": formalizer}
    for role in ROLES:
        env_key = f"QUOD_{role.upper()}_MODEL"
        if explicit[role]:
            out[role], src[role] = explicit[role], "argument"
        elif os.environ.get(env_key):
            out[role], src[role] = os.environ[env_key], f"env:{env_key}"
        elif models.get(role):
            out[role], src[role] = str(models[role]), "quod.toml"
        elif role == "formalizer":
            # Unset means "no split": the proposer's model formalizes, exactly as before this role existed.
            out[role], src[role] = out["proposer"], "default(proposer)"
        else:
            out[role], src[role] = DEFAULT_MODEL, "default"
    timeout = int(os.environ.get("QUOD_CRITIC_TIMEOUT", cfg.get("critic", {}).get("timeout", 900)))
    lean_timeout = int(os.environ.get("QUOD_LEAN_TIMEOUT", cfg.get("formal", {}).get("timeout", 900)))
    # Local, weights-on-disk model for `quod neural-hammer`. Empty means the feature is off, which is the
    # default: it needs multi-gigabyte weights that this repository does not ship.
    prover_model = os.environ.get("QUOD_PROVER_MODEL", cfg.get("neural", {}).get("prover_model", "")) or ""
    return ModelConfig(out["proposer"], out["adversary"], out["overseer"], out["formalizer"], timeout,
                       lean_timeout, prover_model, src)
