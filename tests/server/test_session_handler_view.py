"""Session handler projection view — zone + visibility wiring.

Verifies that ``views.build_game_state_view(handler)`` pulls zone
information off the live ``GameSnapshot`` so projection-filter
predicates (``visible_to``, ``in_same_zone``) see real data rather than
the conservative ``None`` / ``False`` defaults.
"""

from __future__ import annotations

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.session import Npc
from sidequest.game.status import Status, StatusSeverity
from sidequest.server import views


def _make_character(name: str, *, statuses: list[str] | None = None) -> Character:
    return Character(
        core=CreatureCore(
            name=name,
            description=f"{name}, test hero",
            personality="stoic",
            inventory=Inventory(),
            statuses=list(statuses or []),
        ),
        char_class="Fighter",
        race="Human",
        backstory=f"{name} wanders the test suite.",
    )


def _make_npc(name: str, *, location: str | None = None, statuses: list[str] | None = None) -> Npc:
    return Npc(
        core=CreatureCore(
            name=name,
            description=f"NPC {name}",
            personality="mysterious",
            inventory=Inventory(),
            statuses=list(statuses or []),
        ),
        location=location,
    )


def test_session_view_reflects_party_location(session_fixture) -> None:
    """All player characters share the snapshot's party-level location."""
    sd, handler = session_fixture
    sd.snapshot.characters.append(_make_character("Alice"))
    sd.snapshot.characters.append(_make_character("Bob"))
    # Wave 2B: per-character locations replace the party-level field.
    sd.snapshot.character_locations["Alice"] = "Main Hall"
    sd.snapshot.character_locations["Bob"] = "Main Hall"

    view = views.build_game_state_view(handler)

    assert view.zone_of("Alice") == "Main Hall"
    assert view.zone_of("Bob") == "Main Hall"
    # Same zone -> visible.
    assert view.visible_to("Alice", "Bob") is True


def test_session_view_reflects_npc_location(session_fixture) -> None:
    """NPCs carry their own location — the view should mirror it."""
    sd, handler = session_fixture
    sd.snapshot.characters.append(_make_character("Alice"))
    sd.snapshot.character_locations["Alice"] = "Main Hall"
    sd.snapshot.npcs.append(_make_npc("Barkeep", location="The Tavern"))

    view = views.build_game_state_view(handler)

    assert view.zone_of("Barkeep") == "The Tavern"
    # Alice is in "Main Hall", Barkeep in "The Tavern" -> different zones.
    assert view.visible_to("Alice", "Barkeep") is False


def test_session_view_hides_stealthed_character(session_fixture) -> None:
    """A character whose status marks them hidden is masked from visible_to."""
    sd, handler = session_fixture
    sd.snapshot.characters.append(_make_character("Alice"))
    sd.snapshot.characters.append(_make_character("Bob", statuses=["hidden"]))
    sd.snapshot.character_locations["Alice"] = "Main Hall"
    sd.snapshot.character_locations["Bob"] = "Main Hall"

    view = views.build_game_state_view(handler)

    # Same party zone, but Bob is hidden -> not visible.
    assert view.zone_of("Bob") == "Main Hall"
    assert view.visible_to("Alice", "Bob") is False


def test_session_view_when_snapshot_missing_returns_empty(session_fixture) -> None:
    """No session data -> conservative empty view, no exceptions."""
    _sd, handler = session_fixture
    handler._session_data = None

    view = views.build_game_state_view(handler)

    assert view.zone_of("Anyone") is None
    assert view.visible_to("A", "B") is False


def test_session_view_hides_stealth_npc(session_fixture) -> None:
    """NPC with a stealth-flavored status is also flagged hidden."""
    sd, handler = session_fixture
    sd.snapshot.characters.append(_make_character("Alice"))
    sd.snapshot.character_locations["Alice"] = "Main Hall"
    sd.snapshot.npcs.append(
        _make_npc("ShadowThief", location="Main Hall", statuses=["Invisible"]),
    )

    view = views.build_game_state_view(handler)

    assert view.zone_of("ShadowThief") == "Main Hall"
    # Co-located but hidden -> masked.
    assert view.visible_to("Alice", "ShadowThief") is False


# ---------------------------------------------------------------------------
# Finding 1: player_id_to_character must be populated so predicates can reach
# the zone/visibility data. Without this mapping, view.character_of(player_id)
# returns None, which short-circuits visible_to / in_same_zone to False before
# any zone data is consulted.
# ---------------------------------------------------------------------------


def test_session_view_maps_player_id_to_character(session_fixture) -> None:
    """The session's single player-character is reachable via character_of()."""
    sd, handler = session_fixture
    sd.snapshot.characters.append(_make_character("Alice"))

    view = views.build_game_state_view(handler)

    assert view.character_of(sd.player_id) == "Alice"


def test_session_view_player_mapping_empty_when_no_characters(session_fixture) -> None:
    """No characters yet (pre-chargen) -> mapping stays empty; no exception."""
    sd, handler = session_fixture
    assert sd.snapshot.characters == []

    view = views.build_game_state_view(handler)

    assert view.character_of(sd.player_id) is None


def test_session_view_player_mapping_unknown_player(session_fixture) -> None:
    """An unknown player_id returns None (conservative default)."""
    sd, handler = session_fixture
    sd.snapshot.characters.append(_make_character("Alice"))

    view = views.build_game_state_view(handler)

    assert view.character_of("someone-else") is None


# ---------------------------------------------------------------------------
# Finding 2: one-shot OTEL/log warn when party_zone is absent but characters
# exist. Silent zone absence collapses visible_to to False everywhere, which
# is the safe direction but invisible to the GM panel.
# ---------------------------------------------------------------------------


def test_session_view_warns_once_when_party_zone_absent(session_fixture, caplog) -> None:
    """Warn exactly once per session when characters exist but party zone is empty."""
    import logging

    sd, handler = session_fixture
    # Fresh session, pre-first-encounter — no per-character entries.
    sd.snapshot.character_locations.clear()
    sd.snapshot.characters.append(_make_character("Alice"))

    with caplog.at_level(logging.WARNING, logger="sidequest.server.views"):
        views.build_game_state_view(handler)
        views.build_game_state_view(handler)
        views.build_game_state_view(handler)

    matching = [r for r in caplog.records if "party_zone_absent_with_characters" in r.getMessage()]
    assert len(matching) == 1, (
        f"expected exactly one party_zone_absent warning, got {len(matching)}: "
        f"{[r.getMessage() for r in matching]}"
    )


def test_session_view_does_not_warn_when_party_zone_present(session_fixture, caplog) -> None:
    """With a party zone set, no zone-absent warning fires."""
    import logging

    sd, handler = session_fixture
    # session_fixture defaults TestHero to "Main Hall"; mirror for Alice.
    sd.snapshot.characters.append(_make_character("Alice"))
    sd.snapshot.character_locations["Alice"] = "Main Hall"

    with caplog.at_level(logging.WARNING, logger="sidequest.server.views"):
        views.build_game_state_view(handler)

    matching = [r for r in caplog.records if "party_zone_absent_with_characters" in r.getMessage()]
    assert matching == []


def test_session_view_does_not_warn_when_no_characters(session_fixture, caplog) -> None:
    """Pre-chargen (no characters) -> no warning even if zone is empty."""
    import logging

    sd, handler = session_fixture
    sd.snapshot.character_locations.clear()
    assert sd.snapshot.characters == []

    with caplog.at_level(logging.WARNING, logger="sidequest.server.views"):
        views.build_game_state_view(handler)

    matching = [r for r in caplog.records if "party_zone_absent_with_characters" in r.getMessage()]
    assert matching == []


# ---------------------------------------------------------------------------
# Finding 3: is_hidden_status_list must use whole-token membership, not
# substring match. "unhidden", "hidden_buff_removed", etc. must NOT match.
# ---------------------------------------------------------------------------


def test_hidden_status_whole_token_membership() -> None:
    check = views.is_hidden_status_list

    def s(text: str) -> Status:
        return Status(text=text, severity=StatusSeverity.Scratch)

    # Exact tokens -> True.
    assert check([s("hidden")]) is True
    assert check([s("invisible")]) is True
    assert check([s("stealth")]) is True
    assert check([s("concealed")]) is True

    # Case-insensitive exact tokens -> True.
    assert check([s("Hidden")]) is True
    assert check([s("INVISIBLE")]) is True

    # Substring false-positives previously matched; must be rejected now.
    assert check([s("unhidden")]) is False
    assert check([s("hidden_buff_removed")]) is False
    assert check([s("no_longer_concealed")]) is False
    assert check([s("revealed")]) is False

    # Empty / unrelated.
    assert check([]) is False
    assert check([s("bleeding"), s("poisoned")]) is False

    # Mixed list with one exact match -> True.
    assert check([s("bleeding"), s("hidden")]) is True
