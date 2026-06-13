// Locate the sabnyx repo root by walking up for SPEC.md — mirrors paths.py::repo_root().

use std::path::PathBuf;

/// Walk up from cwd until we find a directory containing SPEC.md.
/// Returns None if not found (caller should report an error).
pub fn find() -> Option<PathBuf> {
    let cwd = std::env::current_dir().ok()?;
    let mut dir: &std::path::Path = &cwd;
    loop {
        if dir.join("SPEC.md").is_file() {
            return Some(dir.to_path_buf());
        }
        match dir.parent() {
            Some(parent) => dir = parent,
            None => return None,
        }
    }
}

/// Like `find()` but panics with a clear message if not found.
pub fn require() -> PathBuf {
    find().expect(
        "Could not locate sabnyx repo root (no SPEC.md found). \
        Run opsctl from inside the sabnyx repository.",
    )
}
