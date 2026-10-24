"""The one reader for vault/intake/.observations/ (IDD §5/§6, D1, D7, D13).

Every command that needs observations asks this module; nothing else
enumerates the folder (a source guard in tests/test_source_guards.py
enforces that). One parse path means every reader agrees on what is
pending: an archived or malformed file is never a pending one.

This module never writes and never imports `writers`. Imports point one
way: commands → writers → observations → fm / context / schemas / paths / ledger.

References ``paths`` by module attribute (``paths.OBSERVATIONS``, never
``from .paths import OBSERVATIONS``) so tests redirect the vault without
listing this module in conftest's path-binding tuple.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import context, fm, ledger, paths
from .schemas import ObservationFrontmatter, ObservationStatus
from .util import WIKILINK_RE, slug_fold, slugify


@dataclass(frozen=True)
class Observation:
    """One valid observation file. `category` is the type; this is the instance.

    No body (no reader needs it) and no obs_id: the id needs the vault's
    alias index, and keeping it off the record keeps status polls cheap.
    """

    path: Path  # absolute
    rel: str  # DATA_ROOT-relative, POSIX: the ledger `subject` form
    fm: ObservationFrontmatter


def load_all() -> tuple[list[Observation], list[tuple[Path, str]]]:
    """Every observation file: (valid records, malformed files with the error).

    Both lists are sorted by filename. A malformed file is returned, never
    swallowed and never counted: callers report it (health, triage) or
    ignore it explicitly. Only regular ``*.md`` files directly under the
    folder are read. A missing folder is empty, not an error.

    Cost: parses every observation file on every call (no cache).
    """
    valid: list[Observation] = []
    malformed: list[tuple[Path, str]] = []
    if not paths.OBSERVATIONS.is_dir():
        return valid, malformed
    files = sorted(
        (p for p in paths.OBSERVATIONS.glob("*.md") if p.is_file()), key=lambda p: p.name
    )
    for p in files:
        try:
            meta, _body, _kind = fm.load(p)
        except fm.FrontmatterError as e:
            malformed.append((p, "; ".join(e.errors)))
            continue
        except UnicodeDecodeError as e:
            # Not UTF-8: as malformed as bad YAML, and must not crash a
            # status poll. Anything else (permissions, I/O) propagates.
            malformed.append((p, f"not UTF-8: {e}"))
            continue
        valid.append(
            Observation(
                path=p,
                rel=p.relative_to(paths.DATA_ROOT).as_posix(),
                fm=ObservationFrontmatter.model_validate(meta),
            )
        )
    return valid, malformed


def observations(
    status: ObservationStatus | Literal["all"] = "pending",
) -> list[Observation]:
    """Valid observations with `status` (default: pending), sorted by filename.

    Malformed files are in no status: use load_all() to see them.
    """
    valid, _malformed = load_all()
    if status == "all":
        return valid
    return [o for o in valid if o.fm.status == status]


# -- identity (IDD §5/§6, D8, D8b, D9, D14) -----------------------------------------
#
# A derivable signal (a contradiction's pair, a gap's target) from a trusted
# producer has an identity: obs_id. It is computed on every read from the
# frontmatter and the vault's name index, never stored. `involves` and
# `target` values are TOKENS (IDD §10a): they reach only _component and
# lookup, never Path(...), open or fm.load.

# @decision DEC-001: identity is gated on IDENTITY_SOURCES (D14).
# @rationale: an untrusted source (telegram, R1, web ingest) gets no identity,
#   so it is never indexed and can never suppress or reopen a trusted signal;
#   a label missing here degrades to a duplicate file, never to a lost signal.
#   `source_agent` comes from the writer's `actor` argument, never from model
#   output. Kept separate from SABNYX_TRUSTED_SOURCES, whose default
#   {'sabnyx-ingest'} would strip identity from every scan/entities file (M3).
IDENTITY_SOURCES = frozenset(
    {
        "sabnyx-scan",
        "sabnyx-entities",
        "sabnyx-reweave",
        "sabnyx-tend",
        "sabnyx-cli",
        "sabnyx-ingest",
        "manual",
    }
)


@dataclass(frozen=True)
class Resolver:
    """The vault's names: token -> claiming slugs, stem claims and alias claims apart."""

    stem: Mapping[str, frozenset[str]]
    alias: Mapping[str, frozenset[str]]


def resolver() -> Resolver:
    """Build a Resolver from the vault as it is now.

    Rebuilt on every call (D5, no cache). Cost: walks vault/ and parses every
    page's frontmatter once (context.link_candidates).
    """
    stem, alias = context.link_candidates(context.vault_slug_index())
    return Resolver(stem=stem, alias=alias)


@dataclass(frozen=True)
class Found:
    slug: str


@dataclass(frozen=True)
class Ambiguous:
    claimants: tuple[str, ...]  # sorted


@dataclass(frozen=True)
class Missing:
    pass


Lookup = Found | Ambiguous | Missing


# @decision DEC-002: one lookup rule for identity, gap closure and health (D8b).
# @rationale: the first key with any claim decides, in the order
#   stem[token.lower()], stem[slugify(token)], alias[slugify(token)]; one
#   claimant is Found, two or more are Ambiguous (never guessed). So a stem
#   beats any alias, an alias claimed by two pages is ambiguous, and two stems
#   that slugify alike ('Foo Bar.md', 'foo-bar.md') are ambiguous. Stem and
#   alias claims are kept apart because a merged map cannot tell which
#   claimant is the stem (Demonstrate c3). The stem keys match
#   context.link_resolution_index, so a Found agrees with resolve_wikilink.
def lookup(token: str, resolver: Resolver) -> Lookup:
    """What `token` names in the vault: Found(slug), Ambiguous(claimants) or Missing()."""
    slug_form = slugify(token)
    for table, key in (
        (resolver.stem, token.strip().lower()),
        (resolver.stem, slug_form),
        (resolver.alias, slug_form),
    ):
        claims = table.get(key)
        if claims:
            if len(claims) == 1:
                return Found(next(iter(claims)))
            return Ambiguous(tuple(sorted(claims)))
    return Missing()


def _component(value: str, resolver: Resolver) -> str | None:
    """Normalise one `involves`/`target` token to a slug, or None (no identity).

    The order defends against four slugify traps (probe evidence, IDD §6):
    slugify('') is 'untitled-e3b0c442', slugify('Foo.md') is 'foo-md',
    slugify('atlas/people/foo') is 'atlas-people-foo', and two distinct
    81-character names collide at the default max_len=80. A fifth: slugify
    turns a token with no slug-able content ('[[]]', '!!!') into
    'untitled-<hash>', so the Missing branch uses slug_fold (None instead).
    Splits on '/' only:
    involves values are vault-relative POSIX forms.
    """
    v = value.strip()
    m = WIKILINK_RE.fullmatch(v)
    if m:
        v = m.group(1).strip()  # '[[x|a]]', '[[x#h]]' -> 'x'
    v = v.split("/")[-1]
    if v.lower().endswith(".md"):
        v = v[:-3]
    # One empty check, before any slugify: an empty value stays empty through
    # the split and the .md strip, so this covers both '' and 'foo/.md'.
    v = v.strip()
    if not v:
        return None
    found = lookup(v, resolver)
    if isinstance(found, Found):
        return found.slug
    if isinstance(found, Ambiguous):
        return None
    # slug_fold, not slugify: a token with no slug-able content ('[[]]',
    # '!!!', a fully non-Latin name no page claims) is prose, not an
    # 'untitled-<hash>' identity that could be indexed, suppressed or
    # reopened. D8's empty-string trap, arriving by another road.
    return slug_fold(v, max_len=10_000)


# signal and obs_id take the frontmatter, not the Observation the IDD names
# (`signal(o, resolver)`): write_observation needs the id of metadata before
# any file exists. Call sites with a record pass `o.fm`.
def signal(meta: ObservationFrontmatter, resolver: Resolver) -> list[str] | str | None:
    """The derivable signal of an observation, or None (prose, or untrusted).

    contradiction: exactly two `involves`, both resolvable and distinct ->
    sorted [a, b]. gap: a resolvable `target` -> its slug. Anything else ->
    None.
    """
    if meta.source_agent not in IDENTITY_SOURCES:
        return None
    if meta.category == "contradiction":
        if len(meta.involves) != 2:
            return None
        a, b = (_component(x, resolver) for x in meta.involves)
        if a is None or b is None or a == b:
            return None
        return sorted([a, b])
    if meta.category == "gap" and meta.target is not None:
        return _component(meta.target, resolver)
    return None


# @decision DEC-003: obs_id is computed on every read, never stored (D8).
# @rationale: the id depends on the vault's names (an alias added later moves
#   a token to its page), so a stored id would go stale silently. It is not a
#   frontmatter field nor an Observation attribute; it appears only in ledger
#   payloads, as a snapshot.
def obs_id(meta: ObservationFrontmatter, resolver: Resolver) -> str | None:
    """'sha256:<64 hex>' for an identity-bearing observation, else None."""
    s = signal(meta, resolver)
    if s is None:
        return None
    # content_hash's contract is "canonical JSON -> sha256", not "ledger
    # payloads only": the same canonical form makes the id reproducible.
    return ledger.content_hash({"category": meta.category, "signal": s})


@dataclass(frozen=True)
class ObsIndex:
    """Identity-bearing observations by id. Records without an id are absent.

    pending:    id -> pending records, filename order (duplicates possible).
    resolved:   id -> chain head (see index()).
    multi_head: id -> every head candidate, sorted by rel, when the head is not
                unique by `supersedes`: >=2 unsuperseded resolved records, or
                every record of the id superseded (a forged cycle).
    dangling:   records whose `supersedes` names a rel not among the records.
    mismatched: records whose `supersedes` names a record with another id or
                none (includes records that themselves have no id).
    The last three are for health warnings; tuples are sorted by rel.
    """

    pending: dict[str, list[Observation]]
    resolved: dict[str, Observation]
    multi_head: dict[str, tuple[Observation, ...]]
    dangling: tuple[Observation, ...]
    mismatched: tuple[Observation, ...]


def _by_name(o: Observation) -> tuple[str, str]:
    return (o.path.name, o.rel)


def _head_key(o: Observation) -> tuple:
    # Resolved records always carry `resolved` (strict status<->kind schema).
    return (o.fm.resolved, o.fm.observed, o.rel)


# @decision DEC-004: the chain head follows `supersedes`, not filename order (D9).
# @rationale: sorted([...foo.md, ...foo-2.md, ...foo-3.md]) puts -2 and -3
#   first ('-' 0x2d < '.' 0x2e), so "latest by filename" picks the OLDEST file.
#   The head is the resolved record that no record of the same id (any status)
#   supersedes; ties break by max (resolved, observed, rel), i.e. the latest
#   resolution wins, and are reported in multi_head. Only same-id edges count:
#   an edge to an unknown rel is dangling, to another id mismatched, and
#   neither removes a head. If every record of an id is superseded (a forged
#   cycle) every resolved one is a candidate: the id never falls through to
#   New, which would re-file a dismissed signal (P3).
def index(records: Iterable[Observation], resolver: Resolver) -> ObsIndex:
    """Index records by obs_id. Deterministic: input order does not matter.

    Cost: one obs_id per record (pure, given the resolver); reads no files.
    """
    recs = sorted(records, key=_by_name)
    ids = {o.rel: obs_id(o.fm, resolver) for o in recs}
    by_rel = {o.rel: o for o in recs}

    superseded: set[str] = set()
    dangling: list[Observation] = []
    mismatched: list[Observation] = []
    for o in recs:
        if o.fm.supersedes is None:
            continue
        prev = by_rel.get(o.fm.supersedes)
        if prev is None:
            dangling.append(o)
        elif ids[o.rel] is None or ids[prev.rel] != ids[o.rel]:
            mismatched.append(o)
        else:
            superseded.add(prev.rel)

    pending: dict[str, list[Observation]] = {}
    closed: dict[str, list[Observation]] = {}
    for o in recs:
        oid = ids[o.rel]
        if oid is None:
            continue
        group = pending if o.fm.status == "pending" else closed
        group.setdefault(oid, []).append(o)

    resolved: dict[str, Observation] = {}
    multi_head: dict[str, tuple[Observation, ...]] = {}
    for oid, done in closed.items():
        heads = [o for o in done if o.rel not in superseded]
        if not heads:
            if any(o.rel not in superseded for o in pending.get(oid, [])):
                # A reopen in progress: the chain's tip is pending, so the id
                # is AlreadyPending; there is no resolved head yet.
                continue
            heads = done  # every record superseded: a forged cycle
            multi_head[oid] = tuple(sorted(heads, key=lambda o: o.rel))
        elif len(heads) > 1:
            multi_head[oid] = tuple(sorted(heads, key=lambda o: o.rel))
        resolved[oid] = max(heads, key=_head_key)

    # P3: an id with a resolved record never falls through to New. An explicit
    # raise, not an assert (python -O strips asserts).
    if any(oid not in resolved and oid not in pending for oid in closed):
        raise RuntimeError("index: an id with a resolved record has no head and no pending record")
    return ObsIndex(
        pending=pending,
        resolved=resolved,
        multi_head=multi_head,
        dangling=tuple(sorted(dangling, key=lambda o: o.rel)),
        mismatched=tuple(sorted(mismatched, key=lambda o: o.rel)),
    )


@dataclass(frozen=True)
class New:
    pass


@dataclass(frozen=True)
class AlreadyPending:
    obs: Observation


@dataclass(frozen=True)
class Reopen:
    head: Observation


@dataclass(frozen=True)
class Suppressed:
    head: Observation


Policy = New | AlreadyPending | Reopen | Suppressed


def recurrence_policy(idx: ObsIndex, oid: str) -> Policy:
    """What a producer does with a signal of id `oid`. Four checks, in this order."""
    pending = idx.pending.get(oid)
    if pending:
        return AlreadyPending(pending[0])
    head = idx.resolved.get(oid)
    if head is not None and head.fm.resolution_kind == "done":
        return Reopen(head)
    if head is not None:
        return Suppressed(head)
    return New()
