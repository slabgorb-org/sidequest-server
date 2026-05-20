"""Wiring test: LOCATION_DESCRIPTION fires on room change + session resume.

Story 54-2 / ADR-109. The integration test required by CLAUDE.md
"Every test suite needs a wiring test" — proves _maybe_emit_location_description
has a non-test caller in production code and emits the right shape.

Covers AC-5 (helper exists, graceful absence on missing source), AC-6
(wiring assertions: non-test caller + real fixture round-trip),
AC-8 (overlays array is []).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock


def test_emit_helper_is_importable():
    """AC-5: the helper must exist on websocket_session_handler.

    Pre-test for the more involved wiring checks. If the function is
    missing the import fails fast with a clear message.
    """
    from sidequest.server.websocket_session_handler import (
        _maybe_emit_location_description,
    )

    assert callable(_maybe_emit_location_description)


def test_emit_skips_when_no_room_id():
    """AC-5 graceful absence: no current room → no emit, no error."""
    from sidequest.server.websocket_session_handler import (
        _maybe_emit_location_description,
    )

    emit_fn = MagicMock()
    sd = MagicMock()
    sd.genre_slug = "caverns_and_claudes"
    sd.world_slug = "caverns_sunden"
    sd.player_id = ""
    # Empty character_locations means no actor has a room yet.
    snapshot = MagicMock()
    snapshot.character_locations = {}

    _maybe_emit_location_description(
        MagicMock(),
        sd=sd,
        snapshot=snapshot,
        actor="alice",
        emit_fn=emit_fn,
    )
    emit_fn.assert_not_called()


def _seed_synthetic_world(tmp_path: Path) -> Path:
    """Build a minimal genre-pack/world dir with one settlement room
    carrying a real entities: block. Returns the genre-pack root so the
    GenreLoader.find monkeypatch can return it.
    """
    genre_root = tmp_path / "test_pack"
    world_dir = genre_root / "worlds" / "test_world"
    rooms = world_dir / "rooms"
    rooms.mkdir(parents=True)
    (rooms / "test_room.yaml").write_text(
        "name: Test Square\n"
        "room_type: settlement\n"
        "description: A well at the centre, lit by a cobwebbed lantern.\n"
        "entities:\n"
        "  - id: square_well\n"
        "    label: the well at the centre\n"
        "    tier: real_object\n"
        "    binding:\n"
        "      kind: location_feature\n"
        "      ref: test_square_well\n"
        "    affordances:\n"
        "      - draw_water\n"
        "  - id: cobwebbed_lantern\n"
        "    label: a cobwebbed lantern\n"
        "    tier: flavor_only\n"
    )
    return genre_root


def _patch_genre_loader_find(monkeypatch, genre_root: Path):
    """Patch GenreLoader.find so the helper resolves world_dir to our tmp tree."""
    from sidequest.genre import loader as loader_mod

    def _fake_find(self, slug):  # noqa: ARG001
        return genre_root

    monkeypatch.setattr(loader_mod.GenreLoader, "find", _fake_find)


def test_emit_sends_message_when_room_has_manifest(tmp_path, monkeypatch):
    """AC-5 + AC-6 production path: room with entities → LocationDescriptionMessage.

    Validates the full transit: GenreLoader.find → load_room_payload →
    typed manifest → LocationDescriptionPayload → LocationDescriptionMessage
    → emit_fn. Uses tmp_path-built content because the live worlds either
    don't use room_graph navigation (beneath_sunden is procedural per
    ADR-106) or don't carry static room YAMLs yet.
    """
    from sidequest.protocol.enums import MessageType
    from sidequest.protocol.messages import LocationDescriptionMessage
    from sidequest.server.websocket_session_handler import (
        _maybe_emit_location_description,
    )

    genre_root = _seed_synthetic_world(tmp_path)
    _patch_genre_loader_find(monkeypatch, genre_root)

    emit_fn = MagicMock()
    sd = MagicMock()
    sd.genre_slug = "test_pack"
    sd.world_slug = "test_world"
    sd.player_id = ""
    sd.genre_pack = MagicMock()
    sd.genre_pack.worlds = {"test_world": MagicMock()}
    snapshot = MagicMock()
    snapshot.character_locations = {"alice": "test_room"}

    _maybe_emit_location_description(
        MagicMock(),
        sd=sd,
        snapshot=snapshot,
        actor="alice",
        emit_fn=emit_fn,
    )

    emit_fn.assert_called_once()
    call_args = emit_fn.call_args
    sent_msg = call_args.args[0] if call_args.args else call_args.kwargs.get("msg")
    sent_type = (
        call_args.args[1]
        if len(call_args.args) > 1
        else call_args.kwargs.get("type") or call_args.kwargs.get("msg_type")
    )
    assert sent_type == "LOCATION_DESCRIPTION"
    assert isinstance(sent_msg, LocationDescriptionMessage)
    assert sent_msg.type == MessageType.LOCATION_DESCRIPTION
    assert sent_msg.payload.region_id == "test_room"
    assert len(sent_msg.payload.entities) == 2
    by_id = {e.id: e for e in sent_msg.payload.entities}
    assert by_id["square_well"].tier == "real_object"
    assert by_id["square_well"].binding is not None
    assert by_id["square_well"].binding.kind == "location_feature"
    assert by_id["cobwebbed_lantern"].tier == "flavor_only"
    # AC-8: overlays empty until Story 54-7.
    assert sent_msg.payload.overlays == []


def test_emit_room_id_override_takes_precedence(tmp_path, monkeypatch):
    """AC-5: room_id_override path used by session-resume bypasses actor lookup."""
    from sidequest.server.websocket_session_handler import (
        _maybe_emit_location_description,
    )

    genre_root = _seed_synthetic_world(tmp_path)
    _patch_genre_loader_find(monkeypatch, genre_root)

    emit_fn = MagicMock()
    sd = MagicMock()
    sd.genre_slug = "test_pack"
    sd.world_slug = "test_world"
    sd.player_id = ""
    sd.genre_pack = MagicMock()
    sd.genre_pack.worlds = {"test_world": MagicMock()}
    snapshot = MagicMock()
    # No character_locations — override is what wins.
    snapshot.character_locations = {}

    _maybe_emit_location_description(
        MagicMock(),
        sd=sd,
        snapshot=snapshot,
        actor=None,
        emit_fn=emit_fn,
        room_id_override="test_room",
    )

    emit_fn.assert_called_once()


def test_emit_uses_cartography_fallback_when_no_room_yaml(tmp_path, monkeypatch):
    """AC-5 cartography-fallback path: per-room YAML missing → cartography region wins.

    POI worlds (e.g. ``tea_and_murder/glenross`` post-54-4) carry their
    manifest on ``world.cartography.regions[room_id]``. The helper's
    second source path must consume that when ``load_room_payload``
    raises ``RoomNotFoundError``. If this branch silently breaks, 54-4
    content will emit nothing.
    """
    from sidequest.genre.models.world import Region
    from sidequest.protocol.messages import LocationDescriptionMessage
    from sidequest.protocol.models import LocationEntity
    from sidequest.server.websocket_session_handler import (
        _maybe_emit_location_description,
    )

    # Make sure the world dir lookup succeeds (returns tmp_path) so the
    # helper proceeds to load_room_payload (which will raise) rather
    # than bailing on world_dir_lookup_failed.
    _patch_genre_loader_find(monkeypatch, tmp_path)

    # Force load_room_payload to raise RoomNotFoundError — the explicit
    # signal that no per-room YAML exists for this room. The cartography
    # fallback must take over.
    from sidequest.game import room_file_loader as rfl_mod

    def _raise_not_found(*_args, **_kwargs):
        raise rfl_mod.RoomNotFoundError("no room yaml in test")

    monkeypatch.setattr(rfl_mod, "load_room_payload", _raise_not_found)

    region = Region(
        name="The Glenross Pub",
        summary="A quiet country pub.",
        description="A low-beamed taproom with a fire in the grate.",
        terrain="building",
        entities=[
            LocationEntity(
                id="hearth",
                label="the hearth",
                tier="real_object",
                binding={"kind": "location_feature", "ref": "pub_hearth"},
            ),
            LocationEntity(
                id="ticking_clock",
                label="a ticking long-case clock",
                tier="flavor_only",
            ),
        ],
    )

    world = MagicMock()
    world.cartography = MagicMock()
    world.cartography.regions = {"glenross_pub": region}

    emit_fn = MagicMock()
    sd = MagicMock()
    sd.genre_slug = "tea_and_murder"
    sd.world_slug = "glenross"
    sd.player_id = ""
    sd.genre_pack = MagicMock()
    sd.genre_pack.worlds = {"glenross": world}
    snapshot = MagicMock()
    snapshot.character_locations = {"alice": "glenross_pub"}

    _maybe_emit_location_description(
        MagicMock(),
        sd=sd,
        snapshot=snapshot,
        actor="alice",
        emit_fn=emit_fn,
    )

    emit_fn.assert_called_once()
    call_args = emit_fn.call_args
    sent_msg = call_args.args[0] if call_args.args else call_args.kwargs.get("msg")
    assert isinstance(sent_msg, LocationDescriptionMessage)
    assert sent_msg.payload.region_id == "glenross_pub"
    assert sent_msg.payload.prose == "A low-beamed taproom with a fire in the grate."
    assert sent_msg.payload.terrain == "building"
    by_id = {e.id: e for e in sent_msg.payload.entities}
    assert set(by_id.keys()) == {"hearth", "ticking_clock"}
    assert by_id["hearth"].tier == "real_object"
    assert by_id["ticking_clock"].tier == "flavor_only"


def test_emit_fires_no_source_when_neither_path_resolves(tmp_path, monkeypatch):
    """AC-5 no_source watcher: both paths empty → emit_fn NOT called, watcher event fires.

    Per the OTEL Observability Principle: the GM panel needs to see
    when the lie detector is firing — silent skip would mask a real
    content gap. Mirrors the watcher contract documented on the helper.
    """
    from sidequest.server import websocket_session_handler as wsh
    from sidequest.server.websocket_session_handler import (
        _maybe_emit_location_description,
    )

    _patch_genre_loader_find(monkeypatch, tmp_path)

    from sidequest.game import room_file_loader as rfl_mod

    def _raise_not_found(*_args, **_kwargs):
        raise rfl_mod.RoomNotFoundError("no room yaml in test")

    monkeypatch.setattr(rfl_mod, "load_room_payload", _raise_not_found)

    watcher_calls: list[tuple[str, dict]] = []

    def _capture_watcher(event_name, fields, **_kwargs):
        watcher_calls.append((event_name, fields))

    monkeypatch.setattr(wsh, "_watcher_publish", _capture_watcher)

    # World present but no cartography → second source path also empty.
    world = MagicMock()
    world.cartography = None

    emit_fn = MagicMock()
    sd = MagicMock()
    sd.genre_slug = "tea_and_murder"
    sd.world_slug = "glenross"
    sd.player_id = ""
    sd.genre_pack = MagicMock()
    sd.genre_pack.worlds = {"glenross": world}
    snapshot = MagicMock()
    snapshot.character_locations = {"alice": "missing_room"}

    _maybe_emit_location_description(
        MagicMock(),
        sd=sd,
        snapshot=snapshot,
        actor="alice",
        emit_fn=emit_fn,
    )

    emit_fn.assert_not_called()
    event_names = [name for name, _ in watcher_calls]
    assert "location_description.no_source" in event_names, (
        f"expected location_description.no_source watcher event; got {event_names}"
    )
    no_source_fields = next(
        fields for name, fields in watcher_calls if name == "location_description.no_source"
    )
    assert no_source_fields["room_id"] == "missing_room"
    assert no_source_fields["genre"] == "tea_and_murder"
    assert no_source_fields["world"] == "glenross"


def test_emit_called_from_room_change_dispatch():
    """AC-6 wiring test — proves _maybe_emit_location_description has a non-test caller.

    Per CLAUDE.md 'Verify wiring, not just existence': the function must
    actually be invoked from production dispatch code, not just defined.
    """
    here = Path(__file__).resolve()
    repo = here.parents[3]
    handler_path = (
        repo / "sidequest-server" / "sidequest" / "server" / "websocket_session_handler.py"
    )
    handler_src = handler_path.read_text()
    assert "def _maybe_emit_location_description(" in handler_src, (
        "definition missing — Dev hasn't added the helper yet"
    )
    # Definition + at least one production call site.
    call_count = handler_src.count("_maybe_emit_location_description(")
    assert call_count >= 2, (
        "expected definition + at least one production call site; "
        f"found {call_count} mentions in websocket_session_handler.py "
        "(definition = 1 occurrence). Per CLAUDE.md every test suite "
        "needs a wiring test."
    )


def test_emit_called_at_session_resume_path():
    """AC-5: session-resume call site exists with room_id_override.

    Distinguishes the resume call from the room-change call — they pass
    different actor/override args, but both must be present per AC-5.
    The regex pins ``room_id_override=`` to a
    ``_maybe_emit_location_description(...)`` call site so the test
    fails if the 54-2 resume call is removed even though the
    tactical-grid resume call (which also uses ``room_id_override=``)
    is unchanged.
    """
    import re

    here = Path(__file__).resolve()
    repo = here.parents[3]
    handler_path = (
        repo / "sidequest-server" / "sidequest" / "server" / "websocket_session_handler.py"
    )
    handler_src = handler_path.read_text()
    assert "_maybe_emit_location_description(" in handler_src
    pattern = re.compile(
        r"_maybe_emit_location_description\([^)]*room_id_override=",
        re.DOTALL,
    )
    assert pattern.search(handler_src), (
        "session-resume call site for _maybe_emit_location_description must "
        "pass room_id_override=; see plan Task 5 Step 6"
    )


def test_chargen_region_mode_call_site_exists():
    """Playtest 2026-05-20 — LocationPanel tab never appeared on
    beneath_sunden because the chargen emit branch was gated on
    ``navigation_mode == room_graph``. Region-mode worlds
    (beneath_sunden surface, glenross, coyote_star) never reached
    ``_maybe_emit_location_description`` at chargen so the UI's
    ``if (currentLocation)`` data gate stayed false forever — no tab.

    Wiring test: the chargen flow must contain a region-mode branch
    that fires the emit with ``room_id_override=...current_region``.
    """
    import re

    here = Path(__file__).resolve()
    repo = here.parents[3]
    handler_path = (
        repo / "sidequest-server" / "sidequest" / "server" / "websocket_session_handler.py"
    )
    handler_src = handler_path.read_text()
    # Find every region-mode chargen guard and verify at least one
    # has an emit call within the following ~40 lines (the chargen
    # branch is bounded; an emit far outside is a different branch).
    pattern = re.compile(
        r"NavigationMode\.region(?:.*?\n){1,40}?_maybe_emit_location_description\(\s*self,\s*"
        r"sd=sd,\s*snapshot=sd\.snapshot,\s*actor=None,\s*"
        r"emit_fn=[^,]+,\s*room_id_override=sd\.snapshot\.current_region",
        re.DOTALL,
    )
    assert pattern.search(handler_src), (
        "chargen region-mode call site for _maybe_emit_location_description "
        "is missing — region-mode worlds (beneath_sunden, glenross, "
        "coyote_star) will not get an opening LOCATION_DESCRIPTION so the "
        "Location tab will never appear in the UI"
    )


def test_per_turn_region_change_call_site_exists():
    """Playtest 2026-05-20 — region-mode worlds need a per-turn
    LOCATION_DESCRIPTION emit too. The existing per-turn emit fires
    on ``if result.location:`` which is character-level (room_graph
    territory). Region-mode region moves arrive via the
    ``current_region`` patch — without a parallel emit the Location
    tab freezes on turn-1 prose forever (e.g. ropefoot →
    the_dropmouth would not update).

    Wiring test: the per-turn dispatch must contain a region-mode
    branch that fires the emit when ``snapshot.current_region`` !=
    the captured ``prior_current_region``.
    """
    import re

    here = Path(__file__).resolve()
    repo = here.parents[3]
    handler_path = (
        repo / "sidequest-server" / "sidequest" / "server" / "websocket_session_handler.py"
    )
    handler_src = handler_path.read_text()
    assert "prior_current_region" in handler_src, (
        "per-turn region-change detection requires capturing the "
        "current_region BEFORE the narration patch applies — the "
        "prior_current_region snapshot is missing"
    )
    pattern = re.compile(
        r"current_region\s*!=\s*prior_current_region.*?"
        r"_maybe_emit_location_description\(",
        re.DOTALL,
    )
    assert pattern.search(handler_src), (
        "per-turn region-change call site for _maybe_emit_location_description "
        "is missing — region-mode worlds will not refresh the Location tab "
        "when the party moves between cartography regions"
    )


def test_emit_includes_active_overlay_in_payload(tmp_path, monkeypatch):
    """Story 54-7: when an encounter with location_overlay is live and
    bound to the actor's room, the emitted LocationDescriptionPayload
    carries the overlay summary, and payload.prose includes the suffix.

    Without this, a session-resume client sees stale base prose during
    an active overlay until the next LOCATION_OVERLAY_CHANGED delta.
    """
    from sidequest.game.encounter import (
        EncounterMetric,
        StructuredEncounter,
    )
    from sidequest.protocol.messages import LocationDescriptionMessage
    from sidequest.protocol.models import (
        EncounterLocationOverlay,
        LocationEntity,
    )
    from sidequest.server.websocket_session_handler import (
        _maybe_emit_location_description,
    )

    genre_root = _seed_synthetic_world(tmp_path)
    _patch_genre_loader_find(monkeypatch, genre_root)

    enc = StructuredEncounter(
        encounter_type="tavern_brawl",
        player_metric=EncounterMetric(name="composure", current=10, starting=10, threshold=20),
        opponent_metric=EncounterMetric(name="brawl_energy", current=10, starting=10, threshold=20),
        resolved=False,
        location_overlay=EncounterLocationOverlay(
            bound_room_id="test_room",
            entity_delta=[
                LocationEntity(
                    id="overturned_cart",
                    label="an overturned cart",
                    tier="yes_and",
                ),
            ],
            prose_suffix="Smoke drifts from the alley.",
        ),
    )

    emit_fn = MagicMock()
    sd = MagicMock()
    sd.genre_slug = "test_pack"
    sd.world_slug = "test_world"
    sd.player_id = ""
    sd.genre_pack = MagicMock()
    sd.genre_pack.worlds = {"test_world": MagicMock()}
    snapshot = MagicMock()
    snapshot.character_locations = {"alice": "test_room"}
    snapshot.encounter = enc

    _maybe_emit_location_description(
        MagicMock(),
        sd=sd,
        snapshot=snapshot,
        actor="alice",
        emit_fn=emit_fn,
    )

    emit_fn.assert_called_once()
    sent_msg = emit_fn.call_args.args[0]
    sent_type = emit_fn.call_args.args[1]
    assert sent_type == "LOCATION_DESCRIPTION"
    assert isinstance(sent_msg, LocationDescriptionMessage)
    assert len(sent_msg.payload.overlays) == 1
    overlay_summary = sent_msg.payload.overlays[0]
    assert overlay_summary.prose_suffix == "Smoke drifts from the alley."
    assert overlay_summary.entity_delta_count == 1
    assert "Smoke drifts" in sent_msg.payload.prose
