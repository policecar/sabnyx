// project subcommand: rebuild state projection from the ledger.
//
// Demonstrates "segments are authority; projections rebuild."
// - Counts per kind
// - Pending reweave (atlas.promoted or reweave.proposed, minus a later
//   reweave.completed or atlas.removed for the same subject)
// - Current atlas files (atlas.promoted minus later atlas.removed subjects)
// - Last tend.completed payload

use crate::ledger::replay;
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};
use std::path::Path;

pub struct Projection {
    pub counts_by_kind: HashMap<String, usize>,
    pub pending_reweave: Vec<String>,
    pub atlas_files: Vec<String>,
    pub last_tend_completed: Option<Value>,
}

/// Build the projection by replaying the full ledger.
pub fn build(events_path: &Path) -> anyhow::Result<Projection> {
    let events = replay(events_path)?;

    let mut counts_by_kind: HashMap<String, usize> = HashMap::new();

    // Track reweave state per subject: events replay in file order, so the last
    // relevant event for a subject wins. promoted or proposed → pending;
    // completed → resolved; removed → forgotten. atlas.promoted queues on its
    // own because promote appends it and reweave.proposed separately: a
    // failure between the two must not lose the reweave (R05). Same rule as
    // src/sabnyx/commands/reweave.py::_pending_targets, which additionally
    // skips subjects whose file is missing.
    let mut reweave_pending: HashMap<String, bool> = HashMap::new();

    let mut atlas_files: HashSet<String> = HashSet::new();
    let mut last_tend_completed: Option<Value> = None;

    for event in events.iter() {
        *counts_by_kind.entry(event.kind.clone()).or_insert(0) += 1;

        match event.kind.as_str() {
            "reweave.proposed" => {
                reweave_pending.insert(event.subject.clone(), true);
            }
            "reweave.completed" => {
                reweave_pending.insert(event.subject.clone(), false);
            }
            // Only atlas.promoted exists — no producer anywhere emits an
            // "atlas.created" kind, so it is not matched here.
            "atlas.promoted" => {
                atlas_files.insert(event.subject.clone());
                reweave_pending.insert(event.subject.clone(), true);
            }
            "atlas.removed" => {
                atlas_files.remove(&event.subject);
                reweave_pending.remove(&event.subject);
            }
            "tend.completed" => {
                last_tend_completed = Some(event.payload.clone());
            }
            _ => {}
        }
    }

    // Pending reweave: subjects whose latest relevant event queued them.
    let mut pending_reweave: Vec<String> = reweave_pending
        .into_iter()
        .filter(|(_, pending)| *pending)
        .map(|(subject, _)| subject)
        .collect();
    pending_reweave.sort();

    let mut atlas_files: Vec<String> = atlas_files.into_iter().collect();
    atlas_files.sort();

    Ok(Projection {
        counts_by_kind,
        pending_reweave,
        atlas_files,
        last_tend_completed,
    })
}

pub fn run(events_path: &Path, json_output: bool) -> anyhow::Result<()> {
    let proj = build(events_path)?;

    if json_output {
        let mut kinds_map: Vec<Value> = proj
            .counts_by_kind
            .iter()
            .map(|(k, v)| json!({"kind": k, "count": v}))
            .collect();
        kinds_map.sort_by_key(|v| v["kind"].as_str().unwrap_or("").to_string());

        let out = json!({
            "counts_by_kind": kinds_map,
            "pending_reweave": proj.pending_reweave,
            "atlas_files": proj.atlas_files,
            "last_tend_completed": proj.last_tend_completed,
        });
        println!("{}", serde_json::to_string_pretty(&out)?);
    } else {
        print_human(&proj);
    }

    Ok(())
}

fn print_human(proj: &Projection) {
    println!("=== Ledger Projection ===\n");

    // Counts by kind
    println!("Events by kind:");
    let mut kinds: Vec<(&String, &usize)> = proj.counts_by_kind.iter().collect();
    kinds.sort_by_key(|(k, _)| k.as_str());
    for (kind, count) in &kinds {
        println!("  {:40}  {}", kind, count);
    }

    // Pending reweave
    println!(
        "\nPending reweave intents ({}):",
        proj.pending_reweave.len()
    );
    if proj.pending_reweave.is_empty() {
        println!("  (none)");
    } else {
        for s in &proj.pending_reweave {
            println!("  {}", s);
        }
    }

    // Atlas files
    println!("\nAtlas files seen ({}):", proj.atlas_files.len());
    if proj.atlas_files.is_empty() {
        println!("  (none)");
    } else {
        for f in &proj.atlas_files {
            println!("  {}", f);
        }
    }

    // Last tend.completed
    println!("\nLast tend.completed payload:");
    match &proj.last_tend_completed {
        None => println!("  (none)"),
        Some(v) => println!("  {}", serde_json::to_string_pretty(v).unwrap_or_default()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ledger::{append, new_event};
    use serde_json::json;
    use tempfile::NamedTempFile;

    fn emit(path: &Path, kind: &str, subject: &str, payload: Value) {
        let e = new_event("event", kind, "opsctl", subject, payload).expect("no floats");
        append(path, &e).expect("append");
    }

    #[test]
    fn test_pending_reweave_is_last_event_wins() {
        // The pending-reweave semantics are last-event-wins per subject:
        // a completed followed by a *later* proposed re-opens the subject
        // (unlike a set difference, where any historical completed would
        // permanently mask later proposals).
        let tmp = NamedTempFile::new().unwrap();
        let path = tmp.path();

        // a: proposed then completed → resolved.
        emit(path, "reweave.proposed", "vault/atlas/a.md", json!({}));
        emit(path, "reweave.completed", "vault/atlas/a.md", json!({}));
        // b: proposed, completed, proposed again → pending again.
        emit(path, "reweave.proposed", "vault/atlas/b.md", json!({}));
        emit(path, "reweave.completed", "vault/atlas/b.md", json!({}));
        emit(path, "reweave.proposed", "vault/atlas/b.md", json!({}));
        // c: proposed only → pending.
        emit(path, "reweave.proposed", "vault/atlas/c.md", json!({}));

        let proj = build(path).expect("build");
        assert_eq!(
            proj.pending_reweave,
            vec![
                "vault/atlas/b.md".to_string(),
                "vault/atlas/c.md".to_string()
            ]
        );
    }

    #[test]
    fn test_projection_counts_atlas_and_last_tend() {
        let tmp = NamedTempFile::new().unwrap();
        let path = tmp.path();

        emit(
            path,
            "atlas.promoted",
            "vault/atlas/concepts/x.md",
            json!({}),
        );
        emit(
            path,
            "atlas.promoted",
            "vault/atlas/concepts/x.md",
            json!({}),
        );
        emit(path, "tend.completed", "tend", json!({"run": 1}));
        emit(path, "tend.completed", "tend", json!({"run": 2}));

        let proj = build(path).expect("build");
        assert_eq!(proj.counts_by_kind["atlas.promoted"], 2);
        assert_eq!(
            proj.atlas_files,
            vec!["vault/atlas/concepts/x.md".to_string()]
        );
        assert_eq!(
            proj.last_tend_completed,
            Some(json!({"run": 2})),
            "last tend.completed payload wins"
        );
        // A promotion with no reweave.completed still owes its reweave.
        assert_eq!(
            proj.pending_reweave,
            vec!["vault/atlas/concepts/x.md".to_string()]
        );
    }

    #[test]
    fn test_promotion_without_intent_is_pending_until_completed() {
        // promote appends atlas.promoted and reweave.proposed separately; a
        // failure between them must still leave the reweave queued, and the
        // usual completion / re-promotion rules must apply (R05).
        let tmp = NamedTempFile::new().unwrap();
        let path = tmp.path();

        // a: promoted, intent lost → pending.
        emit(path, "atlas.promoted", "vault/atlas/a.md", json!({}));
        // b: promoted, intent lost, completed → resolved.
        emit(path, "atlas.promoted", "vault/atlas/b.md", json!({}));
        emit(path, "reweave.completed", "vault/atlas/b.md", json!({}));
        // c: normal promotion completed, removed, re-promoted without intent → pending.
        emit(path, "atlas.promoted", "vault/atlas/c.md", json!({}));
        emit(path, "reweave.proposed", "vault/atlas/c.md", json!({}));
        emit(path, "reweave.completed", "vault/atlas/c.md", json!({}));
        emit(path, "atlas.removed", "vault/atlas/c.md", json!({}));
        emit(path, "atlas.promoted", "vault/atlas/c.md", json!({}));

        let proj = build(path).expect("build");
        assert_eq!(
            proj.pending_reweave,
            vec![
                "vault/atlas/a.md".to_string(),
                "vault/atlas/c.md".to_string()
            ]
        );
    }

    #[test]
    fn test_atlas_removed_compensates_projection_state() {
        let tmp = NamedTempFile::new().unwrap();
        let path = tmp.path();
        let subject = "vault/atlas/concepts/gone.md";

        emit(path, "atlas.promoted", subject, json!({}));
        emit(path, "reweave.proposed", subject, json!({}));
        emit(path, "atlas.removed", subject, json!({}));

        let proj = build(path).expect("build");
        assert!(proj.atlas_files.is_empty());
        assert!(proj.pending_reweave.is_empty());
        assert_eq!(proj.counts_by_kind["atlas.removed"], 1);
    }
}
