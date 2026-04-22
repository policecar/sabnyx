# obs-dailynotes — reuse assessment for sabnyx

Source: ~/src/joi/obs-dailynotes (Node.js / CommonJS, ~5,000 LOC under lib/, ~80 scripts under tools/)

## (1) What it does
A Node toolkit that joins Google Calendar + Apple Reminders + Apple Notes + Gmail + Notion into Joi Ito's Obsidian "switchboard" vault. Pipeline (lib/pipelines/daily.js, 133 LOC) generates a per-day file by upserting marker-delimited blocks (HEADER / MEETINGS / REMINDERS / GTD-FOCUS / KNOWLEDGE-STATUS) into ~/switchboard/dailynote/YYYY-MM-DD.md. Side pipelines: bidi Notes.app sync, person-page enrichment via Gmail/IMAP/LLM, knowledgeGraph indexer that emits data/graph.jsonl from frontmatter+wikilinks. README lines 10–34 declare the repo DEPRECATED, superseded by Python `amplifier-bundle-gtd` — so this is a quarry, not a dependency.

## (4) Runtime / deps
Node 20 (.nvmrc), CommonJS. package.json deps: googleapis, gray-matter, fast-glob, dotenv, commander, inquirer, imap, @notionhq/client, chalk. Tests via Python (run_tests.py wraps pytest) + jest. External services: Google OAuth (Calendar + Gmail), `reminders` CLI (keith/reminders-cli), AppleScript/osascript for Notes.app, IMAP for Mailstore, Notion API. macOS-only for Reminders/Notes paths.

## REUSE_AS_IS
- tools/knowledgeGraph/indexer.js (296 LOC) — fast-glob + gray-matter walker that emits data/graph.jsonl with `id = "${type}:${slug}"`, plus graph_meta.json and a markdown report. `toNode()` (L55) and `extractRelations()` (L87) are exactly the normalized-envelope + events-ledger pattern sabnyx wants. Drop in, point SWITCHBOARD_PATH at the vault, strip the person/meeting type heuristics.
- lib/notes-sync/{applescript.js 257, converter.js 221, state.js 180, index.js 395} — Notes.app → Obsidian intake with HTML↔Markdown conversion and a JSON sync-state tracker. Useful as-is as an `intake/` source: macOS Notes is a great phone-capture inbox. NotesSync class (index.js L39) and SyncState are self-contained.
- tools/updateRemindersCache.js (144 LOC) — pulls all Apple Reminders lists via `reminders show-lists/show --format json` and writes a normalized cache (id, title, list, due, flagged, tags). Clean pydantic-shaped record per item; works as the Reminders ingestor for events.jsonl.

## STEAL_PATTERN
- Marker-block upsert in lib/writer.js `upsertTodaySection()` (L20–95): wraps each section in `<!-- BEGIN X -->` / `<!-- END X -->` and only rewrites that block. Perfect for sabnyx's reweave step that must edit atlas/ files non-destructively. Pair with lib/fsSafe.js `writeFileAtomic`.
- External-ID round-trip via HTML comment: tools/generateReminders.js L31 embeds `<!--reminders-id:${id}-->` inside checkbox lines. Trivial, durable, Obsidian-invisible — use the same trick for x402 receipts and Lemma proof IDs.
- Frontmatter-as-contract harmonization in tools/knowledgeGraph/harmonizeFrontmatter.js (133 LOC): `harmonize()` (L55) enforces type/slug/id triple, validates id with `/^[a-z]+:[a-z0-9-]+$/`, runs in plan/apply mode (graph:fm:harmonize:plan|apply). This is the routing-decision-tree contract enforcer sabnyx needs.
- Wikilink + frontmatter-relations extraction in indexer.js L87–125: regex `/\[\[([^\]|#]+)(?:#[^\]]+)?\]\]/g`, kebabify (L38), conservative `note:slug` fallback for unresolved targets, confidence-scored edges (1.0 frontmatter, 0.4 wikilink). Copy verbatim into the atlas/ link resolver.
- Calendar URL parsers in lib/parsers.js (234 LOC): parseGoogleHangout/parseZoom/parseTeams/parseWebex with structured `conferenceData.entryPoints` preferred, falling back to location → description → summary regex hunt. Worth copying if/when sabnyx grows a calendar intake.

## SKIP
- Entire daily-note pipeline (index.js, lib/pipelines/daily.js, lib/writer.js HEADER/MEETINGS/REMINDERS sections, lib/agendasInjector.js, lib/meetingBlockManager.js, lib/gtdFocus.js, lib/gtd-simple/dashboard.js 532 LOC) — user explicitly does not want daily notes; this is the bulk of the repo.
- All person-graph code: tools/upsertPersonTemplate.js (212), tools/normalizePersonPage.js (179), tools/setFrontmatterLinks.js (64), tools/generatePeopleFromDailyNotes.js, tools/runAllPeople.js, tools/batchEnrichPeople.js, tools/enrichFromLLM.js, tools/importContactsFromCSV.js, tools/tagPeopleWithLists.js — user explicitly does not want a people graph yet.
- Gmail/IMAP/Mailstore swamp: ~25 scripts (tools/*Mail*.js, tools/*Gmail*.js, tools/mcpServers/gmailServer.js, lib/services/gmailService.js) plus OAuth bootstrap dance — out of scope for sabnyx and the deprecation notice points to a cleaner Python rewrite anyway.
- Empty stubs: lib/knowledgeGraph/{extractors,graphBuilder,graphQuery,index,models}.js are 0 bytes each — the real KG code lives under tools/knowledgeGraph/, ignore the lib/ namespace.
- run_tests.py + Python pytest wrapper for a Node codebase — don't port; sabnyx is Python-first, write native pytest against pydantic envelopes instead.
