// verify subcommand: replay the ledger and recompute every content_hash.
//
// A hash that is written but never checked is inert: this restores the spec's
// "integrity verification" intent for the ledger. Unlike replay(), a bad line
// does not abort the pass — every parse failure and hash mismatch is reported
// with its 1-based line number, and the command exits nonzero if any is found.

use crate::ledger::{content_hash, snippet, Event};
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::Path;

pub struct Outcome {
    /// Number of successfully parsed events whose hash was recomputed.
    pub checked: usize,
    /// One human-readable message per parse failure or hash mismatch.
    pub failures: Vec<String>,
}

/// Scan the whole ledger, recomputing content_hash for every event.
pub fn check(events_path: &Path) -> anyhow::Result<Outcome> {
    let mut checked = 0usize;
    let mut failures: Vec<String> = Vec::new();

    if !events_path.exists() {
        return Ok(Outcome { checked, failures });
    }

    let reader = BufReader::new(File::open(events_path)?);
    for (idx, line) in reader.lines().enumerate() {
        let lineno = idx + 1;
        let line = line?;
        let line = line.trim();
        if line.is_empty() {
            continue;
        }

        let event: Event = match serde_json::from_str(line) {
            Ok(e) => e,
            Err(e) => {
                failures.push(format!(
                    "line {}: parse failure: {} — line starts: {}",
                    lineno,
                    e,
                    snippet(line)
                ));
                continue;
            }
        };

        checked += 1;
        match content_hash(&event.payload) {
            Ok(recomputed) if recomputed == event.content_hash => {}
            Ok(recomputed) => {
                failures.push(format!(
                    "line {}: content_hash mismatch (kind={}, subject={}): recorded {} != recomputed {}",
                    lineno, event.kind, event.subject, event.content_hash, recomputed
                ));
            }
            Err(e) => {
                // e.g. a float in the payload (DEC-004) — unhashable on both sides.
                failures.push(format!("line {}: cannot recompute hash: {}", lineno, e));
            }
        }
    }

    Ok(Outcome { checked, failures })
}

/// Run the verification and print a report. Returns true if the ledger is clean.
pub fn run(events_path: &Path) -> anyhow::Result<bool> {
    let outcome = check(events_path)?;
    for failure in &outcome.failures {
        eprintln!("[opsctl verify] {}", failure);
    }
    println!(
        "verified {} event(s): {} failure(s)",
        outcome.checked,
        outcome.failures.len()
    );
    Ok(outcome.failures.is_empty())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ledger::{append, new_event};
    use serde_json::json;
    use std::io::Write;
    use tempfile::NamedTempFile;

    fn append_raw(path: &Path, raw: &str) {
        let mut f = std::fs::OpenOptions::new().append(true).open(path).unwrap();
        f.write_all(raw.as_bytes()).unwrap();
    }

    #[test]
    fn test_verify_clean_ledger() {
        let tmp = NamedTempFile::new().unwrap();
        let path = tmp.path();

        for i in 0..3 {
            let e = new_event("event", "test.event", "opsctl", "s", json!({"i": i})).unwrap();
            append(path, &e).unwrap();
        }

        let outcome = check(path).unwrap();
        assert_eq!(outcome.checked, 3);
        assert!(outcome.failures.is_empty(), "{:?}", outcome.failures);
    }

    #[test]
    fn test_verify_missing_file_is_clean() {
        let outcome = check(Path::new("/nonexistent/events.jsonl")).unwrap();
        assert_eq!(outcome.checked, 0);
        assert!(outcome.failures.is_empty());
    }

    #[test]
    fn test_verify_reports_hash_mismatch_with_line_number() {
        let tmp = NamedTempFile::new().unwrap();
        let path = tmp.path();

        let good = new_event("event", "ok.event", "opsctl", "s", json!({"a": 1})).unwrap();
        append(path, &good).unwrap();

        // Line 2: valid envelope, but the recorded hash does not match the payload.
        let mut tampered = new_event("event", "bad.event", "opsctl", "s", json!({"a": 2})).unwrap();
        tampered.content_hash =
            "sha256:0000000000000000000000000000000000000000000000000000000000000000".to_string();
        append(path, &tampered).unwrap();

        let outcome = check(path).unwrap();
        assert_eq!(outcome.checked, 2);
        assert_eq!(outcome.failures.len(), 1);
        assert!(
            outcome.failures[0].contains("line 2"),
            "{}",
            outcome.failures[0]
        );
        assert!(
            outcome.failures[0].contains("content_hash mismatch"),
            "{}",
            outcome.failures[0]
        );
    }

    #[test]
    fn test_verify_continues_past_malformed_lines() {
        let tmp = NamedTempFile::new().unwrap();
        let path = tmp.path();

        let good = new_event("event", "ok.event", "opsctl", "s", json!({})).unwrap();
        append(path, &good).unwrap();
        append_raw(path, "{not json at all\n");
        append(path, &good).unwrap();

        let outcome = check(path).unwrap();
        assert_eq!(
            outcome.checked, 2,
            "good lines around the bad one still verify"
        );
        assert_eq!(outcome.failures.len(), 1);
        assert!(
            outcome.failures[0].contains("line 2"),
            "{}",
            outcome.failures[0]
        );
        assert!(
            outcome.failures[0].contains("parse failure"),
            "{}",
            outcome.failures[0]
        );
    }
}
