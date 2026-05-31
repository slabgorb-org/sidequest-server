"""Multiplayer party status / acting-character / turn-context wiring.

Covers the Phase 2 fixes from the 2026-04-24 Mawdeep playtest:

- ``_resolve_acting_character_name`` identifies the requesting socket's
  PC by ``player_id`` via the room's seat map (not by guessing
  ``snapshot.characters[0]`` which is arbitrary across commit order).
- ``_build_turn_context`` builds ``party_peers`` excluding the acting
  PC, so the narrator no longer absorbs the peer as a hireling.
- ``views.build_session_start_party_status(handler, ...)`` enumerates
  every PC in the snapshot, mapping each character_slot back to its
  owning player_id via the room.

These three together close the gap behind the playtest bugs:
"Party panel only shows self" and "Narrator absorbs peer as hireling".
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.persistence import GameMode
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.loader import load_genre_pack
from sidequest.server import views
from sidequest.server.session_handler import (
    WebSocketSessionHandler,
    _build_turn_context,
    _resolve_acting_character_name,
    _SessionData,
)
from sidequest.server.session_room import SessionRoom

CONTENT_GENRE_PACKS = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


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


def _sd(player_id: str, player_name: str, characters: list[Character]) -> _SessionData:
    """Build a session-data fixture (Wave 2B / story 45-48 — no party-level
    ``location=`` kwarg; per-character locations live in
    ``snapshot.character_locations`` and tests seed them per case)."""
    return _SessionData(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        player_name=player_name,
        player_id=player_id,
        snapshot=GameSnapshot(
            genre_slug="caverns_and_claudes",
            world_slug="mawdeep",
            turn_manager=TurnManager(interaction=1),
            characters=list(characters),
        ),
        repository=MagicMock(),
        dungeon_repository=MagicMock(),
        telemetry_sink=MagicMock(),
        genre_pack=load_genre_pack(CONTENT_GENRE_PACKS / "caverns_and_claudes"),
        orchestrator=MagicMock(),
        mode=GameMode.MULTIPLAYER,
    )


def test_acting_character_resolved_via_room_seat_map() -> None:
    """When a room is bound, the acting PC is identified by player_id."""
    laverne = _char("Laverne")
    shirley = _char("Shirley")
    room = SessionRoom(slug="2026-04-24-mawdeep-mp", mode=GameMode.MULTIPLAYER)
    room.seat("p:laverne", character_slot="Laverne")
    room.seat("p:shirley", character_slot="Shirley")

    sd_p2 = _sd("p:shirley", "Shirley", [laverne, shirley])
    # snapshot.characters[0] is Laverne — naively returning the first char
    # would mis-identify the acting player. Resolver must pick Shirley.
    assert _resolve_acting_character_name(sd_p2, room) == "Shirley"


def test_acting_character_falls_back_to_first_when_no_room() -> None:
    """Solo / pre-MP path: no room → return first character (legacy)."""
    pc = _char("Lonewolf")
    sd = _sd("p:lonewolf", "Lonewolf", [pc])
    assert _resolve_acting_character_name(sd, room=None) == "Lonewolf"


def test_acting_character_returns_player_name_when_snapshot_empty() -> None:
    """Empty snapshot.characters: return lobby player_name."""
    sd = _sd("p:none", "Newbie", [])
    assert _resolve_acting_character_name(sd, room=None) == "Newbie"


def test_party_peers_excludes_acting_pc_in_multiplayer() -> None:
    """Party-peer block must not include the acting socket's own PC."""
    laverne = _char("Laverne")
    shirley = _char("Shirley")
    room = SessionRoom(slug="slug-a", mode=GameMode.MULTIPLAYER)
    room.seat("p:laverne", character_slot="Laverne")
    room.seat("p:shirley", character_slot="Shirley")

    # Bind the room so _build_turn_context can read room.session.
    snap = GameSnapshot(genre_slug="caverns_and_claudes")
    room.bind_world(snapshot=snap, store=MagicMock())

    # Player 2's session: party_peers should be [Laverne], not [Shirley]
    sd = _sd("p:shirley", "Shirley", [laverne, shirley])
    ctx = _build_turn_context(sd, room=room)
    peer_names = [p.name for p in ctx.party_peers]
    assert peer_names == ["Laverne"]
    assert ctx.character_name == "Shirley"


def test_party_peers_empty_in_solo() -> None:
    """Solo session with one PC produces no party peers."""
    pc = _char("Solo")
    sd = _sd("p:solo", "Solo", [pc])
    ctx = _build_turn_context(sd, room=None)
    assert ctx.party_peers == []
    assert ctx.character_name == "Solo"


def test_party_status_enumerates_all_pcs_in_multiplayer() -> None:
    """_build_session_start_party_status returns one PartyMember per PC,
    with the requesting socket's PC first and peers mapped via the room
    seat table.
    """
    laverne = _char("Laverne")
    shirley = _char("Shirley")
    sd = _sd("p:shirley", "Shirley", [laverne, shirley])

    handler = WebSocketSessionHandler(save_dir=Path("/tmp/sq-test-saves"))
    room = SessionRoom(slug="2026-04-24-mawdeep-mp", mode=GameMode.MULTIPLAYER)
    room.seat("p:laverne", character_slot="Laverne")
    room.seat("p:shirley", character_slot="Shirley")
    handler._room = room

    msg = views.build_session_start_party_status(handler, sd, shirley, "p:shirley")
    members = msg.payload.members
    # Self first, then peer
    assert [str(m.character_name) for m in members] == ["Shirley", "Laverne"]
    # Peer player_id resolved from seat map (not the synthetic peer:<name>)
    laverne_member = members[1]
    assert str(laverne_member.player_id) == "p:laverne"
    # Self is the requesting socket's player_id
    shirley_member = members[0]
    assert str(shirley_member.player_id) == "p:shirley"


def test_build_session_start_party_status_carries_player_identity_from_room() -> None:
    """Wiring test (Story 67-6): the real ``build_session_start_party_status``
    must read the room store and stamp each member's ``player_identity``.

    The room knows the connected self player's identity (set via
    ``set_player_identity``); the peer has NO room identity entry. The builder
    must therefore stamp the self member with the room identity and leave the
    peer's ``player_identity`` as None — never fabricating it from the
    character name.

    If the ``get_player_identity`` call is dropped from the builder, the self
    assertion fails — this drives the load-bearing wiring of the story through
    the real function, not a direct PartyMember construction.
    """
    laverne = _char("Laverne")
    shirley = _char("Shirley")
    sd = _sd("p:shirley", "Shirley", [laverne, shirley])

    handler = WebSocketSessionHandler(save_dir=Path("/tmp/sq-test-saves"))
    room = SessionRoom(slug="2026-05-31-identity-mp", mode=GameMode.MULTIPLAYER)
    room.seat("p:laverne", character_slot="Laverne")
    room.seat("p:shirley", character_slot="Shirley")
    # Self (connected) has a resolved identity; peer does NOT.
    room.set_player_identity("p:shirley", "shirley@example.com")
    handler._room = room

    msg = views.build_session_start_party_status(handler, sd, shirley, "p:shirley")
    by_name = {str(m.character_name): m for m in msg.payload.members}

    self_member = by_name["Shirley"]
    peer_member = by_name["Laverne"]

    # Self identity flows from the room store.
    assert self_member.player_identity == "shirley@example.com"
    # Peer has no room identity -> None, and is NEVER given the character name.
    assert peer_member.player_identity is None
    assert peer_member.player_identity != peer_member.character_name


def test_build_session_start_party_status_connected_peer_identity_visible() -> None:
    """Gap 1 (Story 67-6): a connected peer WITH a room identity entry shows
    their identity in the PARTY_STATUS frame.

    The prior test proved "no room entry -> None". This test pins the OTHER
    side of the seam: when the peer IS connected and their identity WAS bound
    via ``set_player_identity``, the builder must resolve it through
    ``seat_map.get(char.core.name)`` -> ``get_player_identity(pid)`` and stamp
    it onto the peer's PartyMember.

    Mutation check: if ``get_player_identity`` is dropped from the builder, or
    the pid used for the lookup doesn't match the pid the identity was stored
    under, ``peer_member.player_identity`` becomes None and the assertion fails.
    """
    laverne = _char("Laverne")
    shirley = _char("Shirley")
    sd = _sd("p:shirley", "Shirley", [laverne, shirley])

    handler = WebSocketSessionHandler(save_dir=Path("/tmp/sq-test-saves"))
    room = SessionRoom(slug="2026-05-31-peer-identity-mp", mode=GameMode.MULTIPLAYER)
    room.seat("p:laverne", character_slot="Laverne")
    room.seat("p:shirley", character_slot="Shirley")
    # Both players have resolved identities — peer is connected, not absent.
    room.set_player_identity("p:shirley", "shirley@example.com")
    room.set_player_identity("p:laverne", "laverne@example.com")
    handler._room = room

    msg = views.build_session_start_party_status(handler, sd, shirley, "p:shirley")
    by_name = {str(m.character_name): m for m in msg.payload.members}

    self_member = by_name["Shirley"]
    peer_member = by_name["Laverne"]

    # Self identity flows from the room store.
    assert self_member.player_identity == "shirley@example.com"
    # Connected peer's identity also flows from the room store — this is what
    # the prior test did NOT cover.
    assert peer_member.player_identity == "laverne@example.com"
    # Sanity: neither identity value equals the character name (no fabrication).
    assert self_member.player_identity != str(self_member.character_name)
    assert peer_member.player_identity != str(peer_member.character_name)


def test_party_status_falls_back_to_synthetic_peer_id_when_no_seat() -> None:
    """If a peer character is in the snapshot but the room has no seat
    record (e.g. pre-PLAYER_SEAT race), use a stable synthetic id rather
    than colliding on a real player_id.
    """
    laverne = _char("Laverne")
    shirley = _char("Shirley")
    sd = _sd("p:shirley", "Shirley", [laverne, shirley])

    handler = WebSocketSessionHandler(save_dir=Path("/tmp/sq-test-saves"))
    # Room exists but only Shirley has claimed her seat
    room = SessionRoom(slug="slug-a", mode=GameMode.MULTIPLAYER)
    room.seat("p:shirley", character_slot="Shirley")
    handler._room = room

    msg = views.build_session_start_party_status(handler, sd, shirley, "p:shirley")
    members = msg.payload.members
    laverne_member = next(m for m in members if str(m.character_name) == "Laverne")
    assert str(laverne_member.player_id) == "peer:Laverne"


def test_party_status_solo_returns_single_member() -> None:
    """Solo path: single PC, no room — one member equal to the requesting PC."""
    pc = _char("Solo")
    sd = _sd("p:solo", "Solo", [pc])

    handler = WebSocketSessionHandler(save_dir=Path("/tmp/sq-test-saves"))
    msg = views.build_session_start_party_status(handler, sd, pc, "p:solo")
    assert len(msg.payload.members) == 1
    assert str(msg.payload.members[0].character_name) == "Solo"


def test_resolve_self_character_uses_player_seats_binding() -> None:
    """Playtest 2026-04-25 "Tab 2 sees Laverne (YOU)" regression test.

    snapshot has [Laverne, Shirley]; player_seats binds Shirley→Shirley.
    For sd.player_id=p:shirley the resolver must return the Shirley
    Character, NOT snapshot.characters[0] (Laverne).
    """
    laverne = _char("Laverne")
    shirley = _char("Shirley")
    sd = _sd("p:shirley", "Shirley", [laverne, shirley])
    sd.snapshot.player_seats = {"p:laverne": "Laverne", "p:shirley": "Shirley"}

    handler = WebSocketSessionHandler(save_dir=Path("/tmp/sq-test-saves"))
    resolved = views.resolve_self_character(handler, sd)
    assert resolved is shirley
    assert resolved is not laverne


def test_resolve_self_character_uses_room_seat_when_player_seats_empty() -> None:
    """Pre-2026-04-25 saves have empty player_seats but a live room seat.

    The resolver must fall through to the room's slot_to_player_id() so
    multi-PC snapshots still resolve correctly without a persisted
    binding.
    """
    laverne = _char("Laverne")
    shirley = _char("Shirley")
    sd = _sd("p:shirley", "Shirley", [laverne, shirley])
    # No player_seats (legacy snapshot).
    assert sd.snapshot.player_seats == {}

    handler = WebSocketSessionHandler(save_dir=Path("/tmp/sq-test-saves"))
    room = SessionRoom(slug="slug-x", mode=GameMode.MULTIPLAYER)
    room.seat("p:laverne", character_slot="Laverne")
    room.seat("p:shirley", character_slot="Shirley")
    handler._room = room

    resolved = views.resolve_self_character(handler, sd)
    assert resolved is shirley


def test_resolve_self_character_returns_none_for_legacy_solo() -> None:
    """Legacy solo save: no player_seats, no room. Resolver returns None,
    callers fall back to snapshot.characters[0].
    """
    pc = _char("Solo")
    sd = _sd("p:solo", "Solo", [pc])

    handler = WebSocketSessionHandler(save_dir=Path("/tmp/sq-test-saves"))
    assert views.resolve_self_character(handler, sd) is None


def test_party_status_uses_resolver_when_caller_passes_resolved_character() -> None:
    """Wiring test: turn-end / slug-resume PARTY_STATUS callers MUST pass
    the resolver's result (not snapshot.characters[0]) so the requesting
    socket's PC is tagged as 'self', not whichever PC happened to be
    appended first.

    This test simulates the exact playtest 2026-04-25 repro: two PCs in
    the snapshot, the requesting socket is the second player, and the
    PARTY_STATUS frame is built via the resolver. Self-tagged member
    must be Shirley with character_name="Shirley", not Laverne tagged
    with Shirley's player_id.
    """
    laverne = _char("Laverne")
    shirley = _char("Shirley")
    sd = _sd("p:shirley", "Shirley", [laverne, shirley])
    sd.snapshot.player_seats = {"p:laverne": "Laverne", "p:shirley": "Shirley"}

    handler = WebSocketSessionHandler(save_dir=Path("/tmp/sq-test-saves"))
    room = SessionRoom(slug="slug-x", mode=GameMode.MULTIPLAYER)
    room.seat("p:laverne", character_slot="Laverne")
    room.seat("p:shirley", character_slot="Shirley")
    handler._room = room

    # Mimic the fixed call-site pattern: resolver-or-fallback.
    self_char = views.resolve_self_character(handler, sd) or sd.snapshot.characters[0]
    assert self_char is shirley, (
        "Resolver must pick Shirley for sd.player_id=p:shirley — picking "
        "characters[0] (Laverne) is the bug we're regressing against"
    )

    msg = views.build_session_start_party_status(handler, sd, self_char, "p:shirley")
    members = msg.payload.members
    # Self comes first; self's character_name and player_id must agree.
    self_member = members[0]
    assert str(self_member.character_name) == "Shirley"
    assert str(self_member.player_id) == "p:shirley"
    # Peer is Laverne with Laverne's player_id (NOT Shirley's).
    peer_member = members[1]
    assert str(peer_member.character_name) == "Laverne"
    assert str(peer_member.player_id) == "p:laverne"
    # No two members share a player_id (the bug produced colliding ids).
    pids = [str(m.player_id) for m in members]
    assert len(set(pids)) == len(pids), f"Duplicate player_id in PartyMember frame: {pids}"


# ---------------------------------------------------------------------------
# Shared Room.snapshot wiring (ADR-037 Python port)
# ---------------------------------------------------------------------------


def test_two_handlers_share_room_snapshot_after_bind():
    """Two handlers bound to the same room observe the same snapshot
    object — mutating one's sd.snapshot.characters is visible to the
    other without any reload.

    This is the core regression guard for the per-session divergence
    that the _merge_peer_state_into_snapshot band-aid was masking.
    """
    from pathlib import Path as _Path

    room = SessionRoom(slug="2026-04-25-shared-test", mode=GameMode.MULTIPLAYER)
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        location="Entrance",
    )
    snap.characters = []
    store = MagicMock()
    room.bind_world(snapshot=snap, store=store)

    laverne = _char("Laverne")
    shirley = _char("Shirley")

    # Two handlers, each with sd bound to the same room snapshot ref.
    handler_a = WebSocketSessionHandler(save_dir=_Path("/tmp/sq-test-saves"))
    handler_a._room = room
    sd_a = _sd("p:laverne", "Laverne", [])
    sd_a.snapshot = room.snapshot  # type: ignore[assignment]
    sd_a.repository = room.store  # type: ignore[assignment]
    handler_a._session_data = sd_a

    handler_b = WebSocketSessionHandler(save_dir=_Path("/tmp/sq-test-saves"))
    handler_b._room = room
    sd_b = _sd("p:shirley", "Shirley", [])
    sd_b.snapshot = room.snapshot  # type: ignore[assignment]
    sd_b.repository = room.store  # type: ignore[assignment]
    handler_b._session_data = sd_b

    # Mutate via handler_a's sd; handler_b sees it.
    handler_a._session_data.snapshot.characters.append(laverne)
    assert [c.core.name for c in handler_b._session_data.snapshot.characters] == [
        "Laverne",
    ]

    # And vice versa.
    handler_b._session_data.snapshot.characters.append(shirley)
    assert sorted(c.core.name for c in handler_a._session_data.snapshot.characters) == [
        "Laverne",
        "Shirley",
    ]


def test_chargen_commit_visible_to_peer_handler_immediately() -> None:
    """ADR-037 regression: when peer commits chargen, our handler's
    sd.snapshot reflects both PCs and both seats without reload.
    """
    room = SessionRoom(slug="2026-04-25-chargen-share", mode=GameMode.MULTIPLAYER)
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        location="Entrance",
    )
    snap.characters = []
    snap.player_seats = {}
    store = MagicMock()
    room.bind_world(snapshot=snap, store=store)

    # Player A's chargen-commit equivalent: append PC + record seat in
    # the canonical snapshot.
    laverne = _char("Laverne")
    room.snapshot.characters.append(laverne)
    room.snapshot.player_seats["p:laverne"] = "Laverne"

    # Player B observes both immediately via the same reference.
    assert [c.core.name for c in room.snapshot.characters] == ["Laverne"]
    assert room.snapshot.player_seats == {"p:laverne": "Laverne"}

    # Player B's chargen-commit equivalent: same snapshot.
    shirley = _char("Shirley")
    room.snapshot.characters.append(shirley)
    room.snapshot.player_seats["p:shirley"] = "Shirley"

    # Player A observes both immediately.
    assert sorted(c.core.name for c in room.snapshot.characters) == [
        "Laverne",
        "Shirley",
    ]
    assert room.snapshot.player_seats == {
        "p:laverne": "Laverne",
        "p:shirley": "Shirley",
    }


def test_room_save_routes_through_canonical_store() -> None:
    """room.save() persists the canonical snapshot via the canonical
    store. Verifies the per-session store.save calls have been removed
    in favor of the room-level save.
    """
    room = SessionRoom(slug="slug", mode=GameMode.MULTIPLAYER)
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        location="Entrance",
    )
    store = MagicMock()
    room.bind_world(snapshot=snap, store=store)

    room.save()

    store.save.assert_called_once_with(snap)


def test_solo_path_unaffected_by_shared_room_model() -> None:
    """Single-occupant SOLO room round-trips through bind/save with
    identical semantics to multiplayer. Regression guard for the
    'don't break solo' constraint in the shared-snapshot refactor.
    """
    room = SessionRoom(slug="2026-04-25-solo", mode=GameMode.SOLO)
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        location="Entrance",
    )
    snap.characters = [_char("Solo")]
    store = MagicMock()
    room.bind_world(snapshot=snap, store=store)

    assert room.snapshot is snap
    assert [c.core.name for c in room.snapshot.characters] == ["Solo"]

    room.save()
    store.save.assert_called_once_with(snap)


# ---------------------------------------------------------------------------
# Per-character location tracking (playtest 2026-05-02 [BUG] — multiplayer
# location header showed peer's scene, not the viewer's). PartyMember frames
# must carry each character's own last-known location, not the global
# snapshot.location which is whichever player most recently narrated.
# ---------------------------------------------------------------------------


def test_party_member_uses_per_character_location_when_set() -> None:
    """``party_member_from_character`` projects ``character_locations[name]``
    into the member's ``current_location``. Wave 2B (story 45-48):
    per-character is the only source of truth — there is no global
    ``snapshot.location`` field anymore.
    """
    laverne = _char("Laverne")
    shirley = _char("Shirley")
    sd = _sd("p:shirley", "Shirley", [laverne, shirley])
    sd.snapshot.character_locations = {
        "Laverne": "Galley",
        "Shirley": "Cockpit",
    }

    handler = WebSocketSessionHandler(save_dir=Path("/tmp/sq-test-saves"))

    laverne_member = views.party_member_from_character(handler, sd, laverne, "p:laverne", "Laverne")
    shirley_member = views.party_member_from_character(handler, sd, shirley, "p:shirley", "Shirley")

    assert str(laverne_member.current_location) == "Galley"
    assert str(shirley_member.current_location) == "Cockpit"


def test_party_member_projects_origin_calling_flavor_labels() -> None:
    """#G2 live-panel extension: a Character carrying display-only
    ``origin_label``/``calling_label`` (the chargen flavor the player chose)
    must project them onto ``member.sheet`` so the live CharacterPanel renders
    "Country Veterinary Surgeon · The Village Itself" instead of the collapsed
    mechanical slug "Doctor · Servant". The mechanical race/class are untouched.
    """
    pc = _char("Vyvyan")
    pc.origin_label = "The Village Itself"
    pc.calling_label = "Country Veterinary Surgeon"
    sd = _sd("p:vyvyan", "Vyvyan", [pc])

    handler = WebSocketSessionHandler(save_dir=Path("/tmp/sq-test-saves"))
    member = views.party_member_from_character(handler, sd, pc, "p:vyvyan", "Vyvyan")

    assert member.sheet is not None
    assert str(member.sheet.origin_label) == "The Village Itself"
    assert str(member.sheet.calling_label) == "Country Veterinary Surgeon"
    # Mechanical class/race slug still flows for systems that key on it.
    assert str(member.class_) == "Delver"
    assert str(member.sheet.race) == "Human"


def test_party_member_omits_flavor_labels_when_absent() -> None:
    """When chargen produced no distinct flavor label (label == archetype),
    the sheet's ``origin_label``/``calling_label`` are None so the UI cleanly
    falls back to the mechanical race/class slug — no empty-string leakage."""
    pc = _char("Solo")  # _char leaves origin_label/calling_label = ""
    sd = _sd("p:solo", "Solo", [pc])

    handler = WebSocketSessionHandler(save_dir=Path("/tmp/sq-test-saves"))
    member = views.party_member_from_character(handler, sd, pc, "p:solo", "Solo")

    assert member.sheet is not None
    assert member.sheet.origin_label is None
    assert member.sheet.calling_label is None


def test_party_member_omits_location_when_per_char_absent() -> None:
    """Wave 2B (story 45-48) — when a PC has no ``character_locations``
    entry yet (pre-first-narration), the PARTY_STATUS frame omits the
    ``current_location`` field entirely. The UI renders "(unknown)"
    rather than inheriting whichever player most recently narrated.
    There is no global ``snapshot.location`` fallback anymore.
    """
    pc = _char("Solo")
    sd = _sd("p:solo", "Solo", [pc])
    sd.snapshot.character_locations = {}

    handler = WebSocketSessionHandler(save_dir=Path("/tmp/sq-test-saves"))
    member = views.party_member_from_character(handler, sd, pc, "p:solo", "Solo")
    assert member.current_location is None


def test_build_session_start_party_status_carries_per_member_locations() -> None:
    """Wiring test: the dispatcher-built PARTY_STATUS broadcast frame must
    carry per-member ``current_location`` values so the client header /
    state mirror can render the right scene per player.

    This is the end-to-end regression guard for the playtest 2026-05-02
    bug: P1 (Itchy) opened their tab and saw P2 (Charlie)'s location in
    the header because every PartyMember was tagged with whichever
    player most recently narrated.
    """
    laverne = _char("Laverne")
    shirley = _char("Shirley")
    sd = _sd("p:shirley", "Shirley", [laverne, shirley])
    sd.snapshot.character_locations = {
        "Laverne": "Galley",
        "Shirley": "Cockpit",
    }

    handler = WebSocketSessionHandler(save_dir=Path("/tmp/sq-test-saves"))
    room = SessionRoom(slug="slug-loc", mode=GameMode.MULTIPLAYER)
    room.seat("p:laverne", character_slot="Laverne")
    room.seat("p:shirley", character_slot="Shirley")
    handler._room = room

    msg = views.build_session_start_party_status(handler, sd, shirley, "p:shirley")
    by_name = {str(m.character_name): m for m in msg.payload.members}
    assert str(by_name["Laverne"].current_location) == "Galley"
    assert str(by_name["Shirley"].current_location) == "Cockpit"


def test_apply_narration_writes_per_character_location_for_acting_pc() -> None:
    """Wiring test: when ``_apply_narration_result_to_snapshot`` processes
    a turn whose ``result.location`` is set, the acting character's
    entry in ``snapshot.character_locations`` is updated. Other
    characters' entries are not touched — peer movement only happens
    on their own turns.
    """
    from sidequest.agents.orchestrator import NarrationTurnResult
    from sidequest.server.narration_apply import _apply_narration_result_to_snapshot

    laverne = _char("Laverne")
    shirley = _char("Shirley")
    sd = _sd("p:shirley", "Shirley", [laverne, shirley])
    sd.snapshot.character_locations = {"Laverne": "Galley", "Shirley": "Galley"}

    room = SessionRoom(slug="slug-narr", mode=GameMode.MULTIPLAYER)
    room.seat("p:laverne", character_slot="Laverne")
    room.seat("p:shirley", character_slot="Shirley")

    # Construct a NarrationTurnResult that emits a location update.
    # Pick the minimal field set the apply path reads on this branch.
    result = NarrationTurnResult(
        narration="Shirley walks to the cockpit.",
        location="Cockpit",
    )

    _apply_narration_result_to_snapshot(
        sd.snapshot,
        result,
        sd.player_name,
        room=room,
        pack=sd.genre_pack,
        acting_character_name="Shirley",
    )

    # Acting character moved; peer's last-known location is unchanged.
    # Wave 2B (story 45-48): there is no party-level snapshot.location
    # to "also advance" — character_locations is the only source of truth.
    assert sd.snapshot.character_locations["Shirley"] == "Cockpit"
    assert sd.snapshot.character_locations["Laverne"] == "Galley"
