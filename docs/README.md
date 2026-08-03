# docs/ — everything that isn't the contract

The four root files (`README.md`, `SPEC.md`, `ROUTING.md`, `AGENTS.md`) are
the contract every agent loads before touching the vault; they stay at the
root deliberately (`opsctl` also uses `SPEC.md` as its repo-root marker).
Everything else lives here:

| Where | What |
|-------|------|
| `architecture.md` | plain-English tour of the whole system (the gentle version) |
| `testing.md` | test playbook: free suites → live smoke tests |
| `deployment.md` | production playbook: two-repo split, systemd, Telegram, R1 |
| `reserved-syntax.md` | ZK/x402 HTML-comment syntax, reserved for stage 3 |
| `spec/jibot.md` | local-only (gitignored) copy of the jibot/Switchboard source spec sabnyx adapts |
| `decisions/` | ADRs 0001–0007 — one decision each, immutable |
| `plans/` | historical roadmap/build-out plus scoped future experiments and implementation plans |
| `reviews/` | dated reviews: architecture, simplification, codebase-vs-spec, and post-hardening jibot divergence |
| `recon/` | notes from scouting external systems (Amplifier, obs-dailynotes, Rabbit R1) |

Related but elsewhere: `../.solutions/` holds incident writeups (deployment,
security, testing) filed where debugging tools expect them; `../creations/`
documents the Rabbit R1 client next to its code.

Current scoped plans:

- `plans/observations-and-age-triage.md` — manual observation lifecycle and a
  deterministic starvation fix;
- `plans/atlas-update-intents-experiment.md` — interactive source-backed
  amendment experiment, with unattended application explicitly deferred;
- `plans/feedback-telemetry.md` — precise outcome events before analytics or
  notifications.
