"""Ingest a Claude CLI stream-json log into per-turn context occupancy and per-tool-call timings.

The stream carries a timestamp on every message, a usage block on every assistant turn (so the context the
model actually read = input + cache_read + cache_creation), and tool_use/tool_result pairs. That is enough to
reconstruct where an epoch's minutes and tokens went without any probe inside the agent.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

KIND_RULES = [
    (r"quod formal check", "lean.check"), (r"quod formal attach", "lean.attach"), (r"quod formal hammer", "lean.hammer"),
    (r"quod formal search", "search"), (r"quod attack", "engine.attack"), (r"quod claim check", "engine.check"),
    (r"quod critic", "critic"), (r"quod propose", "propose"), (r"quod claim ", "state.claim"), (r"quod checkpoint", "state.checkpoint"),
    (r"quod epoch", "state.epoch"), (r"quod dossier|quod branch|quod library|quod ledger|quod fail-strategy|quod strategy", "state.other"),
    (r"lake env lean|lake build", "lean.raw"), (r"pixi run --quiet python|python3? ", "compute.python"),
    (r"\bsage\b", "compute.sage"), (r"\bgrep\b|\brg\b|\bfind\b|\bls\b|\bcat\b|\bsed\b|\bhead\b|\btail\b", "read"),
]


def _kind(tool_name: str, inp: dict) -> tuple[str, str]:
    if tool_name == "Bash":
        cmd = inp.get("command", "")
        for pat, kind in KIND_RULES:
            if re.search(pat, cmd):
                return kind, cmd
        return "bash.other", cmd
    if tool_name in ("Read", "Grep", "Glob"):
        return "read", json.dumps(inp)[:200]
    if tool_name in ("Write", "Edit"):
        return "write", inp.get("file_path", "")
    return "tool." + tool_name.lower(), json.dumps(inp)[:200]


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def parse_stream(path: str | Path) -> dict:
    turns, pending, calls = [], {}, []
    result = None
    # A log can carry more than one result envelope (the CLI continues after an interruption), and taking the
    # last one recorded a fifth of an epoch's real cost. Usage is summed over all of them.
    env_in = env_out = 0
    for line in Path(path).read_text().splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = d.get("type")
        if t == "assistant":
            m = d.get("message", {})
            u = m.get("usage") or {}
            if u:
                inp, cr, cc, out = (u.get("input_tokens", 0), u.get("cache_read_input_tokens", 0),
                                    u.get("cache_creation_input_tokens", 0), u.get("output_tokens", 0))
                turns.append({"idx": len(turns), "ts": d.get("timestamp", ""), "input": inp, "cache_read": cr,
                              "cache_create": cc, "output": out, "context": inp + cr + cc})
            for c in m.get("content", []) or []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    kind, summary = _kind(c.get("name", ""), c.get("input") or {})
                    pending[c.get("id")] = {"ts": d.get("timestamp", ""), "tool": c.get("name", ""), "kind": kind, "summary": summary}
        elif t == "user":
            m = d.get("message", {})
            for c in (m.get("content", []) if isinstance(m.get("content"), list) else []):
                if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("tool_use_id") in pending:
                    start = pending.pop(c["tool_use_id"])
                    try:
                        secs = (_ts(d["timestamp"]) - _ts(start["ts"])).total_seconds()
                    except Exception:  # noqa: BLE001
                        secs = None
                    text = c.get("content")
                    if isinstance(text, list):
                        text = " ".join(x.get("text", "") for x in text if isinstance(x, dict))
                    text = str(text or "")
                    ok = not c.get("is_error", False) and not re.search(r'"verdict": "(failed|statement_error)"|"error":', text[:400])
                    calls.append({**start, "seconds": secs, "ok": bool(ok), "result_head": text[:200]})
        elif t == "result":
            result = d
            u = d.get("usage") or {}
            env_in += sum(u.get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
            env_out += u.get("output_tokens", 0)
    input_total, output = env_in, env_out
    measured_from = "result envelope"
    if not input_total and turns:
        # A run killed by the wall-clock watchdog or the turn cap writes no result envelope, so its cost would
        # be recorded as zero and eta would be uncomputable. The per-turn usage blocks are already in the log.
        input_total = sum(t["context"] for t in turns)
        output = sum(t["output"] for t in turns)
        measured_from = "per-turn usage (no result envelope: run was cut off)"
    return {"turns": turns, "tool_calls": calls, "result": {
        "input_total": input_total, "output": output, "cost_usd": (result or {}).get("total_cost_usd"),
        "measured_from": measured_from,
        "num_turns": (result or {}).get("num_turns") or len(turns), "duration_ms": (result or {}).get("duration_ms")}}
