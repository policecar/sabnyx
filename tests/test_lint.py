"""Tests for `sabnyx lint`: the --staged data-root gate and domains/ skipping.

`lint --staged` is the pre-commit hook SPEC §3 leans on; it must query the
DATA repo's git index (SABNYX_DATA_DIR split), not the code repo's.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
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


def git(cwd, *args) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


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

    def test_staged_fails_closed_when_git_diff_fails(self, tmp_vault, monkeypatch):
        _patch_lint(monkeypatch, tmp_vault)

        def failed(*args, **kwargs):
            return subprocess.CompletedProcess(args[0], 128, "", "fatal: not a git repository")

        monkeypatch.setattr(subprocess, "run", failed)
        result = _run_lint(["--staged"])
        assert result.exit_code == 1
        assert "could not inspect staged files" in result.output


# -- staged mode validates the index, not the working tree --------------------

_INVALID = "---\ntype: note\ndescription: bad\n---\nbody\n"


def _page(meta: dict, body: str = "# page\n\nbody\n") -> str:
    """The exact text fm.write produces for `meta` + `body`."""
    import frontmatter

    post = frontmatter.Post(body)
    post.metadata = dict(meta)
    return frontmatter.dumps(post) + "\n"


_VALID = _page(_intake_meta())
_ATLAS = _page(
    {
        "type": "concept",
        "description": "a deliberately long-enough description satisfying the 30-char minimum",
        "status": "final",
    }
)


def _isolate_git(monkeypatch, root) -> None:
    """Git with built-in defaults (core.quotePath on, no renames/hooks
    config from the operator's ~/.gitconfig), and never a parent repo."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(root.parent))


class TestStagedReadsIndex:
    """lint --staged validates the blob the commit will contain (R04)."""

    @pytest.fixture
    def repo(self, tmp_vault, monkeypatch):
        _patch_lint(monkeypatch, tmp_vault)
        root = tmp_vault["DATA_ROOT"]
        _isolate_git(monkeypatch, root)
        git(root, "init", "-q")
        git(root, "config", "user.email", "t@example.net")
        git(root, "config", "user.name", "t")
        return root

    @staticmethod
    def _stage(root, rel: str, text: str | bytes) -> Path:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, str):
            text = text.encode()
        p.write_bytes(text)
        git(root, "add", "--", rel)
        return p

    @staticmethod
    def _state(root) -> tuple[bytes, bytes]:
        """Index entries and working-tree status: lint must change neither."""
        return (
            git(root, "ls-files", "-s", "-z").stdout,
            git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout,
        )

    def test_invalid_staged_valid_unstaged_fails(self, repo):
        p = self._stage(repo, "vault/intake/staged-note.md", _INVALID)
        p.write_text(_VALID, encoding="utf-8")  # the correction, not staged
        before = self._state(repo)

        result = _run_lint(["--staged"])
        assert result.exit_code == 1, result.output
        assert "1 file(s) failed lint" in result.output
        assert "description" in result.output
        # lint read the index without touching it or the working tree
        assert self._state(repo) == before
        assert git(repo, "show", ":vault/intake/staged-note.md").stdout.decode() == _INVALID
        assert p.read_text(encoding="utf-8") == _VALID

    def test_valid_staged_invalid_unstaged_passes(self, repo):
        p = self._stage(repo, "vault/intake/staged-note.md", _VALID)
        p.write_text(_INVALID, encoding="utf-8")  # an unrelated, unstaged edit

        result = _run_lint(["--staged"])
        assert result.exit_code == 0, result.output
        assert "lint OK (1 file(s))" in result.output

    def test_staged_file_missing_from_worktree_is_checked(self, repo):
        p = self._stage(repo, "vault/intake/gone.md", _INVALID)
        p.unlink()

        result = _run_lint(["--staged"])
        assert result.exit_code == 1, result.output
        assert "1 file(s) failed lint" in result.output

    def test_tier_comes_from_index_path_not_worktree_symlink(self, repo):
        """The staged page is atlas-shaped but sits in intake/: invalid there.
        In the working tree the same name is a symlink to an atlas page with
        identical bytes; resolving it would classify (and pass) it as atlas."""
        p = self._stage(repo, "vault/intake/shifted.md", _ATLAS)
        target = repo / "vault" / "atlas" / "concepts" / "shifted.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_ATLAS, encoding="utf-8")
        p.unlink()
        p.symlink_to(target)

        result = _run_lint(["--staged"])
        assert result.exit_code == 1, result.output
        assert "1 file(s) failed lint" in result.output

    def test_unusual_names_are_checked(self, repo):
        """Space, quote, bracket and non-ASCII: --name-only C-quotes these
        ("vault/intake/caf\303\251 ..."), so the old line-split lookup found
        no such file and linted nothing. -z names reach the validator."""
        name = 'vault/intake/café "quoted" [draft].md'
        self._stage(repo, name, _INVALID)
        assert git(repo, "diff", "--cached", "--name-only").stdout.startswith(b'"')

        result = _run_lint(["--staged"])
        assert result.exit_code == 1, result.output
        assert "1 file(s) failed lint" in result.output
        assert "[draft]" in result.output  # printed as text, not eaten as markup

    def test_rename_and_copy_check_the_new_path(self, repo):
        """A rename checks the destination path's blob (here an atlas page
        moved into intake/, invalid there) and never reads the deleted side.
        A detected copy is checked like an addition."""
        self._stage(repo, "vault/atlas/concepts/moved.md", _ATLAS)
        self._stage(repo, "vault/intake/original.md", _INVALID)
        git(repo, "commit", "-q", "--no-verify", "-m", "base")
        git(repo, "config", "diff.renames", "copies")
        git(repo, "mv", "vault/atlas/concepts/moved.md", "vault/intake/moved.md")
        self._stage(repo, "vault/intake/copied.md", _INVALID)

        result = _run_lint(["--staged"])
        assert result.exit_code == 1, result.output
        assert "2 file(s) failed lint" in result.output
        assert "moved.md" in result.output
        assert "copied.md" in result.output

    def test_staged_deletion_is_not_read(self, repo):
        self._stage(repo, "vault/intake/doomed.md", _INVALID)
        git(repo, "commit", "-q", "--no-verify", "-m", "base")
        git(repo, "rm", "-q", "vault/intake/doomed.md")

        result = _run_lint(["--staged"])
        assert result.exit_code == 0, result.output
        assert "no vault .md files" in result.output

    def test_staged_symlink_fails(self, repo):
        """A symlink entry holds a link target, not a page: refuse it rather
        than validate whatever the link points to."""
        (repo / "vault" / "intake").mkdir(parents=True, exist_ok=True)
        (repo / "vault" / "intake" / "real.md").write_text(_VALID, encoding="utf-8")
        (repo / "vault" / "intake" / "link.md").symlink_to("real.md")
        git(repo, "add", "vault/intake/link.md")

        result = _run_lint(["--staged"])
        assert result.exit_code == 1, result.output
        assert "mode 120000" in result.output

    def test_undecodable_staged_blob_fails(self, repo):
        self._stage(
            repo,
            "vault/intake/latin1.md",
            b'---\ntype: note\ndescription: "caf\xe9 caf\xe9 caf\xe9 caf\xe9 caf\xe9"\n---\n',
        )
        result = _run_lint(["--staged"])
        assert result.exit_code == 1, result.output
        assert "parse error" in result.output

    def test_exclusions_hold_for_index_paths(self, repo):
        """sources/, _review/ and non-vault files are not linted; domains/ is
        skipped with a note, exactly as in working-tree mode."""
        self._stage(repo, "vault/sources/raw-capture.md", "no frontmatter\n")
        self._stage(repo, "vault/_review/pending.md", "scratch\n")
        self._stage(repo, "README.md", "not vault\n")
        self._stage(repo, "vault/domains/chanoyu/wabi.md", "---\nkind: aesthetic\n---\n")
        self._stage(repo, "vault/intake/fine.md", _VALID)

        result = _run_lint(["--staged"])
        assert result.exit_code == 0, result.output
        assert "1 file(s) skipped" in result.output
        assert "lint OK (2 file(s))" in result.output

    def test_missing_blob_fails_closed(self, repo):
        """An index entry whose object cannot be read is an error, never
        'no files to lint'."""
        self._stage(repo, "vault/intake/staged-note.md", _VALID)
        sha = git(repo, "rev-parse", ":vault/intake/staged-note.md").stdout.decode().strip()
        obj = repo / ".git" / "objects" / sha[:2] / sha[2:]
        obj.chmod(0o644)
        obj.unlink()

        result = _run_lint(["--staged"])
        assert result.exit_code == 1, result.output
        assert "could not inspect staged files" in result.output
        assert sha in result.output

    def test_not_a_repository_fails_closed(self, tmp_vault, monkeypatch):
        _patch_lint(monkeypatch, tmp_vault)
        _isolate_git(monkeypatch, tmp_vault["DATA_ROOT"])

        result = _run_lint(["--staged"])
        assert result.exit_code == 1, result.output
        assert "could not inspect staged files" in result.output
        assert "not a git repository" in result.output


def test_split_root_hook_checks_data_repo(tmp_path):
    """The installed script may live in the code repo, but Git invokes it
    while committing the split DATA repo; it must inspect and lint that index."""
    import os

    from sabnyx.paths import ROOT

    data = tmp_path / "data"
    intake = data / "vault" / "intake"
    intake.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=data, check=True)
    fm.write(intake / "invalid.md", _intake_meta(description="too short"), "# bad\n")
    subprocess.run(["git", "add", "vault/intake/invalid.md"], cwd=data, check=True)

    hook = data / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.symlink_to(ROOT / "ops" / "scripts" / "pre-commit.sh")
    env = {**os.environ, "SABNYX_DATA_DIR": str(data)}
    result = subprocess.run(
        ["bash", str(hook)],
        cwd=data,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "commit blocked" in result.stdout


@pytest.mark.parametrize(
    ("name", "staged", "unstaged", "blocked"),
    [
        ("vault/intake/staged-note.md", _INVALID, _VALID, True),
        ("vault/intake/staged-note.md", _VALID, _INVALID, False),
        # The only staged change has a C-quoted name: the old '^vault/' grep
        # over --name-only missed it and the hook exited 0 without linting.
        ("vault/intake/café note.md", _INVALID, None, True),
    ],
    ids=["invalid-staged-valid-unstaged", "valid-staged-invalid-unstaged", "non-ascii-name"],
)
def test_split_root_hook_validates_index_blobs(tmp_path, name, staged, unstaged, blocked):
    """End to end through the installed hook in a split DATA repo: Git's
    index decides, the working tree does not."""
    from sabnyx.paths import ROOT

    data = tmp_path / "data"
    data.mkdir()
    env = {
        **os.environ,
        "SABNYX_DATA_DIR": str(data),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CEILING_DIRECTORIES": str(tmp_path),
    }
    subprocess.run(["git", "init", "-q"], cwd=data, check=True, env=env)
    page = data / name
    page.parent.mkdir(parents=True)
    page.write_text(staged, encoding="utf-8")
    subprocess.run(["git", "add", "--", name], cwd=data, check=True, env=env)
    if unstaged is not None:
        page.write_text(unstaged, encoding="utf-8")

    hook = data / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.symlink_to(ROOT / "ops" / "scripts" / "pre-commit.sh")
    result = subprocess.run(["bash", str(hook)], cwd=data, env=env, capture_output=True, text=True)
    out = result.stdout + result.stderr
    if blocked:
        assert result.returncode == 1, out
        assert "commit blocked" in result.stdout
        assert "failed lint" in result.stdout
    else:
        assert result.returncode == 0, out
        assert "lint OK (1 file(s))" in result.stdout


def test_install_hooks_targets_split_data_repo(tmp_path):
    import os

    from sabnyx.paths import ROOT

    data = tmp_path / "data"
    data.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=data, check=True)
    env = {**os.environ, "SABNYX_DATA_DIR": str(data)}
    result = subprocess.run(
        ["bash", str(ROOT / "ops" / "scripts" / "install-hooks.sh")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    hook = data / ".git" / "hooks" / "pre-commit"
    assert hook.is_symlink()
    assert hook.resolve() == ROOT / "ops" / "scripts" / "pre-commit.sh"


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

    def test_undecodable_file_fails_not_skipped(self, tmp_vault, monkeypatch):
        """UnicodeDecodeError is a ValueError, but only classification may
        skip a file: an intake file that is not UTF-8 is a failure."""
        _patch_lint(monkeypatch, tmp_vault)
        (tmp_vault["INTAKE"] / "latin1.md").write_bytes(
            b'---\ntype: note\ndescription: "caf\xe9 caf\xe9 caf\xe9 caf\xe9 caf\xe9"\n---\n'
        )

        result = _run_lint([])
        assert result.exit_code == 1, result.output
        assert "skipped" not in result.output
        assert "parse error" in result.output
