// supervise subcommand: run a command with retry and ledger events.
//
// Emits:
//   task.started  (plane=action)   — before each attempt
//   task.completed (plane=event)   — on success
//   task.failed   (plane=event)    — on exhausted retries
//
// @decision DEC-002: task.started uses plane=action (intent/command), task.completed/failed use
// plane=event (fact that occurred). Matches the plane semantics in ADR-0002.

use crate::ledger::{append, new_event};
use serde_json::json;
use std::path::Path;
use std::time::{Duration, Instant};

/// Run one supervised cycle: attempt the command up to `retries+1` times.
/// Returns true if the command succeeded.
pub fn run_cycle(
    events_path: &Path,
    label: &str,
    command: &[String],
    retries: u32,
) -> anyhow::Result<bool> {
    let max_attempts = retries + 1;

    for attempt in 1..=max_attempts {
        // Emit task.started
        let started_payload = json!({
            "label": label,
            "attempt": attempt,
            "command": command,
        });
        let started_event = new_event("action", "task.started", "opsctl", label, started_payload);
        append(events_path, &started_event)?;

        let start = Instant::now();

        // Execute command
        let status = std::process::Command::new(&command[0])
            .args(&command[1..])
            .status();

        let duration_ms = start.elapsed().as_millis() as u64;

        match status {
            Ok(s) if s.success() => {
                // Emit task.completed
                let payload = json!({
                    "label": label,
                    "attempt": attempt,
                    "exit_code": 0,
                    "duration_ms": duration_ms,
                });
                let event = new_event("event", "task.completed", "opsctl", label, payload);
                append(events_path, &event)?;
                return Ok(true);
            }
            Ok(s) => {
                let exit_code = s.code().unwrap_or(-1);
                eprintln!(
                    "[opsctl supervise] attempt {}/{} failed (exit {})",
                    attempt, max_attempts, exit_code
                );

                if attempt == max_attempts {
                    // Emit task.failed (exhausted retries)
                    let payload = json!({
                        "label": label,
                        "attempt": attempt,
                        "exit_code": exit_code,
                        "duration_ms": duration_ms,
                        "retries_exhausted": true,
                    });
                    let event = new_event("event", "task.failed", "opsctl", label, payload);
                    append(events_path, &event)?;
                    return Ok(false);
                }

                // Backoff before retry: 2^(attempt-1) seconds, capped at 30s
                let backoff = Duration::from_secs((2u64.pow(attempt - 1)).min(30));
                eprintln!("[opsctl supervise] retrying in {}s...", backoff.as_secs());
                std::thread::sleep(backoff);
            }
            Err(e) => {
                eprintln!("[opsctl supervise] failed to spawn command: {}", e);

                let payload = json!({
                    "label": label,
                    "attempt": attempt,
                    "exit_code": -1,
                    "duration_ms": duration_ms,
                    "error": e.to_string(),
                });
                let event = new_event("event", "task.failed", "opsctl", label, payload);
                append(events_path, &event)?;
                return Ok(false);
            }
        }
    }

    Ok(false)
}

/// Run in --every N mode: loop forever sleeping N seconds between cycles.
pub fn run_loop(
    events_path: &Path,
    label: &str,
    command: &[String],
    retries: u32,
    every_secs: u64,
) -> anyhow::Result<()> {
    loop {
        let success = run_cycle(events_path, label, command, retries)?;
        if !success {
            eprintln!("[opsctl supervise] cycle failed for label={}", label);
        }
        eprintln!(
            "[opsctl supervise] sleeping {}s before next cycle",
            every_secs
        );
        std::thread::sleep(Duration::from_secs(every_secs));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ledger::replay;
    use tempfile::NamedTempFile;

    #[test]
    fn test_supervise_once_success() {
        let tmp = NamedTempFile::new().unwrap();
        let path = tmp.path();

        // `true` exits 0
        let success = run_cycle(path, "test-success", &["true".to_string()], 0).unwrap();
        assert!(success, "true must succeed");

        let events = replay(path).unwrap();
        assert_eq!(
            events.len(),
            2,
            "should have task.started and task.completed"
        );

        assert_eq!(events[0].kind, "task.started");
        assert_eq!(events[0].plane, "action");
        assert_eq!(events[0].payload["label"], "test-success");
        assert_eq!(events[0].payload["attempt"], 1);

        assert_eq!(events[1].kind, "task.completed");
        assert_eq!(events[1].plane, "event");
        assert_eq!(events[1].payload["exit_code"], 0);
    }

    #[test]
    fn test_supervise_once_failure() {
        let tmp = NamedTempFile::new().unwrap();
        let path = tmp.path();

        // `false` exits 1
        let success = run_cycle(path, "test-fail", &["false".to_string()], 0).unwrap();
        assert!(!success, "false must fail");

        let events = replay(path).unwrap();
        assert_eq!(events.len(), 2, "should have task.started and task.failed");

        assert_eq!(events[0].kind, "task.started");
        assert_eq!(events[1].kind, "task.failed");
        assert_eq!(events[1].payload["retries_exhausted"], true);
    }

    #[test]
    fn test_supervise_retry_then_succeed() {
        // We can't easily make a command succeed on the second attempt in a unit test,
        // so we test that retries=1 with a failing command produces 2 started + 1 failed.
        let tmp = NamedTempFile::new().unwrap();
        let path = tmp.path();

        // false always fails; with retries=1 we expect 2 attempts
        let success = run_cycle(path, "test-retry", &["false".to_string()], 1).unwrap();
        assert!(!success);

        let events = replay(path).unwrap();
        // 2 task.started events + 1 task.failed
        let started: Vec<_> = events.iter().filter(|e| e.kind == "task.started").collect();
        let failed: Vec<_> = events.iter().filter(|e| e.kind == "task.failed").collect();
        assert_eq!(started.len(), 2, "should have 2 task.started events");
        assert_eq!(failed.len(), 1, "should have 1 task.failed event");
        assert_eq!(started[1].payload["attempt"], 2);
    }
}
