// @decision DEC-001: Canonical JSON matches Python's json.dumps(sort_keys=True, separators=(",",":"))
// @rationale: Byte-for-byte compatible hash requires identical canonicalization. Python defaults to
//   ensure_ascii=True, which escapes non-ASCII as \uXXXX. We replicate this with a custom serializer.
//
// @decision DEC-003: Only `payload` is hashed; `ts` is an opaque RFC3339 string.
// @rationale: The content_hash commits to payload bytes only — never `ts` or any other envelope
//   field. So the two writers need only agree on payload canonicalization, NOT on timestamp
//   formatting. `new_event` emits microsecond precision (to_rfc3339_opts(Micros, true)) to match
//   pydantic's serialization in the common case; the zero-microsecond edge may differ, which is
//   harmless precisely because `ts` is excluded from the hash and only compared lexicographically.
//   Do NOT fold `ts` into the hash without making both writers emit byte-identical timestamps.
//
// @decision DEC-004: Hashed payloads must not contain floats.
// @rationale: Python's json.dumps and serde_json stringify floats differently at scientific-notation
//   magnitudes (1e+20 vs 100000000000000000000.0), so a float would silently break hash parity. The
//   Python side enforces this (ledger._reject_floats); opsctl's own task.* payloads are ints/strings.

use chrono::{SecondsFormat, Utc};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::fmt::Write as FmtWrite;
use std::fs::{File, OpenOptions};
use std::io::{self, BufRead, BufReader, Write as IoWrite};
use std::path::Path;

/// One ledger record — field order mirrors Python's LedgerEvent for stable JSON serialization.
/// Fields must appear in this exact order: ts, plane, kind, actor, subject, payload, content_hash.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Event {
    pub ts: String,
    pub plane: String,
    pub kind: String,
    pub actor: String,
    pub subject: String,
    pub payload: Value,
    pub content_hash: String,
}

/// Serialize a JSON value to canonical form matching Python's:
///   json.dumps(obj, sort_keys=True, separators=(",",":"), ensure_ascii=True)
///
/// Rules:
/// - No whitespace (compact)
/// - Object keys sorted lexicographically (ascending, like Python dict sort)
/// - Recursive: nested objects also sorted
/// - Strings: non-ASCII characters escaped as \uXXXX (Python ensure_ascii default)
/// - Numbers, booleans, null: standard JSON representation
pub fn canonicalize(val: &Value) -> String {
    let mut out = String::new();
    write_canonical(val, &mut out);
    out
}

fn write_canonical(val: &Value, out: &mut String) {
    match val {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => out.push_str(&n.to_string()),
        Value::String(s) => {
            out.push('"');
            write_json_string(s, out);
            out.push('"');
        }
        Value::Array(arr) => {
            out.push('[');
            for (i, v) in arr.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_canonical(v, out);
            }
            out.push(']');
        }
        Value::Object(map) => {
            out.push('{');
            // Sort keys — serde_json's Map preserves insertion order by default;
            // we must sort explicitly to match Python's sort_keys=True.
            let mut pairs: Vec<(&String, &Value)> = map.iter().collect();
            pairs.sort_by_key(|(k, _)| k.as_str());
            for (i, (k, v)) in pairs.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                out.push('"');
                write_json_string(k, out);
                out.push('"');
                out.push(':');
                write_canonical(v, out);
            }
            out.push('}');
        }
    }
}

/// Write a string with JSON escaping, replicating Python's ensure_ascii=True:
/// - Standard escapes: \", \\, \n, \r, \t, \b, \f
/// - Control chars < 0x20: \uXXXX
/// - Non-ASCII (>= 0x80): \uXXXX for BMP, \uXXXX\uXXXX surrogate pair for supplementary
fn write_json_string(s: &str, out: &mut String) {
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\x08' => out.push_str("\\b"),
            '\x0C' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => {
                // Other control characters
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c if (c as u32) < 0x80 => {
                // ASCII printable — emit as-is
                out.push(c);
            }
            c => {
                // Non-ASCII: Python ensure_ascii=True uses \uXXXX for BMP chars
                // and \uXXXX\uXXXX (surrogate pair) for supplementary chars.
                let cp = c as u32;
                if cp <= 0xFFFF {
                    let _ = write!(out, "\\u{:04x}", cp);
                } else {
                    // Surrogate pair encoding for supplementary characters
                    let cp = cp - 0x10000;
                    let high = 0xD800 + (cp >> 10);
                    let low = 0xDC00 + (cp & 0x3FF);
                    let _ = write!(out, "\\u{:04x}\\u{:04x}", high, low);
                }
            }
        }
    }
}

/// Compute `sha256:<hex>` of the canonical JSON of payload — byte-compatible with Python's
///   `ledger.content_hash(payload)`.
pub fn content_hash(payload: &Value) -> String {
    let canonical = canonicalize(payload);
    let mut hasher = Sha256::new();
    hasher.update(canonical.as_bytes());
    let result = hasher.finalize();
    format!("sha256:{}", hex::encode(result))
}

/// Append one event to the ledger at `events_path`. Uses O_APPEND for POSIX atomicity.
pub fn append(events_path: &Path, event: &Event) -> io::Result<()> {
    // Ensure parent directory exists
    if let Some(parent) = events_path.parent() {
        std::fs::create_dir_all(parent)?;
    }

    // Serialize with field order preserved (serde struct field order)
    let line = serde_json::to_string(event).map_err(io::Error::other)?;

    let mut f = OpenOptions::new()
        .create(true)
        .append(true)
        .open(events_path)?;

    writeln!(f, "{}", line)?;
    f.flush()?;
    Ok(())
}

/// Read and parse every non-empty line from `events_path`. Returns all valid events.
pub fn replay(events_path: &Path) -> io::Result<Vec<Event>> {
    if !events_path.exists() {
        return Ok(vec![]);
    }

    let f = File::open(events_path)?;
    let reader = BufReader::new(f);
    let mut events = Vec::new();

    for line in reader.lines() {
        let line = line?;
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let event: Event = serde_json::from_str(line)
            .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
        events.push(event);
    }

    Ok(events)
}

/// Build a new Event with current UTC timestamp (microsecond precision, `Z`).
/// Matches pydantic's serialization (see DEC-003); `ts` is excluded from the
/// hash. The caller must provide the content_hash.
pub fn new_event(plane: &str, kind: &str, actor: &str, subject: &str, payload: Value) -> Event {
    let ts = Utc::now().to_rfc3339_opts(SecondsFormat::Micros, true);
    let ch = content_hash(&payload);
    Event {
        ts,
        plane: plane.to_string(),
        kind: kind.to_string(),
        actor: actor.to_string(),
        subject: subject.to_string(),
        payload,
        content_hash: ch,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::process::Command;
    use tempfile::NamedTempFile;

    // ---------------------------------------------------------------------------
    // Hardcoded canonical + hash tests
    // These expected values were generated by running the Python implementation:
    //   python -c "from sabnyx.ledger import content_hash, _canonical; ..."
    // ---------------------------------------------------------------------------

    #[test]
    fn test_canonical_task_tend() {
        let payload = json!({"task": "tend", "status": "started"});
        // Python: json.dumps({"task":"tend","status":"started"}, sort_keys=True, separators=(",",":"))
        // => '{"status":"started","task":"tend"}'
        assert_eq!(
            canonicalize(&payload),
            r#"{"status":"started","task":"tend"}"#
        );
    }

    #[test]
    fn test_canonical_nested() {
        let payload = json!({"a": 1, "b": [1, 2, 3], "nested": {"z": 1, "y": 2}});
        // Python canonical: '{"a":1,"b":[1,2,3],"nested":{"y":2,"z":1}}'
        assert_eq!(
            canonicalize(&payload),
            r#"{"a":1,"b":[1,2,3],"nested":{"y":2,"z":1}}"#
        );
    }

    #[test]
    fn test_canonical_empty() {
        let payload = json!({});
        assert_eq!(canonicalize(&payload), "{}");
    }

    #[test]
    fn test_hash_task_tend() {
        let payload = json!({"task": "tend", "status": "started"});
        // Expected: sha256:a1f2b81593a4865bd65cc621c82d60b87042fd86963fe94de77ccbe21093d2fe
        assert_eq!(
            content_hash(&payload),
            "sha256:a1f2b81593a4865bd65cc621c82d60b87042fd86963fe94de77ccbe21093d2fe"
        );
    }

    #[test]
    fn test_hash_nested() {
        let payload = json!({"a": 1, "b": [1, 2, 3], "nested": {"z": 1, "y": 2}});
        // Expected: sha256:7b72446e66532f78b25981b5ac9df976562d763d83d37f4436ce673289b8c4d4
        assert_eq!(
            content_hash(&payload),
            "sha256:7b72446e66532f78b25981b5ac9df976562d763d83d37f4436ce673289b8c4d4"
        );
    }

    #[test]
    fn test_hash_empty() {
        let payload = json!({});
        // Expected: sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a
        assert_eq!(
            content_hash(&payload),
            "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
        );
    }

    // ---------------------------------------------------------------------------
    // Cross-language compatibility test
    // Shells out to Python via `uv run python -c ...` to verify hash parity.
    // ---------------------------------------------------------------------------

    fn python_hash(json_str: &str) -> Option<String> {
        // Find repo root (SPEC.md) to invoke uv from the right directory
        let mut dir = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        dir = dir.parent().unwrap().to_path_buf(); // opsctl/ -> repo root

        let script =
            "import json,sys; from sabnyx.ledger import content_hash; print(content_hash(json.loads(sys.argv[1])), end='')";
        let output = Command::new("uv")
            .args(["run", "python", "-c", script, json_str])
            .current_dir(&dir)
            .output()
            .ok()?;

        if output.status.success() {
            Some(String::from_utf8(output.stdout).ok()?.trim().to_string())
        } else {
            eprintln!("python stderr: {}", String::from_utf8_lossy(&output.stderr));
            None
        }
    }

    #[test]
    fn test_compat_task_tend() {
        let payload = json!({"task": "tend", "status": "started"});
        let py_hash = python_hash(r#"{"task":"tend","status":"started"}"#)
            .expect("Python must be available for compat test");
        assert_eq!(
            content_hash(&payload),
            py_hash,
            "Hash mismatch for task/tend payload"
        );
    }

    #[test]
    fn test_compat_nested() {
        let payload = json!({"a": 1, "b": [1, 2, 3], "nested": {"z": 1, "y": 2}});
        let py_hash = python_hash(r#"{"a":1,"b":[1,2,3],"nested":{"z":1,"y":2}}"#)
            .expect("Python must be available for compat test");
        assert_eq!(
            content_hash(&payload),
            py_hash,
            "Hash mismatch for nested payload"
        );
    }

    #[test]
    fn test_compat_empty() {
        let payload = json!({});
        let py_hash = python_hash("{}").expect("Python must be available for compat test");
        assert_eq!(
            content_hash(&payload),
            py_hash,
            "Hash mismatch for empty payload"
        );
    }

    #[test]
    fn test_compat_unicode() {
        // Non-ASCII must hash identically: Python's ensure_ascii=True escapes to
        // \uXXXX (and surrogate pairs for supplementary chars); write_json_string
        // replicates that. café = BMP, 🎉 = supplementary (surrogate pair).
        let payload = json!({"k": "café 日本語 🎉"});
        let py_hash = python_hash(r#"{"k":"café 日本語 🎉"}"#)
            .expect("Python must be available for compat test");
        assert_eq!(
            content_hash(&payload),
            py_hash,
            "Hash mismatch for non-ASCII payload — ensure_ascii escaping diverged"
        );
    }

    #[test]
    fn test_ts_is_microsecond_rfc3339() {
        // ts is opaque and excluded from the hash, but should match pydantic's
        // microsecond `...Z` format in the common (nonzero-micros) case (DEC-003).
        let e = new_event("event", "test", "opsctl", "subj", json!({}));
        assert!(e.ts.ends_with('Z'), "ts must use Z suffix, got {}", e.ts);
        // RFC3339 with microseconds: 'YYYY-MM-DDTHH:MM:SS.ffffffZ' (27 chars).
        assert_eq!(
            e.ts.len(),
            27,
            "ts must be microsecond precision, got {}",
            e.ts
        );
    }

    // ---------------------------------------------------------------------------
    // Round-trip test: append then replay
    // ---------------------------------------------------------------------------

    #[test]
    fn test_roundtrip_append_replay() {
        let tmp = NamedTempFile::new().expect("temp file");
        let path = tmp.path();

        let payload = json!({"label": "test", "attempt": 1});
        let event = new_event(
            "action",
            "task.started",
            "opsctl",
            "test/subject",
            payload.clone(),
        );
        let expected_hash = content_hash(&payload);
        assert_eq!(event.content_hash, expected_hash);

        append(path, &event).expect("append must succeed");

        let events = replay(path).expect("replay must succeed");
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].kind, "task.started");
        assert_eq!(events[0].plane, "action");
        assert_eq!(events[0].actor, "opsctl");
        assert_eq!(events[0].content_hash, expected_hash);
    }

    #[test]
    fn test_roundtrip_multiple_events() {
        let tmp = NamedTempFile::new().expect("temp file");
        let path = tmp.path();

        for i in 0..5 {
            let payload = json!({"i": i});
            let event = new_event("event", "test.event", "opsctl", "subject", payload);
            append(path, &event).expect("append");
        }

        let events = replay(path).expect("replay");
        assert_eq!(events.len(), 5);
        for (i, e) in events.iter().enumerate() {
            assert_eq!(e.payload["i"], i as i64);
        }
    }

    // ---------------------------------------------------------------------------
    // Parse real ledger lines (ops/events.jsonl)
    // ---------------------------------------------------------------------------

    #[test]
    fn test_parse_real_ledger() {
        let mut path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        path = path.parent().unwrap().to_path_buf(); // repo root
        path.push("ops/events.jsonl");

        if !path.exists() {
            // Acceptable if running in an environment without the vault
            return;
        }

        let events = replay(&path).expect("replay real ledger");
        assert!(!events.is_empty(), "Real ledger should have events");

        // Verify first line from the known ledger
        let first = &events[0];
        assert_eq!(first.kind, "intake.created");
        assert_eq!(first.plane, "event");
        assert_eq!(first.actor, "sabnyx-cli");
    }
}
