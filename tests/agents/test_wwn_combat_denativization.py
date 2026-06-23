"""WWN combat de-nativization (sq-playtest 2026-06-22, ADR-143/113/074).

A seated WN ``hp_depletion`` combat used to brick the turn: the narrator was
handed the full combat WRITE toolset AND told to drive beats, so it ground
roll/apply/advance in its own claude-agent-sdk loop past ``max_turns=8`` and the
turn died (``Reached maximum number of turns (8)``) before any beat resolved.

The fix mirrors the Fate precedent (``narrator.py`` contest/conflict branches):
under a WN binding the ruleset OWNS the round — combat resolves on the player's
DICE_THROW via ``run_wn_round`` (epic 108), so the narrator must NARRATE the
seated beat, not resolve it. Three coordinated gates, all keyed on the single
predicate ``is_live_wn_combat``:

* A1 — the narrator tool filter withholds combat-RESOLUTION tools.
* A2 — the narrator prompt drops the native beat menu (de-nativized live zone).
* A3 — narration-apply drops a stray narrator beat_selection before the legacy
  dial arm (so a hallucinated beat can't double-apply HP outside the WN round).

These tests pin all three plus the native-pack (``dial``) regression guard.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from sidequest.agents.narrator import NarratorAgent
from sidequest.agents.orchestrator import BeatSelection, NarrationTurnResult, NpcMention
from sidequest.agents.prompt_framework.core import PromptRegistry
from sidequest.agents.tool_registry import default_registry
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
    is_live_wn_combat,
)
from sidequest.game.persistence import GameMode
from sidequest.game.repository import SaveRepository
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import (
    BeatDef,
    ConfrontationDef,
    MetricDef,
    RulesConfig,
    WwnConfig,
)
from sidequest.protocol.dice import RollOutcome
from sidequest.server import narration_apply
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from sidequest.server.session_room import SessionRoom

# The five combat-RESOLUTION tools withheld from the narrator on a live WN combat.
_COMBAT_RESOLUTION_TOOLS = frozenset(
    {"roll_dice", "apply_damage", "advance_encounter_beat", "advance_confrontation", "apply_status"}
)


def _wn_combat_encounter(*, win_condition: str = "hp_depletion", resolved: bool = False):
    return StructuredEncounter(
        encounter_type="combat",
        category="combat",
        win_condition=win_condition,  # type: ignore[arg-type]
        resolved=resolved,
        player_metric=EncounterMetric(name="hp", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="hp", current=0, starting=0, threshold=10),
        actors=[
            EncounterActor(name="Groucho", role="combatant", side="player"),
            EncounterActor(name="Pale Thing", role="combatant", side="opponent"),
        ],
    )


def _combat_cdef() -> ConfrontationDef:
    # A native-shaped combat def (default win_condition); the de-nativization
    # branch is gated on the suppress_native_combat PARAM, not on cdef fields, so
    # any valid cdef exercises it. ``strike`` is the stray beat A3 drops.
    return ConfrontationDef(
        type="combat",
        label="Dungeon Combat",
        category="combat",
        player_metric=MetricDef(name="momentum", threshold=10),
        opponent_metric=MetricDef(name="momentum", threshold=10),
        beats=[
            BeatDef(id="strike", label="Strike", kind="strike", base=2, stat_check="STR"),
            BeatDef(id="brace", label="Brace", kind="brace", base=1, stat_check="CON"),
        ],
    )


# ---------------------------------------------------------------------------
# Predicate — is_live_wn_combat (the single source of truth for all 3 gates)
# ---------------------------------------------------------------------------


def test_predicate_true_for_live_wn_hp_depletion_combat() -> None:
    assert is_live_wn_combat(_wn_combat_encounter(), "wwn") is True
    # Whole WN family qualifies.
    for slug in ("swn", "wwn", "cwn", "awn"):
        assert is_live_wn_combat(_wn_combat_encounter(), slug) is True


def test_predicate_false_when_resolved_absent_or_not_wn_or_not_hp() -> None:
    # Resolved combat is over — narrate the resolution, don't suppress tools.
    assert is_live_wn_combat(_wn_combat_encounter(resolved=True), "wwn") is False
    # No encounter at all.
    assert is_live_wn_combat(None, "wwn") is False
    # Non-WN binding (native dial / Fate) keeps its own path (ADR-143 cuts both ways).
    assert is_live_wn_combat(_wn_combat_encounter(), "dial") is False
    assert is_live_wn_combat(_wn_combat_encounter(), "fate") is False
    assert is_live_wn_combat(_wn_combat_encounter(), None) is False
    # A WN encounter that is NOT hp_depletion combat (e.g. a dial chase) is untouched.
    assert is_live_wn_combat(_wn_combat_encounter(win_condition="dial_threshold"), "wwn") is False


# ---------------------------------------------------------------------------
# A1 — registry withholds combat-resolution tools, keeps read tools
# ---------------------------------------------------------------------------


def test_exclude_combat_resolution_withholds_exactly_the_resolution_tools() -> None:
    full = {t.name for t in default_registry.tool_definitions("wwn")}
    filtered = {
        t.name for t in default_registry.tool_definitions("wwn", exclude_combat_resolution=True)
    }
    assert full - filtered == set(_COMBAT_RESOLUTION_TOOLS), (
        "exactly the combat-resolution tools must be withheld on a live WN combat"
    )
    # Read/lookup tools the narrator still needs to NARRATE must survive.
    assert {"query_encounter", "lookup_monster"} <= filtered


def test_exclude_combat_resolution_defaults_off_unchanged() -> None:
    # Default False = no behavior change for every existing call site / native pack.
    assert {t.name for t in default_registry.tool_definitions("wwn")} == {
        t.name for t in default_registry.tool_definitions("wwn", exclude_combat_resolution=False)
    }


# ---------------------------------------------------------------------------
# A2 — narrator prompt de-nativizes WN combat (no native beat menu)
# ---------------------------------------------------------------------------


def test_build_encounter_context_suppresses_native_wn_combat_beat_menu() -> None:
    narrator = NarratorAgent()
    reg = PromptRegistry()
    narrator.build_encounter_context(
        reg,
        encounter=_wn_combat_encounter(),
        cdef=_combat_cdef(),
        encounter_summary="The Pale Thing rises from the black water.",
        suppress_native_combat=True,
    )
    composed = reg.compose(narrator.name())
    # Resolved-exchange context + participants still reach the narrator.
    assert "The Pale Thing rises from the black water." in composed
    assert "Groucho" in composed and "Pale Thing" in composed
    # De-nativized: resolution is by the player's throw; no beat-driving.
    assert "Do NOT emit beat_selections" in composed
    assert "player's die throw" in composed
    # The native beat menu MUST NOT render (the instruction that drove the loop).
    assert "beat_selections.beat_id MUST be one of" not in composed


def test_build_encounter_context_keeps_native_beat_menu_when_not_suppressed() -> None:
    """Native-pack regression guard: with suppress_native_combat False (a dial
    pack, or any non-WN combat), the native beat menu still renders — the legacy
    beat-driven path is untouched (ADR-143 leaves native engines alone)."""
    narrator = NarratorAgent()
    reg = PromptRegistry()
    narrator.build_encounter_context(
        reg,
        encounter=_wn_combat_encounter(),
        cdef=_combat_cdef(),
        encounter_summary="stub",
        suppress_native_combat=False,
    )
    composed = reg.compose(narrator.name())
    assert "beat_selections.beat_id MUST be one of" in composed
    assert "strike" in composed and "brace" in composed


# ---------------------------------------------------------------------------
# A3 — narration-apply drops a stray narrator beat before the legacy dial arm
# ---------------------------------------------------------------------------


_WN_ATTRIBUTE_MAP = {
    a: a for a in ("STRENGTH", "DEXTERITY", "CONSTITUTION", "INTELLIGENCE", "WISDOM", "CHARISMA")
}


def _wn_pack() -> GenrePack:
    pack = MagicMock(spec=GenrePack)
    pack.rules = RulesConfig(
        ruleset="wwn",
        ability_score_names=list(_WN_ATTRIBUTE_MAP.values()),
        wwn=WwnConfig(attribute_map=_WN_ATTRIBUTE_MAP),
        confrontations=[_combat_cdef()],
    )
    pack.effective_cultures.return_value = ([], "genre")
    pack.source_dir = None
    pack.worlds = {}
    return pack


def _make_room() -> tuple[SessionRoom, GameSnapshot]:
    room = SessionRoom(slug="beneath_sunden", mode=GameMode.SOLO)
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        turn_manager=TurnManager(interaction=1),
    )
    room.bind_world(snapshot=snap, store=MagicMock(spec=SaveRepository))
    return room, snap


def _opponent_strike_result() -> NarrationTurnResult:
    # A stray OPPONENT beat (bypasses the PC-consent gate so from_explicit_action
    # stays at its production default of False) the narrator could hallucinate.
    return NarrationTurnResult(
        narration="The Pale Thing lunges at Groucho.",
        beat_selections=[
            BeatSelection(actor="Pale Thing", beat_id="strike", outcome=RollOutcome.Success)
        ],
        npcs_present=[NpcMention(name="Pale Thing", side="opponent", role="hostile")],
    )


def _capture_watcher(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    events: list[dict] = []

    def _spy(event_type, fields, *, component=None, severity="info"):
        events.append({"event_type": event_type, "fields": fields, "component": component})

    monkeypatch.setattr(narration_apply, "_watcher_publish", _spy)
    return events


def test_live_wn_combat_drops_stray_beat_and_does_not_touch_the_dial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live WN ``hp_depletion`` combat drops a stray narrator beat BEFORE the
    legacy dial arm — the opponent dial stays 0, no ``beat_applied`` fires, and a
    ``wn_combat_beat_dropped_engine_owns_round`` event surfaces on the GM panel.
    Resolution belongs to the player's DICE_THROW (run_wn_round), not the narrator."""
    events = _capture_watcher(monkeypatch)
    room, snap = _make_room()
    snap.encounter = _wn_combat_encounter()

    _apply_narration_result_to_snapshot(
        snap, _opponent_strike_result(), "Groucho", room=room, pack=_wn_pack()
    )

    assert snap.encounter is not None
    assert snap.encounter.opponent_metric.current == 0, (
        "a live WN combat resolves via run_wn_round on the player's throw, NOT the "
        f"native dial — a stray beat advanced the dial to {snap.encounter.opponent_metric.current}"
    )
    assert not [e for e in events if e["fields"].get("op") == "beat_applied"], (
        "no beat_applied may fire — the stray beat was dropped, not applied"
    )
    dropped = [
        e for e in events if e["fields"].get("op") == "wn_combat_beat_dropped_engine_owns_round"
    ]
    assert len(dropped) == 1, (
        "the narration pipeline must DROP the stray beat for a live WN combat and "
        f"surface it on the GM panel; got ops={[e['fields'].get('op') for e in events]}"
    )
    assert dropped[0]["fields"].get("beat_id") == "strike"
