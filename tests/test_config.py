"""Local env loading: dotenv parsing + non-overriding load."""

from __future__ import annotations

import pytest

from sabnyx import config


def test_parse_dotenv_basics():
    parsed = config.parse_dotenv(
        "\n".join(
            [
                "# a comment",
                "",
                "MOONSHOT_API_KEY=sk-abc123",
                'SABNYX_LLM_PROVIDER="moonshot"',
                "export SABNYX_MODEL_FAST='moonshot-v1-32k'",
                "NOT_A_PAIR",
            ]
        )
    )
    assert parsed == {
        "MOONSHOT_API_KEY": "sk-abc123",
        "SABNYX_LLM_PROVIDER": "moonshot",
        "SABNYX_MODEL_FAST": "moonshot-v1-32k",
    }


def test_load_local_env_sets_from_dotenv_file(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("MOONSHOT_API_KEY=sk-fromfile\n")
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    loaded = config.load_local_env(tmp_path)
    import os

    assert "MOONSHOT_API_KEY" in loaded
    assert os.environ["MOONSHOT_API_KEY"] == "sk-fromfile"


def test_load_local_env_does_not_override_real_env(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("MOONSHOT_API_KEY=sk-fromfile\n")
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-already-set")
    loaded = config.load_local_env(tmp_path)
    import os

    assert "MOONSHOT_API_KEY" not in loaded  # explicit env wins
    assert os.environ["MOONSHOT_API_KEY"] == "sk-already-set"


def test_load_local_env_moonshot_file_is_not_read(tmp_path, monkeypatch):
    """The legacy 'moonshot' credential file is no longer consulted."""
    (tmp_path / "moonshot").write_text("MOONSHOT_API_KEY=sk-legacy\n")
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    loaded = config.load_local_env(tmp_path)
    import os

    assert "MOONSHOT_API_KEY" not in loaded
    assert "MOONSHOT_API_KEY" not in os.environ


def test_load_local_env_missing_files_is_noop(tmp_path):
    assert config.load_local_env(tmp_path) == []


# -- ingress spend cap settings: fail fast on an invalid value ----------------
#
# A typo'd or out-of-range SABNYX_INGRESS_MAX_CALLS/_WINDOW_MINUTES would
# otherwise silently remove the only bound on externally-triggered LLM spend
# (spend.py) — these must raise, not fall back to a working default.


def test_ingress_window_minutes_zero_raises(monkeypatch):
    monkeypatch.setenv("SABNYX_INGRESS_WINDOW_MINUTES", "0")
    with pytest.raises(ValueError, match="SABNYX_INGRESS_WINDOW_MINUTES"):
        config.ingress_window_minutes()


def test_ingress_window_minutes_non_int_raises(monkeypatch):
    monkeypatch.setenv("SABNYX_INGRESS_WINDOW_MINUTES", "abc")
    with pytest.raises(ValueError, match="SABNYX_INGRESS_WINDOW_MINUTES"):
        config.ingress_window_minutes()


def test_ingress_max_calls_non_int_raises(monkeypatch):
    monkeypatch.setenv("SABNYX_INGRESS_MAX_CALLS", "abc")
    with pytest.raises(ValueError, match="SABNYX_INGRESS_MAX_CALLS"):
        config.ingress_max_calls()


def test_ingress_max_calls_zero_is_a_valid_disable(monkeypatch):
    monkeypatch.setenv("SABNYX_INGRESS_MAX_CALLS", "0")
    assert config.ingress_max_calls() == 0  # disabled, not an error
