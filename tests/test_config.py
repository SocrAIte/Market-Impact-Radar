from app.config import PROJECT_ROOT, Settings, load_yaml_config


def test_load_yaml_config_falls_back_to_project_example_when_cwd_differs(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    config = load_yaml_config("missing-config.yaml")

    assert "gdelt" in config.sources
    assert "rss" in config.sources
    assert "sec_edgar" in config.sources
    assert "china_sites" in config.sources
    assert config.sources["china_sites"].extra["sites"][0]["name"] == "证券时报"


def test_relative_sqlite_database_url_resolves_to_project_root(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    settings = Settings(database_url="sqlite:///./market_impact_radar.db")

    assert settings.database_url == f"sqlite:///{(PROJECT_ROOT / 'market_impact_radar.db').as_posix()}"


def test_title_translation_enabled_flag():
    assert Settings(database_url="sqlite:///./market_impact_radar.db", translation_provider="mymemory").title_translation_enabled
    assert Settings(
        database_url="sqlite:///./market_impact_radar.db",
        translation_provider="llm",
        llm_base_url="https://llm.example/v1",
        llm_api_key="key",
        llm_model="model",
    ).title_translation_enabled
    assert Settings(
        database_url="sqlite:///./market_impact_radar.db",
        translation_provider="libretranslate",
        llm_base_url="https://llm.example/v1",
        llm_api_key="key",
        llm_model="model",
    ).title_translation_enabled
    assert not Settings(
        database_url="sqlite:///./market_impact_radar.db",
        translation_provider="disabled",
    ).title_translation_enabled


def test_yaml_config_merges_missing_example_sources(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
sources:
  gdelt:
    enabled: false
""",
        encoding="utf-8",
    )

    config = load_yaml_config(config_path)

    assert config.sources["gdelt"].enabled is False
    assert "china_sites" in config.sources
