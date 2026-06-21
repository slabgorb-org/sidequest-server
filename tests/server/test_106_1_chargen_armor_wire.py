"""Wire-first test for Story 106-1: the chargen armor-derivation step fires
through the production ``_chargen_confirmation()`` seam.

Reviewer [HIGH][TEST] (rework): every existing 106-1 test invoked
``equip_starting_armor`` DIRECTLY. ``chargen_mixin.py:1257`` is the only
production call site, and nothing exercised it — drop that line and the whole
suite stays green while every Warrior silently reverts to AC 10, re-breaking
ramp lever #1. That is *exactly* how the original bug survived: a mechanical
value that silently did nothing, with no test asserting the engine engaged from
the real pipeline. Server CLAUDE.md: "Every Test Suite Needs a Wiring Test" +
"No Source-Text Wiring Tests" (assert the OTEL span fired through the real
handler, not that a literal appears in source).

This test walks the real ``caverns_and_claudes`` (WWN) chargen flow in MP
context through ``handle_message``, selects the Warrior Calling, sends a real
CONFIRMATION, and asserts the armor-derivation step fired.

**World choice is incidental.** ``equip_starting_armor`` and ``warrior_kit``
(equipment_tables.yaml) are both genre-tier surfaces, and the leather
``armor_class`` lives in the genre-tier ``caverns_and_claudes/inventory.yaml`` —
so any cnc world hosts the identical wire. We use ``flickering_reach`` (the
hermetic world the canonical ``test_45_2_chargen_to_playing_wire`` walks) rather
than the WWN megadungeon ``beneath_sunden``, whose ADR-106 connect-time init
reaches the Anthropic SDK and would break test hermeticity.

**Why a roll-agnostic assertion.** ``warrior_kit.armor`` is a 3-option random
table (``[leather_armor, shield_wood, helmet_iron]``, one uniform pick). The
armor spans (``chargen.armor_equipped`` / ``chargen.armor_unresolved``) are
emitted EXCLUSIVELY inside ``equip_starting_armor`` (chargen_loadout.py:388/410),
whose only production caller is chargen_mixin.py:1257. Since the kit always rolls
exactly one armor piece, exactly one of those two spans MUST fire per Warrior
chargen-confirm — regardless of which piece rolled. If line 1257 is removed,
NEITHER fires and this test fails. Deterministic pass/fail on the wire, immune to
the random roll and to harmless refactors.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import psycopg
import pytest

from sidequest.game.persistence import GameMode
from sidequest.protocol import GameMessage
from sidequest.protocol.messages import (
    CharacterCreationMessage,
    CharacterCreationPayload,
)
from sidequest.server.session_handler import WebSocketSessionHandler
from sidequest.server.session_room import LobbyState, RoomRegistry

CONTENT_ROOT = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"

# The two armor spans the derivation step emits — one of them MUST fire.
SPAN_ARMOR_EQUIPPED = "chargen.armor_equipped"
SPAN_ARMOR_UNRESOLVED = "chargen.armor_unresolved"

LEATHER_AC = 13  # WWN-SRD leather value (content-sourced, story 106-1 AC2)


def _seed_mp_save(slug: str, genre: str, world: str) -> None:
    """Register an empty MP session in Postgres (ADR-115 — connect reads PG)."""
    from sidequest.game import db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug

    _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug=slug,
        mode=str(GameMode.MULTIPLAYER),
        genre_slug=genre,
        world_slug=world,
    )


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Point the process-global pool at a per-worker throwaway PG database and
    TRUNCATE per-test so connect can't resume a leaked snapshot (mirrors
    test_45_2_chargen_to_playing_wire)."""
    from sidequest.game import db_pool

    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(plain, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename <> 'alembic_version'"
        ).fetchall()
        if rows:
            names = ", ".join(f'"{r[0]}"' for r in rows)
            conn.execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    yield
    db_pool.close_pool()


async def _walk_to_confirmation(handler, builder, player_id: str, *, name: str) -> None:
    """Walk cnc WWN scenes (the_calling → the_story → the_kit → the_mouth),
    selecting the Warrior Calling at the choice-bearing scene."""
    max_steps = 20
    for _step in range(max_steps):
        if builder.is_confirmation():
            return
        if not builder.is_in_progress():
            pytest.fail(f"unexpected chargen phase: {builder._phase!r}")  # noqa: SLF001
        scene = builder.current_scene()
        if scene.choices:
            # the_calling: choice "1" is the Warrior Calling (char_creation.yaml
            # the_calling, first choice class_hint: Warrior).
            payload = CharacterCreationPayload(phase="scene", choice="1")
        elif scene.allows_freeform:
            payload = CharacterCreationPayload(phase="scene", choice=name)
        else:
            payload = CharacterCreationPayload(phase="continue")
        await handler.handle_message(CharacterCreationMessage(payload=payload, player_id=player_id))
    pytest.fail(f"chargen did not reach confirmation within {max_steps} steps")


@pytest.mark.asyncio
async def test_chargen_confirm_fires_armor_derivation_through_real_wire(otel_capture) -> None:
    """THE wire test for chargen_mixin.py:1257.

    Drive a real cnc Warrior through ``_chargen_confirmation``.
    ``warrior_kit`` rolls exactly one armor item, so exactly ONE of
    {chargen.armor_equipped, chargen.armor_unresolved} must fire — tagged with
    this PC's name. Remove the equip_starting_armor call and neither fires.
    """
    if not (CONTENT_ROOT / "caverns_and_claudes").is_dir():
        pytest.skip("caverns_and_claudes content not found")

    slug = "wire-test-106-1-armor"
    genre = "caverns_and_claudes"
    world = "flickering_reach"
    player_id = "grix"
    char_name = "Grix"

    _seed_mp_save(slug, genre, world)
    registry = RoomRegistry()
    handler = WebSocketSessionHandler(
        save_dir=Path("/tmp/sq-106-1-wire"),
        genre_pack_search_paths=[CONTENT_ROOT],
    )
    out_queue: asyncio.Queue[object] = asyncio.Queue()
    handler.attach_room_context(
        registry=registry,
        socket_id=f"sock-{player_id}",
        out_queue=out_queue,
    )

    connect_msg = GameMessage.model_validate(
        {
            "type": "SESSION_EVENT",
            "player_id": player_id,
            "payload": {"event": "connect", "game_slug": slug, "player_name": char_name},
        }
    )
    out = await handler.handle_message(connect_msg)
    assert any(
        getattr(m, "type", None) == "SESSION_EVENT"
        and getattr(getattr(m, "payload", None), "event", None) == "connected"
        for m in out
    ), "connect must produce a SESSION_EVENT payload.event='connected'"

    seat_msg = GameMessage.model_validate(
        {"type": "PLAYER_SEAT", "player_id": player_id, "payload": {"character_slot": char_name}}
    )
    await handler.handle_message(seat_msg)
    room = registry.get(slug)
    assert room is not None, "room must be created on slug-connect"

    sd = handler._session_data  # type: ignore[attr-defined]  # noqa: SLF001
    assert sd is not None and sd.builder is not None, "connect must construct a chargen builder"
    builder = sd.builder

    await _walk_to_confirmation(handler, builder, player_id, name=char_name)
    assert builder.is_confirmation()

    # PRE-CONDITION: no armor span has fired yet (build() hasn't run).
    pre = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name in (SPAN_ARMOR_EQUIPPED, SPAN_ARMOR_UNRESOLVED)
    ]
    assert not pre, "armor derivation must not fire before confirmation"

    # THE WIRE: confirmation runs _chargen_confirmation → apply_starting_loadout
    # → equip_starting_armor (chargen_mixin.py:1257).
    await handler.handle_message(
        CharacterCreationMessage(
            payload=CharacterCreationPayload(phase="confirmation"), player_id=player_id
        )
    )
    assert room._seated[player_id].state == LobbyState.PLAYING, (  # noqa: SLF001
        "sanity: chargen confirmation should have completed and seated the player"
    )

    equipped = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_ARMOR_EQUIPPED]
    unresolved = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_ARMOR_UNRESOLVED]

    # The load-bearing assertion: the derivation step engaged from the real
    # handler. warrior_kit rolls exactly one armor piece → exactly one span.
    total = len(equipped) + len(unresolved)
    assert total == 1, (
        f"exactly one armor span must fire from the production chargen-confirm "
        f"wire (warrior_kit rolls one armor piece) — got {len(equipped)} equipped "
        f"+ {len(unresolved)} unresolved. If 0, the equip_starting_armor call at "
        f"chargen_mixin.py:1257 is missing or unreachable (ramp lever #1 silently "
        f"reverted to AC 10)."
    )

    # The span belongs to THIS PC, and the AC outcome is consistent with which
    # span fired (documents the real production behavior end-to-end, not vacuous).
    character = next(c for c in sd.snapshot.characters if c.core.name == char_name)
    if equipped:
        attrs = dict(equipped[0].attributes or {})
        assert attrs.get("pc_name") == char_name
        assert attrs.get("ac_after") == LEATHER_AC, "the equipped roll derives the WWN-SRD AC 13"
        assert character.core.armor_class == LEATHER_AC, (
            "the derived AC must land on the built character's core, not just the span"
        )
        # warrior_kit rolls one AC-13 armor from the WWN-verbatim catalog
        # (wwn_linothorax or wwn_small_shield, per the rolled seed). Cross-check
        # the span's reported item_id actually landed equipped in inventory —
        # end-to-end, not a hardcoded assumption about which piece rolled.
        armor_item_id = attrs.get("item_id")
        armor = next(
            i for i in character.core.inventory.items if i.get("id") == armor_item_id
        )
        assert armor["equipped"] is True, "the rolled armor must be equipped"
    else:
        # shield_wood / helmet_iron — no catalog armor_class today: loud fail,
        # AC stays 10 (No-Silent-Fallback). See blocking delivery finding:
        # 2/3 of warrior_kit armor rolls currently take THIS branch.
        attrs = dict(unresolved[0].attributes or {})
        assert attrs.get("pc_name") == char_name
        assert attrs.get("reason") in ("catalog_armor_class_missing", "no_catalog_entry")
        assert character.core.armor_class == 10, (
            "armor with no catalog armor_class must fail loud, never silently fabricate an AC"
        )
