// opsctl — minimal control plane for the sabnyx ops ledger.
//
// @decision DEC-001: Single binary, three subcommands (query / project / supervise).
//   `append` is internal-only (used by supervise); no public append subcommand to
//   avoid accidental hand-crafted writes that bypass content_hash validation.
// @rationale: The Python side owns append for vault operations; opsctl owns append
//   only for its own task.* events. This keeps the ledger format stable.

mod ledger;
mod project;
mod query;
mod root;
mod supervise;

use clap::{Parser, Subcommand};
use std::path::PathBuf;

#[derive(Parser)]
#[command(name = "opsctl", about = "sabnyx operations control plane", version)]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Query and filter ledger events
    Query {
        /// Filter by kind (substring match)
        #[arg(long)]
        kind: Option<String>,

        /// Filter by actor (substring match)
        #[arg(long)]
        actor: Option<String>,

        /// Filter by plane (intent|event|projection|action)
        #[arg(long)]
        plane: Option<String>,

        /// Only show events at or after this ISO timestamp
        #[arg(long)]
        since: Option<String>,

        /// Maximum number of events to show
        #[arg(long)]
        limit: Option<usize>,

        /// Output raw JSON (one event per line)
        #[arg(long)]
        json: bool,
    },

    /// Rebuild state projection from the ledger
    Project {
        /// Output as JSON
        #[arg(long)]
        json: bool,
    },

    /// Run a command with supervision and ledger events
    Supervise {
        /// Label for task.* events (used as subject)
        #[arg(long)]
        label: String,

        /// Run every N seconds (loop mode)
        #[arg(long, conflicts_with = "once")]
        every: Option<u64>,

        /// Run once and exit (cron-friendly)
        #[arg(long)]
        once: bool,

        /// Number of retries on failure (0 = no retry)
        #[arg(long, default_value = "0")]
        retries: u32,

        /// Command to run (everything after --)
        #[arg(last = true, required = true)]
        command: Vec<String>,
    },
}

fn events_path() -> PathBuf {
    // Resolve the data root exactly like the Python side (SABNYX_DATA_DIR env /
    // <code-root>/.env / code root) so both writers share one ledger file.
    root::data_root().join("ops").join("events.jsonl")
}

fn main() -> anyhow::Result<()> {
    let cli = Cli::parse();
    let path = events_path();

    match cli.command {
        Commands::Query {
            kind,
            actor,
            plane,
            since,
            limit,
            json,
        } => {
            let filter = query::QueryFilter {
                kind,
                actor,
                plane,
                since,
                limit,
            };
            query::run(&path, filter, json)?;
        }

        Commands::Project { json } => {
            project::run(&path, json)?;
        }

        Commands::Supervise {
            label,
            every,
            once,
            retries,
            command,
        } => {
            if command.is_empty() {
                eprintln!("opsctl supervise: command is required");
                std::process::exit(1);
            }

            if let Some(interval) = every {
                supervise::run_loop(&path, &label, &command, retries, interval)?;
            } else {
                // Default to --once behavior
                let _ = once; // --once is just explicit; default is single-shot
                let success = supervise::run_cycle(&path, &label, &command, retries)?;
                if !success {
                    std::process::exit(1);
                }
            }
        }
    }

    Ok(())
}
