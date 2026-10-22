"""fm.write must be crash-safe: a mid-write failure must never leave the
target file truncated/corrupt, and must never leave a stray temp file
behind. Mode bits on an existing target must survive the rewrite too —
callers (promote_file, reweave) rely on the vault's files keeping whatever
permissions the operator set, not silently reverting to the process umask.
"""

from __future__ import annotations

import os

import pytest

from sabnyx import fm


def test_write_creates_file_with_frontmatter(tmp_path):
    p = tmp_path / "new.md"
    fm.write(p, {"type": "concept", "description": "x" * 30}, "# T\n\nbody\n")
    assert p.exists()
    assert "type: concept" in p.read_text()


def test_crash_mid_write_leaves_original_intact_and_no_stray_tmp(tmp_path, monkeypatch):
    p = tmp_path / "existing.md"
    original = "---\ntype: concept\ndescription: original content here, long enough\n---\n\n# Old\n"
    p.write_text(original, encoding="utf-8")

    real_dumps = fm.frontmatter.dumps

    def _boom(*args, **kwargs):
        # Simulate a crash that happens AFTER the temp file has some partial
        # content written to disk but before the write completes.
        raise OSError("simulated crash mid-write")

    monkeypatch.setattr(fm.frontmatter, "dumps", _boom)

    with pytest.raises(OSError, match="simulated crash mid-write"):
        fm.write(p, {"type": "concept", "description": "new content, long enough"}, "# New\n")

    monkeypatch.setattr(fm.frontmatter, "dumps", real_dumps)

    # Original file untouched.
    assert p.read_text(encoding="utf-8") == original

    # No stray temp file left in the directory.
    leftover = [f for f in os.listdir(tmp_path) if f != "existing.md"]
    assert leftover == [], f"stray temp file(s) left behind: {leftover}"


def test_crash_after_tmp_write_before_replace_leaves_original_intact(tmp_path, monkeypatch):
    """A crash AFTER the temp file is fully written (partial content already
    flushed to disk) but BEFORE os.replace swaps it in — the harder case,
    since the temp file genuinely exists with content at the failure point."""
    p = tmp_path / "existing.md"
    original = "---\ntype: concept\ndescription: original content here, long enough\n---\n\n# Old\n"
    p.write_text(original, encoding="utf-8")

    real_replace = fm.os.replace

    def _boom_replace(*args, **kwargs):
        raise OSError("simulated crash before replace")

    monkeypatch.setattr(fm.os, "replace", _boom_replace)

    with pytest.raises(OSError, match="simulated crash before replace"):
        fm.write(p, {"type": "concept", "description": "new content, long enough"}, "# New\n")

    monkeypatch.setattr(fm.os, "replace", real_replace)

    assert p.read_text(encoding="utf-8") == original
    leftover = [f for f in os.listdir(tmp_path) if f != "existing.md"]
    assert leftover == [], f"stray temp file(s) left behind: {leftover}"


def test_write_preserves_mode_bits_of_existing_file(tmp_path):
    p = tmp_path / "secret.md"
    p.write_text("---\ntype: concept\ndescription: x\n---\n\nbody\n", encoding="utf-8")
    os.chmod(p, 0o600)

    fm.write(p, {"type": "concept", "description": "updated, long enough for schema"}, "new body\n")

    mode = p.stat().st_mode & 0o777
    assert mode == 0o600, f"expected mode 0o600, got {oct(mode)}"


def test_write_result_survives_os_replace_is_atomic_rename(tmp_path):
    """A reader that opened the path before the write keeps seeing the OLD
    inode's content (os.replace swaps the directory entry, it never mutates
    the file an existing fd points at) — the atomicity os.replace buys over
    a plain write_text."""
    p = tmp_path / "atomic.md"
    p.write_text("---\ntype: concept\ndescription: original, long enough text\n---\n\nold\n")
    old_inode = p.stat().st_ino

    fd = os.open(p, os.O_RDONLY)
    try:
        fm.write(p, {"type": "concept", "description": "new, long enough text here"}, "new\n")
        old_fd_content = os.pread(fd, 4096, 0).decode()
        assert "old" in old_fd_content
        assert "new" not in old_fd_content
    finally:
        os.close(fd)

    assert p.stat().st_ino != old_inode
    assert "new" in p.read_text()


def test_write_new_file_honors_process_umask(tmp_path):
    """A brand-new file (no existing target to preserve mode from) must get
    the SAME mode `path.write_text` would have given it: 0o666 & ~umask, via
    the kernel's own umask application at file-creation time — not whatever
    fixed mode tempfile.NamedTemporaryFile happens to default to (0o600),
    which would silently tighten every new intake/atlas file on the server."""
    prev_umask = os.umask(0o022)
    try:
        p = tmp_path / "brand_new.md"
        fm.write(p, {"type": "concept", "description": "x" * 30}, "body\n")
        mode = p.stat().st_mode & 0o777
        assert mode == 0o666 & ~0o022, f"expected mode 0o644, got {oct(mode)}"
    finally:
        os.umask(prev_umask)
