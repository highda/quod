import json

from typer.testing import CliRunner

from quod.cli import app

runner = CliRunner()


def test_epoch_end_reads_cli_envelope(root):
    runner.invoke(app, ["branch", "new", "B", "--problem", "p", "--objective", "o"])
    runner.invoke(app, ["epoch", "start", "-b", "B", "--model", "opus"])
    env = {"type": "result", "usage": {"input_tokens": 10, "cache_creation_input_tokens": 100,
                                       "cache_read_input_tokens": 1000, "output_tokens": 50}, "total_cost_usd": 0.1}
    log = root / "run.jsonl"
    log.write_text('{"type":"assistant"}\n' + json.dumps(env) + "\n")
    r = runner.invoke(app, ["epoch", "end", "-b", "B", "--reason", "done", "--usage-file", str(log), "--force"])
    assert r.exit_code == 0, r.stdout
    ep = json.loads(r.stdout)
    assert (ep["tokens_in"], ep["tokens_out"]) == (1110, 50)


def test_epoch_usage_amends_closed_epoch(root):
    runner.invoke(app, ["branch", "new", "B", "--problem", "p", "--objective", "o"])
    runner.invoke(app, ["epoch", "start", "-b", "B", "--model", "opus"])
    runner.invoke(app, ["epoch", "end", "-b", "B", "--reason", "agent ended it"])
    log = root / "run.json"
    log.write_text(json.dumps({"type": "result", "usage": {"input_tokens": 1, "cache_creation_input_tokens": 2,
                                                            "cache_read_input_tokens": 3, "output_tokens": 4}}))
    r = runner.invoke(app, ["epoch", "usage", "-b", "B", "--usage-file", str(log)])
    assert r.exit_code == 0, r.stdout
    assert json.loads(r.stdout)["epoch"]["tokens_in"] == 6


def test_usage_sums_every_result_envelope_in_a_stream_log(root):
    """A stream log can carry several result envelopes; taking the last recorded a fifth of the real cost."""
    from quod.cli import _usage_from_envelope
    from quod import ingest as ingest_mod
    def env(n):
        return json.dumps({"type": "result", "usage": {"input_tokens": n, "cache_read_input_tokens": 10 * n,
                                                      "cache_creation_input_tokens": 0, "output_tokens": n}})
    log = root / "two.jsonl"
    log.write_text(env(100) + "\n" + '{"type":"assistant"}\n' + env(5) + "\n")
    u = _usage_from_envelope(log)
    assert (u["input_total"], u["output"]) == (1155, 105)
    assert ingest_mod.parse_stream(log)["result"]["input_total"] == 1155
