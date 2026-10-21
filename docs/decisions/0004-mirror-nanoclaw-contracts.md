# ADR 0004 — Mirror nanoclaw's data contracts

Date: 2026-04-22
Status: accepted

## Context

Nanoclaw (github.com/Joi/nanoclaw) is Joi's lightweight alternative
to Clawdbot — a TypeScript orchestrator that runs containerized
agents, has per-group memory, a scheduled-task system, and
file-based IPC. Recon (findings in the stage 0 session log; no
standalone recon doc was written for this one) found:

  - ~24k LOC total; the reusable core is ~2.4k LOC
  - WhatsApp/Signal/Slack transport is ~3.6k LOC we don't want
  - Per-scope queue prevents concurrent writes to the same vault
  - SQLite tables `scheduled_tasks` / `task_run_logs` are clean
  - File-IPC pattern (ipc/<scope>/{tasks,messages}/*.json) is
    dependency-free and survives restarts
  - container/skills/<name>/SKILL.md loading is pure convention

Sabnyx wants none of this at stage 0. At stage 3 we probably want
a scheduled-task runner.

## Decision

Do not fork nanoclaw. But mirror its data contracts now so lifting
its code later is mechanical:

  - If we introduce SQLite, use nanoclaw's schema shapes for
    scheduled_tasks + task_run_logs
  - If we introduce file-based IPC, use nanoclaw's path convention:
    `ipc/<scope>/{tasks,messages}/*.json`, `_close` sentinel,
    stdout marker blocks
  - Skills (stage 3) load from `container/skills/<name>/SKILL.md`
    with no registry
  - Per-scope queue: one in-flight agent per scope, FIFO

## Consequences

+ Stage 3 has a clean upgrade path: lift task-scheduler.ts,
  group-queue.ts, ipc.ts, container-runtime.ts
+ Our data shapes are compatible with nanoclaw's ecosystem if that
  ecosystem matures.
- Some minor abstraction overhead at stage 0 to preserve these
  shapes (mostly just naming conventions). Acceptable.
