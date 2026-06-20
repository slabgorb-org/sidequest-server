"""Story 150-3 (sq-playtest 2026-06-20, spaghetti_western/five_points, turn 5):
a poker table SEATS but is instantly deactivated by the same-turn location
change → no playable table surface.

Repro: the player buys chips and takes a seat; the router/``instantiate_table_
encounter`` correctly seats a ``table_showdown`` encounter with a dealt 5-card
PC hand (ADR-129). But the narrator set the scene location to the poker table
("The Groggery — Poker Table") in the SAME response that seated the poker, and
``_apply_narration_result_to_snapshot``'s deactivate-on-location-change ladder
killed the freshly-instantiated encounter (``encounter.deactivated_on_location_
change encounter_type=poker``). The UI showed poker-table NARRATION but
``hasTableTab: []`` — no antes/seats/sealed-commit loop ever reached the player.

Fix: a table scene is INHERENTLY a new location, so seating + entering-the-table-
location coincide. ``StructuredEncounter.created_turn`` is stamped at the single
seating chokepoint (``instantiate_encounter_from_trigger``); the abandon ladder
EXEMPTS an encounter whose ``created_turn`` equals the current interaction — it
cannot have been "walked away from" the turn it was born. A genuine LATER
departure (``created_turn < interaction``) still abandons. Emits the OTEL
``confrontation_continued_fresh_this_turn`` watcher event (the GM-panel lie
detector for this engine decision).
"""

from __future__ import annotations

import pytest

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.server import narration_apply
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from tests._helpers.session_room import room_for

_CONTINUED_FRESH_EVENT = "confrontation_continued_fresh_this_turn"
_DEACTIVATED_EVENT = "confrontation_deactivated_on_location_change"


def _attach_table(snap, *, created_turn: int | None) -> StructuredEncounter:
    """Mount a live ``table_showdown`` poker encounter exactly as
    ``instantiate_table_encounter`` builds it: inert dual dials (threshold=1),
    category="social", two seats. ``created_turn`` stamps the birth turn.

    win_condition=table_showdown ⇒ ``dial_threshold_outcome`` returns None, and
    no actor is withdrawn ⇒ ``opponent_yield_outcome`` returns None, and
    category!="movement" ⇒ not mobile — so this falls through the won/yield/
    mobile/same-region branches exactly like the real poker encounter, reaching
    the created_turn exemption (or, for a stale encounter, the abandon else).
    """
    encounter = StructuredEncounter(
        encounter_type="poker",
        win_condition="table_showdown",
        category="social",
        player_metric=EncounterMetric(
            name="table_player_inert", current=0, starting=0, threshold=1
        ),
        opponent_metric=EncounterMetric(
            name="table_opponent_inert", current=0, starting=0, threshold=1
        ),
        actors=[
            EncounterActor(name="Linus", role="seat_1", side="player"),
            EncounterActor(name="The Thick-Necked Man", role="seat_2", side="opponent"),
        ],
        created_turn=created_turn,
    )
    snap.encounter = encounter
    return encounter


@pytest.fixture
def watcher_events(monkeypatch):
    """Capture every ``_watcher_publish`` call the apply step makes (the OTEL
    lie-detector emits), so a test can assert the engine decision fired.

    ``narration_apply`` binds ``publish_event`` at import as the module-level
    name ``_watcher_publish``; patching that name records emits synchronously.
    """
    captured: list[dict] = []

    def _recorder(event_type, fields, *, component="sidequest-server", **kwargs):
        captured.append({"event_type": event_type, "fields": fields, "component": component})

    monkeypatch.setattr(narration_apply, "_watcher_publish", _recorder)
    return captured


def _event_types(captured: list[dict]) -> list[str]:
    return [e["event_type"] for e in captured]


def test_fresh_table_continues_across_birth_location_change(
    snapshot_with_pack,
    character_named_sam,
    watcher_events,
):
    """The repro. A table encounter born THIS turn must survive the location
    change that moved the scene to the table itself — it stays active so the
    playable table surface (antes/seats/sealed-commit) reaches the UI.
    """
    snap, pack = snapshot_with_pack
    snap.character_locations["Linus"] = "Paradise Square"
    snap.characters.append(character_named_sam)
    born = snap.turn_manager.interaction
    encounter = _attach_table(snap, created_turn=born)
    assert encounter.resolved is False  # baseline: freshly dealt

    # Same response seats the table AND moves the scene onto the table.
    result = NarrationTurnResult(
        narration="Brennan buys in; three men already seated, cards close to the chest.",
        location="The Groggery — Poker Table",
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        player_name="Linus",
        room=room_for(snapshot=snap),
    )

    assert snap.encounter is encounter
    assert snap.encounter.resolved is False, (
        "a table encounter created THIS turn must CONTINUE across the location "
        "change that birthed it — the table scene IS the new location; "
        f"got resolved={snap.encounter.resolved}, outcome={snap.encounter.outcome!r}"
    )
    assert snap.encounter.outcome is None
    assert snap.encounter.table_state is None or snap.encounter.encounter_type == "poker"

    # OTEL lie-detector: the engine must record it CHOSE to keep the fresh
    # encounter alive, and must NOT have emitted the abandon span.
    events = _event_types(watcher_events)
    assert _CONTINUED_FRESH_EVENT in events, (
        f"expected {_CONTINUED_FRESH_EVENT!r} watcher event so the GM panel can "
        f"verify the fresh-this-turn exemption fired; got {events!r}"
    )
    assert _DEACTIVATED_EVENT not in events, (
        f"the abandon span must NOT fire for a fresh-this-turn encounter; got {events!r}"
    )


def test_stale_table_abandons_on_later_location_change(
    snapshot_with_pack,
    character_named_sam,
    watcher_events,
):
    """Regression guard: the exemption is scoped to the BIRTH turn only. A table
    seated on a PRIOR turn (``created_turn < interaction``) that the player then
    walks away from (location change) must still ABANDON — otherwise leaving the
    groggery would never end the game. The fix must not turn tables into
    un-abandonable encounters.
    """
    snap, pack = snapshot_with_pack
    snap.character_locations["Linus"] = "The Groggery — Poker Table"
    snap.characters.append(character_named_sam)
    born = snap.turn_manager.interaction
    encounter = _attach_table(snap, created_turn=born - 1)  # seated a turn ago
    assert encounter.resolved is False

    # The player stands up and leaves — a genuine departure, not the birth turn.
    result = NarrationTurnResult(
        narration="Brennan rakes his winnings and pushes back out into the square.",
        location="Paradise Square",
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        player_name="Linus",
        room=room_for(snapshot=snap),
    )

    assert snap.encounter is encounter
    assert snap.encounter.resolved is True, (
        "a table seated on a PRIOR turn that the player walks away from must "
        f"abandon; got resolved={snap.encounter.resolved}"
    )
    assert snap.encounter.outcome == "abandoned_on_location_change"

    events = _event_types(watcher_events)
    assert _CONTINUED_FRESH_EVENT not in events, (
        "the fresh-this-turn exemption must NOT fire for a stale "
        f"(created_turn < interaction) encounter; got {events!r}"
    )
    assert _DEACTIVATED_EVENT in events, (
        f"the abandon span must fire for a genuine later departure; got {events!r}"
    )
