"""Protocol round-trip tests for TokenPayload + HpPayload (158-18).

Wire shape mirrors the server contract: token_id/label/position/faction/hp/ac.
The cross-boundary contract: the UI parser (tacticalGridFromWire) consumes this
exact shape — changes here must be mirrored in the UI side test.
"""

from __future__ import annotations

from sidequest.protocol.models import HpPayload, TokenPayload


def test_enriched_token_round_trip() -> None:
    """Fully-enriched PC token serializes to the expected wire shape."""
    tok = TokenPayload(
        token_id="pc:Rux",
        label="Rux",
        position=(1, 2),
        faction="player",
        hp=HpPayload(current=18, max=22),
        ac=15,
    )
    dumped = tok.model_dump()
    assert dumped["token_id"] == "pc:Rux"
    assert dumped["label"] == "Rux"
    assert dumped["position"] == [1, 2], "position must serialize to [x,y] array"
    assert dumped["faction"] == "player"
    assert dumped["hp"] == {"current": 18, "max": 22}
    assert dumped["ac"] == 15


def test_minimal_token_serializes_with_faction_default() -> None:
    """Un-enriched token (no hp/ac) still serializes; hp/ac omitted when None."""
    tok = TokenPayload(token_id="x", label="x", position=(0, 0))
    dumped = tok.model_dump()
    assert dumped["token_id"] == "x"
    assert dumped["position"] == [0, 0]
    # faction default "neutral" is a non-empty string — ProtocolBase retains it.
    assert dumped["faction"] == "neutral"
    # hp and ac are None with None defaults → ProtocolBase omits them.
    assert "hp" not in dumped, "hp must be omitted when None"
    assert "ac" not in dumped, "ac must be omitted when None"


def test_hp_payload_round_trip() -> None:
    """HpPayload serializes to {current, max}."""
    hp = HpPayload(current=8, max=12)
    dumped = hp.model_dump()
    assert dumped == {"current": 8, "max": 12}


def test_hostile_creature_token() -> None:
    """Hostile creature token carries correct faction and hp/ac."""
    tok = TokenPayload(
        token_id="creature:rope-spider",
        label="rope-spider",
        position=(3, 1),
        faction="hostile",
        hp=HpPayload(current=8, max=12),
        ac=13,
    )
    dumped = tok.model_dump()
    assert dumped["faction"] == "hostile"
    assert dumped["hp"] == {"current": 8, "max": 12}
    assert dumped["ac"] == 13
