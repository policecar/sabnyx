// Query subcommand: filter ledger events and display them.

use crate::ledger::{replay, Event};
use chrono::DateTime;
use std::path::Path;

/// Filter criteria for query.
pub struct QueryFilter {
    pub kind: Option<String>,
    pub actor: Option<String>,
    pub plane: Option<String>,
    pub since: Option<String>,
    pub limit: Option<usize>,
}

/// True if `ts` is at or after `since`.
///
/// Timestamps are compared as parsed instants, not lexicographically: the two
/// writers emit different precisions (pydantic drops `.000000` for
/// zero-microsecond times, opsctl always emits 6-digit micros), and a plain
/// string compare gets mixed precision wrong ('.' < 'Z', so
/// `12:00:00.500000Z` would sort *before* `12:00:00Z`). If either side is not
/// RFC3339, fall back to the lexicographic compare.
fn ts_at_or_after(ts: &str, since: &str) -> bool {
    match (
        DateTime::parse_from_rfc3339(ts),
        DateTime::parse_from_rfc3339(since),
    ) {
        (Ok(t), Ok(s)) => t >= s,
        _ => ts >= since,
    }
}

impl QueryFilter {
    fn matches(&self, event: &Event) -> bool {
        if let Some(k) = &self.kind {
            if !event.kind.contains(k.as_str()) {
                return false;
            }
        }
        if let Some(a) = &self.actor {
            if !event.actor.contains(a.as_str()) {
                return false;
            }
        }
        if let Some(p) = &self.plane {
            if event.plane != *p {
                return false;
            }
        }
        if let Some(since) = &self.since {
            if !ts_at_or_after(&event.ts, since) {
                return false;
            }
        }
        true
    }
}

/// Apply the filter, then `limit`. Limit keeps the *most recent* N matches
/// (the tail of the file-ordered ledger) — an operator asking for
/// `--limit 10` wants the latest ten, not the first ten ever.
fn select<'a>(all: &'a [Event], filter: &QueryFilter) -> Vec<&'a Event> {
    let mut matching: Vec<&Event> = all.iter().filter(|e| filter.matches(e)).collect();

    if let Some(limit) = filter.limit {
        if matching.len() > limit {
            matching.drain(..matching.len() - limit);
        }
    }

    matching
}

pub fn run(events_path: &Path, filter: QueryFilter, json_output: bool) -> anyhow::Result<()> {
    let all = replay(events_path)?;
    let matching = select(&all, &filter);

    if json_output {
        for e in &matching {
            println!("{}", serde_json::to_string(e)?);
        }
    } else {
        // Human-readable table
        print_table(&matching);
    }

    Ok(())
}

fn print_table(events: &[&Event]) {
    if events.is_empty() {
        println!("(no matching events)");
        return;
    }

    // Column widths
    let ts_w = 26;
    let plane_w = 10;
    let kind_w = events
        .iter()
        .map(|e| e.kind.len())
        .max()
        .unwrap_or(10)
        .max(4);
    let actor_w = events
        .iter()
        .map(|e| e.actor.len())
        .max()
        .unwrap_or(10)
        .max(5);

    println!(
        "{:<ts_w$}  {:<plane_w$}  {:<kind_w$}  {:<actor_w$}  subject",
        "ts",
        "plane",
        "kind",
        "actor",
        ts_w = ts_w,
        plane_w = plane_w,
        kind_w = kind_w,
        actor_w = actor_w,
    );
    println!(
        "{}",
        "-".repeat(ts_w + 2 + plane_w + 2 + kind_w + 2 + actor_w + 2 + 30)
    );

    for e in events {
        // Truncate ts to 26 chars (drop microseconds if present). Char-based,
        // not a byte slice: a corrupt multibyte ts must not panic the table.
        let ts_display: String = e.ts.chars().take(ts_w).collect();
        println!(
            "{:<ts_w$}  {:<plane_w$}  {:<kind_w$}  {:<actor_w$}  {}",
            ts_display,
            e.plane,
            e.kind,
            e.actor,
            e.subject,
            ts_w = ts_w,
            plane_w = plane_w,
            kind_w = kind_w,
            actor_w = actor_w,
        );
    }
    println!("\n{} event(s)", events.len());
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn event(ts: &str, kind: &str, actor: &str) -> Event {
        Event {
            ts: ts.to_string(),
            plane: "event".to_string(),
            kind: kind.to_string(),
            actor: actor.to_string(),
            subject: "subj".to_string(),
            payload: json!({}),
            content_hash: "sha256:0".to_string(),
        }
    }

    fn no_filter() -> QueryFilter {
        QueryFilter {
            kind: None,
            actor: None,
            plane: None,
            since: None,
            limit: None,
        }
    }

    #[test]
    fn test_since_mixed_precision_timestamps() {
        // pydantic emits no micros for zero-microsecond times; opsctl always
        // emits 6-digit micros. Lexicographically '.' < 'Z', so the old string
        // compare excluded 12:00:00.500000Z from --since 12:00:00Z.
        assert!(ts_at_or_after(
            "2026-04-22T12:00:00.500000Z",
            "2026-04-22T12:00:00Z"
        ));
        assert!(ts_at_or_after(
            "2026-04-22T12:00:00Z",
            "2026-04-22T12:00:00.000000Z"
        ));
        assert!(!ts_at_or_after(
            "2026-04-22T11:59:59.999999Z",
            "2026-04-22T12:00:00Z"
        ));
        // Equal instants at both precisions are included (at-or-after).
        assert!(ts_at_or_after(
            "2026-04-22T12:00:00.000000Z",
            "2026-04-22T12:00:00Z"
        ));
        // Non-RFC3339 input falls back to lexicographic comparison.
        assert!(ts_at_or_after("b", "a"));
        assert!(!ts_at_or_after("a", "b"));
    }

    #[test]
    fn test_select_since_includes_mixed_precision_events() {
        let all = vec![
            event("2026-04-22T11:00:00Z", "a.one", "opsctl"),
            event("2026-04-22T12:00:00.500000Z", "a.two", "opsctl"),
            event("2026-04-22T13:00:00Z", "a.three", "opsctl"),
        ];
        let filter = QueryFilter {
            since: Some("2026-04-22T12:00:00Z".to_string()),
            ..no_filter()
        };
        let got = select(&all, &filter);
        let kinds: Vec<&str> = got.iter().map(|e| e.kind.as_str()).collect();
        assert_eq!(kinds, vec!["a.two", "a.three"]);
    }

    #[test]
    fn test_limit_keeps_most_recent_tail() {
        let all = vec![
            event("2026-01-01T00:00:01.000000Z", "k.first", "opsctl"),
            event("2026-01-01T00:00:02.000000Z", "k.second", "opsctl"),
            event("2026-01-01T00:00:03.000000Z", "k.third", "opsctl"),
        ];
        let filter = QueryFilter {
            limit: Some(2),
            ..no_filter()
        };
        let got = select(&all, &filter);
        let kinds: Vec<&str> = got.iter().map(|e| e.kind.as_str()).collect();
        assert_eq!(
            kinds,
            vec!["k.second", "k.third"],
            "limit must take the tail"
        );

        // Limit larger than the match count returns everything, in order.
        let filter = QueryFilter {
            limit: Some(10),
            ..no_filter()
        };
        assert_eq!(select(&all, &filter).len(), 3);
    }

    #[test]
    fn test_select_kind_actor_plane_filters() {
        let mut e1 = event("2026-01-01T00:00:01.000000Z", "task.started", "opsctl");
        e1.plane = "action".to_string();
        let e2 = event("2026-01-01T00:00:02.000000Z", "task.completed", "opsctl");
        let e3 = event(
            "2026-01-01T00:00:03.000000Z",
            "intake.created",
            "sabnyx-cli",
        );
        let all = vec![e1, e2, e3];

        let filter = QueryFilter {
            kind: Some("task".to_string()),
            ..no_filter()
        };
        assert_eq!(select(&all, &filter).len(), 2);

        let filter = QueryFilter {
            actor: Some("sabnyx".to_string()),
            ..no_filter()
        };
        assert_eq!(select(&all, &filter).len(), 1);

        let filter = QueryFilter {
            plane: Some("action".to_string()),
            ..no_filter()
        };
        assert_eq!(select(&all, &filter).len(), 1);
    }

    #[test]
    fn test_print_table_multibyte_ts_does_not_panic() {
        // A corrupt ts with a multibyte char straddling the 26-byte boundary
        // used to panic the byte slice `&e.ts[..26]`.
        let e = event(
            "2026-01-01T00:00:00.00000日本語extra",
            "k.corrupt",
            "opsctl",
        );
        assert!(e.ts.len() > 26 && !e.ts.is_char_boundary(26));
        print_table(&[&e]); // must not panic
    }
}
