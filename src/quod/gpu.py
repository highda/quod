"""Metal-backed array evaluation for wide, approximate, re-checkable work.

Nothing in here produces evidence above `empirical`: a GPU pass finds candidates — counterexamples to recheck
exactly, witnesses to verify, certificates to round — and the exact engines or the kernel decide what they are
worth. The CPU path is a drop-in fallback so a machine without Metal runs the same code, slower.
"""
from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from typing import Any, Callable

import sympy as sp

from .compute import mlx_gpu_available


@dataclass(frozen=True)
class Array:
    """A tiny backend shim: `mx` when Metal is usable, else numpy. Kept explicit so the caller can log which
    backend actually ran rather than guessing."""

    name: str
    mod: Any

    def asarray(self, x):
        return self.mod.array(x)

    def arange(self, n: int):
        return self.mod.arange(n)

    def to_list(self, x) -> list:
        return [*map(_scalar, x.tolist())] if hasattr(x, "tolist") else list(x)


def _scalar(v):
    return int(v) if isinstance(v, float) and float(v).is_integer() else v


def backend(prefer_gpu: bool = True) -> Array:
    if prefer_gpu and mlx_gpu_available():
        import mlx.core as mx  # noqa: PLC0415

        return Array("mlx-gpu", mx)
    if importlib.util.find_spec("numpy") is None:  # pragma: no cover - numpy is a hard dependency
        raise RuntimeError("numpy is required for the CPU fallback")
    import numpy as np  # noqa: PLC0415

    return Array("numpy-cpu", np)


# SymPy functions the array backends can evaluate. Anything else falls back to CPU sympy evaluation, because a
# silently approximated function is worse than a slow one.
ARRAY_FUNCS = {
    "Add": lambda b, *a: _fold(b, "add", a),
    "Mul": lambda b, *a: _fold(b, "multiply", a),
    "Pow": lambda b, x, y: b.mod.power(x, y),
    "Abs": lambda b, x: b.mod.abs(x),
    "Mod": lambda b, x, y: x - b.mod.floor(x / y) * y,
    "floor": lambda b, x: b.mod.floor(x),
    "Max": lambda b, *a: _fold(b, "maximum", a),
    "Min": lambda b, *a: _fold(b, "minimum", a),
    "sqrt": lambda b, x: b.mod.sqrt(x),
    "exp": lambda b, x: b.mod.exp(x),
    "log": lambda b, x: b.mod.log(x),
    "sin": lambda b, x: b.mod.sin(x),
    "cos": lambda b, x: b.mod.cos(x),
}


def _fold(b: Array, op: str, args):
    out = args[0]
    fn = getattr(b.mod, op)
    for a in args[1:]:
        out = fn(out, a)
    return out


class Unsupported(RuntimeError):
    """The expression contains something the array backend will not approximate."""


def compile_expr(expr: sp.Basic, order: list[sp.Symbol], b: Array) -> Callable:
    """Turn a SymPy expression into a function of backend arrays. Raises `Unsupported` rather than guessing."""
    index = {s: i for i, s in enumerate(order)}

    def walk(e: sp.Basic):
        if e.is_Integer:
            return float(int(e))
        if e.is_Rational:
            return float(e)
        if e.is_Float:
            return float(e)
        if e in index:
            return ("var", index[e])
        name = type(e).__name__
        if name not in ARRAY_FUNCS:
            raise Unsupported(f"{name} is not evaluable on the array backend: {e}")
        return (name, [walk(a) for a in e.args])

    tree = walk(sp.sympify(expr))

    def evaluate(cols: list):
        def ev(node):
            if isinstance(node, float):
                return node
            if node[0] == "var":
                return cols[node[1]]
            return ARRAY_FUNCS[node[0]](b, *[ev(a) for a in node[1]])

        return ev(tree)

    return evaluate


def relation_parts(rel: sp.Basic) -> tuple[str, sp.Basic]:
    """(kind, lhs - rhs) for a relation, so one array pass decides it. `Eq` uses an exact-integer comparison
    where the grid is integral, never a tolerance, because a near miss is not a solution."""
    if isinstance(rel, sp.Eq):
        return "eq", rel.lhs - rel.rhs
    if isinstance(rel, sp.Ne):
        return "ne", rel.lhs - rel.rhs
    if isinstance(rel, (sp.Lt, sp.StrictLessThan)):
        return "lt", rel.lhs - rel.rhs
    if isinstance(rel, (sp.Le, sp.LessThan)):
        return "le", rel.lhs - rel.rhs
    if isinstance(rel, (sp.Gt, sp.StrictGreaterThan)):
        return "gt", rel.lhs - rel.rhs
    if isinstance(rel, (sp.Ge, sp.GreaterThan)):
        return "ge", rel.lhs - rel.rhs
    raise Unsupported(f"not a relation this backend decides: {rel}")


def holds(kind: str, diff, b: Array):
    """Boolean mask for the relation, given lhs - rhs."""
    zero = 0.0
    if kind == "eq":
        return b.mod.abs(diff) < 0.5  # integral grids: |diff| < 1/2 means diff == 0
    if kind == "ne":
        return b.mod.abs(diff) >= 0.5
    if kind == "lt":
        return diff < zero
    if kind == "le":
        return diff <= zero
    if kind == "gt":
        return diff > zero
    return diff >= zero
