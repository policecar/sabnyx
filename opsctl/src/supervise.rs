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
#[cfg(unix)]
use std::os::unix::process::ExitStatusExt;
use std::path::Path;
use std::time::{Duration, Instant};

/// Backoff before the next retry after failed attempt N (1-based):
/// 1, 2, 4, 8, 16, 30, 30, ... seconds. The shift exponent is capped *before*
/// the arithmetic so a huge `--retries` cannot overflow (2u64.pow(attempt-1)
/// panics in debug / wraps in release once attempt >= 65).
fn backoff_secs(attempt: u32) -> u64 {
    (1u64 << attempt.saturating_sub(1).min(5)).min(30)
}

/// The signal that killed the child, if any (Unix only; None elsewhere and
/// for normal exits). Keeps signal-kill distinguishable from `exit -1`.
#[cfg(unix)]
fn exit_signal(status: &std::process::ExitStatus) -> Option<i32> {
    status.signal()
}

#[cfg(not(unix))]
fn exit_signal(_status: &std::process::ExitStatus) -> Option<i32> {
    None
}

/// Run one supervised cycle: attempt the command up to `retries+1` times.
/// Returns true if the command succeeded.
pub fn run_cycle(
    events_path: &Path,
    label: &str,
    command: &[String],
    retries: u32,
) -> anyhow::Result<bool> {
    // saturating: `--retries 4294967295` must not overflow the attempt count.
    let max_attempts = retries.saturating_add(1);

    for attempt in 1..=max_attempts {
        // Emit task.started
        let started_payload = json!({
            "label": label,
            "attempt": attempt,
            "command": command,
        });
        let started_event = new_event("action", "task.started", "opsctl", label, started_payload)?;
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
                let event = new_event("event", "task.completed", "opsctl", label, payload)?;
                append(events_path, &event)?;
                return Ok(true);
            }
            Ok(s) => {
                // A signal-killed child has no exit code; record the signal
                // separately so it is not conflated with a real `exit -1`.
                let exit_code = s.code().unwrap_or(-1);
                let signal = exit_signal(&s);
                match signal {
                    Some(sig) => eprintln!(
                        "[opsctl supervise] attempt {}/{} failed (killed by signal {})",
                        attempt, max_attempts, sig
                    ),
                    None => eprintln!(
                        "[opsctl supervise] attempt {}/{} failed (exit {})",
                        attempt, max_attempts, exit_code
                    ),
                }

                if attempt == max_attempts {
                    // Emit task.failed (exhausted retries)
                    let mut payload = json!({
                        "label": label,
                        "attempt": attempt,
                        "exit_code": exit_code,
                        "duration_ms": duration_ms,
                        "retries_exhausted": true,
                    });
                    if let Some(sig) = signal {
                        payload["signal"] = json!(sig);
                    }
                    let event = new_event("event", "task.failed", "opsctl", label, payload)?;
                    append(events_path, &event)?;
                    return Ok(false);
                }

                // Backoff before retry: 1,2,4,8,16 then capped at 30s
                let backoff = Duration::from_secs(backoff_secs(attempt));
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
                let event = new_event("event", "task.failed", "opsctl", label, payload)?;
                append(events_path, &event)?;
                return Ok(false);
            }
        }
    }

    Ok(false)
}

/// Sleep needed to keep an N-second period anchored to the cycle *start*:
/// the remainder of the period after the cycle's own runtime, zero if the
/// cycle overran. Sleeping a full N *after* each cycle would drift the real
/// period to cycle + backoffs + N.
fn remaining_sleep(period: Duration, elapsed: Duration) -> Duration {
    period.saturating_sub(elapsed)
}

/// Run in --every N mode: loop forever, one cycle per N-second period
/// (anchored to cycle start). A failed cycle — including a failed ledger
/// append — is logged and does not kill the daemon: the supervisor must
/// outlive its own bookkeeping (e.g. a transient ENOSPC).
pub fn run_loop(
    events_path: &Path,
    label: &str,
    command: &[String],
    retries: u32,
    every_secs: u64,
) -> anyhow::Result<()> {
    let period = Duration::from_secs(every_secs);
    loop {
        let cycle_start = Instant::now();
        match run_cycle(events_path, label, command, retries) {
            Ok(true) => {}
            Ok(false) => {
                eprintln!("[opsctl supervise] cycle failed for label={}", label);
            }
            Err(e) => {
                eprintln!(
                    "[opsctl supervise] cycle error for label={} (continuing): {}",
                    label, e
                );
            }
        }
        let sleep = remaining_sleep(period, cycle_start.elapsed());
        eprintln!(
            "[opsctl supervise] sleeping {}s before next cycle",
            sleep.as_secs()
        );
        std::thread::sleep(sleep);
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

    #[test]
    fn test_backoff_secs_caps_without_overflow() {
        assert_eq!(backoff_secs(1), 1);
        assert_eq!(backoff_secs(2), 2);
        assert_eq!(backoff_secs(3), 4);
        assert_eq!(backoff_secs(4), 8);
        assert_eq!(backoff_secs(5), 16);
        assert_eq!(backoff_secs(6), 30); // 1<<5 = 32, capped at 30
        assert_eq!(backoff_secs(7), 30);
        // The old 2u64.pow(attempt-1) overflowed here (panic in debug builds).
        assert_eq!(backoff_secs(64), 30);
        assert_eq!(backoff_secs(65), 30);
        assert_eq!(backoff_secs(u32::MAX), 30);
    }

    #[test]
    fn test_remaining_sleep_anchors_to_cycle_start() {
        let period = Duration::from_secs(900);
        // Fast cycle: sleep the remainder of the period.
        assert_eq!(
            remaining_sleep(period, Duration::from_secs(300)),
            Duration::from_secs(600)
        );
        // Exact fit and overrun: no sleep, never negative/panicking.
        assert_eq!(remaining_sleep(period, period), Duration::ZERO);
        assert_eq!(
            remaining_sleep(period, Duration::from_secs(1200)),
            Duration::ZERO
        );
    }

    #[cfg(unix)]
    #[test]
    fn test_supervise_signal_killed_child_records_signal() {
        let tmp = NamedTempFile::new().unwrap();
        let path = tmp.path();

        // The child kills itself with SIGTERM: no exit code, signal 15.
        let cmd = vec![
            "sh".to_string(),
            "-c".to_string(),
            "kill -TERM $$".to_string(),
        ];
        let success = run_cycle(path, "test-signal", &cmd, 0).unwrap();
        assert!(!success, "signal-killed child is a failure");

        let events = replay(path).unwrap();
        let failed = events
            .iter()
            .find(|e| e.kind == "task.failed")
            .expect("task.failed event");
        assert_eq!(failed.payload["signal"], 15, "SIGTERM must be recorded");
        assert_eq!(
            failed.payload["exit_code"], -1,
            "no exit code for a signal-killed child"
        );
    }

    #[test]
    fn test_supervise_normal_failure_has_no_signal_field() {
        let tmp = NamedTempFile::new().unwrap();
        let path = tmp.path();

        run_cycle(path, "test-no-signal", &["false".to_string()], 0).unwrap();
        let events = replay(path).unwrap();
        let failed = events
            .iter()
            .find(|e| e.kind == "task.failed")
            .expect("task.failed event");
        assert!(
            failed.payload.get("signal").is_none(),
            "exit-code failures must not carry a signal field"
        );
    }
}
