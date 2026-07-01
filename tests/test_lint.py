"""Tests for `sabnyx lint`: the --staged data-root gate and domains/ skipping.

`lint --staged` is the pre-commit hook SPEC §3 leans on; it must query the
DATA repo's git index (SABNYX_DATA_DIR split), not the code repo's.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime

from click.testing import CliRunner

from sabnyx import fm


def _patch_lint(monkeypatch, tmp_vault) -> None:
    """Point lint's module-level path constants at the tmp data root."""
    from sabnyx.commands import lint as lint_mod

    for name in ("DATA_ROOT", "VAULT", "OBSERVATIONS", "SOURCES"):
        monkeypatch.setattr(lint_mod, name, tmp_vault[name])


def _intake_meta(**overrides) -> dict:
    meta = {
        "type": "note",
        "description": "a deliberately long-enough description satisfying the 30-char minimum",
        "source": "manual",
        "status": "draft",
        "created": datetime.now(UTC).isoformat(),
        "source_agent": "manual",
    }
    meta.update(overrides)
    return meta


def _run_lint(args):
    from sabnyx.commands import lint as lint_mod

    return CliRunner().invoke(lint_mod.lint_cmd, args)


class TestStagedUsesDataRoot:
    def test_staged_finds_vault_file_in_data_repo(self, tmp_vault, monkeypatch):
        """With ROOT != DATA_ROOT, --staged must query the data repo's index."""
        _patch_lint(monkeypatch, tmp_vault)
        data_root = tmp_vault["DATA_ROOT"]
        subprocess.run(["git", "init", "-q"], cwd=data_root, check=True)

        p = tmp_vault["INTAKE"] / "staged-note.md"
        fm.write(p, _intake_meta(), "# staged\n\nbody\n")
        subprocess.run(["git", "add", "vault/intake/staged-note.md"], cwd=data_root, check=True)

        result = _run_lint(["--staged"])
        assert result.exit_code == 0, result.output
        # the gate saw the file — it is not the "no vault .md files" no-op
        assert "no vault .md files" not in result.output
        assert "1 file(s)" in result.output

    def test_staged_blocks_violation_in_data_repo(self, tmp_vault, monkeypatch):
        _patch_lint(monkeypatch, tmp_vault)
        data_root = tmp_vault["DATA_ROOT"]
        subprocess.run(["git", "init", "-q"], cwd=data_root, check=True)

        # Gate 2 violation: description below the 30-char minimum.
        p = tmp_vault["INTAKE"] / "bad-note.md"
        fm.write(p, _intake_meta(description="too short"), "# bad\n")
        subprocess.run(["git", "add", "vault/intake/bad-note.md"], cwd=data_root, check=True)

        result = _run_lint(["--staged"])
        assert result.exit_code == 1, result.output
        assert "failed lint" in result.output


class TestDomainsSkipped:
    def test_domains_file_does_not_fail_lint(self, tmp_vault, monkeypatch):
        """domains/ carries its own per-topic schema (SPEC §1): skip, don't fail."""
        _patch_lint(monkeypatch, tmp_vault)

        d = tmp_vault["VAULT"] / "domains" / "chanoyu"
        d.mkdir(parents=True)
        (d / "wabi.md").write_text("---\nkind: aesthetic\n---\n# wabi\n", encoding="utf-8")
        fm.write(tmp_vault["INTAKE"] / "fine.md", _intake_meta(), "# fine\n\nbody\n")

        result = _run_lint([])
        assert result.exit_code == 0, result.output
        assert "skipped" in result.output
        assert "lint OK" in result.output
