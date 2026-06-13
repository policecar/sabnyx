// project subcommand: rebuild state projection from the ledger.
//
// Demonstrates "segments are authority; projections rebuild."
// - Counts per kind
// - Pending reweave intents (reweave.proposed minus later reweave.completed for same subject)
// - Atlas files seen (atlas.promoted / atlas.created subjects)
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

    // Track reweave state: subjects that have a reweave.proposed but no later reweave.completed
    // We track the last-seen index for each subject for each event type.
    let mut reweave_proposed_idx: HashMap<String, usize> = HashMap::new();
    let mut reweave_completed_idx: HashMap<String, usize> = HashMap::new();

    let mut atlas_files: HashSet<String> = HashSet::new();
    let mut last_tend_completed: Option<Value> = None;

    for (idx, event) in events.iter().enumerate() {
        *counts_by_kind.entry(event.kind.clone()).or_insert(0) += 1;

        match event.kind.as_str() {
            "reweave.proposed" => {
                reweave_proposed_idx.insert(event.subject.clone(), idx);
            }
            "reweave.completed" => {
                reweave_completed_idx.insert(event.subject.clone(), idx);
            }
            "atlas.promoted" | "atlas.created" => {
                atlas_files.insert(event.subject.clone());
            }
            "tend.completed" => {
                last_tend_completed = Some(event.payload.clone());
            }
            _ => {}
        }
    }

    // Pending reweave: proposed with no later completed for same subject
    let mut pending_reweave: Vec<String> = reweave_proposed_idx
        .iter()
        .filter(|(subject, proposed_idx)| {
            reweave_completed_idx
                .get(*subject)
                .map(|completed_idx| completed_idx < proposed_idx)
                .unwrap_or(true) // no completed at all → pending
        })
        .map(|(subject, _)| subject.clone())
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
