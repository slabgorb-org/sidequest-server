"""Typed NPC/creature provenance and the id-keyed identity surface (story 162-2).

Survey ``docs/superpowers/specs/2026-07-05-npc-generation-inventory.md`` §4
conflict #7 / §8 D1-D2: identity was a name string at every seam (MM dedup,
seater matching, pool promotion), each seam with its own normalization, and
provenance was smeared across partial fields. This module is the single typed
view over the legacy provenance trio ``invented_from`` / ``manual_origin`` /
``creature_id`` — reuse-first: the legacy fields stay for JSON round-trip;
``Origin`` is derived from them for old saves and stamped by creation paths
going forward (extending 162-1's derive-don't-cache doctrine to identity: no
save-file migration, ever). ``pool_origin`` is a DELIBERATE exclusion (rework
round 1, reviewer audit): it records *which pool member* an Npc was promoted
from — promotion lineage, not a creation family — and stays a separate field;
it influences derivation only by falling through to NARRATOR_INVENTED.

Four seams consume this module today: the combat opponent seeder and the
108-2 roster conscription (``encounter_lifecycle``), the authored-vs-
procedural dedup in ``monster_manual_inject.inject``, and the session-start
authored preload (``world_materialization.preload_authored_npcs`` — the one
place that stamps AUTHORED and carries ``authored_id``).
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel

from sidequest.foundation.slug_fold import fold_to_ascii

if TYPE_CHECKING:
    # ``Npc`` lives in ``sidequest.game.session``, which imports THIS module
    # for the ``Npc.origin`` field — annotation-only here to avoid the cycle
    # (same pattern as ``sidequest.game.npc_pool``).
    from sidequest.game.session import Npc


class OriginKind(StrEnum):
    """The creation families that land an ``Npc`` (§3a's six, plus GENERIC).

    The narrator-mention and prose-extraction paths share
    ``NARRATOR_INVENTED`` — both are narrator mints (§3a paths 2/3).
    ``GENERIC`` (story 162-3) is the sanctioned last-resort seat drawn from
    the world bestiary's authored ``generics:`` section — the precedence rung
    that replaced default-path ``EPHEMERAL_STUB`` fabrication. Like AUTHORED
    and ROOM_BOUND it is stamped at creation, never derived from legacy
    fields.
    """

    AUTHORED = "authored"
    ROOM_BOUND = "room_bound"
    REGION_POPULATION = "region_population"
    MANUAL_POOL = "manual_pool"
    NARRATOR_INVENTED = "narrator_invented"
    EPHEMERAL_STUB = "ephemeral_stub"
    GENERIC = "generic"


class Origin(BaseModel):
    """Typed provenance stamp. ``kind`` is required — an Origin with no kind
    is a construction error, never a guessed default (No Silent Fallbacks)."""

    model_config = {"extra": "forbid"}

    kind: OriginKind
    creature_id: str | None = None
    """Stable bestiary id, when creature-derived."""
    authored_id: str | None = None
    """``AuthoredNpc.id``, when world-authored."""
    invented_from: str | None = None
    """The narrator's ORIGINAL invented name when the ADR-091 culture namer
    rerouted it (the perseus double-mint binding)."""
    content_version: str | None = None
    """Content sha the stamp was minted against. RESERVED: no creation path
    stamps it yet (every builder leaves it None) — it exists so a future
    story can key staleness the way 162-1's ``reconcile_content`` does,
    without a model change."""


def normalize_name(name: str) -> str:
    """THE single name normalization for every identity seam: fold diacritics
    to their ASCII base (rework round 1, review [RULE] — reuses the shared
    :func:`sidequest.foundation.slug_fold.fold_to_ascii` primitive the
    alias-matching seam already depends on, so "Veyra Solnë" and "veyra
    solne" are one identity), then casefold, strip, and collapse internal
    whitespace. Replaces the per-seam divergence (exact / ``.lower()`` /
    casefold) the survey called out."""
    return " ".join(fold_to_ascii(name).split()).casefold()


def identity_key(origin: Origin | None, display_name: str) -> str:
    """The dedup/purge/seating key (AC2).

    Precedence: authored id, then creature id, then the normalized display
    name. ``origin=None`` (a legacy, unstamped entity) keys on the normalized
    name — the same key an id-less stamped origin produces, so legacy and new
    entities dedup against each other.
    """
    if origin is not None:
        if origin.authored_id:
            return f"authored:{origin.authored_id}"
        if origin.creature_id:
            return f"creature:{origin.creature_id}"
    return f"name:{normalize_name(display_name)}"


def derive_origin(npc: Npc) -> Origin:
    """Resolve an ``Npc``'s typed origin: a stamped ``npc.origin`` wins
    verbatim; otherwise derive from the legacy provenance fields.

    Legacy mapping (see test_162_2_origin_model — the derivation table):

    * ``ephemeral=True`` → EPHEMERAL_STUB (the 108-2 seater fabrication)
    * ``manual_origin=True`` + ``region`` set → REGION_POPULATION (``region``
      is stamped only by the ADR-106 region-population inject)
    * ``manual_origin=True`` → MANUAL_POOL
    * otherwise → NARRATOR_INVENTED

    AUTHORED, ROOM_BOUND and GENERIC are NOT derivable from legacy fields (an
    authored Npc carries no legacy marker; a room-bound patch is
    byte-identical to an encounter patch; a generics seat predates no save
    written before 162-3) — those paths stamp ``origin`` at creation.
    """
    if npc.origin is not None:
        return npc.origin
    if npc.ephemeral:
        return Origin(
            kind=OriginKind.EPHEMERAL_STUB,
            creature_id=npc.creature_id,
            invented_from=npc.invented_from,
        )
    if npc.manual_origin:
        kind = OriginKind.REGION_POPULATION if npc.region else OriginKind.MANUAL_POOL
        return Origin(
            kind=kind,
            creature_id=npc.creature_id,
            invented_from=npc.invented_from,
        )
    return Origin(
        kind=OriginKind.NARRATOR_INVENTED,
        creature_id=npc.creature_id,
        invented_from=npc.invented_from,
    )


def resolve_roster_npc(npcs: Sequence[Npc], name: str) -> Npc | None:
    """THE single roster lookup every identity seam shares.

    Resolution order (whole-roster passes, so a canonical name always
    outranks another entity's alias):

    1. canonical name (``normalize_name`` on both sides)
    2. alias ledger (``npc.aliases``)
    3. ``invented_from`` (the perseus original→mint binding)

    A hit through leg 2 or 3 is an identity DERIVATION — the engine asserting
    "prose name X is entity Y" — and emits ``identity.resolved`` so the GM
    panel can verify the binding (CLAUDE.md OTEL principle). An exact
    canonical hit derives nothing and emits nothing (no span spam). Blank or
    unknown names return ``None``.
    """
    query = normalize_name(name)
    if not query:
        return None
    for npc in npcs:
        if normalize_name(npc.core.name) == query:
            return npc
    via: str | None = None
    hit: Npc | None = None
    for npc in npcs:
        if any(normalize_name(alias) == query for alias in npc.aliases):
            via, hit = "alias", npc
            break
    if hit is None:
        for npc in npcs:
            if npc.invented_from and normalize_name(npc.invented_from) == query:
                via, hit = "invented_from", npc
                break
    if hit is None:
        return None

    from sidequest.telemetry.spans import SPAN_IDENTITY_RESOLVED, Span

    with Span.open(
        SPAN_IDENTITY_RESOLVED,
        {
            "query": name,
            "canonical": hit.core.name,
            "via": via or "",
            "identity_key": identity_key(derive_origin(hit), hit.core.name),
        },
    ):
        pass
    return hit
