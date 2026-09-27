from quod import config


def test_precedence(root, monkeypatch):
    assert config.resolve().proposer == "opus"
    (root / "quod.toml").write_text('[models]\nproposer = "fable"\nadversary = "sonnet"\n[critic]\ntimeout = 5\n')
    cfg = config.resolve()
    assert (cfg.proposer, cfg.adversary, cfg.critic_timeout) == ("fable", "sonnet", 5)
    assert cfg.source["proposer"] == "quod.toml" and cfg.warnings() == []
    monkeypatch.setenv("QUOD_ADVERSARY_MODEL", "fable")
    cfg = config.resolve()
    assert cfg.adversary == "fable" and cfg.source["adversary"].startswith("env:") and cfg.warnings()
    assert config.resolve(adversary="haiku").adversary == "haiku"
