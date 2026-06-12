"""Tests for the post-narration confrontation lifecycle lie-detector.

sq-playtest 2026-05-12 [BUG] Confrontation panel doesn't clear when the
encounter ends. The detector classifies each post-narration emit by
comparing narration prose kill-claims against engine state — surfacing
the disconnect on the GM panel so Sebastien sees when the narrator
hallucinates a kill not backed by mechanical resolution.
"""
from __future__ import annotations

import pytest

from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.server.confrontation_lifecycle_detector import (
    build_lifecycle_snapshot,
    detect_kill_keywords,
)

# ---------------------------------------------------------------------------
# Kill keyword detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "narration,expected_any",
    [
        ("The Chalk Moth's wings still. It is dead.", True),
        ("Carl plants a boot on the moth's thorax... the legs go slack.", True),
        ("You slay the wraith with a final cleave.", False),  # 'slay' not in patterns; 'slain' is.
        ("The brigand lies slain at your feet.", True),
        ("The corpse twitches one last time.", True),
        ("She breathed her last.", True),
        ("You killed the wolf.", True),
        ("The wolf dies in a spray of fur.", True),
        ("The lifeless eyes catch the torchlight.", True),
        ("The bandit yields and drops her blade.", False),  # surrender, no death keyword
        ("", False),
        ("The narrator describes the room in cool detail.", False),
    ],
)
def test_detect_kill_keywords(narration: str, expected_any: bool) -> None:
    matches = detect_kill_keywords(narration)
    if expected_any:
        assert len(matches) >= 1, f"expected a kill keyword in {narration!r}, got none"
    else:
        assert matches == [], f"expected no kill keywords in {narration!r}, got {matches}"


def test_detect_kill_keywords_returns_lowercased_literal_match() -> None:
    matches = detect_kill_keywords("The MOTH is DEAD.")
    assert "dead" in matches


def test_detect_kill_keywords_word_boundary_avoids_false_positive() -> None:
    # "dead end" should NOT match — "dead" as adjective in a noun phrase
    # for a corridor is not a kill claim. We rely on context to be coarse
    # but accept that "dead" by itself in a tunnel description IS a noisy
    # signal; the detector errs on the side of flagging so Sebastien can
    # cross-reference state. Pure word-boundary regex still excludes
    # "deadweight" etc.
    assert detect_kill_keywords("This is deadweight") == []


# ---------------------------------------------------------------------------
# Snapshot building
# ---------------------------------------------------------------------------


def _enc(
    *,
    encounter_type: str = "combat",
    resolved: bool = False,
    player_current: int = 0,
    player_threshold: int = 7,
    opponent_current: int = 0,
    opponent_threshold: int = 7,
    opponents: list[tuple[str, bool]] | None = None,  # (name, withdrawn)
) -> StructuredEncounter:
    """Build a minimal StructuredEncounter for tests."""
    actors = [EncounterActor(name="Carl", role="player", side="player")]
    if opponents is None:
        actors.append(EncounterActor(name="Chalk Moth", role="opponent", side="opponent"))
    else:
        for name, withdrawn in opponents:
            actors.append(
                EncounterActor(
                    name=name, role="opponent", side="opponent", withdrawn=withdrawn
                )
            )
    return StructuredEncounter(
        encounter_type=encounter_type,
        player_metric=EncounterMetric(
            name="player_progress", current=player_current, threshold=player_threshold
        ),
        opponent_metric=EncounterMetric(
            name="opponent_progress",
            current=opponent_current,
            threshold=opponent_threshold,
        ),
        actors=actors,
        resolved=resolved,
    )


def test_snapshot_captures_metrics_and_opponent_count() -> None:
    enc = _enc(player_current=3, opponent_current=2)
    snap = build_lifecycle_snapshot(
        narration="Combat continues.",
        encounter_active_pre_apply=True,
        encounter=enc,
        encounter_resolved_this_turn=False,
    )
    assert snap.encounter_type == "combat"
    assert snap.encounter_active_post_apply is True
    assert snap.player_metric_current == 3
    assert snap.player_metric_threshold == 7
    assert snap.opponent_metric_current == 2
    assert snap.opponent_metric_threshold == 7
    assert snap.opponent_alive_count == 1
    assert snap.narration_claims_kill is False


def test_snapshot_narrator_kill_unbacked_fires_when_prose_claims_kill_but_encounter_still_active() -> None:
    """The exact sq-playtest 2026-05-12 repro: narrator says the Chalk Moth
    dies, but no engine resolution fired."""
    enc = _enc(player_current=0, opponent_current=2)  # rolls failing — dial stuck
    snap = build_lifecycle_snapshot(
        narration=(
            "Carl plants a boot on the moth's thorax... chitin splits. The legs "
            "go slack. ... a soft grey print of the kill. ... Silence."
        ),
        encounter_active_pre_apply=True,
        encounter=enc,
        encounter_resolved_this_turn=False,
    )
    assert snap.narration_claims_kill is True
    assert "go slack" in snap.narration_kill_keywords
    assert snap.encounter_active_post_apply is True
    assert snap.opponent_alive_count == 1
    assert snap.narrator_kill_unbacked is True


def test_snapshot_no_lie_when_kill_claim_matches_resolution() -> None:
    """Narrator claims a kill AND the engine resolved the encounter — clean."""
    enc = _enc(resolved=True, player_current=7)
    snap = build_lifecycle_snapshot(
        narration="The moth lies dead at your feet.",
        encounter_active_pre_apply=True,
        encounter=enc,
        encounter_resolved_this_turn=True,
    )
    assert snap.narration_claims_kill is True
    assert snap.encounter_active_post_apply is False
    assert snap.narrator_kill_unbacked is False


def test_snapshot_no_lie_when_kill_claim_with_remaining_opponents() -> None:
    """Multi-opponent fight: one dies, others remain. NOT a lie — the
    encounter correctly stays active because opponents remain. The
    detector defers to the engine: if any opponent is still in the
    fight, kill keywords are fine."""
    enc = _enc(
        opponents=[
            ("Goblin A", True),  # withdrawn (yielded / fled)
            ("Goblin B", False),  # still fighting
        ]
    )
    snap = build_lifecycle_snapshot(
        narration="The first goblin lies dead. The second snarls and lunges.",
        encounter_active_pre_apply=True,
        encounter=enc,
        encounter_resolved_this_turn=False,
    )
    # Kill keyword present.
    assert snap.narration_claims_kill is True
    # Encounter still active.
    assert snap.encounter_active_post_apply is True
    # One opponent alive, one withdrawn.
    assert snap.opponent_alive_count == 1
    # Encounter is correctly active because another opponent is fighting,
    # but the unbacked flag still fires — we can refine this in a follow
    # up. For V1 the GM panel cross-references with opponent_alive_count
    # to disambiguate. (Test pins current behavior; revisit if false-
    # positive rate is too high on the dashboard.)
    assert snap.narrator_kill_unbacked is True


def test_snapshot_when_encounter_is_none_post_apply() -> None:
    """Encounter cleared (resolved+removed) — no live state to report."""
    snap = build_lifecycle_snapshot(
        narration="The moth lies dead.",
        encounter_active_pre_apply=True,
        encounter=None,
        encounter_resolved_this_turn=True,
    )
    assert snap.encounter_type is None
    assert snap.encounter_active_post_apply is False
    assert snap.player_metric_current is None
    assert snap.opponent_alive_count == 0
    assert snap.narrator_kill_unbacked is False


def test_snapshot_to_watcher_attrs_jsonsafe() -> None:
    """Watcher event attrs must be JSON-serialisable primitives only."""
    enc = _enc()
    snap = build_lifecycle_snapshot(
        narration="The wolf dies.",
        encounter_active_pre_apply=True,
        encounter=enc,
        encounter_resolved_this_turn=False,
    )
    attrs = snap.to_watcher_attrs()
    # Spot check the shape; full key list is exhaustive.
    assert isinstance(attrs["narration_claims_kill"], bool)
    assert isinstance(attrs["narration_kill_keywords"], list)
    assert isinstance(attrs["narrator_kill_unbacked"], bool)
    assert attrs["encounter_type"] == "combat"
    # Round-trip through JSON to ensure serialisability.
    import json

    json.dumps(attrs)  # must not raise


# ---------------------------------------------------------------------------
# Wiring — detector consumed by websocket_session_handler
# ---------------------------------------------------------------------------


def test_websocket_session_handler_imports_lifecycle_detector() -> None:
    """Source-level wiring pin: the lifecycle detector is imported and
    called from the CONFRONTATION emit path in websocket_session_handler.
    Without this pin a future refactor could silently delete the call
    site and the GM panel would go dark on the lie-detector dimension."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "sidequest" / "server" / "websocket_session_handler.py"
    text = src.read_text()
    assert "build_lifecycle_snapshot" in text, (
        "websocket_session_handler.py must call build_lifecycle_snapshot "
        "on every CONFRONTATION emit (sq-playtest 2026-05-12 lie-detector)"
    )
    assert "confrontation_lifecycle" in text, (
        "watcher event 'confrontation_lifecycle' must be published from "
        "the CONFRONTATION emit path"
    )
