import json

from quod import ingest, propose, strategy, trace
from quod.models import Branch, Claim, Epoch, Evidence, FormalClaim, Rigor
from quod.state import StateStore

STREAM = [
    {"type": "assistant", "timestamp": "2026-09-20T10:00:00.000Z", "message": {"usage": {"input_tokens": 5, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 200, "output_tokens": 50},
     "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pixi run --quiet quod formal check --statement 'theorem x : 1 = 1 := rfl'"}}]}},
    {"type": "user", "timestamp": "2026-09-20T10:00:12.000Z", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "{\"verdict\": \"proved\"}"}]}},
    {"type": "assistant", "timestamp": "2026-09-20T10:00:20.000Z", "message": {"usage": {"input_tokens": 3, "cache_read_input_tokens": 1300, "cache_creation_input_tokens": 100, "output_tokens": 20},
     "content": [{"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "pixi run --quiet python -c 'print(1)'"}}]}},
    {"type": "user", "timestamp": "2026-09-20T10:00:21.500Z", "message": {"content": [{"type": "tool_result", "tool_use_id": "t2", "content": "1"}]}},
    {"type": "result", "usage": {"input_tokens": 8, "cache_creation_input_tokens": 300, "cache_read_input_tokens": 2300, "output_tokens": 70}, "total_cost_usd": 0.01, "num_turns": 2, "duration_ms": 25000},
]


def test_ingest_stream(root):
    log = root / "run.jsonl"
    log.write_text("\n".join(json.dumps(x) for x in STREAM))
    p = ingest.parse_stream(log)
    assert len(p["turns"]) == 2 and p["turns"][0]["context"] == 1205
    kinds = [c["kind"] for c in p["tool_calls"]]
    assert kinds == ["lean.check", "compute.python"]
    assert abs(p["tool_calls"][0]["seconds"] - 12.0) < 0.01 and p["tool_calls"][0]["ok"]
    assert p["result"]["input_total"] == 2608


def test_trace_phases_and_closure(root):
    with StateStore(root / "state" / "t.sqlite3") as s:
        s.create_branch(Branch(id="B", problem="p", objective="o"))
        s.start_epoch(Epoch(branch_id="B", model="opus"))
        s.add_claim(Claim(id="B-target", branch_id="B", statement="t", tags=["target"],
                          formal=FormalClaim(lean="theorem main : 1 = 1 := by sorry")))
        s.add_claim(Claim(id="L1", branch_id="B", statement="lemma used", formal=FormalClaim(lean="theorem helper : 2 = 2 := by rfl")))
        s.add_claim(Claim(id="L2", branch_id="B", statement="lemma unused", formal=FormalClaim(lean="theorem orphan : 3 = 3 := by rfl")))
        for cid in ("L1", "L2"):
            s.add_evidence(cid, Evidence(kind="lean", summary="ok", produced_by="engine:lean"))
            s.promote(cid, Rigor.FORMALLY_PROVED)
        s.tool_event("B", "formal.check", "lean.check", 3.0, "failed: main", False)
        s.set_lean("B-target", "theorem helper : 2 = 2 := by rfl\ntheorem main : 1 = 1 := by\n  have := helper\n  rfl")
        s.add_evidence("B-target", Evidence(kind="lean", summary="ok", produced_by="engine:lean"))
        s.promote("B-target", Rigor.FORMALLY_PROVED)
        s.end_epoch("B", "done", tokens_in=1_000_000, tokens_out=1000)
        s.set_epoch_usage("B", 1_000_000, 1000)
        a = trace.analyze(s, "B")
        assert a["closure"]["claims_in"] == ["L1"] and a["closure"]["claims_outside"] == ["L2"]
        assert a["progress_points"] == 5.0  # target units: a proved target is 5 however many lemmas it took
        assert a["tools"]["lean.check"]["failed"] == 1
        assert trace.render(a).startswith("# Trace — B")
        # strategy outcome accounting rides on the same closure
        assert strategy.outcome_for(s, "B", ["helper"]) == "decisive"
        assert strategy.outcome_for(s, "B", ["L2"]) == "useful"
        assert strategy.outcome_for(s, "B", ["nothing"]) == "attempted"


def test_strategy_store_and_match(root):
    ss = strategy.StrategyStore(root / "lib.sqlite3")
    try:
        ss.add(strategy.StrategyCard(id="S-sym", name="sym", trigger_features=["inequality", "symmetry"], procedure=["a", "b"]))
        ss.add(strategy.StrategyCard(id="S-int", name="int", trigger_features=["integral"], procedure=["c"]))
        ss.record_use("S-sym", "B1", "decisive", ["l"])
        ranked = strategy.match(ss.all(), ["inequality", "symmetry", "sum"])
        assert ranked[0][0].id == "S-sym" and len(ranked) == 1 and set(ranked[0][2]) == {"inequality", "symmetry"}
        # rarity weighting: a card triggered on a rare feature does not fire on generic overlap alone
        ss.add(strategy.StrategyCard(id="S-sphere", name="sphere", trigger_features=["measure", "symmetry", "equation", "integral"], procedure=["x"]))
        idf = {"measure": 3.0, "symmetry": 2.0, "equation": 0.1, "integral": 0.5, "inequality": 1.0, "sum": 1.0}
        hits = strategy.match(ss.all(), ["equation", "integral", "sum"], idf=idf)
        assert all(c.id != "S-sphere" for c, _, _ in hits)
        assert ss.get("S-sym").decisive == 1 and ss.get("S-sym").attempted == 1
    finally:
        ss.close()


def test_propose_experiments_are_deterministic():
    killed = propose.run_experiment({"claim": {"variables": {"x": "R"}, "conclusion": "Eq(x**2, x)"}})
    assert killed["verdict"] == "killed"
    alive = propose.run_experiment({"claim": {"variables": {"x": "R"}, "conclusion": "Ge(x**2, 0)"}})
    assert alive["verdict"] == "survived" and alive["strength"] == "strong"
    assert propose.run_experiment(None)["verdict"] == "untested"
    assert propose.run_experiment({"claim": {"variables": {"x": "R"}, "conclusion": "Eq(undefined_fn(x), 0)"}})["verdict"] == "malformed"


def test_critic_mode_scheduler():
    from quod import critic
    from quod.models import AttackReport
    c = Claim(id="C", branch_id="B", statement="s", rigor=Rigor.EMPIRICAL)
    assert critic.choose_mode(c, []) == "counterexample"
    prior = [AttackReport(claim_id="C", branch_id="B", attacker="model:opus", kind="llm:counterexample", outcome="survived", details="")]
    assert critic.choose_mode(c, prior) == "assumption"
    proved = Claim(id="C", branch_id="B", statement="s", rigor=Rigor.FORMALLY_PROVED, formal=FormalClaim(lean="theorem t : 1 = 1 := rfl"))
    assert critic.choose_mode(proved, []) == "representation"


def test_outcome_resolves_claim_ids_to_declarations(root):
    with StateStore(root / "state" / "o.sqlite3") as s:
        s.create_branch(Branch(id="B", problem="p", objective="o"))
        s.add_claim(Claim(id="B-target", branch_id="B", statement="t", tags=["target"], formal=FormalClaim(lean="theorem main : 1 = 1 := by sorry")))
        s.add_claim(Claim(id="B-target-L1", branch_id="B", statement="l", formal=FormalClaim(lean="theorem key_step : 2 = 2 := by rfl")))
        s.set_lean("B-target", "theorem key_step : 2 = 2 := by rfl\ntheorem main : 1 = 1 := by rfl")
        assert strategy.outcome_for(s, "B", ["B-target-L1"]) == "decisive"


def test_register_declarations_from_accepted_proof(root):
    proof = ("open Nat\n\nlemma helper_one (n : ℕ) : n + 0 = n := by simp\n\n"
             "/-- doc -/\ntheorem helper_two (n : ℕ) : 0 + n = n := by simp\n\n"
             "theorem putnam_1900_a1 (n : ℕ) : n + 0 = 0 + n := by rw [helper_one, helper_two]\n")
    with StateStore(root / "state" / "r.sqlite3") as s:
        s.create_branch(Branch(id="B", problem="p", objective="o"))
        s.add_claim(Claim(id="B-target", branch_id="B", statement="t", tags=["target"],
                          formal=FormalClaim(lean="theorem putnam_1900_a1 (n : ℕ) : n + 0 = 0 + n := by sorry",
                                             frozen=["theorem putnam_1900_a1 (n : ℕ) : n + 0 = 0 + n"])))
        s.start_epoch(Epoch(branch_id="B", model="opus"))
        s.tool_event("B", "formal.check", "lean.check", 2.0, "proved: x.lean [helper_one]", True)
        s.set_lean("B-target", proof)
        s.add_evidence("B-target", Evidence(kind="lean", summary="ok", produced_by="engine:lean"))
        s.promote("B-target", Rigor.FORMALLY_PROVED)
        created = s.register_declarations("B-target", proof, s.first_seen_declarations("B"))
        assert sorted(created) == ["B-target-decl-helper_one", "B-target-decl-helper_two"]
        h1 = s.get_claim("B-target-decl-helper_one")
        assert h1.rigor == Rigor.FORMALLY_PROVED and h1.formal.lean.startswith("lemma helper_one") and "auto" in h1.tags
        assert s.register_declarations("B-target", proof) == []  # idempotent
        a = trace.analyze(s, "B")
        assert set(a["closure"]["claims_in"]) == {"B-target-decl-helper_one", "B-target-decl-helper_two"}
        assert a["progress_points"] == 5.0


def test_propose_records_all_outcomes(root, monkeypatch):
    # stub the planner: two approaches, one refutable, one inconclusive (integral the engines can't decide symbolically)
    fake = json.dumps([
        {"name": "wrong-identity", "idea": "x", "kills_if": "any x", "experiment": {"claim": {"variables": {"x": "R"}, "conclusion": "Eq(x**3, x)"}}},
        {"name": "no-experiment", "idea": "y", "kills_if": "-", "experiment": None},
    ])
    monkeypatch.setattr(propose, "_model_call", lambda system, prompt, model, timeout: (fake, None))
    with StateStore(root / "state" / "p.sqlite3") as s:
        s.create_branch(Branch(id="B", problem="p", objective="o"))
        rep = propose.run(s, "B", n=5, model="opus", use_hammer=False)
        assert rep["tally"]["killed"] == 1 and rep["tally"]["untested"] == 1
        killed = s.get_claim("B-approach-1")
        assert killed and killed.rigor == Rigor.REFUTED and "killed" in killed.tags
        assert s.get_claim("B-approach-2") is None  # untested approaches are not claims


def test_ablation_switches_refuse(root, monkeypatch):
    from typer.testing import CliRunner
    from quod.cli import app
    r = CliRunner()
    r.invoke(app, ["branch", "new", "B", "--problem", "p", "--objective", "o"])
    r.invoke(app, ["claim", "add", "C", "-b", "B", "--statement", "s", "--formal", json.dumps({"variables": {"x": "R"}, "conclusion": "Eq(x, x)"})])
    monkeypatch.setenv("QUOD_ABLATE", "engines,adversary,strategy")
    assert r.invoke(app, ["attack", "C"]).exit_code == 3
    assert r.invoke(app, ["critic", "C", "--dry-run"]).exit_code == 3
    res = r.invoke(app, ["strategy", "match", "-b", "B"])
    assert res.exit_code == 0 and "disabled" in res.stdout
    monkeypatch.setenv("QUOD_ABLATE", "")
    assert r.invoke(app, ["attack", "C", "--no-lean"]).exit_code == 0


def test_ingest_falls_back_to_per_turn_usage_when_the_run_was_cut_off(tmp_path):
    """A watchdog-killed run writes no result envelope; its cost must still be measured."""
    import json as _json

    from quod import ingest as ingest_mod
    log = tmp_path / "cutoff.jsonl"
    lines = []
    for i in range(3):
        lines.append(_json.dumps({"type": "assistant", "timestamp": f"2026-09-27T00:0{i}:00Z", "message": {
            "usage": {"input_tokens": 10, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 100,
                      "output_tokens": 20}, "content": []}}))
    log.write_text("\n".join(lines) + "\n")
    parsed = ingest_mod.parse_stream(log)
    assert parsed["result"]["input_total"] == 3 * 1110
    assert parsed["result"]["output"] == 60
    assert "cut off" in parsed["result"]["measured_from"]
    assert parsed["result"]["num_turns"] == 3
