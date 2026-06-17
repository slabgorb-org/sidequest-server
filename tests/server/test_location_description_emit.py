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
    # BUG-LOW (2026-06-02 playtest): header must show the authored display
    # name, not the snake_case room id. Room-YAML path sources it from the
    # room's ``name`` (TacticalGridPayload.room_name).
    assert sent_msg.payload.region_name == "Test Square"
    # Location-tab POI landscape (2026-06-04): built from the region_id VERBATIM
    # (no slugify) so the URL matches the underscore R2 object key. The UI
    # hides it on a 404, so emitting it unconditionally is safe.
    assert sent_msg.payload.poi_image_url is not None
    assert (
        "genre_packs/test_pack/worlds/test_world/assets/poi/test_room.png"
        in sent_msg.payload.poi_image_url
    )
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
    # BUG-LOW (2026-06-02 playtest): region-mode header shows the authored
    # ``Region.name`` ("The Glenross Pub"), not the slug ("glenross_pub").
    assert sent_msg.payload.region_name == "The Glenross Pub"
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
    from sidequest.server.websocket_handlers import (
        map_emit as wsh,  # patches _watcher_publish where the helper now binds it
    )
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


# ---------------------------------------------------------------------------
# Story 63-6: LocationPanel region-header reference deep-link.
#
# The region header in LocationPanel becomes a hyperlink into the
# /reference/lore wiki when the region has a lore-page anchor. The
# authoritative source of "which regions have a lore anchor" is the world's
# history.yaml points_of_interest[].slug set (Story 63-8) — the same manifest
# the lore page's geography presenter uses to emit `location-{slug}` card ids.
# These are RED tests for that wiring; production code does not exist yet.
# ---------------------------------------------------------------------------


def test_location_description_payload_accepts_reference_url():
    """AC2: LocationDescriptionPayload carries an optional reference_url.

    The model uses extra=forbid, so this raises until the field is added —
    a clean RED. The field is the region-header deep-link URL.
    """
    from sidequest.protocol.models import LocationDescriptionPayload

    p = LocationDescriptionPayload(
        region_id="test_room",
        prose="x",
        reference_url="/reference/lore/test_pack/test_world#location-test-room",
    )
    assert p.reference_url == "/reference/lore/test_pack/test_world#location-test-room"


def test_location_description_payload_reference_url_defaults_none():
    """AC1 graceful default: omitting reference_url yields None, so old
    snapshots and region-mode worlds without lore anchors round-trip cleanly."""
    from sidequest.protocol.models import LocationDescriptionPayload

    p = LocationDescriptionPayload(region_id="test_room", prose="x")
    assert p.reference_url is None


def test_emit_populates_reference_url_when_region_is_a_known_poi(tmp_path, monkeypatch):
    """AC1 wiring (positive): a region whose slug is in the world's
    history.yaml points_of_interest gets a region-header lore URL on the
    emitted payload.

    POI slugs are the authoritative source of which locations have a
    /reference/lore anchor (Story 63-8 `_load_poi_image_slugs`). This keeps
    the dev honest: the handler must actually resolve the region against that
    manifest and set the field — not leave it defaulted to None.
    """
    from sidequest.protocol.messages import LocationDescriptionMessage
    from sidequest.server.websocket_session_handler import (
        _maybe_emit_location_description,
    )

    genre_root = _seed_synthetic_world(tmp_path)
    world_dir = genre_root / "worlds" / "test_world"
    # The region "test_room" is a point of interest with a lore page anchor.
    (world_dir / "history.yaml").write_text(
        "points_of_interest:\n  - slug: test_room\n    name: Test Square\n"
    )
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
    sent_msg = emit_fn.call_args.args[0]
    assert isinstance(sent_msg, LocationDescriptionMessage)
    assert sent_msg.payload.reference_url == (
        "/reference/lore/test_pack/test_world#location-test-room"
    )


def test_emit_reference_url_is_none_when_region_has_no_poi_anchor(tmp_path, monkeypatch):
    """AC1 wiring (graceful None / no silent fallback): a region with no
    matching POI slug emits reference_url=None — never a guessed or broken URL.

    The synthetic world has no history.yaml, so there are no lore anchors.
    """
    from sidequest.protocol.messages import LocationDescriptionMessage
    from sidequest.server.websocket_session_handler import (
        _maybe_emit_location_description,
    )

    genre_root = _seed_synthetic_world(tmp_path)  # no history.yaml -> no POIs
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
    sent_msg = emit_fn.call_args.args[0]
    assert isinstance(sent_msg, LocationDescriptionMessage)
    assert sent_msg.payload.reference_url is None
