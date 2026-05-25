"""Regression guards for the 2026-04-26 multiplayer fix.

Two defects collapsed multiplayer to "two parallel solo games on one
slug" (playtest 2026-04-26 Fonzie/Richie / Mawdeep):

1. ``_handle_character_creation_complete`` reassigned ``sd.snapshot``
   to the freshly materialized world snapshot, orphaning the
   ``room._snapshot`` reference. ``room.save()`` then persisted the
   stale pre-chargen snapshot. The next peer connecting loaded an
   empty save, treated themselves as the first commit, materialized
   their own world, and ran a divergent narrator session.

2. Each WS session constructed its own ``Orchestrator`` at connect
   time. ADR-067 mandates a single persistent narrator session per
   slug — two orchestrators meant two ``--resume`` ids, which meant
   two narrators producing two unrelated scenes.

These tests pin the fixes so they don't regress.
"""

from __future__ import annotations

from sidequest.agents.orchestrator import Orchestrator
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.persistence import GameMode
from sidequest.game.session import GameSnapshot
from sidequest.server.session_room import SessionRoom


def _char(name: str) -> Character:
    return Character(
        core=CreatureCore(
            name=name,
            description="d",
            personality="p",
            inventory=Inventory(),
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        backstory=f"{name}'s tale.",
        char_class="Delver",
        race="Human",
    )


def test_replace_with_preserves_identity_and_propagates_through_room() -> None:
    """``GameSnapshot.replace_with`` mutates in place, so a peer session
    bound to the same room observes the new state without reloading.
    """
    room = SessionRoom(slug="2026-04-26-replace-with", mode=GameMode.MULTIPLAYER)
    canonical = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
    )
    room.bind_world(snapshot=canonical, store=object())  # type: ignore[arg-type]

    # The peer's session held this reference at connect time; after
    # the first committer's chargen-complete, the reference must still
    # be live and observe the new state.
    peer_view = room.snapshot
    canonical_id = id(canonical)

    materialized = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
    )
    materialized.characters = [_char("Fonzie")]
    materialized.player_seats = {"p:fonzie": "Fonzie"}
    materialized.character_locations["Fonzie"] = "Mouth of Mawdeep"

    canonical.replace_with(materialized)

    # Identity preserved — peer_view, room.snapshot, and canonical
    # are still the same object.
    assert id(canonical) == canonical_id
    assert peer_view is room.snapshot
    assert peer_view is canonical
    # Content updated — Wave 2B: per-character location.
    assert peer_view.character_locations.get("Fonzie") == "Mouth of Mawdeep"
    assert [c.core.name for c in peer_view.characters] == ["Fonzie"]
    assert peer_view.player_seats == {"p:fonzie": "Fonzie"}

    # And a second commit on this snapshot stacks rather than clobbers.
    canonical.characters.append(_char("Richie"))
    canonical.player_seats["p:richie"] = "Richie"
    assert sorted(c.core.name for c in peer_view.characters) == ["Fonzie", "Richie"]
    assert peer_view.player_seats == {"p:fonzie": "Fonzie", "p:richie": "Richie"}


def test_room_get_or_create_orchestrator_returns_same_instance() -> None:
    """ADR-067: two players on one slug must share a single Orchestrator
    so they share a single persistent narrator session.
    """
    room = SessionRoom(slug="2026-04-26-shared-narrator", mode=GameMode.MULTIPLAYER)

    factory_calls = {"n": 0}

    def factory() -> Orchestrator:
        factory_calls["n"] += 1
        # Real Orchestrator construction — verifies the factory shape
        # that session_handler.py uses lambda: Orchestrator(client=...).
        return Orchestrator()

    first = room.get_or_create_orchestrator(factory)
    second = room.get_or_create_orchestrator(factory)
    third = room.get_or_create_orchestrator(factory)

    assert first is second is third
    assert room.orchestrator is first
    # Factory is invoked exactly once: the second/third callers must
    # not pay for a fresh Claude client setup, and must not register a
    # second narrator session id.
    assert factory_calls["n"] == 1
