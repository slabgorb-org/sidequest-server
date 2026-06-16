"""Opposed-check resolution — both sides roll, tier from the shift.

Spec: ``.archive/handoffs/opposed-checks-design.md`` (Architect, 2026-04-26).

The legacy ``beat_selection`` resolution mode rolls only the player's d20 and
lets the narrator pick the opponent's outcome tier from prose. Combat ran
through that path: the player rolled, the opponent narrated, the dial moved
whatever direction the LLM picked. Combat felt structurally unfair because it
*was* — a Challenge resolver was doing a Conflict's job.

This module is the third branch (``ResolutionMode.opposed_check``):

1. Both sides roll d20 + ability modifier.
2. ``shift = player_roll_with_mod - opponent_roll_with_mod``.
3. Tier comes from the shift bands (calibrated per ADR-093)::

       shift >= +10  → CritSuccess
       shift >= +2   → Success
       shift in [-1, +1] → Tie
       shift <= -2   → Fail
       shift <= -10  → CritFail

4. The derived tier feeds the existing ``apply_beat()`` once for each side.

Stat sourcing (player side reuses the existing dice path; opponent side is
the new contract):

- Look in ``EncounterActor.per_actor_state["stats"]`` first (per-instance).
- Fall back to ``ConfrontationDef.opponent_default_stats[<stat_check>]``.
- Hard-fail-loud if neither carries the stat. **No silent zero defaults**
  (CLAUDE.md no-silent-fallback rule).

Modifier formula matches the dice dispatcher: ``floor((score - 10) / 2)``.

OTEL: callers MUST emit ``encounter.opposed_roll_resolved`` BEFORE applying
beats (``encounter_opposed_roll_resolved_span``). That span is the lie-
detector for "did the engine actually run an opposed check or did the
narrator backslide into picking the tier."
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sidequest.game.beat_kinds import EdgeResolver, numerical_advantage_for
from sidequest.game.encounter import EncounterActor, StructuredEncounter
from sidequest.protocol.dice import RollOutcome

# Shift bands. Inclusive thresholds. Order matters in ``_tier_from_shift``:
# CritSuccess and CritFail are checked first because the bands are nested
# (any +10 is also a +2, any -10 is also a -2). Calibrated per ADR-093:
# tie band narrowed from ±2 to ±1 to reduce the inert-tie rate and let
# the player's mod advantage actually move the dial.
_CRIT_SUCCESS_SHIFT = 10
_SUCCESS_SHIFT = 2
_FAIL_SHIFT = -2
_CRIT_FAIL_SHIFT = -10


def _tier_from_shift(shift: int) -> RollOutcome:
    """Map a numeric shift to a ``RollOutcome`` per the calibrated bands (ADR-093).

    Bands are::

        shift >= +10  → CritSuccess
        shift >= +2   → Success
        shift in [-1, +1] → Tie
        shift <= -2   → Fail
        shift <= -10  → CritFail
    """
    if shift >= _CRIT_SUCCESS_SHIFT:
        return RollOutcome.CritSuccess
    if shift >= _SUCCESS_SHIFT:
        return RollOutcome.Success
    if shift <= _CRIT_FAIL_SHIFT:
        return RollOutcome.CritFail
    if shift <= _FAIL_SHIFT:
        return RollOutcome.Fail
    return RollOutcome.Tie


def _ability_modifier(score: int) -> int:
    """D&D-style modifier: ``floor((score - 10) / 2)``.

    Mirrors ``sidequest.server.dispatch.dice._stat_modifier`` so a stat
    score sourced from a character sheet and one sourced from the genre
    pack's ``opponent_default_stats`` produce the same modifier.
    """
    return (score - 10) // 2


@dataclass(frozen=True)
class OpposedRollResult:
    """Engine-derived outcome of one opposed-check resolution.

    Carries everything the GM panel needs to audit the resolution
    (mechanically and narratively). The ``tier`` value is what gets fed
    to ``apply_beat`` for both sides.

    ``player_num_advantage`` / ``opponent_num_advantage`` carry the
    side-aggregate shift bonus from numerical advantage (Step 3 of the
    numerical-advantage design). Both default to 0 when the caller
    didn't supply an ``edge_resolver``. They're added into ``shift``;
    the ``mod`` fields remain the per-actor stat modifier so the GM
    panel can show stat-bonus and side-bonus separately.
    """

    player_roll: int
    player_mod: int
    opponent_roll: int
    opponent_mod: int
    shift: int
    tier: RollOutcome
    player_num_advantage: int = 0
    opponent_num_advantage: int = 0


def _stat_score_from_actor(
    actor: EncounterActor,
    stat_check: str,
) -> int | None:
    """Return the actor's stat score from ``per_actor_state['stats']``.

    Returns ``None`` when the per-actor block lacks the stat — caller
    falls back to the cdef-level map. Lookup is case-insensitive (genre
    packs use mixed conventions: STR / Strength / Strength / Reflex).
    """
    pas = actor.per_actor_state or {}
    stats = pas.get("stats")
    if not isinstance(stats, dict):
        return None
    if stat_check in stats:
        return int(stats[stat_check])
    for k, v in stats.items():
        if isinstance(k, str) and k.lower() == stat_check.lower():
            return int(v)
    return None


def _stat_score_from_cdef_default(
    opponent_default_stats: dict[str, int] | None,
    stat_check: str,
) -> int | None:
    """Return the stat score from the cdef's ``opponent_default_stats``.

    Returns ``None`` when the map is unset or the stat isn't in it.
    """
    if not opponent_default_stats:
        return None
    if stat_check in opponent_default_stats:
        return int(opponent_default_stats[stat_check])
    for k, v in opponent_default_stats.items():
        if isinstance(k, str) and k.lower() == stat_check.lower():
            return int(v)
    return None


def resolve_opponent_modifier(
    *,
    actor: EncounterActor,
    cdef: Any,  # ConfrontationDef — typed Any to dodge import cycle
    stat_check: str,
) -> int:
    """Resolve the opponent's ability modifier for ``stat_check``.

    Walks ``actor.per_actor_state['stats']`` first, then
    ``cdef.opponent_default_stats``. Hard-fails with a clear message if
    neither carries the stat — CLAUDE.md no-silent-fallback rule. The
    raised ``ValueError`` is the loud surface; callers should let it
    propagate (no zero default).

    Returns the integer modifier (``floor((score - 10) / 2)``).
    """
    if not stat_check:
        raise ValueError(
            "opposed_check resolution requires a non-empty stat_check on "
            "the opponent's beat — pack data error"
        )

    score = _stat_score_from_actor(actor, stat_check)
    if score is None:
        score = _stat_score_from_cdef_default(
            getattr(cdef, "opponent_default_stats", None),
            stat_check,
        )
    if score is None:
        cdef_default = getattr(cdef, "opponent_default_stats", None) or {}
        cdef_keys = sorted(cdef_default.keys()) if cdef_default else []
        per_actor_keys = sorted((actor.per_actor_state or {}).get("stats", {}).keys())
        raise ValueError(
            f"opposed_check: no stat {stat_check!r} for opponent "
            f"{actor.name!r} — neither per_actor_state.stats "
            f"({per_actor_keys}) nor cdef.opponent_default_stats "
            f"({cdef_keys}) carries it. Pack must declare the stat in "
            f"opponent_default_stats or instantiate the actor with the "
            f"stat in per_actor_state.stats."
        )
    return _ability_modifier(score)


def resolve_opposed_check(
    *,
    player_actor: EncounterActor,
    opponent_actor: EncounterActor,
    player_beat: Any,  # BeatDef
    opponent_beat: Any,  # BeatDef
    cdef: Any,  # ConfrontationDef
    player_roll: int,
    opponent_roll: int,
    encounter: StructuredEncounter | None = None,
    edge_resolver: EdgeResolver | None = None,
    fixed_opponent_roll: int | None = None,
) -> OpposedRollResult:
    """Run an opposed-check resolution.

    Both rolls must be raw d20 face values in ``1..=20``. Modifiers are
    sourced as follows:

    - Player: ``player_actor.per_actor_state['stats'][player_beat.stat_check]``
      first, then ``cdef.opponent_default_stats`` (rare — players usually
      carry their own stat block; this fallback exists so the
      symmetry of the resolver doesn't crash on a legacy fixture that
      stuffed the player's stats only on the cdef).
    - Opponent: same pair, walked in the same order.

    Both stat-check resolutions hard-fail-loud if neither source carries
    the stat (CLAUDE.md no-silent-fallback).

    The tier is derived from the shift between the modified rolls and
    fed back via ``OpposedRollResult.tier``. Callers (the dispatch
    branch) are responsible for emitting the OTEL span and then calling
    ``apply_beat`` once per side using ``tier``.

    ``encounter`` is currently unused; carried in the signature so the
    resolver can grow contextual logic (tag invocations, fleeting
    modifiers, etc.) without a call-site refactor.

    ``fixed_opponent_roll``: when set, overrides ``opponent_roll`` with
    the given value as the opponent's d20 face. Used by the save
    resolver (``sidequest.game.saves``) to pin the threat's "roll" to
    the B/X B26 table target — the threat does not roll randomly; the
    table value IS its deterministic d20. Validated to ``1..20``.
    """
    if fixed_opponent_roll is not None:
        if not (1 <= fixed_opponent_roll <= 20):
            raise ValueError(f"fixed_opponent_roll {fixed_opponent_roll} not in 1..20")
        opponent_roll = fixed_opponent_roll

    if not (1 <= player_roll <= 20):
        raise ValueError(
            f"opposed_check: player_roll {player_roll} not in 1..20 — d20 face value required"
        )
    if not (1 <= opponent_roll <= 20):
        raise ValueError(
            f"opposed_check: opponent_roll {opponent_roll} not in 1..20 — d20 face value required"
        )

    player_stat = getattr(player_beat, "stat_check", None)
    opponent_stat = getattr(opponent_beat, "stat_check", None)

    player_mod = resolve_opponent_modifier(
        actor=player_actor,
        cdef=cdef,
        stat_check=player_stat,
    )
    opponent_mod = resolve_opponent_modifier(
        actor=opponent_actor,
        cdef=cdef,
        stat_check=opponent_stat,
    )

    # Numerical advantage (Step 3): when an edge_resolver is provided AND
    # an encounter is available, fold each side's swarm-pressure modifier
    # into the shift. The modifiers default to 0 when either input is
    # missing — back-compat for fixtures that don't seed cores.
    player_num_adv = 0
    opponent_num_adv = 0
    if edge_resolver is not None and encounter is not None:
        player_num_adv = numerical_advantage_for(player_actor, encounter, edge_resolver)
        opponent_num_adv = numerical_advantage_for(opponent_actor, encounter, edge_resolver)

    shift = (player_roll + player_mod + player_num_adv) - (
        opponent_roll + opponent_mod + opponent_num_adv
    )
    tier = _tier_from_shift(shift)

    return OpposedRollResult(
        player_roll=player_roll,
        player_mod=player_mod,
        opponent_roll=opponent_roll,
        opponent_mod=opponent_mod,
        shift=shift,
        tier=tier,
        player_num_advantage=player_num_adv,
        opponent_num_advantage=opponent_num_adv,
    )
