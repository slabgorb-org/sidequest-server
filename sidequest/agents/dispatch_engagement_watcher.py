"""Router-vs-engine lie-detector watcher — Story 59-3 / ADR-113.

Post-turn observer that compares what the Intent Router *dispatched* against
what the engines actually engaged on the resulting snapshot. Emits one
``dispatch_engagement.{subsystem}.mismatch`` OTEL span per detected
mismatch so the GM panel surfaces "convincing prose with zero mechanical
backing" turns immediately.

The watcher is a **pure decision** with a thin OTEL-emitting wrapper:

- :func:`detect_dispatch_engagement_mismatch` — pure function, no I/O, no
  tracer touch. Returns :class:`DispatchMismatch` records for callers that
  want to introspect decisions without spinning up an exporter.
- :func:`run_dispatch_engagement_watcher` — calls the pure function and
  emits one span per mismatch. The live integration site
  (``_execute_narration_turn`` in the websocket session handler) calls
  this wrapper.

Replaces the 59-1 self-report-reprompt path
(``confrontation_intent_mismatch_reprompt_failed_span`` + the reprompt
loop in ``_execute_narration_turn``) per memory
``feedback_one_mechanism_per_problem``. The new mechanism is router-
driven, not narrator-driven: the producer is the Intent Router (objective
reality), not the narrator (the suspect under investigation).

Fail-loud discipline (memory ``feedback_no_fallbacks_hard``):
- A malformed dispatch (missing ``type`` / ``actor`` / ``fact_id`` /
  ``npc_name``) is a real router defect — and it is reported as a
  **mismatch span**, the loudest channel available here: the
  ``dispatch_engagement.{subsystem}.mismatch`` span reaches the GM panel
  with evidence naming the missing key. The watcher does NOT raise on it.
  This watcher runs POST-narration in the WS turn pipeline; an uncaught
  ``KeyError`` here propagates to ``ws_endpoint`` and crashes turn
  *delivery* (playtest 2026-05-25: narration succeeded, len=1626, but the
  watcher crash closed the socket → MP UI hung). A crash that prevents the
  span from ever exporting is the *silent-worst* failure mode, not a loud
  one — the corrected contract surfaces the defect louder AND keeps the
  turn deliverable.
- Unknown subsystem names raise :class:`KeyError` via
  :func:`span_name_for_subsystem`.

The watcher does NOT correct, retry, or re-dispatch. It observes and
emits. Correction belongs to a later story (or to the operator reading
the GM panel).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass

from opentelemetry import trace

from sidequest.game.session import GameSnapshot
from sidequest.protocol.dispatch import DispatchPackage, SubsystemDispatch
from sidequest.telemetry.spans.dispatch_engagement import (
    dispatch_engagement_mismatch_span,
    dispatch_engagement_watcher_crashed_span,
    narration_improvised_combat_span,
    narration_unminted_objective_span,
)

logger = logging.getLogger(__name__)

# Curated combat-injury markers — phrases that almost exclusively appear when
# violence is actively LANDING on a body. Kept deliberately tight: the
# improvised-combat detector fires a GM-panel beep (observability), never a
# control-flow block, so a missed synonym is far cheaper than a flood of
# flavor-blood false alarms. The two state gates (no live encounter, no
# confrontation dispatched) do most of the discriminating; these markers only
# confirm the prose actually depicts a wound being dealt. Tunable as findings
# accrue. Matched case-insensitively as substrings.
_IMPROVISED_COMBAT_MARKERS: tuple[str, ...] = (
    "wet with blood",
    "slick with blood",
    "drives the blade",
    "drives the sword",
    "drives the spear",
    "sinks the blade",
    "buries the blade",
    "buries the sword",
    "runs you through",
    "runs him through",
    "runs her through",
    "opens a gash",
    "the blade bites",
    "blade across",
    "blood wells",
    "spurts blood",
)

# QUEST-MAJOR (sq-playtest 2026-06-14): curated objective-GIVING phrases — the
# specific constructions a narrator uses when a hook becomes a concrete quest (a
# giver names a task). Kept deliberately tight and biased toward precision: this
# fires a GM-panel beep (observability), never a control-flow block, and the
# empty-quest_log gate does the heavy discriminating, so a missed phrasing is far
# cheaper than crying wolf on every early-game scene that merely mentions a goal.
# Matched case-insensitively as substrings.
#
# DEPRECATED (ADR-146, Story 117-6): this keyword matcher is the Zork verb-set
# anti-pattern (SOUL: The Zork Problem) and is SUPERSEDED by the un-seeded
# post-narration classifier in
# ``sidequest.agents.post_narration_classifier.run_unseeded_objective_classifier_watcher``
# (real Haiku classification, keyword-free, emits detection_method="classifier").
# RETAINED as a non-primary emergency backstop for the router-silent /
# classifier-unavailable edge case — do NOT extend this list; add coverage by
# improving the classifier instead. Full removal deferred until the classifier has
# soaked in playtest.
_UNMINTED_OBJECTIVE_MARKERS: tuple[str, ...] = (
    "if you can find",
    "find the missing",
    "settle the debt",
    "pay the debt",
    "pay her debt",
    "pay his debt",
    "your task is",
    "i need you to find",
    "you must find",
    "find her and bring",
    "find him and bring",
    "bring it back to",
    "has not returned",
)


@dataclass(frozen=True)
class DispatchMismatch:
    """One detected router-vs-engine mismatch.

    Carries enough information for the wrapper to emit a useful span AND
    for callers that consume the pure function's output to filter /
    aggregate without re-deriving facts.
    """

    subsystem: str
    idempotency_key: str
    dispatched_type: str
    evidence: str


# ---------------------------------------------------------------------------
# Per-subsystem engagement witnesses
#
# Each witness answers: given this dispatch and this post-turn snapshot,
# did the engine engage? Returns ``None`` on engagement, or a short
# evidence string on mismatch. The evidence string is what the GM panel
# shows as the "why" of the lie-detector beep — keep it terse.
#
# Dispatch param key contract (TEA deviation note, RED phase):
#   - confrontation: params["type"]   → encounter.encounter_type
#   - magic_working: params["actor"]  → working_log[*].actor
#   - scenario_clue: params["fact_id"] → scenario_state.discovered_clues
#   - npc_agency:    params["npc_name"] → npc_pool[*].name (story 59-7)
#   - distinctive_detail_hint: always engaged (directive-only, no snapshot dep)
#   - reflect_absence: always engaged (directive-only, no snapshot dep)
# ---------------------------------------------------------------------------


# Evidence string for a dispatch missing (or nulling) its required
# identifying param. A missing key OR a non-string value (None, int, …) is
# a router defect the watcher SURFACES (mismatch span), not an exception it
# raises — raising would crash post-narration WS delivery (see module
# docstring; playtest 2026-06-07: params["npc_name"]=None → .lower() crash
# → WS teardown mid-turn, turn never persisted). The evidence names the
# offending key so the GM panel shows exactly what the router omitted or
# nulled.
_MALFORMED_EVIDENCE = (
    "malformed dispatch: router omitted or nulled required params['{key}'] for {subsystem}"
)


def _required_str_param(dispatch: SubsystemDispatch, key: str) -> str | None:
    """Return ``params[key]`` when it is a string; ``None`` when absent/non-string.

    The watcher's witnesses compare these values against snapshot state
    (often case-insensitively via ``.lower()``), so anything that is not a
    ``str`` — including an explicit ``None`` with the key present — is
    malformed, not merely missing.
    """
    value = dispatch.params.get(key)
    return value if isinstance(value, str) else None


def _check_confrontation_engaged(
    dispatch: SubsystemDispatch, snapshot: GameSnapshot, player_id: str | None
) -> str | None:
    dispatched_type = _required_str_param(dispatch, "type")
    if dispatched_type is None:
        return _MALFORMED_EVIDENCE.format(subsystem="confrontation", key="type")
    encounter = snapshot.encounter
    if encounter is None:
        return "snapshot.encounter is None"
    if encounter.encounter_type != dispatched_type:
        return (
            f"snapshot.encounter.encounter_type={encounter.encounter_type!r} "
            f"!= dispatched type={dispatched_type!r}"
        )
    return None


def _check_magic_working_engaged(
    dispatch: SubsystemDispatch, snapshot: GameSnapshot, player_id: str | None
) -> str | None:
    """magic_working witness — three engagement evidences (102-3, 102-7):

    1. WN cast spine: a turn-scoped ``WwnCastLogEntry`` receipt in
       ``snapshot.wwn_spell_cast_log`` for THIS turn and THIS actor (the
       59-30 ledger pattern; cast AND refused both count — the engine
       answered either way).
    2. AWN mutation engine (Story 102-7): a turn-scoped
       ``MutationUseLogEntry`` receipt in ``snapshot.mutation_use_log``
       (mutations ARE an AWN pack's magic; applied AND refused both count).
    3. ADR-126 pact-working: a ``WorkingRecord`` in
       ``magic_state.working_log`` (the original 59-3 evidence).

    Any satisfies the witness; none means the prose had no mechanical
    backing — mismatch.
    """
    actor = _required_str_param(dispatch, "actor")
    if actor is None:
        return _MALFORMED_EVIDENCE.format(subsystem="magic_working", key="actor")
    current_turn = snapshot.turn_manager.interaction
    if any(r.turn == current_turn and r.actor == actor for r in snapshot.wwn_spell_cast_log):
        return None
    if any(r.turn == current_turn and r.actor == actor for r in snapshot.mutation_use_log):
        return None
    magic_state = snapshot.magic_state
    if magic_state is None:
        return (
            f"no WN cast receipt and no mutation-use receipt for actor={actor!r} "
            f"at turn={current_turn} and snapshot.magic_state is None (no magic "
            "engine engaged)"
        )
    if not any(r.actor == actor for r in magic_state.working_log):
        return f"no WorkingRecord with actor={actor!r} in magic_state.working_log"
    return None


def _check_scenario_clue_engaged(
    dispatch: SubsystemDispatch, snapshot: GameSnapshot, player_id: str | None
) -> str | None:
    fact_id = _required_str_param(dispatch, "fact_id")
    if fact_id is None:
        return _MALFORMED_EVIDENCE.format(subsystem="scenario_clue", key="fact_id")
    scenario = snapshot.scenario_state
    if scenario is None:
        return "snapshot.scenario_state is None"
    if fact_id not in scenario.discovered_clues:
        return f"fact_id={fact_id!r} not in scenario_state.discovered_clues"
    return None


def _check_npc_agency_engaged(
    dispatch: SubsystemDispatch, snapshot: GameSnapshot, player_id: str | None
) -> str | None:
    npc_name = _required_str_param(dispatch, "npc_name")
    if npc_name is None:
        return _MALFORMED_EVIDENCE.format(subsystem="npc_agency", key="npc_name")
    needle = npc_name.lower()
    # npc_agency resolves against the authored roster (snapshot.npcs) first,
    # then the present-in-scene npc_pool (playtest #C1). The engagement check
    # must consider BOTH or it false-flags a mismatch for every roster NPC
    # (the crew, Old Tam) the subsystem correctly engaged.
    in_roster = any(n.core.name.lower() == needle for n in snapshot.npcs)
    in_pool = any(m.name.lower() == needle for m in snapshot.npc_pool)
    if not (in_roster or in_pool):
        return f"npc_name={npc_name!r} not in snapshot.npcs or snapshot.npc_pool"
    return None


def _check_distinctive_detail_engaged(
    dispatch: SubsystemDispatch, snapshot: GameSnapshot, player_id: str | None
) -> str | None:
    return None


def _check_reflect_absence_engaged(
    dispatch: SubsystemDispatch, snapshot: GameSnapshot, player_id: str | None
) -> str | None:
    return None


def _check_witnessed_act_engaged(
    dispatch: SubsystemDispatch, snapshot: GameSnapshot, player_id: str | None
) -> str | None:
    """witnessed_act witness — turn-scoped political-ledger read (Story 59-30).

    ``apply_witnessed_act`` appends a ``BeliefLedgerEntry`` (stamped with ``turn``
    + ``act_id``) for every dial it moves. Engaged iff such an entry exists for
    THIS turn and THIS act. Turn-scoping is REQUIRED — a prior-turn entry for the
    same act_id is NOT this-turn engagement (the false-negative TEA flagged).

    The engine resolves ``act_id = params.get("act_id") or
    params.get("act_archetype")`` (``witnessed_act.py:102``); the witness MUST
    mirror that alias or it false-flags a legitimately-engaged turn.
    """
    # Alias parity with the engine (witnessed_act.py:102) — NOT act_id-only.
    act_id = dispatch.params.get("act_id") or dispatch.params.get("act_archetype")
    if not act_id:
        return _MALFORMED_EVIDENCE.format(subsystem="witnessed_act", key="act_id")
    state = snapshot.political_state
    if state is None:
        return "snapshot.political_state is None (world has no political layer loaded)"
    current_turn = snapshot.turn_manager.interaction
    if any(e.turn == current_turn and e.act_id == act_id for e in state.ledger):
        return None
    return (
        f"no political ledger entry for act_id={act_id!r} at turn={current_turn} "
        f"(router dispatched witnessed_act; no dial moved)"
    )


def _check_movement_engaged(
    dispatch: SubsystemDispatch, snapshot: GameSnapshot, player_id: str | None
) -> str | None:
    """movement witness — "relocation-occurred", turn-scoped per-PC (Story 59-30).

    Reads the ``region_transitions`` ledger (stamped on both relocation seams).
    Engaged iff a transition exists for THIS turn and THIS PC. Per-PC keying
    keeps MP/split-party correct (PC-B's successful move must not mask PC-A's
    stuck move). The witness is via-agnostic — it reads turn + pc_name only — so
    a ``narration_apply`` (region-mode) stamp reads as engaged exactly like a
    ``world_patch`` one.

    The moving PC is resolved player_id → character name via ``player_seats``
    (the same character-name key the relocation writers use). No Silent
    Fallbacks: an unresolvable player_id surfaces LOUD evidence — never a guess,
    never a single-seat default.
    """
    if player_id is None:
        return (
            "movement dispatched with no owning player (cross_player movement is a router defect)"
        )
    pc_name = snapshot.player_seats.get(player_id)
    if pc_name is None:
        return (
            f"movement player_id={player_id!r} has no seat→character mapping "
            f"(player_seats={snapshot.player_seats!r})"
        )
    current_turn = snapshot.turn_manager.interaction
    if any(t.turn == current_turn and t.pc_name == pc_name for t in snapshot.region_transitions):
        return None
    return (
        f"no region_transition for pc={pc_name!r} at turn={current_turn} "
        f"(router dispatched movement; PC did not relocate)"
    )


def _check_quest_offer_engaged(
    dispatch: SubsystemDispatch, snapshot: GameSnapshot, player_id: str | None
) -> str | None:
    """quest_offer witness — "accept-minted-a-quest" (Story 117-3, ADR-146 §4).

    The structurally-sound replacement for the keyword ``detect_unminted_objective``:
    it fires on *router-claimed-but-engine-idle* (objective reality), not on
    *prose-matched-a-phrase* (string guess). When the router dispatched
    ``quest_offer accept`` for a named ``quest_id`` but that quest_id is absent
    from ``quest_log`` after the turn, the engine never minted — a real mismatch.

    A ``decline`` correctly leaves ``quest_log`` untouched, so it is never a
    mismatch (the engine honestly did not mint). A malformed dispatch (no
    ``quest_id``) surfaces loud evidence.
    """
    decision = dispatch.params.get("decision")
    if decision == "decline":
        return None  # honest non-mint — declining is engagement, not a miss.
    quest_id = _required_str_param(dispatch, "quest_id")
    if quest_id is None:
        return _MALFORMED_EVIDENCE.format(subsystem="quest_offer", key="quest_id")
    if quest_id not in snapshot.quest_log:
        return (
            f"quest_id={quest_id!r} not in quest_log after accept "
            f"(router dispatched quest_offer accept; engine minted nothing)"
        )
    return None


def _check_course_engaged(
    dispatch: SubsystemDispatch, snapshot: GameSnapshot, player_id: str | None
) -> str | None:
    """course witness — "course-was-plotted-or-arrived" (Story 153-5).

    Router-claimed-but-engine-idle for the orbital course/clock: when the router
    dispatched ``course`` for a named ``destination`` but neither a plotted
    course to that body nor an arrival there landed on the post-turn snapshot,
    the course/clock engine never engaged — a real mismatch (the narrator
    improvised the burn). A committed ``plotted_course`` OR an arrival
    (``party_body_id == destination``, the state after the course clears on
    arrival) is honest engagement.
    """
    destination = _required_str_param(dispatch, "destination")
    if destination is None:
        return _MALFORMED_EVIDENCE.format(subsystem="course", key="destination")
    plotted = snapshot.plotted_course
    if plotted is not None and plotted.to_body_id == destination:
        return None
    if snapshot.party_body_id == destination:
        return None  # arrived — the course committed then cleared on arrival.
    return (
        f"course dispatched to destination={destination!r} but no plotted_course "
        f"to it and no arrival (plotted_course="
        f"{plotted.to_body_id if plotted else None!r}, "
        f"party_body_id={snapshot.party_body_id!r}) — engine idle"
    )


_DISPATCHED_TYPE_KEY: dict[str, str] = {
    "confrontation": "type",
    "magic_working": "actor",
    "scenario_clue": "fact_id",
    "npc_agency": "npc_name",
    "distinctive_detail_hint": "target",
    "reflect_absence": "addressee_hint",
    "witnessed_act": "act_id",
    "movement": "direction",
    "quest_offer": "quest_id",
    "course": "destination",
}


_WITNESSES = {
    "confrontation": _check_confrontation_engaged,
    "magic_working": _check_magic_working_engaged,
    "scenario_clue": _check_scenario_clue_engaged,
    "npc_agency": _check_npc_agency_engaged,
    "distinctive_detail_hint": _check_distinctive_detail_engaged,
    "reflect_absence": _check_reflect_absence_engaged,
    "witnessed_act": _check_witnessed_act_engaged,
    "movement": _check_movement_engaged,
    "quest_offer": _check_quest_offer_engaged,
    "course": _check_course_engaged,
}


# ---------------------------------------------------------------------------
# Dispatch traversal — yields every SubsystemDispatch in the package
# (per_player AND cross_player). Watcher must cover both — PvP / cross-
# player dispatches are the same shape and must not slip past silently.
# ---------------------------------------------------------------------------


def _iter_all_dispatches(
    package: DispatchPackage,
) -> Iterable[tuple[str | None, SubsystemDispatch]]:
    """Yield ``(player_id, dispatch)`` for every dispatch in the package.

    ``per_player`` dispatches carry their owning ``player_id``; ``cross_player``
    dispatches have no single owner, so they yield ``player_id=None`` (a
    movement dispatch arriving via this leg is a router defect the witness
    surfaces). This player-attribution thread is shared infra that Story 59-31
    (opponent-yield) reuses.
    """
    for pd in package.per_player:
        for d in pd.dispatch:
            yield (pd.player_id, d)
    for ca in package.cross_player:
        for d in ca.dispatch:
            yield (None, d)


# ---------------------------------------------------------------------------
# Public API — pure function + OTEL-emitting wrapper
# ---------------------------------------------------------------------------


def detect_dispatch_engagement_mismatch(
    *,
    package: DispatchPackage | None,
    snapshot: GameSnapshot,
) -> list[DispatchMismatch]:
    """Detect router-vs-engine mismatches without touching OTEL.

    Returns one :class:`DispatchMismatch` per dispatch whose subsystem
    engagement witness returned non-None evidence. Quiet turns
    (``package=None`` or empty package) return ``[]``.

    Subsystems whose names are not in :data:`_WITNESSES` are *ignored* —
    not every router subsystem is the watcher's concern. As of story 153-5,
    ten live-path subsystems have witnesses: ``confrontation``,
    ``magic_working``, ``scenario_clue``, ``npc_agency``,
    ``distinctive_detail_hint``, ``reflect_absence``, ``witnessed_act``
    (turn-scoped political-ledger read), ``movement`` (per-PC
    relocation-occurred read), ``quest_offer`` (accept-minted-a-quest read,
    ADR-146 §4 — the structurally-sound replacement for the keyword
    unminted-objective detector), and ``course`` (course-plotted-or-arrived
    read, ADR-130/153-5 — the orbital course/clock engine engaged).
    """
    if package is None:
        return []
    mismatches: list[DispatchMismatch] = []
    for player_id, dispatch in _iter_all_dispatches(package):
        check = _WITNESSES.get(dispatch.subsystem)
        if check is None:
            continue  # subsystem outside the watcher's vocabulary
        evidence = check(dispatch, snapshot, player_id)
        if evidence is None:
            continue
        dispatched_type_key = _DISPATCHED_TYPE_KEY[dispatch.subsystem]
        dispatched_type = str(dispatch.params.get(dispatched_type_key, ""))
        mismatches.append(
            DispatchMismatch(
                subsystem=dispatch.subsystem,
                idempotency_key=dispatch.idempotency_key,
                dispatched_type=dispatched_type,
                evidence=evidence,
            )
        )
    return mismatches


def run_dispatch_engagement_watcher(
    *,
    package: DispatchPackage | None,
    snapshot: GameSnapshot,
    tracer: trace.Tracer | None = None,
) -> None:
    """Run the watcher and emit one OTEL span per detected mismatch.

    No-op when ``package is None`` (the live SDK path between 59-3 ship
    and 59-4 ship — ``turn_context.dispatch_package`` stays None until
    59-4 wires the producer onto the live turn pipeline).

    **Non-fatal by contract** (playtest 2026-06-07): this is a pure
    observability pass running POST-narration in the WS turn pipeline. A
    crash here previously propagated to ``ws_endpoint`` and tore down the
    connection AFTER narration broadcast but BEFORE ``session.persisted`` —
    the player lost the turn to a lie-detector bug. Any exception is caught,
    logged at ERROR, and surfaced as a loud
    ``dispatch_engagement.watcher.crashed`` span; the turn pipeline
    continues. The trade: that turn loses mismatch coverage — acceptable,
    because the alternative was losing the turn itself.
    """
    try:
        mismatches = detect_dispatch_engagement_mismatch(package=package, snapshot=snapshot)
        for m in mismatches:
            with dispatch_engagement_mismatch_span(
                subsystem=m.subsystem,
                idempotency_key=m.idempotency_key,
                dispatched_type=m.dispatched_type,
                evidence=m.evidence,
                _tracer=tracer,
            ):
                pass
    except Exception as exc:  # noqa: BLE001 — observability must never abort the turn
        logger.error(
            "dispatch_engagement.watcher_crashed error_type=%s error=%s "
            "(turn pipeline continues; mismatch coverage lost this turn)",
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        with dispatch_engagement_watcher_crashed_span(
            error_type=type(exc).__name__,
            error=str(exc),
            _tracer=tracer,
        ):
            pass


# ---------------------------------------------------------------------------
# Narration-vs-state lie-detector — the improvised-combat detector.
#
# The dispatch-engagement watcher above catches "router dispatched X, engine
# didn't engage X". This sibling catches the INVERSE failure the phantom-wound
# CRITICAL exposed (sq-playtest 2026-06-14, heavy_metal/barsoom): the router
# dispatched NOTHING (it errored on the schema), yet the narrator wrote a full
# sword wound — no encounter, no dice, no HP delta. There is no dispatch to
# check, so the engagement watcher is structurally blind to it. This detector
# reads the narration text against the snapshot instead.
# ---------------------------------------------------------------------------


def _package_dispatched_confrontation(package: DispatchPackage | None) -> bool:
    """True when the router emitted any ``confrontation`` dispatch this turn.

    A ``None`` package (the router failed/produced nothing — the phantom-wound
    case) means no confrontation was dispatched. When a confrontation WAS
    dispatched, ownership belongs to the dispatch-engagement confrontation
    witness, not this detector — so it stands down to avoid double-flagging.
    """
    if package is None:
        return False
    return any(d.subsystem == "confrontation" for _player_id, d in _iter_all_dispatches(package))


def detect_improvised_combat(
    *,
    narration: str,
    package: DispatchPackage | None,
    snapshot: GameSnapshot,
) -> str | None:
    """Detect narrated combat injury with zero mechanical backing.

    Returns a short evidence string when ALL hold, else ``None``:

    1. No live encounter — ``snapshot.encounter`` is None or already resolved.
       A live encounter mechanically backs the violence; not improvised.
    2. The router dispatched no ``confrontation`` this turn — so the
       dispatch-engagement confrontation witness is not already covering it
       (and the phantom-wound case, where the router produced nothing at all,
       is included).
    3. The narration contains a curated combat-injury marker — the prose
       actually depicts a wound being dealt, not merely a tense standoff.

    This is the narrator-improvised form of the Illusionism failure mode the
    OTEL panel exists to catch (SOUL: Genre Truth / mechanical scaffold).
    Pure — no I/O, no tracer touch — so callers can introspect without an
    exporter; the wrapper emits the span.
    """
    if not narration:
        return None
    encounter = snapshot.encounter
    if encounter is not None and not encounter.resolved:
        return None
    if _package_dispatched_confrontation(package):
        return None
    lowered = narration.lower()
    hits = [marker for marker in _IMPROVISED_COMBAT_MARKERS if marker in lowered]
    if not hits:
        return None
    return (
        f"narration depicts combat injury ({', '.join(hits[:3])}) but no encounter is "
        "active and the router dispatched no confrontation — no dice, no HP delta backs it"
    )


def run_improvised_combat_watcher(
    *,
    narration: str,
    package: DispatchPackage | None,
    snapshot: GameSnapshot,
    tracer: trace.Tracer | None = None,
) -> None:
    """Run the improvised-combat detector and emit one span on a hit.

    **Non-fatal by contract** — identical discipline to
    :func:`run_dispatch_engagement_watcher`: this is a pure-observability pass
    running POST-narration in the WS turn pipeline, so any exception is caught,
    logged, and surfaced as the watcher-crashed span rather than tearing down
    turn delivery.
    """
    try:
        evidence = detect_improvised_combat(narration=narration, package=package, snapshot=snapshot)
        if evidence is not None:
            with narration_improvised_combat_span(evidence=evidence, _tracer=tracer):
                pass
    except Exception as exc:  # noqa: BLE001 — observability must never abort the turn
        logger.error(
            "improvised_combat.watcher_crashed error_type=%s error=%s "
            "(turn pipeline continues; improvised-combat coverage lost this turn)",
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        with dispatch_engagement_watcher_crashed_span(
            error_type=type(exc).__name__,
            error=str(exc),
            _tracer=tracer,
        ):
            pass


def _package_accepted_quest_offer(package: DispatchPackage | None) -> bool:
    """True when the router dispatched a quest_offer ACCEPT this turn.

    This is the structural, OTEL-backed objective signal (Story 117-4): when the
    Intent Router (ADR-113) classified the player's turn as *accepting* an
    offered objective, it emits a ``quest_offer`` ``SubsystemDispatch`` with
    ``params["decision"] == "accept"`` (Story 117-3). Only an accept is an
    unminted-objective candidate, matching the engine's mint contract:

    - ``decline`` is an honest non-mint — the engine correctly mints nothing and
      emits ``quest.offer_declined`` — so it is NEVER a lie to catch (this mirrors
      the 117-3 witness ``_check_quest_offer_engaged``, which also returns ``None``
      on a decline). Firing here would flood the GM panel on every normal
      early-game "no thanks".
    - ``unknown_decision`` is already surfaced by ``quest_offer.py`` as its own
      ``quest_offer.mismatch`` — not this detector's concern.

    A ``None`` package (the router failed/produced nothing) means no objective was
    classified — the keyword backstop covers that un-seeded case.
    """
    if package is None:
        return False
    return any(
        d.subsystem == "quest_offer" and d.params.get("decision") == "accept"
        for _player_id, d in _iter_all_dispatches(package)
    )


def detect_unminted_objective(
    *,
    narration: str,
    snapshot: GameSnapshot,
    package: DispatchPackage | None = None,
) -> str | None:
    """Detect a concrete objective authored in prose with no minted quest.

    Two complementary paths, both gated on an EMPTY ``quest_log`` (nothing
    minted — a ``quest_offer`` accept, ``record_quest``, or ``seed_drive`` all
    land in ``quest_log``, so its emptiness is the single mint-vs-not gate):

    1. **Router-backed (Story 117-4, the seeded path).** When the Intent Router
       classified this turn as *accepting* an offered objective — a ``quest_offer``
       dispatch with ``decision == "accept"`` is present in ``package`` — but
       ``quest_log`` stayed empty, the engine never minted. This rides the
       router's structural classification (ADR-113), not a keyword guess, so an
       open-ended hook that trips ZERO curated markers (the perseus_cloud noir
       "discreet job" repro, session 594dcc7e) still beeps. A ``decline`` is an
       honest non-mint and is excluded (it never trips this path) — only an
       accept-without-mint is an unminted objective.
    2. **Keyword backstop (provisional, pending Story 117-6).** When the router
       emitted no ``quest_offer`` signal (``package=None`` or no objective
       dispatch — the un-seeded, narrator-improvised case), the curated
       ``_UNMINTED_OBJECTIVE_MARKERS`` substring path still fires on
       objective-giving prose. This keyword matcher is the Zork verb-set
       anti-pattern and is RETAINED only as a backstop until 117-6 builds the
       un-seeded narrator-objective classifier; do not extend it.

    Returns a short evidence string on a hit, else ``None``. The quest analogue
    of :func:`detect_improvised_combat`: a promotion that happened in narration
    but not in state (SOUL: Diamonds & Coal — taken bait must earn promotion into
    persistent state, not live only in prose). Pure — no I/O, no tracer touch —
    so callers can introspect without an exporter; the wrapper emits the span.
    """
    if not narration:
        return None
    quest_log = getattr(snapshot, "quest_log", None) or {}
    if quest_log:
        return None

    # Router-backed seeded path: the router dispatched a quest_offer ACCEPT this
    # turn but quest_log stayed empty — structural, keyword-free. Declines and
    # unknown-decisions are excluded (honest non-mint / already-flagged).
    if _package_accepted_quest_offer(package):
        return (
            "router dispatched a quest_offer accept this turn but quest_log is "
            "empty — the offer was accepted in prose, never minted into a tracked "
            "quest"
        )

    # Keyword backstop (un-seeded / router-silent): curated objective-giving prose
    # with no router signal. Provisional pending 117-6's un-seeded classifier.
    lowered = narration.lower()
    hits = [marker for marker in _UNMINTED_OBJECTIVE_MARKERS if marker in lowered]
    if not hits:
        return None
    return (
        f"narration establishes a concrete objective ({', '.join(hits[:3])}) but "
        "quest_log is empty — the hook was promoted in prose, never minted via "
        "record_quest"
    )


def run_unminted_objective_watcher(
    *,
    narration: str,
    snapshot: GameSnapshot,
    package: DispatchPackage | None = None,
    tracer: trace.Tracer | None = None,
) -> None:
    """Run the unminted-objective detector and emit one span on a hit.

    Threads the turn's router ``package`` (Story 117-4) into the detector so the
    seeded, OTEL-backed path can fire on the router's ``quest_offer``
    classification even when no curated keyword matches. ``package=None`` falls
    back to the keyword backstop (the un-seeded case, pending 117-6).

    **Non-fatal by contract** — identical discipline to
    :func:`run_improvised_combat_watcher`: a pure-observability post-narration
    pass, so any exception is caught, logged, and surfaced as the watcher-crashed
    span rather than tearing down turn delivery.
    """
    try:
        evidence = detect_unminted_objective(
            narration=narration, snapshot=snapshot, package=package
        )
        if evidence is not None:
            # Story 117-6: tag the span so the GM panel sees WHICH path flagged.
            # This sync watcher fires on two paths: the 117-4 router-backed
            # quest_offer-accept classification ("router") and the legacy curated
            # substring backstop ("keyword"). The post-narration Haiku classifier
            # ("classifier") emits from run_unseeded_objective_classifier_watcher.
            detection_method = "router" if _package_accepted_quest_offer(package) else "keyword"
            with narration_unminted_objective_span(
                evidence=evidence, detection_method=detection_method, _tracer=tracer
            ):
                pass
    except Exception as exc:  # noqa: BLE001 — observability must never abort the turn
        logger.error(
            "unminted_objective.watcher_crashed error_type=%s error=%s "
            "(turn pipeline continues; unminted-objective coverage lost this turn)",
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        with dispatch_engagement_watcher_crashed_span(
            error_type=type(exc).__name__,
            error=str(exc),
            _tracer=tracer,
        ):
            pass


__all__ = [
    "DispatchMismatch",
    "detect_dispatch_engagement_mismatch",
    "detect_improvised_combat",
    "detect_unminted_objective",
    "run_dispatch_engagement_watcher",
    "run_improvised_combat_watcher",
    "run_unminted_objective_watcher",
]
