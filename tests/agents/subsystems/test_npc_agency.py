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
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.protocol.dispatch import SubsystemDispatch, VisibilityTag


def _tag_all() -> VisibilityTag:
    return VisibilityTag(
        visible_to="all",
        perception_fidelity={},
        secrets_for=[],
        redact_from_narrator_canonical=False,
    )


@pytest.mark.asyncio
async def test_npc_agency_returns_output_with_directive_and_data(minimal_npc_pool):
    """Looking up a known NPC emits a must_narrate directive + structured data."""
    dispatch = SubsystemDispatch(
        subsystem="npc_agency",
        params={"npc_name": "Harlan", "situation": "player enters the inn"},
        depends_on=[],
        idempotency_key="idem:a",
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
async def test_npc_agency_case_insensitive_lookup(minimal_npc_pool):
    """Pool stores 'Harlan'; decomposer may emit 'harlan' — both should resolve."""
    dispatch = SubsystemDispatch(
        subsystem="npc_agency",
        params={"npc_name": "harlan", "situation": "spotted"},  # lowercase!
        depends_on=[],
        idempotency_key="idem:case",
        visibility=_tag_all(),
    )
    out = await run_npc_agency(dispatch, npc_pool=minimal_npc_pool)
    assert out.directives != []
    assert out.data["npc_name"] == "Harlan"  # returns the canonical pool casing
