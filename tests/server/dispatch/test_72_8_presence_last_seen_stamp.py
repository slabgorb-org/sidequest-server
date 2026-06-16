"""Story 72-8 — stamp ``last_seen_turn`` / ``last_seen_location`` on encounter
PRESENCE, not just on the narrator's prose name-drop.

Before this story ``Npc.last_seen_*`` was refreshed only by the prose-mention
path (``narration_apply._apply_npc_mentions`` → ``npcs_hit``). An NPC physically
seated as an encounter opponent — its HP/dial mutated round over round — went
un-stamped whenever the narrator didn't ALSO name it in that turn's
``npcs_present`` prose. The engine then treated a combatant the party had been
fighting for rounds as "not recently seen", which 72-6's LRU/last-seen prune
would read as stale and mis-evict while the NPC was still on the board.

This story closes the gap at the two encounter-presence seams in
``encounter_lifecycle``:

* ``_seed_combat_hp_depletion_to_npcs`` — the hp_depletion opponent seam.
* ``_publish_combat_edge_to_npcs`` — the legacy dial-threshold opponent seam.

Both now stamp recency (mirroring the prose path's write discipline:
``last_seen_turn`` always advances, ``last_seen_location`` only when a location
resolved — No Silent Fallbacks) and surface the stamped values on the existing
``npc.edge_published`` OTEL span so the GM-panel lie-detector can confirm
presence-stamping fired.

AC2 ("prose-mention stamping still fires — no regression") is guarded for the
prose path's TRUTHY-location branch by the untouched prose-path coverage in
``tests/server/test_npc_pool_narration_apply.py::test_cite_known_npc_updates_last_seen_on_npc``
— that test sets ``character_locations`` so a location resolves, so it covers the
location-overwrite branch only; it does NOT exercise the prose path's no-location
branch. The *presence* path's own no-location branch (the symmetric "advance the
turn, never stamp a bogus location" case this story adds) is covered locally by
``test_presence_no_resolved_location_stamps_turn_not_location`` below. The
"present AND prose-mentioned same turn ⇒ one consistent stamp" edge case is
exercised below by driving BOTH paths against the same NPC on the same turn.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.agents.orchestrator import NpcMention
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.encounter import EncounterActor, EncounterMetric
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager
from sidequest.genre.loader import load_genre_pack
from sidequest.server.dispatch.encounter_lifecycle import (
    _publish_combat_edge_to_npcs,
    _seed_combat_hp_depletion_to_npcs,
)
from sidequest.server.narration_apply import _apply_npc_mentions
from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path
from tests._helpers.trigger_encounter import trigger_encounter

_FIXTURE_PACK = Path(__file__).resolve().parents[2] / "fixtures" / "packs" / "test_genre"


def _make_npc(name: str, *, location: str | None = None, turn: int = 0) -> Npc:
    return Npc(
        core=CreatureCore(
            name=name,
            description="An NPC.",
            personality="Neutral.",
            level=1,
            xp=0,
            inventory=Inventory(),
            statuses=[],
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        npc_role_id="hostile",
        last_seen_location=location,
        last_seen_turn=turn,
    )


@pytest.fixture
def otel_capture():
    from sidequest.telemetry.setup import init_tracer

    init_tracer()
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


# ---------------------------------------------------------------------------
# AC1 — presence stamps even WITHOUT a prose mention (production wiring path)
# ---------------------------------------------------------------------------


def test_dial_presence_stamps_last_seen_without_prose_mention() -> None:
    """An NPC seated as an opponent via the location fallback (``npcs_present=[]``
    — i.e. NOT named in prose this turn) gets ``last_seen_turn`` advanced to the
    encounter turn and ``last_seen_location`` set to the acting PC's location.

    This drives the production handshake (``instantiate_encounter_from_trigger``
    → ``_publish_combat_edge_to_npcs``), so it doubles as the wiring guard.
    """
    pack = load_genre_pack(_FIXTURE_PACK)
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=4),
    )
    snap.character_locations["Orin"] = "Mawdeep Caverns"
    # Seated by the location fallback (same room as Orin); stale recency from turn 3.
    npc = _make_npc("Crawling Scavenger", location="Mawdeep Caverns", turn=3)
    snap.npcs.append(npc)

    trigger_encounter(snap, pack, "combat", "Orin", npcs_present=[])

    assert npc.last_seen_turn == 4, (
        "presence at the encounter seam must advance last_seen_turn to the "
        f"encounter turn even with no prose mention; got {npc.last_seen_turn}"
    )
    assert npc.last_seen_location == "Mawdeep Caverns"


def test_dial_presence_stamp_rides_npc_edge_published_span(otel_capture) -> None:
    """The stamped recency is surfaced as attributes on the existing
    ``npc.edge_published`` span (GM-panel lie-detector), not a new span family.
    """
    pack = load_genre_pack(_FIXTURE_PACK)
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=4),
    )
    snap.character_locations["Orin"] = "Mawdeep Caverns"
    snap.npcs.append(_make_npc("Crawling Scavenger", location="Mawdeep Caverns", turn=3))

    trigger_encounter(snap, pack, "combat", "Orin", npcs_present=[])

    edge_spans = [s for s in otel_capture.get_finished_spans() if s.name == "npc.edge_published"]
    assert edge_spans, (
        "npc.edge_published span never fired; "
        f"finished={[s.name for s in otel_capture.get_finished_spans()]!r}"
    )
    attrs = dict(edge_spans[0].attributes or {})
    assert attrs.get("last_seen_turn") == 4, (
        f"span missing/incorrect last_seen_turn presence stamp; attrs={sorted(attrs)!r}"
    )
    assert attrs.get("last_seen_location") == "Mawdeep Caverns", (
        f"span missing/incorrect last_seen_location presence stamp; attrs={sorted(attrs)!r}"
    )


def test_hp_depletion_seam_stamps_presence() -> None:
    """The hp_depletion opponent seam (``_seed_combat_hp_depletion_to_npcs``)
    stamps recency on the opponent NPC it seeds — the seam the story names first.
    """
    snap = GameSnapshot(
        genre_slug="space_opera",
        world_slug="coyote_star",
        turn_manager=TurnManager(interaction=6),
    )
    snap.character_locations["Vesh"] = "Docking Ring"
    npc = _make_npc("Hegemony Marine", location="Docking Ring", turn=2)
    snap.npcs.append(npc)

    cdef = types.SimpleNamespace(opponent_hp=8, opponent_armor_class=12)
    actors = [
        EncounterActor(name="Vesh", role="protagonist", side="player"),
        EncounterActor(name="Hegemony Marine", role="adversary", side="opponent"),
    ]

    _seed_combat_hp_depletion_to_npcs(
        snapshot=snap,
        actors=actors,
        cdef=cdef,
        turn=6,
        source="encounter_handshake",
        acting_character_name="Vesh",
    )

    assert npc.last_seen_turn == 6
    assert npc.last_seen_location == "Docking Ring"


# ---------------------------------------------------------------------------
# Edge case — no resolved location: stamp the turn, never a bogus location
# ---------------------------------------------------------------------------


def test_presence_no_resolved_location_stamps_turn_not_location() -> None:
    """When ``party_location`` returns ``None`` (the acting PC has no resolved
    location), the presence stamp advances ``last_seen_turn`` but must NOT
    overwrite ``last_seen_location`` with a bogus/empty value — it stays frozen
    at whatever it last was (mirrors the prose path; No Silent Fallbacks).
    """
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=9),
    )
    # No character_locations entry for "Ghost" → party_location(perspective) is None.
    npc = _make_npc("Wraith", location="Old Crypt", turn=2)
    snap.npcs.append(npc)

    _publish_combat_edge_to_npcs(
        snapshot=snap,
        actors=[
            EncounterActor(name="Ghost", role="protagonist", side="player"),
            EncounterActor(name="Wraith", role="adversary", side="opponent"),
        ],
        opponent_metric=EncounterMetric(name="opponent", threshold=10, current=0),
        turn=9,
        source="encounter_handshake",
        acting_character_name="Ghost",
    )

    assert npc.last_seen_turn == 9, "turn must still advance when location is unresolved"
    assert npc.last_seen_location == "Old Crypt", (
        "no resolved location must NOT clobber the prior last_seen_location with "
        f"a bogus value; got {npc.last_seen_location!r}"
    )


# ---------------------------------------------------------------------------
# Edge case — NPC leaves the encounter: recency freezes, does not keep advancing
# ---------------------------------------------------------------------------


def test_npc_leaving_encounter_freezes_last_seen() -> None:
    """Once an NPC is no longer a seated opponent, the presence seam stops
    stamping it — its ``last_seen_turn`` stays frozen at the last turn it was
    actually present (so 72-6's staleness measure works)."""
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=4),
    )
    snap.character_locations["Orin"] = "Mawdeep Caverns"
    npc = _make_npc("Goblin", location="Mawdeep Caverns", turn=1)
    snap.npcs.append(npc)
    metric = EncounterMetric(name="opponent", threshold=10, current=0)

    # Turn 4: present as opponent → stamped.
    _publish_combat_edge_to_npcs(
        snapshot=snap,
        actors=[
            EncounterActor(name="Orin", role="protagonist", side="player"),
            EncounterActor(name="Goblin", role="adversary", side="opponent"),
        ],
        opponent_metric=metric,
        turn=4,
        source="encounter_handshake",
        acting_character_name="Orin",
    )
    assert npc.last_seen_turn == 4

    # Turn 7: the Goblin is gone — only a different opponent is seated.
    snap.npcs.append(_make_npc("Kobold", location="Mawdeep Caverns", turn=0))
    _publish_combat_edge_to_npcs(
        snapshot=snap,
        actors=[
            EncounterActor(name="Orin", role="protagonist", side="player"),
            EncounterActor(name="Kobold", role="adversary", side="opponent"),
        ],
        opponent_metric=metric,
        turn=7,
        source="encounter_handshake",
        acting_character_name="Orin",
    )
    assert npc.last_seen_turn == 4, (
        "an NPC absent from the opponent roster must NOT keep advancing its "
        f"last_seen_turn; got {npc.last_seen_turn} (should be frozen at 4)"
    )


# ---------------------------------------------------------------------------
# Edge case — present AND prose-mentioned same turn ⇒ one consistent stamp
# ---------------------------------------------------------------------------


def test_present_and_prose_mentioned_same_turn_one_consistent_stamp() -> None:
    """When an NPC is BOTH seated in the encounter and named in prose the same
    turn, both write paths use the same turn number and the same resolved
    location — the final state is one consistent stamp, never two conflicting
    values."""
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=5),
    )
    snap.character_locations["Orin"] = "Mawdeep Caverns"
    npc = _make_npc("Crawling Scavenger", location="Old Tunnel", turn=2)
    snap.npcs.append(npc)

    # Presence path (encounter seam).
    _publish_combat_edge_to_npcs(
        snapshot=snap,
        actors=[
            EncounterActor(name="Orin", role="protagonist", side="player"),
            EncounterActor(name="Crawling Scavenger", role="adversary", side="opponent"),
        ],
        opponent_metric=EncounterMetric(name="opponent", threshold=10, current=0),
        turn=5,
        source="encounter_handshake",
        acting_character_name="Orin",
    )
    # Prose path (narrator names the same NPC the same turn).
    _apply_npc_mentions(
        snapshot=snap,
        mentions=[NpcMention(name="Crawling Scavenger")],
        turn_num=5,
        acting_character_name="Orin",
    )

    assert npc.last_seen_turn == 5
    assert npc.last_seen_location == "Mawdeep Caverns", (
        "both paths resolve the acting PC's location identically; final stamp "
        f"must be consistent, got {npc.last_seen_location!r}"
    )


# ---------------------------------------------------------------------------
# AC1/AC2 — hp_depletion PRODUCTION-PATH wiring test (real space_opera pack)
# ---------------------------------------------------------------------------
#
# ``test_hp_depletion_seam_stamps_presence`` (above) calls
# ``_seed_combat_hp_depletion_to_npcs`` DIRECTLY with a ``types.SimpleNamespace``
# cdef — it bypasses ``instantiate_encounter_from_trigger`` and the real
# ``ConfrontationDef`` accessors, so it proves the seam stamps but NOT that the
# hp_depletion dispatch gate (``encounter_lifecycle`` win_condition branch ~1216,
# threading ``acting_character_name=player_name``) actually reaches it. These
# tests close that wiring gap ("Every Test Suite Needs a Wiring Test"): they
# drive the authored ``space_opera`` combat (``win_condition: hp_depletion``)
# end-to-end through ``trigger_encounter`` → ``instantiate_encounter_from_trigger``
# and assert the opponent ``Npc`` is presence-stamped through production —
# INCLUDING the CREATE branch where the opponent has no backing ``Npc`` before
# instantiation. Loading the real pack here is the sanctioned exception to "tests
# don't point at content" (same rationale as
# ``tests/server/test_space_opera_swn_combat_e2e.py``): the point is proving the
# real seating + dispatch path, not a synthetic fixture.

# Full SWN-flavor stat block (mirrors test_space_opera_swn_combat_e2e) so the
# instantiation seam can roll 1d8+DEX initiative without KeyError after seeding.
_SWN_STATS = {
    "Physique": 12,
    "Reflex": 12,
    "Will": 10,
    "Intellect": 12,
    "Resolve": 12,
    "Cunning": 12,
}


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _space_opera_pack():
    try:
        return load_genre_pack(find_pack_path("space_opera"))
    except PackNotFound:
        pytest.skip("sidequest-content not on disk in this checkout")


def _spacer(name: str) -> Character:
    """A player Character with a backing core + SWN stats so the instantiation
    seam can resolve DEX for initiative (the hp_depletion gate runs
    ``_roll_and_persist_initiative`` after seeding the opponent)."""
    return Character(
        core=CreatureCore(
            name=name,
            description="Station-side spacer.",
            personality="steady",
            inventory=Inventory(),
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        char_class="Soldier",
        race="Coreworlder",
        backstory="Ex-Hegemonic infantry.",
        stats=dict(_SWN_STATS),
    )


def _opponent_npc(snap: GameSnapshot, name: str) -> Npc | None:
    return next((n for n in snap.npcs if n.core.name == name), None)


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_hp_depletion_presence_stamp_production_path_create_branch() -> None:
    """AC1+AC2 (CREATE branch): driving a real ``space_opera`` hp_depletion combat
    through ``instantiate_encounter_from_trigger`` presence-stamps the opponent
    ``Npc`` the seam CREATES (the opponent had no backing ``Npc`` before
    instantiation). This is the production wiring guard the direct-seam unit test
    above cannot provide."""
    pack = _space_opera_pack()
    snap = GameSnapshot(
        genre_slug="space_opera",
        world_slug="coyote_star",
        turn_manager=TurnManager(interaction=6),
    )
    snap.character_locations["Vesh"] = "Docking Ring"
    snap.characters.append(_spacer("Vesh"))
    # CREATE branch precondition: no backing Npc for this opponent yet.
    assert _opponent_npc(snap, "Corsair") is None

    trigger_encounter(
        snap,
        pack,
        "combat",
        "Vesh",
        npcs_present=[NpcMention(name="Corsair", side="opponent")],
    )

    opponent = _opponent_npc(snap, "Corsair")
    assert opponent is not None, (
        "the hp_depletion seam must CREATE a backing Npc for a router-named "
        "opponent (create branch) when none exists"
    )
    assert opponent.last_seen_turn == 6, (
        "presence stamp must advance last_seen_turn to the encounter turn through "
        f"the production dispatch gate; got {opponent.last_seen_turn}"
    )
    assert opponent.last_seen_location == "Docking Ring", (
        "presence stamp must set last_seen_location to the acting PC's resolved "
        f"location; got {opponent.last_seen_location!r}"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_hp_depletion_presence_stamp_production_path_existing_branch() -> None:
    """AC1 (OVERWRITE branch): the same production path refreshes a PRE-EXISTING
    opponent ``Npc`` with stale recency in place (no duplicate) when it is seated
    through ``instantiate_encounter_from_trigger``."""
    pack = _space_opera_pack()
    snap = GameSnapshot(
        genre_slug="space_opera",
        world_slug="coyote_star",
        turn_manager=TurnManager(interaction=8),
    )
    snap.character_locations["Vesh"] = "Docking Ring"
    snap.characters.append(_spacer("Vesh"))
    stale = _make_npc("Corsair", location="Old Hold", turn=2)
    snap.npcs.append(stale)

    trigger_encounter(
        snap,
        pack,
        "combat",
        "Vesh",
        npcs_present=[NpcMention(name="Corsair", side="opponent")],
    )

    corsairs = [n for n in snap.npcs if n.core.name == "Corsair"]
    assert len(corsairs) == 1, (
        f"opponent must be matched + refreshed in place, not duplicated; got {len(corsairs)}"
    )
    assert stale.last_seen_turn == 8, (
        "the production path must advance the existing opponent's last_seen_turn "
        f"to the encounter turn; got {stale.last_seen_turn}"
    )
    assert stale.last_seen_location == "Docking Ring", (
        "the production path must refresh the existing opponent's stale location; "
        f"got {stale.last_seen_location!r}"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_hp_depletion_production_path_stamp_rides_npc_edge_published_span(otel_capture) -> None:
    """The hp_depletion production path surfaces the presence stamp on the existing
    ``npc.edge_published`` span (GM-panel lie-detector), tagged ``created=True`` for
    the freshly-materialized opponent — the hp_depletion analog of the dial-path
    span proof above."""
    pack = _space_opera_pack()
    snap = GameSnapshot(
        genre_slug="space_opera",
        world_slug="coyote_star",
        turn_manager=TurnManager(interaction=6),
    )
    snap.character_locations["Vesh"] = "Docking Ring"
    snap.characters.append(_spacer("Vesh"))

    trigger_encounter(
        snap,
        pack,
        "combat",
        "Vesh",
        npcs_present=[NpcMention(name="Corsair", side="opponent")],
    )

    edge_spans = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "npc.edge_published" and (s.attributes or {}).get("npc_name") == "Corsair"
    ]
    assert edge_spans, (
        "npc.edge_published span never fired for the seeded opponent; "
        f"finished={[s.name for s in otel_capture.get_finished_spans()]!r}"
    )
    attrs = dict(edge_spans[0].attributes or {})
    assert attrs.get("created") is True, (
        f"the materialized opponent must be tagged created=True; attrs={sorted(attrs)!r}"
    )
    assert attrs.get("last_seen_turn") == 6, (
        f"span missing/incorrect last_seen_turn presence stamp; attrs={sorted(attrs)!r}"
    )
    assert attrs.get("last_seen_location") == "Docking Ring", (
        f"span missing/incorrect last_seen_location presence stamp; attrs={sorted(attrs)!r}"
    )
