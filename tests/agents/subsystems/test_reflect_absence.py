"""Tests for the reflect_absence subsystem.

Spec: decomposer-design.md §6.3 — unresolvable referent path.
"""

from __future__ import annotations

import pytest

from sidequest.agents.subsystems.reflect_absence import run_reflect_absence
from sidequest.protocol.dispatch import SubsystemDispatch, VisibilityTag


def _tag_all() -> VisibilityTag:
    return VisibilityTag(
        visible_to="all",
        perception_fidelity={},
        secrets_for=[],
        redact_from_narrator_canonical=False,
    )


@pytest.mark.asyncio
async def test_reflect_absence_emits_must_not_and_must_directives():
    dispatch = SubsystemDispatch(
        subsystem="reflect_absence",
        params={"addressee_hint": "no party"},
        depends_on=[],
        idempotency_key="idem:t:p:0",
        visibility=_tag_all(),
    )
    out = await run_reflect_absence(dispatch)
    directives = out.directives
    kinds = {d.kind for d in directives}
    assert "must_not_narrate" in kinds
    assert "must_narrate" in kinds
    # Must-not payload references invention.
    must_nots = [d for d in directives if d.kind == "must_not_narrate"]
    assert any("invent" in d.payload.lower() or "follower" in d.payload.lower() for d in must_nots)
    # Must-narrate payload references emptiness.
    musts = [d for d in directives if d.kind == "must_narrate"]
    assert any("empty" in d.payload.lower() or "absence" in d.payload.lower() for d in musts)


@pytest.mark.asyncio
async def test_reflect_absence_propagates_visibility_tag():
    """Directives inherit the dispatch's visibility tag by default."""
    tag = VisibilityTag(
        visible_to=["player:Alice"],
        perception_fidelity={"player:Alice": "full"},
        secrets_for=[],
        redact_from_narrator_canonical=False,
    )
    dispatch = SubsystemDispatch(
        subsystem="reflect_absence",
        params={},
        depends_on=[],
        idempotency_key="idem:x",
        visibility=tag,
    )
    out = await run_reflect_absence(dispatch)
    directives = out.directives
    assert all(d.visibility == tag for d in directives)
