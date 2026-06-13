// Query subcommand: filter ledger events and display them.

use crate::ledger::{replay, Event};
use std::path::Path;

/// Filter criteria for query.
pub struct QueryFilter {
    pub kind: Option<String>,
    pub actor: Option<String>,
    pub plane: Option<String>,
    pub since: Option<String>,
    pub limit: Option<usize>,
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
            // Lexicographic ISO comparison works for RFC3339 with Z suffix
            if event.ts.as_str() < since.as_str() {
                return false;
            }
        }
        true
    }
}

pub fn run(events_path: &Path, filter: QueryFilter, json_output: bool) -> anyhow::Result<()> {
    let all = replay(events_path)?;
    let mut matching: Vec<&Event> = all.iter().filter(|e| filter.matches(e)).collect();

    if let Some(limit) = filter.limit {
        matching.truncate(limit);
    }

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
        // Truncate ts to 26 chars (drop microseconds if present)
        let ts_display = if e.ts.len() > ts_w {
            &e.ts[..ts_w]
        } else {
            &e.ts
        };
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
