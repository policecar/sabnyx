// Locate the sabnyx code root (SPEC.md) and the data root (where the ledger
// lives) — mirrors paths.py::repo_root() and paths.py::effective_data_dir().

use std::path::{Path, PathBuf};

/// Walk up from `start` until we find a directory containing SPEC.md.
fn find_from(start: &Path) -> Option<PathBuf> {
    let mut dir = start;
    loop {
        if dir.join("SPEC.md").is_file() {
            return Some(dir.to_path_buf());
        }
        dir = dir.parent()?;
    }
}

/// Locate the repo root (SPEC.md marker), preferring the binary's own location.
///
/// @decision DEC-005: Walk up from the executable's path before falling back to cwd.
/// @rationale: Python resolves the root from the installed module file
///   (paths.repo_root walks up from __file__), never from cwd. The opsctl binary
///   lives at <repo>/opsctl/target/{debug,release}/opsctl, so walking up from
///   std::env::current_exe reaches the same repo regardless of the working
///   directory — the documented cron invocation runs with cwd=$HOME, where a
///   cwd-only walk finds nothing (and running inside a *different* checkout
///   containing a SPEC.md would silently adopt that repo's root). cwd remains a
///   fallback for a binary installed outside the repo (e.g. ~/.cargo/bin).
pub fn find() -> Option<PathBuf> {
    if let Ok(exe) = std::env::current_exe() {
        // Canonicalize so a symlinked binary resolves to its real location,
        // like Python's Path(__file__).resolve().
        let exe = exe.canonicalize().unwrap_or(exe);
        if let Some(root) = find_from(&exe) {
            return Some(root);
        }
    }
    let cwd = std::env::current_dir().ok()?;
    find_from(&cwd)
}

/// Like `find()` but returns a clean error (not a panic) if no root is found,
/// so `main` exits nonzero with a message instead of a backtrace.
pub fn require() -> anyhow::Result<PathBuf> {
    find().ok_or_else(|| {
        anyhow::anyhow!(
            "could not locate the sabnyx repo root: no SPEC.md found above the \
             opsctl binary or the current directory. Run opsctl from inside the \
             sabnyx repository, or set SABNYX_DATA_DIR."
        )
    })
}

/// Resolve the data root — where `ops/events.jsonl` and `vault/` live.
///
/// Mirrors Python's `paths.effective_data_dir` so opsctl and the Python CLI
/// always agree on which ledger file to use. Resolution order (first non-empty
/// wins):
///   1. `SABNYX_DATA_DIR` from the real environment.
///   2. `SABNYX_DATA_DIR` from `<code-root>/.env`.
///   3. the code root (SPEC.md), unchanged.
///
/// Without this, `opsctl supervise` would append its `task.*` events to the
/// code repo's `ops/events.jsonl` while the Python side writes to the separate
/// data repo's — splitting the ledger into two files (the documented
/// "separate private data repo" deployment).
pub fn data_root() -> anyhow::Result<PathBuf> {
    // 1. real env var
    if let Ok(v) = std::env::var("SABNYX_DATA_DIR") {
        if !v.trim().is_empty() {
            return Ok(resolve(v.trim()));
        }
    }

    // 2. .env in the code root (real env var was absent/empty)
    let code_root = require()?;
    if let Some(v) = dotenv_value(&code_root.join(".env"), "SABNYX_DATA_DIR") {
        if !v.trim().is_empty() {
            return Ok(resolve(v.trim()));
        }
    }

    // 3. default
    Ok(code_root)
}

/// Expand a leading `~` and make the path absolute (relative to cwd). Does not
/// canonicalize — the directory may not exist yet, matching Python's
/// `Path(...).expanduser().resolve()` tolerance of missing paths.
fn resolve(raw: &str) -> PathBuf {
    let expanded: PathBuf = if let Some(rest) = raw.strip_prefix("~/") {
        match std::env::var_os("HOME") {
            Some(home) => Path::new(&home).join(rest),
            None => PathBuf::from(raw),
        }
    } else if raw == "~" {
        std::env::var_os("HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from(raw))
    } else {
        PathBuf::from(raw)
    };

    if expanded.is_absolute() {
        expanded
    } else {
        std::env::current_dir()
            .map(|cwd| cwd.join(&expanded))
            .unwrap_or(expanded)
    }
}

/// Read one KEY=VALUE from a .env file. Mirrors Python's `_dotenv.parse_dotenv`:
/// skips blanks and `#` comments, drops a leading `export `, strips matching
/// surrounding single/double quotes. Returns None if the file or key is absent.
fn dotenv_value(path: &Path, key: &str) -> Option<String> {
    let text = std::fs::read_to_string(path).ok()?;
    parse_dotenv_value(&text, key)
}

/// Pure parser half of `dotenv_value`, split out so it is unit-testable.
///
/// On duplicate keys the *last* occurrence wins, matching Python's
/// `parse_dotenv` (a dict assignment per line, so later lines overwrite
/// earlier ones). Returning the first match here would silently split the
/// ledger when a user appends a new SABNYX_DATA_DIR without deleting the old
/// line — the exact failure this module exists to prevent.
fn parse_dotenv_value(text: &str, key: &str) -> Option<String> {
    let mut found: Option<String> = None;
    for raw in text.lines() {
        let mut line = raw.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        if let Some(rest) = line.strip_prefix("export ") {
            line = rest.trim_start();
        }
        let Some((k, v)) = line.split_once('=') else {
            continue;
        };
        if k.trim() != key {
            continue;
        }
        let mut value = v.trim();
        if value.len() >= 2 {
            let bytes = value.as_bytes();
            let first = bytes[0];
            let last = bytes[value.len() - 1];
            if first == last && (first == b'"' || first == b'\'') {
                value = &value[1..value.len() - 1];
            }
        }
        found = Some(value.to_string());
    }
    found
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_dotenv_value_basic() {
        let text = "# comment\nFOO=bar\nSABNYX_DATA_DIR=/data/vault\n";
        assert_eq!(
            parse_dotenv_value(text, "SABNYX_DATA_DIR"),
            Some("/data/vault".to_string())
        );
        assert_eq!(parse_dotenv_value(text, "FOO"), Some("bar".to_string()));
        assert_eq!(parse_dotenv_value(text, "MISSING"), None);
    }

    #[test]
    fn test_dotenv_value_quotes_and_export() {
        let text = "export SABNYX_DATA_DIR=\"/quoted/path\"\nOTHER='single'\n";
        assert_eq!(
            parse_dotenv_value(text, "SABNYX_DATA_DIR"),
            Some("/quoted/path".to_string())
        );
        assert_eq!(
            parse_dotenv_value(text, "OTHER"),
            Some("single".to_string())
        );
    }

    #[test]
    fn test_dotenv_value_skips_blank_and_comment() {
        let text = "\n\n   \n# SABNYX_DATA_DIR=ignored\nSABNYX_DATA_DIR=real\n";
        assert_eq!(
            parse_dotenv_value(text, "SABNYX_DATA_DIR"),
            Some("real".to_string())
        );
    }

    #[test]
    fn test_dotenv_value_duplicate_key_last_wins() {
        // Python's parse_dotenv is a dict assignment per line: the last
        // occurrence of a key wins. The Rust side must agree, or the two
        // writers resolve different data roots (split ledger).
        let text = "SABNYX_DATA_DIR=/old/path\nOTHER=x\nSABNYX_DATA_DIR=/new/path\n";
        assert_eq!(
            parse_dotenv_value(text, "SABNYX_DATA_DIR"),
            Some("/new/path".to_string())
        );

        // A later *empty* assignment also wins (Python then falls through to
        // the default root because the parsed value is empty).
        let text = "SABNYX_DATA_DIR=/old/path\nSABNYX_DATA_DIR=\n";
        assert_eq!(
            parse_dotenv_value(text, "SABNYX_DATA_DIR"),
            Some("".to_string())
        );
    }

    #[test]
    fn test_find_from_exe_location() {
        // The test binary lives under <repo>/opsctl/target/..., so walking up
        // from the executable must find the repo root regardless of cwd.
        let exe = std::env::current_exe().expect("current_exe");
        let root = find_from(&exe).expect("SPEC.md above the test binary");
        assert!(root.join("SPEC.md").is_file());

        let expected = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .to_path_buf();
        assert_eq!(root, expected, "must resolve to this checkout's root");
    }

    #[test]
    fn test_find_from_unrelated_dir_is_none() {
        // No SPEC.md anywhere above a temp dir → None, and require() maps that
        // to a clean error (no expect-panic left on this path).
        let tmp = tempfile::tempdir().expect("tempdir");
        assert_eq!(find_from(tmp.path()), None);
    }

    #[test]
    fn test_require_succeeds_without_panic() {
        // In the test environment the exe lives inside the repo, so require()
        // must return Ok — and by construction it can no longer panic.
        let root = require().expect("repo root");
        assert!(root.join("SPEC.md").is_file());
    }

    #[test]
    fn test_resolve_absolute_unchanged() {
        assert_eq!(resolve("/abs/path"), PathBuf::from("/abs/path"));
    }

    #[test]
    fn test_resolve_tilde_uses_home() {
        // Only assert behavior when HOME is set in the test environment.
        if let Some(home) = std::env::var_os("HOME") {
            assert_eq!(resolve("~/vault"), Path::new(&home).join("vault"));
        }
    }

    #[test]
    fn test_resolve_relative_is_absolutized() {
        let r = resolve("rel/dir");
        assert!(
            r.is_absolute(),
            "relative path must be made absolute: {:?}",
            r
        );
        assert!(r.ends_with("rel/dir"));
    }
}
