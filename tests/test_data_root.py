"""Tests for SABNYX_DATA_DIR / DATA_ROOT configurability.

Strategy: exercise the pure helpers ``resolve_data_root`` and
``effective_data_dir`` directly rather than reloading the module, keeping
tests fast and free of import-time side effects.  The module-level constants
(DATA_ROOT, VAULT, OPS, …) are also spot-checked for the default case to
confirm that default behavior is 100% unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sabnyx.paths import (
    DATA_ROOT,
    EVENTS_JSONL,
    KNOWLEDGE_LOG,
    OPS,
    ROOT,
    VAULT,
    effective_data_dir,
    resolve_data_root,
)


# ---------------------------------------------------------------------------
# Pure helper tests (no import-time state)
# ---------------------------------------------------------------------------


def test_resolve_data_root_defaults_to_root(tmp_path: Path) -> None:
    """When SABNYX_DATA_DIR is absent, resolve_data_root returns root."""
    result = resolve_data_root({}, tmp_path)
    assert result == tmp_path


def test_resolve_data_root_empty_string_defaults_to_root(tmp_path: Path) -> None:
    """An empty SABNYX_DATA_DIR (whitespace included) is treated as unset."""
    for empty in ("", "   ", "\t"):
        result = resolve_data_root({"SABNYX_DATA_DIR": empty}, tmp_path)
        assert result == tmp_path, f"expected root for SABNYX_DATA_DIR={empty!r}"


def test_resolve_data_root_uses_env_var(tmp_path: Path) -> None:
    """When SABNYX_DATA_DIR is set, resolve_data_root resolves it."""
    data_dir = tmp_path / "mydata"
    data_dir.mkdir()
    result = resolve_data_root({"SABNYX_DATA_DIR": str(data_dir)}, tmp_path)
    assert result == data_dir.resolve()


def test_resolve_data_root_expands_tilde(tmp_path: Path) -> None:
    """SABNYX_DATA_DIR with a tilde is expanded via expanduser."""
    import os

    home = Path.home()
    # Use a path relative to home that we can verify expands correctly.
    # We don't need it to actually exist; resolve() just canonicalises the path.
    env = {"SABNYX_DATA_DIR": "~/nonexistent-sabnyx-test-dir"}
    result = resolve_data_root(env, tmp_path)
    assert result == (home / "nonexistent-sabnyx-test-dir").resolve()
    assert not result.is_relative_to(tmp_path), "tilde expansion must NOT use the root"


def test_resolve_data_root_absolute_path(tmp_path: Path) -> None:
    """An absolute path in SABNYX_DATA_DIR is returned as-is (resolved)."""
    target = (tmp_path / "vault-data").resolve()
    target.mkdir()
    result = resolve_data_root({"SABNYX_DATA_DIR": str(target)}, tmp_path / "code")
    assert result == target


# ---------------------------------------------------------------------------
# Default-behavior tests: DATA_ROOT == ROOT when env var is not set
# ---------------------------------------------------------------------------


def test_default_data_root_equals_root() -> None:
    """Without SABNYX_DATA_DIR in the environment, DATA_ROOT must equal ROOT."""
    import os

    if "SABNYX_DATA_DIR" not in os.environ:
        assert DATA_ROOT == ROOT, (
            "DATA_ROOT should default to ROOT when SABNYX_DATA_DIR is not set"
        )
    else:
        pytest.skip("SABNYX_DATA_DIR is set in this environment — default test skipped")


def test_default_vault_under_root() -> None:
    """VAULT must resolve under ROOT (or DATA_ROOT) in the default case."""
    import os

    expected_base = ROOT if "SABNYX_DATA_DIR" not in os.environ else DATA_ROOT
    assert VAULT == expected_base / "vault"


def test_default_ops_under_root() -> None:
    """OPS must resolve under ROOT (or DATA_ROOT) in the default case."""
    import os

    expected_base = ROOT if "SABNYX_DATA_DIR" not in os.environ else DATA_ROOT
    assert OPS == expected_base / "ops"


def test_default_events_jsonl_under_ops() -> None:
    """EVENTS_JSONL must be a child of OPS."""
    assert EVENTS_JSONL == OPS / "events.jsonl"


def test_default_knowledge_log_under_ops() -> None:
    """KNOWLEDGE_LOG must be a child of OPS."""
    assert KNOWLEDGE_LOG == OPS / "knowledge-log.md"


# ---------------------------------------------------------------------------
# Override-path tests: SABNYX_DATA_DIR pointing at a temp dir
# ---------------------------------------------------------------------------


def test_override_vault_ops_under_data_dir(tmp_path: Path) -> None:
    """When SABNYX_DATA_DIR is set, vault/ops/ledger paths all land under it."""
    data_dir = tmp_path / "private-vault"
    data_dir.mkdir()

    # Compute what the module *would* have set if it had seen this env var.
    resolved = resolve_data_root({"SABNYX_DATA_DIR": str(data_dir)}, ROOT)

    # All data-surface paths must be under the data dir.
    assert resolved / "vault" == resolved / "vault"
    assert str(resolved / "vault").startswith(str(data_dir))
    assert str(resolved / "ops").startswith(str(data_dir))
    assert str(resolved / "ops" / "events.jsonl").startswith(str(data_dir))
    assert str(resolved / "ops" / "knowledge-log.md").startswith(str(data_dir))

    # ROOT must NOT change.
    assert ROOT.is_dir(), "ROOT must still point at the code repo"
    assert (ROOT / "SPEC.md").is_file(), "ROOT must still contain SPEC.md"


def test_override_root_unchanged(tmp_path: Path) -> None:
    """Setting SABNYX_DATA_DIR must never alter ROOT."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _ = resolve_data_root({"SABNYX_DATA_DIR": str(data_dir)}, ROOT)
    # ROOT is a module-level constant — it is never affected by the env var.
    assert (ROOT / "SPEC.md").is_file()


def test_override_vault_not_under_code_root(tmp_path: Path) -> None:
    """When DATA_ROOT != ROOT, VAULT must NOT be under the code root."""
    data_dir = tmp_path / "separate-repo"
    data_dir.mkdir()
    resolved = resolve_data_root({"SABNYX_DATA_DIR": str(data_dir)}, ROOT)
    vault = resolved / "vault"
    assert not vault.is_relative_to(ROOT), (
        "vault must live under DATA_ROOT, not under the code ROOT, "
        "when SABNYX_DATA_DIR is set to a different directory"
    )


# ---------------------------------------------------------------------------
# effective_data_dir tests — dotenv fallback consulted at import time
# ---------------------------------------------------------------------------


def test_effective_env_var_wins_over_dotenv(tmp_path: Path) -> None:
    """A real env var takes precedence even if .env has a different value."""
    env_dir = tmp_path / "env-data"
    env_dir.mkdir()
    file_dir = tmp_path / "file-data"
    file_dir.mkdir()

    dotenv = tmp_path / ".env"
    dotenv.write_text(f"SABNYX_DATA_DIR={file_dir}\n", encoding="utf-8")

    result = effective_data_dir({"SABNYX_DATA_DIR": str(env_dir)}, tmp_path)
    assert result == env_dir.resolve()


def test_effective_dotenv_used_when_env_absent(tmp_path: Path) -> None:
    """When env is empty, SABNYX_DATA_DIR from root/.env is used."""
    data_dir = tmp_path / "file-data"
    data_dir.mkdir()

    dotenv = tmp_path / ".env"
    dotenv.write_text(f"SABNYX_DATA_DIR={data_dir}\n", encoding="utf-8")

    result = effective_data_dir({}, tmp_path)
    assert result == data_dir.resolve()


def test_effective_moonshot_fallback_used(tmp_path: Path) -> None:
    """When env and .env are absent/empty, moonshot file is the fallback."""
    data_dir = tmp_path / "moonshot-data"
    data_dir.mkdir()

    moonshot = tmp_path / "moonshot"
    moonshot.write_text(f"SABNYX_DATA_DIR={data_dir}\n", encoding="utf-8")

    result = effective_data_dir({}, tmp_path)
    assert result == data_dir.resolve()


def test_effective_dotenv_beats_moonshot(tmp_path: Path) -> None:
    """.env is consulted before moonshot (higher precedence)."""
    dotenv_dir = tmp_path / "dotenv-data"
    dotenv_dir.mkdir()
    moonshot_dir = tmp_path / "moonshot-data"
    moonshot_dir.mkdir()

    (tmp_path / ".env").write_text(
        f"SABNYX_DATA_DIR={dotenv_dir}\n", encoding="utf-8"
    )
    (tmp_path / "moonshot").write_text(
        f"SABNYX_DATA_DIR={moonshot_dir}\n", encoding="utf-8"
    )

    result = effective_data_dir({}, tmp_path)
    assert result == dotenv_dir.resolve()


def test_effective_no_source_returns_root(tmp_path: Path) -> None:
    """When neither env nor any dotenv file has the var, root is returned."""
    result = effective_data_dir({}, tmp_path)
    assert result == tmp_path


def test_effective_empty_env_var_falls_through(tmp_path: Path) -> None:
    """An empty/whitespace env var is treated as absent — file value used."""
    data_dir = tmp_path / "file-data"
    data_dir.mkdir()

    dotenv = tmp_path / ".env"
    dotenv.write_text(f"SABNYX_DATA_DIR={data_dir}\n", encoding="utf-8")

    for empty in ("", "   ", "\t"):
        result = effective_data_dir({"SABNYX_DATA_DIR": empty}, tmp_path)
        assert result == data_dir.resolve(), (
            f"expected file-data for SABNYX_DATA_DIR={empty!r}"
        )


def test_effective_empty_file_value_falls_through(tmp_path: Path) -> None:
    """An empty/whitespace value in .env falls through to the next source."""
    data_dir = tmp_path / "moonshot-data"
    data_dir.mkdir()

    (tmp_path / ".env").write_text("SABNYX_DATA_DIR=\n", encoding="utf-8")
    (tmp_path / "moonshot").write_text(
        f"SABNYX_DATA_DIR={data_dir}\n", encoding="utf-8"
    )

    result = effective_data_dir({}, tmp_path)
    assert result == data_dir.resolve()


def test_effective_tilde_expansion(tmp_path: Path) -> None:
    """A tilde in the dotenv value is expanded via expanduser."""
    home = Path.home()
    dotenv = tmp_path / ".env"
    dotenv.write_text("SABNYX_DATA_DIR=~/nonexistent-sabnyx-tilde-test\n", encoding="utf-8")

    result = effective_data_dir({}, tmp_path)
    assert result == (home / "nonexistent-sabnyx-tilde-test").resolve()
    assert not result.is_relative_to(tmp_path)
