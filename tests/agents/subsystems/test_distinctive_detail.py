"""Tests for distinctive_detail_hint subsystem (spec §6.2)."""

from __future__ import annotations

import pytest

from sidequest.agents.subsystems.distinctive_detail import run_distinctive_detail
from sidequest.protocol.dispatch import SubsystemDispatch, VisibilityTag


def _tag_all() -> VisibilityTag:
    return VisibilityTag(
        visible_to="all",
        perception_fidelity={},
        secrets_for=[],
        redact_from_narrator_canonical=False,
    )


@pytest.mark.asyncio
async def test_distinctive_detail_emits_single_narrator_directive():
    dispatch = SubsystemDispatch(
        subsystem="distinctive_detail_hint",
        params={"target": "npc:goblin_2", "hint": "broken tooth"},
        depends_on=[],
        idempotency_key="idem:a",
        visibility=_tag_all(),
    )
    out = await run_distinctive_detail(dispatch)
    directives = out.directives
    assert len(directives) == 1
    d = directives[0]
    assert d.kind == "distinctive_detail_for_referent"
    assert "npc:goblin_2" in d.payload
    assert "broken tooth" in d.payload


@pytest.mark.asyncio
async def test_distinctive_detail_degrades_to_noop_on_missing_target():
    """LLM-emitted dispatch missing target → no-op + data['error'].

    Raising would spam ValueError into the orchestrator log every time
    the decomposer LLM emits a malformed dispatch (~once per session in
    practice). Degrading to a no-op surfaces the failure via the bank's
    span attribute (``data["error"]``) without breaking the narration
    pipeline.
    """
    dispatch = SubsystemDispatch(
        subsystem="distinctive_detail_hint",
        params={"hint": "broken tooth"},  # missing target
        depends_on=[],
        idempotency_key="idem:b",
        visibility=_tag_all(),
    )
    out = await run_distinctive_detail(dispatch)
    assert out.directives == []
    assert out.data["error"] == "missing_params.target"


@pytest.mark.asyncio
async def test_distinctive_detail_degrades_to_noop_on_missing_hint():
    """LLM-emitted dispatch missing hint → no-op + data['error']."""
    dispatch = SubsystemDispatch(
        subsystem="distinctive_detail_hint",
        params={"target": "npc:goblin_2"},  # missing hint
        depends_on=[],
        idempotency_key="idem:c",
        visibility=_tag_all(),
    )
    out = await run_distinctive_detail(dispatch)
    assert out.directives == []
    assert out.data["error"] == "missing_params.hint"
    assert out.data["target"] == "npc:goblin_2"
