import json

from typer.testing import CliRunner

from quod.cli import app

runner = CliRunner()


def run(*args):
    r = runner.invoke(app, list(args))
    return r.exit_code, r.stdout


def test_cli_roundtrip(root):
    assert run("init")[0] == 0
    assert run("branch", "new", "B", "--problem", "p", "--objective", "o")[0] == 0
    code, _ = run("claim", "add", "C", "-b", "B", "--statement", "s", "--formal",
                  json.dumps({"variables": {"x": "R"}, "conclusion": "Eq((x+1)**2, x**2+2*x+1)"}))
    assert code == 0
    code, out = run("attack", "C", "--no-lean")
    assert code == 0 and json.loads(out)["final_rigor"] in ("symbolically_verified", "solver_verified")
    code, out = run("claim", "promote", "C", "empirical")
    assert code == 1
    assert run("fail-strategy", "-b", "B", "--strategy", "s", "--reason", "too short")[0] == 1
    assert run("checkpoint", "create", "-b", "B", "--goal", "g", "--next", "a")[0] == 0
    code, out = run("epoch", "mark", "--reason", "compaction")
    assert code == 0
    code, out = run("dossier", "-b", "B")
    assert code == 0 and "symbolically_verified" in out or "solver_verified" in out
    assert run("ledger", "add", "tool", "--title", "t", "--rationale", "r")[0] == 0
    # exports are the user's material: they land in the git-ignored local/ workspace, not in docs/
    assert run("ledger", "export")[0] == 0 and (root / "local" / "reports" / "LEDGER.md").exists()
    assert run("critic", "C", "--dry-run")[0] == 0
    code, out = run("claim", "check", "--formal", json.dumps({"variables": {"n": "N"}, "conclusion": "Gt(n, 0)"}))
    assert code == 0 and json.loads(out)["instances"]["outcome"] == "refuted"
