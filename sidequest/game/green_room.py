"""The Green Room — ADR-156's single-gate NPC materializer (accepted 2026-07-11).

One door onto the stage: every production path that lands an ``Npc`` in
``snapshot.npcs`` routes through :func:`admit`. The ladder arbitrates
IDENTITY (which record is real when feeders collide); it never picks
confrontation targets — that is Amendment A's line (targeting is the
dispatch's named target, `encounter_lifecycle`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sidequest.game.origin import (
    Origin,
    OriginKind,
    derive_origin,
    identity_key,
    normalize_name,
)

if TYPE_CHECKING:
    # Same cycle-avoidance pattern as origin.py:33-37 — ``Npc``/``GameSnapshot``
    # live in ``sidequest.game.session``, which this module must not import at
    # runtime.
    from sidequest.game.session import GameSnapshot, Npc

LADDER: dict[OriginKind, int] = {
    OriginKind.AUTHORED: 1,
    OriginKind.GENERIC: 1,
    OriginKind.ROOM_BOUND: 2,
    OriginKind.REGION_POPULATION: 3,
    OriginKind.MANUAL_POOL: 4,
    OriginKind.NARRATOR_INVENTED: 5,
}
# EPHEMERAL_STUB is deliberately absent: a stub reaching the gate is a
# construction error (162-3 made stub-mint a loud failure) — admit() raises.


@dataclass
class MaterializationCandidate:
    npc: Npc
    origin: Origin
    source: str


@dataclass
class AdmitResult:
    admitted: list[Npc] = field(default_factory=list)
    merged: list[str] = field(default_factory=list)
    aliases_attached: int = 0
    dropped: list[str] = field(default_factory=list)


# The live mechanical state admit() must NEVER reset on an already-
# materialized identity (ADR-139 Invariant 2, now structural):
# core.hp, disposition, belief_state, disposition_log, last_seen_location,
# last_seen_turn, non_transactional_interactions, last_development_turn.

_MERGE_FILL_FIELDS = (
    "pronouns",
    "appearance",
    "age",
    "build",
    "height",
    "location",
    "region",
    "current_room",
    "threat_level",
    "jungian_id",
    "rpg_role_id",
    "npc_role_id",
)


def attach_alias(npc: Npc, alias: str, *, from_source: str) -> bool:
    """Record a prose/stage name against an identity. Dedups against the
    canonical name and existing aliases via normalize_name. Emits
    green_room.alias_attached on a NEW attachment only."""
    q = normalize_name(alias)
    if not q or q == normalize_name(npc.core.name):
        return False
    if any(normalize_name(a) == q for a in npc.aliases):
        return False
    npc.aliases.append(alias)
    from sidequest.telemetry.spans import SPAN_GREEN_ROOM_ALIAS_ATTACHED, Span

    with Span.open(
        SPAN_GREEN_ROOM_ALIAS_ATTACHED,
        {
            "identity_key": identity_key(derive_origin(npc), npc.core.name),
            "alias": alias,
            "from_source": from_source,
        },
    ):
        pass
    return True


def _fill_absent(winner: Npc, donor: Npc) -> None:
    """Additive merge: donor fills the winner's ABSENT fields only."""
    for f in _MERGE_FILL_FIELDS:
        if getattr(winner, f, None) is None and getattr(donor, f, None) is not None:
            setattr(winner, f, getattr(donor, f))
    for df in donor.distinguishing_features:
        if df not in winner.distinguishing_features:
            winner.distinguishing_features.append(df)


def admit(snapshot: GameSnapshot, candidates: Sequence[MaterializationCandidate]) -> AdmitResult:
    from sidequest.telemetry.spans import (
        SPAN_GREEN_ROOM_MATERIALIZED,
        SPAN_GREEN_ROOM_PRECEDENCE_CONFLICT,
        Span,
    )

    result = AdmitResult()
    groups: dict[str, list[MaterializationCandidate]] = {}
    for cand in candidates:
        if cand.origin.kind not in LADDER:
            raise ValueError(
                f"green_room.admit: candidate {cand.npc.core.name!r} carries "
                f"unadmittable origin kind {cand.origin.kind} "
                f"(EPHEMERAL_STUB and unknown kinds never reach the gate — "
                f"No Silent Fallbacks)"
            )
        groups.setdefault(identity_key(cand.origin, cand.npc.core.name), []).append(cand)

    # Within each group, the sample's (LADDER[kind], source) sort picks the
    # group's own canonical. Groups are then VISITED in ladder order (best
    # candidate's rank, tiebroken by source then key) rather than dict-
    # insertion order — otherwise two feeders for the SAME real-world
    # identity that key differently (an authored row and a bestiary-bound
    # pool row for the same creature, say) resolve in arrival order instead
    # of precedence order. See the Controller Adjudication on task-1-brief.
    for group in groups.values():
        group.sort(key=lambda c: (LADDER[c.origin.kind], c.source))

    ordered_keys = sorted(
        groups.keys(),
        key=lambda k: (LADDER[groups[k][0].origin.kind], groups[k][0].source, k),
    )

    # id()s of Npc objects this admit() call itself appended to snapshot.npcs
    # — distinguishes "found a batch-mate from an earlier group in THIS call"
    # (dropped: the later group's candidate never had a real seat to begin
    # with) from "found a pre-existing snapshot entry from a prior admit()
    # call" (merged: an existing seat absorbed new information).
    admitted_this_batch: set[int] = set()

    for key in ordered_keys:
        group = groups[key]
        canonical = group[0]
        if len(group) > 1:
            with Span.open(
                SPAN_GREEN_ROOM_PRECEDENCE_CONFLICT,
                {
                    "identity_key": key,
                    "winning_tier": LADDER[canonical.origin.kind],
                    "losing_tiers": sorted({LADDER[c.origin.kind] for c in group[1:]}),
                },
            ):
                pass
        alias_count = 0
        for loser in group[1:]:
            _fill_absent(canonical.npc, loser.npc)
            if attach_alias(canonical.npc, loser.npc.core.name, from_source=loser.source):
                alias_count += 1
            result.dropped.append(identity_key(loser.origin, loser.npc.core.name))

        existing = _find_existing(snapshot, key, canonical.npc.core.name)
        batch_winner: Npc | None = None
        if existing is not None:
            _fill_absent(existing, canonical.npc)
            if attach_alias(existing, canonical.npc.core.name, from_source=canonical.source):
                alias_count += 1
            for a in canonical.npc.aliases:
                if attach_alias(existing, a, from_source=canonical.source):
                    alias_count += 1
            if id(existing) in admitted_this_batch:
                # The "existing" seat is this SAME admit() call's own earlier
                # group — that group already holds the real, snapshot-
                # appended seat. This group's canonical never had one; it
                # dropped into the earlier seat, not merged onto a prior seat.
                result.dropped.append(key)
                batch_winner = existing
            else:
                result.merged.append(key)
        else:
            canonical.npc.origin = canonical.origin
            snapshot.npcs.append(canonical.npc)
            result.admitted.append(canonical.npc)
            admitted_this_batch.add(id(canonical.npc))
        result.aliases_attached += alias_count

        if batch_winner is not None:
            # Cross-group fold (task-1 rework, reviewer finding): this group's
            # identity did NOT materialize — it folded into a batch-mate's seat
            # the ladder ranked higher. Emitting green_room.materialized here
            # would tell the GM panel this identity won a seat it never had;
            # the honest record is a precedence_conflict carrying the WINNER's
            # identity_key/tier and this group's tier as the loser.
            winner_origin = derive_origin(batch_winner)
            with Span.open(
                SPAN_GREEN_ROOM_PRECEDENCE_CONFLICT,
                {
                    "identity_key": identity_key(winner_origin, batch_winner.core.name),
                    "winning_tier": LADDER[winner_origin.kind],
                    "losing_tiers": [LADDER[canonical.origin.kind]],
                },
            ):
                pass
        else:
            with Span.open(
                SPAN_GREEN_ROOM_MATERIALIZED,
                {
                    "identity_key": key,
                    "canonical_tier": LADDER[canonical.origin.kind],
                    "canonical_source": canonical.source,
                    "candidates_seen": len(group),
                    "candidates_dropped": len(group) - 1,
                    "alias_count": alias_count,
                },
            ):
                pass
    return result


def _find_existing(snapshot: GameSnapshot, key: str, name: str) -> Npc | None:
    """Reconcile by identity key first (id-keyed), then by the resolver's
    name/alias/invented_from legs (catches a legacy unstamped entity whose
    derived key differs)."""
    for npc in snapshot.npcs:
        if identity_key(derive_origin(npc), npc.core.name) == key:
            return npc
    from sidequest.game.origin import resolve_roster_npc

    return resolve_roster_npc(snapshot.npcs, name)
