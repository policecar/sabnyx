# AGENTS — operator instructions

Any agent (human or LLM) operating on this vault reads this file
first, then SPEC.md, then ROUTING.md. In that order.

This file is stable. Change it deliberately.

## 0. The prime directive

  The vault is the substrate. The ledger is the substrate's shadow.
  Everything else is a projection. Protect the substrate.

## 1. Before you write anything

  (a) Read SPEC.md once per session.
  (b) Read ROUTING.md once per session.
  (c) Know which workspace you are allowed to write to (below).
  (d) Know the ledger kind you will emit for this mutation.

## 2. Workspaces and write permissions

    Role             May write to                              May read
    -------------------------------------------------------------------
    operator (you)   anywhere                                   anywhere
    intake-agent     vault/intake/, vault/sources/,
                     ops/events.jsonl, ops/knowledge-log.md     everything
    telegram-agent   (same as intake-agent; trusted senders only,
                     auto-applies to intake/ — see ADR 0007)        everything
    triage-agent     vault/_review/, vault/intake/ (frontmatter only),
                     ops/events.jsonl                           everything
    promote-agent    vault/atlas/, vault/intake/ (delete source),
                     ops/events.jsonl, ops/knowledge-log.md     everything
    reweave-agent    vault/atlas/ (marker blocks only),
                     vault/intake/.observations/,
                     ops/events.jsonl                           everything
    health-agent     vault/_review/, vault/intake/.observations/,
                     ops/events.jsonl                           everything

Stage 0 runs everything under `operator`. Role separation is enforced
by convention and audited by the ledger. Stage 3 introduces hard
sandboxing (see docs/recon/nanoclaw-mirror.md).

## 3. Every mutation emits one ledger event

Append to `ops/events.jsonl`. Use `ops/scripts/ledger.py` helpers:

    from ops.scripts.ledger import append_event
    append_event(
        plane="event",
        kind="intake.created",
        actor="sabnyx-cli",
        subject="vault/intake/my-slug.md",
        payload={"type": "thought", "description": "..."},
    )

The helper computes `content_hash` and writes the canonical line.
Never edit events.jsonl by hand.

## 4. Observations are cheap; write them

If during normal work you notice any of:

  - an entity referenced but lacking an atlas file
  - two atlas files making conflicting claims
  - two concepts that should link and don't
  - the routing tree giving you the wrong answer
  - a file that is in the wrong tier

...drop an observation file in `vault/intake/.observations/` with
the appropriate `category`. Cost: ~30 seconds. Value over time:
enormous.

## 5. Reweave after every promote

`promote` emits a `reweave.proposed` event carrying a prompt. The
reweave agent (or the operator in stage 0) runs the backward pass:

  1. Identify entities the new file mentions or embodies
  2. Search the vault for files that should link to it and don't
  3. Search for contradicting claims in existing atlas files
  4. Propose additions to marker blocks only; never rewrite prose

Contradiction findings become observation files with
`category: contradiction`. Link findings become marker-block
additions. Everything else is ignored.

## 6. Tone and voice (when the agent writes prose)

This is a personal vault. Files are for the operator to find and
re-read, not to publish. Voice is:

  - terse but not curt
  - specific over abstract
  - the description field is dense; the body can breathe
  - avoid marketing language, avoid "journey", avoid "ecosystem"

Stage 3 public surface will have a separate voice document.

## 7. Failure modes

  - Writing to atlas/ without going through promote
  - Dropping frontmatter to "fix" a lint failure
  - Editing the ledger
  - Creating a new atlas file when updating an existing one would do
  - Ignoring contradictions instead of logging them
  - Putting dates in filenames (slugs are atemporal)
  - Meeting notes or session scratch in atlas/

If you catch yourself doing any of these, STOP, write an observation
with `category: friction`, and hand the task back to the operator.

## 8. Session startup checklist

At the top of any session that will mutate the vault:

    git -C ~/src/_px/sabnyx status           # confirm clean tree
    uv run sabnyx lint                        # gates 1, 2, 4
    uv run sabnyx health --quick              # known state
    # read SPEC.md, ROUTING.md if not fresh

At the end:

    git -C ~/src/_px/sabnyx diff --stat
    uv run sabnyx lint
    git commit                                # pre-commit hook lints staged files
