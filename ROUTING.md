# ROUTING — where does a given piece of content go?

Every piece of content follows this decision tree. The tree is the
single source of truth; ad-hoc placement is a bug.

## The tree

    Is this durable domain knowledge?
      (a concept, reference, idea worth finding again after months)
    |
    +-- YES: Does an entity for it already exist in atlas/ or domains/?
    |     +-- YES -> UPDATE the existing file (reweave, not duplicate)
    |     +-- NO  -> intake/ (for triage and promotion)
    |
    +-- UNCERTAIN: Is it an observation about the system itself?
    |     (gap, contradiction, surprising connection, friction signal)
    |     +-- YES -> intake/.observations/
    |
    +-- NO: Is it operational or temporal?
          (triage report, session state, scratch)
          +-- YES -> _review/ or don't persist at all

## Content-type routing table

    Content                         Destination                         Why
    -----------------------------------------------------------------------------
    URL/bookmark                    intake/<slug>.md (type: bookmark)   triage first
    Paper abstract + notes          intake/<slug>.md (type: paper)      triage first
    Long-form note (import)         intake/<slug>.md (type: note)       triage first
    Original thought / reflection   intake/<slug>.md (type: thought)    triage first
    Update to existing atlas file   direct edit in atlas/               reweave, not re-intake
    System friction signal          intake/.observations/               meta, not content
    Triage proposal                 _review/pending.md                  operational
    Session summary / scratch       _review/ or DON'T PERSIST           ephemeral
    Graduated deep topic            domains/<topic>/                    only after critical mass

## When to promote intake -> atlas

Ask, in order:

  1. Will I want to find this again in 6 months?
  2. Is the content stable enough that editing in place makes sense?
  3. Does it describe a thing (concept, reference, idea) rather than
     a moment (meeting, session, day)?

If all three are YES, promote. Otherwise leave in intake/ (fine) or
move to _review/ (operational) or delete (ephemeral).

## When to graduate atlas -> domains

A topic graduates from atlas/concepts/ to domains/<topic>/ when:

  - there are 10+ atlas files on the topic
  - they have recurring subject matter (people, places, terms, dates)
    that would benefit from topic-specific schema
  - the operator wants to invest in the topic long-term

Graduation is a deliberate act, not automatic. It requires writing a
`domains/<topic>/SCHEMA.md` and migrating files with care.

## Observations routing

Drop an observation any time you notice one of these while doing
normal work:

    gap           entity referenced N times but no page exists
    contradiction two files make conflicting claims
    connection    two concepts repeatedly co-occur without linking
    friction      routing or triage repeatedly does the same thing
    structural    a file is in the wrong tier

Observations have a near-zero cost to write and a high cost to miss.
Err on the side of writing them.

## The reweave principle

After promoting a new entity to atlas/, ALWAYS run a reweave pass:

  "Now that this file exists, what existing files should link to it
   or be updated by it?"

Connecting 5 existing concepts is worth more than writing 5 new
unconnected files. Before creating a new atlas file, check whether
you can enrich an existing one instead.
