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

from dataclasses import dataclass, field
from typing import Any

from sidequest.game.dogfight_shot import GunSolution, resolve_geometry_modifier
from sidequest.game.encounter import EncounterActor, StructuredEncounter
from sidequest.genre.models.rules import (
    GeometryModifiers,
    InteractionCell,
    InteractionTable,
    SwnConfig,
)
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
    narration hint, whether the extend-and-return rule fired, and any
    gun solutions detected for this cell (populated only when SWN kwargs
    are supplied to ``resolve_sealed_letter_lookup``).
    """

    cell_name: str
    red_maneuver: str
    blue_maneuver: str
    narration_hint: str
    extend_and_return_triggered: bool = False
    gun_solutions: list[GunSolution] = field(default_factory=list)


def resolve_sealed_letter_lookup(
    encounter: StructuredEncounter,
    commits: dict[str, str],
    table: InteractionTable,
    *,
    geometry_modifiers: GeometryModifiers | None = None,
    shot_inputs: dict[str, dict[str, Any]] | None = None,
    swn_cfg: SwnConfig | None = None,
    commit_sources: dict[str, str] | None = None,
    blue_attitude: str = "",
) -> SealedLetterOutcome:
    """Resolve a sealed-letter lookup turn.

    Given committed maneuvers (keyed by actor role: "red" / "blue") and
    an interaction table, looks up the cross-product cell, applies
    ``red_view`` / ``blue_view`` descriptor deltas to each actor's
    ``per_actor_state``, optionally fires the extend-and-return rule,
    and emits OTEL spans bracketing the pipeline.

    The optional SWN kwargs (``geometry_modifiers``, ``shot_inputs``,
    ``swn_cfg``) are all-or-nothing: when all three are supplied, the
    resolver also detects which actors scored a ``gun_solution`` this cell
    and computes the SWN ship-gunnery params (``AttackRollParams`` + weapon
    / armor inputs) needed for the subsequent dice roll. Results are
    returned as ``GunSolution`` objects on ``SealedLetterOutcome.gun_solutions``.
    When any of the three is omitted (backward-compat callers), gun solution
    detection is skipped and ``gun_solutions`` is empty.

    Args:
        encounter: The active StructuredEncounter (mutated in place — actor
            ``per_actor_state`` is merged with the cell views).
        commits: Mapping of role -> committed maneuver. Must contain both
            "red" and "blue"; each value must be in
            ``table.maneuvers_consumed``.
        table: The InteractionTable to look up the (red, blue) pair in.
        geometry_modifiers: Aspect/range modifier table for SWN gunnery.
            Required when ``shot_inputs`` / ``swn_cfg`` are supplied.
        shot_inputs: Per-role dict of SWN gunnery params. Each value must
            carry ``attacker_stats``, ``pilot_skill``, ``attack_bonus``,
            ``target_ac``, ``target_armor``, ``weapon`` (DamageSpec), and
            ``weapon_name``. Must include an entry for every role that
            scores a ``gun_solution`` — missing entries raise ValueError
            (no silent skip per CLAUDE.md).
        swn_cfg: RulesConfig-compatible object exposing ``attribute_map``
            (SWN attr name → flavor stat name mapping).

    Returns:
        SealedLetterOutcome carrying cell metadata, the extend-and-return
        flag, and (when SWN kwargs supplied) any gun solutions detected.

    Raises:
        ValueError: ``commits`` is missing the "red" or "blue" key, the
            committed maneuver is not in ``table.maneuvers_consumed``, the
            encounter has no actor for one of the required roles, or a
            shooter has a ``gun_solution`` but no ``shot_inputs`` entry.
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

    # ADR-153 §4: stamp the opponent-brain stance source (narrator | fallback |
    # substituted) so the GM panel can tell whether the narrator or the engine
    # chose each maneuver; the blue commit also carries the motivating attitude.
    _sources = commit_sources or {}
    with dogfight_maneuver_committed_span(
        actor=red_actor.name,
        maneuver=red_maneuver,
        role=ROLE_RED,
        source=_sources.get(ROLE_RED, "narrator"),
    ):
        pass
    _blue_attrs: dict[str, Any] = {"source": _sources.get(ROLE_BLUE, "narrator")}
    if blue_attitude:
        _blue_attrs["attitude"] = blue_attitude
    with dogfight_maneuver_committed_span(
        actor=blue_actor.name,
        maneuver=blue_maneuver,
        role=ROLE_BLUE,
        **_blue_attrs,
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

    # ---- Step 6: detect gun solutions + compute SWN gunnery params ----
    # The three SWN kwargs are ALL-OR-NOTHING (use ``is not None`` — an empty
    # shot_inputs dict is still "provided", so it must not be treated as absent):
    #   - none provided  -> backward-compat path, gun_solutions stays empty.
    #   - all provided   -> run detection.
    #   - some-but-not-all -> loud ValueError (a partial wire is a config bug, not
    #     a silent no-op — CLAUDE.md no-silent-fallbacks).
    swn_kwargs = {
        "geometry_modifiers": geometry_modifiers,
        "shot_inputs": shot_inputs,
        "swn_cfg": swn_cfg,
    }
    provided = {name for name, value in swn_kwargs.items() if value is not None}
    gun_solutions: list[GunSolution] = []
    if provided and provided != set(swn_kwargs):
        missing = sorted(set(swn_kwargs) - provided)
        raise ValueError(
            "dogfight SWN resolution requires geometry_modifiers, shot_inputs, and "
            f"swn_cfg together; missing: {missing}"
        )
    if provided:
        # All three present (the partial case raised above). Narrow for the type
        # checker — the dict values are still Optional in the annotation.
        assert geometry_modifiers is not None
        assert shot_inputs is not None
        assert swn_cfg is not None
        from sidequest.game.ruleset.swn import SwnRulesetModule

        swn = SwnRulesetModule()
        role_actor = {ROLE_RED: red_actor, ROLE_BLUE: blue_actor}
        for shooter_role, shooter in role_actor.items():
            if not bool(shooter.per_actor_state.get("gun_solution")):
                continue
            inp = shot_inputs.get(shooter_role)
            if inp is None:
                raise ValueError(
                    f"actor role={shooter_role!r} has a gun_solution but no shot_inputs "
                    "entry — dispatch must supply SWN params for every shooter (no silent skip)"
                )
            target_role = ROLE_BLUE if shooter_role == ROLE_RED else ROLE_RED
            geo = resolve_geometry_modifier(shooter.per_actor_state, geometry_modifiers)
            attack = swn.ship_attack_params(
                attacker_stats=inp["attacker_stats"],
                pilot_skill=inp["pilot_skill"],
                attack_bonus=inp["attack_bonus"],
                geometry_modifier=geo,
                target_ac=inp["target_ac"],
                cfg=swn_cfg,
            )
            gun_solutions.append(
                GunSolution(
                    shooter_role=shooter_role,
                    shooter_name=shooter.name,
                    target_role=target_role,
                    target_name=role_actor[target_role].name,
                    attack=attack,
                    weapon=inp["weapon"],
                    weapon_name=inp["weapon_name"],
                    target_armor=int(inp["target_armor"]),
                    geometry_modifier=geo,
                )
            )

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
        gun_solutions=gun_solutions,
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
