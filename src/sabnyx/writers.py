"""Validated writers shared by LLM-driven commands.

The model proposes; this module slugifies, writes, validates, rolls
back on schema failure, and ledgers. References `paths` by module
attribute so tests can redirect the vault wholesale.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape

from . import fm, ledger, paths
from . import observations as obs
from .schemas import (
    ObservationFrontmatter,
    ObservationStatus,
    Producer,
    ProposedIntake,
    ProposedObservation,
    ResolutionKind,
)
from .util import slugify


def _write_checked(path, meta: dict, body: str, *, console: Console) -> str | None:
    """Skip-if-exists, write, re-validate, roll back on schema failure.

    Returns the DATA_ROOT-relative path string on success, or None if the file
    already existed or failed validation (in which case it is unlinked). The
    rollback is the bit you least want to maintain in two copies; callers keep
    their own meta/ledger/messaging.
    """
    if path.exists():
        console.print(f"  [yellow]skip (exists)[/yellow] {path.relative_to(paths.DATA_ROOT)}")
        return None
    fm.write(path, meta, body)
    try:
        fm.load(path)  # belt + suspenders
    except fm.FrontmatterError as e:
        console.print(f"  [red]invalid, rolled back:[/red] {e}")
        path.unlink()
        return None
    return str(path.relative_to(paths.DATA_ROOT))


def write_intake(
    pi: ProposedIntake,
    *,
    actor: str,
    source: str,
    source_url: str | None = None,
    trailer: str | None = None,
    console: Console,
) -> str | None:
    """Write one intake file from a model proposal.

    `trailer` is an optional provenance HTML comment appended to the body
    (reserved syntax, docs/reserved-syntax.md). Returns the path relative
    to ROOT, or None if skipped/rolled back.
    """
    slug = slugify(pi.title)
    path = paths.INTAKE / f"{slug}.md"

    if pi.type in ("bookmark", "paper") and not source_url:
        # SPEC §3: bookmark/paper require a source_url. A model can propose
        # type=bookmark for pasted text that has no URL — downgrade to note
        # rather than write an invalid file (or crash on validation).
        console.print(
            f"  [yellow]downgrade[/yellow] {slug}: {pi.type} proposed without a source_url → note"
        )
        pi = pi.model_copy(update={"type": "note"})

    meta: dict = {
        "type": pi.type,
        "description": pi.description,
        "source": source,
        "tags": pi.tags,
        "status": "draft",
        "created": ledger.now().isoformat(),
        "source_agent": actor,
    }
    if source_url:
        meta["source_url"] = source_url
    body = f"# {pi.title}\n\n{pi.body.strip()}\n" + (f"\n{trailer}\n" if trailer else "")
    rel = _write_checked(path, meta, body, console=console)
    if rel is None:
        return None

    ledger.append_event(
        kind="intake.created",
        actor=actor,
        subject=rel,
        payload={"type": pi.type, "description": pi.description, "slug": slug},
    )
    console.print(f"  [green]wrote[/green] {rel}")
    return rel


def append_knowledge_log(operation: str, title: str, fields: dict[str, str | None]) -> None:
    """Gate 3 (materia): grep-able provenance line, jibot's log contract.
    `grep "^# \\[" knowledge-log.md | grep ingest` finds ingestions."""
    log = paths.KNOWLEDGE_LOG
    log.parent.mkdir(parents=True, exist_ok=True)
    if not log.exists():
        log.write_text("# Knowledge log\n\nAppend-only provenance trail.\n\n", encoding="utf-8")
    with log.open("a", encoding="utf-8") as f:
        f.write(f"# [{ledger.now().isoformat()}] {operation} | {title}\n")
        for k, v in fields.items():
            if v:
                f.write(f"  {k}: {v}\n")


# -- observations (IDD §6, D4, D5, D10, P1, P3) --------------------------------------

OutcomeKind = Literal["written", "reopened", "already_pending", "suppressed", "exists", "invalid"]

# Highest `-N` suffix tried for an identity-bearing write (IDD §6 step 4).
_MAX_SUFFIX = 99


# @decision DEC-001: write_observation returns one of six outcome kinds (D10, P3).
# @rationale: the old `str | None` folded "already tracked", "dismissed",
#   "name taken" and "schema refused" into one None, so a caller could not
#   tell a lost signal from a deliberate skip. Every non-write now says why,
#   and carries the path it is about (the would-be file, the colliding file,
#   the pending record or the head), so a producer can count and report
#   without re-reading the folder. An identity-bearing write never collides
#   silently: it takes the first free `-N` (P1); only prose can be `exists`.
@dataclass(frozen=True)
class WriteOutcome:
    """What write_observation did, or would do on a dry run.

    path, by kind (absolute):
      written / reopened  the new file (dry run: the file that would be written)
      exists              the file already holding the prose write's name
      already_pending     the first pending record of the same id
      suppressed          the chain head that dismissed the id
      invalid             None
    obs_id: the computed id, or None for a prose (or untrusted) observation.
    error: why, set only for invalid. resolution_kind: the head's kind, set
    only for suppressed (ledger_suppressed reads it instead of the file).
    """

    kind: OutcomeKind
    path: Path | None
    obs_id: str | None = None
    dry_run: bool = False
    error: str | None = None
    resolution_kind: ResolutionKind | None = None

    def __post_init__(self) -> None:
        # The contract above, as checks: a malformed outcome is a writer bug.
        if (self.kind == "invalid") != (self.path is None):
            raise ValueError(f"WriteOutcome: path must be None iff invalid (kind {self.kind!r})")
        if (self.kind == "invalid") != (self.error is not None):
            raise ValueError(f"WriteOutcome: error must be set iff invalid (kind {self.kind!r})")
        if (self.kind == "suppressed") != (self.resolution_kind is not None):
            raise ValueError("WriteOutcome: resolution_kind must be set iff suppressed")
        if self.kind in ("already_pending", "suppressed", "reopened") and self.obs_id is None:
            raise ValueError(f"WriteOutcome: {self.kind} requires an obs_id")

    @property
    def rel(self) -> str:
        """`path` relative to DATA_ROOT: the string the old writer returned."""
        if self.path is None:
            raise ValueError("an invalid WriteOutcome has no path")
        return str(self.path.relative_to(paths.DATA_ROOT))


def _errors(e: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors())


def _invalid(
    error: str, *, dry_run: bool, console: Console, obs_id: str | None = None
) -> WriteOutcome:
    if not dry_run:
        console.print(f"  [red]invalid:[/red] {escape(error)}")
    return WriteOutcome("invalid", None, obs_id=obs_id, dry_run=dry_run, error=error)


def write_observation(
    ob: ProposedObservation,
    *,
    producer: Producer,
    actor: str,
    target: str | None = None,
    dry_run: bool = False,
    console: Console,
) -> WriteOutcome:
    """Write one observation under intake/.observations/, through the recurrence policy.

    `producer` is the code path that built the observation; `actor` becomes
    `source_agent`. `target` is a gap's missing page as a slug. Neither, nor
    `ob.involves`, is ever used as a path: they are identity tokens.

    Order: validate the metadata (an invalid one wins over an existing
    file and reads nothing); compute the id; for an id, ask the policy
    (pending -> already_pending, dismissed -> suppressed, done -> reopen
    with `supersedes`); pick the name (an id takes the first free of base,
    -2 .. -99; prose colliding at base is `exists`); return here on a dry
    run, which prints, writes and ledgers nothing; else write, re-validate,
    and append `observation.created`. already_pending and suppressed write
    nothing and append nothing (count suppressions with ledger_suppressed).

    Cost: reads the frontmatter of every vault page (the resolver) and, for
    an identity-bearing observation, every observation file. Rebuilt on
    every call, no cache (D5).
    """
    observed = ledger.today().isoformat()
    slug = slugify(ob.title)
    meta: dict = {
        "type": "observation",
        "category": ob.category,
        "observed": observed,
        "source_agent": actor,
        "status": "pending",
        "involves": ob.involves,
        "producer": producer,
    }
    if target is not None:
        meta["target"] = target

    # 1. Validate before touching the disk or the index.
    try:
        model = ObservationFrontmatter.model_validate(meta)
    except ValidationError as e:
        return _invalid(_errors(e), dry_run=dry_run, console=console)

    # 2. Identity, then the policy for an identity-bearing signal.
    resolver = obs.resolver()
    oid = obs.obs_id(model, resolver)
    supersedes: str | None = None
    if oid is not None:
        valid, _malformed = obs.load_all()
        policy = obs.recurrence_policy(obs.index(valid, resolver), oid)
        if isinstance(policy, obs.AlreadyPending):
            if not dry_run:
                console.print(f"  [dim]skip (pending)[/dim] {policy.obs.rel}")
            return WriteOutcome("already_pending", policy.obs.path, obs_id=oid, dry_run=dry_run)
        if isinstance(policy, obs.Suppressed):
            head = policy.head
            if not dry_run:
                console.print(
                    f"  [dim]skip (suppressed: {head.fm.resolution_kind})[/dim] {head.rel}"
                )
            return WriteOutcome(
                "suppressed",
                head.path,
                obs_id=oid,
                dry_run=dry_run,
                resolution_kind=head.fm.resolution_kind,
            )
        if isinstance(policy, obs.Reopen):
            supersedes = policy.head.rel
            # The frontmatter about to be written includes supersedes; validate
            # it now, before both the dry-run return and the write, so a dry-run
            # "reopened" makes the same claim a real one does (safe by
            # construction, not only by the reader's provenance of head.rel).
            try:
                ObservationFrontmatter.model_validate(meta | {"supersedes": supersedes})
            except ValidationError as e:
                return _invalid(_errors(e), dry_run=dry_run, console=console, obs_id=oid)

    # 3. The name. Per-candidate exists() checks, never a listing (M1 guard).
    base = f"{observed}-{ob.category}-{slug}"
    path = paths.OBSERVATIONS / f"{base}.md"
    if path.exists():
        if oid is None:
            if not dry_run:
                console.print(
                    f"  [yellow]skip (exists)[/yellow] {path.relative_to(paths.DATA_ROOT)}"
                )
            return WriteOutcome("exists", path, dry_run=dry_run)
        free = (paths.OBSERVATIONS / f"{base}-{n}.md" for n in range(2, _MAX_SUFFIX + 1))
        path = next((p for p in free if not p.exists()), None)
        if path is None:
            return _invalid(
                f"no free filename: {base}.md and -2 .. -{_MAX_SUFFIX} are all taken",
                dry_run=dry_run,
                console=console,
                obs_id=oid,
            )

    kind: OutcomeKind = "written" if supersedes is None else "reopened"
    if dry_run:
        return WriteOutcome(kind, path, obs_id=oid, dry_run=True)

    # 4. Write, re-validate, ledger.
    if supersedes is not None:
        meta["supersedes"] = supersedes
    rel = _write_checked(path, meta, f"# {ob.title}\n\n{ob.body.strip()}\n", console=console)
    if rel is None:
        # Pre-validated, so only a race (the name was taken since the check)
        # or a re-validation rollback lands here; _write_checked has printed.
        if path.exists():
            return WriteOutcome("exists", path, obs_id=oid)
        return WriteOutcome(
            "invalid", None, obs_id=oid, error="written file failed validation; rolled back"
        )

    payload: dict = {
        "category": ob.category,
        "slug": slug,
        "involves": ob.involves,
        "producer": producer,
    }
    if oid is not None:
        payload["obs_id"] = oid
    if target is not None:
        payload["target"] = target
    if supersedes is not None:
        payload["supersedes"] = supersedes
    ledger.append_event(kind="observation.created", actor=actor, subject=rel, payload=payload)
    if supersedes is None:
        console.print(f"  [yellow]wrote[/yellow] {rel}")
    else:
        console.print(f"  [yellow]reopened[/yellow] {rel} (supersedes {supersedes})")
    return WriteOutcome(kind, path, obs_id=oid)


# @decision DEC-002: suppressions are counted once per run, not per write (D4).
# @rationale: write_observation stays a pure per-signal decision with no state
#   threaded through producers; the entry point (entities gaps, scan, one
#   ingest apply, one reweave_one) hands its outcomes here once, so a run that
#   meets a dismissed signal N times appends one event with count N instead of
#   N lines. Dry-run outcomes are previews and are never ledgered.
def ledger_suppressed(outcomes: Iterable[WriteOutcome], *, actor: str) -> int:
    """Append one `observation.suppressed` (plane projection) per suppressed id.

    Only non-dry-run suppressed outcomes count. Ids in first-seen order; the
    subject is the head's rel; payload {obs_id, count, kind}. Returns the
    number of events appended (0 appends nothing).
    """
    groups: dict[str, list[WriteOutcome]] = {}
    for o in outcomes:
        # A suppressed outcome always has an obs_id: WriteOutcome.__post_init__
        # raises otherwise, so the key below is never None.
        if o.kind == "suppressed" and not o.dry_run:
            groups.setdefault(o.obs_id, []).append(o)
    for oid, group in groups.items():
        first = group[0]
        ledger.append_event(
            plane="projection",
            kind="observation.suppressed",
            actor=actor,
            subject=first.rel,
            payload={"obs_id": oid, "count": len(group), "kind": first.resolution_kind},
        )
    return len(groups)


# -- transitions (IDD §6 writers.py, §10, §10a, D11, D12, D15, P2) -------------------

RefusalReason = Literal[
    "outside",
    "not_found",
    "malformed",
    "not_pending",
    "empty_reason",
    "multiline_reason",
    "unencodable_reason",
]


class ObservationRefused(Exception):
    """An operator-facing refusal: nothing was written, nothing was ledgered.

    `reason` is the machine-readable class (the CLI prints it and exits 1);
    `detail` is the human explanation.
    """

    def __init__(self, reason: RefusalReason, detail: str) -> None:
        self.reason: RefusalReason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


@dataclass(frozen=True)
class Transition:
    """What transition_observation did, or would do on a dry run.

    path: the resolved absolute file (a symlink's target, never the link).
    rel: DATA_ROOT-relative POSIX, the ledger `subject` form.
    obs_id: the id snapshot taken before the patch, or None for prose.
    old_status is always "pending"; new_status is derived from kind
    (done -> promoted, anything else -> archived). resolution is the
    stripped reason; resolved is ledger.today(). dry_run: nothing written.
    """

    path: Path
    rel: str
    obs_id: str | None
    old_status: ObservationStatus
    new_status: ObservationStatus
    kind: ResolutionKind
    resolution: str
    resolved: date
    dry_run: bool = False


def _check_path(path: Path) -> Path:
    """Step 1 (§10a): the resolved file must sit directly in .observations/."""
    try:
        resolved = Path(path).resolve()
    except (OSError, RuntimeError) as e:
        # A symlink loop: Python 3.11/3.12 raise RuntimeError here; 3.13 returns
        # the unresolved path instead (and is_file() below is False). Either
        # way there is no file to transition: refused not_found, never a
        # traceback.
        raise ObservationRefused("not_found", f"{path}: cannot resolve: {e}") from e
    folder = paths.OBSERVATIONS.resolve()
    if resolved.parent != folder or resolved.suffix != ".md":
        raise ObservationRefused(
            "outside", f"{path} does not resolve to a .md file directly under {folder}"
        )
    if not resolved.is_file():
        raise ObservationRefused("not_found", f"{path}: no such observation file")
    return resolved


def _check_reason(reason: str) -> str:
    """Step 2 (P2): non-empty, one line by splitlines (all 8 break characters),
    and encodable as UTF-8 (no lone surrogates)."""
    r = reason.strip()
    if not r:
        raise ObservationRefused("empty_reason", "the reason is empty")
    if r.splitlines() != [r]:
        raise ObservationRefused("multiline_reason", f"the reason must be one line: {r!r}")
    try:
        r.encode("utf-8")
    except UnicodeEncodeError as e:
        # A lone surrogate (non-UTF-8 bytes on argv arrive as \udcXX): YAML
        # would write it escaped, then the ledger event could not serialise it.
        raise ObservationRefused(
            "unencodable_reason", f"the reason is not valid UTF-8 text: {r!r}"
        ) from e
    return r


# @decision DEC-003: on a ledger failure, restore the file iff the ledger line
#   cannot be on disk (D15, IDD §10).
# @rationale: the file is written first and the ledger second, because the
#   ledger records mutations that happened. append_event raises
#   LedgerWriteError for every failure from the first write(2) through close:
#   the line may be on disk, so restoring the file could leave the ledger
#   recording a resolution that never happened; the file stays resolved and a
#   retry is refused not_pending. Any other exception (OPS.mkdir, event
#   construction, open: a read-only mount, permissions) means nothing reached
#   the ledger, so the original bytes are put back and a retry works. Deciding
#   by exception type is the only signal there is: a LedgerError-only rule is
#   unsafe in 3 of 8 failure modes, the subclass in 0 (Demonstrate c1).
#   ENOSPC with 0 bytes written is a LedgerWriteError too: conservative.
def transition_observation(
    path: Path,
    *,
    kind: ResolutionKind,
    reason: str,
    actor: str,
    dry_run: bool = False,
) -> Transition:
    """Resolve (kind done) or archive one pending observation, with a reason.

    Order: path (outside, not_found) -> reason (empty_reason,
    multiline_reason, unencodable_reason) -> parse (malformed) -> pending
    (not_pending); each
    raises ObservationRefused and touches nothing. The file is read once as
    raw bytes (CRLF intact), parsed from those bytes, patched by
    fm.patch_frontmatter (every byte it does not own is copied, D11), and
    the patched text is strictly re-validated before any write; a
    re-validation failure raises RuntimeError and writes nothing. A
    PatchError (a file the patcher cannot touch safely) is refused malformed.

    Then the file is written atomically and one `observation.resolved`
    (plane event) is appended, subject rel, payload {category, old_status,
    new_status, kind, resolution} plus obs_id and target when set. On a
    ledger failure (DEC-003): LedgerWriteError propagates and the file stays
    resolved with possibly no event; any other exception restores the
    original bytes and propagates; if the restore fails too, a RuntimeError
    chained to both is raised and the file is left resolved with no ledger
    event. Both leftovers have the signature Cycle 2 `reconcile` detects: a
    non-pending observation with no `observation.resolved` event for its rel.

    Prints nothing. Cost: builds one Resolver (walks the vault) for the id.
    """
    resolved_path = _check_path(path)
    resolution = _check_reason(reason)
    rel = (paths.OBSERVATIONS.relative_to(paths.DATA_ROOT) / resolved_path.name).as_posix()

    try:
        # Bytes, not read_text(): universal newlines would turn CRLF into LF
        # and the patcher could no longer copy the bytes it does not own.
        raw = resolved_path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as e:
        raise ObservationRefused("malformed", f"{rel}: not UTF-8: {e}") from e
    try:
        meta, _body = fm.load_text(raw, "observation", path=resolved_path)
    except fm.FrontmatterError as e:
        raise ObservationRefused("malformed", f"{rel}: {'; '.join(e.errors)}") from e
    model = ObservationFrontmatter.model_validate(meta)
    if model.status != "pending":
        raise ObservationRefused(
            "not_pending", f"{rel} is already {model.status} ({model.resolution_kind})"
        )

    oid = obs.obs_id(model, obs.resolver())
    new_status: ObservationStatus = "promoted" if kind == "done" else "archived"
    today = ledger.today()
    try:
        patched = fm.patch_frontmatter(
            raw,
            {
                "status": new_status,
                "resolved": today,  # a date renders unquoted
                "resolution": resolution,
                "resolution_kind": kind,
            },
        )
    except fm.PatchError as e:
        raise ObservationRefused("malformed", f"{rel}: cannot patch safely: {e}") from e
    try:
        fm.load_text(patched, "observation", path=resolved_path)
    except fm.FrontmatterError as e:
        raise RuntimeError(
            f"{rel}: patched frontmatter failed strict validation; nothing written: "
            f"{'; '.join(e.errors)}"
        ) from e

    t = Transition(
        path=resolved_path,
        rel=rel,
        obs_id=oid,
        old_status="pending",
        new_status=new_status,
        kind=kind,
        resolution=resolution,
        resolved=today,
        dry_run=dry_run,
    )
    if dry_run:
        return t

    fm.write_text_atomic(resolved_path, patched)
    payload: dict = {
        "category": model.category,
        "old_status": "pending",
        "new_status": new_status,
        "kind": kind,
        "resolution": resolution,
    }
    if oid is not None:
        payload["obs_id"] = oid
    if model.target is not None:
        payload["target"] = model.target
    try:
        ledger.append_event(
            plane="event", kind="observation.resolved", actor=actor, subject=rel, payload=payload
        )
    except ledger.LedgerWriteError:
        raise  # the line may be on disk: never restore (DEC-003)
    except Exception as ledger_err:
        try:
            fm.write_text_atomic(resolved_path, raw)
        except Exception as restore_err:
            # restore_err.__context__ is ledger_err: both are in the chain.
            raise RuntimeError(
                f"{rel}: ledger append failed ({ledger_err!r}) and restoring the "
                f"original bytes failed too ({restore_err!r}); the file is left "
                "resolved with no ledger event (Cycle 2 reconcile detects this)"
            ) from restore_err
        raise
    return t
