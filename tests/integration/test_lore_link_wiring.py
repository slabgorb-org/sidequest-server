"""Story 93-4 — wiring + OTEL for the History lore-link surface.

Mandatory wiring test per CLAUDE.md "Every Test Suite Needs a Wiring Test"
and "Verify Wiring, Not Just Existence". The unit suite
(``tests/game/test_lore_linking.py``) proves the resolver works in
isolation; this proves the resolver is actually *wired* into the sheet the
WebSocket state-mirror sends, and that the resolution emits the OTEL
lie-detector event the GM panel needs (CLAUDE.md OTEL Observability
Principle, AC5).

Chain exercised (mirrors tests/integration/test_creation_answers_wiring.py):
    char_creation.yaml (real caverns_and_claudes content)
    → CharacterBuilder scene walk → builder.build() → Character.creation_answers
    → seed_lore_from_char_creation(sd.lore_store, pack.char_creation)
    → party_member_from_character (views.py)
    → linked_lore_for_character → CharacterSheetDetails.lore_fragments
    → model_dump() — the protocol shape the WS state-mirror sends

OTEL: building the sheet must publish a ``lore_retrieval`` watcher event
carrying the per-character resolved count (reason=character_history_link),
captured via the watcher hub exactly like
``tests/integration/test_lore_wiring.py``.
"""

from __future__ import annotations

import asyncio
import random
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sidequest.game.builder import CharacterBuilder, StoryInput
from sidequest.game.lore_seeding import seed_lore_from_char_creation
from sidequest.game.session import GameSnapshot
from sidequest.genre.loader import load_genre_pack
from sidequest.server.session_handler import _SessionData
from sidequest.server.views import party_member_from_character
from sidequest.telemetry.watcher_hub import watcher_hub

CONTENT_ROOT = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


@pytest.fixture
def cc_pack():
    path = CONTENT_ROOT / "caverns_and_claudes"
    if not path.is_dir():
        pytest.skip(f"content pack not found at {path}")
    return load_genre_pack(path)


def _walk_chargen_calling(pack, *, calling_idx: int, lobby: str, rng_seed: int = 42):
    """Walk the C&C chargen flow, picking ``calling_idx`` at the_calling.

    Returns ``(character, chosen_label)``. The chosen label is the value the
    server records on ``creation_answers`` for the_calling — the key the
    linker matches against the seeded fragment's ``choice_label``.
    """
    builder = (
        CharacterBuilder(
            scenes=list(pack.char_creation),
            rules=pack.rules,
            backstory_tables=pack.backstory_tables,
            rng=random.Random(rng_seed),
        )
        .with_lobby_name(lobby)
        .with_equipment_tables(pack.equipment_tables)
        .with_classes(pack.classes)
    )
    # WWN port (2026-06-12): point-buy 4-scene flow (the_calling → the_story →
    # the_kit → the_mouth); no the_roll / the_arrangement (stats come from the
    # point-buy budget).
    # Scene 0 the_calling — the differentiating pick.
    scene = builder.current_scene()
    assert scene.id == "the_calling", f"expected the_calling first, got {scene.id!r}"
    assert len(scene.choices) > calling_idx, (
        f"the_calling needs >{calling_idx} choices, has {len(scene.choices)}"
    )
    chosen_label = scene.choices[calling_idx].label
    builder.apply_choice(calling_idx)
    # Scene 1 the_story — freeform (not fragment-backed).
    builder.apply_response(
        StoryInput(
            pronouns="they/them",
            background="Raised by candle-cartographers in the deep caverns.",
            description="Soot-stained, steady-handed.",
        )
    )
    # Scenes 2-3 the_kit / the_mouth — auto-advance.
    builder.apply_auto_advance()
    builder.apply_auto_advance()

    return builder.build(lobby), chosen_label


def _make_session_data(pack, characters) -> _SessionData:
    snapshot = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        characters=list(characters),
    )
    sd = _SessionData(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        player_name="Wiring Player",
        player_id="player:wiring",
        snapshot=snapshot,
        repository=MagicMock(),
        dungeon_repository=MagicMock(),
        telemetry_sink=MagicMock(),
        genre_pack=pack,
        orchestrator=MagicMock(),
    )
    # The chargen-confirm seam seeds the per-session lore store; replay it
    # here so the store holds the same creation-seed fragments a live
    # session would have.
    seed_lore_from_char_creation(sd.lore_store, list(pack.char_creation))
    return sd


async def _capture_watcher() -> list[dict]:
    """Bind the hub to this loop + attach a capturing subscriber."""
    watcher_hub.bind_loop(asyncio.get_running_loop())
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001

    captured: list[dict] = []

    class _Sock:
        async def send_json(self, data: dict) -> None:
            captured.append(data)

    await watcher_hub.subscribe(_Sock())  # type: ignore[arg-type]
    return captured


# ---------------------------------------------------------------------------
# AC1 / AC3 — the linked fragments reach the serialized sheet payload
# ---------------------------------------------------------------------------


def test_real_chargen_flow_exposes_lore_fragments_in_sheet_payload(cc_pack) -> None:
    char, chosen_label = _walk_chargen_calling(cc_pack, calling_idx=0, lobby="Solo")
    assert char.creation_answers, "precondition: chargen produced provenance"

    sd = _make_session_data(cc_pack, [char])
    party_member = party_member_from_character(
        MagicMock(), sd, char, player_id="player:wiring", player_name="Wiring Player"
    )
    sheet = party_member.sheet

    fragments = sheet.lore_fragments
    assert fragments, (
        "CharacterSheetDetails.lore_fragments is empty — the views.py wiring "
        "to linked_lore_for_character is missing (in-memory store alone is "
        "not exposure)"
    )
    # The character's chosen calling is surfaced as one of the linked rows.
    titles = {f.title for f in fragments}
    assert chosen_label in titles, (
        f"the chosen calling {chosen_label!r} must surface in the History lore; got titles {titles}"
    )
    # Source is honest — these are creation-seed fragments.
    assert all(f.source == "character_creation" for f in fragments)

    # The serialized payload (what the WS state-mirror actually sends)
    # carries the data with the contract keys, not just the live object.
    dumped = party_member.model_dump()
    dumped_frags = dumped["sheet"]["lore_fragments"]
    assert dumped_frags, "lore_fragments missing from the serialized sheet payload"
    assert set(dumped_frags[0]) >= {
        "fragment_id",
        "title",
        "summary",
        "source",
        "lore_route",
    }, f"serialized entry missing contract keys: {sorted(dumped_frags[0])}"


def test_unpicked_calling_choice_does_not_leak_into_sheet(cc_pack) -> None:
    """The store is seeded with a fragment for EVERY calling choice; the
    sheet must carry only the PC's own pick — sibling callings must not
    appear (AC6, single-PC framing)."""
    char, chosen_label = _walk_chargen_calling(cc_pack, calling_idx=0, lobby="Solo")
    sd = _make_session_data(cc_pack, [char])

    # A different calling label that exists in the pack but the PC didn't pick.
    calling_scene = next(s for s in cc_pack.char_creation if s.id == "the_calling")
    other_labels = [c.label for c in calling_scene.choices if c.label != chosen_label]
    assert other_labels, "need a sibling calling to prove non-leak"

    party_member = party_member_from_character(
        MagicMock(), sd, char, player_id="player:wiring", player_name="Wiring Player"
    )
    titles = {f.title for f in party_member.sheet.lore_fragments}
    for unpicked in other_labels:
        assert unpicked not in titles, f"unpicked calling {unpicked!r} leaked into the PC's History"


# ---------------------------------------------------------------------------
# AC6 — cross-character firewall through the real views path
# ---------------------------------------------------------------------------


def test_cross_character_firewall_through_views(cc_pack) -> None:
    """Two PCs in one session pick different callings against the shared
    lore store. Neither PC's sheet may carry the other's chosen-calling
    fragment (another player's fragments do NOT leak)."""
    alice, alice_label = _walk_chargen_calling(cc_pack, calling_idx=0, lobby="Alice")
    bob, bob_label = _walk_chargen_calling(cc_pack, calling_idx=1, lobby="Bob")
    assert alice_label != bob_label, "fixture must pick distinct callings"

    sd = _make_session_data(cc_pack, [alice, bob])

    alice_titles = {
        f.title
        for f in party_member_from_character(
            MagicMock(), sd, alice, player_id="player:a", player_name="Alice"
        ).sheet.lore_fragments
    }
    bob_titles = {
        f.title
        for f in party_member_from_character(
            MagicMock(), sd, bob, player_id="player:b", player_name="Bob"
        ).sheet.lore_fragments
    }

    assert alice_label in alice_titles and bob_label not in alice_titles, (
        f"Alice's History leaked Bob's calling: {alice_titles}"
    )
    assert bob_label in bob_titles and alice_label not in bob_titles, (
        f"Bob's History leaked Alice's calling: {bob_titles}"
    )


# ---------------------------------------------------------------------------
# AC5 — OTEL: lore-link resolution emits a per-character count for the GM panel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lore_link_emits_resolution_count_watcher_event(cc_pack) -> None:
    """Building the sheet must publish a ``lore_retrieval`` watcher event
    whose fields carry the per-character resolved fragment count — the GM
    panel's lie-detector that lore actually reached the surface (AC5)."""
    captured = await _capture_watcher()

    char, _ = _walk_chargen_calling(cc_pack, calling_idx=0, lobby="Solo")
    sd = _make_session_data(cc_pack, [char])

    party_member = party_member_from_character(
        MagicMock(), sd, char, player_id="player:wiring", player_name="Wiring Player"
    )
    resolved = len(party_member.sheet.lore_fragments)
    assert resolved >= 1, "precondition: at least one fragment linked"

    await asyncio.sleep(0.05)

    typed = [
        e
        for e in captured
        if e.get("event_type") == "lore_retrieval"
        and e.get("fields", {}).get("reason") == "character_history_link"
    ]
    assert len(typed) >= 1, (
        "expected a lore_retrieval/character_history_link watcher event; "
        f"captured event types: {[e.get('event_type') for e in captured]}"
    )
    fields = typed[0]["fields"]
    assert fields.get("resolved_count") == resolved, (
        f"resolved_count must equal the surfaced fragment count {resolved}; "
        f"got {fields.get('resolved_count')}"
    )
    # Tagged to the character so the GM panel can attribute per-PC.
    assert "Wiring Player" in str(fields.get("character") or fields.get("player_name")), (
        f"event must identify the character/player; got {fields}"
    )
