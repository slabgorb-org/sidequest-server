"""Wiring test: LOCATION_OVERLAY_CHANGED fires on encounter activate + deactivate.

Story 54-7 / ADR-109. CLAUDE.md "Every test suite needs a wiring test" —
proves _maybe_emit_location_overlay_changed has a non-test caller in the
session handler.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from sidequest.game.encounter import (
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.protocol.enums import MessageType
from sidequest.protocol.messages import LocationOverlayChangedMessage
from sidequest.protocol.models import (
    EncounterLocationOverlay,
    LocationEntity,
)


def _enc(*, resolved: bool = False) -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="tavern_brawl",
        player_metric=EncounterMetric(name="composure", current=10, starting=10, threshold=20),
        opponent_metric=EncounterMetric(name="brawl_energy", current=10, starting=10, threshold=20),
        resolved=resolved,
        location_overlay=EncounterLocationOverlay(
            bound_room_id="glenross_pub",
            entity_delta=[
                LocationEntity(
                    id="overturned_table",
                    label="an overturned table",
                    tier="yes_and",
                ),
            ],
            prose_suffix="A chair lies in splinters by the door.",
        ),
    )


def test_activate_emits_with_overlay_in_payload():
    from sidequest.server.websocket_session_handler import (
        _maybe_emit_location_overlay_changed,
    )

    emit_fn = MagicMock()
    sd = MagicMock()
    sd.genre_slug = "tea_and_murder"
    sd.world_slug = "glenross"
    sd.player_id = ""
    snapshot = MagicMock()
    snapshot.encounter = _enc(resolved=False)

    _maybe_emit_location_overlay_changed(
        MagicMock(),
        sd=sd,
        snapshot=snapshot,
        transition="activate",
        emit_fn=emit_fn,
    )

    emit_fn.assert_called_once()
    call_args = emit_fn.call_args
    sent_msg = call_args.args[0]
    sent_type = call_args.args[1]
    assert sent_type == "LOCATION_OVERLAY_CHANGED"
    assert isinstance(sent_msg, LocationOverlayChangedMessage)
    assert sent_msg.type == MessageType.LOCATION_OVERLAY_CHANGED
    assert sent_msg.payload.region_id == "glenross_pub"
    assert len(sent_msg.payload.overlays) == 1


def test_deactivate_emits_with_empty_overlay_list():
    """When the encounter has just resolved, payload.overlays is empty —
    the UI replaces its overlay slice rather than reconciling diffs."""
    from sidequest.server.websocket_session_handler import (
        _maybe_emit_location_overlay_changed,
    )

    emit_fn = MagicMock()
    sd = MagicMock()
    sd.genre_slug = "tea_and_murder"
    sd.world_slug = "glenross"
    sd.player_id = ""
    snapshot = MagicMock()
    # Deactivate path: pass the prior overlay explicitly so the emitter
    # knows which region_id was affected.
    prior_overlay = EncounterLocationOverlay(
        bound_room_id="glenross_pub",
        entity_delta=[],
        prose_suffix="A chair lies in splinters by the door.",
    )

    _maybe_emit_location_overlay_changed(
        MagicMock(),
        sd=sd,
        snapshot=snapshot,
        transition="deactivate",
        emit_fn=emit_fn,
        prior_overlay=prior_overlay,
    )

    emit_fn.assert_called_once()
    sent_msg = emit_fn.call_args.args[0]
    sent_type = emit_fn.call_args.args[1]
    assert sent_type == "LOCATION_OVERLAY_CHANGED"
    assert sent_msg.payload.region_id == "glenross_pub"
    assert sent_msg.payload.overlays == []


def test_activate_skips_when_encounter_has_no_overlay():
    """Encounters without location_overlay never emit."""
    from sidequest.server.websocket_session_handler import (
        _maybe_emit_location_overlay_changed,
    )

    emit_fn = MagicMock()
    sd = MagicMock()
    sd.genre_slug = "tea_and_murder"
    sd.world_slug = "glenross"
    sd.player_id = ""
    snapshot = MagicMock()
    snapshot.encounter = StructuredEncounter(
        encounter_type="tavern_brawl",
        player_metric=EncounterMetric(name="composure", current=10, starting=10, threshold=20),
        opponent_metric=EncounterMetric(name="brawl_energy", current=10, starting=10, threshold=20),
    )

    _maybe_emit_location_overlay_changed(
        MagicMock(),
        sd=sd,
        snapshot=snapshot,
        transition="activate",
        emit_fn=emit_fn,
    )
    emit_fn.assert_not_called()


def test_activate_skips_when_encounter_is_resolved():
    """A resolved encounter does not fire an activate emit even if it
    carries an overlay — activation is the live edge, not the post-resolve
    state."""
    from sidequest.server.websocket_session_handler import (
        _maybe_emit_location_overlay_changed,
    )

    emit_fn = MagicMock()
    sd = MagicMock()
    sd.genre_slug = "tea_and_murder"
    sd.world_slug = "glenross"
    sd.player_id = ""
    snapshot = MagicMock()
    snapshot.encounter = _enc(resolved=True)

    _maybe_emit_location_overlay_changed(
        MagicMock(),
        sd=sd,
        snapshot=snapshot,
        transition="activate",
        emit_fn=emit_fn,
    )
    emit_fn.assert_not_called()


def test_deactivate_skips_when_no_prior_overlay():
    from sidequest.server.websocket_session_handler import (
        _maybe_emit_location_overlay_changed,
    )

    emit_fn = MagicMock()
    sd = MagicMock()
    sd.genre_slug = "tea_and_murder"
    sd.world_slug = "glenross"
    sd.player_id = ""
    snapshot = MagicMock()

    _maybe_emit_location_overlay_changed(
        MagicMock(),
        sd=sd,
        snapshot=snapshot,
        transition="deactivate",
        emit_fn=emit_fn,
        prior_overlay=None,
    )
    emit_fn.assert_not_called()


def test_overlay_emit_called_from_encounter_transition_dispatch():
    """Static wiring proof — production code paths invoke the emit helper.

    CLAUDE.md "Verify wiring, not just existence." We grep the handler
    source for the call sites instead of running the whole narration
    pipeline; the two transition edges are the activate path right after
    encounter instantiation and the deactivate path inside the
    encounter_resolved_this_turn branch.
    """
    from pathlib import Path

    import sidequest.server.websocket_session_handler as wsh

    handler_src = Path(wsh.__file__).read_text()
    assert "def _maybe_emit_location_overlay_changed(" in handler_src
    call_count = handler_src.count("_maybe_emit_location_overlay_changed(")
    # 1 def + 2 call sites (activate, deactivate) = 3 minimum.
    assert call_count >= 3, (
        f"expected definition + activate + deactivate call sites, found {call_count} mentions"
    )
