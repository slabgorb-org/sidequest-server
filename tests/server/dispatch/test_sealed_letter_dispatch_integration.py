"""End-to-end integration tests for the sealed-letter dispatch wiring (T5).

T3 added ``resolve_sealed_letter_lookup`` and unit-tested it in isolation.
T5 wires it into the production confrontation resolution path so that a
real game turn arriving for a confrontation with
``resolution_mode == sealed_letter_lookup`` actually fires the dogfight
engine.

These tests drive the *production* dispatch entry point
(``_apply_narration_result_to_snapshot``) — not the resolver directly —
so they catch wiring regressions that unit tests of the handler can't see.

Coverage:
  - End-to-end: narrator emits dogfight confrontation + maneuver beat
    selections → snapshot's encounter has per_actor_state mutated, OTEL
    cell_resolved span fires, narration_hint pushed to encounter
  - Role assignment: instantiator special-cases sealed-letter
    confrontations to assign role="red" / "blue" rather than "combatant"
  - Validation: missing interaction_table raises, wrong actor count
    raises (CLAUDE.md no-silent-fallbacks)
  - Regression: legacy ``beat_selection`` confrontations still resolve
    via apply_beat — the new branch is additive, not destructive
  - Persistence: per_actor_state survives a snapshot model_dump round
    trip after sealed-letter resolution

Story 96-1: the dogfight tests drive the ``swn_test_pack`` FIXTURE
(world-tier ``multifocal_laser`` catalog in ``test_world``) instead of live
space_opera content, so content-only changes can never turn them red. Only
the two legacy beat_selection regression tests still load live
caverns_and_claudes (and carry their own content skipif) — flagged as a
follow-up in the 96-1 delivery findings.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.agents.orchestrator import (
    BeatSelection,
    NarrationTurnResult,
    NpcMention,
)
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.encounter import StructuredEncounter
from sidequest.game.session import GameSnapshot
from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import ResolutionMode
from sidequest.protocol.dice import RollOutcome
from sidequest.server.narration_apply import (
    NarrationApplyOutcome,
    _apply_narration_result_to_snapshot,
)
from tests._helpers.session_room import room_for
from tests._helpers.trigger_encounter import trigger_encounter


def _make_pilot(name: str) -> Character:
    """Minimal SWN PC with the flavor attrs that ship_attack_params needs.

    Both Reflex and Intellect are 10 (modifier=0) so the to-hit arithmetic is
    deterministic. The character model does not yet carry an SWN Pilot skill, so
    pilot_skill falls back to the authored cdef default (player_default_stats) —
    this is an authored default, not a silent fallback.
    """
    return Character(
        core=CreatureCore(name=name, description="Test pilot.", personality="Calm."),
        backstory="A pilot.",
        char_class="Pilot",
        race="Human",
        stats={"Reflex": 10, "Intellect": 10},
    )


# Live-content root — used ONLY by the two legacy beat_selection regression
# tests below (caverns_and_claudes), which carry their own skipif. The
# dogfight tests drive the swn_test_pack fixture (story 96-1).
CONTENT_ROOT = Path(__file__).resolve().parents[3].parent / "sidequest-content" / "genre_packs"

_NEEDS_LIVE_CONTENT = pytest.mark.skipif(
    not CONTENT_ROOT.is_dir(),
    reason="sidequest-content not on disk alongside sidequest-server",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def swn_fixture_pack() -> GenrePack:
    from tests._helpers.fixture_packs import SWN_TEST_PACK, load_fixture_pack

    return load_fixture_pack(SWN_TEST_PACK)


@pytest.fixture
def swn_snap(swn_fixture_pack: GenrePack) -> tuple[GameSnapshot, GenrePack]:
    from tests._helpers.fixture_packs import SWN_TEST_PACK, TEST_WORLD

    snap = GameSnapshot(genre=SWN_TEST_PACK)
    snap.genre_slug = SWN_TEST_PACK
    # Epic 94 production shape: weapon lookup resolves world-tier inventory.
    snap.world_slug = TEST_WORLD
    return snap, swn_fixture_pack


@pytest.fixture
def cac_pack() -> GenrePack:
    """caverns_and_claudes for the legacy beat_selection regression test.

    Loaded directly from the sidequest-content side repo so we exercise
    real content (not the test fixture pack) for the regression — that
    way both branches of the dispatch dispatch through identical loader
    paths.
    """
    return load_genre_pack(CONTENT_ROOT / "caverns_and_claudes")


def _make_wwn_pc(name: str) -> Character:
    """Minimal caverns_and_claudes (WWN ruleset) PC carrying the WWN attribute
    block. cac was ported to WWN (PR #429), so combat instantiation rolls
    initiative (1d8+DEX) and resolves each player-side actor's DEX from
    ``snapshot.characters`` — failing loud on a seated player that isn't a real
    Character. The WWN DEXTERITY flavor is "DEX" (rules.yaml attribute_map)."""
    return Character(
        core=CreatureCore(name=name, description="A delver.", personality="Steady."),
        backstory="Sünden-born.",
        char_class="Warrior",
        race="Human",
        stats={"STR": 12, "DEX": 12, "CON": 12, "INT": 10, "WIS": 10, "CHA": 10},
    )


@pytest.fixture
def cac_snap(cac_pack: GenrePack) -> tuple[GameSnapshot, GenrePack]:
    snap = GameSnapshot(genre="caverns_and_claudes")
    snap.genre_slug = "caverns_and_claudes"
    # Seat the PC the legacy-beat regression tests drive ("Rux") so the WWN
    # initiative seam can resolve its DEX (No Silent Fallbacks).
    snap.characters.append(_make_wwn_pc("Rux"))
    return snap, cac_pack


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


# ---------------------------------------------------------------------------
# Role assignment — sealed-letter confrontations get red/blue tags
# ---------------------------------------------------------------------------


def test_dogfight_instantiation_assigns_red_blue_roles(
    swn_snap: tuple[GameSnapshot, GenrePack],
) -> None:
    """When a dogfight starts, the instantiator must tag actors with
    role="red" (player) and role="blue" (opponent) — NOT "combatant" —
    so the sealed-letter handler can find them by role lookup.
    """
    snap, pack = swn_snap
    trigger_encounter(
        snap,
        pack,
        "dogfight",
        "Maverick",
        npcs_present=[
            NpcMention(name="Bandit Ace", role="hostile", side="opponent"),
        ],
    )

    enc = snap.encounter
    assert enc is not None
    assert enc.encounter_type == "dogfight"
    assert len(enc.actors) == 2
    roles = sorted(a.role for a in enc.actors)
    assert roles == ["blue", "red"], (
        f"sealed-letter encounter must assign role=red+blue, got {roles}"
    )
    red = next(a for a in enc.actors if a.role == "red")
    blue = next(a for a in enc.actors if a.role == "blue")
    assert red.name == "Maverick"
    assert red.side == "player"
    assert blue.name == "Bandit Ace"
    assert blue.side == "opponent"


def test_dogfight_instantiation_rejects_zero_npcs(
    swn_snap: tuple[GameSnapshot, GenrePack],
    otel_capture: InMemorySpanExporter,
) -> None:
    """Sealed-letter dogfights need exactly one opponent. Playtest
    2026-05-08: the prior crash-on-arity behavior wedged the player on
    turn 1 (auto-save + reconnect = sticky crash loop). Now the lifecycle
    raises ``SealedLetterArityError`` and fires
    ``encounter.sealed_letter_arity_rejected`` — no encounter instantiates.
    """
    from sidequest.server.dispatch.encounter_lifecycle import SealedLetterArityError

    snap, pack = swn_snap
    with pytest.raises(SealedLetterArityError):
        trigger_encounter(snap, pack, "dogfight", "Maverick", npcs_present=[])
    assert snap.encounter is None, "no encounter must instantiate when arity guard fires"

    span_names = {span.name for span in otel_capture.get_finished_spans()}
    assert "encounter.sealed_letter_arity_rejected" in span_names, (
        "OTEL lie-detector span must fire so the GM panel sees the rejection"
    )


def test_dogfight_instantiation_rejects_two_npcs(
    swn_snap: tuple[GameSnapshot, GenrePack],
    otel_capture: InMemorySpanExporter,
) -> None:
    """Sealed-letter dogfights are 1v1 — multi-NPC scenes (drift gang
    pack, posse, mob) decline gracefully. Playtest 2026-05-08 was the
    forcing function: narrator legitimately staged a 3-raider pack, the
    1v1 contract refused 3 actors, the turn crashed and auto-save +
    reconnect put the player back on the same crashing turn forever.
    """
    from sidequest.server.dispatch.encounter_lifecycle import SealedLetterArityError

    snap, pack = swn_snap
    with pytest.raises(SealedLetterArityError):
        trigger_encounter(
            snap,
            pack,
            "dogfight",
            "Maverick",
            npcs_present=[
                NpcMention(name="Bandit One", role="hostile", side="opponent"),
                NpcMention(name="Bandit Two", role="hostile", side="opponent"),
            ],
        )
    assert snap.encounter is None, "no encounter must instantiate when arity guard fires"

    arity_spans = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "encounter.sealed_letter_arity_rejected"
    ]
    assert len(arity_spans) == 1, "exactly one arity-rejection span per declined trigger"
    attrs = arity_spans[0].attributes or {}
    assert attrs.get("encounter_type") == "dogfight"
    assert attrs.get("npc_count") == 2
    assert attrs.get("player_name") == "Maverick"


def test_dogfight_instantiation_arity_error_propagates_at_lifecycle_layer(
    swn_snap: tuple[GameSnapshot, GenrePack],
) -> None:
    """The wrapper at ``_apply_narration_result_to_snapshot`` is what
    grants the graceful skip. The lifecycle helper itself must still
    raise ``SealedLetterArityError`` — direct callers (tests, future
    dispatch paths that want loud failure) MUST see the typed exception.
    """
    from sidequest.server.dispatch.encounter_lifecycle import (
        SealedLetterArityError,
        instantiate_encounter_from_trigger,
    )

    snap, pack = swn_snap
    with pytest.raises(SealedLetterArityError, match="exactly one opponent"):
        instantiate_encounter_from_trigger(
            snapshot=snap,
            pack=pack,
            encounter_type="dogfight",
            player_name="Maverick",
            npcs_present=[
                NpcMention(name="Bandit One", role="hostile", side="opponent"),
                NpcMention(name="Bandit Two", role="hostile", side="opponent"),
            ],
            genre_slug="space_opera",
        )


# ---------------------------------------------------------------------------
# End-to-end dispatch — narrator turn resolves through sealed_letter
# ---------------------------------------------------------------------------


def test_dogfight_turn_resolves_through_sealed_letter_dispatch(
    swn_snap: tuple[GameSnapshot, GenrePack],
    otel_capture: InMemorySpanExporter,
) -> None:
    """The keystone wiring test.

    Turn 1: narrator initiates the dogfight (player + bandit).
    Turn 2: narrator emits beat_selections for both pilots — these
    commits must flow through the sealed-letter dispatch branch (not
    apply_beat), mutate per_actor_state, fire the cell_resolved span,
    and push the narration_hint onto the encounter.
    """
    snap, pack = swn_snap
    snap.characters = [_make_pilot("Vega")]

    # Turn 1: instantiate the dogfight encounter
    trigger_encounter(
        snap,
        pack,
        "dogfight",
        "Vega",
        npcs_present=[
            NpcMention(name="Iron Fang", role="ace", side="opponent"),
        ],
    )
    enc = snap.encounter
    assert enc is not None
    # Task 12: frame HP is now seeded at instantiation — per_actor_state carries
    # frame_hp/frame_hp_max; the turn resolver will add gun-geometry keys on top.
    from sidequest.game.dogfight_shot import FRAME_HP_KEY, FRAME_HP_MAX_KEY

    for actor in enc.actors:
        assert FRAME_HP_KEY in actor.per_actor_state, (
            f"actor {actor.name!r} missing frame_hp after instantiation"
        )
        assert FRAME_HP_MAX_KEY in actor.per_actor_state, (
            f"actor {actor.name!r} missing frame_hp_max after instantiation"
        )
    assert enc.narrator_hints == []

    # Clear the captured spans so the next turn's spans are isolated
    otel_capture.clear()

    # Turn 2: narrator emits maneuver commits keyed by actor name
    # ("loop" + "kill_rotation" → mutual gunline cell, both pilots score)
    _apply_narration_result_to_snapshot(
        snap,
        NarrationTurnResult(
            narration="You pull the loop; he counters with the kill-rotation.",
            beat_selections=[
                BeatSelection(
                    actor="Vega",
                    beat_id="loop",
                    outcome=RollOutcome.Success,
                ),
                BeatSelection(
                    actor="Iron Fang",
                    beat_id="kill_rotation",
                    outcome=RollOutcome.Success,
                ),
            ],
        ),
        player_name="Vega",
        pack=pack,
        room=room_for(snap),
    )

    # per_actor_state was mutated — both pilots have a gun_solution
    # because the (loop, kill_rotation) cell is mutual gunline
    red = next(a for a in enc.actors if a.role == "red")
    blue = next(a for a in enc.actors if a.role == "blue")
    assert red.per_actor_state.get("gun_solution") is True, (
        f"red per_actor_state not mutated: {red.per_actor_state!r}"
    )
    assert blue.per_actor_state.get("gun_solution") is True, (
        f"blue per_actor_state not mutated: {blue.per_actor_state!r}"
    )

    # narration_hint pushed onto encounter so narrator can surface it
    assert len(enc.narrator_hints) >= 1
    assert any(h.strip() for h in enc.narrator_hints)

    # OTEL spans fired — confrontation_started, two maneuver_committed,
    # and cell_resolved
    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert "dogfight.confrontation_started" in span_names
    assert "dogfight.cell_resolved" in span_names
    maneuver_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "dogfight.maneuver_committed"
    ]
    assert len(maneuver_spans) == 2, (
        f"expected 2 maneuver_committed spans, got {len(maneuver_spans)}"
    )


def test_dogfight_dispatch_does_not_invoke_apply_beat(
    swn_snap: tuple[GameSnapshot, GenrePack],
) -> None:
    """Sealed-letter resolution is exclusive of the legacy beat path.

    The dogfight beats and maneuver IDs share a namespace ("straight",
    "bank", "loop", "kill_rotation"). Without the dispatch branch, the
    legacy apply_beat loop would also fire and double-apply mechanics
    (e.g., both bumping the player_metric AND merging cell deltas).
    Pin: player_metric.current MUST stay at its starting value because
    the sealed-letter path does not move dual-track dials directly.
    """
    snap, pack = swn_snap
    snap.characters = [_make_pilot("Pilot")]

    trigger_encounter(
        snap,
        pack,
        "dogfight",
        "Pilot",
        npcs_present=[
            NpcMention(name="Wraith", role="hostile", side="opponent"),
        ],
    )
    enc = snap.encounter
    assert enc is not None
    starting_player = enc.player_metric.current
    starting_opponent = enc.opponent_metric.current

    _apply_narration_result_to_snapshot(
        snap,
        NarrationTurnResult(
            narration="Straight pass.",
            beat_selections=[
                BeatSelection(actor="Pilot", beat_id="straight"),
                BeatSelection(actor="Wraith", beat_id="bank"),
            ],
        ),
        player_name="Pilot",
        pack=pack,
        room=room_for(snap),
    )

    # Sealed-letter does not advance dual-track dials directly — it
    # mutates per_actor_state, and the narrator reads narrator_hints
    # to declare the next dial change. If apply_beat had also fired,
    # the dial would have moved.
    assert enc.player_metric.current == starting_player, (
        "apply_beat fired in addition to sealed_letter — dial moved unexpectedly"
    )
    assert enc.opponent_metric.current == starting_opponent


# ---------------------------------------------------------------------------
# Persistence — per_actor_state round-trips after dispatch
# ---------------------------------------------------------------------------


def test_per_actor_state_round_trip_after_dispatch(
    swn_snap: tuple[GameSnapshot, GenrePack],
) -> None:
    """After sealed-letter dispatch mutates per_actor_state, the
    StructuredEncounter must survive model_dump → model_validate without
    losing the cockpit descriptors. This is the save/load contract.
    """
    snap, pack = swn_snap
    snap.characters = [_make_pilot("Lance")]

    trigger_encounter(
        snap,
        pack,
        "dogfight",
        "Lance",
        npcs_present=[
            NpcMention(name="Spectre", role="hostile", side="opponent"),
        ],
    )
    _apply_narration_result_to_snapshot(
        snap,
        NarrationTurnResult(
            narration="Loop vs kill rotation.",
            beat_selections=[
                BeatSelection(actor="Lance", beat_id="loop"),
                BeatSelection(actor="Spectre", beat_id="kill_rotation"),
            ],
        ),
        player_name="Lance",
        pack=pack,
        room=room_for(snap),
    )

    enc_before = snap.encounter
    assert enc_before is not None
    red_before = next(a for a in enc_before.actors if a.role == "red")
    assert red_before.per_actor_state.get("gun_solution") is True

    dumped = enc_before.model_dump(mode="json")
    enc_after = StructuredEncounter.model_validate(dumped)
    red_after = next(a for a in enc_after.actors if a.role == "red")
    blue_after = next(a for a in enc_after.actors if a.role == "blue")

    assert red_after.per_actor_state == red_before.per_actor_state
    assert (
        blue_after.per_actor_state
        == next(a for a in enc_before.actors if a.role == "blue").per_actor_state
    )


# ---------------------------------------------------------------------------
# Regression — legacy beat_selection still works (additive, not destructive)
# ---------------------------------------------------------------------------


@_NEEDS_LIVE_CONTENT
def test_legacy_beat_selection_path_still_works(
    cac_snap: tuple[GameSnapshot, GenrePack],
) -> None:
    """The CAC ``combat`` confrontation must continue to resolve through
    the legacy non-sealed-letter path. After the WWN port (PR #429) CAC
    combat resolves via ``beat_selection`` (the literal legacy beat path
    this test is named for). What's being pinned is the legacy code path
    that handles non-sealed-letter resolution via apply_beat — not the
    specific resolution_mode value; the only invariant that matters here
    is that it is NOT ``sealed_letter_lookup``. If the sealed-letter
    branch were wired too greedily, this test would diverge from prior
    behavior.
    """
    snap, pack = cac_snap

    # Turn 1: instantiate combat with a hostile NPC
    trigger_encounter(
        snap,
        pack,
        "combat",
        "Rux",
        npcs_present=[
            NpcMention(name="Goblin", role="hostile", side="opponent"),
        ],
    )
    enc = snap.encounter
    assert enc is not None
    assert enc.encounter_type == "combat"
    # CAC combat is NOT sealed_letter — actors keep the legacy role tag
    from sidequest.server.dispatch.confrontation import find_confrontation_def

    cdef = find_confrontation_def(
        pack.rules.confrontations if pack.rules else [],
        "combat",
    )
    assert cdef is not None
    assert cdef.resolution_mode != ResolutionMode.sealed_letter_lookup, (
        "the legacy beat path must not be the sealed-letter branch; CAC "
        f"combat resolves via {cdef.resolution_mode} after the WWN port"
    )
    assert all(a.role in ("combatant", "participant") for a in enc.actors), (
        f"legacy combat encounter should keep legacy role tags, got "
        f"{[(a.name, a.role) for a in enc.actors]}"
    )

    # The WWN port (PR #429) renamed the attack beat to "strike" (ablative-HP
    # damage channel). 108-3 later stripped the WWN action beats from cdef.beats
    # (the WWN engine owns the action set, ADR-143), so "strike" is no longer an
    # authored cdef beat — the legacy opposed/narration-apply path below resolves
    # it through the strike damage channel directly. The assertion that it was an
    # authored cdef beat is therefore obsolete.

    starting_opp = enc.opponent_metric.current

    # Turn 2: player strikes — this MUST go through apply_beat (the legacy
    # non-sealed-letter resolution path).
    _apply_narration_result_to_snapshot(
        snap,
        NarrationTurnResult(
            narration="Rux strikes the goblin clean.",
            beat_selections=[
                BeatSelection(
                    actor="Rux",
                    beat_id="strike",
                    outcome=RollOutcome.Success,
                ),
            ],
        ),
        player_name="Rux",
        pack=pack,
        room=room_for(snap),
    )
    # If sealed_letter dispatch had hijacked this turn, the opponent
    # dial would have stayed flat AND the resolver would have raised
    # because combat has no interaction_table.
    assert enc.opponent_metric.current >= starting_opp, (
        "apply_beat path no longer fires for legacy combat — sealed-letter "
        "branch is destructive, not additive"
    )


@_NEEDS_LIVE_CONTENT
def test_legacy_beat_path_returns_narration_apply_outcome(
    cac_snap: tuple[GameSnapshot, GenrePack],
) -> None:
    """The legacy beat path must return ``NarrationApplyOutcome``, not the
    inner ``RollOutcome`` from a beat selection.

    Regression: T7 follow-up introduced a shadowing bug where the legacy
    beat-loop body bound a local ``outcome = sel.outcome`` that shadowed
    the function-scoped ``outcome = NarrationApplyOutcome()``. The
    function then returned a RollOutcome enum from the last selection,
    silently breaking the contract. Production callers ignored the
    return value so it surfaced nowhere — but the playtest fixture and
    any future field on NarrationApplyOutcome would AttributeError.
    """
    snap, pack = cac_snap

    # Turn 1: instantiate combat with a hostile NPC.
    trigger_encounter(
        snap,
        pack,
        "combat",
        "Rux",
        npcs_present=[
            NpcMention(name="Goblin", role="hostile", side="opponent"),
        ],
    )

    # Turn 2: drive a beat selection through the legacy apply_beat path.
    # The shadowing bug surfaced HERE — `outcome = sel.outcome` rebinds
    # the function local, so the return type was the RollOutcome enum.
    apply_outcome = _apply_narration_result_to_snapshot(
        snap,
        NarrationTurnResult(
            narration="Rux strikes the goblin clean.",
            beat_selections=[
                BeatSelection(
                    actor="Rux",
                    beat_id="attack",
                    outcome=RollOutcome.Success,
                ),
            ],
        ),
        player_name="Rux",
        pack=pack,
        room=room_for(snap),
    )
    assert isinstance(apply_outcome, NarrationApplyOutcome), (
        f"legacy beat path must return NarrationApplyOutcome, "
        f"got {type(apply_outcome).__name__} — likely the inner "
        f"`outcome = sel.outcome` is shadowing the function-scoped "
        f"outcome dataclass again"
    )
    assert apply_outcome.sealed_letter is None, (
        "legacy beat path must not populate the sealed_letter outcome field"
    )


# ---------------------------------------------------------------------------
# Bounded narrator_hints — only the LAST cell's hint survives across turns
# ---------------------------------------------------------------------------


def test_narrator_hints_does_not_accumulate_across_dogfight_turns(
    swn_snap: tuple[GameSnapshot, GenrePack],
) -> None:
    """narrator_hints must hold only the LAST cell's hint, not the history.

    Stale hints across turns bloat the narrator prompt and confuse the
    narrator (turn 1's "merge" hint is wrong context for turn 5's
    "knife fight"). ``encounter_render`` joins ``narrator_hints`` with
    "; " and pastes that into the prompt every turn — accumulation here
    silently degrades narration quality with each round.
    """
    snap, pack = swn_snap
    snap.characters = [_make_pilot("Saber")]

    # Turn 1: instantiate the dogfight encounter
    trigger_encounter(
        snap,
        pack,
        "dogfight",
        "Saber",
        npcs_present=[
            NpcMention(name="Reaper", role="ace", side="opponent"),
        ],
    )
    enc = snap.encounter
    assert enc is not None
    assert enc.narrator_hints == []

    # Three resolution turns with different maneuver pairs — each must
    # OVERWRITE the previous hint, not append.
    turn_pairs = [
        ("straight", "straight"),
        ("loop", "kill_rotation"),
        ("bank", "loop"),
    ]
    captured_hints: list[str] = []
    for player_maneuver, opponent_maneuver in turn_pairs:
        _apply_narration_result_to_snapshot(
            snap,
            NarrationTurnResult(
                narration="Maneuver.",
                beat_selections=[
                    BeatSelection(actor="Saber", beat_id=player_maneuver),
                    BeatSelection(actor="Reaper", beat_id=opponent_maneuver),
                ],
            ),
            player_name="Saber",
            pack=pack,
            room=room_for(snap),
        )
        # After each turn, exactly one hint — never accumulating.
        assert len(enc.narrator_hints) == 1, (
            f"narrator_hints accumulated to {len(enc.narrator_hints)} entries "
            f"after maneuver pair ({player_maneuver}, {opponent_maneuver}); "
            f"got {enc.narrator_hints!r}"
        )
        captured_hints.append(enc.narrator_hints[0])

    # The last turn's hint is what survives — not turn 1's.
    assert enc.narrator_hints == [captured_hints[-1]]
    # Sanity: at least one transition produced a different hint string,
    # otherwise the test wouldn't actually be proving "replace" semantics.
    assert len(set(captured_hints)) > 1, (
        f"all 3 turns produced identical hints {captured_hints!r}; pick "
        f"maneuver pairs that map to distinct cells so the test guards "
        f"against append-vs-replace drift"
    )


# ---------------------------------------------------------------------------
# Validation — unknown maneuver in sealed-letter beat surfaces loudly
# ---------------------------------------------------------------------------


def test_unknown_maneuver_in_sealed_letter_raises(
    swn_snap: tuple[GameSnapshot, GenrePack],
) -> None:
    """A beat_id that is not in maneuvers_consumed must surface as a
    ValueError from the dispatch path (CLAUDE.md no-silent-fallback)."""
    snap, pack = swn_snap
    snap.characters = [_make_pilot("Apex")]

    trigger_encounter(
        snap,
        pack,
        "dogfight",
        "Apex",
        npcs_present=[
            NpcMention(name="Hydra", role="hostile", side="opponent"),
        ],
    )

    with pytest.raises(ValueError, match="not in maneuvers_consumed"):
        _apply_narration_result_to_snapshot(
            snap,
            NarrationTurnResult(
                narration="A maneuver no table covers.",
                beat_selections=[
                    BeatSelection(actor="Apex", beat_id="cobra_pugachev"),
                    BeatSelection(actor="Hydra", beat_id="bank"),
                ],
            ),
            player_name="Apex",
            pack=pack,
            room=room_for(snap),
        )
