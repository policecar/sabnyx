# ADR 0008 — Ledger append failure semantics

Date: 2026-10-25
Status: accepted

## Context

The ledger records mutations that happened (ADR 0002). A command that
changes a vault file and then appends an event has to decide what to do
when the append fails. It can put the file back, or leave it changed.
Neither answer is right for every failure:

- If no byte reached `ops/events.jsonl`, restoring the file leaves vault
  and ledger consistent, and the command can be retried.
- If the line, or part of it, is on disk, restoring the file leaves the
  ledger recording a mutation that was undone. That is worse than a
  missing event: the ledger is the audit trail, and it would now be wrong.

Until now `ledger.append_event` gave the caller no way to tell these
apart. Only a short write raised `LedgerError`. An `OSError` from `open`
(nothing written) and from `fsync` (the whole line written) were the same
exception type.

The observation transitions (`sabnyx observations resolve|archive`) were
the first caller that writes a file before appending. To find a rule that
holds on any filesystem, eight failures were injected at the OS boundary:
`OPS.mkdir`, event construction, `open`, `write` with ENOSPC and 0 bytes
written, `write` with EIO after a partial write, a short write, `fsync`,
and `close`. For each one, the bytes actually on disk were compared with
what a classifier would decide.

| Rule for "do not restore" | Unsafe (restores with bytes on disk) | Conservative (keeps the file, nothing on disk) | Correct |
|---|---|---|---|
| `isinstance(e, LedgerError)` (old behaviour) | 3 | 0 | 5 |
| `isinstance(e, (LedgerError, OSError))` | 0 | 3 | 5 |
| `isinstance(e, LedgerWriteError)` (this ADR) | 0 | 1 | 7 |

The middle rule is safe, but it also refuses to restore after a failed
`open`. That is the common failure (a read-only mount, wrong permissions),
and it would leave the operator unable to retry.

## Decision

1. **File first, ledger second.** A command mutates the vault file, then
   appends the event. Never the other way round: an event must not
   describe a change that didn't happen.
2. **From the first `write(2)` through `close`, `append_event` raises
   `LedgerWriteError(LedgerError)` for any exception, and
   `LedgerWriteInterrupted(KeyboardInterrupt)` for a Ctrl-C.** The
   exceptions cover a short write and any error from `write`, `fsync` or
   `close`. The region also covers the directory sync that follows the
   append creating `events.jsonl`: the line is already on disk there. That
   sync swallows `OSError` (best effort); anything else it raises, and a
   Ctrl-C, is classified like the rest of the region. Both messages say the line may be on disk and to run `opsctl
   verify`. `LedgerWriteInterrupted` is still an interrupt: exit 130, and
   no `except Exception` handler can swallow it. Failures before that point
   (`OPS.mkdir`, event construction, `open`, or a Ctrl-C there) propagate
   as themselves and mean no byte reached the ledger.
3. **A caller restores iff the error is neither.**
   - `LedgerWriteError` or `LedgerWriteInterrupted`: re-raise, no restore.
     The mutation stands. A retry of the same transition is refused,
     because the file is no longer in its original state.
   - Any other `Exception`, or a `KeyboardInterrupt`: restore the original
     bytes atomically and re-raise. If the restore also fails, raise an
     error chained to both.
   - Other `BaseException`s (`SystemExit`, `GeneratorExit`): no restore.
     Neither is something an operator triggers mid-command, and keeping
     the file is the conservative direction.

`LedgerError`'s own meaning (a ledger line failed to parse) is not widened.
Existing `except LedgerError` sites keep catching the short write through
the subclass.

The executable form is
`tests/test_ledger.py::test_append_failure_class_matches_what_is_on_disk`
(the injection matrix, including the interrupt rows) and the pre-write /
post-write transition tests in `tests/test_writers_transition.py`. They inject at the real file
object, `Path.open` / `Path.mkdir` and event construction, never by
replacing `append_event`.

## Consequences

+ A new mutate-then-append command follows the same rule and needs no
  failure analysis of its own: `except (LedgerWriteError,
  LedgerWriteInterrupted): raise`, and on any other `Exception` or a
  `KeyboardInterrupt`, restore.
- Today only `writers.transition_observation` implements the restore. The
  older callers (`write_observation`, `write_intake`, promote and the
  others) never restore. That is safe under this rule, since they never
  undo a change whose line may be on disk. But a pre-write failure leaves
  them with a changed file and no event, and a retry is not offered.
  Adopting the restore is a per-caller change, not made here.
  `write_observation` is the first candidate: it is the other path where
  an operator will actually meet a changed file with no event.
+ Vault and ledger can disagree in one direction only: a changed file with
  no event (or a partial line) for it. A future `reconcile` check detects
  both leftover states: a non-pending observation with no
  `observation.resolved` for its path, and a pending one with no
  `observation.created`.
+ The common failure (read-only mount, permissions) stays retryable.
- ENOSPC with 0 bytes written raises `LedgerWriteError` and keeps the
  file changed, because the caller can't tell it from a partial EIO. This
  is the conservative direction and is accepted.
+ The restore also catches `KeyboardInterrupt`, so an operator's Ctrl-C
  before the ledger write is handled like any other pre-write failure.
- A Ctrl-C during the restore itself can still leave the leftover state
  (changed file, no event): the atomic replace either happened or it
  didn't.
- `opsctl`'s Rust appender has no mutate-first callers today (`supervise`
  appends `task.*` events and changes no files). If it ever gains one, it
  must use the same classification: a distinct error (and a distinct
  interrupt) from the first `write(2)` through `close` and any directory
  sync after it, and pass through anything earlier unchanged.
