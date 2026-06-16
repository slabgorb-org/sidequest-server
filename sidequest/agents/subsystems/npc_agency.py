"""npc_agency — Live-path subsystem (story 59-7).

Wired onto the live turn path via Intent Router dispatch (ADR-113).
The bank executor calls this handler when the router emits an
``npc_agency`` dispatch; the handler surfaces NPC identity facts as
narrator directives so the narrator references established NPCs
rather than inventing new ones.

Wave 2A signature note (story 45-52): the subsystem accepts ``npc_pool``
(``list[NpcPoolMember]``) instead of the dropped ``npc_registry``. Pool
members are identity-only — they carry name / role / pronouns / appearance
but not last-seen tracking.

Playtest #C1 (2026-05-28, coyote_star): the subsystem ALSO accepts ``npcs``
(the authored roster, ``list[Npc]``). Roster NPCs are the game's primary,
persistent NPCs (the Kestrel crew, Old Tam) — they are deliberately NOT
mirrored into ``npc_pool`` (their presence is tracked via
``Npc.last_seen_location``, see ``narration_apply._apply_npc_mentions``).
Resolving the dispatch target against ``npc_pool`` alone meant npc_agency
returned ``npc_not_registered`` for every roster NPC and NEVER engaged for
the NPCs that actually matter — a disposition read on Old Tam or the crew
produced no directive and no mechanical provenance (pure narrator improv,
the exact Illusionism the OTEL lie-detector exists to catch). The roster is
resolved FIRST (it is authoritative and carries the ADR-020 disposition the
directive surfaces); the pool is the walk-on fallback.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sidequest.agents.subsystems import SubsystemOutput
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.protocol.dispatch import NarratorDirective, SubsystemDispatch

if TYPE_CHECKING:
    from sidequest.game.session import Npc


async def run_npc_agency(
    dispatch: SubsystemDispatch,
    *,
    npc_pool: list[NpcPoolMember],
    npcs: list[Npc] | None = None,
) -> SubsystemOutput:
    """Surface NPC identity + disposition facts as a narrator directive + data.

    Resolves the dispatch target against the authored roster (``npcs``)
    first, then the present-in-scene ``npc_pool``. Roster hits carry the
    NPC's live ADR-020 disposition, which the directive surfaces so the
    narrator's portrayal is anchored to mechanical state (and the GM panel
    can see the disposition that drove the scene — Sebastien/Jade want this
    legible).

    Tolerates a missing ``params.npc_name`` (returns empty directives + a
    structured skip marker) rather than raising — the local_dm decomposer
    emits opening-crisis ``npc_agency`` cascades on turn 1 of every fresh
    game across packs, before any NPCs are in the pool. Raising fired
    ``subsystems.dispatch_failed`` + ``orchestrator.subsystem_error``
    warnings on every fresh-game first turn (playtest 2026-04-25 [P3-MED]);
    the structured skip surfaces in the GM panel via the dispatcher's
    normal ``data`` channel without polluting the warning stream.
    """
    npc_name = dispatch.params.get("npc_name")
    situation = dispatch.params.get("situation", "unspecified")
    if not npc_name:
        return SubsystemOutput(
            directives=[],
            data={
                "error": "no_npc_name",
                "skipped": True,
                "rationale": (
                    "dispatch arrived without params.npc_name "
                    "(typically an opening-crisis cascade before any NPC "
                    "is registered); no-op is correct for the empty-pool case"
                ),
                "situation": situation,
            },
        )

    needle = npc_name.lower()

    # Step 1: authored roster (snapshot.npcs). Authoritative and carries
    # disposition; roster NPCs are not in npc_pool (playtest #C1).
    roster = npcs or []
    npc_hit = next((n for n in roster if n.core.name.lower() == needle), None)
    if npc_hit is not None:
        attitude = npc_hit.disposition.attitude().value
        payload = (
            f"{npc_hit.core.name} (disposition toward the party: {attitude}) "
            f"responds to {situation} consistent with that established "
            f"disposition. Do not invent a new identity or relocate them silently."
        )
        directive = NarratorDirective(
            kind="must_narrate",
            payload=payload,
            visibility=dispatch.visibility,
        )
        return SubsystemOutput(
            directives=[directive],
            data={
                "npc_name": npc_hit.core.name,
                "source": "npcs_roster",
                "disposition": attitude,
                "disposition_value": npc_hit.disposition.value,
                "situation": situation,
            },
        )

    # Step 2: present-in-scene pool (narrator-invented walk-ons).
    member = next((m for m in npc_pool if m.name.lower() == needle), None)
    if member is None:
        return SubsystemOutput(
            directives=[],
            data={"error": "npc_not_registered", "npc_name": npc_name},
        )

    name_part = f"{member.name} ({member.role})" if member.role else member.name
    payload = (
        f"{name_part} responds to {situation} consistent with their "
        f"established role. Do not invent a new identity or relocate them silently."
    )
    directive = NarratorDirective(
        kind="must_narrate",
        payload=payload,
        visibility=dispatch.visibility,
    )
    return SubsystemOutput(
        directives=[directive],
        data={
            "npc_name": member.name,
            "source": "npc_pool",
            "role": member.role,
            "situation": situation,
        },
    )


__all__ = ["run_npc_agency"]
