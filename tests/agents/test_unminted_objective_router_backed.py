"""RED (Story 117-4, ADR-146): router-backed unminted-objective detection.

THE PROBLEM these tests pin
---------------------------
``detect_unminted_objective`` (``sidequest/agents/dispatch_engagement_watcher.py``)
gates on ``_UNMINTED_OBJECTIVE_MARKERS`` — a ~13-phrase hardcoded substring
matcher. A noir "discreet job" / "someone of mine has stopped checking in" hook
trips NONE of those phrasings, so ``narration.unminted_objective.suspected``
stays silent while ``quest_log`` sits empty (the live perseus_cloud failure,
session 594dcc7e, 2026-06-14). This is the Zork verb-set anti-pattern: a keyword
matcher policing open-ended natural-language narration.

THE FIX 117-4 must build (the seam these tests force into existence)
-------------------------------------------------------------------
The detector must ride the Intent Router (ADR-113), which already classifies the
turn, instead of (or augmenting) the keyword list. The router's
``quest_offer`` subsystem (Story 117-3) is the objective-engagement signal: when
the router classifies the player's turn as accepting an offered job, it emits a
``quest_offer`` ``SubsystemDispatch``. The detector, given the turn's
``DispatchPackage``, fires ``suspected`` when:

  router classified the turn as objective-giving  (a quest_offer dispatch is
      present in the package)
  AND quest_log stayed EMPTY after the turn
  AND nothing minted it (no quest landed — no quest_offer accept reached the
      log, no record_quest entry, no seed_drive spine).

It must NOT fire (no false positive) when minting DID happen — quest_log
non-empty after the turn.

These tests target the REAL signal: the router ``DispatchPackage`` threaded into
the detector. To stay deterministic we INJECT a synthetic ``DispatchPackage``
(established repo lore: real intent-router passes make tests flaky — see
tests/agents/test_dispatch_engagement_watcher.py). No content-pack dependency.

The feature does NOT exist yet: ``detect_unminted_objective`` today takes only
``narration`` + ``snapshot`` and has no ``package`` parameter, so every
router-backed test below FAILS at call time (unexpected-keyword TypeError) or on
the missing router path — RED for feature-absence, not for an avoidable import
error.

Coverage:
  (a) headline — open-ended hook, ZERO keyword markers, router-classified as
      objective-giving (quest_offer dispatch), quest_log empty → suspected FIRES.
  (b) no false positive — same objective but a quest WAS minted → does NOT fire.
  (c) keyword backstop preserved — the curated-phrase path still fires when the
      router emitted nothing (narrator-improvised, router-silent).
  (d) wiring — the production watcher path (run_unminted_objective_watcher) reads
      the real router package and emits the span; the live handler call site
      passes the package through (reflection, not source-grep).
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.session import GameSnapshot, QuestEntry
from sidequest.protocol.dispatch import (
    DispatchPackage,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)

# An open-ended noir job hook — the exact failure shape. It establishes a
# concrete objective (a giver hands the PC a task) but trips ZERO of the curated
# _UNMINTED_OBJECTIVE_MARKERS phrasings ("your task is", "find the missing", "pay
# the debt", "has not returned", …). If the keyword path alone were enough, this
# string would not flag — and that is precisely the bug.
_OPEN_ENDED_HOOK = (
    "The floor-boss leans in, voice low. \"I have a... situation. Someone of "
    "mine stopped checking in down in the under-levels. Discreet work. You look "
    "like the type who can handle that kind of thing.\""
)

# Sanity anchor for test (c): a phrase that IS in the curated marker list, so the
# keyword backstop must still fire on it with no router package at all.
_CURATED_HOOK = (
    "Your task is to find the missing courier before the Conglomerate does."
)


def _fresh_tracer_and_exporter() -> tuple[trace.Tracer, InMemorySpanExporter]:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


def _snapshot(*, quest_log: dict[str, QuestEntry] | None = None) -> GameSnapshot:
    snap = GameSnapshot(genre_slug="space_opera", world_slug="perseus_cloud")
    if quest_log is not None:
        snap.quest_log = quest_log
    return snap


def _open_viz() -> VisibilityTag:
    return VisibilityTag(visible_to="all")


def _quest_offer_package(
    *,
    quest_id: str = "floor_boss_missing_person",
    decision: str = "accept",
    turn_id: str = "turn-1",
) -> DispatchPackage:
    """A DispatchPackage carrying a single ``quest_offer`` dispatch.

    This is the REAL router signal the detector must ride: the Intent Router
    classified the player's open-ended turn as engaging an objective/offer. We
    inject it synthetically (no live router pass) for determinism.
    """
    dispatch = SubsystemDispatch(
        subsystem="quest_offer",
        params={"quest_id": quest_id, "decision": decision},
        idempotency_key="qo-1",
        confidence=0.9,
        visibility=_open_viz(),
    )
    return DispatchPackage(
        turn_id=turn_id,
        per_player=[
            PlayerDispatch(
                player_id="player:Alice",
                raw_action="yeah, alright, I'll look into it",
                dispatch=[dispatch],
            )
        ],
        confidence_global=0.9,
    )


def _empty_package(turn_id: str = "turn-1") -> DispatchPackage:
    """A quiet turn — the router classified no objective engagement."""
    return DispatchPackage(turn_id=turn_id, confidence_global=1.0)


# ---------------------------------------------------------------------------
# (a) HEADLINE — router-classified objective-giving, ZERO keyword markers,
#     empty quest_log → suspected FIRES.
# ---------------------------------------------------------------------------


def test_fires_on_router_objective_with_zero_keyword_markers() -> None:
    """The perseus_cloud repro. An open-ended hook trips NO curated marker, but
    the router classified the turn as objective-giving (a quest_offer dispatch).
    With quest_log empty and nothing minted, the detector must beep — riding the
    router, not the keyword list."""
    from sidequest.agents.dispatch_engagement_watcher import (
        _UNMINTED_OBJECTIVE_MARKERS,
        detect_unminted_objective,
    )

    # Guard the premise: the hook genuinely trips none of the curated phrases, so
    # the OLD keyword path is silent on it (this is why the keyword matcher missed
    # the live failure). If a future marker happens to cover it, this assertion
    # fails loud and the test author must pick a different open-ended hook.
    lowered = _OPEN_ENDED_HOOK.lower()
    assert not any(m in lowered for m in _UNMINTED_OBJECTIVE_MARKERS), (
        "the open-ended hook must trip ZERO curated markers — that's the whole "
        "point of the router-backed path"
    )

    evidence = detect_unminted_objective(
        narration=_OPEN_ENDED_HOOK,
        snapshot=_snapshot(quest_log={}),
        package=_quest_offer_package(),
    )

    assert evidence is not None, (
        "router classified the turn as objective-giving (quest_offer dispatch) and "
        "quest_log stayed empty — the unminted-objective detector MUST fire on the "
        "router signal even when no curated keyword matches"
    )
    assert "quest_log" in evidence


def test_watcher_emits_span_on_router_objective() -> None:
    """The OTEL wrapper fires ``narration.unminted_objective.suspected`` on the
    router-backed path — the GM panel beep for the open-ended hook."""
    from sidequest.agents.dispatch_engagement_watcher import (
        run_unminted_objective_watcher,
    )

    tracer, exporter = _fresh_tracer_and_exporter()
    run_unminted_objective_watcher(
        narration=_OPEN_ENDED_HOOK,
        snapshot=_snapshot(quest_log={}),
        package=_quest_offer_package(),
        tracer=tracer,
    )

    names = [s.name for s in exporter.get_finished_spans()]
    assert "narration.unminted_objective.suspected" in names, (
        f"expected the unminted-objective span on the router-backed path; got {names}"
    )


# ---------------------------------------------------------------------------
# (b) NO FALSE POSITIVE — the router classified objective-giving, but a quest
#     WAS minted (quest_log non-empty) → suspected does NOT fire.
# ---------------------------------------------------------------------------


def test_no_fire_when_quest_was_minted() -> None:
    """Same objective hook, same router quest_offer dispatch — but the quest_offer
    accept MINTED a QuestEntry (quest_log now non-empty). The objective is
    tracked; the lie-detector must stay silent. This is the inverse the witness
    proves: router-claimed-AND-engine-engaged is honest, not a miss."""
    from sidequest.agents.dispatch_engagement_watcher import detect_unminted_objective

    minted = {
        "floor_boss_missing_person": QuestEntry(
            title="The Floor-Boss's Missing Person",
            objective="Find out who the floor-boss has lost in the under-levels.",
            status="active",
        )
    }
    evidence = detect_unminted_objective(
        narration=_OPEN_ENDED_HOOK,
        snapshot=_snapshot(quest_log=minted),
        package=_quest_offer_package(),
    )

    assert evidence is None, (
        "a quest WAS minted (quest_log non-empty) — the detector must NOT cry "
        "wolf; minting happened, no lie to catch"
    )


def test_watcher_silent_when_quest_minted() -> None:
    from sidequest.agents.dispatch_engagement_watcher import (
        run_unminted_objective_watcher,
    )

    minted = {
        "floor_boss_missing_person": QuestEntry(
            title="The Floor-Boss's Missing Person",
            objective="Find the missing person.",
            status="active",
        )
    }
    tracer, exporter = _fresh_tracer_and_exporter()
    run_unminted_objective_watcher(
        narration=_OPEN_ENDED_HOOK,
        snapshot=_snapshot(quest_log=minted),
        package=_quest_offer_package(),
        tracer=tracer,
    )

    names = [s.name for s in exporter.get_finished_spans()]
    assert "narration.unminted_objective.suspected" not in names, (
        f"minted quest must suppress the beep; got spans {names}"
    )


def test_no_fire_on_quiet_router_turn() -> None:
    """The router classified NO objective engagement (empty package) and the
    narration is open-ended-but-not-objective. With no router signal and no
    curated marker, the detector stays silent — no false positive on every
    early-game scene that merely sets a mood."""
    from sidequest.agents.dispatch_engagement_watcher import detect_unminted_objective

    quiet = (
        "You push through the bead curtain into the pot-house. The air is thick "
        "with smoke and the low murmur of off-shift dock hands."
    )
    evidence = detect_unminted_objective(
        narration=quiet,
        snapshot=_snapshot(quest_log={}),
        package=_empty_package(),
    )
    assert evidence is None


# ---------------------------------------------------------------------------
# (c) KEYWORD BACKSTOP PRESERVED — when the router emitted NOTHING (the
#     narrator-improvised, router-silent case), the curated keyword path must
#     still fire so the backstop is not regressed.
# ---------------------------------------------------------------------------


def test_keyword_backstop_fires_when_router_silent() -> None:
    """The router produced no package (it errored, or this is the un-routed
    narrator-improvised case). A curated objective phrase in prose + empty
    quest_log must STILL trip the keyword backstop — 117-4 augments the router
    path, it does not delete the keyword backstop for router-silent turns."""
    from sidequest.agents.dispatch_engagement_watcher import detect_unminted_objective

    evidence = detect_unminted_objective(
        narration=_CURATED_HOOK,
        snapshot=_snapshot(quest_log={}),
        package=None,
    )
    assert evidence is not None, (
        "curated phrase + empty quest_log + no router package must still fire the "
        "keyword backstop (router-silent narrator-improvised case)"
    )


def test_keyword_backstop_suppressed_by_minted_quest() -> None:
    """The keyword backstop still respects the empty-quest_log gate: a minted
    quest suppresses it even with no router package."""
    from sidequest.agents.dispatch_engagement_watcher import detect_unminted_objective

    minted = {"q": QuestEntry(title="t", objective="o", status="active")}
    evidence = detect_unminted_objective(
        narration=_CURATED_HOOK,
        snapshot=_snapshot(quest_log=minted),
        package=None,
    )
    assert evidence is None


# ---------------------------------------------------------------------------
# (d) WIRING — the detector is invoked in the REAL engagement-watcher path and
#     reads the REAL router signal, not a helper tested in isolation.
# ---------------------------------------------------------------------------


def test_detector_accepts_router_package_param() -> None:
    """Signature wiring (reflection, not source-grep): the pure detector must
    accept the router ``package`` — the seam through which the router signal
    reaches it. Today it takes only narration+snapshot, so this FAILS until
    117-4 threads the package in."""
    import inspect

    from sidequest.agents.dispatch_engagement_watcher import detect_unminted_objective

    params = inspect.signature(detect_unminted_objective).parameters
    assert "package" in params, (
        "detect_unminted_objective must accept the router DispatchPackage — "
        "without it the detector cannot read the router's objective classification "
        "and is stuck on the keyword matcher"
    )


def test_run_watcher_accepts_router_package_param() -> None:
    """The OTEL wrapper must also accept the package so the live handler can pass
    the turn's router output through to the detector."""
    import inspect

    from sidequest.agents.dispatch_engagement_watcher import (
        run_unminted_objective_watcher,
    )

    params = inspect.signature(run_unminted_objective_watcher).parameters
    assert "package" in params, (
        "run_unminted_objective_watcher must accept the router DispatchPackage to "
        "thread it from the live turn pipeline into the detector"
    )


def test_live_handler_passes_router_package_to_watcher() -> None:
    """Production wiring: the live WS turn pipeline must call the unminted-objective
    watcher WITH the turn's router package, not just narration+snapshot.

    The handler already owns ``turn_context.dispatch_package`` (it threads the
    same object into ``run_dispatch_engagement_watcher`` one call above). 117-4
    must thread it into ``run_unminted_objective_watcher`` too, or the production
    detector is blind to the router signal even after the seam exists.

    Reflection-based AST inspection of the call's keyword arguments — NOT a
    source-text substring grep (CLAUDE.md "No Source-Text Wiring Tests"). We parse
    the handler module's AST and assert the ``run_unminted_objective_watcher``
    call passes a ``package=`` keyword. This interrogates the call structure, not
    a raw string, and survives reformatting.
    """
    import ast
    import inspect

    import sidequest.server.websocket_session_handler as handler_mod

    source = inspect.getsource(handler_mod)
    tree = ast.parse(source)

    package_kwarg_seen = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else None
        )
        if name != "run_unminted_objective_watcher":
            continue
        if any(kw.arg == "package" for kw in node.keywords):
            package_kwarg_seen = True

    assert package_kwarg_seen, (
        "the live handler must call run_unminted_objective_watcher(package=...) so "
        "the router signal reaches the detector in production — found the call but "
        "no package= keyword (the detector would run keyword-only, the exact gap "
        "117-4 closes)"
    )


def _walk_dispatches(package: DispatchPackage) -> list[SubsystemDispatch]:
    out: list[SubsystemDispatch] = []
    for pd in package.per_player:
        out.extend(pd.dispatch)
    for ca in package.cross_player:
        out.extend(ca.dispatch)
    return out


def test_injected_package_is_a_real_quest_offer_signal() -> None:
    """Premise guard (not the feature): confirm the synthetic package we inject as
    the router signal genuinely carries a quest_offer dispatch — so a failure of
    the headline test is the missing FEATURE, not a malformed fixture."""
    pkg = _quest_offer_package()
    subsystems = [d.subsystem for d in _walk_dispatches(pkg)]
    assert subsystems == ["quest_offer"], subsystems
