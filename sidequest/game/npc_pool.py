"""NPC pool members — identity-only entries that the narrator can cite as
"people who exist in this world." Regenerable; no mechanical state. Promote
to ``Npc`` (with ``pool_origin = member.name``) when the NPC actually engages
mechanically (combat handshake, persistent dialog state).

Distinct from ``Npc`` (sidequest.game.session) which carries CreatureCore,
EdgePool, beliefs, and last-seen tracking. The split was Wave 2A of the
snapshot split-brain cleanup (spec:
docs/superpowers/specs/2026-05-04-snapshot-split-brain-cleanup-design.md).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from sidequest.game.disposition import Disposition

if TYPE_CHECKING:
    # ``Npc`` lives in ``sidequest.game.session``, which already imports THIS
    # module (``from sidequest.game.npc_pool import NpcPoolMember``). Importing
    # it at runtime here would be a circular import, so it is annotation-only.
    from sidequest.game.session import Npc


class NpcPoolMember(BaseModel):
    """Identity-only member of the world's NPC cast pool.

    Pool members exist as scaffolding for narrator name-continuity: when the
    narrator wants to introduce "the bartender at the Black Hart," the pool
    provides a name + appearance hook so the same character can be re-cited
    in a later narration without drift.

    Pool members are re-citable, not consumed. When the same name engages
    mechanically (combat, persistent dialog), an ``Npc`` is created with
    ``pool_origin = self.name``; the pool member remains in
    ``GameSnapshot.npc_pool`` and is shadowed by the ``Npc`` lookup at
    narration_apply time.
    """

    model_config = {"extra": "forbid"}

    name: str
    role: str | None = None
    pronouns: str | None = None
    appearance: str | None = None
    disposition: Disposition = Field(default_factory=Disposition)
    """Story 72-2 (epic 72 — NPC Identity Hardening): the relationship score
    the scaffold carries so a known disposition survives the pool→``Npc``
    promotion (``_promote_pool_member_to_npc``) instead of silently
    flattening to neutral. Defaults neutral-0 — a narrator-invented or
    legacy member enters the pool with no recorded relationship, so promotion
    still spawns it neutral (preserving the Story 72-5 born-neutral default).
    The NPC *development* pipeline (disposition drift) is 72-1's deliverable;
    this field only preserves and round-trips an existing value."""
    archetype_id: str | None = None
    """OTEL attribution back to the genre-pack archetype source. ``None``
    for narrator-invented members or legacy-migrated members where
    provenance was lost."""
    drawn_from: str
    """Source tag: ``"name_generator"``, ``"world_authored"``,
    ``"legacy_registry"``, ``"narrator_invented"``,
    ``"dialogue_extraction"`` (Story 49-2)."""
    observation_pending: bool = False
    """Story 49-6 ratification gate flag. ``True`` means the member was
    auto-minted from prose this turn and has not yet been re-cited by
    the narrator on a subsequent turn. The gate evaluates pending members
    each turn and either flips the flag to ``False`` (promote — narrator
    cited the member again, treat as canonical) or removes the entry
    entirely (purge — narrator dropped the one-off mention, do not pin
    a phantom NPC).

    Default ``False`` keeps legacy snapshots, world-authored, and
    name-generator-sourced members exempt: they enter the pool already
    ratified. Only ``_auto_mint_prose_only_npcs`` flags new entries as
    pending."""
    is_creature: bool = False
    """ping-pong #74: this member is a wild animal / beast / monster, not a
    person. Set from ``NpcMention.is_creature`` at the invented-name seam.
    A creature belongs to no culture or faction, so it keeps the narrator's
    descriptive name verbatim — it is NEVER routed through the culture-bound
    person namer (which would mint a person-name + a random culture). Defaults
    ``False`` so every existing / authored / person member stays a person.
    The full Monster Manual identity (creature_id / threat_level / hp / stat
    block, ADR-059) is wired at promotion time via ``_promote_creature_to_npc``
    in narration_apply.py (story 83-1)."""
    creature_data: dict | None = None
    """Story 83-1: pre-fetched encountergen enemy dict (Monster Manual shape)
    embedded at pool-mint time so the promotion seam is self-contained.
    ``None`` for pool members minted without MM context (narrator-invented
    creatures receive synthetic bestiary identity at promotion time via
    ``_synthetic_creature_dict``)."""


def is_projectable(entity: NpcPoolMember | Npc) -> bool:
    """Whether ``entity`` is eligible to be projected to any downstream surface.

    ADR-138 §D1/D3: ratification is the single projection-eligibility gate, shared
    by every projection surface (the ADR-118 retrieval index and the ADR-135 public
    reference page) so none re-implements the rule.

    - A ``NpcPoolMember`` is projectable iff it is **ratified**
      (``observation_pending is False``). An unratified, auto-minted member is a
      phantom the Story 49-6 gate may purge next turn; the world has not committed
      to it, so it must not be embedded or rendered as if it had.
    - A promoted ``Npc`` (``sidequest.game.session.Npc``) is **always** projectable —
      promotion to the mechanical entity is itself the world's commitment.

    This is the §D3 predicate only; wiring it into the projection surfaces is
    deferred to stories 75-12 (ADR-118) and 75-13 (ADR-135).
    """
    if isinstance(entity, NpcPoolMember):
        return not entity.observation_pending
    return True
