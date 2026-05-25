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
"""

from __future__ import annotations

from sidequest.agents.subsystems import SubsystemOutput
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.protocol.dispatch import NarratorDirective, SubsystemDispatch


async def run_npc_agency(
    dispatch: SubsystemDispatch,
    *,
    npc_pool: list[NpcPoolMember],
) -> SubsystemOutput:
    """Surface ``NpcPoolMember`` facts as a narrator directive + structured data.

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
            "role": member.role,
            "situation": situation,
        },
    )


__all__ = ["run_npc_agency"]
