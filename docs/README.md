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
| `plans/` | roadmap (historical stage 0–3 arc), build-out plan (milestone 1, complete) |
| `reviews/` | dated reviews: architecture (2026-06), simplification (2026-06), codebase-vs-spec (2026-07) |
| `recon/` | notes from scouting external systems (Amplifier, obs-dailynotes, Rabbit R1) |

Related but elsewhere: `../.solutions/` holds incident writeups (deployment,
security, testing) filed where debugging tools expect them; `../creations/`
documents the Rabbit R1 client next to its code.
