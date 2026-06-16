"""Tool: advance_confrontation — advance the player or opponent dial.

Phase C Task 21 — WRITE tool
----------------------------
Replaces the sidecar ``confrontation_advances`` field. The narrator
calls this during a structured confrontation (combat, chase, trial,
poker, debate) to advance one of the two side-routed dials.

ADR-033 status
~~~~~~~~~~~~~~
ADR-033 (Genre Mechanics Engine — Confrontations & Resource Pools) is
*partial* in the live codebase. No formal ``Confrontation`` class with
named axes exists yet. The closest live system is
:class:`~sidequest.game.encounter.StructuredEncounter`, which carries
two :class:`~sidequest.game.encounter.EncounterMetric` ascending dials:
``player_metric`` and ``opponent_metric``. Each has a ``current`` value
that advances toward a ``threshold``; the side that reaches threshold
first triggers resolution.

v1 mapping
~~~~~~~~~~
* ``axis: Literal["player", "opponent"]`` — which metric dial to
  advance. Other named axes (e.g. ``"stakes"``, ``"tension"``,
  ``"composure"``) are an ADR-033 forward-looking concept and not
  implemented here; passing anything else is rejected at the args
  model.
* ``delta: int`` — signed delta added to ``metric.current``. The engine
  does *not* clamp; values can grow past ``threshold`` (or below zero
  for negative deltas — useful for "regroup" beats). The narrator is
  responsible for sensible deltas; ``crossed_threshold`` in the result
  signals that a resolution beat is now due. The engine no longer relies
  on the narrator to follow up: ``_resolve_dial_threshold_and_phase`` in
  ``server/narration_apply.py`` runs a post-turn sweep that resolves the
  encounter at threshold and advances ``structured_phase`` to track dial
  heat, regardless of whether this tool or ``advance_encounter_beat``
  moved the dial (playtest 2026-06-01: a standoff driven by this tool
  alone otherwise heated to threshold frozen in ``Setup`` and never
  resolved).
* ``confrontation_id: str`` — accepted forward-compat for the eventual
  multi-confrontation registry (ADR-033's ``ConfrontationDefinition``
  graph). v1 always targets ``snapshot.encounter``; the id is recorded
  in OTEL so the GM panel can show what the narrator *intended* to
  select even before the registry exists.
* ``reason: str`` — free-form one-line audit note for OTEL.

OTEL attributes
~~~~~~~~~~~~~~~
* ``tool.confrontation.id`` — forward-compat id; empty string by default.
* ``tool.confrontation.axis`` — ``"player"`` or ``"opponent"``.
* ``tool.confrontation.delta`` — signed delta the narrator passed in.
* ``tool.confrontation.value_after`` — ``metric.current`` after the
  mutation; lets the GM panel chart the dial in real time.
* ``tool.confrontation.reason`` — narrator's audit note (empty string
  when omitted).
* ``tool.confrontation.crossed_threshold`` — ``True`` iff this call
  pushed ``current`` from below ``threshold`` to at-or-above. A metric
  that was *already* past threshold and advances further is *not* a
  fresh crossing (the resolution beat already fired).
* ``tool.confrontation.canonical`` — ``True`` (Story 73-3). Signals the
  advance mutated the *canonical* in-turn snapshot (which the end-of-turn
  save persists), not a fresh ``repository.load()`` copy. Lets the GM
  panel confirm the dial move is durable, closing the "span fires but the
  write is lost" lie.

Concurrency
~~~~~~~~~~~
Sequential-per-session execution is provided by the Registry's
``_write_locks`` map — WRITE handlers don't need their own locking.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from sidequest.agents.tool_registry import (
    ToolCategory,
    ToolContext,
    ToolResult,
    tool,
)
from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish


class AdvanceConfrontationArgs(BaseModel):
    model_config = {"extra": "forbid"}

    confrontation_id: str = Field(
        default="",
        description=(
            "Reserved for ADR-033 multi-confrontation support. v1 always "
            "advances the current snapshot.encounter; the arg is recorded "
            "in OTEL so the GM panel can audit the narrator's intent."
        ),
    )
    axis: Literal["player", "opponent"] = Field(
        ...,
        description=(
            "Which metric dial to advance: 'player' targets "
            "encounter.player_metric, 'opponent' targets opponent_metric."
        ),
    )
    delta: int = Field(
        ...,
        description=(
            "Signed delta to add to metric.current. The engine does not "
            "clamp; values can exceed threshold or go negative. Use "
            "crossed_threshold in the result to detect a resolution event."
        ),
    )
    reason: str = Field(
        default="",
        description="One-line narrator note for OTEL / GM-panel audit.",
    )


def _find_active_cdef(genre_pack: Any, encounter_type: str) -> Any:
    """Resolve the active encounter's ConfrontationDef from the pack rules.

    Inline exact-type match (the same contract as
    ``sidequest.server.dispatch.confrontation.find_confrontation_def`` —
    not imported because ``sidequest.agents`` must not depend on
    ``sidequest.server``). Returns ``None`` when the pack is absent, has no
    rules, or no def matches; the caller decides (here: the guard stands
    down — the dial engine remains the narrator's channel).
    """
    rules = getattr(genre_pack, "rules", None)
    for d in getattr(rules, "confrontations", None) or []:
        if d.confrontation_type == encounter_type:
            return d
    return None


@tool(
    name="advance_confrontation",
    description=(
        "Advance a Confrontation Def axis by a delta. Use during "
        "structured confrontations (combat, chase, trial, poker, debate). "
        "v1 binds to the active StructuredEncounter's player_metric or "
        "opponent_metric dial; pass axis='player' or 'opponent'. Returns "
        "value_before/value_after and a crossed_threshold flag; fails "
        "fatally if no encounter is active."
    ),
    category=ToolCategory.WRITE,
)
async def advance_confrontation(args: AdvanceConfrontationArgs, ctx: ToolContext) -> ToolResult:
    # Story 73-3: mutate the CANONICAL in-turn snapshot the narration pipeline
    # holds (ADR-037 — owned by the SessionRoom), NOT a fresh
    # ``repository.load()`` copy. The old read-modify-write against a fresh load
    # was silently clobbered by the end-of-turn ``room.save()`` (which persists
    # the canonical object after the tool runs). Fail loud if the canonical
    # snapshot is not wired onto the context — never silently fall back to
    # ``repository.load()`` (CLAUDE.md "No Silent Fallbacks"), as that would
    # re-introduce the exact lost-update this story fixes.
    snapshot = ctx.snapshot
    if snapshot is None:
        return ToolResult.error(
            "no canonical snapshot on tool context — cannot advance "
            "confrontation (the in-turn snapshot was not threaded onto "
            "ToolContext; refusing to fall back to a fresh repository.load() "
            "that the end-of-turn save would clobber)",
            recoverable=False,
        )

    encounter = snapshot.encounter
    if encounter is None:
        return ToolResult.error(
            "no active encounter — cannot advance confrontation",
            recoverable=False,
        )

    # Zombie-dial guard (road_warrior chase bug symptom #2, playtest 2026-06-04).
    # A RESOLVED encounter's dial must never be silently nudged. The narrator kept
    # calling advance_confrontation on an abandoned (resolved) chase, creeping the
    # separation dial with zero mechanical backing — the exact "convincing
    # narration, no engine" lie the OTEL principle exists to catch. Refuse loudly
    # (recoverable: the turn proceeds on prose; the dead dial just doesn't move)
    # and surface the refusal on the GM panel (No Silent Fallbacks).
    if encounter.resolved:
        ctx.otel_span.set_attribute("tool.confrontation.refused_resolved", True)
        ctx.otel_span.set_attribute("tool.confrontation.encounter_type", encounter.encounter_type)
        ctx.otel_span.set_attribute("tool.confrontation.outcome", encounter.outcome or "")
        return ToolResult.error(
            f"encounter {encounter.encounter_type!r} is already resolved "
            f"(outcome={encounter.outcome!r}) — cannot advance a resolved "
            "confrontation's dial. A resolved encounter is over; its dial is "
            "frozen. If a new confrontation is starting, trigger it instead.",
            recoverable=True,
        )

    # Inert-dial guard (barsoom-2 playtest 2026-06-10). Under
    # ``win_condition=hp_depletion`` the dials are inert 1e6 placeholders —
    # the HP channel is the authoritative track, and ``apply_beat`` suppresses
    # its own dial mutation for exactly this reason (beat_kinds.py
    # dial_suppressed_hp_depletion). The narrator free-handing this tool
    # drifted a dead dial 0→4→−2 across a Blade-work fight, polluting the
    # persisted forensics (``final_player_metric=-2``) badly enough that the
    # playtest DRIVER hypothesized a momentum sign-flip in the resolver.
    # Refuse loudly (recoverable — the turn proceeds on prose) and surface the
    # refusal on the GM panel (No Silent Fallbacks), mirroring the two guards
    # above.
    if encounter.win_condition == "hp_depletion":
        ctx.otel_span.set_attribute("tool.confrontation.refused_hp_depletion", True)
        ctx.otel_span.set_attribute("tool.confrontation.encounter_type", encounter.encounter_type)
        ctx.otel_span.set_attribute("tool.confrontation.axis", args.axis)
        ctx.otel_span.set_attribute("tool.confrontation.delta", args.delta)
        return ToolResult.error(
            f"encounter {encounter.encounter_type!r} resolves via hp_depletion — "
            "its dials are inert placeholders and must not move; the HP channel "
            "is the authoritative track. Damage flows through committed beats "
            "and the dice engine. Do not free-hand dial advances on this "
            "confrontation.",
            recoverable=True,
        )

    # Opposed-check guard (RW-2 road_warrior chase, playtest 2026-06-05).
    # On a ``resolution_mode: opposed_check`` confrontation the DICE ENGINE
    # owns every dial delta: the player's stashed DICE_THROW d20 is paired
    # with the narrator-picked OPPONENT beat by
    # ``narration_apply._resolve_opposed_check_branch``, which derives the
    # tier and applies both sides. The playtest measured the narrator
    # free-handing this tool with invented deltas instead (sep +0 on a crit,
    # pursuit +2 from nowhere) — convincing narration, zero mechanical
    # backing. Refuse loudly (recoverable — the turn proceeds on prose) and
    # steer the narrator to the sanctioned channel. Mirrors the zombie-dial
    # guard above. ``genre_pack=None`` (legacy fixtures / un-wired call
    # sites) stands down — the mode cannot be determined without the pack.
    cdef = _find_active_cdef(ctx.genre_pack, encounter.encounter_type)
    if cdef is not None and str(getattr(cdef, "resolution_mode", "")) == "opposed_check":
        ctx.otel_span.set_attribute("tool.confrontation.refused_opposed_check", True)
        ctx.otel_span.set_attribute("tool.confrontation.encounter_type", encounter.encounter_type)
        ctx.otel_span.set_attribute("tool.confrontation.axis", args.axis)
        ctx.otel_span.set_attribute("tool.confrontation.delta", args.delta)
        return ToolResult.error(
            f"encounter {encounter.encounter_type!r} resolves via opposed_check — "
            "the dice engine derives every dial delta from the paired rolls; "
            "this tool must not move the dial. Emit the OPPONENT's "
            "beat_selection (actor + beat_id from the confrontation's beat "
            "list) in the game_patch instead; the engine rolls the opponent's "
            "d20 and applies both sides.",
            recoverable=True,
        )

    metric = encounter.player_metric if args.axis == "player" else encounter.opponent_metric
    value_before = metric.current
    metric.current = value_before + args.delta
    value_after = metric.current

    # No in-tool save: the dial move rides the canonical snapshot, which the
    # single end-of-turn ``room.save()`` persists. A second save here would be
    # redundant and re-create the ordering hazard.

    crossed_threshold = (value_before < metric.threshold) and (value_after >= metric.threshold)

    ctx.otel_span.set_attribute("tool.confrontation.id", args.confrontation_id)
    ctx.otel_span.set_attribute("tool.confrontation.axis", args.axis)
    ctx.otel_span.set_attribute("tool.confrontation.delta", args.delta)
    ctx.otel_span.set_attribute("tool.confrontation.value_after", value_after)
    ctx.otel_span.set_attribute("tool.confrontation.reason", args.reason)
    ctx.otel_span.set_attribute("tool.confrontation.crossed_threshold", crossed_threshold)
    # Story 73-3: the lie-detector signal — proves this advance mutated the
    # canonical snapshot (which the end-of-turn save persists), not a doomed
    # fresh-load copy. The GM panel uses this to confirm the dial move is real.
    ctx.otel_span.set_attribute("tool.confrontation.canonical", True)

    # RW-2: surface the narrator-driven dial move on the GM TIMELINE, not
    # just the tool span attrs — the playtest DRIVER could not attribute a
    # pursuit 4→5 tick because narrator moves were invisible next to the
    # engine's own state_transition events (OTEL Observability Principle).
    _watcher_publish(
        "state_transition",
        {
            "field": "encounter",
            "op": "narrator_dial_advance",
            "encounter_type": encounter.encounter_type,
            "axis": args.axis,
            "delta": args.delta,
            "reason": args.reason,
            "value_before": value_before,
            "value_after": value_after,
            "threshold": metric.threshold,
            "crossed_threshold": crossed_threshold,
            "source": "advance_confrontation",
        },
        component="encounter",
    )

    return ToolResult.ok(
        {
            "confrontation_id": args.confrontation_id,
            "axis": args.axis,
            "delta": args.delta,
            "value_before": value_before,
            "value_after": value_after,
            "threshold": metric.threshold,
            "crossed_threshold": crossed_threshold,
            "metric_name": metric.name,
        }
    )
