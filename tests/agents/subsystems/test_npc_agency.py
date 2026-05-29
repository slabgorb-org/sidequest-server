"""Tests for npc_agency subsystem (wraps the post-Wave-2A NPC pool).

Story 45-52 cleanup: ``npc_registry`` was dropped; ``run_npc_agency`` now
takes ``npc_pool: list[NpcPoolMember]``. Pool members carry identity only —
the ``last_seen_*`` fields that lived on the legacy registry entry are
gone (they live on a promoted ``Npc`` instead).
"""

from __future__ import annotations

import pytest

from sidequest.agents.subsystems import SubsystemOutput
from sidequest.agents.subsystems.npc_agency import run_npc_agency
from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.disposition import Disposition
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.session import Npc
from sidequest.protocol.dispatch import SubsystemDispatch, VisibilityTag


def _tag_all() -> VisibilityTag:
    return VisibilityTag(
        visible_to="all",
        perception_fidelity={},
        secrets_for=[],
        redact_from_narrator_canonical=False,
    )


def _roster_npc(name: str, *, disposition: int = 0) -> Npc:
    """An authored-roster Npc (lives in snapshot.npcs, never in npc_pool)."""
    return Npc(
        core=CreatureCore(
            name=name,
            description="An NPC.",
            personality="Neutral.",
            level=1,
            xp=0,
            statuses=[],
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        disposition=Disposition(value=disposition),
    )


@pytest.mark.asyncio
async def test_npc_agency_returns_output_with_directive_and_data(minimal_npc_pool):
    """Looking up a known NPC emits a must_narrate directive + structured data."""
    dispatch = SubsystemDispatch(
        subsystem="npc_agency",
        params={"npc_name": "Harlan", "situation": "player enters the inn"},
        depends_on=[],
        idempotency_key="idem:a",
        confidence=1.0,
        visibility=_tag_all(),
    )
    out = await run_npc_agency(dispatch, npc_pool=minimal_npc_pool)
    assert isinstance(out, SubsystemOutput)
    assert out.data["npc_name"] == "Harlan"
    assert out.data["role"] == "innkeeper"
    assert len(out.directives) == 1
    d = out.directives[0]
    assert d.kind == "must_narrate"
    assert "Harlan" in d.payload
    assert "innkeeper" in d.payload.lower()


@pytest.mark.asyncio
async def test_npc_agency_unknown_npc_returns_no_directive_with_error_data(minimal_npc_pool):
    """Unknown NPC name yields empty directives + diagnostic data, not an exception."""
    dispatch = SubsystemDispatch(
        subsystem="npc_agency",
        params={"npc_name": "NotAnNpc", "situation": "x"},
        depends_on=[],
        idempotency_key="idem:b",
        confidence=1.0,
        visibility=_tag_all(),
    )
    out = await run_npc_agency(dispatch, npc_pool=minimal_npc_pool)
    assert out.directives == []
    assert out.data.get("error") == "npc_not_registered"
    assert out.data.get("npc_name") == "NotAnNpc"


@pytest.mark.asyncio
async def test_npc_agency_skips_with_structured_data_when_npc_name_missing(
    minimal_npc_pool,
):
    """Regression: previously raised `ValueError("npc_agency requires
    params.npc_name")`. The local_dm decomposer emits opening-crisis
    `npc_agency` cascades on turn 1 of every fresh game across packs,
    before any NPCs are in the pool — raising fired
    `subsystems.dispatch_failed` warnings every fresh game (playtest
    2026-04-25 [P3-MED]). Now returns an empty-directive output with
    structured `error: no_npc_name` + `skipped: True` so the GM panel
    sees the skip via the dispatcher's normal `data` channel without
    polluting the WARNING stream.
    """
    dispatch = SubsystemDispatch(
        subsystem="npc_agency",
        params={"situation": "x"},
        depends_on=[],
        idempotency_key="idem:c",
        confidence=1.0,
        visibility=_tag_all(),
    )
    out = await run_npc_agency(dispatch, npc_pool=minimal_npc_pool)
    assert out.directives == []
    assert out.data["error"] == "no_npc_name"
    assert out.data["skipped"] is True
    assert out.data["situation"] == "x"


@pytest.mark.asyncio
async def test_npc_agency_handles_npc_with_null_role():
    """Directive stays grammatical when optional fields are None (fresh auto-mint)."""
    pool = [
        NpcPoolMember(
            name="Stranger",
            role=None,
            pronouns=None,
            appearance=None,
            drawn_from="narrator_invented",
        )
    ]
    dispatch = SubsystemDispatch(
        subsystem="npc_agency",
        params={"npc_name": "Stranger", "situation": "spotted"},
        depends_on=[],
        idempotency_key="idem:null-optionals",
        confidence=1.0,
        visibility=_tag_all(),
    )
    out = await run_npc_agency(dispatch, npc_pool=pool)
    assert len(out.directives) == 1
    payload = out.directives[0].payload
    # No double spaces anywhere.
    assert "  " not in payload
    # Still mentions the NPC and the situation.
    assert "Stranger" in payload
    assert "spotted" in payload


@pytest.mark.asyncio
async def test_npc_agency_resolves_roster_npc_not_in_pool(minimal_npc_pool):
    """Playtest #C1: a disposition read on an AUTHORED roster NPC (in
    snapshot.npcs, NOT in npc_pool) must engage. Before the fix, run_npc_agency
    looked up only npc_pool and returned npc_not_registered for every roster
    NPC — the crew, Old Tam — so the subsystem never fired for the game's
    primary NPCs.
    """
    old_tam = _roster_npc("Old Tam", disposition=40)  # friendly
    dispatch = SubsystemDispatch(
        subsystem="npc_agency",
        params={"npc_name": "Old Tam", "situation": "the doctor studies his face"},
        depends_on=[],
        idempotency_key="idem:roster",
        confidence=1.0,
        visibility=_tag_all(),
    )
    # npc_pool deliberately does NOT contain Old Tam — he is a roster NPC.
    out = await run_npc_agency(dispatch, npc_pool=minimal_npc_pool, npcs=[old_tam])
    assert len(out.directives) == 1
    assert out.data["npc_name"] == "Old Tam"
    assert out.data["source"] == "npcs_roster"
    # The ADR-020 disposition is surfaced (mechanical legibility for the
    # GM panel + the narrator directive).
    assert out.data["disposition"] == "friendly"
    assert out.data["disposition_value"] == 40
    assert "friendly" in out.directives[0].payload


@pytest.mark.asyncio
async def test_npc_agency_roster_takes_precedence_over_pool(minimal_npc_pool):
    """When a name is in both stores, the authoritative roster wins (it carries
    disposition; the pool is the walk-on fallback)."""
    harlan_roster = _roster_npc("Harlan", disposition=-50)  # hostile
    dispatch = SubsystemDispatch(
        subsystem="npc_agency",
        params={"npc_name": "Harlan", "situation": "x"},
        depends_on=[],
        idempotency_key="idem:precedence",
        confidence=1.0,
        visibility=_tag_all(),
    )
    out = await run_npc_agency(dispatch, npc_pool=minimal_npc_pool, npcs=[harlan_roster])
    assert out.data["source"] == "npcs_roster"
    assert out.data["disposition"] == "hostile"


@pytest.mark.asyncio
async def test_npc_agency_falls_back_to_pool_when_not_in_roster(minimal_npc_pool):
    """A name only in npc_pool (a narrator-invented walk-on) still resolves via
    the pool path with source=npc_pool."""
    dispatch = SubsystemDispatch(
        subsystem="npc_agency",
        params={"npc_name": "Harlan", "situation": "x"},
        depends_on=[],
        idempotency_key="idem:poolfallback",
        confidence=1.0,
        visibility=_tag_all(),
    )
    out = await run_npc_agency(dispatch, npc_pool=minimal_npc_pool, npcs=[])
    assert len(out.directives) == 1
    assert out.data["npc_name"] == "Harlan"
    assert out.data["source"] == "npc_pool"


@pytest.mark.asyncio
async def test_npc_agency_case_insensitive_lookup(minimal_npc_pool):
    """Pool stores 'Harlan'; decomposer may emit 'harlan' — both should resolve."""
    dispatch = SubsystemDispatch(
        subsystem="npc_agency",
        params={"npc_name": "harlan", "situation": "spotted"},  # lowercase!
        depends_on=[],
        idempotency_key="idem:case",
        confidence=1.0,
        visibility=_tag_all(),
    )
    out = await run_npc_agency(dispatch, npc_pool=minimal_npc_pool)
    assert out.directives != []
    assert out.data["npc_name"] == "Harlan"  # returns the canonical pool casing
