# ADR 0001 — Substrate: plain git + markdown

Date: 2026-04-22
Status: accepted

## Context

The jibot architecture uses Obsidian + Syncthing. We considered:

  (a) Obsidian + Syncthing (jibot default)
  (b) Plain git repo of markdown
  (c) A graph database (Neo4j, DuckDB, SQLite FTS)
  (d) A proprietary system (Notion, Anytype, Logseq)

## Decision

Plain git repo of markdown, with frontmatter and wikilinks. Obsidian
is supported as an optional reader; nothing in sabnyx requires it.

## Consequences

+ Zero lock-in. Any markdown editor or IDE works.
+ Git gives us history, branches, blame, merge, portability.
+ Every CLI agent on earth can operate directly on the filesystem.
+ The ledger (events.jsonl) is also git-trackable, which means
  "what did the vault do this week" is a valid git log question.
- No graph view out of the box. Obsidian provides one if wanted.
- No real-time sync across devices unless we layer Syncthing or git
  push/pull. Acceptable for solo stage 0.
- Full-text search is grep + ripgrep until we build or adopt one.
  Also acceptable for solo stage 0.
