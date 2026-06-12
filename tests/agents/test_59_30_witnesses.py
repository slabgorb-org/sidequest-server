"""RED tests — Story 59-30: engagement witnesses for ``witnessed_act`` + ``movement``.

Closes the political-spine lie-detector gap: the dispatch-engagement watcher
(``sidequest/agents/dispatch_engagement_watcher.py``) had witnesses for only six
subsystems. ``witnessed_act`` and ``movement`` are live dispatch paths with no
watcher coverage, so the "router-dispatched-but-engine-didn't-engage" lie
detector could not flag:
  - router classified ``witnessed_act`` but no political-state dial moved, or
  - router classified ``movement`` but the PC did not relocate.

Contract source of truth: the White Queen's "Architect Technical Note — MOVEMENT
WITNESS CONTRACT" + "Architect Ruling (TEA blocking questions)" in
``.session/59-30-session.md`` (verified 2026-06-04). Key contract points these
tests pin:

witnessed_act (turn-scoped political-ledger read; snapshot-only, no baseline):
  - required param ``act_id`` (engine also accepts ``act_archetype`` alias,
    ``witnessed_act.py:102`` — see TEA deviation note, enforced for engine/witness
    param-parity), missing → ``_MALFORMED_EVIDENCE``;
  - ``state = snapshot.political_state``; ``None`` → evidence;
  - current turn = ``snapshot.turn_manager.interaction`` (the exact source
    ``witnessed_act.py:147`` stamps ``BeliefLedgerEntry.turn`` from);
  - ENGAGED iff ``any(e.turn == current_turn and e.act_id == act_id
    for e in state.ledger)`` — turn-scoping is REQUIRED, not optional: without it
    a *prior-turn* ledger entry for the same act_id produces a false-negative
    (witness says "engaged" on a turn the dial did not move).

movement ("relocation-occurred", turn-scoped, per-PC):
  - ``_iter_all_dispatches`` now yields ``(player_id, dispatch)`` tuples — the
    player-attribution thread 59-31 (opponent-yield) reuses;
  - uniform witness signature
    ``_check_<x>_engaged(dispatch, snapshot, player_id) -> str | None``;
  - resolve moving PC: ``pc_name = snapshot.player_seats.get(player_id)``
    (``player_id is None`` → cross_player movement is a router defect → evidence;
    no seat mapping → evidence);
  - ENGAGED iff ``any(t.turn == current_turn and t.pc_name == pc_name
    for t in snapshot.region_transitions)`` — the new turn-stamped
    ``RegionTransition`` provenance artifact, written on BOTH the
    ``apply_world_patch`` (Site A) and ``narration_apply`` region-mode (Site B)
    relocation paths (stamp-site tests live in
    ``tests/server/test_59_30_region_transition_stamp.py``).

Project-rule coverage (CLAUDE.md / SOUL):
  - "Every Test Suite Needs a Wiring Test" — ``test_*_registered_in_witnesses``,
    ``test_*_reachable_via_public_watcher_path``, ``test_iter_all_dispatches_*``.
  - "No Source-Text Wiring Tests" — wiring tests assert on the runtime registry
    (``_WITNESSES`` dict, ``__doc__`` reflection), never source-grep.
  - "No Silent Fallbacks" — a malformed dispatch (missing required param) is
    surfaced as a LOUD mismatch span, never a silent no-op and never a crash that
    would take down post-narration WS turn-delivery.
  - "OTEL Observability Principle" — every mismatch path emits a span; every
    engaged path emits ZERO spans (no false positive on legitimate engagement).

These tests import the new symbols INSIDE each test (matching the convention in
``test_dispatch_engagement_watcher.py``) so each fails individually with a clear
RED signal (the witness / registration / model does not exist yet) rather than a
module-collection error.
"""

from __future__ import annotations

from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.political_state import BeliefLedgerEntry, PoliticalState
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.protocol.dispatch import (
    CrossAction,
    DispatchPackage,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)

# ---------------------------------------------------------------------------
# OTEL plumbing — isolated tracer/exporter per test (matches
# tests/agents/test_dispatch_engagement_watcher.py convention)
# ---------------------------------------------------------------------------


def _fresh_tracer_and_exporter() -> tuple[trace.Tracer, InMemorySpanExporter]:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    return tracer, exporter


def _engagement_spans(exporter: InMemorySpanExporter) -> list[Any]:
    return [s for s in exporter.get_finished_spans() if "dispatch_engagement" in s.name]


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _open_viz() -> VisibilityTag:
    return VisibilityTag(visible_to="all")


def _make_dispatch(
    *,
    subsystem: str,
    params: dict[str, Any],
    idempotency_key: str = "k1",
) -> SubsystemDispatch:
    return SubsystemDispatch(
        subsystem=subsystem,
        params=params,
        idempotency_key=idempotency_key,
        confidence=1.0,
        visibility=_open_viz(),
    )


def _per_player_package(
    *dispatches: SubsystemDispatch,
    player_id: str = "p1",
    turn_id: str = "turn-1",
) -> DispatchPackage:
    return DispatchPackage(
        turn_id=turn_id,
        per_player=[
            PlayerDispatch(
                player_id=player_id,
                raw_action="(synthetic for witness test)",
                dispatch=list(dispatches),
            )
        ],
        confidence_global=1.0,
    )


def _cross_player_package(
    *dispatches: SubsystemDispatch,
    turn_id: str = "turn-1",
) -> DispatchPackage:
    return DispatchPackage(
        turn_id=turn_id,
        cross_player=[
            CrossAction(
                participants=["player:Alice", "player:Bob"],
                witnesses=["player:Alice", "player:Bob"],
                dispatch=list(dispatches),
            )
        ],
        confidence_global=1.0,
    )


def _ledger_entry(*, turn: int, act_id: str, target_kind: str = "premise") -> BeliefLedgerEntry:
    """One BeliefLedgerEntry — the engine's per-dial receipt
    (``political_engine.py:81-93``). The witness reads ``turn`` + ``act_id``."""
    return BeliefLedgerEntry(
        turn=turn,
        act_id=act_id,
        target_id="some_premise",
        target_kind=target_kind,
        effect="drained",
        delta=-3,
        new_value=5,
    )


def _snapshot(
    *,
    political_state: PoliticalState | None = None,
    interaction: int = 1,
    player_seats: dict[str, str] | None = None,
) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="test_genre",
        world_slug="test_world",
        political_state=political_state,
        turn_manager=TurnManager(interaction=interaction),
        player_seats=player_seats or {},
    )


# ===========================================================================
# witnessed_act — turn-scoped political-ledger witness (direct unit calls)
# ===========================================================================


def test_witnessed_act_engaged_with_ledger_entry_this_turn_returns_none() -> None:
    """Positive: a ledger entry stamped with THIS turn for the dispatched
    act_id → engaged → witness returns ``None``."""
    from sidequest.agents.dispatch_engagement_watcher import _check_witnessed_act_engaged

    snap = _snapshot(
        political_state=PoliticalState(ledger=[_ledger_entry(turn=7, act_id="topple_statue")]),
        interaction=7,
    )
    dispatch = _make_dispatch(subsystem="witnessed_act", params={"act_id": "topple_statue"})

    assert _check_witnessed_act_engaged(dispatch, snap, "p1") is None


def test_witnessed_act_dispatched_with_empty_ledger_returns_evidence() -> None:
    """Negative: political_state exists but the ledger is empty → no dial moved
    → witness returns a non-None evidence string."""
    from sidequest.agents.dispatch_engagement_watcher import _check_witnessed_act_engaged

    snap = _snapshot(political_state=PoliticalState(), interaction=7)
    dispatch = _make_dispatch(subsystem="witnessed_act", params={"act_id": "topple_statue"})

    evidence = _check_witnessed_act_engaged(dispatch, snap, "p1")
    assert evidence is not None, "empty ledger must read NOT engaged"
    assert isinstance(evidence, str) and evidence


def test_witnessed_act_prior_turn_ledger_entry_reads_not_engaged() -> None:
    """Negative — turn-scoping guard (the false-negative TEA flagged): the same
    act_id moved a dial on a PRIOR turn (6) but NOT this turn (7). Without
    turn-scoping the witness would falsely report "engaged"; the contract
    requires keying on ``e.turn == current_turn``, so this MUST read NOT engaged.
    """
    from sidequest.agents.dispatch_engagement_watcher import _check_witnessed_act_engaged

    snap = _snapshot(
        political_state=PoliticalState(ledger=[_ledger_entry(turn=6, act_id="topple_statue")]),
        interaction=7,
    )
    dispatch = _make_dispatch(subsystem="witnessed_act", params={"act_id": "topple_statue"})

    assert _check_witnessed_act_engaged(dispatch, snap, "p1") is not None, (
        "a prior-turn ledger entry must NOT count as this-turn engagement"
    )


def test_witnessed_act_different_act_same_turn_reads_not_engaged() -> None:
    """Negative — act attribution: a DIFFERENT act moved a dial this turn, but
    the dispatched act_id did not. Keying on both turn AND act_id avoids
    crediting this dispatch with another act's dial move."""
    from sidequest.agents.dispatch_engagement_watcher import _check_witnessed_act_engaged

    snap = _snapshot(
        political_state=PoliticalState(ledger=[_ledger_entry(turn=7, act_id="some_other_act")]),
        interaction=7,
    )
    dispatch = _make_dispatch(subsystem="witnessed_act", params={"act_id": "topple_statue"})

    assert _check_witnessed_act_engaged(dispatch, snap, "p1") is not None


def test_witnessed_act_no_political_state_returns_evidence() -> None:
    """Negative: ``snapshot.political_state is None`` (world has no political
    layer) but witnessed_act was dispatched. Engine could not have moved a dial
    → mismatch (mirrors magic_working's "snapshot.magic_state is None")."""
    from sidequest.agents.dispatch_engagement_watcher import _check_witnessed_act_engaged

    snap = _snapshot(political_state=None, interaction=7)
    dispatch = _make_dispatch(subsystem="witnessed_act", params={"act_id": "topple_statue"})

    assert _check_witnessed_act_engaged(dispatch, snap, "p1") is not None


def test_witnessed_act_act_archetype_alias_engaged_returns_none() -> None:
    """Positive — alias parity (TEA deviation, see session file): the subsystem
    resolves ``act_id = params.get("act_id") or params.get("act_archetype")``
    (``witnessed_act.py:102``). A dispatch carrying only ``act_archetype`` still
    engages the engine, whose ledger entry is keyed by the resolved act_id. The
    witness MUST resolve the alias the same way, or it would false-flag a
    legitimately-engaged turn. Here ``act_archetype="topple_statue"`` + a ledger
    entry ``act_id="topple_statue"`` this turn → engaged → ``None``."""
    from sidequest.agents.dispatch_engagement_watcher import _check_witnessed_act_engaged

    snap = _snapshot(
        political_state=PoliticalState(ledger=[_ledger_entry(turn=7, act_id="topple_statue")]),
        interaction=7,
    )
    dispatch = _make_dispatch(subsystem="witnessed_act", params={"act_archetype": "topple_statue"})

    assert _check_witnessed_act_engaged(dispatch, snap, "p1") is None, (
        "witness must resolve the act_archetype alias the same way the engine "
        "does (witnessed_act.py:102), else it false-flags an engaged turn"
    )


def test_witnessed_act_malformed_missing_act_id_surfaces_evidence_not_crash() -> None:
    """No Silent Fallbacks: a witnessed_act dispatch with neither ``act_id`` nor
    ``act_archetype`` is a router defect. The watcher runs POST-narration in the
    WS turn pipeline, so it must SURFACE the defect as a loud mismatch span
    naming the missing param — never raise (a crash hangs turn delivery) and
    never silently no-op."""
    from sidequest.agents.dispatch_engagement_watcher import run_dispatch_engagement_watcher

    tracer, exporter = _fresh_tracer_and_exporter()
    package = _per_player_package(_make_dispatch(subsystem="witnessed_act", params={}))
    snap = _snapshot(political_state=PoliticalState(), interaction=7)

    # MUST NOT raise.
    run_dispatch_engagement_watcher(package=package, snapshot=snap, tracer=tracer)

    spans = _engagement_spans(exporter)
    assert len(spans) == 1, f"expected 1 malformed-dispatch span, got {len(spans)}"
    assert spans[0].name == "dispatch_engagement.witnessed_act.mismatch"
    assert "params['act_id']" in str(dict(spans[0].attributes or {}).get("evidence", "")), (
        "malformed-dispatch evidence must name the missing required param key"
    )


def test_witnessed_act_mismatch_reachable_via_public_watcher_path() -> None:
    """Wiring / OTEL: a witnessed_act mismatch emitted through the PUBLIC
    ``run_dispatch_engagement_watcher`` wrapper proves the witness is registered
    in ``_WITNESSES`` AND that ``_DISPATCHED_TYPE_KEY["witnessed_act"]`` exists
    (the wrapper indexes it). Empty ledger this turn → exactly one mismatch
    span."""
    from sidequest.agents.dispatch_engagement_watcher import run_dispatch_engagement_watcher

    tracer, exporter = _fresh_tracer_and_exporter()
    package = _per_player_package(
        _make_dispatch(subsystem="witnessed_act", params={"act_id": "topple_statue"})
    )
    snap = _snapshot(political_state=PoliticalState(), interaction=7)

    run_dispatch_engagement_watcher(package=package, snapshot=snap, tracer=tracer)

    spans = _engagement_spans(exporter)
    assert len(spans) == 1
    assert spans[0].name == "dispatch_engagement.witnessed_act.mismatch"
    assert dict(spans[0].attributes or {}).get("subsystem") == "witnessed_act"


def test_witnessed_act_engaged_emits_no_span_via_public_path() -> None:
    """No false positive: a legitimately-engaged witnessed_act (ledger entry this
    turn) emits ZERO spans through the public wrapper.

    Guarded against vacuity: a NO-span assertion passes trivially when the
    subsystem is simply unregistered (no witness → nothing to check → no span).
    The registration precondition ties the "no false positive" claim to the
    witness actually being wired, so this fails RED for the right reason."""
    from sidequest.agents.dispatch_engagement_watcher import (
        _WITNESSES,
        run_dispatch_engagement_watcher,
    )

    assert "witnessed_act" in _WITNESSES, (
        "witness must be registered for this no-false-positive check to be meaningful"
    )

    tracer, exporter = _fresh_tracer_and_exporter()
    package = _per_player_package(
        _make_dispatch(subsystem="witnessed_act", params={"act_id": "topple_statue"})
    )
    snap = _snapshot(
        political_state=PoliticalState(ledger=[_ledger_entry(turn=7, act_id="topple_statue")]),
        interaction=7,
    )

    run_dispatch_engagement_watcher(package=package, snapshot=snap, tracer=tracer)

    assert _engagement_spans(exporter) == []


# ===========================================================================
# movement — "relocation-occurred" turn-scoped per-PC witness (direct calls)
# ===========================================================================


def test_movement_engaged_with_region_transition_this_turn_returns_none() -> None:
    """Positive: a RegionTransition stamped THIS turn for the dispatched PC
    (resolved player_id → character name via ``player_seats``) → engaged →
    ``None``."""
    from sidequest.agents.dispatch_engagement_watcher import _check_movement_engaged

    rt_cls = _region_transition_cls()
    snap = _snapshot(interaction=7, player_seats={"p1": "Rux"})
    snap.region_transitions = [
        rt_cls(turn=7, pc_name="Rux", from_region="a", to_region="b", via="world_patch")
    ]
    dispatch = _make_dispatch(subsystem="movement", params={"direction": "deeper"})

    assert _check_movement_engaged(dispatch, snap, "p1") is None


def test_movement_no_region_transition_returns_evidence_naming_pc_and_turn() -> None:
    """Negative: dispatched movement but no RegionTransition this turn → the PC
    did not relocate → evidence string naming the PC and turn (so the GM panel
    shows exactly who failed to move and when)."""
    from sidequest.agents.dispatch_engagement_watcher import _check_movement_engaged

    snap = _snapshot(interaction=7, player_seats={"p1": "Rux"})
    # region_transitions defaults to [] (Dev adds the field in GREEN)
    dispatch = _make_dispatch(subsystem="movement", params={"direction": "deeper"})

    evidence = _check_movement_engaged(dispatch, snap, "p1")
    assert evidence is not None, "no relocation must read NOT engaged"
    assert "Rux" in evidence, f"evidence must name the stuck PC; got {evidence!r}"
    assert "7" in evidence, f"evidence must name the turn; got {evidence!r}"


def test_movement_prior_turn_transition_reads_not_engaged() -> None:
    """Negative — turn-scoping: the PC relocated on a PRIOR turn (6) but not this
    turn (7). Carried-over relocation is not this-turn engagement."""
    from sidequest.agents.dispatch_engagement_watcher import _check_movement_engaged

    rt_cls = _region_transition_cls()
    snap = _snapshot(interaction=7, player_seats={"p1": "Rux"})
    snap.region_transitions = [
        rt_cls(turn=6, pc_name="Rux", from_region="a", to_region="b", via="world_patch")
    ]
    dispatch = _make_dispatch(subsystem="movement", params={"direction": "deeper"})

    assert _check_movement_engaged(dispatch, snap, "p1") is not None


def test_movement_mp_split_party_per_pc_correctness() -> None:
    """Negative + positive — per-PC keying (MP / split-party correctness, the
    ADR-036 submit-and-wait case): two co-located PCs resolve under the SAME
    interaction turn; Mara relocates, Rux gets stuck. On ONE snapshot the witness
    must read each PC independently — Rux (stuck) → NOT engaged, Mara (moved) →
    engaged. This is the precise false-negative a turn-only (player-less) check
    would cause: Mara's transition would mask Rux's stuck move."""
    from sidequest.agents.dispatch_engagement_watcher import _check_movement_engaged

    rt_cls = _region_transition_cls()
    snap = _snapshot(interaction=7, player_seats={"p1": "Rux", "p2": "Mara"})
    snap.region_transitions = [
        rt_cls(turn=7, pc_name="Mara", from_region="a", to_region="b", via="world_patch")
    ]
    dispatch = _make_dispatch(subsystem="movement", params={"direction": "deeper"})

    # p1 → Rux did NOT move → mismatch; p2 → Mara DID move → engaged. Same turn,
    # same snapshot: Mara's move must not mask Rux's stuck move, and vice versa.
    assert _check_movement_engaged(dispatch, snap, "p1") is not None, (
        "stuck PC (Rux) must read NOT engaged even though a co-located PC moved"
    )
    assert _check_movement_engaged(dispatch, snap, "p2") is None, (
        "moved PC (Mara) must read engaged on the same turn/snapshot"
    )


def test_movement_none_player_id_returns_evidence() -> None:
    """Negative — router defect: a movement dispatch with no owning player
    (``player_id is None`` — the shape cross_player dispatches take). Movement is
    never cross-dispatched, so this is a defect the witness surfaces, not a
    silent skip."""
    from sidequest.agents.dispatch_engagement_watcher import _check_movement_engaged

    snap = _snapshot(interaction=7, player_seats={"p1": "Rux"})
    dispatch = _make_dispatch(subsystem="movement", params={"direction": "deeper"})

    assert _check_movement_engaged(dispatch, snap, None) is not None


def test_movement_player_id_without_seat_returns_evidence() -> None:
    """Negative — No Silent Fallbacks: a player_id with no ``player_seats`` entry
    cannot be resolved to a character name. The witness returns loud evidence
    (NOT a silent guess / "single seated PC" fallback)."""
    from sidequest.agents.dispatch_engagement_watcher import _check_movement_engaged

    snap = _snapshot(interaction=7, player_seats={})  # no seat mapping
    dispatch = _make_dispatch(subsystem="movement", params={"direction": "deeper"})

    assert _check_movement_engaged(dispatch, snap, "p1") is not None


def test_movement_reads_narration_apply_via_stamp_engaged_returns_none() -> None:
    """Region-mode false-negative guard (witness side): a relocation in a
    region-mode world (oz/wonderland/gulliver) is stamped with
    ``via="narration_apply"`` (Site B), NOT ``via="world_patch"``. The witness
    must be via-agnostic — it keys on turn + pc_name only — so a
    ``narration_apply`` stamp reads as engaged exactly like a ``world_patch``
    one. Without this, every region-mode move would false-flag."""
    from sidequest.agents.dispatch_engagement_watcher import _check_movement_engaged

    rt_cls = _region_transition_cls()
    snap = _snapshot(interaction=7, player_seats={"p1": "Susan"})
    snap.region_transitions = [
        rt_cls(
            turn=7,
            pc_name="Susan",
            from_region="munchkin_country",
            to_region="the_emerald_city",
            via="narration_apply",
        )
    ]
    dispatch = _make_dispatch(subsystem="movement", params={"direction": "toward_exit"})

    assert _check_movement_engaged(dispatch, snap, "p1") is None


def test_movement_region_mode_deferred_without_relocation_reads_not_engaged() -> None:
    """Region-mode false-negative guard — the UNSTAMPED-deferred case (Architect
    FINALIZATION Q2): a movement that returns ``region_mode_deferred``
    (``movement.py:161``) applies no patch and is deliberately NOT stamped at the
    deferred branch — it hands off to Site B (narration_apply). If narration_apply
    does NOT end in a real region change (the heading matched no known region), no
    ``RegionTransition`` is stamped this turn → the PC did not relocate → witness
    MUST read NOT engaged. Stamping the deferred branch itself would falsely read
    'engaged' here, which is exactly the false-negative the two-site design avoids.
    """
    from sidequest.agents.dispatch_engagement_watcher import _check_movement_engaged

    # Region-mode dispatch, deferred, narration_apply did not relocate →
    # region_transitions stays empty (default []) this turn.
    snap = _snapshot(interaction=7, player_seats={"p1": "Susan"})
    dispatch = _make_dispatch(subsystem="movement", params={"direction": "toward_exit"})

    assert _check_movement_engaged(dispatch, snap, "p1") is not None, (
        "a deferred region-mode move that never produced a Site-B relocation must "
        "read NOT engaged (the unstamped-deferred false-negative guard)"
    )


def test_movement_mismatch_reachable_via_public_watcher_path() -> None:
    """Wiring / OTEL: a movement mismatch emitted through the PUBLIC wrapper
    proves the witness is registered in ``_WITNESSES`` AND that
    ``_DISPATCHED_TYPE_KEY["movement"]`` exists (the wrapper indexes it — a
    missing key would KeyError). No relocation this turn → one mismatch span."""
    from sidequest.agents.dispatch_engagement_watcher import run_dispatch_engagement_watcher

    tracer, exporter = _fresh_tracer_and_exporter()
    package = _per_player_package(
        _make_dispatch(subsystem="movement", params={"direction": "deeper"}),
        player_id="p1",
    )
    snap = _snapshot(interaction=7, player_seats={"p1": "Rux"})

    run_dispatch_engagement_watcher(package=package, snapshot=snap, tracer=tracer)

    spans = _engagement_spans(exporter)
    assert len(spans) == 1
    assert spans[0].name == "dispatch_engagement.movement.mismatch"
    assert dict(spans[0].attributes or {}).get("subsystem") == "movement"


def test_cross_player_movement_dispatch_surfaces_as_mismatch_via_public_path() -> None:
    """Wiring: a movement dispatch on the ``cross_player`` leg flows through
    ``_iter_all_dispatches`` as ``(None, dispatch)`` and the witness surfaces the
    no-owning-player defect as a mismatch span (never a silent skip)."""
    from sidequest.agents.dispatch_engagement_watcher import run_dispatch_engagement_watcher

    tracer, exporter = _fresh_tracer_and_exporter()
    package = _cross_player_package(
        _make_dispatch(
            subsystem="movement", params={"direction": "deeper"}, idempotency_key="cx1"
        )
    )
    snap = _snapshot(interaction=7, player_seats={"p1": "Rux"})

    run_dispatch_engagement_watcher(package=package, snapshot=snap, tracer=tracer)

    spans = _engagement_spans(exporter)
    assert len(spans) == 1
    assert spans[0].name == "dispatch_engagement.movement.mismatch"


# ===========================================================================
# Registration + iterator-contract wiring (runtime registry, not source-grep)
# ===========================================================================


def test_both_witnesses_registered_in_witnesses_dict() -> None:
    """AC: both new witnesses are registered in ``_WITNESSES`` and callable."""
    from sidequest.agents.dispatch_engagement_watcher import _WITNESSES

    assert "witnessed_act" in _WITNESSES, "witnessed_act witness not registered"
    assert "movement" in _WITNESSES, "movement witness not registered"
    assert callable(_WITNESSES["witnessed_act"])
    assert callable(_WITNESSES["movement"])


def test_both_subsystems_registered_in_dispatched_type_key() -> None:
    """The wrapper indexes ``_DISPATCHED_TYPE_KEY[subsystem]`` when building a
    mismatch record; a missing entry would KeyError on every mismatch. Contract:
    ``witnessed_act → "act_id"``, ``movement → "direction"``."""
    from sidequest.agents.dispatch_engagement_watcher import _DISPATCHED_TYPE_KEY

    assert _DISPATCHED_TYPE_KEY.get("witnessed_act") == "act_id"
    assert _DISPATCHED_TYPE_KEY.get("movement") == "direction"


def test_witnesses_count_is_eight_and_docstring_not_stale() -> None:
    """AC: the stale "all six live-path subsystems" docstring is corrected to the
    actual registered count. Asserted via ``__doc__`` reflection (runtime object,
    NOT source-text grep). After this story there are 8 registered witnesses."""
    from sidequest.agents.dispatch_engagement_watcher import (
        _WITNESSES,
        detect_dispatch_engagement_mismatch,
    )

    assert len(_WITNESSES) == 8, (
        f"expected 8 registered witnesses (6 original + witnessed_act + movement); "
        f"got {len(_WITNESSES)}: {sorted(_WITNESSES)}"
    )
    doc = (detect_dispatch_engagement_mismatch.__doc__ or "").lower()
    assert "six" not in doc, "stale 'all six live-path subsystems' docstring not corrected"
    assert "witnessed_act" in doc and "movement" in doc, (
        "corrected docstring should mention the two newly-registered subsystems"
    )


def test_iter_all_dispatches_yields_player_id_dispatch_tuples() -> None:
    """The iterator contract change (shared infra 59-31 reuses):
    ``_iter_all_dispatches`` yields ``(player_id, dispatch)`` — ``per_player``
    carries the owning ``player_id``; ``cross_player`` yields ``None`` (no single
    owner)."""
    from sidequest.agents.dispatch_engagement_watcher import _iter_all_dispatches

    package = DispatchPackage(
        turn_id="turn-1",
        per_player=[
            PlayerDispatch(
                player_id="p1",
                raw_action="x",
                dispatch=[_make_dispatch(subsystem="movement", params={"direction": "deeper"})],
            )
        ],
        cross_player=[
            CrossAction(
                participants=["player:Alice", "player:Bob"],
                witnesses=[],
                dispatch=[
                    _make_dispatch(
                        subsystem="confrontation",
                        params={"type": "social_duel"},
                        idempotency_key="cx1",
                    )
                ],
            )
        ],
        confidence_global=1.0,
    )

    yielded = list(_iter_all_dispatches(package))
    # Every item is a (player_id, dispatch) pair.
    for item in yielded:
        assert isinstance(item, tuple) and len(item) == 2, (
            f"_iter_all_dispatches must yield (player_id, dispatch) tuples; got {item!r}"
        )
    by_subsystem = {d.subsystem: pid for pid, d in yielded}
    assert by_subsystem["movement"] == "p1", "per_player dispatch must carry its player_id"
    assert by_subsystem["confrontation"] is None, "cross_player dispatch must yield player_id=None"


# ---------------------------------------------------------------------------
# RegionTransition import shim — the model is new in 59-30. The Architect note
# leaves its home open ("game/region_transition.py or session.py"); since the
# field is homed on GameSnapshot, ``sidequest.game.session`` is the preferred
# import surface (re-export if defined elsewhere). Trying both keeps the test
# stable to Dev's file choice while still failing RED (neither exists yet).
# ---------------------------------------------------------------------------


def _region_transition_cls() -> Any:
    try:
        from sidequest.game.session import RegionTransition

        return RegionTransition
    except ImportError:
        from sidequest.game.region_transition import RegionTransition

        return RegionTransition
