"""The proposer/formalizer split. With one model in both roles it must behave exactly as before."""
import json

import pytest
from typer.testing import CliRunner

from quod import config as config_mod, formalize
from quod.cli import app
from quod.models import Branch, Claim, FormalClaim
from quod.state import StateStore

runner = CliRunner()


@pytest.fixture
def store(root):
    with StateStore(root / "state" / "harness.sqlite3") as s:
        s.create_branch(Branch(id="B", problem="p", objective="o"))
        s.add_claim(Claim(id="C", branch_id="B", statement="one plus one is two",
                          formal=FormalClaim(lean="theorem c : 1 + 1 = 2 := by norm_num",
                                             frozen=["theorem c : 1 + 1 = 2"])))
        yield s


# -- the split is opt-in --------------------------------------------------------------------
def test_formalizer_defaults_to_the_proposer(monkeypatch, root):
    monkeypatch.delenv("QUOD_FORMALIZER_MODEL", raising=False)
    cfg = config_mod.resolve(proposer="fable")
    assert cfg.formalizer == "fable"
    assert cfg.source["formalizer"] == "default(proposer)"
    assert cfg.warnings() == []  # an unset formalizer is not a misconfiguration


def test_explicitly_matching_models_warn_but_work(monkeypatch, root):
    monkeypatch.setenv("QUOD_FORMALIZER_MODEL", "opus")
    cfg = config_mod.resolve(proposer="opus")
    assert cfg.formalizer == "opus"
    assert any("buys nothing" in w for w in cfg.warnings())


def test_split_roles_resolve_independently(monkeypatch, root):
    monkeypatch.setenv("QUOD_FORMALIZER_MODEL", "claude-opus-5-5")
    cfg = config_mod.resolve(proposer="claude-fable-5-1")
    assert (cfg.proposer, cfg.formalizer) == ("claude-fable-5-1", "claude-opus-5-5")


# -- the delegated session ------------------------------------------------------------------
def test_dry_run_carries_the_claim_and_its_frozen_signatures(store, monkeypatch):
    monkeypatch.setenv("QUOD_FORMALIZER_MODEL", "claude-opus-5-5")
    rep = formalize.run(store, "C", dry_run=True)
    assert rep["model"] == "claude-opus-5-5" and rep["same_model_as_proposer"] is False
    assert "theorem c : 1 + 1 = 2" in rep["prompt"]
    assert "one plus one is two" in rep["prompt"]
    cmd = rep["command"]
    assert "--allowed-tools" in cmd and "Bash,Read,Write,Edit,Glob,Grep" in cmd
    assert "--strict-mcp-config" in cmd and "--no-session-persistence" in cmd


def test_result_line_is_parsed_from_the_envelope():
    env = json.dumps({"result": "I closed it.\nRESULT: proved formal/Quod/Foo.lean\n"})
    assert formalize.parse_result(env) == ("proved", "formal/Quod/Foo.lean")
    env2 = json.dumps({"result": "RESULT: proved a.lean\nlater\nRESULT: failed b.lean"})
    assert formalize.parse_result(env2) == ("failed", "b.lean")  # the last line wins
    assert formalize.parse_result(json.dumps({"result": "no result line"})) == (None, None)


def test_a_missing_file_is_not_a_proof(store, monkeypatch):
    """The session claiming success is not evidence; the file must exist and the kernel must accept it."""
    monkeypatch.setattr(formalize.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(formalize.subprocess, "run", lambda *a, **k: type(
        "P", (), {"returncode": 0, "stdout": json.dumps({"result": "RESULT: proved formal/Quod/Nope.lean"}),
                  "stderr": ""})())
    rep = formalize.run(store, "C")
    assert rep["verdict"] == "no_file"


def test_cli_exposes_the_role(root):
    assert runner.invoke(app, ["init"]).exit_code == 0
    r = runner.invoke(app, ["config"])
    assert r.exit_code == 0 and "formalizer" in json.loads(r.stdout)["models"]
