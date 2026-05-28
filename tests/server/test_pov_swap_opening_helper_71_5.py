"""Story 71-5 — unit tests for the opening-driver POV-swap helper.

Architect-approved seam (Option A): a thin orchestration helper

    _pov_swap_opening_for_driver(
        messages, *, driver_player_id, view, snapshot
    ) -> list[object]

loops the opening messages, calls the EXISTING ``_apply_pov_swap`` per message
(recipient = the driver), emits the ``opening.narration_pov_swapped`` watcher
event with the aggregate ``swap_count`` + attrs when a swap fires, strips the
consumed ``_visibility`` sidecar on the driver's rendered card, and returns the
(possibly swapped) message list. Peer broadcast is untouched (separate path).

These are pure-function tests — no DB, no chargen — building a snapshot + view
directly. The helper does not exist yet (the RED phase); importing it fails
until Dev extracts it.

Final ruling pinned here:
- PROSE card anchored to the driver → swapped to 2nd person ("You ...").
- Generic cold-open SEED (no _visibility / no PC reference) → natural NO-OP,
  text unchanged (NO synthetic stamping).
- Driver's swapped card has its _visibility sidecar STRIPPED on egress.
- opening.narration_pov_swapped fires ONLY when swap_count > 0.
"""

from __future__ import annotations

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.projection.view import SessionGameStateView
from sidequest.game.session import GameSnapshot
from sidequest.protocol.messages import NarrationMessage, NarrationPayload
from sidequest.protocol.types import NonBlankString
from sidequest.server.websocket_handlers import chargen_mixin

DRIVER_PID = "p_driver"


def _helper():  # noqa: ANN202
    """Deferred import — the helper does not exist yet (the RED signal). Each
    test fails here with ImportError until Dev extracts it, which keeps the
    failures per-test rather than a whole-file collection error."""
    from sidequest.server.websocket_handlers.chargen_mixin import (
        _pov_swap_opening_for_driver,
    )

    return _pov_swap_opening_for_driver


def _snapshot_with_driver(pc_name: str = "Rux", pronouns: str = "they/them") -> GameSnapshot:
    snap = GameSnapshot(genre_slug="caverns_and_claudes")
    snap.characters.append(
        Character(
            core=CreatureCore(
                name=pc_name, description="A stoic fighter", personality="stoic", inventory=Inventory()
            ),
            char_class="Fighter",
            race="Human",
            backstory="A wanderer.",
            pronouns=pronouns,
        )
    )
    return snap


def _view(driver_pc: str = "Rux") -> SessionGameStateView:
    return SessionGameStateView(
        gm_player_id=None, player_id_to_character={DRIVER_PID: driver_pc}
    )


def _narration(text: str, *, anchor_pc: str | None) -> NarrationMessage:
    sidecar = (
        {"visible_to": "all", "anchor_pc": anchor_pc, "pov_strategy": "pc_anchored"}
        if anchor_pc is not None
        else None
    )
    return NarrationMessage(
        payload=NarrationPayload(text=NonBlankString(text), visibility_sidecar=sidecar)
    )


def _text(msg: object) -> str:
    payload = getattr(msg, "payload", None)
    raw = getattr(payload, "text", None)
    root = getattr(raw, "root", None)
    return root if isinstance(root, str) else str(raw)


def _record_watcher(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []

    def _spy(event_type, fields, **kwargs):  # noqa: ANN001, ANN202
        events.append((event_type, dict(fields)))

    monkeypatch.setattr(chargen_mixin, "_watcher_publish", _spy)
    return events


def test_prose_card_swaps_and_generic_seed_is_noop() -> None:
    """AC1: the driver-anchored PROSE card swaps to 2nd person; a generic
    cold-open SEED with no anchor is left unchanged (natural no-op)."""
    seed = _narration("The vault's threshold yawns open; cold air rises.", anchor_pc=None)
    prose = _narration("Rux steps into the galley as the hatch seals behind Rux.", anchor_pc="Rux")

    out = _helper()(
        [seed, prose], driver_player_id=DRIVER_PID, view=_view(), snapshot=_snapshot_with_driver()
    )

    # Prose (driver is anchor) → 2nd person.
    assert "You step into the galley" in _text(out[1])
    assert "Rux steps into the galley" not in _text(out[1])
    # Generic seed → unchanged.
    assert _text(out[0]) == "The vault's threshold yawns open; cold air rises."


def test_driver_card_strips_visibility_sidecar_on_egress() -> None:
    """AC (consumed-not-leaked): the driver's swapped card carries no
    _visibility sidecar (it is consumed by the swap, not rendered)."""
    prose = _narration("Rux steps into the galley.", anchor_pc="Rux")
    out = _helper()(
        [prose], driver_player_id=DRIVER_PID, view=_view(), snapshot=_snapshot_with_driver()
    )
    assert out[0].payload.visibility_sidecar is None  # type: ignore[attr-defined]


def test_skip_when_anchor_is_not_driver() -> None:
    """AC2: a card anchored to a DIFFERENT PC is NOT swapped for the driver."""
    prose = _narration("Donut steps into the galley as the hatch seals behind Donut.", anchor_pc="Donut")
    out = _helper()(
        [prose], driver_player_id=DRIVER_PID, view=_view("Rux"), snapshot=_snapshot_with_driver()
    )
    assert "Donut steps into the galley" in _text(out[0])
    assert "You step into the galley" not in _text(out[0])


def test_watcher_event_emitted_with_attrs_when_swap_fires() -> None:
    """AC3: opening.narration_pov_swapped fires once with the documented attrs
    when swap_count > 0."""
    # monkeypatch via fixture-free context: build a local MonkeyPatch.
    mp = pytest.MonkeyPatch()
    try:
        events = _record_watcher(mp)
        prose = _narration("Rux steps into the galley.", anchor_pc="Rux")
        _helper()(
            [prose], driver_player_id=DRIVER_PID, view=_view(), snapshot=_snapshot_with_driver()
        )
    finally:
        mp.undo()

    swapped = [f for (name, f) in events if name == "opening.narration_pov_swapped"]
    assert len(swapped) == 1
    fields = swapped[0]
    for key in (
        "driver_player_id",
        "anchor_pc",
        "anchor_pronouns",
        "swap_count",
        "original_text_length",
        "swapped_text_length",
    ):
        assert key in fields, f"missing attr {key!r}: {fields!r}"
    assert fields["driver_player_id"] == DRIVER_PID
    assert fields["anchor_pc"] == "Rux"
    assert fields["swap_count"] > 0


def test_no_watcher_event_when_nothing_swaps() -> None:
    """AC3 (negative): no opening.narration_pov_swapped when swap_count == 0
    (anchor is not the driver)."""
    mp = pytest.MonkeyPatch()
    try:
        events = _record_watcher(mp)
        prose = _narration("Donut steps into the galley.", anchor_pc="Donut")
        _helper()(
            [prose], driver_player_id=DRIVER_PID, view=_view("Rux"), snapshot=_snapshot_with_driver()
        )
    finally:
        mp.undo()

    swapped = [f for (name, f) in events if name == "opening.narration_pov_swapped"]
    assert swapped == []
