"""Shared autouse guards for ``tests/e2e/``.

The autouse fixtures in ``tests/server/conftest.py`` are scoped to that
directory and do NOT apply to this sibling directory. Without them, any e2e
test that drives ``_execute_narration_turn`` reaches the live pre-narrator
pass, whose factory ``intent_router_pass.build_intent_router_for_session``
constructs an ``AnthropicSdkClient`` that eagerly validates
``ANTHROPIC_API_KEY`` — raising if absent, or (worse) spending real API
credits on a live Haiku ``decompose`` call if the key is present.

Per the project rule "tests MUST NOT spawn a real Claude client", this module
makes the guard STRUCTURAL (autouse, opt-out) rather than per-test opt-in.
Tests that need a non-empty router output (e.g. ``deterministic_combat_router``
in ``test_encounter_wiring_e2e.py``) install their own monkeypatch on the same
symbol, which shadows this default for that test and unwinds LIFO. Added per the
73-12 review (reviewer-security finding).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _stub_intent_router_factory(monkeypatch):
    """Replace the production Intent Router factory with an in-process stub
    whose ``decompose`` returns an empty ``DispatchPackage`` — no dispatches to
    engage, so the pre-narrator pass is a pass-through. Mirrors
    ``tests/server/conftest.py``'s guard of the same name.
    """
    from sidequest.protocol.dispatch import DispatchPackage

    async def _empty_decompose(*, action: str, state_summary: object) -> DispatchPackage:  # noqa: ARG001
        return DispatchPackage(turn_id="e2e-stub", confidence_global=0.0)

    stub_router = MagicMock()
    stub_router.decompose = _empty_decompose

    monkeypatch.setattr(
        "sidequest.server.intent_router_pass.build_intent_router_for_session",
        lambda **_kwargs: stub_router,
    )
