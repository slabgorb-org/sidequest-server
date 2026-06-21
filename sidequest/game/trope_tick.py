"""Story 45-27 — trope progression tick engine.

The tick advances passive progression on every turn, fires beats with
stagger discipline (one beat per tick across all active tropes), and
gates new ``dormant → progressing`` transitions through a simultaneous-
active cap and a post-fire cooldown window.

Wire site: ``_execute_narration_turn`` in
``sidequest/server/websocket_session_handler.py`` calls
``tick_tropes(snapshot, genre_pack, now_turn=interaction)`` after
``record_interaction()`` so the cooldown bookkeeping aligns with the
panel's interaction count.

Design (per context-story-45-27.md):

1. **Pass A — progression.** For each progressing trope, advance
   ``progress`` by ``rate_per_turn * PROGRESSION_RATE_MULTIPLIER`` and
   emit a ``trope.tick`` span with before/after/delta.

2. **Pass B — beat fire (staggered).** Collect candidates: each
   progressing trope's next-unfired beat is a candidate when
   ``progress`` is at or past the beat's threshold. Stagger picks the
   single highest-progress candidate to fire — the others hold and
   become eligible on the next tick. The fire kicks the cooldown for
   that trope (``fire_cooldown_until = now_turn + FIRE_COOLDOWN_TURNS``).

3. **Pass C — implicit resolve.** A trope whose beats have all fired
   AND whose progress is at 1.0 transitions to ``resolved`` and emits
   ``trope_resolve`` carrying ``cooldown_until_turn``.

4. **Pass D — activation.** Each dormant trope is checked for
   activation eligibility. Cooldown (any active trope's
   ``fire_cooldown_until`` > now_turn) blocks first; cap blocks next.
   Eligible candidates become ``progressing``; refusals emit
   ``trope.cooldown_blocked`` or ``trope.cap_blocked`` for GM-panel
   visibility.

5. **Pass E — aggregate.** A ``turn.tropes`` span fires once per call,
   even when ``active_trope_count == 0``. Silence on the wire would
   lie about engine engagement.

The whole sequence is wrapped in the ``turn.tropes`` aggregate span
so per-trope spans register as children, giving the watcher hub
per-turn correlation for free.

Helper: ``select_foreground_tropes`` splits the progressing tropes
into ``(foreground, background)`` for the prompt-zone wiring in
``_build_turn_context``. Foreground is the K most-active by progress
(stable secondary sort by id); background is the rest.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sidequest.game import zone_eligibility
from sidequest.game.session import GameSnapshot, TropeState
from sidequest.game.trope_time_skip import _pass_a2_time_skip
from sidequest.game.trope_tuning import (
    FIRE_COOLDOWN_TURNS,
    FOREGROUND_K,
    MAX_SIMULTANEOUS_ACTIVE,
    PROGRESSION_RATE_MULTIPLIER,
)
from sidequest.telemetry.spans import (
    SPAN_TROPE_ACTIVATE,
    SPAN_TROPE_CAP_BLOCKED,
    SPAN_TROPE_COOLDOWN_BLOCKED,
    SPAN_TROPE_RESOLVE,
    SPAN_TROPE_TICK_PER,
    SPAN_TROPE_TIME_SKIP,
    SPAN_TURN_TROPES,
    Span,
)
from sidequest.telemetry.spans.zone_eligibility import SPAN_ZONE_ELIGIBILITY_FILTERED

# ---------------------------------------------------------------------------
# Public — the engine called from _execute_narration_turn.
# ---------------------------------------------------------------------------


def tick_tropes(
    snapshot: GameSnapshot,
    pack: Any,
    *,
    now_turn: int,
    days_advanced: int = 0,
) -> None:
    """Advance the trope engine by one turn.

    Mutates ``snapshot.active_tropes`` in place. ``pack`` is duck-typed
    on a single attribute, ``pack.tropes``, a list of
    :class:`~sidequest.genre.models.tropes.TropeDefinition`. The duck
    typing keeps test fixtures small (no full ``GenrePack`` required)
    and matches the usage at the wire site (``sd.genre_pack.tropes``).
    """

    pack_tropes_by_id: dict[str, Any] = {
        t.id: t for t in getattr(pack, "tropes", []) if t.id is not None
    }

    with Span.open(
        SPAN_TURN_TROPES,
        {"turn_number": now_turn},  # filled in below before exit
    ) as turn_span:
        # PASS A — progression for already-progressing tropes.
        _advance_progress(snapshot.active_tropes, pack_tropes_by_id)

        # PASS A2 — Story 50-4 time-skip drift. Only runs when the
        # narrator's game_patch carried days_advanced > 0. Fires every
        # crossed beat threshold (unlike Pass B's one-per-tick stagger)
        # since a multi-day jump implies multiple narrative beats land
        # off-screen between sessions.
        if days_advanced > 0:
            time_skip_fields = _pass_a2_time_skip(
                snapshot,
                pack_tropes_by_id,
                days_advanced=days_advanced,
                now_turn=now_turn,
            )
            with Span.open(
                SPAN_TROPE_TIME_SKIP,
                {
                    "days_requested": time_skip_fields.days_requested,
                    "days_applied": time_skip_fields.days_applied,
                    "clamped": time_skip_fields.clamped,
                    "tropes_affected": tuple(time_skip_fields.tropes_affected),
                    "tropes_skipped_zero_rate": tuple(time_skip_fields.tropes_skipped_zero_rate),
                    "beats_fired_count": time_skip_fields.beats_fired_count,
                    "resolved_during_skip": tuple(time_skip_fields.resolved_during_skip),
                },
            ):
                pass

        # PASS B — staggered beat fire (one beat per tick, max).
        # PASS C — implicit resolution after fire.
        _fire_one_staggered_beat(
            snapshot.active_tropes,
            pack_tropes_by_id,
            now_turn=now_turn,
            interaction=snapshot.turn_manager.interaction,
            genre_slug=snapshot.genre_slug,
        )

        # PASS D — activation gate. Seam 3 (epic-157, ADR-059 amendment): a
        # faction-zone candidate filter runs FIRST (a dormant trope tagged for a
        # faction outside the party's active zone is not even a candidate), then
        # cooldown, then cap. The zone context is resolved once here and only for
        # a zoned world (Cost Scales with Drama — the 11 single-zone packs skip the
        # region query entirely); an unzoned world passes zoned=False so
        # ``is_eligible`` is a no-op (zero behavior change).
        zoned = zone_eligibility.world_is_zoned(zone_eligibility.cartography_for(snapshot, pack))
        zone_active: set[str] = set()
        region = ""
        if zoned:
            zone_active = zone_eligibility.active_factions(snapshot, pack)
            region = snapshot.region_for() or ""
        queued_count = _gate_activations(
            snapshot.active_tropes,
            pack_tropes_by_id,
            now_turn=now_turn,
            zoned=zoned,
            active=zone_active,
            region=region,
        )

        # PASS E — aggregate metrics on the wrapping span.
        progressing = [t for t in snapshot.active_tropes if t.status == "progressing"]
        active_trope_count = len(progressing)
        progression_max = max((t.progress for t in progressing), default=0.0)
        progression_avg = (
            sum(t.progress for t in progressing) / len(progressing) if progressing else 0.0
        )
        # Match the predicate used in _gate_activations — cooldown extends
        # through (and including) the cooldown_until turn.
        cooldown_active = any(
            (t.fire_cooldown_until or 0) >= now_turn for t in snapshot.active_tropes
        )

        # Late-bind the metrics on the wrapping span so subscribers
        # see one event per turn carrying every required attribute.
        turn_span.set_attribute("active_trope_count", active_trope_count)
        turn_span.set_attribute("progression_max", float(progression_max))
        turn_span.set_attribute("progression_avg", float(progression_avg))
        turn_span.set_attribute("queued_count", queued_count)
        turn_span.set_attribute("cooldown_active", cooldown_active)


def select_foreground_tropes(
    active_tropes: Iterable[TropeState],
) -> tuple[list[TropeState], list[TropeState]]:
    """Split progressing tropes into ``(foreground, background)``.

    Foreground is the ``FOREGROUND_K`` most-active progressing tropes
    (sorted by progress descending, with id as a stable secondary key
    so tied progress does not churn the prompt turn-to-turn).
    Background is the remainder of the progressing tropes.

    Dormant and resolved tropes are not part of either list — dormant
    is "queued" (a different concept) and resolved is terminal.
    """

    progressing = [t for t in active_tropes if t.status == "progressing"]
    progressing.sort(key=lambda t: (-t.progress, t.id))
    return progressing[:FOREGROUND_K], progressing[FOREGROUND_K:]


# ---------------------------------------------------------------------------
# Internals.
# ---------------------------------------------------------------------------


def _advance_progress(
    active_tropes: list[TropeState],
    pack_tropes_by_id: dict[str, Any],
) -> None:
    """Pass A — passive progression for progressing tropes.

    Emits one ``trope.tick`` span per progressing trope so the GM
    panel can render a per-trope progression sparkline. Dormant and
    resolved tropes are inert — dormant must not catch up while
    cap-blocked (the cap would be cosmetic), and resolved is terminal.
    """

    for trope in active_tropes:
        if trope.status != "progressing":
            continue
        tdef = pack_tropes_by_id.get(trope.id)
        if tdef is None or tdef.passive_progression is None:
            continue
        rate = tdef.passive_progression.rate_per_turn or 0.0
        delta = rate * PROGRESSION_RATE_MULTIPLIER
        progress_before = trope.progress
        progress_after = min(1.0, progress_before + delta)
        trope.progress = progress_after
        with Span.open(
            SPAN_TROPE_TICK_PER,
            {
                "trope_id": trope.id,
                "progress_before": float(progress_before),
                "progress_after": float(progress_after),
                "delta": float(progress_after - progress_before),
                # Accelerator/decelerator keyword matching is left to a
                # future sub-story; surface zero so the route extract
                # stays consistent.
                "accelerator_hits": 0,
                "decelerator_hits": 0,
            },
        ):
            pass


def _fire_one_staggered_beat(
    active_tropes: list[TropeState],
    pack_tropes_by_id: dict[str, Any],
    *,
    now_turn: int,
    interaction: int,
    genre_slug: str,
) -> None:
    """Pass B+C — staggered beat fire and implicit resolution.

    Collects every progressing trope's next-unfired beat as a
    candidate and fires only the highest-progress one. The fire
    kicks the per-trope cooldown (``fire_cooldown_until``); the
    global cooldown gate in :func:`_gate_activations` reads it.

    If the fire completes the trope's escalation list AND progress
    has reached 1.0, the trope transitions to ``resolved`` and a
    ``trope_resolve`` span fires carrying ``cooldown_until_turn``.

    Resolution alone (without a fire) does not call this function —
    that path is the chapter-promotion handshake, owned by
    ``_handshake_resolved_tropes`` (Story 45-20).
    """

    candidates: list[tuple[TropeState, int, float]] = []
    for trope in active_tropes:
        if trope.status != "progressing":
            continue
        tdef = pack_tropes_by_id.get(trope.id)
        if tdef is None or not tdef.escalation:
            continue
        next_beat_index = trope.beats_fired
        if next_beat_index >= len(tdef.escalation):
            continue
        threshold = tdef.escalation[next_beat_index].at
        if trope.progress >= threshold:
            candidates.append((trope, next_beat_index, threshold))

    if not candidates:
        return

    # Stagger: only the single highest-progress trope fires this tick.
    # Tied progress: deterministic by id (stable across reloads).
    candidates.sort(key=lambda c: (-c[0].progress, c[0].id))
    winner, beat_index, _threshold = candidates[0]
    winner.beats_fired = beat_index + 1
    winner.last_fired_turn = now_turn
    cooldown_until = now_turn + FIRE_COOLDOWN_TURNS
    winner.fire_cooldown_until = cooldown_until

    # Implicit resolution: a trope with all beats fired AND progress
    # at 1.0 is terminal. Either condition alone is not enough —
    # progress can reach 1.0 before the final beat fires (last beat
    # threshold == 1.0), and beats can finish before progress reaches
    # 1.0 if the YAML ladder ends below 1.0.
    tdef = pack_tropes_by_id.get(winner.id)
    if tdef is not None and winner.beats_fired >= len(tdef.escalation) and winner.progress >= 1.0:
        winner.status = "resolved"
        with Span.open(
            SPAN_TROPE_RESOLVE,
            {
                "trope_id": winner.id,
                "interaction": interaction,
                "genre_slug": genre_slug,
                "final_progress": float(winner.progress),
                "beats_fired_total": int(winner.beats_fired),
                "cooldown_until_turn": int(cooldown_until),
            },
        ):
            pass


def _gate_activations(
    active_tropes: list[TropeState],
    pack_tropes_by_id: dict[str, Any],
    *,
    now_turn: int,
    zoned: bool = False,
    active: set[str] | None = None,
    region: str = "",
) -> int:
    """Pass D — gate dormant→progressing transitions through faction-zone
    eligibility, then cooldown, then cap. Returns the count of tropes held
    back this tick by cooldown/cap (queued).

    Seam 3 (epic-157, ADR-059 amendment): a dormant trope is a CANDIDATE only
    if ``is_eligible(tdef.factions, active, zoned)`` — a trope tagged for a
    faction outside the party's active zone is dropped before the cooldown/cap
    gates and fires a ``zone_eligibility.filtered`` span (the GM-panel
    lie-detector that the engine actively held it, not that it merely never
    rolled). ``zoned=False`` (the 11 single-zone packs, or a pre-bind turn)
    makes the predicate permissive — zero behavior change.

    Beyond the zone filter the activation predicate stays simple (any in-zone
    dormant is a candidate); future stories may add trigger-keyword evaluation.
    """

    active_factions = active or set()

    cooldown_until = max(
        ((t.fire_cooldown_until or 0) for t in active_tropes),
        default=0,
    )
    # Cooldown extends through (and including) the cooldown_until turn —
    # so a fire on turn N with FIRE_COOLDOWN_TURNS=2 blocks turns
    # N, N+1, N+2 and unblocks on N+3. The test in
    # ``test_trope_tick.py::TestTickFireCooldown::test_cooldown_blocks_new_activation``
    # pins this semantics directly.
    cooldown_active = cooldown_until >= now_turn

    progressing_count = sum(1 for t in active_tropes if t.status == "progressing")
    queued = 0

    for trope in active_tropes:
        if trope.status != "dormant":
            continue

        # Seam 3 faction-zone candidate filter — runs BEFORE cooldown/cap so a
        # wrong-zone trope is never a candidate; its exclusion is the
        # zone_eligibility.filtered span, not a cooldown/cap block, and it is not
        # counted as queued (it is not "held this tick" — it is out of zone).
        tdef = pack_tropes_by_id.get(trope.id)
        content_factions = list(getattr(tdef, "factions", [])) if tdef is not None else []
        if not zone_eligibility.is_eligible(content_factions, active_factions, zoned=zoned):
            with Span.open(
                SPAN_ZONE_ELIGIBILITY_FILTERED,
                {
                    "subsystem": "trope",
                    "content_id": trope.id,
                    "content_factions": sorted(content_factions),
                    "active_factions": sorted(active_factions),
                    "region": region,
                },
            ):
                pass
            continue

        # Cooldown gate first — Sebastien-tier visibility distinguishes
        # "cooldown blocked" from "cap blocked" with separate spans.
        if cooldown_active:
            with Span.open(
                SPAN_TROPE_COOLDOWN_BLOCKED,
                {
                    "trope_id": trope.id,
                    "cooldown_until_turn": int(cooldown_until),
                    "current_turn": int(now_turn),
                },
            ):
                pass
            queued += 1
            continue

        # Cap gate.
        if progressing_count >= MAX_SIMULTANEOUS_ACTIVE:
            with Span.open(
                SPAN_TROPE_CAP_BLOCKED,
                {
                    "trope_id": trope.id,
                    "current_active_count": int(progressing_count),
                    "cap": int(MAX_SIMULTANEOUS_ACTIVE),
                },
            ):
                pass
            queued += 1
            continue

        # Activate.
        from_status = trope.status
        trope.status = "progressing"
        progressing_count += 1
        with Span.open(
            SPAN_TROPE_ACTIVATE,
            {
                "trope_id": trope.id,
                "from_status": from_status,
                "to_status": "progressing",
                "cap_used": int(progressing_count),
            },
        ):
            pass

    return queued


# ---------------------------------------------------------------------------
# Prompt-zone helpers — render the foreground / background blocks the
# narrator sees. _build_turn_context (session_helpers.py) reads these
# and assigns to TurnContext.pending_trope_context (Early zone) and
# TurnContext.active_trope_summary (Valley zone).
# ---------------------------------------------------------------------------


def render_foreground_block(
    foreground: list[TropeState],
    pack_tropes_by_id: dict[str, Any],
) -> str:
    """Format the K most-active tropes as a foreground prompt block.

    Empty input returns the empty string; the caller normalizes to
    ``None`` so the orchestrator's prompt-section registry skips
    registration entirely (zero-byte-leak per the discipline at
    orchestrator.py:1320).
    """

    if not foreground:
        return ""

    lines = ["[ACTIVE TROPES — load-bearing this turn]"]
    for trope in foreground:
        tdef = pack_tropes_by_id.get(trope.id)
        next_beat = _next_beat_summary(trope, tdef)
        lines.append(f"- {trope.id} (progress {trope.progress:.2f}): {next_beat}")
    return "\n".join(lines)


def render_background_block(
    background: list[TropeState],
    pack_tropes_by_id: dict[str, Any],
) -> str:
    """Format the Valley-zone background summary.

    Tighter than the foreground — one line per trope, no beat
    directive, just enough context that the narrator can reference the
    thread without the prompt budget cost of a full directive.
    """

    if not background:
        return ""

    lines = ["[BACKGROUND TROPES — context only]"]
    for trope in background:
        lines.append(f"- {trope.id}: progress {trope.progress:.2f}")
    return "\n".join(lines)


def _next_beat_summary(trope: TropeState, tdef: Any | None) -> str:
    if tdef is None or not getattr(tdef, "escalation", None):
        return "no escalation defined"
    next_index = trope.beats_fired
    if next_index >= len(tdef.escalation):
        return "all beats fired, awaiting resolution"
    beat = tdef.escalation[next_index]
    event_short = (beat.event or "").splitlines()[0][:120]
    return f"next beat at {beat.at:.2f} — {event_short}"
