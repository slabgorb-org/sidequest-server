"""intent_router_pass — pre-narrator engagement helper (Story 59-4, ADR-113).

The Intent Router (``sidequest/agents/intent_router.py``) and the
dispatch bank (``sidequest/agents/subsystems/__init__.py:run_dispatch_bank``)
are independently testable units. The wiring that runs the router and
then the bank against a turn's snapshot/pack/action lived as a dormant
comment in ``websocket_session_handler._execute_narration_turn`` between
Stories 59-2 and 59-4; this module is the extracted callable that
replaces the comment with real wiring.

Extracting the pre-narrator pass into one function does three things:

  1. Makes the wiring testable in isolation (the session-handler's
     turn pipeline has many cross-cutting concerns — monster manual
     injection, status-tick handshakes, OTEL bridge bookkeeping — that
     swamp a focused test of the router→bank ordering).
  2. Keeps the failure surface explicit: ``IntentRouterFailure``
     propagates out of this function and gets handled by the caller
     with no swallow-to-fallback shenanigans (memory rule
     ``feedback_no_fallbacks_hard``).
  3. Provides a reusable seam for future scenes/openings that need to
     engage the spine outside the main narration turn (e.g., scene
     transitions, opening setpieces) — Story 59-5+ may call this from
     non-_execute_narration_turn sites.

Ordering contract: when this function returns successfully, all
mechanical engines for the dispatched subsystems have engaged on the
snapshot. The narrator (which runs after this) sees already-real
state. That is the SOUL Illusionism counter the epic exists to deliver.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sidequest.agents.dispatch_precondition_gate import (
    run_dispatch_precondition_gate,
    run_unregistered_subsystem_gate,
)
from sidequest.agents.intent_router import IntentRouter, _serialize_state_summary
from sidequest.agents.subsystems import BankResult, get_registered, run_dispatch_bank
from sidequest.dungeon.region_projection import project_region
from sidequest.dungeon.seed_bootstrap import ENTRANCE_ID as _DUNGEON_ENTRANCE_ID
from sidequest.game.npc_scene import is_npc_in_scene
from sidequest.game.ruleset.fate_projection import build_fate_projection
from sidequest.game.seams import seam_route_for, surface_owner_for_entrance
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.pack import GenrePack
from sidequest.protocol.dispatch import (
    DispatchPackage,
    SubsystemDispatch,
    VisibilityTag,
)
from sidequest.server.ability_invocation_telemetry import (
    emit_ability_invocation_unrouted,
)
from sidequest.server.snapshot_slimming import apply_snapshot_slimming
from sidequest.telemetry.phase_timing import PhaseTimings
from sidequest.telemetry.spans.intent_router import (
    intent_router_beat_outside_confrontation_span,
    intent_router_call_budget_breach_span,
    intent_router_confrontation_classified_span,
    intent_router_confrontation_vocabulary_span,
    intent_router_region_exits_span,
    intent_router_state_summary_slimmed_span,
    intent_router_witnessed_act_classified_span,
    intent_router_witnessed_act_vocabulary_span,
)

logger = logging.getLogger(__name__)

# Story 91-2 (epic 91 "Dark Spend"): the documented per-turn Haiku
# classification call budget. The [COST-1] forensics (2026-06-05) measured
# ~8 classification calls/turn against a design expectation of ~1; this
# budget is the per-turn assertion that a recurrence cannot pass silently.
#
# Why 2: one pre-narrator pass per turn makes exactly ONE ``decompose`` call,
# and ``decompose`` is bounded at ``_MAX_TOTAL_ATTEMPTS = 2`` SDK round-trips
# (first attempt + one informed retry on timeout/transport/empty/schema). Two
# round-trips is therefore the maximum a LEGITIMATE turn can spend; a third
# cannot come from the retry loop and means a structural multiplier returned
# (e.g. the dice-replay re-entry this story removed). The legitimate retry
# already surfaces its own ERROR evidence via ``intent_router.failed`` — the
# budget breach is reserved for structurally impossible counts.
#
# Read LATE-BOUND (module attribute at call time) so tests and operators
# diagnosing a storm can patch it — same contract as 91-1's
# ``build_async_anthropic`` choke-point seam.
INTENT_ROUTER_CALL_BUDGET_PER_TURN: int = 2


def build_intent_router_for_session(*, session_id: str | None) -> IntentRouter:
    """Construct the production IntentRouter for a turn.

    Extracted module-level so tests can monkeypatch this factory with
    a stub that returns a ``MagicMock`` (or a fake router yielding an
    empty ``DispatchPackage``) without needing ``ANTHROPIC_API_KEY`` in
    the test environment. Tests MUST NOT spawn a real Claude client —
    pre-existing project rule mirrored from how ``Orchestrator`` is
    stubbed via ``MagicMock(spec=Orchestrator)`` in
    ``tests/server/conftest.py``.

    Production callers reach this through
    ``websocket_session_handler._execute_narration_turn`` once per turn
    (the SDK client is lightweight; per-turn construction matches the
    transient AnthropicAsync client lifecycle and avoids stale
    connection state across long-lived sessions).

    Story 91-4: ``session_id`` is required keyword-only and flows into
    the Haiku adapter so the router's every-turn spend runs the ADR-134
    detector and feeds the per-session cumulative ceiling. The caller
    has the canonical id (room slug / ``sd.game_slug``) — passing
    ``None`` is the explicit sessionless opt-out, never a default.
    """
    # Lazy import — keeps the helper module importable in test envs
    # that have not set ANTHROPIC_API_KEY (the SDK client validates the
    # key at construction).
    from sidequest.agents.llm_factory import build_intent_router_llm

    return IntentRouter(llm=build_intent_router_llm(session_id=session_id))


def _present_npc_names(snapshot: GameSnapshot) -> list[str]:
    """Return the names of NPCs the player's action could be witnessed by.

    The witness candidate set for ``witnessed_act`` classification (spec §5: an
    act with no witness moves nothing). Reuses the canonical scene-membership
    predicate (``sidequest/game/npc_scene.py``) — the SAME one the narrator's
    scene projection trusts — so "present" means here what it means everywhere
    else in the system (no parallel, divergent definition). Scene id is the
    party's consensus location; an unresolved location (pre-chargen / party
    split) yields an empty set unless an unresolved encounter anchors actors.
    """
    current_room = snapshot.party_location()
    encounter = getattr(snapshot, "encounter", None)
    names: list[str] = []
    for npc in snapshot.npcs or []:
        if is_npc_in_scene(npc, current_room=current_room, encounter=encounter):
            names.append(npc.core.name)
    return names


def _witnessed_act_ids(package: DispatchPackage) -> list[str]:
    """Collect the act_ids of every witnessed_act dispatch in the package."""
    ids: list[str] = []
    for pd in package.per_player:
        for d in pd.dispatch:
            if d.subsystem == "witnessed_act":
                ids.append(str((d.params or {}).get("act_id", "")))
    for ca in package.cross_player:
        for d in ca.dispatch:
            if d.subsystem == "witnessed_act":
                ids.append(str((d.params or {}).get("act_id", "")))
    return ids


def _confrontation_types_emitted(package: DispatchPackage) -> list[str]:
    """Collect the type of every confrontation dispatch in the package."""
    types: list[str] = []
    for pd in package.per_player:
        for d in pd.dispatch:
            if d.subsystem == "confrontation":
                types.append(str((d.params or {}).get("type", "")))
    for ca in package.cross_player:
        for d in ca.dispatch:
            if d.subsystem == "confrontation":
                types.append(str((d.params or {}).get("type", "")))
    return types


def _confrontation_verb_hits(action: str, pack: GenrePack | None) -> list[str]:
    """Lexical ``type:verb`` matches between the action and authored intent_verbs.

    Word-boundary, case-insensitive — the deterministic half of the
    standoff-seam decline detector (sq-playtest 2026-06-07). A hit here with
    zero confrontation dispatches emitted is the loud unrouted shape; the
    Haiku-judgment half (paraphrased intent, no literal verb) is steered by
    the pre-combat paragraph in ``CONFRONTATION_TRIGGER_CORE`` and cannot be
    lexically detected, by construction.
    """
    # getattr walk — duck-typed test packs (bare objects, fakes without
    # ``rules``) pass through, same access style as the witnessed_acts gate.
    rules = getattr(pack, "rules", None)
    confrontations = getattr(rules, "confrontations", None) if rules else None
    if not confrontations:
        return []
    folded = action.casefold()
    hits: list[str] = []
    for cdef in confrontations:
        for verb in getattr(cdef, "intent_verbs", None) or []:
            if re.search(rf"\b{re.escape(verb.casefold())}\b", folded):
                hits.append(f"{cdef.confrontation_type}:{verb}")
    return hits


def _beat_invocations_outside_confrontation(
    action: str, pack: GenrePack | None, snapshot: GameSnapshot
) -> list[tuple[str, str]]:
    """``(confrontation_type, beat_id)`` hits for MULTI-WORD beat labels
    invoked by name while no confrontation is active.

    sq-playtest 2026-06-07 (Size Up outside the standoff): a confrontation-only
    beat invoked by name out of context was freehanded as prose with zero gate
    telemetry. Single-word labels ("Shoot", "Retreat") are ordinary verbs, not
    invocations — excluded by design so normal prose stays quiet; single-verb
    intent is the confrontation classifier's lane (see
    ``_confrontation_verb_hits``).
    """
    enc = snapshot.encounter
    if enc is not None and not getattr(enc, "resolved", False):
        return []
    rules = getattr(pack, "rules", None)
    confrontations = getattr(rules, "confrontations", None) if rules else None
    if not confrontations:
        return []
    folded = action.casefold()
    hits: list[tuple[str, str]] = []
    for cdef in confrontations:
        for beat in getattr(cdef, "beats", None) or []:
            label = (getattr(beat, "label", "") or "").strip()
            if " " not in label:
                continue
            if re.search(rf"\b{re.escape(label.casefold())}\b", folded):
                hits.append((cdef.confrontation_type, str(getattr(beat, "id", ""))))
    return hits


# ADR-144 F2b (Story 116-2): the Fate projection relocated to the game layer so the
# narrator prompt builder shares ONE projector with this router pass. ``_build_fate_summary``
# is kept as a thin back-compat alias for the F2a callers/tests that import it directly.
_build_fate_summary = build_fate_projection


def _build_state_summary(
    snapshot: GameSnapshot,
    *,
    pack: GenrePack | None = None,
    dungeon_store: Any | None = None,
    palette: Any | None = None,
) -> dict[str, Any]:
    """Build the slimmed JSON-able state summary the router consumes.

    ADR-110 Phase A: ``model_dump`` with ``exclude_defaults`` and
    ``exclude_none`` drops empty/zero pydantic defaults so the
    router's classification call doesn't pay for noise.

    Story 82-10 / ADR-110 amendment ("extract-and-reuse, not a second
    slimmer"): the dump now also gets the shared
    ``apply_snapshot_slimming`` cut — Phase B field-pruning drop +
    Phase C projections (npcs → in-scene only, room_states → current
    room, known_facts tail-K, clues cap) — the same audited cut the
    narrator's ``_build_turn_context`` applies. The 2026-06-06 router
    corpus (295 real rows) measured the unslimmed summary at p50 34.9KB
    / p95 54.3KB with ``npcs`` up to 82% of the payload; the router only
    *picks dispatch handlers*, a strictly weaker need than the
    narrator's anti-confabulation contract, so every narrator-safe drop
    is router-safe (ADR-110 amendment §"Why this is safe for the
    router"). Actor location resolves in party-consensus mode
    (``party_location()`` with no perspective) — on a split party it
    returns ``None`` and the room/NPC projections pass through, loud,
    per the gaslighting doctrine.

    Router-specific extra drop (82-10, corpus-measured): ``world_history``
    — full chapter prose, 15KB median where populated, zero dispatch
    value. The narrator keeps it (anti-confabulation anchor for citing
    campaign history); the router never reads it. This is one targeted
    negative drop, NOT the deferred positive-allowlist follow-up the
    amendment scopes out.

    Story 59-4 kept this builder local to avoid coupling the router
    pass to the much heavier ``session_helpers._build_turn_context``
    (sealed-letter handshakes, party-peer assembly, notorious-party
    gating — orthogonal concerns the router does not care about); the
    82-10 extraction honors that by sharing only the consumer-agnostic
    cut via ``sidequest.server.snapshot_slimming``.

    Story 59-10: when ``pack`` is provided, appends a compact
    ``confrontation_types`` projection so the Haiku router knows the
    valid type names for the current genre pack.

    Story 59-27: the projection now also carries each type's authored
    ``intent_verbs``. 59-10 shipped a ``{type, category}``-only projection
    on the theory that Haiku's language understanding would bridge prose to
    a type from the name + category alone. The wry_whimsy/oz playtest
    (2026-06-02) disproved it: across a full session only ``escape``
    (movement) ever fired and only after physical escalation — the verbal
    types (``persuasion``/``audience``/``wit_duel``/``wonder_shock``) NEVER
    dispatched, because the shared ``CONFRONTATION_TRIGGER_CORE`` recognition
    rules name social-trigger TYPES from other packs (negotiation/trial/
    auction/social_duel/scandal) and a category alone ("social") is too thin
    a signal to route "convince me he's worth the walk" to ``persuasion``.
    The authored verbs ARE the lexical bridge — emitting them gives the
    router a per-type vocabulary to match natural verbal prose against. The
    cost is modest (a handful of words per type) and follows
    "Cost Scales with Drama": routing the mechanical spine is exactly where
    the extra tokens belong.
    """
    summary = snapshot.model_dump(
        mode="json",
        exclude_defaults=True,
        exclude_none=True,
    )
    bytes_before = len(_serialize_state_summary(summary).encode("utf-8"))

    # Shared ADR-110 Phase B + C cut (Story 82-10). Consensus-mode actor
    # location: with no ``perspective`` the call returns the party's agreed
    # location, or ``None`` on a split party / pre-chargen — in which case
    # the room/NPC projections pass through (bigger prompt, never a
    # gaslit-empty one) and the span below records projection_skipped=True.
    current_room_id = snapshot.party_location()
    if not current_room_id:
        logger.warning(
            "intent_router.state_summary_slimmed projection_skipped "
            "reason=actor_location_unresolved interaction=%d",
            snapshot.turn_manager.interaction,
        )
    counts = apply_snapshot_slimming(snapshot, summary, current_room_id=current_room_id)

    # Router-specific drop (82-10, corpus-measured — see docstring): full
    # chapter prose with zero dispatch-selection value. Narrator payload
    # unaffected (its builder never pops this field).
    summary.pop("world_history", None)

    if pack is not None:
        confrontation_defs = pack.rules.confrontations if pack.rules else []
        if confrontation_defs:
            projection: list[dict[str, Any]] = []
            verb_count = 0
            for cdef in confrontation_defs:
                entry: dict[str, Any] = {
                    "type": cdef.confrontation_type,
                    "category": cdef.category,
                }
                # The lexical bridge (Story 59-27): the authored verbs let the
                # router match natural prose to a type. Omit the key entirely
                # for a type that declares none, rather than emitting an empty
                # list (keeps the projection compact and honest about what the
                # pack authored).
                if cdef.intent_verbs:
                    entry["intent_verbs"] = list(cdef.intent_verbs)
                    verb_count += len(cdef.intent_verbs)
                projection.append(entry)
            summary["confrontation_types"] = projection
            with intent_router_confrontation_vocabulary_span(
                type_count=len(confrontation_defs),
                genre_slug=snapshot.genre_slug or "",
                # GM-panel evidence the verbal vocabulary actually reached the
                # router this turn (Story 59-27): a zero here on a social pack
                # means the lexical bridge is missing and verbal confrontations
                # will silently fail to route, exactly the regression this fix
                # closes.
                verb_count=verb_count,
            ):
                pass

    # Fate vocabulary (ADR-144 F2a): when the pack binds the Fate ruleset, the
    # router needs the PCs' skills + the live aspects to classify a freeform
    # action into one of the four Fate actions. Gated on the ruleset slug so no
    # non-Fate pack's router prompt carries this block (same conditional-vocab
    # discipline as confrontation_types / witnessed_act_vocabulary above).
    if pack is not None and getattr(pack.rules, "ruleset", "") == "fate":
        summary["fate"] = _build_fate_summary(snapshot)

    # Witnessed-act vocabulary + witness candidate set (wry_whimsy political
    # substrate, Plan 2b). Double-gated: the pack must declare witnessed-act
    # archetypes AND the world must have hydrated a political layer
    # (snapshot.political_state). The second gate keeps us from prompting the
    # model to emit a dispatch the precondition gate would immediately drop —
    # and keeps every non-political genre's router prompt free of this noise.
    if (
        pack is not None
        and getattr(pack, "witnessed_acts", None)
        and snapshot.political_state is not None
    ):
        summary["witnessed_act_vocabulary"] = [
            {"id": a.id, "label": a.label, "description": a.description}
            for a in pack.witnessed_acts
        ]
        present = _present_npc_names(snapshot)
        summary["present_npcs"] = present
        with intent_router_witnessed_act_vocabulary_span(
            act_count=len(pack.witnessed_acts),
            present_npc_count=len(present),
            genre_slug=snapshot.genre_slug or "",
        ):
            pass

    # Story 105-2 (Piece 2): the PC's current cartography region's actual
    # exits — adjacency neighbors + seam routes. The 2026-06-12 dive's
    # turn-3 miss happened because the router was asked to recognize a
    # descent it was never told existed; this is the lexical bridge
    # (59-27 precedent: authored vocabulary beats inference). Region
    # resolved in party-consensus mode (no perspective) to match the
    # room/NPC projections above. A split party (or any unseeded seat)
    # makes region_for() return None, so the projection is OMITTED — the
    # router gets NO exit vocabulary that turn. The warning below is the
    # GM-panel evidence distinguishing "split party swallowed the exits"
    # from "this world has no cartography" (which is silent by design).
    if pack is not None:
        _worlds = getattr(pack, "worlds", None)
        _world = _worlds.get(snapshot.world_slug) if _worlds else None
        _cart = getattr(_world, "cartography", None)
        if _cart is not None:
            _region_id = snapshot.region_for() or ""
            if not _region_id:
                logger.warning(
                    "intent_router.region_exits projection_skipped "
                    "reason=region_unresolved interaction=%d",
                    snapshot.turn_manager.interaction,
                )
            _region = _cart.regions.get(_region_id) if _region_id else None
            region_exits: list[dict[str, str]] = []
            if _region is not None:
                for adj_id in _region.adjacent:
                    adj = _cart.regions.get(adj_id)
                    # Raw-id fallback only for DANGLING adjacency — an
                    # authored neighbor id with no region entry, which is
                    # the pack validator's concern, not ours.
                    region_exits.append(
                        {
                            "name": adj.name if adj is not None else adj_id,
                            "kind": "adjacent",
                        }
                    )
                seam = seam_route_for(_cart, _region_id)
                if seam is not None:
                    region_exits.append({"name": seam.name, "kind": "seam"})
                if region_exits:
                    summary["current_region_exits"] = region_exits
                    with intent_router_region_exits_span(
                        exit_count=len(region_exits),
                        seam_count=sum(1 for e in region_exits if e["kind"] == "seam"),
                        region_id=_region_id,
                        genre_slug=snapshot.genre_slug or "",
                    ):
                        pass
            elif _region_id:
                # Pingpong 2026-06-12: the PC's region is NOT a cartography
                # region — post seam-crossing it is a dungeon graph node
                # ('entrance' / 'expNNN.rN'). The silent skip here was root
                # cause #2 of the confabulated-crawl bug: the router was never
                # told the dungeon has exits, so in-dungeon descent intents
                # never classified as movement. Project the REAL graph exits
                # (hidden edges withheld unless the route is discovered —
                # reverse-Illusionism, same rule as movement §Q1 step 3).
                _graph = (
                    dungeon_store.load_map(entrance_id=_DUNGEON_ENTRANCE_ID)
                    if dungeon_store is not None
                    else None
                )
                _is_graph_node = (
                    _graph is not None and _region_id in _graph.nodes and palette is not None
                )
                node_exits: list[dict[str, str]] = []
                if _is_graph_node:
                    assert _graph is not None and palette is not None
                    _proj = project_region(_graph, _region_id, palette)
                    _discovered_routes = set(snapshot.discovered_routes or [])
                    node_exits.extend(
                        {"name": e.to_region_id, "kind": e.kind}
                        for e in _proj.exits
                        if (not e.hidden) or (e.to_region_id in _discovered_routes)
                    )
                # Story 105-3: at the entrance node the onward vocabulary ALSO
                # includes the seam back UP to the surface region that owns the
                # crossing — the reverse of the 105-2 descent bridge. Without it
                # a "back up the rope" intent has no exit to map onto and the
                # party is stranded below. This is derivable from cartography
                # ALONE (the registered-kind route's from_id), so it is added
                # independently of the dungeon graph above — the router must
                # offer the ascent even before the dungeon store is attached. No
                # owner (or an ambiguous map) → no fabricated exit (No Silent
                # Fallbacks).
                if _region_id == _DUNGEON_ENTRANCE_ID:
                    ascent = surface_owner_for_entrance(_cart)
                    if ascent is not None:
                        surface_id = ascent.from_id
                        owner = _cart.regions.get(surface_id) if surface_id else None
                        node_exits.append(
                            {
                                "name": owner.name
                                if owner is not None
                                else (surface_id or _region_id),
                                "kind": "seam",
                            }
                        )
                if node_exits:
                    summary["current_region_exits"] = node_exits
                    with intent_router_region_exits_span(
                        exit_count=len(node_exits),
                        seam_count=sum(1 for e in node_exits if e["kind"] == "seam"),
                        region_id=_region_id,
                        genre_slug=snapshot.genre_slug or "",
                    ):
                        pass
                elif not _is_graph_node:
                    # Neither a reachable dungeon node nor a cartography-derivable
                    # ascent — an unmapped position. Loud skip (No Silent
                    # Fallbacks): the GM panel must be able to tell "router got
                    # no exit vocabulary" from "world has no cartography". A valid
                    # graph node that simply has no open exits is NOT warned —
                    # that is an honest dead-end, not a wiring gap.
                    logger.warning(
                        "intent_router.region_exits projection_skipped "
                        "reason=region_unmapped region_id=%s store=%s interaction=%d",
                        _region_id,
                        "present" if dungeon_store is not None else "absent",
                        snapshot.turn_manager.interaction,
                    )

    # 82-10 before/after evidence — fires once per pass, AFTER the
    # router-specific additions so bytes_after is what actually ships to
    # the model (the ADR-110 amendment's mandated GM-panel contract).
    bytes_after = len(_serialize_state_summary(summary).encode("utf-8"))
    with intent_router_state_summary_slimmed_span(
        bytes_before=bytes_before,
        bytes_after=bytes_after,
        npcs_dropped=counts["npcs_dropped"],
        room_states_dropped=counts["room_states_dropped"],
        projection_skipped=not current_room_id,
    ):
        pass

    return summary


def _normalize_per_player_ids(
    package: DispatchPackage, *, snapshot: GameSnapshot, player_name: str
) -> None:
    """Normalize each ``per_player`` entry's LLM-emitted ``player_id`` to the
    real submitting seat id — Story 59-30 (Architect CRY-WOLF ruling).

    The IntentRouter's ``per_player[].player_id`` is **purely LLM-emitted**:
    ``decompose`` sees only ``(action, state_summary)`` and the model copies or
    guesses a value with nothing constraining it. It is **unconsumed** on the
    live bank/orchestrator path EXCEPT by the ``movement`` engagement witness,
    which resolves ``player_id → character name`` via ``snapshot.player_seats``
    to key its per-PC relocation read. An unresolvable id makes that witness
    fire a ``dispatch_engagement.movement`` mismatch on **legitimately
    relocated** moves — cry-wolf, strictly worse than no witness, poisoning the
    GM-panel lie-detector.

    The live decompose pass is **single-submitter**: ``player_name`` is THE
    acting character (a ``player_seats`` *value*), so the submitting seat id is
    the key mapping to it. ``prompt_redaction`` rebuilds ``PlayerDispatch`` via
    ``model_copy`` and never branches on the value, so normalizing here flows
    through cleanly.

    No Silent Fallbacks: if ``player_name`` maps to no seat (pre-chargen
    binding / solo with empty ``player_seats``) the LLM value is left untouched,
    and the witness surfaces LOUD evidence downstream rather than guessing — the
    genuine no-seat plumbing signal, NOT a relocation lie.
    """
    seat_id = next(
        (pid for pid, name in snapshot.player_seats.items() if name == player_name),
        None,
    )
    if seat_id is None:
        return
    for pd in package.per_player:
        pd.player_id = seat_id


def effective_dispatch_turn_number(turn_manager: Any, *, is_opening_turn: bool) -> int:
    """The turn number the dispatch-bank spans should carry so they grid to the
    same column as ``turn_complete``.

    The pre-narrator dispatch bank runs BEFORE ``record_interaction()`` bumps the
    counter, while ``turn_complete`` emits ``turn_id=interaction`` AFTER the bump.
    A player turn runs ``record_interaction()`` (interaction+1), so its dispatch
    spans must be stamped one ahead. The opening scene-set skips
    ``record_interaction()`` (ADR-051), so its effective turn number is the
    current interaction unchanged. Reading the raw interaction at dispatch time
    left intent_router/inventory dark on the resolving turn (DRIVER 2026-06-04).
    """
    interaction = int(getattr(turn_manager, "interaction", 0)) if turn_manager is not None else 0
    return interaction if is_opening_turn else interaction + 1


# Light & darkness survival clock (Task 3.2). The set of subsystems whose
# emission means "game time advanced this turn" — the only turns the survival
# clock should tick. Conservative on purpose: movement (a step deeper) and
# confrontation (a fight) are unambiguously time-advancing. Widen this set via
# OTEL evidence (spec §12), not speculation — a too-wide set burns light on
# turns the table doesn't perceive as time passing.
TIME_ADVANCING_SUBSYSTEMS = frozenset({"movement", "confrontation"})


def _region_is_lit(region_obj: Any) -> bool:
    """Read the ``lit`` flag off a cartography region.

    The real runtime object is a pydantic ``world.Region`` (``extra='allow'``)
    that exposes the authored ``lit`` key as an attribute; a test stub may pass
    a plain dict. Cover both. Absent ⇒ False (unlit → burns), which is also the
    intended default for a region that isn't in ``cartography.regions`` at all
    (e.g. beneath_sunden's procedurally generated DEEP).
    """
    if isinstance(region_obj, dict):
        return bool(region_obj.get("lit", False))
    return bool(getattr(region_obj, "lit", False))


def inject_environment_clock(
    package: DispatchPackage,
    snapshot: GameSnapshot,
    pack: GenrePack | None,
    *,
    player_name: str,
) -> None:
    """Deterministically append an ``environment_clock`` tick on time-advancing
    turns (light & darkness survival clock, Phase 3).

    NOT LLM-emitted — the time-advancing signal is derived from the router's own
    dispatch set ({movement, confrontation}) so phrasing cannot dodge the burn.
    No-op when there is no ``light`` pool or no time-advancing dispatch present.

    ``character_name`` is set to ``player_name`` — the acting PC's creature-core
    NAME, which is what ``environment_clock``'s ``snapshot.find_creature_core``
    matches (``core.name``). The LLM-emitted ``player_id`` is a SEAT id and would
    silently no-op the darkness penalty if passed instead. Single-acting-PC
    targeting: the survival clock reconciles the acting PC's darkness penalty;
    MP multi-core targeting is out of scope here (the burn is global to the
    ``light`` pool either way).

    ``lit`` is read from the acting PC's cartography region. A region absent from
    ``cartography.regions`` defaults to ``lit=False`` (unlit → burns) — intended
    for procedurally generated DEEP regions, NOT a silent fallback.
    """
    if snapshot.resources.get("light") is None:
        return

    all_dispatches = [d for pd in package.per_player for d in pd.dispatch] + [
        d for ca in package.cross_player for d in ca.dispatch
    ]
    if not any(d.subsystem in TIME_ADVANCING_SUBSYSTEMS for d in all_dispatches):
        return

    if not package.per_player:
        # A time-advancing dispatch was found in cross_player but there is no
        # per_player seat to attach the tick to. The burn targets a PC core;
        # with no per_player entry there is no acting PC to reconcile. Surface
        # loud rather than guess a target.
        logger.warning(
            "environment_clock.inject_skipped reason=no_per_player_seat "
            "world=%s player=%s — a time-advancing dispatch fired with an empty "
            "per_player; cannot attach the survival-clock tick",
            getattr(snapshot, "world_slug", ""),
            player_name,
        )
        return

    # Resolve the acting PC's cartography region: the per-PC graph region
    # (region_for with the acting player's perspective). region_for returns
    # None for a split party / unseeded PC; the tick still fires with an empty
    # region id and the default-unlit burn (No Silent Fallbacks: empty region is
    # the loud "unresolved" signal, not a substituted current_region).
    region_name = snapshot.region_for(perspective=player_name) or ""
    lit = False
    worlds = getattr(pack, "worlds", None) or {}
    world = worlds.get(getattr(snapshot, "world_slug", "") or "")
    carto = getattr(world, "cartography", None)
    if carto is not None and region_name:
        region_obj = getattr(carto, "regions", {}).get(region_name)
        if region_obj is not None:
            lit = _region_is_lit(region_obj)

    clock = SubsystemDispatch(
        subsystem="environment_clock",
        params={
            "region": region_name,
            "lit": lit,
            "character_name": player_name,
        },
        idempotency_key=f"environment_clock_{snapshot.turn_manager.interaction}",
        confidence=1.0,
        visibility=VisibilityTag(visible_to="all"),
    )
    package.per_player[0].dispatch.append(clock)


async def execute_intent_router_pre_narrator_pass(
    *,
    intent_router: IntentRouter,
    snapshot: GameSnapshot,
    pack: GenrePack,
    action: str,
    player_name: str,
    additional_player_names: list[str] | None = None,
    dungeon_store: Any | None = None,
    palette: Any | None = None,
    lookahead_handle: Any | None = None,
    phase_timings: PhaseTimings | None = None,
    turn_number: int = 0,
) -> tuple[DispatchPackage, BankResult]:
    """Run the IntentRouter and dispatch bank pre-narrator.

    Returns ``(package, bank_result)``. The caller assigns the package to
    ``turn_context.dispatch_package`` (the narrator prompt builder reads it
    for redaction) and the ``BankResult`` to ``turn_context.bank_result``.
    This pass is the SINGLE dispatch-bank run for the turn: the engines
    engage here, on the snapshot, before the narrator. The orchestrator
    consumes ``bank_result`` for narrator directives + the lethality
    arbiter rather than re-running the bank — re-running would engage every
    engine a second time (a PC moves twice, a clue is consumed twice).

    Raises ``IntentRouterFailure`` (from the router itself) if the
    bounded retry fails. The caller MUST NOT swallow this — the player's
    turn surfaces an explicit error rather than silently continuing to
    the narrator with no mechanical backing.

    The dispatch bank itself catches per-handler exceptions and records
    them as error spans (the bank does not re-raise) — so a single
    subsystem regression does not break the turn, but the watcher
    (Story 59-3) catches the resulting dispatch-without-engagement
    mismatch on the post-turn snapshot.

    ``phase_timings`` (Bug B fix): when supplied, the entire router +
    dispatch-bank call is wrapped in a ``timings.phase("intent_router_pass")``
    context so the Timeline pipeline has a named intent_router stage between
    the barrier-wait and the narrator phases. The caller (websocket_session_handler)
    passes ``turn_context.phase_timings`` here; test callers pass a fresh
    ``PhaseTimings`` instance to assert the phase was recorded.
    """
    _timings = phase_timings if phase_timings is not None else PhaseTimings.NULL
    with _timings.phase("intent_router_pass"):
        state_summary = _build_state_summary(
            snapshot, pack=pack, dungeon_store=dungeon_store, palette=palette
        )
        package = await intent_router.decompose(
            action=action,
            state_summary=state_summary,
        )

        # Per-turn call-budget assertion (Story 91-2). ``observed`` counts SDK
        # ROUND-TRIPS, not decompose invocations — the real ``IntentRouter``
        # reports first-attempt + bounded-retry via
        # ``sdk_round_trips_last_decompose``, so a retry storm (the [COST-1]
        # "2x floor" suspect) is visible to the budget. A router that does not
        # report the metric (test stubs, future alternate producers) counts as
        # the minimum truth of one round-trip for the decompose that just
        # returned. The breach is LOUD evidence (ERROR span + log), never a
        # circuit breaker — the player's turn continues; hard-kill is the
        # ADR-134 detector's job (story 91-4).
        _round_trips = getattr(intent_router, "sdk_round_trips_last_decompose", None)
        observed = _round_trips if isinstance(_round_trips, int) and _round_trips > 0 else 1
        budget = INTENT_ROUTER_CALL_BUDGET_PER_TURN
        if observed > budget:
            with intent_router_call_budget_breach_span(
                turn_id=turn_number,
                observed=observed,
                budget=budget,
            ):
                pass
            logger.error(
                "intent_router.call_budget.breach turn_id=%s observed=%d budget=%d "
                "player=%s — per-turn Haiku classification call count exceeded the "
                "documented budget (epic 91 Dark Spend; see "
                "INTENT_ROUTER_CALL_BUDGET_PER_TURN)",
                turn_number,
                observed,
                budget,
                player_name,
            )

        # Story 59-30 — normalize the LLM-emitted per_player player_id to the real
        # submitting seat id BEFORE the gates/bank, so the normalized id rides into
        # the package the caller assigns to ``turn_context.dispatch_package`` (the
        # post-turn movement witness reads it). See ``_normalize_per_player_ids``.
        # Sits inside the measured ``intent_router_pass`` timing phase (#624),
        # post-decompose, before the classification span and the gates/bank.
        _normalize_per_player_ids(package, snapshot=snapshot, player_name=player_name)

        # Classification-result observability (Plan 2b): only when the vocabulary
        # was surfaced this turn (a political world) — so the GM panel can see the
        # router's front-door decision, "classified as witnessed_act:X" vs "had the
        # vocabulary and declined". Fires before the gates so it reflects the raw
        # router output, not the post-gate package.
        # Confrontation classification evidence (sq-playtest 2026-06-07
        # standoff seat seam): record the front-door decision whenever the
        # action lexically hits an authored intent_verb OR a confrontation
        # dispatch was emitted. emitted=0 with verb_hits non-empty is the
        # decline the GM panel could not previously see — turn 5's armed
        # brace yielded confrontation=None with zero telemetry.
        conf_types = _confrontation_types_emitted(package)
        verb_hits = _confrontation_verb_hits(action, pack)
        if conf_types or verb_hits:
            with intent_router_confrontation_classified_span(
                emitted=len(conf_types),
                types=",".join(conf_types),
                verb_hits=",".join(verb_hits),
                genre_slug=snapshot.genre_slug or "",
            ):
                pass
            if verb_hits and not conf_types:
                logger.warning(
                    "intent_router.confrontation_verb_unrouted verb_hits=%s "
                    "action_preview=%r — the action lexically matched authored "
                    "intent_verbs but the router emitted no confrontation dispatch",
                    ",".join(verb_hits),
                    action[:120],
                )

        # Ability-invocation decline evidence (sq-playtest 2026-06-07 Reroute
        # Power): a party character's ADR-097 ability declared verbatim has NO
        # dispatch route — the bank has no ability subsystem — and previously
        # produced zero telemetry. Shared emit seam: the beat-commit dice path
        # (router-suppressed by story 91-2) calls the same helper, so both
        # submission paths feed one GM-panel query.
        emit_ability_invocation_unrouted(action=action, snapshot=snapshot)

        # Beat-out-of-context decline evidence (sq-playtest 2026-06-07 Size Up):
        # only when no confrontation dispatch was emitted this turn — a seated
        # confrontation makes the beat playable, which is not a decline.
        if not conf_types:
            for _beat_type, _beat_id in _beat_invocations_outside_confrontation(
                action, pack, snapshot
            ):
                with intent_router_beat_outside_confrontation_span(
                    beat_id=_beat_id,
                    confrontation_type=_beat_type,
                    genre_slug=snapshot.genre_slug or "",
                ):
                    pass
                logger.warning(
                    "intent_router.beat_invoked_outside_confrontation beat_id=%r "
                    "type=%r — a confrontation-only beat was invoked by name with "
                    "no confrontation active and none seated this turn; the "
                    "invocation has no playable surface",
                    _beat_id,
                    _beat_type,
                )

        if "witnessed_act_vocabulary" in state_summary:
            act_ids = _witnessed_act_ids(package)
            with intent_router_witnessed_act_classified_span(
                emitted=len(act_ids),
                act_ids=",".join(act_ids),
                genre_slug=snapshot.genre_slug or "",
            ):
                pass

        # Unregistered-subsystem gate (Story 71-27): drop dispatches whose
        # ``subsystem`` names no registered handler — the canonical case is the
        # router emitting ``combat`` (a confrontation *type*, routed through the
        # ``confrontation`` subsystem) as if it were a subsystem key. Such a
        # dispatch can NEVER engage; gating it here — BEFORE the precondition gate,
        # the bank, AND the caller's ``turn_context.dispatch_package`` — stops the
        # router from emitting an unhandlable dispatch into the redaction path and
        # the post-turn watcher, while a loud ``intent_router.dispatch.unregistered``
        # span records each drop (NOT a silent fallback). The dispatch bank's own
        # unknown-subsystem skip remains as a defense-in-depth backstop. The
        # registry is the single source of truth — injected here so the gate stays
        # registry-free and testable.
        package = run_unregistered_subsystem_gate(package=package, registered=set(get_registered()))

        # Precondition gate (Story 59-8): drop dispatches that are STRUCTURALLY
        # inert on this snapshot — they can never engage no matter what the
        # narrator does (e.g. scenario_clue with no ADR-053 scenario graph loaded),
        # so engaging them only ever produces a guaranteed
        # ``dispatch_engagement.*.mismatch`` false-positive. Gating here — BEFORE
        # the bank AND before the package is returned to the caller (which assigns
        # it to ``turn_context.dispatch_package`` for the post-turn watcher) —
        # removes the inert dispatch from both the engine run and the lie-detector,
        # while a loud ``intent_router.dispatch.gated`` span records each skip (NOT
        # a silent fallback). A real scenario world is unaffected: the gate only
        # fires when the precondition is unmet.
        package = run_dispatch_precondition_gate(package=package, snapshot=snapshot)

        # Light & darkness survival clock (Task 3.2). Deterministically append an
        # environment_clock tick when this turn is time-advancing (the router
        # emitted a movement/confrontation dispatch) AND a light pool exists.
        # Placed AFTER the gates (so a gated-away movement dispatch does not
        # trigger a phantom burn) and BEFORE the bank (so the tick engages in the
        # SAME single pass, on the snapshot, before the narrator). Derives the
        # signal from the router's own emission — phrasing cannot dodge the burn.
        inject_environment_clock(package, snapshot, pack, player_name=player_name)

        bank_result = await run_dispatch_bank(
            package,
            context={
                "snapshot": snapshot,
                "pack": pack,
                "player_name": player_name,
                "npcs_present": [],
                "additional_player_names": additional_player_names,
                # ``npc_pool`` is required (kw-only, no default) by
                # ``run_npc_agency``; sourced from the live snapshot so the NPC
                # disposition subsystem engages in THIS pass instead of failing
                # on a missing kwarg.
                "npc_pool": list(snapshot.npc_pool or []),
                # ``npcs`` — the authored roster. npc_agency resolves its target
                # against the roster FIRST (roster NPCs are not mirrored into
                # npc_pool; presence is tracked via last_seen_location), so
                # without this the subsystem never engaged for the game's primary
                # NPCs (playtest #C1, 2026-05-28). Signature-filtered by the bank.
                "npcs": list(snapshot.npcs or []),
                # Movement subsystem (§0 context threading): the live region
                # graph + palette + worker handle the movement handler needs.
                # The bank signature-filters context, so subsystems that do not
                # declare these kwargs are unaffected.
                "dungeon_store": dungeon_store,
                "palette": palette,
                "lookahead_handle": lookahead_handle,
                # Effective (post-record_interaction) turn number so the bank +
                # equip spans grid to the same column turn_complete emits — see
                # ``effective_dispatch_turn_number``. 0 (the default) means the
                # caller did not thread it; the bank then falls back to the
                # snapshot's interaction (preserves direct-caller behavior).
                "turn_number": turn_number,
            },
        )

    logger.debug(
        "intent_router_pass.complete turn_id=%s dispatch_count=%d player=%s encounter_engaged=%s",
        package.turn_id,
        sum(len(pd.dispatch) for pd in package.per_player)
        + sum(len(ca.dispatch) for ca in package.cross_player),
        player_name,
        snapshot.encounter is not None,
    )

    return package, bank_result


__all__ = [
    "INTENT_ROUTER_CALL_BUDGET_PER_TURN",
    "TIME_ADVANCING_SUBSYSTEMS",
    "_normalize_per_player_ids",
    "effective_dispatch_turn_number",
    "execute_intent_router_pre_narrator_pass",
    "inject_environment_clock",
]
