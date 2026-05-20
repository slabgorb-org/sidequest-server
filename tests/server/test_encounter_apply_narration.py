from __future__ import annotations

from pathlib import Path

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.agents.orchestrator import BeatSelection, NarrationTurnResult, NpcMention
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.game.session import GameSnapshot
from sidequest.genre.loader import load_genre_pack
from sidequest.protocol.dice import RollOutcome
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from tests._helpers.session_room import room_for

# Load directly from the fixture pack on disk, bypassing GenreLoader's
# session-wide cache. The session cache keys on slug only, so another test
# that loads ``caverns_and_claudes`` from ``sidequest-content/`` (e.g.
# test_opening_turn_bootstrap.py) poisons the cache with the real content
# pack — which is missing the beats these tests need. load_genre_pack() is
# the cache-free path.
_FIXTURE_PACK = Path(__file__).resolve().parents[1] / "fixtures" / "packs" / "test_genre"


def _load_pack(_genre: str):
    # ``_genre`` is intentionally ignored — every fixture slug symlinks to
    # the same ``test_genre`` directory, and we skip the cache to avoid
    # cross-test pack poisoning. Callers still pass a slug for readability.
    return load_genre_pack(_FIXTURE_PACK)


@pytest.fixture
def cac_snap():
    snap = GameSnapshot(genre="caverns_and_claudes")
    pack = _load_pack("caverns_and_claudes")
    return snap, pack


@pytest.fixture
def otel_capture():
    """Attach an in-memory exporter to the running TracerProvider.

    Mirrors the otel_capture fixture in test_room_graph_init.py — adds a
    SimpleSpanProcessor alongside the existing processors so span emissions
    from production code fan out to the in-memory sink for assertion.
    """
    from sidequest.telemetry.setup import init_tracer

    init_tracer()  # idempotent
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider), (
        f"expected SDK TracerProvider, got {type(provider)!r}"
    )
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


def test_narrator_confrontation_trigger_creates_encounter(cac_snap) -> None:
    from sidequest.server.session_handler import _apply_narration_result_to_snapshot

    snap, pack = cac_snap
    # Story 45-33: combat now requires an opponent post-fallback. This test's
    # focus is "narrator-emitted confrontation creates an encounter," so
    # supplying an explicit opponent preserves the original intent.
    result = NarrationTurnResult(
        narration="Goblins leap from the shadows.",
        confrontation="combat",
        npcs_present=[NpcMention(name="Goblin pack", role="hostile", is_new=True)],
    )
    _apply_narration_result_to_snapshot(
        snap,
        result,
        player_name="Rux",
        pack=pack,
        room=room_for(snap),
    )
    assert snap.encounter is not None
    assert snap.encounter.encounter_type == "combat"


# ---------------------------------------------------------------------------
# Lie-detector: confrontation-trigger with empty npcs_present
# (pingpong 2026-04-24 — "Confrontation panel has no enemy combatants")
# ---------------------------------------------------------------------------


def test_confrontation_trigger_with_empty_npcs_present_fires_empty_actor_list_span(
    cac_snap, otel_capture: InMemorySpanExporter
) -> None:
    """Narrator emits combat confrontation but no npcs_present and no NPCs at
    the player's location → both lie-detector spans fire and the encounter
    is NOT created.

    Story 45-33 changed this contract: the wrapper still emits the
    ``encounter.empty_actor_list`` span (lie-detector for the narrator-side
    extraction gap), the lifecycle then emits ``encounter.no_opponent_available``
    and raises (CLAUDE.md No Silent Fallbacks — combat with no opponent is a
    refused state), and the wrapper catches the ValueError so the turn stays
    resilient (CLAUDE.md "strict helper, lenient caller"). Net result: the
    GM panel sees both signals and the snapshot is left without a fabricated
    solo encounter.
    """
    from sidequest.server.session_handler import _apply_narration_result_to_snapshot

    snap, pack = cac_snap
    snap.genre_slug = "caverns_and_claudes"
    result = NarrationTurnResult(
        narration="Goblins leap from the shadows.",
        confrontation="combat",
        npcs_present=[],  # narrator named goblins in prose but omitted them here
    )
    _apply_narration_result_to_snapshot(
        snap,
        result,
        player_name="Rux",
        pack=pack,
        room=room_for(snap),
    )

    # Story 45-33: encounter must NOT be created when combat resolves to
    # zero opponents post-fallback.
    assert snap.encounter is None

    # Wrapper still emits the narrator-extraction lie-detector.
    spans_by_name = {s.name: s for s in otel_capture.get_finished_spans()}
    assert "encounter.empty_actor_list" in spans_by_name, (
        f"expected encounter.empty_actor_list span; got {sorted(spans_by_name)}"
    )
    eal = spans_by_name["encounter.empty_actor_list"]
    assert eal.attributes["encounter_type"] == "combat"
    assert eal.attributes["player_name"] == "Rux"
    assert eal.attributes["genre_slug"] == "caverns_and_claudes"

    # Story 45-33: the lifecycle-layer guard also fires its own span.
    assert "encounter.no_opponent_available" in spans_by_name, (
        f"expected encounter.no_opponent_available span (Story 45-33 guard); "
        f"got {sorted(spans_by_name)}"
    )
    noa = spans_by_name["encounter.no_opponent_available"]
    assert noa.attributes["encounter_type"] == "combat"
    assert noa.attributes["player_name"] == "Rux"
    assert noa.attributes["category"] == "combat"


def test_confrontation_trigger_with_populated_npcs_present_does_not_fire_span(
    cac_snap, otel_capture: InMemorySpanExporter
) -> None:
    """Healthy case — when npcs_present carries adversaries, the
    lie-detector stays quiet. Asserts the span is scoped to the extraction
    failure, not every confrontation.
    """
    from sidequest.server.session_handler import _apply_narration_result_to_snapshot

    snap, pack = cac_snap
    snap.genre_slug = "caverns_and_claudes"
    result = NarrationTurnResult(
        narration="Goblins leap from the shadows.",
        confrontation="combat",
        npcs_present=[NpcMention(name="Goblin pack", role="hostile", is_new=True)],
    )
    _apply_narration_result_to_snapshot(
        snap,
        result,
        player_name="Rux",
        pack=pack,
        room=room_for(snap),
    )
    span_names = {s.name for s in otel_capture.get_finished_spans()}
    assert "encounter.empty_actor_list" not in span_names


# ---------------------------------------------------------------------------
# Lie-detector: prose described a confrontation trigger but
# confrontation=None (pingpong 2026-05-03 [BUG] — narrator wrote
# "patrol cutter spinning her reactor up from cold-soak" with
# confrontation=None; no encounter fired)
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    "legacy scanner deleted in Task 9; replaced by intent-validator dispatch "
    "(spec 2026-05-20 confrontation-intent-validator step 5). "
    "The skipped_with_trigger_keywords watcher event no longer fires; "
    "confrontation.intent_mismatch span takes over. Task 9 deletes this test."
)
def test_confrontation_trigger_keywords_with_no_confrontation_fires_lie_detector(
    cac_snap, monkeypatch
) -> None:
    """Narrator prose contains a high-precision confrontation trigger
    (e.g. ``spinning her reactor up``) but ``confrontation`` is None —
    the lie-detector watcher event must fire so the GM panel can see
    the prose-vs-mechanics divergence.
    """
    captured: list[tuple[str, dict, dict]] = []

    def fake_publish(event_type, fields, *, component="", severity="info"):
        captured.append((event_type, fields, {"component": component, "severity": severity}))

    import sidequest.server.narration_apply as _napply

    monkeypatch.setattr(_napply, "_watcher_publish", fake_publish)

    snap, pack = cac_snap
    snap.genre_slug = "caverns_and_claudes"
    # Turn 20 from the playtest, paraphrased — the chase-firing prose.
    result = NarrationTurnResult(
        narration=(
            "At nine seconds into climb the proximity scope blooms a new "
            "ignition signature: Far Landing's standing-alert patrol "
            "cutter, spinning her reactor up from cold-soak. She isn't "
            "moving yet. She's asking the tower whether to."
        ),
        confrontation=None,
        npcs_present=[],
    )
    _apply_narration_result_to_snapshot(
        snap,
        result,
        player_name="Itchy",
        pack=pack,
        room=room_for(snap),
    )

    skipped = [
        (fields, meta)
        for et, fields, meta in captured
        if et == "state_transition" and fields.get("op") == "skipped_with_trigger_keywords"
    ]
    assert skipped, (
        "lie-detector must publish "
        "confrontation.skipped_with_trigger_keywords for GM panel visibility"
    )
    fields, meta = skipped[0]
    assert "reactor_spin_up" in fields["matched_keywords"]
    assert fields["player_name"] == "Itchy"
    assert meta["component"] == "confrontation"
    assert meta["severity"] == "warning"
    # No encounter instantiated — the architectural commitment is
    # narrator-emission, NOT server-side auto-firing on prose
    # inference. The warning is observability only.
    assert snap.encounter is None


def test_confrontation_present_skips_lie_detector_scan(cac_snap, monkeypatch) -> None:
    """Healthy case — narrator emitted ``confrontation`` correctly,
    so the keyword scan must not fire even if prose contains trigger
    phrases. The detector is scoped to the prose-vs-mechanics gap, not
    every encounter.
    """
    captured: list[tuple[str, dict, dict]] = []

    def fake_publish(event_type, fields, *, component="", severity="info"):
        captured.append((event_type, fields, {"component": component, "severity": severity}))

    import sidequest.server.narration_apply as _napply

    monkeypatch.setattr(_napply, "_watcher_publish", fake_publish)

    snap, pack = cac_snap
    snap.genre_slug = "caverns_and_claudes"
    result = NarrationTurnResult(
        narration="The patrol cutter draws weapons hot and opens fire.",
        confrontation="combat",
        npcs_present=[NpcMention(name="Patrol cutter", role="hostile", is_new=True)],
    )
    _apply_narration_result_to_snapshot(
        snap,
        result,
        player_name="Itchy",
        pack=pack,
        room=room_for(snap),
    )

    skipped = [
        (et, fields)
        for et, fields, _ in captured
        if et == "state_transition" and fields.get("op") == "skipped_with_trigger_keywords"
    ]
    assert not skipped, "lie-detector must stay quiet when confrontation is populated"


def test_active_encounter_skips_lie_detector_scan(cac_snap, monkeypatch) -> None:
    """When a confrontation is already active and the narrator stays in
    beat-resolution mode, prose may legitimately contain trigger keywords
    (e.g. mid-chase narration). The lie-detector is scoped to "encounter
    SHOULD have fired but didn't" — once an encounter is live, the
    narrator routes through ``beat_selections`` and the trigger
    constraint no longer applies.
    """
    captured: list[tuple[str, dict, dict]] = []

    def fake_publish(event_type, fields, *, component="", severity="info"):
        captured.append((event_type, fields, {"component": component, "severity": severity}))

    import sidequest.server.narration_apply as _napply

    monkeypatch.setattr(_napply, "_watcher_publish", fake_publish)

    snap, pack = cac_snap
    snap.genre_slug = "caverns_and_claudes"
    # Pre-seed an active encounter — simulates mid-chase state.
    snap.encounter = StructuredEncounter(
        encounter_type="combat",
        actors=[
            EncounterActor(name="Itchy", role="ally", side="player"),
            EncounterActor(name="Cutter", role="hostile", side="opponent"),
        ],
        player_metric=EncounterMetric(name="player_edge", threshold=10),
        opponent_metric=EncounterMetric(name="opponent_edge", threshold=10),
        resolved=False,
    )
    result = NarrationTurnResult(
        narration="The cutter spins her reactor up and opens fire on a pursuit vector.",
        confrontation=None,  # narrator correctly skips emission for an active encounter
        npcs_present=[],
    )
    _apply_narration_result_to_snapshot(
        snap,
        result,
        player_name="Itchy",
        pack=pack,
        room=room_for(snap),
    )

    skipped = [
        (et, fields)
        for et, fields, _ in captured
        if et == "state_transition" and fields.get("op") == "skipped_with_trigger_keywords"
    ]
    assert not skipped, (
        "lie-detector must not fire when an encounter is already active — "
        "trigger-keyword prose mid-encounter is expected, not a regression"
    )


def test_no_trigger_keywords_quiet_narration_no_warning(cac_snap, monkeypatch) -> None:
    """Quiet narration with no trigger keywords → no lie-detector
    fires. Guards against the patterns over-matching.
    """
    captured: list[tuple[str, dict, dict]] = []

    def fake_publish(event_type, fields, *, component="", severity="info"):
        captured.append((event_type, fields, {"component": component, "severity": severity}))

    import sidequest.server.narration_apply as _napply

    monkeypatch.setattr(_napply, "_watcher_publish", fake_publish)

    snap, pack = cac_snap
    snap.genre_slug = "caverns_and_claudes"
    result = NarrationTurnResult(
        narration=(
            "Itchy sits at the small steel table in the galley, datapad "
            "face-down, a tin of plateau-grown tea cooling at his elbow. "
            "He sleeps for ninety minutes the way smugglers sleep."
        ),
        confrontation=None,
        npcs_present=[],
    )
    _apply_narration_result_to_snapshot(
        snap,
        result,
        player_name="Itchy",
        pack=pack,
        room=room_for(snap),
    )

    skipped = [
        (et, fields)
        for et, fields, _ in captured
        if et == "state_transition" and fields.get("op") == "skipped_with_trigger_keywords"
    ]
    assert not skipped, "quiet narration must not trip the lie-detector"


def test_scan_helper_pattern_coverage() -> None:
    """Unit test the keyword scanner against every label-pattern pair so
    a regression in the regex set surfaces here, not in an integration
    test where the failure would be obscure.
    """
    from sidequest.server.narration_apply import (
        _scan_for_confrontation_trigger_keywords,
    )

    cases: list[tuple[str, str]] = [
        ("reactor_spin_up", "the cutter spinning her reactor up from cold-soak"),
        ("intercept", "an intercept order from Vaskov tower"),
        ("pursuit", "a pursuit vector toward the gas giant"),
        ("boarding", "a boarding action from the side hatch"),
        ("weapons_hot", "weapons hot, weapons drawn"),
        ("permission_to_engage", "permission to engage on Hegemonic frequency"),
        ("chase_keyword", "they begin chasing the freighter"),
        ("opens_fire", "the cutter opens fire on the climb"),
        ("weapon_drawn", "the lieutenant draws her sidearm"),
        ("weapon_leveled", "he levels a rifle at the doorway"),
    ]
    for expected_label, narration in cases:
        matched = _scan_for_confrontation_trigger_keywords(narration)
        assert expected_label in matched, (
            f"expected {expected_label!r} to match {narration!r}; got {matched!r}"
        )

    # Negative case — quiet prose stays empty
    assert (
        _scan_for_confrontation_trigger_keywords("Itchy keeps the datapad in his coat. He waits.")
        == []
    )
    # Empty narration is safe
    assert _scan_for_confrontation_trigger_keywords("") == []


# ---------------------------------------------------------------------------
# MP bundled-turn confrontation seats every seated PC
# (pingpong 2026-05-03 [BUG] — confrontation widget missing in-fiction
# principal in MP)
# ---------------------------------------------------------------------------


def test_mp_bundle_confrontation_seats_all_player_seats(cac_snap) -> None:
    """Bundled MP turn → narrator confrontation → every seated PC in actors.

    Repro of pingpong 2026-05-03 [BUG]: Itchy + Scratchy both seated and
    acting; Scratchy's frame fires the barrier (so player_name=Scratchy),
    narrator initiates a negotiation. The widget showed only Scratchy as the
    player-side actor; Itchy was missing even though the negotiation was his.

    Fix: ``_apply_narration_result_to_snapshot`` now reads
    ``snapshot.player_seats.values()`` and threads the non-submitter PC names
    into ``instantiate_encounter_from_trigger`` as
    ``additional_player_names``. This test guards the wiring at the apply
    seam — encounter_lifecycle's own tests guard the constructor.
    """
    from sidequest.server.session_handler import _apply_narration_result_to_snapshot

    snap, pack = cac_snap
    snap.player_seats = {"player_1": "Itchy", "player_2": "Scratchy"}
    result = NarrationTurnResult(
        narration="Inspector Volkova fans the manifest on her desk.",
        confrontation="combat",
        npcs_present=[NpcMention(name="Inspector Volkova", role="hostile", is_new=True)],
    )
    _apply_narration_result_to_snapshot(
        snap,
        result,
        player_name="Scratchy",  # the action submitter for the barrier-firing frame
        pack=pack,
        room=room_for(snap),
    )
    assert snap.encounter is not None
    pc_names = {a.name for a in snap.encounter.actors if a.side == "player"}
    assert pc_names == {"Itchy", "Scratchy"}, (
        f"both seated PCs must appear as side=player actors; got {pc_names}"
    )


def test_solo_confrontation_unchanged_when_player_seats_empty(cac_snap) -> None:
    """Solo / pre-MP saves keep single-PC actor list (additional list empty)."""
    from sidequest.server.session_handler import _apply_narration_result_to_snapshot

    snap, pack = cac_snap
    snap.player_seats = {}  # solo / pre-MP shape
    result = NarrationTurnResult(
        narration="Goblins leap from the shadows.",
        confrontation="combat",
        npcs_present=[NpcMention(name="Goblin pack", role="hostile", is_new=True)],
    )
    _apply_narration_result_to_snapshot(
        snap,
        result,
        player_name="Rux",
        pack=pack,
        room=room_for(snap),
    )
    assert snap.encounter is not None
    pc_names = [a.name for a in snap.encounter.actors if a.side == "player"]
    assert pc_names == ["Rux"]


# ---------------------------------------------------------------------------
# Narrator-granted items land on character inventory
# (pingpong 2026-04-24 — "items_gained=1 on Warden defeat — brass memory
# core never appears in Inventory")
# ---------------------------------------------------------------------------


@pytest.fixture
def cac_snap_with_character(cac_snap):
    """CAC snapshot with a minimal Character so inventory tests have a
    target for narrator-granted items. Mirrors the shape used by
    test_perception_rewriter_wiring._make_character."""
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory

    snap, pack = cac_snap
    core = CreatureCore(
        name="Slabgorb",
        description="A scavenger.",
        personality="Cautious.",
        inventory=Inventory(),
        statuses=[],
    )
    character = Character(
        core=core,
        char_class="Ranger",
        race="Human",
        backstory="A wanderer.",
    )
    snap.characters.append(character)
    return snap, pack, character


def test_items_gained_lands_on_character_inventory(
    cac_snap_with_character,
) -> None:
    """Narrator-granted items must reach ``character.core.inventory.items``.

    Regression for pingpong 2026-04-24 "items_gained=1 on Warden defeat —
    brass memory core never appears in Inventory". The orchestrator
    extracted ``items_gained`` but the server only used it for a
    watcher-patch summary — the item never landed on inventory, so the
    UI panel stayed out of sync with the narrative.
    """
    from sidequest.server.session_handler import _apply_narration_result_to_snapshot

    snap, pack, character = cac_snap_with_character
    assert len(character.core.inventory.items) == 0
    result = NarrationTurnResult(
        narration="In its chest cavity, a brass-cased memory core still blinks.",
        items_gained=[
            {
                "name": "Brass Memory Core",
                "description": "A scavenged data spindle; it hums when handled.",
                "category": "quest",
            },
        ],
    )
    _apply_narration_result_to_snapshot(
        snap,
        result,
        player_name="Slabgorb",
        pack=pack,
        room=room_for(snap),
    )
    assert len(character.core.inventory.items) == 1
    item = character.core.inventory.items[0]
    assert item["name"] == "Brass Memory Core"
    assert item["category"] == "quest"
    assert item["description"].startswith("A scavenged")
    assert item["state"] == "Carried"
    assert item["equipped"] is False
    assert item["quantity"] == 1
    assert item["id"] == "narrator:brass_memory_core"


def test_items_gained_normalizes_unknown_category_to_misc(
    cac_snap_with_character,
) -> None:
    """Narrator-declared categories outside the allowed set normalize to
    ``misc`` rather than flow through unchecked — guards against the UI
    filter (``InventoryPanel``) receiving a category it can't render.
    """
    from sidequest.server.session_handler import _apply_narration_result_to_snapshot

    snap, pack, character = cac_snap_with_character
    result = NarrationTurnResult(
        narration="You pocket the oddity.",
        items_gained=[
            {"name": "Strange Trinket", "description": "Odd.", "category": "junk"},
        ],
    )
    _apply_narration_result_to_snapshot(
        snap,
        result,
        player_name="Slabgorb",
        pack=pack,
        room=room_for(snap),
    )
    assert character.core.inventory.items[0]["category"] == "misc"


def test_items_lost_removes_first_matching_name_case_insensitive(
    cac_snap_with_character,
) -> None:
    """Narrator-declared ``items_lost`` removes the first matching item
    from inventory by name (case-insensitive). Missing entries are
    silently skipped (narrator may hallucinate a lost item that wasn't
    actually in inventory).
    """
    from sidequest.server.session_handler import _apply_narration_result_to_snapshot

    snap, pack, character = cac_snap_with_character
    character.core.inventory.items.append(
        {
            "id": "narrator:rusty_compass",
            "name": "Rusty Compass",
            "description": "A corroded compass.",
            "category": "tool",
            "value": 0,
            "weight": 0.1,
            "rarity": "common",
            "narrative_weight": 0.3,
            "tags": [],
            "equipped": False,
            "quantity": 1,
            "uses_remaining": None,
            "state": "Carried",
        }
    )
    assert len(character.core.inventory.items) == 1

    result = NarrationTurnResult(
        narration="The merchant palms your compass.",
        items_lost=[
            {"name": "RUSTY COMPASS", "description": "lost", "category": "tool"},
        ],
    )
    _apply_narration_result_to_snapshot(
        snap,
        result,
        player_name="Slabgorb",
        pack=pack,
        room=room_for(snap),
    )
    assert character.core.inventory.items == []


def test_items_discarded_transitions_state_out_of_carried(
    cac_snap_with_character,
    otel_capture: InMemorySpanExporter,
) -> None:
    """Story 45-14: narrator-extracted ``items_discarded`` flips a Carried
    item's state to Discarded without removing it. Regression for
    Playtest 3 Blutka turn 9: narrator wrote "abandons the spear where it
    stands — shaft quivering in scavenger meat" but the spear still showed
    state=Carried in the final inventory because the discard verb had no
    plumbing.

    Asserts:
    - Item remains in inventory (recoverable narratively).
    - Item state transitions out of "Carried" to "Discarded".
    - Item is no longer equipped.
    - OTEL span fires with the discarded name in the JSON attribute.
    """
    snap, pack, character = cac_snap_with_character
    character.core.inventory.items.append(
        {
            "id": "narrator:bone_spear",
            "name": "Bone Spear",
            "description": "A scavenger's barbed shaft.",
            "category": "weapon",
            "value": 0,
            "weight": 2.0,
            "rarity": "common",
            "narrative_weight": 0.5,
            "tags": [],
            "equipped": True,
            "quantity": 1,
            "uses_remaining": None,
            "state": "Carried",
        }
    )

    result = NarrationTurnResult(
        narration=(
            "Blutka abandons the spear where it stands — shaft quivering in scavenger meat."
        ),
        items_discarded=[
            {"name": "Bone Spear", "category": "weapon"},
        ],
    )
    _apply_narration_result_to_snapshot(
        snap,
        result,
        player_name="Blutka",
        pack=pack,
        room=room_for(snap),
    )

    # Item is still in inventory (discard ≠ removal — narratively
    # recoverable) but state has transitioned out of Carried.
    assert len(character.core.inventory.items) == 1
    item = character.core.inventory.items[0]
    assert item["name"] == "Bone Spear"
    assert item["state"] == "Discarded"
    assert item["equipped"] is False

    # OTEL lie-detector span fired with the discarded name.
    spans_by_name = {s.name: s for s in otel_capture.get_finished_spans()}
    assert "inventory.narrator_extracted" in spans_by_name, (
        f"expected inventory.narrator_extracted span; got {list(spans_by_name)}"
    )
    span = spans_by_name["inventory.narrator_extracted"]
    assert span.attributes["discarded_count"] == 1
    assert span.attributes["discarded_json"] == '["bone spear"]'
    assert span.attributes["unmatched_discards_count"] == 0


def test_items_discarded_unmatched_logs_and_emits_count(
    cac_snap_with_character,
    otel_capture: InMemorySpanExporter,
) -> None:
    """No-silent-fallback: when narrator hallucinates a discard for an
    item not in inventory, the span surfaces the miss via
    ``unmatched_discards_count`` so the GM panel can spot the drift.
    """
    snap, pack, character = cac_snap_with_character
    assert character.core.inventory.items == []

    result = NarrationTurnResult(
        narration="You drop the imaginary lantern.",
        items_discarded=[{"name": "Phantom Lantern"}],
    )
    _apply_narration_result_to_snapshot(
        snap,
        result,
        player_name="Slabgorb",
        pack=pack,
        room=room_for(snap),
    )

    spans_by_name = {s.name: s for s in otel_capture.get_finished_spans()}
    span = spans_by_name["inventory.narrator_extracted"]
    assert span.attributes["discarded_count"] == 0
    assert span.attributes["unmatched_discards_count"] == 1


def test_items_consumed_removes_item_and_emits_otel_span(
    cac_snap_with_character,
    otel_capture: InMemorySpanExporter,
) -> None:
    """Story 45-15: narrator-extracted ``items_consumed`` removes the
    used-up consumable from inventory and fires the OTEL lie-detector
    span with the consumed name.

    Regression for Playtest 3 Felix: ``maintenance_kit.state=Consumed``
    after patch-foam use (rounds 14-16) and foil-strip tear (round 48),
    but the kit remained in inventory at quantity=1 because the consume
    verb had no apply seam. Fix: consume lane removes outright (no
    ``state=Consumed`` is ever written; AC1 satisfied by never setting
    the state without removal).

    Asserts:
    - Item is removed from inventory (no item left at state=Consumed).
    - OTEL span fires with the consumed name in ``consumed_json``.
    - ``consumed_count=1``, ``unmatched_consumes_count=0``.
    """
    snap, pack, character = cac_snap_with_character
    character.core.inventory.items.append(
        {
            "id": "narrator:maintenance_kit",
            "name": "Maintenance Kit",
            "description": "A scuffed tin of patch-foam and foil strips.",
            "category": "consumable",
            "value": 0,
            "weight": 0.5,
            "rarity": "common",
            "narrative_weight": 0.4,
            "tags": [],
            "equipped": False,
            "quantity": 1,
            "uses_remaining": None,
            "state": "Carried",
        }
    )

    result = NarrationTurnResult(
        narration=("Felix sprays the last of the patch-foam over the gash; the kit sputters dry."),
        items_consumed=[
            {"name": "Maintenance Kit", "category": "consumable"},
        ],
    )
    _apply_narration_result_to_snapshot(
        snap,
        result,
        player_name="Felix",
        pack=pack,
        room=room_for(snap),
    )

    # AC1: no item remains at state=Consumed (we don't set Consumed —
    # we just drop the item). Inventory is empty after consumption.
    assert character.core.inventory.items == []

    # AC2: OTEL span fires with the consumed name.
    spans_by_name = {s.name: s for s in otel_capture.get_finished_spans()}
    assert "inventory.narrator_extracted" in spans_by_name, (
        f"expected inventory.narrator_extracted span; got {list(spans_by_name)}"
    )
    span = spans_by_name["inventory.narrator_extracted"]
    assert span.attributes["consumed_count"] == 1
    assert span.attributes["consumed_json"] == '["maintenance kit"]'
    assert span.attributes["unmatched_consumes_count"] == 0
    assert span.attributes["player_name"] == "Felix"


def test_items_consumed_unmatched_logs_and_emits_count(
    cac_snap_with_character,
    otel_capture: InMemorySpanExporter,
) -> None:
    """No-silent-fallback (CLAUDE.md): when narrator hallucinates a
    consume for an item not in inventory, the span surfaces the miss via
    ``unmatched_consumes_count`` so the GM panel can spot the drift —
    rather than silently dropping the consume.
    """
    snap, pack, character = cac_snap_with_character
    assert character.core.inventory.items == []

    result = NarrationTurnResult(
        narration="You drink the imaginary potion.",
        items_consumed=[{"name": "Phantom Potion"}],
    )
    _apply_narration_result_to_snapshot(
        snap,
        result,
        player_name="Slabgorb",
        pack=pack,
        room=room_for(snap),
    )

    spans_by_name = {s.name: s for s in otel_capture.get_finished_spans()}
    span = spans_by_name["inventory.narrator_extracted"]
    assert span.attributes["consumed_count"] == 0
    assert span.attributes["unmatched_consumes_count"] == 1


def test_items_update_tolerates_missing_characters(cac_snap) -> None:
    """Called with no character seated (e.g., pre-chargen narration), the
    inventory block must no-op without raising. Single-player saves
    populate ``snapshot.characters[0]`` before any narration turn so this
    is a safety net, not a hot path.
    """
    from sidequest.server.session_handler import _apply_narration_result_to_snapshot

    snap, pack = cac_snap
    assert snap.characters == []
    result = NarrationTurnResult(
        narration="Something shifts in the dust.",
        items_gained=[{"name": "Phantom Item", "description": "nil", "category": "misc"}],
    )
    # Must not raise.
    _apply_narration_result_to_snapshot(
        snap,
        result,
        player_name="Slabgorb",
        pack=pack,
        room=room_for(snap),
    )


# ---------------------------------------------------------------------------
# Dual-track momentum — new tests (Task 11)
# ---------------------------------------------------------------------------


def _two_dial_enc():
    return StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        actors=[
            EncounterActor(name="Sam", role="combatant", side="player"),
            EncounterActor(name="Promo", role="combatant", side="opponent"),
        ],
    )


def test_narrator_player_strike_advances_player_metric(snapshot_with_pack):
    """Player-side strike applies through the explicit-action path.

    Post Playtest 2026-04-26 [S2-BUG] (SOUL "The Test" gate), PC beats
    must come from a DICE_THROW frame. ``from_explicit_action=True``
    here simulates the dispatch_dice_throw call site that has already
    validated the player's explicit consent on their own socket. The
    test still proves player-side beat application math (strike base=2
    routes to own metric).
    """
    snap, pack = snapshot_with_pack
    snap.encounter = _two_dial_enc()
    result = NarrationTurnResult(
        narration="Sam swings.",
        beat_selections=[BeatSelection(actor="Sam", beat_id="attack", outcome=RollOutcome.Success)],
        npcs_present=[NpcMention(name="Promo", side="opponent", role="hostile")],
    )
    _apply_narration_result_to_snapshot(
        snap,
        result,
        "Sam",
        pack=pack,
        from_explicit_action=True,
        room=room_for(snap),
    )
    assert snap.encounter.player_metric.current == 2
    assert snap.encounter.opponent_metric.current == 0


def test_narrator_player_strike_blocked_without_explicit_action(snapshot_with_pack):
    """Wiring lock for SOUL "The Test" gate (Playtest 2026-04-26 [S2-BUG]).

    Without ``from_explicit_action=True``, the same player-side beat is
    rejected — the production session_handler path NEVER sets that flag,
    so PC beats inferred from narrator extraction can't move the dial.
    Without this assertion the gate could regress silently.
    """
    snap, pack = snapshot_with_pack
    snap.encounter = _two_dial_enc()
    result = NarrationTurnResult(
        narration="Sam swings.",
        beat_selections=[BeatSelection(actor="Sam", beat_id="attack", outcome=RollOutcome.Success)],
        npcs_present=[NpcMention(name="Promo", side="opponent", role="hostile")],
    )
    _apply_narration_result_to_snapshot(snap, result, "Sam", pack=pack, room=room_for(snap))
    assert snap.encounter.player_metric.current == 0
    assert snap.encounter.opponent_metric.current == 0


def test_narrator_opponent_strike_advances_opponent_metric(snapshot_with_pack):
    snap, pack = snapshot_with_pack
    snap.encounter = _two_dial_enc()
    result = NarrationTurnResult(
        narration="Promo lunges.",
        beat_selections=[
            BeatSelection(actor="Promo", beat_id="attack", outcome=RollOutcome.Success)
        ],
        npcs_present=[NpcMention(name="Promo", side="opponent", role="hostile")],
    )
    _apply_narration_result_to_snapshot(snap, result, "Sam", pack=pack, room=room_for(snap))
    assert snap.encounter.opponent_metric.current == 2
    assert snap.encounter.player_metric.current == 0


def test_unknown_actor_in_beat_selection_raises(snapshot_with_pack):
    snap, pack = snapshot_with_pack
    snap.encounter = _two_dial_enc()
    result = NarrationTurnResult(
        narration="Ghost swings.",
        beat_selections=[
            BeatSelection(actor="Ghost", beat_id="attack", outcome=RollOutcome.Success)
        ],
    )
    with pytest.raises(ValueError, match="unknown actor"):
        _apply_narration_result_to_snapshot(snap, result, "Sam", pack=pack, room=room_for(snap))


def test_apply_encounter_updates_no_longer_exported():
    import sidequest.server.narration_apply as mod

    assert not hasattr(mod, "apply_encounter_updates")
