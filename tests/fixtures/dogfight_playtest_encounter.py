"""Reusable playtest fixture for the SWN dogfight engine (T7).

A small helper module that constructs a dogfight encounter from the
``swn_test_pack`` FIXTURE pack wired through the production dispatch
path, and exposes a clean API for driving turns. Used by integration
tests and by manual playtest drivers.

Story 96-1: this helper previously loaded the LIVE space_opera pack from
sidequest-content, which broke when epic 94 migrated weapons to
world-tier inventory — and violated the doctrine that content-only
changes must never turn server tests red. It now loads the synthetic
``swn_test_pack`` fixture and binds ``test_world`` so the dogfight
weapon lookup exercises the same world-tier ``resolve_inventory``
REPLACE path production uses for migrated packs.

This is the "smallest viable playtest scaffold" demonstrating the full
T1-T6 dogfight engine working end-to-end:
    - Fixture content load (tests/fixtures/genre_packs/swn_test_pack/)
    - Production instantiation (assigns role=red/blue per T3)
    - Production dispatch (_apply_narration_result_to_snapshot, T5)
    - Sealed-letter resolution (per_actor_state mutation, OTEL spans)

Per CLAUDE.md no-silent-fallbacks:
    - A missing fixture pack raises FixturePackNotFound. That is a repo
      defect (fixtures ship with the tests), NOT a skippable environment
      condition — callers must not pytest.skip on it.
    - Invalid maneuvers raise ValueError before dispatch.
    - All other errors propagate from the production code path.

Per CLAUDE.md don't-reinvent: this module distills the fixture-construction
patterns from ``tests/server/dispatch/test_sealed_letter_dispatch_integration.py``
into reusable helpers — no new code paths, just a stable surface around
the existing production wiring.
"""

from __future__ import annotations

from pathlib import Path

from sidequest.agents.orchestrator import (
    BeatSelection,
    NarrationTurnResult,
    NpcMention,
)
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.session import GameSnapshot
from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import ConfrontationDef
from sidequest.server.dispatch.confrontation import find_confrontation_def
from sidequest.server.dispatch.encounter_lifecycle import (
    instantiate_encounter_from_trigger,
)
from sidequest.server.dispatch.sealed_letter import SealedLetterOutcome
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from tests._helpers.fixture_packs import (
    FIXTURE_PACKS_DIR,
    SWN_TEST_PACK,
    TEST_WORLD,
)
from tests._helpers.session_room import room_for

# Story 96-1: fixture packs, not live sidequest-content. Kept as a module
# constant so manual playtest drivers can still point pack_root elsewhere.
DEFAULT_CONTENT_ROOT = FIXTURE_PACKS_DIR

GENRE_SLUG = SWN_TEST_PACK
WORLD_SLUG = TEST_WORLD
DOGFIGHT_TYPE = "dogfight"


def make_dogfight_playtest_state(
    *,
    player_pilot_name: str = "Maverick",
    opponent_pilot_name: str = "Vulture",
    pack_root: Path | None = None,
) -> tuple[GameSnapshot, ConfrontationDef, GenrePack]:
    """Construct a fresh GameSnapshot with an active SWN-fixture dogfight.

    Loads the ``swn_test_pack`` fixture from
    ``tests/fixtures/genre_packs/`` via the production loader, then
    drives a single instantiation turn through
    ``_apply_narration_result_to_snapshot`` so the encounter is built by
    the same code path the running server uses. ``test_world`` is bound
    on the snapshot so weapon lookups resolve through the world-tier
    inventory catalog (epic 94 production shape).

    Args:
        player_pilot_name: Name to assign the red (player) actor.
        opponent_pilot_name: Name to assign the blue (opponent) NPC actor.
        pack_root: Optional override for the genre_packs root (e.g. for
            tests that ship their own content). Defaults to
            ``DEFAULT_CONTENT_ROOT``.

    Returns:
        Tuple of (snapshot, dogfight ConfrontationDef, loaded GenrePack).
        The pack is returned so callers don't have to re-load it to drive
        further turns; the ConfrontationDef is exposed because callers
        commonly want to inspect ``interaction_table.maneuvers_consumed``
        for legal-maneuver validation, beat lists, etc.

    Raises:
        FileNotFoundError: the fixture pack is not on disk at the expected
            location. Fixture packs ship with the test suite, so this is a
            repo defect — callers must NOT ``pytest.skip(...)`` on it.
        ValueError: Loaded fixture pack lacks a dogfight ConfrontationDef
            (fixture drift — surface loudly per CLAUDE.md).
        AssertionError: Production instantiation didn't produce the expected
            two-actor red/blue encounter (engine drift — caller wants to know).

    Side effects:
        - Loads the swn_test_pack fixture from disk.
        - Drives one narration turn through the production dispatch path
          to instantiate the encounter; per_actor_state is seeded with
          frame_hp/frame_hp_max at instantiation by production code.
    """
    root = pack_root if pack_root is not None else DEFAULT_CONTENT_ROOT
    pack_path = root / GENRE_SLUG
    if not pack_path.is_dir():
        raise FileNotFoundError(
            f"{GENRE_SLUG} fixture pack not found at {pack_path} — "
            f"the fixture ships with the test suite, so this is a repo "
            f"defect, not an environment gap"
        )

    pack = load_genre_pack(pack_path)
    confrontations = pack.rules.confrontations if pack.rules else []
    cdef = find_confrontation_def(confrontations, DOGFIGHT_TYPE)
    if cdef is None:
        raise ValueError(
            f"{GENRE_SLUG} fixture pack at {pack_path} has no '{DOGFIGHT_TYPE}' "
            f"ConfrontationDef — fixture drift, expected the sealed-letter "
            f"dogfight definition (T1)"
        )

    snap = GameSnapshot(genre=GENRE_SLUG)
    snap.genre_slug = GENRE_SLUG
    # Epic 94 production shape: the dogfight weapon lookup goes through
    # resolve_inventory(pack, snapshot.world_slug) — binding the fixture
    # world exercises the world-tier REPLACE path, like live migrated packs.
    snap.world_slug = WORLD_SLUG

    # Seed the player character so the SWN shot-resolution path in
    # narration_apply can look up pc_char.stats (Reflex/Intellect modifiers
    # feed the to-hit arithmetic). Matches the _make_pilot pattern in
    # test_sealed_letter_dispatch_integration.py: both attrs at 10
    # (modifier=0) so arithmetic is deterministic. pilot_skill /
    # attack_bonus fall back to authored player_default_stats — not a
    # silent fallback, that's the authored MVP default.
    snap.characters = [
        Character(
            core=CreatureCore(
                name=player_pilot_name,
                description="Playtest pilot.",
                personality="Calm.",
            ),
            backstory="A pilot.",
            char_class="Pilot",
            race="Human",
            stats={"Reflex": 10, "Intellect": 10},
        )
    ]

    # Story 59-17: instantiate through the LIVE production primitive.
    # Confrontation engagement is router-driven (Story 59-4 / ADR-113); the
    # narrator-initiated instantiation block inside
    # ``_apply_narration_result_to_snapshot`` was removed, so the old
    # ``confrontation=DOGFIGHT_TYPE`` call no longer seats an encounter
    # (that was the 59-17 repro: ``snap.encounter`` stayed None). Drive the
    # same instantiation primitive the router dispatch calls
    # (``run_confrontation_dispatch`` → ``instantiate_encounter_from_trigger``)
    # so role tagging (red/blue per T3) and instantiation-time wiring fire.
    # We pass the opponent explicitly (the rarer router-with-mentions path);
    # the location-fallback path is covered by
    # ``test_dogfight_instantiation_production_path.py``.
    instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type=DOGFIGHT_TYPE,
        player_name=player_pilot_name,
        npcs_present=[
            NpcMention(
                name=opponent_pilot_name,
                role="hostile",
                side="opponent",
            ),
        ],
        genre_slug=GENRE_SLUG,
    )

    enc = snap.encounter
    assert enc is not None, "instantiation failed to set snap.encounter"
    assert enc.encounter_type == DOGFIGHT_TYPE, (
        f"expected encounter_type={DOGFIGHT_TYPE!r}, got {enc.encounter_type!r}"
    )
    roles = sorted(a.role for a in enc.actors)
    assert roles == ["blue", "red"], (
        f"expected red+blue role tags from sealed-letter instantiation, got {roles!r}"
    )

    return snap, cdef, pack


def drive_dogfight_turn(
    snapshot: GameSnapshot,
    *,
    red_maneuver: str,
    blue_maneuver: str,
    pack: GenrePack,
    narration: str = "Maneuver.",
) -> SealedLetterOutcome:
    """Drive one dogfight resolution turn through the production path.

    Builds a ``NarrationTurnResult`` with BeatSelections for BOTH the red
    (player) and blue (opponent) actors — per the T3 implementer note,
    omitting either side leaves that role un-committed and the resolver
    raises. Routes through ``_apply_narration_result_to_snapshot`` so any
    future dispatch wiring (logging, watcher events, span enrichment)
    fires for free.

    Args:
        snapshot: GameSnapshot from ``make_dogfight_playtest_state``. The
            encounter's ``per_actor_state`` is mutated in place.
        red_maneuver: Maneuver id to commit for the red (player) actor.
            Must be in the ConfrontationDef's
            ``interaction_table.maneuvers_consumed``.
        blue_maneuver: Maneuver id to commit for the blue (opponent) actor.
            Same legal set as red.
        pack: GenrePack returned from ``make_dogfight_playtest_state`` —
            required by the production dispatch path.
        narration: Narration text for the turn. Defaults to a placeholder;
            callers driving descriptive playtests can pass real prose.

    Returns:
        The ``SealedLetterOutcome`` produced by the dispatch path — this
        carries the resolved cell name, narration hint, and the real
        ``extend_and_return_triggered`` flag (read straight from the
        resolver, not reconstructed). The same hint is also pushed onto
        ``encounter.narrator_hints`` (replacing prior, per T5 fix).

    Raises:
        ValueError: maneuver not in the legal ``maneuvers_consumed`` set
            for the encounter, or the snapshot has no active dogfight
            encounter, or the production resolver rejects the commits.
        RuntimeError: dispatch did not invoke the sealed-letter resolver
            (e.g., encounter resolution_mode drifted) — this is a wiring
            failure for the playtest fixture and surfaces loudly.
        KeyError: legal maneuvers but no interaction cell matches the
            (red, blue) pair (content gap — surface loudly).
    """
    enc = snapshot.encounter
    if enc is None:
        raise ValueError(
            "snapshot has no active encounter — call make_dogfight_playtest_state first"
        )
    if enc.encounter_type != DOGFIGHT_TYPE:
        raise ValueError(
            f"snapshot encounter is {enc.encounter_type!r}, expected "
            f"{DOGFIGHT_TYPE!r} — wrong fixture for this turn driver"
        )

    # Front-load maneuver validation so callers get a clean ValueError
    # before any dispatch / OTEL side effects occur. The dispatch resolver
    # ALSO validates (defense in depth), but failing here keeps the OTEL
    # trace clean for genuine engine errors vs. caller bugs.
    confrontations = pack.rules.confrontations if pack.rules else []
    cdef = find_confrontation_def(confrontations, DOGFIGHT_TYPE)
    if cdef is None or cdef.interaction_table is None:
        raise ValueError("loaded pack has no dogfight interaction_table — content drift")
    legal = set(cdef.interaction_table.maneuvers_consumed)
    if red_maneuver not in legal:
        raise ValueError(
            f"red_maneuver {red_maneuver!r} not in maneuvers_consumed (legal: {sorted(legal)})"
        )
    if blue_maneuver not in legal:
        raise ValueError(
            f"blue_maneuver {blue_maneuver!r} not in maneuvers_consumed (legal: {sorted(legal)})"
        )

    red_actor = next(a for a in enc.actors if a.role == "red")
    blue_actor = next(a for a in enc.actors if a.role == "blue")

    apply_outcome = _apply_narration_result_to_snapshot(
        snapshot,
        NarrationTurnResult(
            narration=narration,
            beat_selections=[
                BeatSelection(actor=red_actor.name, beat_id=red_maneuver),
                BeatSelection(actor=blue_actor.name, beat_id=blue_maneuver),
            ],
        ),
        player_name=red_actor.name,
        pack=pack,
        room=room_for(snapshot),
    )

    # The dispatch path now returns the SealedLetterOutcome it built
    # internally — no need to re-implement cell lookup here. If the
    # outcome is None, the dispatch silently took a different branch
    # (no encounter, wrong resolution_mode, etc.) which is a wiring
    # bug for the playtest fixture: surface it loudly per CLAUDE.md.
    sl_outcome = apply_outcome.sealed_letter
    if sl_outcome is None:
        raise RuntimeError(
            "dispatch did not invoke the sealed-letter resolver — encounter "
            f"resolution_mode is not sealed_letter_lookup, or the encounter "
            f"was not active. Encounter type: {enc.encounter_type!r}"
        )
    return sl_outcome
