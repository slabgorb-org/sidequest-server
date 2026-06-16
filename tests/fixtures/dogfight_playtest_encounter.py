"""Reusable playtest fixture for the space_opera dogfight engine (T7).

A small helper module that constructs a live, real-content space_opera
dogfight encounter wired through the production dispatch path, and
exposes a clean API for driving turns. Used by integration tests and by
manual playtest drivers.

This is the "smallest viable playtest scaffold" demonstrating the full
T1-T6 dogfight engine working end-to-end:
    - Real content load (sidequest-content/genre_packs/space_opera/)
    - Production instantiation (assigns role=red/blue per T3)
    - Production dispatch (_apply_narration_result_to_snapshot, T5)
    - Sealed-letter resolution (per_actor_state mutation, OTEL spans)

Per CLAUDE.md no-silent-fallbacks:
    - Missing sidequest-content raises FileNotFoundError (callers can
      pytest.skip on it).
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
from sidequest.game.session import GameSnapshot
from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import ConfrontationDef
from sidequest.server.dispatch.confrontation import find_confrontation_def
from sidequest.server.dispatch.sealed_letter import SealedLetterOutcome
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from tests._helpers.session_room import room_for

# Default location of sidequest-content alongside sidequest-server. Mirrors
# the path-walk in test_sealed_letter_dispatch_integration.py so that
# subrepo layouts (oq-1, oq-2) resolve identically.
DEFAULT_CONTENT_ROOT = (
    Path(__file__).resolve().parents[2].parent / "sidequest-content" / "genre_packs"
)

GENRE_SLUG = "space_opera"
DOGFIGHT_TYPE = "dogfight"


def make_dogfight_playtest_state(
    *,
    player_pilot_name: str = "Maverick",
    opponent_pilot_name: str = "Vulture",
    pack_root: Path | None = None,
) -> tuple[GameSnapshot, ConfrontationDef, GenrePack]:
    """Construct a fresh GameSnapshot with an active space_opera dogfight.

    Loads real content from ``sidequest-content/genre_packs/space_opera/``
    via the production loader, then drives a single instantiation turn
    through ``_apply_narration_result_to_snapshot`` so the encounter is
    built by the same code path the running server uses.

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
        FileNotFoundError: ``sidequest-content`` is not on disk at the
            expected location. Callers in test contexts should
            ``pytest.skip(...)`` on this rather than papering over it.
        ValueError: Loaded space_opera pack lacks a dogfight ConfrontationDef
            (content drift — surface loudly per CLAUDE.md).
        AssertionError: Production instantiation didn't produce the expected
            two-actor red/blue encounter (engine drift — caller wants to know).

    Side effects:
        - Loads space_opera genre pack from disk.
        - Drives one narration turn through the production dispatch path
          to instantiate the encounter (this is what makes per_actor_state
          start at ``{}`` — matching production behavior, not pre-seeded).
    """
    root = pack_root if pack_root is not None else DEFAULT_CONTENT_ROOT
    pack_path = root / GENRE_SLUG
    if not pack_path.is_dir():
        raise FileNotFoundError(
            f"space_opera genre pack not found at {pack_path} — "
            f"sidequest-content checkout missing or pack_root wrong"
        )

    pack = load_genre_pack(pack_path)
    confrontations = pack.rules.confrontations if pack.rules else []
    cdef = find_confrontation_def(confrontations, DOGFIGHT_TYPE)
    if cdef is None:
        raise ValueError(
            f"space_opera pack at {pack_path} has no '{DOGFIGHT_TYPE}' "
            f"ConfrontationDef — content drift, expected the sealed-letter "
            f"dogfight definition (T1)"
        )

    snap = GameSnapshot(genre=GENRE_SLUG)
    snap.genre_slug = GENRE_SLUG

    # Drive instantiation through the production path so role tagging
    # (red/blue per T3) and any other instantiation-time wiring fire.
    _apply_narration_result_to_snapshot(
        snap,
        NarrationTurnResult(
            narration=(
                f"{player_pilot_name} pushes the throttle as "
                f"{opponent_pilot_name} screams in on the merge."
            ),
            confrontation=DOGFIGHT_TYPE,
            npcs_present=[
                NpcMention(
                    name=opponent_pilot_name,
                    role="hostile",
                    side="opponent",
                ),
            ],
        ),
        player_name=player_pilot_name,
        pack=pack,
        room=room_for(snap),
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
