"""Beat kinds + per-kind default delta tables.

Spec: docs/superpowers/specs/2026-04-25-dual-track-momentum-design.md
§"Beat kinds and outcome tiers".

A beat declares one of four ``kind`` values; the kind drives a default delta
table indexed by ``RollOutcome``. A beat can override any per-tier entry
via its ``deltas:`` map. ``resolve_tier_deltas`` merges the kind defaults
with per-beat overrides and returns a flat ``ResolvedDeltas`` consumed by
``_apply_beat``.

All deltas are *signed* and measured against the actor's own/other dials.
``brace`` drains the opponent's dial; that is encoded as a negative
``opponent`` delta so ``opponent.current += deltas.opponent`` is the only
arithmetic the engine needs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Literal

from sidequest.protocol.dice import RollOutcome


class BeatKind(str, Enum):  # noqa: UP042 — matches project convention (see protocol/enums.py)
    """Mechanical contract for a beat.

    - strike: advance own dial / press opponent.
    - brace:  absorb / counter — drains opponent dial.
    - push:   pursue a discrete narrative goal (flee, climb, persuade-out).
    - angle:  set up a scene tag for future leverage.
    """

    strike = "strike"
    brace = "brace"
    push = "push"
    angle = "angle"


@dataclass(frozen=True)
class ResolvedDeltas:
    """Flat deltas resolved for one beat at one outcome tier.

    ``own``/``opponent`` are scalar dial advances. Tag/resolution extras
    are independent flags the engine consults after applying the dials.
    """

    own: int = 0
    opponent: int = 0
    grants_tag: str | None = None
    tag_leverage: int = 0
    grants_fleeting_tag: str | None = None
    tag_backfire: bool = False
    resolution: bool = False


# Closed set of beat-impact categories (Story 73-4). The UI mirrors this as a
# TypeScript union (ConfrontationOverlay.tsx ``BeatEffect``); keeping both as a
# fixed enumeration means a renamed/typo'd category fails type-check on both
# sides instead of silently producing a dead ``beat-impact-${effect}`` class.
BeatEffect = Literal["advance", "setback", "resolution", "tag", "backfire", "inert"]


@dataclass(frozen=True)
class BeatImpact:
    """Player-facing semantic readout of one resolved beat (Story 73-4).

    The dial math is correct but illegible: a ``push`` CritSuccess intentionally
    moves no dial (``own=0``/``opponent=0``, ``resolution=True``, fleeting
    "Clean Exit"), and a mechanics-first player (Sebastien / Jade) reads the 0 as
    a broken roll. This descriptor classifies the *resolved* deltas into a single
    ``effect`` category plus a human-legible ``summary`` so the UI can render
    "clean exit, by design" instead of a bare 0.

    Single source of truth for kind+tier semantics (SOUL: "legible in
    player-facing surfaces"). NOT a dev/OTEL artifact — the existing
    ``beat_no_op`` / ``beat_applied`` watcher emits cover the dev side.

    ``effect`` is one of: ``advance`` (a dial moved in the actor's favor),
    ``setback`` (a dial moved against the actor), ``resolution`` (the beat ends
    the confrontation, no dial change by design), ``tag`` (a scene tag was
    granted, no dial change by design), ``backfire`` (an angle rebounded), or
    ``inert`` (the beat landed but nothing happened — a genuine Fail).
    """

    effect: BeatEffect
    dial_moved: bool
    summary: str
    own: int = 0
    opponent: int = 0
    resolution: bool = False
    tag: str | None = None


def describe_beat_impact(
    deltas: ResolvedDeltas,
    *,
    kind: BeatKind,
    outcome: RollOutcome,
    hp_depletion_suppressed: bool = False,
    hp_removed: int = 0,
) -> BeatImpact:
    """Classify *resolved* deltas into a legible :class:`BeatImpact` (Story 73-4).

    Reads the resolved deltas (so per-tier overrides are honored — an override
    that adds a dial move to a normally-no-move tier reads as a move, not "no
    dial by design"). ``kind``/``outcome`` enrich the summary text only.

    Effect precedence — a fixed order, because the DEFAULT_DELTAS tiers never
    carry two effects but a per-tier ``override`` CAN (e.g. a backfire plus a
    dial penalty). When two are present the dial move wins, deterministically:
    favorable dial move → ``advance``; unfavorable dial move → ``setback``;
    backfire → ``backfire``; resolution → ``resolution``; tag granted → ``tag``;
    else → ``inert``.

    Story 73-8 — ``hp_depletion_suppressed``: under
    ``win_condition="hp_depletion"`` ``apply_beat`` suppresses the dial mutation
    (the dials are inert HP placeholders; the move lands on the HP channel). The
    honest dial classification is then "no dial motion", so zero the dial deltas:
    the favorable/unfavorable branches fall through to ``inert``, ``dial_moved``
    is ``False``, and the surfaced ``own``/``opponent`` numbers (shipped to the UI
    by 73-7) don't read as a phantom dial gain. Tag, resolution, and backfire are
    not dial motion and still fire (read from ``deltas``).

    ``hp_removed`` (sq-playtest 2026-06-13 impact-chip-gap): the HP the strike
    actually ablated on the suppressed-dial path. The early call (pre-damage)
    passes 0 and reads "dial held / moved nothing"; ``apply_beat`` RE-derives
    with the resolved ``hp_removed`` once the HP channel lands, so a damaging
    strike (the most salient mechanical event — a crit removing HP) reads as an
    ``advance`` with the real HP delta instead of "No change — moved nothing".
    Only consulted under ``hp_depletion_suppressed``; takes precedence over the
    inert/suppressed-dial branches (a strike that drew blood is never inert).
    """
    own = deltas.own
    opponent = deltas.opponent
    # A nominal dial move that suppression zeroed means the move actually landed on
    # the HP channel — distinct from a genuine miss (no nominal delta at all), where
    # "moved nothing" stays the honest summary. Captured before zeroing so the inert
    # branch can tell the two apart (Story 73-8 follow-up).
    suppressed_dial_move = hp_depletion_suppressed and (own != 0 or opponent != 0)
    if hp_depletion_suppressed:
        own = 0
        opponent = 0
    dial_moved = own != 0 or opponent != 0
    tag = deltas.grants_tag or deltas.grants_fleeting_tag

    # Favorable: you advanced your own dial OR drained the opponent's (negative
    # opponent delta). Unfavorable: your dial slipped OR theirs rose against you.
    favorable = own > 0 or opponent < 0
    unfavorable = own < 0 or opponent > 0

    effect: BeatEffect
    if hp_depletion_suppressed and hp_removed > 0:
        # HP combat: the strike's real impact is the HP it ablated, not the
        # suppressed dial. Wins over the inert/suppressed-dial branches below so
        # a damaging crit reads as an advance with its HP delta, not "No change"
        # (sq-playtest 2026-06-13). The dial numbers stay zeroed (no dial moved);
        # the HP bar — not this readout's dial — animates the actual loss.
        effect = "advance"
        summary = f"−{hp_removed} to their HP"
        if tag:
            summary = f"{summary} ({tag})"
    elif favorable:
        effect = "advance"
        detail = []
        if own > 0:
            detail.append(f"+{own} to your edge")
        if opponent < 0:
            detail.append(f"{opponent} to their edge")
        summary = "; ".join(detail) or "Your edge advances"
        if tag:
            summary = f"{summary} ({tag})"
    elif unfavorable:
        effect = "setback"
        if own < 0:
            summary = f"Setback — your edge slips ({own})"
        else:
            summary = f"Setback — their edge rises (+{opponent})"
    elif deltas.tag_backfire:
        effect = "backfire"
        summary = (
            f"Backfire — your angle rebounds ({tag})"
            if tag
            else "Backfire — your angle rebounds onto you"
        )
    elif deltas.resolution:
        effect = "resolution"
        if tag:
            summary = f"{tag} — resolves the confrontation (no dial change by design)"
        else:
            summary = "Resolves the confrontation (no dial change by design)"
    elif tag:
        effect = "tag"
        summary = f"Sets up a scene tag: {tag} (no dial change by design)"
    elif suppressed_dial_move:
        # Story 73-8 — the dial was held (hp_depletion), but the beat DID resolve:
        # the move landed on the HP channel, so "moved nothing" would itself be a
        # small lie. The genuine-miss branch below keeps "moved nothing".
        effect = "inert"
        summary = "Dial held — HP channel resolved this beat"
    else:
        effect = "inert"
        summary = "No change — the beat landed but moved nothing"

    return BeatImpact(
        effect=effect,
        dial_moved=dial_moved,
        summary=summary,
        own=own,
        opponent=opponent,
        resolution=deltas.resolution,
        tag=tag,
    )


# Per-kind default delta tables. ``b`` is the beat's ``base``; the lambdas
# defer to runtime so we can substitute the live base + target_tag without
# building a fresh table per call.
_DefaultRule = dict[str, Any]  # {own,opponent,grants_tag,...} keyed by str

DEFAULT_DELTAS: dict[BeatKind, dict[RollOutcome, _DefaultRule]] = {
    BeatKind.strike: {
        RollOutcome.CritFail: {},
        RollOutcome.Fail: {},
        RollOutcome.Tie: {"own_expr": "b // 2"},
        RollOutcome.Success: {"own_expr": "b"},
        RollOutcome.CritSuccess: {"own_expr": "b", "grants_fleeting_tag": "Opening"},
    },
    BeatKind.brace: {
        RollOutcome.CritFail: {"opponent": 1},
        RollOutcome.Fail: {},
        RollOutcome.Tie: {"opponent_expr": "-(b // 2)"},
        RollOutcome.Success: {"opponent_expr": "-b"},
        RollOutcome.CritSuccess: {"opponent_expr": "-b", "grants_fleeting_tag": "Counter Stance"},
    },
    BeatKind.push: {
        RollOutcome.CritFail: {"own": -1},
        RollOutcome.Fail: {},
        RollOutcome.Tie: {},
        RollOutcome.Success: {"resolution": True},
        RollOutcome.CritSuccess: {"resolution": True, "grants_fleeting_tag": "Clean Exit"},
    },
    BeatKind.angle: {
        # CritFail: backfire — tag text from target_tag, fleeting, on opposing side.
        RollOutcome.CritFail: {"tag_backfire": True, "grants_fleeting_tag_from_target": True},
        RollOutcome.Fail: {},
        RollOutcome.Tie: {"grants_fleeting_tag_from_target": True},
        RollOutcome.Success: {"grants_tag_from_target": True, "tag_leverage": 1},
        RollOutcome.CritSuccess: {"grants_tag_from_target": True, "tag_leverage": 2},
    },
}


def _eval_expr(expr: str, base: int) -> int:
    """Evaluate a tiny ``b``-only arithmetic expression — closed form, no eval."""
    # Two forms appear in DEFAULT_DELTAS: ``b``, ``b // 2``, ``-b``, ``-(b // 2)``.
    expr = expr.replace(" ", "")
    if expr == "b":
        return base
    if expr == "-b":
        return -base
    if expr == "b//2":
        return base // 2
    if expr == "-(b//2)":
        return -(base // 2)
    raise ValueError(f"unsupported delta expression: {expr!r}")


def resolve_tier_deltas(
    *,
    kind: BeatKind,
    base: int,
    outcome: RollOutcome,
    overrides: dict[RollOutcome, dict[str, Any]] | None,
    target_tag: str | None,
) -> ResolvedDeltas:
    """Merge kind defaults with per-tier overrides into flat ``ResolvedDeltas``.

    Resolution order: kind defaults → per-tier override → engine zeros.

    ``target_tag`` is required for ``angle`` beats (used as the tag text
    on Success/CritSuccess and as the backfire text on CritFail). Other
    kinds may pass ``None``.
    """
    if outcome is RollOutcome.Unknown:
        raise ValueError("RollOutcome.Unknown cannot resolve a beat tier")

    if kind is BeatKind.angle and not target_tag:
        raise ValueError("angle beats require a target_tag")

    rule = dict(DEFAULT_DELTAS[kind][outcome])
    if overrides and outcome in overrides:
        rule.update(overrides[outcome])

    own = int(rule.get("own", 0))
    if "own_expr" in rule:
        own = _eval_expr(rule["own_expr"], base)

    opponent = int(rule.get("opponent", 0))
    if "opponent_expr" in rule:
        opponent = _eval_expr(rule["opponent_expr"], base)

    grants_tag = rule.get("grants_tag")
    grants_fleeting_tag = rule.get("grants_fleeting_tag")
    tag_leverage = int(rule.get("tag_leverage", 0))
    tag_backfire = bool(rule.get("tag_backfire", False))
    resolution = bool(rule.get("resolution", False))

    if rule.get("grants_tag_from_target"):
        grants_tag = target_tag
    if rule.get("grants_fleeting_tag_from_target"):
        grants_fleeting_tag = target_tag

    return ResolvedDeltas(
        own=own,
        opponent=opponent,
        grants_tag=grants_tag,
        tag_leverage=tag_leverage,
        grants_fleeting_tag=grants_fleeting_tag,
        tag_backfire=tag_backfire,
        resolution=resolution,
    )


# ---------------------------------------------------------------------------
# apply_beat — shared between narrator and dice-throw paths
# ---------------------------------------------------------------------------
from collections.abc import Callable  # noqa: E402
from dataclasses import dataclass  # noqa: E402

from sidequest.game.creature_core import CreatureCore  # noqa: E402
from sidequest.game.encounter import (  # noqa: E402
    EncounterActor,
    EncounterPhase,
    StructuredEncounter,
)
from sidequest.game.encounter_tag import EncounterTag  # noqa: E402
from sidequest.game.hp_depletion import check_hp_depletion  # noqa: E402
from sidequest.telemetry.spans import (  # noqa: E402
    SPAN_ENCOUNTER_TAUNT_ACTIVATED,
    encounter_composure_break_span,
    encounter_edge_debit_span,
    encounter_metric_advance_span,
    encounter_tag_backfire_span,
    encounter_tag_created_span,
    state_patch_hp_span,
)
from sidequest.telemetry.spans.span import Span  # noqa: E402
from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish  # noqa: E402

EdgeResolver = Callable[[str], CreatureCore | None]
DamageResolver = Callable[[], int]


def apply_beat_hp_channel(
    *,
    target: CreatureCore,
    channel: str,
    damage_total: int,
    target_mitigation: int,
    source_beat_id: str = "?",
) -> int:
    """Apply a strike beat's damage to target HP after flat mitigation (ADR-114 §2).

    Returns HP actually removed (>= 0). No-op unless channel == "strike".
    brace supplies mitigation to the NEXT strike (handled at the wiring layer),
    so it does not mutate HP here. Every real HP delta emits a state_patch span
    (ADR-114 §6 — GM-panel lie detector)."""
    if channel != "strike" or damage_total <= 0:
        return 0
    applied = max(0, damage_total - max(0, target_mitigation))
    if applied == 0:
        return 0
    before = target.hp.current
    target.apply_hp_delta(-applied)
    after = target.hp.current
    state_patch_hp_span(
        actor=target.name,
        delta=-applied,
        source=source_beat_id,
        current=after,
        maximum=target.hp.max,
    )
    return before - after


@dataclass(frozen=True)
class ApplyResult:
    """Outcome of one ``apply_beat`` invocation.

    ``skipped_reason`` is non-None when the beat was dropped — the encounter
    state is unchanged. ``resolved`` is True when this beat caused the
    encounter to flip ``resolved=True``.
    """

    deltas: ResolvedDeltas | None
    resolved: bool
    skipped_reason: str | None = None
    # Player-facing legibility descriptor (Story 73-4). None only when the beat
    # was skipped (no deltas resolved).
    impact: BeatImpact | None = None
    # Actual HP removed from the primary target by the strike damage channel
    # (ADR-114 §2). Distinct from ``deltas.opponent``, which is the *dial*
    # delta and is suppressed to 0 under ``win_condition="hp_depletion"`` (the
    # dials are inert placeholders). Without this, the persisted forensics
    # ``ENCOUNTER_BEAT_APPLIED`` event records only the inert dial delta and a
    # post-hoc reader sees ``opponent_delta=0`` for a strike that removed real
    # HP — the lie-detector goes blind on the persisted surface even though the
    # live ``state_patch.hp`` span fired. 0 when no HP channel ran.
    hp_removed: int = 0


def _phase_for_beat(beat: int) -> EncounterPhase:
    ladder = {
        0: EncounterPhase.Setup,
        1: EncounterPhase.Opening,
        2: EncounterPhase.Escalation,
        3: EncounterPhase.Escalation,
        4: EncounterPhase.Escalation,
    }
    return ladder.get(beat, EncounterPhase.Climax)


def _opposite_side_first_actor(
    enc: StructuredEncounter,
    side: str,
) -> str | None:
    """Return the primary target on the opposite side from *side*.

    Taunt bias (spec §8): when ``enc.taunt.active_actor`` is set AND the
    taunter is among the live candidates on the opposite side, the taunter
    is returned instead of the first-listed actor.  This makes taunt absorb
    enemy strikes on the real production damage path (``apply_beat`` focus /
    swarm branch).

    Default (no taunt, or taunter not in candidates): first live actor in
    encounter declaration order.
    """
    other = "opponent" if side == "player" else "player"
    candidates = [a.name for a in enc.actors if a.side == other and not a.withdrawn]
    if not candidates:
        return None
    taunter = enc.taunt.active_actor
    if taunter is not None and taunter in candidates:
        return taunter
    return candidates[0]


def _opposite_side_live_actors(
    enc: StructuredEncounter,
    side: str,
) -> list[str]:
    """All non-withdrawn opposing actors, in encounter declaration order.

    Used by ``target_select=spread`` to divide ``target_edge_delta`` across
    every engaged enemy.
    """
    other = "opponent" if side == "player" else "player"
    return [a.name for a in enc.actors if a.side == other and not a.withdrawn]


# ---------------------------------------------------------------------------
# Numerical advantage (Step 3 of numerical-advantage design)
# ---------------------------------------------------------------------------


_NUMERICAL_ADVANTAGE_DIVISOR = 2
_NUMERICAL_ADVANTAGE_CAP = 3


def numerical_advantage_modifier(
    ally_edge_fractions: list[float],
) -> int:
    """Pure shift modifier from a side's engaged-ally roster.

    ``ally_edge_fractions`` lists the ``edge_fraction`` (current/max) of
    every engaged ally on the initiator's side, EXCLUDING the initiator
    themselves. Withdrawn allies are passed in as ``0.0`` (still bodies
    in the room, contributing nothing).

    Math:
        raw = floor(sum(fractions) / 2)         # +1 per 2 full-edge allies
        cap at +3
        if more than half of allies are broken (fraction <= 0.0):
            return -max(raw, 1)                 # collapsed swarm flips sign

    Tuning: the d20 shift bands (ADR-093) place ``Tie`` at [-1,+1] and
    ``Success`` at >=+2. A +1 numerical-advantage modifier alone is
    therefore tier-neutral in expectation — meaningful on the margin
    but not a free win. +2 (4+ allies) flips the average roll from
    Tie to Success; +3 (6+ allies) is saturated swarm pressure.
    """
    if not ally_edge_fractions:
        return 0

    raw = int(sum(ally_edge_fractions) / _NUMERICAL_ADVANTAGE_DIVISOR)
    capped = min(raw, _NUMERICAL_ADVANTAGE_CAP)

    broken = sum(1 for f in ally_edge_fractions if f <= 0.0)
    if broken * 2 > len(ally_edge_fractions):
        # Majority broken — the swarm has visibly collapsed.
        return -max(capped, 1)

    return capped


def numerical_advantage_for(
    initiator: EncounterActor,
    enc: StructuredEncounter,
    edge_resolver: EdgeResolver,
) -> int:
    """Compute the initiator's side's numerical-advantage shift modifier.

    Walks ``enc.actors`` for engaged allies (same side, not the initiator),
    resolves each to a ``CreatureCore`` via ``edge_resolver``, and computes
    ``edge.current / edge.max`` per ally. Withdrawn allies contribute 0.0.
    Allies the resolver doesn't know are excluded from the computation
    rather than counted as zero — see CLAUDE.md no-silent-fallback: a
    legitimate "actor without a core" arises during MP joiner races and
    should not count toward swarm collapse detection.
    """
    fractions: list[float] = []
    for actor in enc.actors:
        if actor.name == initiator.name:
            continue
        if actor.side != initiator.side:
            continue
        if actor.withdrawn:
            fractions.append(0.0)
            continue
        core = edge_resolver(actor.name)
        if core is None:
            continue
        denom = core.hp.max if core.hp.max > 0 else 1
        fractions.append(core.hp.current / denom)
    return numerical_advantage_modifier(fractions)


def _normalize_overrides(
    raw: dict[str, dict] | None,
) -> dict[RollOutcome, dict] | None:
    if raw is None:
        return None
    mapping = {
        "crit_fail": RollOutcome.CritFail,
        "fail": RollOutcome.Fail,
        "tie": RollOutcome.Tie,
        "success": RollOutcome.Success,
        "crit_success": RollOutcome.CritSuccess,
    }
    return {mapping[k]: v for k, v in raw.items()}


def apply_beat(
    enc: StructuredEncounter,
    actor: EncounterActor,
    beat: Any,  # BeatDef — typed as Any to dodge circular import
    outcome: RollOutcome,
    *,
    turn: int = 0,
    edge_resolver: EdgeResolver | None = None,
    damage_resolver: DamageResolver | None = None,
) -> ApplyResult:
    """Apply one beat at one outcome tier to the encounter.

    Routes the deltas to the actor's side, processes tag/resolution extras,
    advances ``enc.beat`` and ``structured_phase``, and detects threshold
    crossings. Emits ``encounter.metric_advance``, ``encounter.tag_created``,
    and (on angle CritFail) ``encounter.tag_backfire`` spans.

    Skips with a structured reason when the actor is neutral, withdrawn,
    or the encounter is already resolved.
    """
    if enc.resolved:
        return ApplyResult(deltas=None, resolved=False, skipped_reason="encounter_resolved")
    if actor.side == "neutral":
        return ApplyResult(deltas=None, resolved=False, skipped_reason="neutral_actor")
    if actor.withdrawn:
        return ApplyResult(deltas=None, resolved=False, skipped_reason="withdrawn_actor")

    # Story 126-37 (ADR-143/144 "Bind the Ruleset, Don't Balance It"): a Fate conflict —
    # seated by 126-30 with win_condition="fate_conflict" — resolves EXCLUSIVELY through
    # the 4dF conflict engine (fate_conflict.py) reading the Other's FateSheet stress. The
    # native beat/dial engine is REMOVED from the Fate path, not tuned to coexist, so
    # apply_beat short-circuits here — BEFORE any delta / tag / resolution work — the same
    # way the early returns above bail. This is the belt: the narration loop drops Fate
    # beats upstream (narration_apply conflict_beat_dropped_dial_blocked), but any other
    # caller that reaches apply_beat on a Fate conflict is suppressed here too. Emit a
    # suppression event so the GM panel sees the native engine stood down (OTEL
    # Observability Principle / No Silent Fallbacks); the narration loop renders the
    # skipped_reason as encounter.beat_skipped.
    if enc.win_condition == "fate_conflict":
        _watcher_publish(
            "state_transition",
            {
                "field": "encounter",
                "op": "beat_suppressed_fate_conflict",
                "actor": actor.name,
                "actor_side": actor.side,
                "beat_id": getattr(beat, "id", "?"),
                "rationale": (
                    "win_condition=fate_conflict — the 4dF conflict engine owns "
                    "resolution; native beat/dial mechanics are removed from the Fate path"
                ),
            },
            component="encounter",
            severity="info",
        )
        return ApplyResult(deltas=None, resolved=False, skipped_reason="fate_conflict_suppressed")

    overrides = _normalize_overrides(getattr(beat, "deltas", None))
    deltas = resolve_tier_deltas(
        kind=beat.kind,
        base=getattr(beat, "base", 1),
        outcome=outcome,
        overrides=overrides,
        target_tag=getattr(beat, "target_tag", None),
    )

    # Story 73-4 — derive + stamp the player-facing legibility descriptor. Stored
    # per-side so an opposed_check opponent beat (applied later this turn) can't
    # clobber the player's readout. Derived from the *nominal* resolved deltas.
    # CAVEAT (hp_depletion): for win_condition="hp_depletion" the dial application
    # below is suppressed (the dials are inert HP placeholders), so the descriptor
    # can report effect="advance"/dial_moved=True for a beat whose dial never
    # actually moved on-screen. That mode renders HP bars, not this dial-impact
    # panel (out of scope for 73-4 / dial confrontations) — but a future story
    # surfacing last_beat_impact under hp_depletion must read the HP channel, not
    # these nominal dial deltas. See Delivery Findings (Dev) for the follow-up.
    # Story 73-8 — that follow-up: pass the hp_depletion flag so the descriptor
    # stamps a truthful "no dial motion" (inert/tag/resolution) instead of a
    # phantom advance/dial_moved=True for the suppressed dial below.
    impact = describe_beat_impact(
        deltas,
        kind=beat.kind,
        outcome=outcome,
        hp_depletion_suppressed=enc.win_condition == "hp_depletion",
    )
    enc.last_beat_impacts[actor.side] = asdict(impact)

    own_metric = enc.player_metric if actor.side == "player" else enc.opponent_metric
    other_metric = enc.opponent_metric if actor.side == "player" else enc.player_metric

    # hp_depletion (SWN combat): the dials are inert 1e6 placeholders synthesized
    # by the init seam, and the HP channel (damage_channel/edge_delta below) is the
    # authoritative resolution track. Applying dial deltas here advances that
    # placeholder — playtest 67-10 (59-26) caught a Fail shoot pushing the inert
    # dial to 2, broadcast as momentum and rendered by the overlay as the
    # "0/1000000" bar. Suppress the dial mutation for hp_depletion; emit a span so
    # the GM panel sees the deltas were computed-then-suppressed, not silently
    # dropped (OTEL Observability Principle). The resolution-beat / HP branches
    # below are unaffected.
    hp_depletion = enc.win_condition == "hp_depletion"
    if hp_depletion and (deltas.own != 0 or deltas.opponent != 0):
        _watcher_publish(
            "state_transition",
            {
                "field": "encounter",
                "op": "dial_suppressed_hp_depletion",
                "actor": actor.name,
                "actor_side": actor.side,
                "beat_id": getattr(beat, "id", "?"),
                "suppressed_own": deltas.own,
                "suppressed_opponent": deltas.opponent,
                "rationale": (
                    "win_condition=hp_depletion — dials are inert placeholders; "
                    "HP channel is authoritative, dial deltas not applied"
                ),
            },
            component="encounter",
            severity="info",
        )

    if deltas.own != 0 and not hp_depletion:
        before = own_metric.current
        own_metric.current = max(0, own_metric.current + deltas.own)
        with encounter_metric_advance_span(
            side=actor.side,
            delta_kind="own",
            delta=deltas.own,
            before=before,
            after=own_metric.current,
        ):
            pass
        _watcher_publish(
            "state_transition",
            {
                "field": "encounter",
                "op": "metric_advance",
                "side": actor.side,
                "delta_kind": "own",
                "delta": deltas.own,
                "before": before,
                "after": own_metric.current,
            },
            component="encounter",
        )

    if deltas.opponent != 0 and not hp_depletion:
        before = other_metric.current
        # Opponent dial: ``brace`` emits a negative delta; ascending dials
        # are clamped at 0.
        other_metric.current = max(0, other_metric.current + deltas.opponent)
        cross_side = "opponent" if actor.side == "player" else "player"
        with encounter_metric_advance_span(
            side=cross_side,
            delta_kind="cross",
            delta=deltas.opponent,
            before=before,
            after=other_metric.current,
        ):
            pass
        _watcher_publish(
            "state_transition",
            {
                "field": "encounter",
                "op": "metric_advance",
                "side": cross_side,
                "delta_kind": "cross",
                "delta": deltas.opponent,
                "before": before,
                "after": other_metric.current,
            },
            component="encounter",
        )

    if deltas.tag_backfire:
        # Angle CritFail: tag goes onto the opposing side, fleeting.
        target_actor_name = _opposite_side_first_actor(enc, actor.side)
        tag = EncounterTag(
            text=getattr(beat, "target_tag", "Backfire"),
            created_by=actor.name,
            target=target_actor_name,
            leverage=1,
            fleeting=True,
            created_turn=turn,
        )
        enc.tags.append(tag)
        with encounter_tag_backfire_span(
            tag_text=tag.text,
            created_by=actor.name,
            target=target_actor_name or "",
            triggering_beat=beat.id,
        ):
            pass
        _watcher_publish(
            "state_transition",
            {
                "field": "encounter",
                "op": "tag_backfire",
                "tag_text": tag.text,
                "created_by": actor.name,
                "target": target_actor_name or "",
                "triggering_beat": beat.id,
                "fleeting": True,
                "leverage": 1,
            },
            component="encounter",
        )
    elif deltas.grants_tag:
        tag = EncounterTag(
            text=deltas.grants_tag,
            created_by=actor.name,
            target=_opposite_side_first_actor(enc, actor.side),
            leverage=deltas.tag_leverage or 1,
            fleeting=False,
            created_turn=turn,
        )
        enc.tags.append(tag)
        with encounter_tag_created_span(
            tag_text=tag.text,
            created_by=actor.name,
            target=tag.target,
            leverage=tag.leverage,
            fleeting=False,
            created_via="angle_beat",
        ):
            pass
        _watcher_publish(
            "state_transition",
            {
                "field": "encounter",
                "op": "tag_created",
                "tag_text": tag.text,
                "created_by": actor.name,
                "target": tag.target or "",
                "leverage": tag.leverage,
                "fleeting": False,
                "created_via": "angle_beat",
            },
            component="encounter",
        )

    if deltas.grants_fleeting_tag and not deltas.tag_backfire:
        tag = EncounterTag(
            text=deltas.grants_fleeting_tag,
            created_by=actor.name,
            target=_opposite_side_first_actor(enc, actor.side),
            leverage=1,
            fleeting=True,
            created_turn=turn,
        )
        enc.tags.append(tag)
        with encounter_tag_created_span(
            tag_text=tag.text,
            created_by=actor.name,
            target=tag.target,
            leverage=1,
            fleeting=True,
            created_via="extras",
        ):
            pass
        _watcher_publish(
            "state_transition",
            {
                "field": "encounter",
                "op": "tag_created",
                "tag_text": tag.text,
                "created_by": actor.name,
                "target": tag.target or "",
                "leverage": 1,
                "fleeting": True,
                "created_via": "extras",
            },
            component="encounter",
        )

    # ADR-078 §3-4 — apply per-beat edge debits and detect composure break.
    # ``edge_delta`` debits the acting actor; ``target_edge_delta`` debits
    # the first live opposing actor (single-target "focus" mode; Step 2
    # generalises this with a per-beat ``target_select``). When either
    # field is set the caller MUST provide an edge_resolver — silent
    # skipping would let the narrator describe wounds the engine never
    # recorded (CLAUDE.md no silent fallbacks).
    self_edge_delta = getattr(beat, "edge_delta", None) or 0
    target_edge_delta = getattr(beat, "target_edge_delta", None) or 0
    composure_break: tuple[str, str] | None = None  # (broken_char_name, side)

    if self_edge_delta or target_edge_delta:
        if edge_resolver is None:
            raise ValueError(
                f"beat {getattr(beat, 'id', '?')!r} declares edge_delta / "
                f"target_edge_delta but apply_beat received no edge_resolver"
            )

        if self_edge_delta:
            actor_core = edge_resolver(actor.name)
            if actor_core is None:
                raise ValueError(f"edge_resolver returned no CreatureCore for actor {actor.name!r}")
            before = actor_core.hp.current
            actor_core.apply_hp_delta(-self_edge_delta)
            after = actor_core.hp.current
            with encounter_edge_debit_span(
                source_actor=actor.name,
                target_actor=actor.name,
                debit_kind="self",
                delta=-self_edge_delta,
                before=before,
                after=after,
                beat_id=getattr(beat, "id", "?"),
            ):
                pass
            if after <= 0 and composure_break is None:
                composure_break = (actor.name, "self")

        if target_edge_delta:
            mode = getattr(beat, "target_select", None) or "focus"
            if mode == "spread":
                live_targets = _opposite_side_live_actors(enc, actor.side)
                if live_targets:
                    per_target = target_edge_delta // len(live_targets)
                    if per_target > 0:
                        for target_name in live_targets:
                            # Spec: 2026-05-10 class-mechanical-surface §8 —
                            # taunt damage redirect (cap 1/round).
                            # When taunt is active and the original target is NOT
                            # the taunter, attempt a redirect.  If the per-round
                            # cap (1) hasn't been reached, the hit lands on the
                            # taunter instead.  The original ally is untouched.
                            original_target = target_name
                            if (
                                enc.taunt.active_actor
                                and target_name != enc.taunt.active_actor
                                and enc.taunt.active_actor in live_targets
                                and enc.taunt.try_consume_redirect()
                            ):
                                target_name = enc.taunt.active_actor
                            target_core = edge_resolver(target_name)
                            if target_core is None:
                                raise ValueError(
                                    f"edge_resolver returned no CreatureCore "
                                    f"for target {target_name!r}"
                                )
                            before = target_core.hp.current
                            target_core.apply_hp_delta(-per_target)
                            after = target_core.hp.current
                            with encounter_edge_debit_span(
                                source_actor=actor.name,
                                target_actor=target_name,
                                debit_kind="target",
                                delta=-per_target,
                                before=before,
                                after=after,
                                beat_id=getattr(beat, "id", "?"),
                                target_select="spread",
                                taunt_redirected=(target_name != original_target),
                            ):
                                pass
                            if after <= 0 and composure_break is None:
                                composure_break = (target_name, "target")
            else:
                # focus | swarm — single primary target. swarm carries an
                # OTEL flag so Step 3 can detect ally-amplification beats.
                target_name = _opposite_side_first_actor(enc, actor.side)
                if target_name is not None:
                    target_core = edge_resolver(target_name)
                    if target_core is None:
                        raise ValueError(
                            f"edge_resolver returned no CreatureCore for target {target_name!r}"
                        )
                    before = target_core.hp.current
                    target_core.apply_hp_delta(-target_edge_delta)
                    after = target_core.hp.current
                    with encounter_edge_debit_span(
                        source_actor=actor.name,
                        target_actor=target_name,
                        debit_kind="target",
                        delta=-target_edge_delta,
                        before=before,
                        after=after,
                        beat_id=getattr(beat, "id", "?"),
                        target_select=mode,
                    ):
                        pass
                    if after <= 0 and composure_break is None:
                        composure_break = (target_name, "target")

    # ADR-114 §2 — damage_channel HP resolution (ADDITIVE to edge_delta path above).
    # Only fires for strike channel when a damage_resolver is provided.
    # brace does NOT mutate HP here — it supplies mitigation to the next strike
    # (the calling layer holds the pending brace value).
    # When damage_resolver is None and the beat declares a strike channel, the
    # HP path is skipped — Task 7 injects the real dice resolver; until then
    # the engine is silent on HP for channel=strike beats (no phantom zero damage).
    damage_channel = str(getattr(beat, "damage_channel", "none") or "none")
    hp_removed = 0
    if damage_channel == "strike" and damage_resolver is not None:
        damage_total = damage_resolver()
        # Resolve target mitigation: beat.mitigation_override takes precedence;
        # otherwise use the first live opposing actor's equipped armor mitigation
        # (via edge_resolver — same resolver already required for edge_delta).
        mitigation_override = getattr(beat, "mitigation_override", None)
        if mitigation_override is not None:
            target_mitigation = int(mitigation_override)
        else:
            target_mitigation = 0
            # If we can resolve the target's CreatureCore, look for armor mitigation
            # in inventory (CatalogItem.mitigation). Kept simple: first live opponent.
            if edge_resolver is not None:
                primary_target_name = _opposite_side_first_actor(enc, actor.side)
                if primary_target_name is not None:
                    target_core_for_mit = edge_resolver(primary_target_name)
                    if target_core_for_mit is not None:
                        for item_dict in target_core_for_mit.inventory.items:
                            mit = item_dict.get("mitigation")
                            if mit is not None:
                                target_mitigation = int(mit)
                                break

        # Apply HP damage to primary target.
        primary_target = _opposite_side_first_actor(enc, actor.side)
        if primary_target is not None and edge_resolver is not None:
            hp_target = edge_resolver(primary_target)
            if hp_target is not None:
                hp_removed = apply_beat_hp_channel(
                    target=hp_target,
                    channel="strike",
                    damage_total=damage_total,
                    target_mitigation=target_mitigation,
                    source_beat_id=getattr(beat, "id", "?"),
                )

    # Story 73-8 follow-up (sq-playtest 2026-06-13 impact-chip-gap): the early
    # impact stamp above ran BEFORE the HP channel resolved, so under hp_depletion
    # it could only say "dial held / moved nothing" — the overlay's beat-impact
    # chip then read "No change" even on a crit that removed HP. Now that the
    # strike's real HP loss is known, re-derive the descriptor with it so the
    # most salient mechanical event reads as an advance (Sebastien/Jade "see the
    # math"). Only re-stamps when damage actually landed; a true whiff keeps the
    # honest "moved nothing".
    if hp_depletion and hp_removed > 0:
        enc.last_beat_impacts[actor.side] = asdict(
            describe_beat_impact(
                deltas,
                kind=beat.kind,
                outcome=outcome,
                hp_depletion_suppressed=True,
                hp_removed=hp_removed,
            )
        )

    enc.beat += 1
    enc.structured_phase = _phase_for_beat(enc.beat)

    # Story 2026-05-10 — taunt beat activation (Task 3).
    # When a Fighter resolves the taunt beat with a non-failure outcome,
    # activate the encounter's TauntState and emit an OTEL lie-detector span
    # so the GM panel can verify taunt engaged rather than the narrator
    # improvising the attention-pull effect.
    if getattr(beat, "id", None) == "taunt" and outcome not in (
        RollOutcome.Fail,
        RollOutcome.CritFail,
    ):
        enc.taunt.activate(actor_id=actor.name)
        with Span.open(
            SPAN_ENCOUNTER_TAUNT_ACTIVATED,
            {
                "actor_id": actor.name,
                "round": enc.beat,
            },
        ):
            pass
        _watcher_publish(
            "state_transition",
            {
                "field": "encounter.taunt",
                "op": "activated",
                "actor_id": actor.name,
                "round": enc.beat,
            },
            component="encounter",
        )

    # GM-panel visibility for inert beats. Per spec, default delta tables
    # for Fail tier on every kind are {own=0, opponent=0} — a Fail rolls
    # narratively but neither dial moves. Without this event the GM panel
    # sees the beat fire and assumes the engine is responsive; nothing
    # surfaces the silent stalemate. Playtest 2026-04-25 [P0] flagged
    # this as a P0 because from the player's view the dual-track engine
    # looked decorative when in fact it was working as specified.
    # Surfacing the no-op turns the design choice from invisible into
    # observable — Sebastien-the-mechanics-player can see that the
    # encounter didn't progress, and Keith debugging can audit whether
    # the spec's intent matches the playtest experience.
    if (
        deltas.own == 0
        and deltas.opponent == 0
        and not deltas.grants_tag
        and not deltas.grants_fleeting_tag
        and not deltas.tag_backfire
        and not deltas.resolution
    ):
        _watcher_publish(
            "state_transition",
            {
                "field": "encounter",
                "op": "beat_no_op",
                "actor": actor.name,
                "actor_side": actor.side,
                "beat_id": getattr(beat, "id", "?"),
                "beat_kind": str(beat.kind.value)
                if hasattr(beat.kind, "value")
                else str(beat.kind),
                "rationale": (
                    "default delta table for this kind+outcome tier "
                    "is {own=0, opponent=0} (per spec) — beat fired "
                    "but neither dial moved"
                ),
            },
            component="encounter",
            severity="info",
        )

    resolved = False

    # Composure break (ADR-078 §4): edge dropped to 0 → encounter resolves
    # before the dial threshold checks fire, so the narrator's resolution
    # frame receives ``composure_break:<char>`` rather than a dial victory.
    if composure_break is not None:
        broken_name, broken_side = composure_break
        with encounter_composure_break_span(
            char_name=broken_name,
            side=broken_side,
            beat_id=getattr(beat, "id", "?"),
        ):
            pass
        enc.resolved = True
        enc.outcome = f"composure_break:{broken_name}"
        enc.structured_phase = EncounterPhase.Resolution
        resolved = True

    # ADR-114 §2 — HP-depletion resolution. For ``win_condition="hp_depletion"``
    # confrontations the dials are inert (threshold synthesized at 1e6 by the
    # init seam); the encounter ends when a side's primary combatant hits 0 HP
    # as read through the edge_resolver. The dial-threshold branches below are
    # gated OFF for this win condition so an inert dial can never falsely
    # resolve the fight. Emits ``encounter.resolved`` with ``source="hp_depletion"``
    # so the GM panel can tell an HP kill from a dial victory.
    # (``hp_depletion`` computed once near the top of this function.)
    if hp_depletion and not resolved and edge_resolver is not None:
        result = check_hp_depletion(enc, edge_resolver, beat_id=getattr(beat, "id", "?"))
        if result is not None:
            resolved = True

    # Player threshold first, then opponent — sealed-letter order via
    # ADR-036 already places player beats first in the iteration; this
    # second-level tie-break is "first crossing wins". Gated to dial-threshold
    # confrontations only; hp_depletion resolves on the HP branch above.
    if (
        not hp_depletion
        and not resolved
        and enc.player_metric.current >= enc.player_metric.threshold
    ):
        enc.resolved = True
        enc.outcome = "player_victory"
        enc.structured_phase = EncounterPhase.Resolution
        resolved = True
    elif (
        not hp_depletion
        and not resolved
        and enc.opponent_metric.current >= enc.opponent_metric.threshold
    ):
        enc.resolved = True
        enc.outcome = "opponent_victory"
        enc.structured_phase = EncounterPhase.Resolution
        resolved = True
    # Ungated by hp_depletion ON PURPOSE: a resolution / surrender beat ends
    # either kind of confrontation (dial-threshold or HP-depletion).
    elif not resolved and (deltas.resolution or getattr(beat, "resolution", False)):
        enc.resolved = True
        enc.outcome = f"resolution_beat:{beat.id}"
        enc.structured_phase = EncounterPhase.Resolution
        resolved = True

    return ApplyResult(
        deltas=deltas,
        resolved=resolved,
        skipped_reason=None,
        impact=impact,
        hp_removed=hp_removed,
    )
