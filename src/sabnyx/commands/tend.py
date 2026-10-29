"""`sabnyx tend` — the heartbeat: two-speed triage + reweave + health.

One schedulable command that drives the self-maintaining vault cycle:

  1. Auto-promote pass  — deterministic gate, no LLM.
  2. LLM triage pass   — batched model call for deferred items (≥ threshold,
                          or any item past --max-age-days, whichever fires).
  3. Reweave pass       — consume pending reweave.proposed intents.
     Gap closure        — resolve pending gaps whose target page now exists.
  4. Health pass        — print a concise audit summary.

Every mutation is ledgered. --dry-run writes nothing and makes no calls to
promote_file or the reweave apply path.

A run that dies in passes 2-3 on an ordinary exception records
`tend.aborted {error, pass}` before re-raising, so budget and reweave
failures are visible to the anomaly detectors (tend_detectors). Each real
run, completed or aborted, appends one `tend.anomaly` per finding that newly
trips.

@decision DEC-01: tend is a thin orchestrator; all logic lives in services
@rationale: The four passes reuse services.promote_file, services.triage_batch,
  reweave._pending_targets/_apply_links, and health helpers. tend just coordinates
  them and handles --dry-run / --yes gating. This keeps the command testable
  without re-implementing logic.
@decision DEC-02: discard means "note it, do not delete"
@rationale: Auto-deletion of vault files without explicit operator action is too
  destructive. LLM "discard" decisions are surfaced as report lines only.
@decision DEC-03: abort handling records and re-raises; it never recovers
@rationale: The exit code and the heartbeat's behaviour stay as they were; the
  record only makes the failure countable. The ledger's own errors are
  excluded, so a failed append is never answered with another append.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, date, datetime
from pathlib import Path
from types import TracebackType

import click
from rich.console import Console
from rich.table import Table

from .. import context, fm, ledger, llm, tend_detectors, util, writers
from ..paths import ATLAS, DATA_ROOT, INTAKE, ensure_tree
from ..services import (
    ACTOR_TEND,
    AutoPromoteDecision,
    auto_promote_decision,
    promote_file,
    triage_batch,
)


def _atlas_slugs() -> set[str]:
    """All slugs currently in atlas/ (case-insensitive)."""
    return {p.stem.lower() for p in ATLAS.rglob("*.md") if p.is_file()}


def _intake_files() -> list[Path]:
    return sorted(p for p in INTAKE.glob("*.md") if p.is_file())


def _ledger_memory() -> tuple[dict[str, str | None], dict[str, datetime]]:
    """One replay() pass collecting both triage-decision memory and intake
    creation timestamps, keyed by vault-relative subject path.

    Two unrelated features (retriage memory, age-based triage fallback) both
    need a full ledger scan; doing it once here avoids replaying events.jsonl
    twice per tend run.

    ``triage_hashes[subject] is None`` means the last recorded decision for
    that subject predates the content_hash field (a legacy event) — treated
    as "no memory" so it never suppresses re-triage. ``created_ts`` keeps the
    first (earliest) intake.created ts per subject.
    """
    triage_hashes: dict[str, str | None] = {}
    created_ts: dict[str, datetime] = {}
    for e in ledger.replay():
        if e.kind == "triage.decided":
            triage_hashes[e.subject] = e.payload.get("content_hash")
        elif e.kind == "intake.created":
            created_ts.setdefault(e.subject, e.ts)
    return triage_hashes, created_ts


def _record_triage_decision(
    path: Path,
    *,
    slug: str,
    action: str,
    reason: str,
    decision: str,
    extra: dict | None = None,
) -> None:
    """Ledger one triage.decided event for `path` — the single write site for
    every outcome of the pass-2 LLM loop, keep/discard included.

    Every path sent to the LLM tier (`triage_now`) MUST get exactly one of
    these (or be removed from intake by a successful promote) by the end of
    the loop. Without that, `_filter_already_triaged`'s retriage memory never
    learns about it, and under the age fallback the SAME stale item re-fires
    a whole-queue LLM call on every subsequent heartbeat, indefinitely.
    """
    payload: dict = {
        "slug": slug,
        "action": action,
        "reason": reason,
        "decision": decision,
        "content_hash": util.file_hash(path),
    }
    if extra:
        payload.update(extra)
    ledger.append_event(
        plane="projection",
        kind="triage.decided",
        actor=ACTOR_TEND,
        subject=str(path.relative_to(DATA_ROOT)),
        payload=payload,
    )


def _filter_already_triaged(
    deferred: list[Path],
    *,
    retriage: bool,
    remembered: dict[str, str | None],
    console: Console,
) -> list[Path]:
    """Drop intake files unchanged since their last triage.decided decision.

    Without this, every tend run re-sends the whole deferred queue to the LLM
    triage tier, re-rolling a settled "keep" on every heartbeat. --retriage
    bypasses the filter (forces a fresh LLM pass over everything deferred).
    """
    if retriage:
        return deferred
    kept: list[Path] = []
    skipped = 0
    for p in deferred:
        subject = str(p.relative_to(DATA_ROOT))
        last_hash = remembered.get(subject)
        if last_hash is not None and last_hash == util.file_hash(p):
            skipped += 1
            continue
        kept.append(p)
    if skipped:
        console.print(
            f"  skipped {skipped} already-triaged item(s) (unchanged since last "
            f"decision; --retriage to force)"
        )
    return kept


def _item_age_days(
    path: Path,
    created_ts: dict[str, datetime],
    now: datetime | None = None,
) -> float:
    """Age in days of a deferred intake file, for the age-based triage fallback.

    A pure, clock-injectable function so tests don't need to sleep or
    monkeypatch time globally. Priority, falling through on a miss:

      1. the ledger's ``intake.created`` ts for this subject (``created_ts``,
         built by ``_ledger_memory``) — authoritative and independent of any
         later frontmatter edit;
      2. frontmatter ``created`` / ``source_date``, when the file's own
         frontmatter loads and validates;
      3. file mtime, the last resort for a file whose frontmatter is missing
         or invalid — scheduling only, never treated as knowledge data.
    """
    if now is None:
        now = datetime.now(UTC)

    subject = str(path.relative_to(DATA_ROOT))
    ts = created_ts.get(subject)

    if ts is None:
        try:
            meta, _body, _kind = fm.load(path)
        except Exception:
            meta = None
        if meta is not None:
            raw = meta.get("created") or meta.get("source_date")
            try:
                if isinstance(raw, str):
                    ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                elif isinstance(raw, datetime):
                    ts = raw
                elif isinstance(raw, date):
                    ts = datetime(raw.year, raw.month, raw.day, tzinfo=UTC)
            except ValueError:
                # A non-ISO `created` string must fall to mtime, not abort
                # the whole tend run — frontmatter is schema-validated on
                # write, but a hand-edited or legacy file isn't guaranteed to
                # still parse.
                ts = None

    if ts is None:
        ts = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    elif ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)

    return (now - ts).total_seconds() / 86400.0


def _reweave_pending(*, dry_run: bool, yes: bool, console: Console) -> int:
    """Consume pending reweave.proposed intents via reweave.reweave_one.

    Delegates the LLM call, guards, apply, and ledger event to the shared
    helper so the apply path lives in one place. tend's --dry-run skips the
    helper entirely (no LLM call), per DEC-01. Returns files reweaved.
    """
    from . import reweave as rw

    targets = rw._pending_targets()
    if not targets:
        console.print("[dim]reweave: no pending intents[/dim]")
        return 0

    console.print(f"reweave: [bold]{len(targets)}[/bold] pending target(s)")
    index = context.slug_index(ATLAS)
    reweaved = 0

    for tgt in targets:
        rel = str(tgt.relative_to(DATA_ROOT))
        console.print(f"  [bold]reweave[/bold] {rel}")

        if dry_run:
            console.print("    [dim]dry run: skipping LLM call[/dim]")
            continue

        result = rw.reweave_one(
            tgt, index, role="standard", actor=ACTOR_TEND, yes=yes, dry_run=False, console=console
        )
        if result is not None:
            reweaved += 1

    return reweaved


def _health_summary(console: Console) -> None:
    """Print a concise health table (reuses health.py's link scan)."""
    from . import health as health_mod

    idx = context.vault_slug_index()
    broken_links, _linked, _drift, _outgoing = health_mod.scan_wikilinks(idx)
    intake_count = len(list(INTAKE.glob("*.md")))
    atlas_count = sum(1 for p in ATLAS.rglob("*.md") if p.is_file())
    pending_obs = health_mod.pending_observation_count()

    t = Table(title="health summary")
    t.add_column("Check")
    t.add_column("Result", justify="right")
    t.add_row("intake queue", str(intake_count))
    t.add_row("atlas files", str(atlas_count))
    t.add_row("pending observations", str(pending_obs))
    t.add_row("broken wikilinks", str(len(broken_links)))
    t.add_row("ledger events", str(ledger.count()))
    console.print(t)


def _record_anomalies(console: Console) -> None:
    """Append one tend.anomaly per newly tripped finding, then print the
    current findings. Real runs only, after the run's boundary event."""
    from . import health as health_mod

    events = list(ledger.replay())
    for a in tend_detectors.trips(events):
        ledger.append_event(
            plane="projection",
            kind="tend.anomaly",
            actor=ACTOR_TEND,
            subject=a.subject,
            payload={
                "detector": a.detector,
                "subject": a.subject,
                "count": a.count,
                "window": a.window,
            },
        )
    health_mod.print_anomalies(console, tend_detectors.detect(events))


class _AbortGuard:
    """Records `tend.aborted` when passes 2-3 raise; never suppresses.

    Only ordinary exceptions outside the ledger.LedgerError family are
    recorded, and only on a real run: a ledger failure must not be answered
    with another append, interrupts pass through, and --dry-run writes
    nothing. Only the exception's type name is ledgered, never its message
    (provider errors can carry model output).
    """

    def __init__(self, *, dry_run: bool, console: Console) -> None:
        self.dry_run = dry_run
        self.console = console
        self.pass_ = 2

    def __enter__(self) -> _AbortGuard:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc is None or self.dry_run:
            return
        if not isinstance(exc, Exception) or isinstance(exc, ledger.LedgerError):
            return
        # If this append raises, its error propagates with `exc` as context;
        # it is never retried.
        ledger.append_event(
            plane="projection",
            kind="tend.aborted",
            actor=ACTOR_TEND,
            subject="vault/",
            payload={"error": type(exc).__name__, "pass": self.pass_},
        )
        try:
            _record_anomalies(self.console)
        except Exception as detector_error:
            # The original exception is the one that matters; nothing in the
            # notice may replace it.
            with contextlib.suppress(Exception):
                self.console.print(
                    f"[red]anomaly detection failed:[/red] {type(detector_error).__name__}"
                )
        # Returning None: the original exception propagates unchanged.


@click.command("tend")
@click.option("--dry-run", is_flag=True, help="Show the plan; write nothing.")
@click.option("--yes", "-y", is_flag=True, help="Apply LLM triage decisions without prompting.")
@click.option(
    "--threshold",
    default=5,
    show_default=True,
    type=int,
    help="Min deferred items before calling the LLM triage tier.",
)
@click.option("--no-reweave", is_flag=True, help="Skip the reweave pass.")
@click.option("--no-health", is_flag=True, help="Skip the health summary.")
@click.option(
    "--retriage",
    is_flag=True,
    help="re-send already-triaged intakes to the LLM even if unchanged",
)
@click.option(
    "--max-age-days",
    default=7,
    show_default=True,
    type=int,
    help=(
        "Max days a deferred item waits before LLM triage fires even below "
        "--threshold; 0 disables the age fallback."
    ),
)
def tend_cmd(
    dry_run: bool,
    yes: bool,
    threshold: int,
    no_reweave: bool,
    no_health: bool,
    retriage: bool,
    max_age_days: int,
) -> None:
    """Heartbeat: auto-promote → LLM triage → reweave → health.

    Runs the full self-maintenance cycle in one schedulable command.
    Use --dry-run to preview without writing anything.
    """
    ensure_tree()
    console = Console()

    if dry_run:
        console.print("[bold yellow]dry run[/bold yellow] — no mutations will be written")

    from ..config import trusted_sources

    trusted = trusted_sources()
    atlas_slugs = _atlas_slugs()
    intake = _intake_files()

    console.print(
        f"\n[bold]pass 1: auto-promote[/bold]  "
        f"({len(intake)} intake file(s), {len(atlas_slugs)} atlas slug(s))"
    )

    auto_promoted: list[Path] = []
    deferred: list[Path] = []

    for p in intake:
        try:
            meta, _body, kind = fm.load(p)
        except fm.FrontmatterError as e:
            console.print(f"  [red]skip (invalid)[/red] {p.name}: {e.errors}")
            deferred.append(p)
            continue
        except Exception as e:
            # Per-file robustness: one unreadable file must not abort the run.
            console.print(f"  [red]skip (unreadable)[/red] {p.name}: {e}")
            deferred.append(p)
            continue

        decision: AutoPromoteDecision = auto_promote_decision(
            meta,
            kind,
            trusted=trusted,
            atlas_slugs=atlas_slugs,
            source_slug=p.stem.lower(),
        )

        if decision.promotable:
            assert decision.atlas_type is not None
            console.print(
                f"  [green]auto-promote[/green] {p.name} → {decision.atlas_type}"
                + (" [dim](dry run)[/dim]" if dry_run else "")
            )
            if not dry_run:
                try:
                    promote_file(p, decision.atlas_type, actor=ACTOR_TEND)
                    auto_promoted.append(p)
                    # Update atlas_slugs so subsequent files in this run don't
                    # get false-positive "no duplicate" passes.
                    atlas_slugs.add(p.stem.lower())
                except Exception as e:
                    console.print(f"  [red]promote failed:[/red] {e}")
                    deferred.append(p)
            else:
                auto_promoted.append(p)
        else:
            reason_str = "; ".join(decision.reasons) if decision.reasons else "no reason"
            console.print(f"  [dim]defer[/dim] {p.name}  ({reason_str})")
            deferred.append(p)

    console.print(
        f"  auto-promoted: [bold]{len(auto_promoted)}[/bold]  "
        f"deferred: [bold]{len(deferred)}[/bold]"
    )

    # Passes 2 + 3 are the LLM-calling passes. Bound them under one per-run call
    # budget (fail-closed) so an unattended `tend --yes` loop with a misbehaving
    # prompt or a huge queue can't spin the provider without limit.
    llm_triaged = 0
    reweaved = 0
    with _AbortGuard(dry_run=dry_run, console=console) as guard, llm.call_budget():
        # -- pass 2: LLM triage --
        console.print(
            f"\n[bold]pass 2: LLM triage[/bold]  ({len(deferred)} deferred, threshold {threshold})"
        )

        triage_hashes, created_ts = _ledger_memory()
        to_triage = _filter_already_triaged(
            deferred, retriage=retriage, remembered=triage_hashes, console=console
        )

        # Size threshold fires first; below it, fall back to age: a queue that
        # never fills to --threshold must still get triaged once it's been
        # sitting long enough. Age is a trigger over the whole queue (plan's
        # triage_due semantics), not a per-item filter — once the OLDEST item
        # in to_triage is old enough, send the whole (already-filtered)
        # to_triage in one batched call, rather than trickling one call per
        # item as each individually crosses the age limit.
        # --max-age-days 0 disables the age fallback entirely.
        triage_trigger: str | None = None
        triage_now: list[Path] = []
        if to_triage and len(to_triage) >= threshold:
            triage_trigger = "threshold"
            triage_now = to_triage
        elif to_triage and max_age_days > 0:
            oldest_age = max(_item_age_days(p, created_ts) for p in to_triage)
            if oldest_age >= max_age_days:
                triage_trigger = "age"
                triage_now = to_triage
                console.print(
                    f"  oldest deferred item is {oldest_age:.1f} day(s) old "
                    f"(≥ max-age-days) → triaging {len(to_triage)} below threshold"
                )

        if triage_now:
            console.print(f"  calling LLM triage for {len(triage_now)} item(s)…")
            if not dry_run:
                batch = triage_batch(triage_now, role="standard")
                slug_to_path = {p.stem.lower(): p for p in triage_now}
                decided: set[Path] = set()
                # A non-tty run without --yes couldn't ask the operator at
                # all — the LLM did answer (promote), so it's not
                # "unanswered" either. Exclude these from the post-loop
                # sweep without ledgering anything, so the item is re-offered
                # next run instead of settling as a quiet "declined".
                skip_sweep: set[Path] = set()

                for item in batch.items:
                    path = slug_to_path.get(item.slug.lower())
                    if path is None:
                        console.print(f"  [yellow]unknown slug from LLM:[/yellow] {item.slug!r}")
                        continue

                    if item.action == "promote" and item.atlas_type:
                        # Provenance gate: Gate 6 (services.auto_promote_decision)
                        # blocks auto-promotion for an untrusted source_agent in
                        # the deterministic pass; the LLM tier must honour the
                        # same trust boundary (config.trusted_sources), or a
                        # prompt-injected telegram/web/r1 intake could ride an
                        # LLM "promote" verdict straight into atlas/ unattended.
                        try:
                            meta, _body, _kind = fm.load(path)
                        except fm.FrontmatterError as e:
                            # Fail closed: provenance can't be established from
                            # invalid frontmatter, and promote_file would refuse
                            # the file anyway. Report and continue (M3 contract).
                            console.print(f"  [red]promote failed:[/red] {e}")
                            _record_triage_decision(
                                path,
                                slug=item.slug,
                                action=item.action,
                                reason=item.reason,
                                decision="promote_failed",
                            )
                            decided.add(path)
                            continue
                        source_agent = meta.get("source_agent", "")
                        trusted_provenance = source_agent in trusted
                        if not trusted_provenance and yes:
                            console.print(
                                f"  [yellow]skip promote[/yellow] {item.slug}: untrusted "
                                f"provenance (source_agent={source_agent!r}; operator review "
                                f"needed)"
                            )
                            _record_triage_decision(
                                path,
                                slug=item.slug,
                                action=item.action,
                                reason=item.reason,
                                decision="deferred_untrusted",
                                extra={"source_agent": source_agent},
                            )
                            decided.add(path)
                            continue

                        # Gate 5 re-check: the deferred queue systematically
                        # contains duplicate-slug deferrals, and the triage
                        # prompt never sees the atlas index — never let the
                        # LLM overwrite an existing atlas file.
                        if path.stem.lower() in atlas_slugs:
                            console.print(
                                f"  [yellow]skip promote[/yellow] {item.slug}: slug already "
                                f"exists in atlas (kept in intake)"
                            )
                            _record_triage_decision(
                                path,
                                slug=item.slug,
                                action=item.action,
                                reason=item.reason,
                                decision="skipped_atlas_dup",
                            )
                            decided.add(path)
                            continue
                        console.print(
                            f"  [bold]LLM promote[/bold] {item.slug} → {item.atlas_type}  "
                            f"[dim]({item.reason})[/dim]"
                        )
                        aborted = False
                        try:
                            if trusted_provenance:
                                apply = yes or click.confirm(
                                    f"  apply promote for {item.slug}?", default=True
                                )
                            else:
                                apply = click.confirm(
                                    f"  untrusted provenance (source_agent={source_agent!r}) "
                                    f"— apply promote for {item.slug} anyway?",
                                    default=False,
                                )
                        except click.Abort:
                            # non-tty without --yes (cron): keep, don't crash the
                            # run. Not a "no" — the operator was never actually
                            # asked, so this must NOT settle as "declined" (that
                            # would quiet every pending LLM-promote after one
                            # accidental non-tty run until content changes).
                            console.print(
                                "  [yellow]no interactive input; kept in intake "
                                "(use --yes for unattended runs)[/yellow]"
                            )
                            apply = False
                            aborted = True
                        if not apply:
                            if aborted:
                                skip_sweep.add(path)
                                continue
                            # An explicit interactive "no". Recorded so the
                            # item goes quiet until its content changes or
                            # --retriage forces a resend — same contract as a
                            # settled keep/discard.
                            _record_triage_decision(
                                path,
                                slug=item.slug,
                                action=item.action,
                                reason=item.reason,
                                decision="declined",
                            )
                            decided.add(path)
                            continue
                        try:
                            promote_file(path, item.atlas_type, actor=ACTOR_TEND)
                            llm_triaged += 1
                            atlas_slugs.add(path.stem.lower())
                        except Exception as e:
                            console.print(f"  [red]promote failed:[/red] {e}")
                            _record_triage_decision(
                                path,
                                slug=item.slug,
                                action=item.action,
                                reason=item.reason,
                                decision="promote_failed",
                            )
                            decided.add(path)
                    elif item.action in ("keep", "discard"):
                        if item.action == "keep":
                            console.print(f"  [dim]keep[/dim] {item.slug}  ({item.reason})")
                        else:
                            console.print(
                                f"  [yellow]discard (noted, not deleted)[/yellow] {item.slug}  "
                                f"({item.reason})"
                            )
                        # Persist the decision (DEC-02: note it, do not delete) so
                        # unattended runs leave a record, not just console output.
                        _record_triage_decision(
                            path,
                            slug=item.slug,
                            action=item.action,
                            reason=item.reason,
                            decision=item.action,
                        )
                        decided.add(path)

                # Every path sent to the LLM tier must leave this loop either
                # ledgered (`decided`) or gone from intake (a successful
                # promote unlinks its source). Anything left over means the
                # LLM never answered for it (unknown/omitted slug) — without
                # this sweep, that item re-fires the whole-queue LLM call on
                # every subsequent heartbeat once the age fallback trips.
                for path in triage_now:
                    if path in decided or path in skip_sweep or not path.exists():
                        continue
                    console.print(
                        f"  [yellow]unanswered[/yellow] {path.stem}: LLM response omitted "
                        f"this item; recorded so it doesn't re-fire every heartbeat"
                    )
                    _record_triage_decision(
                        path,
                        slug=path.stem.lower(),
                        action="unanswered",
                        reason="LLM response did not include a verdict for this item",
                        decision="unanswered",
                    )
            else:
                console.print("  [dim]dry run: skipping LLM call[/dim]")
        else:
            console.print(
                f"  [dim]below threshold ({len(to_triage)} < {threshold}); skipping LLM tier[/dim]"
            )

        # -- pass 3: reweave --
        guard.pass_ = 3
        if not no_reweave:
            console.print("\n[bold]pass 3: reweave[/bold]")
            reweaved = _reweave_pending(dry_run=dry_run, yes=yes, console=console)
        else:
            console.print("\n[dim]pass 3: reweave skipped (--no-reweave)[/dim]")

    # -- gap closure (D3): before the health summary, so its pending count is
    #    current. Not tied to --no-health: a stale pending count is the bug
    #    closure exists to fix, whether or not the table is printed.
    console.print("\n[bold]closing resolved gaps[/bold]")
    closure = writers.close_resolved_gaps(actor=ACTOR_TEND, dry_run=dry_run, console=console)

    # -- pass 4: health --
    if not no_health:
        console.print("\n[bold]pass 4: health[/bold]")
        _health_summary(console)
    else:
        console.print("\n[dim]pass 4: health skipped (--no-health)[/dim]")

    # -- summary ledger event (skipped in dry-run: dry-run writes nothing) --
    if not dry_run:
        ledger.append_event(
            plane="projection",
            kind="tend.completed",
            actor=ACTOR_TEND,
            subject="vault/",
            payload={
                "auto_promoted": len(auto_promoted),
                "llm_triaged": llm_triaged,
                "reweaved": reweaved,
                "gaps_closed": len(closure.closed),
                "dry_run": dry_run,
                "triage_trigger": triage_trigger,
            },
        )
        _record_anomalies(console)
    else:
        from . import health as health_mod

        health_mod.print_anomalies(console, tend_detectors.detect(ledger.replay()))

    console.print(
        f"\n[bold green]tend complete[/bold green]  "
        f"auto_promoted={len(auto_promoted)}  "
        f"llm_triaged={llm_triaged}  "
        f"reweaved={reweaved}" + ("  [dim](dry run)[/dim]" if dry_run else "")
    )
