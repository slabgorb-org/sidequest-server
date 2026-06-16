"""Story 118-10 (ADR-144 F3d-pre) — the two new FateActionPayload wire fields.

F3d (the per-aspect Invoke affordance on the Fate conflict surface, 118-6) and the
"chandelier for free" freeform rider both need the CLIENT to be able to *say* two
things the wire currently cannot carry:

  * ``invoke_mode``: which KIND of invoke — a flat ``+2`` ('bonus') or a reroll
    ('reroll'). The Fate ruleset's ``invoke_aspect`` has accepted ``mode`` since
    F1b, but ``dispatch_fate_action`` hardcodes ``mode='bonus'`` — so the reroll
    half of F3d is unreachable from the wire. This payload field is the missing
    input.
  * ``player_action``: the freeform text the player typed before clicking a Fate
    action tile ("I swing from the chandelier and fire") — a flavor rider that
    rides alongside the mechanical action into the narrator, mirroring
    ``DiceThrowPayload.player_action`` (the 108-5 precedent).

These are RED: neither field exists on ``FateActionPayload`` yet, so the
constructor rejects the kwargs / the defaults are absent.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.protocol.fate import FateActionPayload


def test_invoke_mode_defaults_to_bonus():
    """Backward-compat: an action that omits ``invoke_mode`` defaults to 'bonus'
    — the exact constant ``dispatch_fate_action`` hardcodes today, so existing
    clients/tests that never set the field keep the +2 behavior unchanged."""
    p = FateActionPayload(request_id="r1", action="attack", skill="Fight")
    assert p.invoke_mode == "bonus"


def test_invoke_mode_accepts_reroll():
    """The reroll half of F3d: the client can declare ``invoke_mode='reroll'``."""
    p = FateActionPayload(
        request_id="r1", action="attack", skill="Fight", invoke_mode="reroll"
    )
    assert p.invoke_mode == "reroll"


def test_invoke_mode_rejects_unknown_literal():
    """No silent fallback: an out-of-band mode is a client/config bug and must be
    rejected at the wire (Literal guard), not silently coerced to 'bonus'. Mirrors
    ``FateRulesetModule.invoke_aspect``'s own loud rejection of an unknown mode."""
    with pytest.raises(ValidationError):
        FateActionPayload(
            request_id="r1", action="attack", skill="Fight", invoke_mode="banana"
        )


def test_player_action_defaults_empty():
    """Backward-compat: no freeform rider → empty string, never None — mirrors the
    str='' default the story specifies (DiceThrowPayload uses str|None, but the
    Fate field is spec'd as ``str=''`` so the dispatch can ``.strip()`` it freely)."""
    p = FateActionPayload(request_id="r1", action="attack", skill="Fight")
    assert p.player_action == ""


def test_player_action_preserves_freeform_text():
    """The freeform rider rides verbatim on the wire — the narrator threading
    (dispatch side) is a separate concern; here we only pin that the field carries
    the player's typed text without mangling it."""
    rider = "I swing from the chandelier and fire"
    p = FateActionPayload(
        request_id="r1", action="attack", skill="Fight", player_action=rider
    )
    assert p.player_action == rider
