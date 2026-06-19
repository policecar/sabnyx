// Locate the sabnyx code root (SPEC.md) and the data root (where the ledger
// lives) — mirrors paths.py::repo_root() and paths.py::effective_data_dir().

use std::path::{Path, PathBuf};

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
pub fn data_root() -> PathBuf {
    // 1. real env var
    if let Ok(v) = std::env::var("SABNYX_DATA_DIR") {
        if !v.trim().is_empty() {
            return resolve(v.trim());
        }
    }

    // 2. .env in the code root (real env var was absent/empty)
    let code_root = require();
    if let Some(v) = dotenv_value(&code_root.join(".env"), "SABNYX_DATA_DIR") {
        if !v.trim().is_empty() {
            return resolve(v.trim());
        }
    }

    // 3. default
    code_root
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
        std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from(raw))
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
fn parse_dotenv_value(text: &str, key: &str) -> Option<String> {
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
        return Some(value.to_string());
    }
    None
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
        assert_eq!(parse_dotenv_value(text, "OTHER"), Some("single".to_string()));
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
        assert!(r.is_absolute(), "relative path must be made absolute: {:?}", r);
        assert!(r.ends_with("rel/dir"));
    }
}
