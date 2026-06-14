from __future__ import annotations

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.session import GameSnapshot
from sidequest.server.dispatch.encounter_lifecycle import award_turn_xp


def _make_char(xp: int = 0) -> Character:
    core = CreatureCore(
        name="Rux",
        description="A stoic fighter",
        personality="stoic",
        inventory=Inventory(),
        xp=xp,
    )
    return Character(
        core=core,
        char_class="Fighter",
        race="Human",
        backstory="A wandering fighter",
    )


@pytest.fixture
def snap_with_char():
    snap = GameSnapshot(genre_slug="caverns_and_claudes")
    snap.characters.append(_make_char())
    return snap


def test_award_out_of_combat_grants_10_xp(snap_with_char):
    award_turn_xp(snap_with_char, in_combat=False)
    assert snap_with_char.characters[0].core.xp == 10


def test_award_in_combat_grants_25_xp(snap_with_char):
    award_turn_xp(snap_with_char, in_combat=True)
    assert snap_with_char.characters[0].core.xp == 25


def test_award_accumulates(snap_with_char):
    """Per-turn award must add to existing XP, not replace."""
    snap_with_char.characters[0].core.xp = 100
    award_turn_xp(snap_with_char, in_combat=True)
    assert snap_with_char.characters[0].core.xp == 125


def test_award_no_character_is_noop():
    snap = GameSnapshot(genre_slug="caverns_and_claudes")
    assert snap.characters == []
    award_turn_xp(snap, in_combat=True)  # must not raise
    assert snap.characters == []


# --- MP sealed-round attribution (playtest 2026-05-17 coyote_star-mp) ---
# SideQuest MP is sealed-rounds (ADR-036): every seated PC acts each
# round, so the per-turn XP tick is PARTY-WIDE. The original Rust port
# (state_mutations.rs:39) hardcoded characters[0] ("party lead"), which
# silently starved every non-host seat — Ritali 1180 XP / Catalina 0
# across 117 rounds. ADR-037 makes the character sheet per-player.


def _named(name: str, xp: int = 0) -> Character:
    core = CreatureCore(
        name=name,
        description="A spacer",
        personality="quick-witted",
        inventory=Inventory(),
        xp=xp,
    )
    return Character(core=core, char_class="Pilot", race="Human", backstory="-")


@pytest.fixture
def mp_snap_two_seats():
    snap = GameSnapshot(genre_slug="space_opera")
    snap.characters.append(_named("Ritali Veer"))
    snap.characters.append(_named("Catalina Valentine"))
    # Seat manifest as production builds it: player_id -> pc name.
    snap.player_seats = {
        "Ritali Veer": "Ritali Veer",
        "Catalina Valentine": "Catalina Valentine",
    }
    return snap


def test_mp_out_of_combat_grants_10_to_every_seated_pc(mp_snap_two_seats):
    award_turn_xp(mp_snap_two_seats, in_combat=False)
    xps = {c.core.name: c.core.xp for c in mp_snap_two_seats.characters}
    assert xps == {"Ritali Veer": 10, "Catalina Valentine": 10}, xps


def test_mp_in_combat_grants_25_to_every_seated_pc(mp_snap_two_seats):
    award_turn_xp(mp_snap_two_seats, in_combat=True)
    xps = {c.core.name: c.core.xp for c in mp_snap_two_seats.characters}
    assert xps == {"Ritali Veer": 25, "Catalina Valentine": 25}, xps


def test_mp_award_accumulates_independently_per_seat(mp_snap_two_seats):
    """The coyote_star regression: a non-host seat must not be starved.
    Each seat accumulates its own XP from its own prior value."""
    by_name = {c.core.name: c for c in mp_snap_two_seats.characters}
    by_name["Ritali Veer"].core.xp = 1180
    by_name["Catalina Valentine"].core.xp = 0
    award_turn_xp(mp_snap_two_seats, in_combat=False)
    assert by_name["Ritali Veer"].core.xp == 1190
    assert by_name["Catalina Valentine"].core.xp == 10


def test_single_player_unchanged_when_no_seat_manifest(snap_with_char):
    """Regression guard: pre-chargen / solo snapshots have no
    player_seats — the lone PC must still get the tick (legacy
    behavior preserved, mirrors the character_locations precedent)."""
    assert snap_with_char.player_seats == {}
    award_turn_xp(snap_with_char, in_combat=True)
    assert snap_with_char.characters[0].core.xp == 25


def test_award_turn_xp_is_wired_into_the_real_narration_turn():
    """Wiring (CLAUDE.md: every suite needs one): award_turn_xp must be
    invoked from the production sealed-round resolution path, not just
    unit-callable."""
    import inspect

    from sidequest.server.websocket_session_handler import WebSocketSessionHandler

    src = inspect.getsource(WebSocketSessionHandler._execute_narration_turn)
    assert "award_turn_xp(" in src


# --- WN-family gate (sq-playtest 2026-06-13 beneath_sunden) ---
# A WWN-bound L1 Warrior ticked to 135 XP because the native ADR-021 per-turn
# tick fired regardless of ruleset. WN uses small-integer GM-awarded expedition
# XP, not an OSR-scale per-turn counter, so award_turn_xp must suppress under
# any Without Number binding (and emit a loud suppression event, not a silent
# no-op). See gm-decisions.md 2026-06-13 (WWN SRD is the authority).


def test_award_turn_xp_suppressed_under_wwn(snap_with_char, monkeypatch):
    from sidequest.game.ruleset.registry import get_ruleset_module
    from sidequest.server.dispatch import encounter_lifecycle

    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        encounter_lifecycle,
        "_watcher_publish",
        lambda event, payload, **kw: captured.append((event, payload)),
    )

    award_turn_xp(snap_with_char, in_combat=True, ruleset=get_ruleset_module("wwn"))

    assert snap_with_char.characters[0].core.xp == 0, (
        "WWN binding must NOT accrue the native per-turn XP tick; "
        f"got {snap_with_char.characters[0].core.xp}"
    )
    suppressed = [p for _, p in captured if p.get("op") == "award_turn_xp_suppressed"]
    assert len(suppressed) == 1, (
        f"a single suppression event must fire (lie-detector); got {captured}"
    )
    assert suppressed[0].get("ruleset") == "wwn"
    assert suppressed[0].get("field") == "xp"


def test_award_turn_xp_still_applies_under_native(snap_with_char):
    """The native dial engine keeps the per-turn tick — the gate is WN-only."""
    from sidequest.game.ruleset.registry import get_ruleset_module

    award_turn_xp(snap_with_char, in_combat=True, ruleset=get_ruleset_module("native"))
    assert snap_with_char.characters[0].core.xp == 25


def test_award_turn_xp_none_ruleset_preserves_native_tick(snap_with_char):
    """No module supplied (legacy/test callers) → native behavior, unchanged."""
    award_turn_xp(snap_with_char, in_combat=False, ruleset=None)
    assert snap_with_char.characters[0].core.xp == 10


def test_without_number_family_suppresses_native_xp_capability():
    """The capability flag: every WN sibling reports awards_native_turn_xp=False;
    the native module reports True. This is the single source the gate reads."""
    from sidequest.game.ruleset.registry import get_ruleset_module

    for slug in ("wwn", "cwn", "swn", "awn"):
        assert get_ruleset_module(slug).awards_native_turn_xp is False, (
            f"{slug} must not use the native per-turn XP tick"
        )
    assert get_ruleset_module("native").awards_native_turn_xp is True


def test_award_turn_xp_suppressed_for_every_seated_pc_under_wwn(mp_snap_two_seats):
    """The suppression is party-wide: no seat accrues native XP under WWN."""
    from sidequest.game.ruleset.registry import get_ruleset_module

    award_turn_xp(mp_snap_two_seats, in_combat=True, ruleset=get_ruleset_module("wwn"))
    xps = {c.core.name: c.core.xp for c in mp_snap_two_seats.characters}
    assert xps == {"Ritali Veer": 0, "Catalina Valentine": 0}, xps
