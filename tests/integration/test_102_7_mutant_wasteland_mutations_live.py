"""Story 102-7 RED — mutant_wasteland's mutation system is LIVE (content-gated).

THE GAP (the epic's disease, measured): AWN Plan 2 shipped the entire mutation
ENGINE (catalog models, chargen MP economy, use_ops, awn.mutation.* spans, the
use_mutation tool — server PR #781) and the chargen seam
(``init_mutation_state_for_session``, called from chargen_mixin). But
``genre_packs/mutant_wasteland/`` ships NO ``mutations.yaml`` — the loader's
catalog hook finds nothing, ``pack.mutations`` is None, every session emits
``mutation.init_skipped`` and the pack's MARQUEE mechanic is dead in
production. Built, not wired.

WHAT THIS FILE PINS (the content half + the real-pack live proof; synthetic
engine behavior lives in tests/server/test_102_7_*.py):

  1. The pack ships a loadable mutation catalog (``pack.mutations`` not None)
     whose ``mutant_classes`` name real archetypes of THIS pack.
  2. The Plan 2 §6.3 retirements happened: the never-live genre-tier
     ``magic.yaml`` and ``flickering_reach/magic.yaml.draft`` are gone —
     mutations are the pack's ONLY magic authority (no double-truth).
  3. The combat "mutant ability" beat carries the §6.3 wiring marker.
  4. The production chargen seam seeds MutationState for a mutant-class PC
     from the REAL catalog (and not for a non-mutant class).
  5. The spec §7 wiring test #1: a mutation use driven through the REAL apply
     path on the REAL pack fires ``awn.mutation.used`` and moves the Strain
     pool — the GM-panel lie-detector proof that mutant_wasteland's crunch is
     live, mirroring test_wwn_elemental_harmony_dispatch.py.

P2-4 discipline: these tests assert WIRING (catalog present, classes resolve,
spans fire), never catalog CONTENT details (counts, ranges, specific powers) —
those belong to the pack validator.
"""

from __future__ import annotations

from typing import Any

import pytest

from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.pack import GenrePack
from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

_GENRE = "mutant_wasteland"


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


pytestmark = pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")


def _load_pack() -> GenrePack:
    try:
        return load_genre_pack(find_pack_path(_GENRE))
    except PackNotFound as exc:  # pragma: no cover - environment guard
        pytest.skip(str(exc))


def _spans_named(otel_capture, name: str) -> list[Any]:
    return [s for s in otel_capture.get_finished_spans() if s.name == name]


# ===========================================================================
# 1 — the catalog is bound and coherent with the pack's archetypes
# ===========================================================================


def test_pack_ships_a_loadable_mutation_catalog() -> None:
    """``pack.mutations`` must be a real MutationCatalog, loaded by the same
    ``load_genre_pack`` production code path the server boots with — not a
    file that merely exists. Today it is None (no mutations.yaml)."""
    pack = _load_pack()
    assert pack.mutations is not None, (
        "mutant_wasteland must ship genre_packs/mutant_wasteland/mutations.yaml "
        "— the pack's marquee mechanic has a fully built engine (PR #781) and "
        "no catalog; pack.mutations is None and every session logs "
        "mutation.init_skipped"
    )


def test_mutant_classes_name_real_archetypes() -> None:
    """Cross-file integrity: every ``mp_economy.mutant_classes`` entry must be
    an archetype this pack actually offers, and there must be at least one —
    otherwise chargen can never seed anyone and the catalog is dead content."""
    pack = _load_pack()
    assert pack.mutations is not None, "needs mutations.yaml (see sibling test)"

    archetype_names = {a.name for a in pack.archetypes}
    mutant_classes = set(pack.mutations.mp_economy.mutant_classes)

    assert mutant_classes, "mutant_classes must not be empty"
    unknown = mutant_classes - archetype_names
    assert not unknown, (
        f"mutant_classes {sorted(unknown)} name no archetype in this pack "
        f"(have {sorted(archetype_names)}) — chargen gates on exact class "
        "names, so a typo here silently disables mutations for everyone"
    )


# ===========================================================================
# 2 — the §6.3 retirements: one authority for mutation truth
# ===========================================================================


def test_genre_magic_yaml_is_retired() -> None:
    """Plan 2 P2-1 (replace, don't reconcile): the never-live genre-tier
    magic.yaml and the flickering_reach draft must be deleted once the
    mutation catalog lands — two descriptors of one mechanic is the
    double-truth the parent spec flagged. (File-shape assertions because
    retirement IS a content-state fact; the live half of the doctrine is the
    catalog binding asserted above.)"""
    pack_dir = find_pack_path(_GENRE)
    assert not (pack_dir / "magic.yaml").exists(), (
        "genre_packs/mutant_wasteland/magic.yaml must be retired (P2-1) — "
        "its narrator_register migrates into mutations.yaml"
    )
    assert not (pack_dir / "worlds" / "flickering_reach" / "magic.yaml.draft").exists(), (
        "flickering_reach/magic.yaml.draft must be retired (P2-1)"
    )


# ===========================================================================
# 3 — the combat beat carries the wiring marker
# ===========================================================================


def test_combat_mutant_ability_beat_is_mutation_marked() -> None:
    """The pack's combat confrontation carries a mutation beat (today:
    ``mutant_ability``) that must bear the §6.3 ``mutation_resolution`` marker
    so the apply path routes it through use_ops instead of bare narration
    (behavior pinned in tests/server/test_102_7_mutation_beat_use_ops.py)."""
    pack = _load_pack()
    combat = next((c for c in pack.rules.confrontations if c.category == "combat"), None)
    assert combat is not None, "mutant_wasteland must declare a combat confrontation"

    marked = [b for b in combat.beats if getattr(b, "mutation_resolution", False)]
    assert marked, (
        "the combat confrontation must carry at least one mutation_resolution "
        "beat (the 'Use Mutation' texture, rules.yaml mutant_ability) — "
        f"beats present: {[b.id for b in combat.beats]}"
    )


# ===========================================================================
# 4 — the production chargen seam seeds from the REAL catalog
# ===========================================================================


def test_chargen_seam_seeds_mutant_class_from_real_catalog(otel_capture) -> None:
    """Drive ``init_mutation_state_for_session`` — the exact function
    chargen_mixin calls at chargen confirmation — with the real pack catalog.
    A mutant-class PC must come out with a CharacterMutationState (MP pool
    per the catalog's economy, negatives rolled) and the acquisition must be
    span-visible; today the call short-circuits on catalog=None."""
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.server.mutation_init import init_mutation_state_for_session

    pack = _load_pack()
    assert pack.mutations is not None, "needs mutations.yaml (see test 1)"
    mutant_class = pack.mutations.mp_economy.mutant_classes[0]

    snap = GameSnapshot(
        genre_slug=_GENRE, world_slug="flickering_reach", turn_manager=TurnManager()
    )
    init_mutation_state_for_session(
        snap,
        catalog=pack.mutations,
        character_name="Rux",
        character_class=mutant_class,
        session_id="test-102-7",
    )

    assert snap.mutation_state is not None, "the seam must create MutationState"
    cs = snap.mutation_state.characters.get("Rux")
    assert cs is not None, f"a {mutant_class!r} PC must be seeded with a CharacterMutationState"
    assert _spans_named(otel_capture, "awn.mutation.acquired"), (
        "chargen seeding must be span-visible (awn.mutation.acquired) — "
        "the GM panel is the lie detector"
    )


def test_chargen_seam_skips_non_mutant_class(otel_capture) -> None:
    """Absence is a valid state, not a fallback: a class outside
    ``mutant_classes`` gets NO per-character mutation state."""
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.server.mutation_init import init_mutation_state_for_session

    pack = _load_pack()
    assert pack.mutations is not None, "needs mutations.yaml (see test 1)"
    mutant_classes = set(pack.mutations.mp_economy.mutant_classes)
    non_mutant = next((a.name for a in pack.archetypes if a.name not in mutant_classes), None)
    assert non_mutant is not None, (
        "fixture premise: the pack must offer at least one non-mutant archetype"
    )

    snap = GameSnapshot(
        genre_slug=_GENRE, world_slug="flickering_reach", turn_manager=TurnManager()
    )
    init_mutation_state_for_session(
        snap,
        catalog=pack.mutations,
        character_name="Dray",
        character_class=non_mutant,
        session_id="test-102-7",
    )

    seeded = snap.mutation_state.characters.get("Dray") if snap.mutation_state is not None else None
    assert seeded is None, (
        f"a {non_mutant!r} PC must NOT receive mutation state — absence is "
        "the correct state for a non-mutant class"
    )


# ===========================================================================
# 5 — spec §7 wiring test #1: production-path mutation use on the REAL pack
# ===========================================================================


def test_production_path_mutation_use_fires_spans_and_strain(otel_capture, monkeypatch) -> None:
    """THE LIVE PROOF (story title: 'prove AWN combat + lethality fire live'):
    seat the real combat confrontation, give a real-catalog mutant a
    Strain-costed mutation, drive the mutation beat through the production
    DICE seam, and assert ``awn.mutation.used`` + a moved Strain pool.

    SEAM REWIRED for story 158-54: the original drive
    (``_apply_narration_result_to_snapshot`` with a narrator BeatSelection)
    is doctrine-dead in a live AWN combat — ADR-143 de-nativization drops all
    stray narrator beats (``wn_combat_beat_dropped_engine_owns_round``,
    measured 2026-07-03; ``awn`` is WN-family). A live WN round resolves ONLY
    on the player's DICE_THROW, so the live proof drives
    ``dispatch_dice_throw`` with the mutation-marked beat + ``mutation_id`` —
    the 102-2 cast-spine seam, retold for the wastes. Do NOT re-point this at
    the narrator apply path; do NOT exempt mutation beats from the drop
    (bind the ruleset, don't balance it)."""
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory
    from sidequest.game.session import GameSnapshot
    from sidequest.game.system_strain import SystemStrainPool
    from sidequest.game.turn import TurnManager
    from sidequest.mutation.state import CharacterMutationState, MutationState
    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.protocol.models import InitiativeEntry
    from sidequest.server.dispatch.dice import dispatch_dice_throw
    from sidequest.server.dispatch.encounter_lifecycle import (
        instantiate_encounter_from_trigger,
    )

    pack = _load_pack()
    assert pack.rules.ruleset == "awn", "mutant_wasteland must stay bound ruleset: awn"
    assert pack.mutations is not None, "needs mutations.yaml (see test 1)"

    # A Strain-costed, usable (non-passive) positive from the REAL catalog —
    # wiring needs one to exist, but pins nothing else about content (P2-4).
    costed = next((m for m in pack.mutations.positives if m.strain_cost > 0), None)
    assert costed is not None, (
        "the catalog must offer at least one Strain-costed positive mutation "
        "(the cost economy is the crunch this story exists to make live)"
    )

    pc_name = "Rux"
    stats = {name: 10 for name in pack.rules.ability_score_names}
    pc_core = CreatureCore(
        name=pc_name,
        description="A mutant of the flickering wastes.",
        personality="watchful",
        inventory=Inventory(),
        hp={"current": 10, "max": 10, "base_max": 10},
        armor_class=12,
        system_strain=SystemStrainPool(current=0, max=10),
    )
    pc = Character(
        core=pc_core,
        char_class=pack.mutations.mp_economy.mutant_classes[0],
        race="Mutant Human",
        backstory="Born under the fallout sky.",
        stats=stats,
    )

    snap = GameSnapshot(
        genre_slug=_GENRE,
        world_slug="flickering_reach",
        turn_manager=TurnManager(interaction=2),
        player_seats={"player:Keith": pc_name},
    )
    snap.characters.append(pc)
    snap.mutation_state = MutationState(
        characters={pc_name: CharacterMutationState(mp_remaining=0, positive_ids=[costed.id])}
    )

    # Seat the real combat confrontation via the production seam.
    from sidequest.agents.orchestrator import NpcMention

    opponent = "Raider Scav"
    snap.character_locations[pc_name] = "The Glass Flats"
    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type="combat",
        player_name=pc_name,
        npcs_present=[NpcMention(name=opponent, side="opponent")],
        genre_slug=_GENRE,
    )
    assert enc is not None, "seating the real combat confrontation must succeed"
    snap.encounter = enc
    # 102-4: the WN walk resolves in PERSISTED initiative order — pin it so
    # the seam's unseeded 1d8+DEX roll can never flip the choreography.
    enc.initiative = [
        InitiativeEntry(token_id=pc_name, value=9),
        InitiativeEntry(token_id=opponent, value=2),
    ]

    combat = next(c for c in pack.rules.confrontations if c.category == "combat")
    mutation_beat = next(
        (b for b in combat.beats if getattr(b, "mutation_resolution", False)), None
    )
    assert mutation_beat is not None, "needs the marked beat (see test 3)"

    # Pin every rng call on the round path (saves, damage dice, reprisal)
    # through the shared stdlib random module object.
    monkeypatch.setattr("random.randint", lambda a, b: a)

    strain_before = pc_core.system_strain.current
    dispatch_dice_throw(
        payload=DiceThrowPayload(
            request_id="req-102-7-live",
            throw_params=ThrowParams(
                velocity=(0.0, 5.0, -2.0),
                angular=(1.0, 1.0, 1.0),
                position=(0.5, 0.5),
            ),
            face=[20],
            beat_id=mutation_beat.id,
            mutation_id=costed.id,  # type: ignore[call-arg]
        ),
        rolling_player_id="player:Keith",
        character_name=pc_name,
        character_stats=dict(stats),
        encounter=enc,
        pack=pack,
        genre_slug=_GENRE,
        session_id="test-102-7-live",
        round_number=1,
        room_broadcast=lambda _msg: None,
        snapshot=snap,
    )

    used = _spans_named(otel_capture, "awn.mutation.used")
    assert len(used) == 1, (
        "a mutation beat on the REAL pack driven through the production dice "
        f"seam must emit awn.mutation.used; got {len(used)} — mutant_wasteland's "
        "marquee mechanic is still improv"
    )
    assert used[0].attributes["mutation_id"] == costed.id

    assert pc_core.system_strain.current == strain_before + costed.strain_cost, (
        "the Strain cost must land on the PC's pool through the live path; "
        f"before={strain_before} cost={costed.strain_cost} "
        f"after={pc_core.system_strain.current}"
    )
