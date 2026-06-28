"""Story 158-49 (RED) — a seated SWN sealed-letter dogfight must hand the player a
ruleset-valid beat set, never the Without-Number personal-combat pool, and committing
the offered beat must not crash the SWN resolver / soft-lock the confrontation.

These drive the REAL surfaces (not ``beats_available_for`` in isolation):

* ``build_confrontation_payload`` — the surface that emits the CONFRONTATION beat menu
  to the client (AC2 + AC5/OTEL).
* ``dispatch_dice_throw`` — the production DICE_THROW commit seam that crashed in the
  2026-06-27 coyote_star playtest (AC1 + AC4).

The ``swn_test_pack`` fixture's pilot/opponent stat blocks are FLAVOR-keyed
(Physique/Reflex/Intellect — NO STR/DEX); the fixture was deliberately left
flavor-keyed when #510 canonicalized live content (158-51 deferred note). That makes
the original ``KeyError stat 'STR'`` crash reproduce deterministically here: the
synthesized WN ``attack`` beat carries ``stat_check='STR'`` and
``without_number._stat`` fails loud on a flavor-keyed block. The structural bug —
a sealed-letter dogfight being handed the personal-combat menu at all — is the same on
live (canonical) content; the crash is just its visible symptom on a flavor-keyed pack.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.agents.orchestrator import NpcMention
from sidequest.game.beat_filter import beats_available_for
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    EncounterPhase,
    StructuredEncounter,
)
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.character import ClassDef
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import ConfrontationDef
from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
from sidequest.server.dispatch.confrontation import build_confrontation_payload
from sidequest.server.dispatch.dice import dispatch_dice_throw
from tests._helpers.fixture_packs import SWN_TEST_PACK, TEST_WORLD, load_fixture_pack
from tests._helpers.trigger_encounter import trigger_encounter

_WN_PERSONAL_COMBAT_IDS = {"attack", "total_defense", "fighting_withdrawal", "run"}

PILOT = "Moe"
OPPONENT = "Freighter Ace"
# Flavor-keyed SWN stat block (NO STR/DEX) — exactly the swn_test_pack shape that
# makes the WN ``attack`` (stat_check STR) commit crash without_number._stat.
_FLAVOR_STATS = {"Physique": 10, "Reflex": 10, "Intellect": 10, "Cunning": 10, "Resolve": 10}


@pytest.fixture(scope="module")
def swn_pack() -> GenrePack:
    return load_fixture_pack(SWN_TEST_PACK)


@pytest.fixture
def otel_capture():
    """Attach an in-memory span exporter to the running TracerProvider."""
    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


def _dogfight_cdef(pack: GenrePack) -> ConfrontationDef:
    cdef = next(
        (c for c in pack.rules.confrontations if c.confrontation_type == "dogfight"),
        None,
    )
    assert cdef is not None, "fixture must author a 'dogfight' confrontation"
    return cdef


def _pilot_class() -> ClassDef:
    """Non-caster pilot whose encounter_beat_choices enumerate no WN action id."""
    return ClassDef(
        id="pilot",
        display_name="Pilot",
        rpg_role="tank",
        jungian_default="warrior",
        prime_requisite="STR",
        minimum_score=9,
        kit_table="pilot_kit",
        flavor="-",
        encounter_beat_choices=["sprint"],
    )


def _make_flavor_pilot(name: str) -> Character:
    return Character(
        core=CreatureCore(
            name=name,
            description="A solo pilot.",
            personality="wry",
            hp={"current": 8, "max": 8, "base_max": 8},
        ),
        backstory="A pilot.",
        char_class="Pilot",
        race="Human",
        stats=dict(_FLAVOR_STATS),
    )


def _dogfight_encounter() -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="dogfight",
        player_metric=EncounterMetric(name="m", current=0, starting=0, threshold=8),
        opponent_metric=EncounterMetric(name="m", current=0, starting=0, threshold=8),
        structured_phase=EncounterPhase.Setup,
        actors=[
            EncounterActor(name=PILOT, role="combatant", side="player"),
            EncounterActor(name=OPPONENT, role="combatant", side="opponent"),
        ],
    )


def _core() -> SimpleNamespace:
    return SimpleNamespace(spellcasting=None, inventory=SimpleNamespace(items=[]))


# ---------------------------------------------------------------------------
# AC2 — the player-facing menu surface excludes the WN personal-combat pool
# ---------------------------------------------------------------------------


def test_dogfight_payload_excludes_wn_personal_combat_menu(swn_pack: GenrePack) -> None:
    """RED (AC2): the CONFRONTATION payload the client renders for a seated SWN
    dogfight must NOT carry the WN personal-combat action ids. Drives the real
    ``build_confrontation_payload`` surface (not the filter in isolation)."""
    payload = build_confrontation_payload(
        encounter=_dogfight_encounter(),
        cdef=_dogfight_cdef(swn_pack),
        genre_slug=SWN_TEST_PACK,
        recipient_pc=(_pilot_class(), 0.0, None),
        recipient_actor_name=PILOT,
        core_resolver=lambda n: _core() if n == PILOT else None,
        rules=swn_pack.rules,
    )
    offered_ids = {b["id"] for b in payload["beats"]}
    leaked = offered_ids & _WN_PERSONAL_COMBAT_IDS

    assert not leaked, (
        "the seated SWN dogfight's player menu must not offer the WN personal-combat "
        f"actions; leaked ids: {sorted(leaked)} (offered={sorted(offered_ids)})"
    )


# ---------------------------------------------------------------------------
# AC5 — OTEL: the dogfight beat-menu decision records the bound ruleset so the GM
# panel can confirm an SWN dogfight got SWN/sealed-letter beats, not WWN defaults.
# ---------------------------------------------------------------------------


def test_dogfight_beat_menu_span_records_ruleset(swn_pack: GenrePack, otel_capture) -> None:
    """RED (AC5): building the dogfight beat menu must emit a span recording the bound
    ruleset AND a beat set free of the WN personal-combat pool — the lie-detector for
    this mismatch class. Today ``confrontation.beat_filter`` carries no ruleset, and its
    ``available_beat_ids`` still lists the leaked personal-combat actions."""
    build_confrontation_payload(
        encounter=_dogfight_encounter(),
        cdef=_dogfight_cdef(swn_pack),
        genre_slug=SWN_TEST_PACK,
        recipient_pc=(_pilot_class(), 0.0, None),
        recipient_actor_name=PILOT,
        core_resolver=lambda n: _core() if n == PILOT else None,
        rules=swn_pack.rules,
    )

    beat_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "confrontation.beat_filter"
    ]
    assert beat_spans, "building the dogfight beat menu must emit a confrontation.beat_filter span"
    span = beat_spans[-1]
    attrs = dict(span.attributes or {})

    assert attrs.get("ruleset") == "swn", (
        "the dogfight beat-menu span must record the bound ruleset so the GM panel can "
        f"confirm SWN got SWN/sealed-letter beats; attributes={attrs}"
    )
    offered_ids = set(str(attrs.get("available_beat_ids", "")).split(","))
    leaked = offered_ids & _WN_PERSONAL_COMBAT_IDS
    assert not leaked, (
        f"the span's available_beat_ids leak WN personal-combat actions: {sorted(leaked)}"
    )


# ---------------------------------------------------------------------------
# AC1 + AC4 — committing the offered dogfight beat through the REAL DICE_THROW seam
# must not crash the SWN resolver or soft-lock the confrontation.
# ---------------------------------------------------------------------------


def test_committing_offered_dogfight_beat_does_not_crash_swn_resolver(
    swn_pack: GenrePack,
) -> None:
    """RED (AC1/AC4): seat a dogfight, take the beat the player is actually offered, and
    commit it through the production ``dispatch_dice_throw`` seam. Today the menu leads
    with ``attack`` (stat_check STR); committing it reaches
    ``without_number.attack_params`` → ``_stat(flavor_block,'STR')`` → KeyError, which
    tears down the websocket and leaves the confrontation soft-locked. The committed
    beat must instead resolve with no exception."""
    pack = swn_pack
    snap = GameSnapshot(genre=SWN_TEST_PACK)
    snap.genre_slug = SWN_TEST_PACK
    snap.world_slug = TEST_WORLD
    snap.characters = [_make_flavor_pilot(PILOT)]
    snap.player_seats["p1"] = PILOT

    trigger_encounter(
        snap,
        pack,
        "dogfight",
        PILOT,
        npcs_present=[NpcMention(name=OPPONENT, role="hostile", side="opponent")],
    )
    enc = snap.encounter
    assert enc is not None, "the dogfight must seat before the commit"

    # The beat the player is actually handed (real menu surface). Today: 'attack' first.
    offered = beats_available_for(
        _dogfight_cdef(pack), _pilot_class(), spell_slots_remaining=0.0, is_wn_binding=True
    )
    assert offered, "a seated dogfight must offer the player at least one beat (no soft-lock)"
    first = offered[0]

    payload = DiceThrowPayload(
        request_id="req-158-49",
        throw_params=ThrowParams(
            velocity=(0.0, 5.0, -2.0), angular=(1.0, 1.0, 1.0), position=(0.5, 0.5)
        ),
        face=[13],
        beat_id=first.id,
    )

    # Production DICE_THROW seam. Must NOT raise (today: KeyError "stat 'STR' not in
    # stat block [...]"). dispatch_dice_throw returning normally == the ws survived.
    outcome = dispatch_dice_throw(
        payload=payload,
        rolling_player_id="p1",
        character_name=PILOT,
        character_stats=dict(_FLAVOR_STATS),
        encounter=enc,
        pack=pack,
        genre_slug=SWN_TEST_PACK,
        session_id="s-158-49",
        round_number=1,
        room_broadcast=None,
        snapshot=snap,
    )

    assert outcome is not None
    # AC4: the confrontation is not abandoned mid-commit by a crash — the encounter
    # is still present and resolvable (a legitimate pending seal is fine; a torn-down
    # ws with an unresolved encounter is the soft-lock this guards against).
    assert snap.encounter is not None
