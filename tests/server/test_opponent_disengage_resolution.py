"""sq-playtest 2026-06-10 (heavy_metal/long_foundry) — zombie negotiation.

The narrator had the seated Other (a writ-server, "The Collector") disengage and
walk back into the smoke, but the Cold Negotiation stayed ``active=True`` with the
Other still seated: beats kept being offered, plain Enter stayed locked, and the
only escape was the explicit Yield button — a *failed* DC-10 Withdraw left the
player trapped against an opponent who had left the fiction.

ADR-116 §4 (end-on-no-Other) already resolves a confrontation once every seated
opponent is ``withdrawn`` (``_resolve_if_no_opponent_remains`` →
``opponent_yield_outcome`` → ``opponent_yielded``/player_victory). The combat
path reaches that state through HP-depletion / the B/X morale rout. The SOCIAL
path had NO signal that an opponent left: ``opponents_disposition`` is morale-only
and nothing flipped a social opponent's ``withdrawn`` flag.

This adds the missing GROUNDED signal (No Silent Fallbacks — never prose
inference): the narrator marks an ``npcs_present`` mention ``side="opponent"`` +
``disengaged=true`` when a seated opponent leaves the confrontation. The engine
flips the matching opponent actor ``withdrawn`` and emits
``confrontation.opponent_disengaged``; the existing end-on-no-Other sweep then
resolves the encounter the same turn. Following Story 59-31 doctrine (an opponent
breaking off = end-on-no-Other = ``opponent_yielded``, e.g. the Cowardly Lion
backing down), a disengaged Other resolves as a player victory, not a trap.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from sidequest.agents.orchestrator import NarrationTurnResult, NpcMention
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.game.session import GameSnapshot
from sidequest.server import narration_apply
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from tests._helpers.session_room import room_for

# ── fixtures / builders ─────────────────────────────────────────────────────


def _negotiation(
    *,
    opponents: list[EncounterActor],
    players: list[EncounterActor] | None = None,
) -> StructuredEncounter:
    """A Cold Negotiation whose dials sit BELOW threshold — no dial win is
    possible, so the only resolution path is end-on-no-Other (exactly the
    long_foundry zombie case: beats offered, plain Enter locked)."""
    return StructuredEncounter(
        encounter_type="negotiation",
        win_condition="dial_threshold",  # type: ignore[arg-type]
        player_metric=EncounterMetric(name="leverage", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="leverage", current=0, starting=0, threshold=10),
        actors=[
            *(players or [EncounterActor(name="Vesska", role="lead", side="player")]),
            *opponents,
        ],
    )


def _collector() -> EncounterActor:
    return EncounterActor(name="The Collector", role="aggressor", side="opponent")


def _mention(
    name: str = "The Collector",
    *,
    side: str = "opponent",
    disengaged: bool = True,
) -> NpcMention:
    return NpcMention(name=name, role="aggressor", side=side, disengaged=disengaged)


@pytest.fixture
def captured_watcher_events(monkeypatch) -> Iterator[list[dict[str, Any]]]:
    captured: list[dict[str, Any]] = []

    def _capture(event_type, fields, *, component="sidequest-server", severity="info"):
        captured.append(
            {
                "event_type": event_type,
                "fields": fields,
                "component": component,
                "severity": severity,
            }
        )

    monkeypatch.setattr(narration_apply, "_watcher_publish", _capture)
    yield captured


# ── AC1 — the structured disengage signal parses ─────────────────────────────


def test_npc_mention_parses_disengaged_field() -> None:
    """The narrator's ``npcs_present`` mention carries an optional
    ``disengaged`` bool (default False, fully backward-compatible)."""
    assert NpcMention.from_value({"name": "The Collector", "side": "opponent"}).disengaged is False
    assert (
        NpcMention.from_value(
            {"name": "The Collector", "side": "opponent", "disengaged": True}
        ).disengaged
        is True
    )


# ── AC2 — a disengaged opponent ends the zombie negotiation same-turn ─────────


def test_disengaged_opponent_resolves_zombie_negotiation() -> None:
    """THE BUG. An active negotiation whose seated Other is reported
    ``disengaged=True`` must resolve the same turn — not stay active with beats
    offered. Resolves as ``opponent_yielded`` (Story 59-31 doctrine)."""
    snap = GameSnapshot(genre_slug="heavy_metal", world_slug="long_foundry")
    snap.encounter = _negotiation(opponents=[_collector()])

    _apply_narration_result_to_snapshot(
        snap,
        NarrationTurnResult(
            narration="The Collector turns, unhurried, and walks back into the Kin Lane smoke.",
            beat_selections=[],
            npcs_present=[_mention()],
        ),
        player_name="Vesska",
        room=room_for(snap),
    )

    enc = snap.encounter
    assert enc is not None
    assert enc.resolved is True, "a disengaged Other must end the confrontation, not zombie it"
    assert enc.outcome == "opponent_yielded", f"got outcome={enc.outcome!r}"


def test_disengaged_opponent_marks_actor_withdrawn() -> None:
    """The grounded mechanism: the matching opponent actor is flipped
    ``withdrawn`` (the same state the end-on-no-Other sweep keys on)."""
    snap = GameSnapshot(genre_slug="heavy_metal", world_slug="long_foundry")
    snap.encounter = _negotiation(opponents=[_collector()])

    _apply_narration_result_to_snapshot(
        snap,
        NarrationTurnResult(
            narration="They do not look back.",
            beat_selections=[],
            npcs_present=[_mention()],
        ),
        player_name="Vesska",
        room=room_for(snap),
    )

    enc = snap.encounter
    assert enc is not None
    collector = next(a for a in enc.actors if a.name == "The Collector")
    assert collector.withdrawn is True


# ── AC3 — over-fire / No-Silent-Fallbacks guards ─────────────────────────────


def test_opponent_mention_without_disengaged_does_not_resolve() -> None:
    """No silent inference. An opponent simply re-cited in ``npcs_present`` with
    no ``disengaged`` flag is STILL IN THE FIGHT — the encounter must stay active.
    Absence of the flag must never be read as departure."""
    snap = GameSnapshot(genre_slug="heavy_metal", world_slug="long_foundry")
    snap.encounter = _negotiation(opponents=[_collector()])

    _apply_narration_result_to_snapshot(
        snap,
        NarrationTurnResult(
            narration="The Collector folds their arms and waits for your answer.",
            beat_selections=[],
            npcs_present=[_mention(disengaged=False)],
        ),
        player_name="Vesska",
        room=room_for(snap),
    )

    enc = snap.encounter
    assert enc is not None
    assert enc.resolved is False
    collector = next(a for a in enc.actors if a.name == "The Collector")
    assert collector.withdrawn is False


def test_disengaged_player_side_mention_does_not_withdraw_opponent() -> None:
    """Side guard (the ADR-116 asymmetry). A ``disengaged=True`` mention on the
    PLAYER side must NOT withdraw any opponent — a player disengaging is the
    player-side yield path, never an opponent departure."""
    snap = GameSnapshot(genre_slug="heavy_metal", world_slug="long_foundry")
    snap.encounter = _negotiation(opponents=[_collector()])

    _apply_narration_result_to_snapshot(
        snap,
        NarrationTurnResult(
            narration="Vesska steps back from the table.",
            beat_selections=[],
            npcs_present=[_mention("Vesska", side="player", disengaged=True)],
        ),
        player_name="Vesska",
        room=room_for(snap),
    )

    enc = snap.encounter
    assert enc is not None
    collector = next(a for a in enc.actors if a.name == "The Collector")
    assert collector.withdrawn is False
    assert enc.resolved is False


def test_one_of_two_opponents_disengaging_does_not_resolve() -> None:
    """A second Other still seated keeps the confrontation live (ADR-116 — an
    Other remains). Only the disengaged opponent is withdrawn."""
    snap = GameSnapshot(genre_slug="heavy_metal", world_slug="long_foundry")
    enforcer = EncounterActor(name="The Enforcer", role="muscle", side="opponent")
    snap.encounter = _negotiation(opponents=[_collector(), enforcer])

    _apply_narration_result_to_snapshot(
        snap,
        NarrationTurnResult(
            narration="The Collector leaves; the Enforcer stays, cracking their knuckles.",
            beat_selections=[],
            npcs_present=[_mention()],
        ),
        player_name="Vesska",
        room=room_for(snap),
    )

    enc = snap.encounter
    assert enc is not None
    assert enc.resolved is False
    collector = next(a for a in enc.actors if a.name == "The Collector")
    enforcer_actor = next(a for a in enc.actors if a.name == "The Enforcer")
    assert collector.withdrawn is True
    assert enforcer_actor.withdrawn is False


# ── AC4 — OTEL lie-detector: the disengage decision emits a span ──────────────


def test_disengaged_opponent_emits_confrontation_span(
    monkeypatch,
    otel_capture,
) -> None:
    """CLAUDE.md OTEL principle. Marking an opponent disengaged is a subsystem
    decision — it must emit ``confrontation.opponent_disengaged`` through the real
    telemetry pipeline so the GM panel sees the ENGINE (not improvisation) ended
    the scene, and WHY (the narrator signalled the Other left)."""
    monkeypatch.setenv("SIDEQUEST_WATCHER_AS_SPANS", "1")

    snap = GameSnapshot(genre_slug="heavy_metal", world_slug="long_foundry")
    snap.encounter = _negotiation(opponents=[_collector()])

    _apply_narration_result_to_snapshot(
        snap,
        NarrationTurnResult(
            narration="The Collector walks away into the smoke.",
            beat_selections=[],
            npcs_present=[_mention()],
        ),
        player_name="Vesska",
        room=room_for(snap),
    )

    spans = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "confrontation.opponent_disengaged"
    ]
    assert len(spans) == 1, (
        "exactly one confrontation.opponent_disengaged span must fire; got "
        f"{[s.name for s in otel_capture.get_finished_spans()]}"
    )
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("name") == "The Collector"
    assert attrs.get("encounter_type") == "negotiation"


def test_no_disengage_span_when_flag_absent(
    monkeypatch,
    otel_capture,
) -> None:
    """The lie-detector stays silent when no opponent disengaged — a plain
    re-cite fires zero disengage spans."""
    monkeypatch.setenv("SIDEQUEST_WATCHER_AS_SPANS", "1")

    snap = GameSnapshot(genre_slug="heavy_metal", world_slug="long_foundry")
    snap.encounter = _negotiation(opponents=[_collector()])

    _apply_narration_result_to_snapshot(
        snap,
        NarrationTurnResult(
            narration="The Collector waits.",
            beat_selections=[],
            npcs_present=[_mention(disengaged=False)],
        ),
        player_name="Vesska",
        room=room_for(snap),
    )

    spans = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "confrontation.opponent_disengaged"
    ]
    assert spans == []
