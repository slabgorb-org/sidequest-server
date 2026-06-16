"""Sealed-letter lookup resolution handler (T3 of the dogfight port).

Port of ``sidequest-api/crates/sidequest-server/src/dispatch/sealed_letter.rs``.

Resolves simultaneous-commit encounters where two actors each commit a
maneuver privately, and the engine resolves via cross-product lookup in
an interaction table (ADR-077, Epic 38).

The handler is synchronous — async commit-gathering from a TurnBarrier
happens at the dispatch call site (T5), which passes the resolved
maneuvers as a ``dict[str, str]`` keyed by actor role ("red" / "blue").

Public surface:
    SealedLetterOutcome      — result dataclass
    resolve_sealed_letter_lookup(encounter, commits, table) — entry point

Errors raised (CLAUDE.md no-silent-fallbacks):
    ValueError — committed maneuvers missing 'red'/'blue' key, or the
                 chosen maneuver is not in ``table.maneuvers_consumed``.
    KeyError   — no interaction cell matches the (red, blue) pair.

OTEL spans emitted (see ``sidequest.telemetry.spans``):
    dogfight.confrontation_started   — handler entry
    dogfight.maneuver_committed      — twice (once per actor)
    dogfight.cell_resolved           — after lookup
"""

from __future__ import annotations

from dataclasses import dataclass

from sidequest.game.encounter import EncounterActor, StructuredEncounter
from sidequest.genre.models.rules import InteractionCell, InteractionTable
from sidequest.telemetry.spans import (
    dogfight_cell_resolved_span,
    dogfight_confrontation_started_span,
    dogfight_maneuver_committed_span,
)

# Handler-protocol identifiers (NOT content schema keys). The sealed-letter
# pipeline addresses actors by these role tags; content-side keys
# ("opening_fast", "gun_solution", etc.) stay inline because they belong
# to the descriptor schema, not the handler contract.
ROLE_RED = "red"
ROLE_BLUE = "blue"

# Merge starting state (from descriptor_schema.yaml). Used by the
# extend-and-return rule to reset geometric fields after the engagement
# breaks apart. Energy fields are intentionally NOT in this dict so they
# survive the reset.
# TODO(post-port): drive from descriptor_schema.starting_states
# (where id == "merge") instead of hardcoding. The dogfight port (T1-T7)
# left this hardcoded; the cleanup is mechanical but out of scope for the
# port itself. Until then, test_merge_starting_geometry_matches_descriptor_schema
# is the safety net that fails loudly if content and this constant drift apart.
_MERGE_STARTING_GEOMETRY: dict[str, object] = {
    "target_bearing": "12",
    "target_range": "close",
    "target_aspect": "head_on",
    "closure": "closing_fast",
    "gun_solution": False,
}


@dataclass
class SealedLetterOutcome:
    """Result of a sealed-letter lookup resolution.

    Carries the matched cell name, the committed maneuvers, the cell's
    narration hint, and whether the extend-and-return rule fired this
    resolution.
    """

    cell_name: str
    red_maneuver: str
    blue_maneuver: str
    narration_hint: str
    extend_and_return_triggered: bool = False


def resolve_sealed_letter_lookup(
    encounter: StructuredEncounter,
    commits: dict[str, str],
    table: InteractionTable,
) -> SealedLetterOutcome:
    """Resolve a sealed-letter lookup turn.

    Given committed maneuvers (keyed by actor role: "red" / "blue") and
    an interaction table, looks up the cross-product cell, applies
    ``red_view`` / ``blue_view`` descriptor deltas to each actor's
    ``per_actor_state``, optionally fires the extend-and-return rule,
    and emits OTEL spans bracketing the pipeline.

    Args:
        encounter: The active StructuredEncounter (mutated in place — actor
            ``per_actor_state`` is merged with the cell views).
        commits: Mapping of role -> committed maneuver. Must contain both
            "red" and "blue"; each value must be in
            ``table.maneuvers_consumed``.
        table: The InteractionTable to look up the (red, blue) pair in.

    Returns:
        SealedLetterOutcome carrying cell metadata and the
        extend-and-return flag.

    Raises:
        ValueError: ``commits`` is missing the "red" or "blue" key, the
            committed maneuver is not in ``table.maneuvers_consumed``, or
            the encounter has no actor for one of the required roles.
        KeyError: No interaction cell matches the (red, blue) pair (no
            silent fallback per CLAUDE.md).
    """
    # ---- Step 1: validate commits ----
    if ROLE_RED not in commits:
        raise ValueError(
            "committed maneuvers missing 'red' key — sealed-letter "
            "resolution requires both 'red' and 'blue' commits"
        )
    if ROLE_BLUE not in commits:
        raise ValueError(
            "committed maneuvers missing 'blue' key — sealed-letter "
            "resolution requires both 'red' and 'blue' commits"
        )
    red_maneuver = commits[ROLE_RED]
    blue_maneuver = commits[ROLE_BLUE]

    legal = set(table.maneuvers_consumed)
    if red_maneuver not in legal:
        raise ValueError(
            f"red maneuver {red_maneuver!r} not in maneuvers_consumed (legal: {sorted(legal)})"
        )
    if blue_maneuver not in legal:
        raise ValueError(
            f"blue maneuver {blue_maneuver!r} not in maneuvers_consumed (legal: {sorted(legal)})"
        )

    # ---- Step 2: validate actor presence (no silent fallback) ----
    # Both roles must be present BEFORE we emit any spans — otherwise the
    # GM panel sees a confrontation_started event with empty actor names
    # and silently-skipped delta application, which is exactly the kind of
    # "engine ran but did nothing" lie CLAUDE.md forbids.
    red_actor = _find_actor_by_role(encounter, ROLE_RED)
    blue_actor = _find_actor_by_role(encounter, ROLE_BLUE)
    if red_actor is None or blue_actor is None:
        missing = [
            role
            for role, actor in (
                (ROLE_RED, red_actor),
                (ROLE_BLUE, blue_actor),
            )
            if actor is None
        ]
        present_roles = sorted({a.role for a in encounter.actors})
        raise ValueError(
            f"sealed-letter encounter requires actors with role(s) {missing}; "
            f"found roles: {present_roles}"
        )

    # ---- OTEL: confrontation_started + per-actor maneuver_committed ----
    with dogfight_confrontation_started_span(
        encounter_type=encounter.encounter_type,
        red_actor=red_actor.name,
        blue_actor=blue_actor.name,
    ):
        pass

    with dogfight_maneuver_committed_span(
        actor=red_actor.name,
        maneuver=red_maneuver,
        role=ROLE_RED,
    ):
        pass
    with dogfight_maneuver_committed_span(
        actor=blue_actor.name,
        maneuver=blue_maneuver,
        role=ROLE_BLUE,
    ):
        pass

    # ---- Step 3: cell lookup ----
    cell = _find_cell(table, red_maneuver, blue_maneuver)
    if cell is None:
        raise KeyError(
            f"no interaction cell for maneuver pair ({red_maneuver!r}, {blue_maneuver!r}) in table"
        )

    # ---- Step 4: apply view deltas to per_actor_state ----
    # InteractionCell.red_view / blue_view are ``Any`` from pydantic — YAML
    # mappings come through as native dicts (PyYAML safe_load), so we don't
    # need a yaml→json converter the way the Rust source does. Verified
    # empirically by the wiring test against the real space_opera content.
    _apply_view_deltas(red_actor, cell.red_view)
    _apply_view_deltas(blue_actor, cell.blue_view)

    # ---- Step 5: maybe extend-and-return ----
    extend_triggered = _maybe_apply_extend_and_return(encounter, cell)

    # ---- OTEL: cell_resolved ----
    with dogfight_cell_resolved_span(
        cell_name=cell.name,
        shape=cell.shape,
        red_maneuver=red_maneuver,
        blue_maneuver=blue_maneuver,
        extend_and_return_triggered=extend_triggered,
    ):
        pass

    return SealedLetterOutcome(
        cell_name=cell.name,
        red_maneuver=red_maneuver,
        blue_maneuver=blue_maneuver,
        narration_hint=cell.narration_hint,
        extend_and_return_triggered=extend_triggered,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _find_actor_by_role(
    encounter: StructuredEncounter,
    role: str,
) -> EncounterActor | None:
    """Return the first actor with the matching role, or None.

    Sealed-letter encounters tag actors with role="red"/"blue" (the
    Rust source convention). Callers that miss a role are expected to
    surface that as a content / dispatch wiring error at T5; this helper
    just reports absence.
    """
    for actor in encounter.actors:
        if actor.role == role:
            return actor
    return None


def _find_cell(
    table: InteractionTable,
    red_maneuver: str,
    blue_maneuver: str,
) -> InteractionCell | None:
    """Linear scan for the cell whose ``pair == [red, blue]``.

    The InteractionTable schema (rules.py) stores each pair as a 2-element
    list rather than a tuple, so we compare element-wise.
    """
    for cell in table.cells:
        if cell.pair[0] == red_maneuver and cell.pair[1] == blue_maneuver:
            return cell
    return None


def _apply_view_deltas(actor: EncounterActor, view: object) -> None:
    """Merge ``view`` into ``actor.per_actor_state``.

    A None view is a legitimate "no state change" signal (matches the
    Rust ``serde_yaml::Value::Null`` branch). A dict view has its keys
    inserted/overwritten while preserving keys not in the view.

    Non-mapping, non-None views are content authoring errors and raise
    TypeError — no silent fallback per CLAUDE.md. The caller (T5
    dispatch) is expected to surface this loudly to the GM panel.
    """
    if view is None:
        return
    if not isinstance(view, dict):
        raise TypeError(
            f"interaction cell view for actor role={actor.role!r} is "
            f"{type(view).__name__}, expected dict — content error"
        )
    for key, value in view.items():
        if not isinstance(key, str):
            raise TypeError(
                f"interaction cell view for actor role={actor.role!r} "
                f"has non-string key {key!r} — content error"
            )
        actor.per_actor_state[key] = value


def _maybe_apply_extend_and_return(
    encounter: StructuredEncounter,
    cell: InteractionCell,
) -> bool:
    """Apply the extend-and-return rule (Story 38-8).

    After deltas are applied, if no actor scored a hit (``gun_solution``
    is falsy across the board) AND at least one actor has
    ``closure == "opening_fast"``, the engagement has broken apart:
    reset every actor's geometric descriptor fields to the merge starting
    state. Energy fields (``viewer_energy``, ``target_energy``) are
    preserved.

    The Rust source keys "no hit" on the resolved per_actor_state, not on
    the cell ``shape`` text — preserved here for parity (a cell that
    sets ``gun_solution=true`` for either actor suppresses the reset
    regardless of how the cell is labeled).

    Returns:
        True if the reset fired, False otherwise.
    """
    any_hit = any(bool(actor.per_actor_state.get("gun_solution")) for actor in encounter.actors)
    if any_hit:
        return False

    any_opening_fast = any(
        actor.per_actor_state.get("closure") == "opening_fast" for actor in encounter.actors
    )
    if not any_opening_fast:
        return False

    for actor in encounter.actors:
        for key, value in _MERGE_STARTING_GEOMETRY.items():
            actor.per_actor_state[key] = value

    return True
