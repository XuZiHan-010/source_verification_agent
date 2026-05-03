from market_source_verification_agent import config


def test_load_settings_loads_dotenv_without_overriding_existing_env(monkeypatch):
    test_root = config.ROOT / "data" / "test_env"
    test_root.mkdir(parents=True, exist_ok=True)
    env_path = test_root / ".env"
    env_path.write_text("OPENAI_API_KEY=local-value\nEXAMPLE_FROM_DOTENV=yes\n", encoding="utf-8")

    monkeypatch.setattr(config, "ROOT", test_root)
    monkeypatch.setattr(config, "_DOTENV_LOADED", False)
    monkeypatch.setenv("OPENAI_API_KEY", "railway-value")

    config.load_settings(test_root / "missing-settings.yaml")

    assert config.os.environ["OPENAI_API_KEY"] == "railway-value"
    assert config.os.environ["EXAMPLE_FROM_DOTENV"] == "yes"
