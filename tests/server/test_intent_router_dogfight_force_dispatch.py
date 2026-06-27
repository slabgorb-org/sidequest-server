"""RED unit tests — Story 158-29 (ADR-153 §7 Plan 2) — the dogfight force-dispatch injector.

The 2026-06-25 ``coyote_star`` playtest crash (develop @aed2d812): a ship-combat
verb ("bring guns online, lock a firing solution") lexically matched the dogfight
def's ``intent_verbs`` (``dogfight:gun`` / ``dogfight:lock``), but the Haiku
``IntentRouter.decompose`` pass emitted NO confrontation dispatch — so the
pre-narrator pass only LOGGED ``intent_router.confrontation_verb_unrouted`` and
moved on. With no engine seated, the narrator was handed a raw ship-combat action
and ground the SDK tool loop to ``max_turns`` (the turn crashed).

The ADR-153 §7 contract (158-29): when dogfight verbs hit, the router emitted no
confrontation dispatch, and no fight is live, the router must FORCE-DISPATCH the
dogfight seater — the narrator gets a real engine instead of a dead-ended log.
``run_dogfight_dispatch`` then seats it (Plan 1 frame-default) or rejects loud via
``dogfight.dispatch.rejected``.

**The over-fire gate is the calibration point.** The dogfight def's ``intent_verbs``
include generic singles (``lock``, ``gun``, ``engage``) — "lock the door" lexically
hits ``dogfight:lock``. Force-dispatching on ANY lexical hit would phantom-seat a
dogfight on a non-combat action (the convincing-prose-with-no-backing failure the
OTEL lie-detector exists to catch). So the force-dispatch fires only on a STRONG
signal: a hit on an unambiguous dogfight verb (``dogfight``/``intercept``/
``pursue``/``missile``), OR ≥2 distinct dogfight-verb hits. "bring guns online,
lock a firing solution" → ``gun`` + ``lock`` = 2 distinct → fires. "lock the door"
→ 1 generic hit → does not.

These import ``force_dispatch_dogfight_on_verb_miss``, which does not exist yet
(158-29 GREEN adds it to ``sidequest/server/intent_router_pass.py``). Collection
fails with ImportError until then — the intended RED. Wiring is proven by behavior
+ the ``dogfight.forced_dispatch`` OTEL span, never a source grep (CLAUDE.md "No
Source-Text Wiring Tests").

Scope (SM 158-29): the dogfight DISPATCH path only. The general "narrator max_turns
must degrade, not crash" robustness fix is split out to 158-41 and is NOT exercised
here. The husk-reaped no-resurrect lifecycle (158-30) and dice-replay narration
anchor (158-35) are sibling stories, out of scope.

Run serially (``-n0``) — these assert OTEL span counts on the shared tracer
provider (``project_server_test_otel_deadlock``).
"""

from __future__ import annotations

from sidequest.protocol.dispatch import DispatchPackage, PlayerDispatch, SubsystemDispatch
from sidequest.server.dispatch.encounter_lifecycle import (
    instantiate_encounter_from_trigger,
)
from sidequest.server.intent_router_pass import force_dispatch_dogfight_on_verb_miss
from tests.fixtures.dogfight_playtest_encounter import (
    DOGFIGHT_TYPE,
    GENRE_SLUG,
    make_dogfight_pack,
    make_empty_snapshot,
)

_PILOT = "Pilot"


def _empty_package(player_id: str = _PILOT) -> DispatchPackage:
    """A package the way a router MISS produces it: one per_player slot for the
    submitting seat, ZERO dispatches (no confrontation routed)."""
    return DispatchPackage(
        turn_id="t1",
        per_player=[PlayerDispatch(player_id=player_id, raw_action="", dispatch=[])],
        confidence_global=1.0,
    )


def _confrontation_package(player_id: str = _PILOT) -> DispatchPackage:
    """A package where the router DID route a confrontation — not a miss, so the
    injector must refrain (it would be a double-dispatch)."""
    return DispatchPackage(
        turn_id="t1",
        per_player=[
            PlayerDispatch(
                player_id=player_id,
                raw_action="",
                dispatch=[
                    SubsystemDispatch(
                        subsystem="confrontation",
                        params={"type": DOGFIGHT_TYPE},
                        idempotency_key="k-conf-1",
                        confidence=0.9,
                    )
                ],
            )
        ],
        confidence_global=1.0,
    )


def _injected_dogfight(package: DispatchPackage) -> list[SubsystemDispatch]:
    return [
        d for pd in package.per_player for d in pd.dispatch if d.subsystem == "dogfight"
    ]


def _spans_named(otel_capture, name: str) -> list:
    return [s for s in otel_capture.get_finished_spans() if s.name == name]


# ---------------------------------------------------------------------------
# The force-dispatch fires on a strong ship-combat signal
# ---------------------------------------------------------------------------


def test_force_dispatches_on_two_distinct_generic_verb_hits(otel_capture) -> None:
    """The exact repro action: ``gun`` + ``lock`` = 2 distinct dogfight verbs.

    Two generic singles together are a strong-enough signal — the injector seats
    the dogfight and emits ``dogfight.forced_dispatch`` (the GM-panel proof the
    engine was seated, not the narrator left to improvise / crash)."""
    pack = make_dogfight_pack()
    snapshot = make_empty_snapshot(pc_name=_PILOT)
    package = _empty_package()

    fired = force_dispatch_dogfight_on_verb_miss(
        package,
        snapshot=snapshot,
        pack=pack,
        action="bring guns online, lock a firing solution",
        player_name=_PILOT,
    )

    assert fired is True, "two distinct dogfight-verb hits must force-dispatch"
    injected = _injected_dogfight(package)
    assert len(injected) == 1, (
        f"exactly one dogfight dispatch must be injected, got {len(injected)}"
    )
    assert injected[0].params.get("type") == DOGFIGHT_TYPE, (
        "the injected dispatch must name the pack's sealed-letter dogfight type so "
        f"run_dogfight_dispatch seats it; got params={injected[0].params!r}"
    )
    assert _spans_named(otel_capture, "dogfight.forced_dispatch"), (
        "dogfight.forced_dispatch span did not fire — the GM panel cannot confirm "
        "the router seated the engine rather than dead-ending on the unrouted log"
    )


def test_force_dispatches_on_single_unambiguous_strong_verb() -> None:
    """A single UNAMBIGUOUS dogfight verb (``intercept``) is itself a strong
    signal — it fires without needing a second distinct hit."""
    pack = make_dogfight_pack()
    snapshot = make_empty_snapshot(pc_name=_PILOT)
    package = _empty_package()

    fired = force_dispatch_dogfight_on_verb_miss(
        package,
        snapshot=snapshot,
        pack=pack,
        action="intercept the inbound contact",
        player_name=_PILOT,
    )

    assert fired is True, "an unambiguous dogfight verb (intercept) must force-dispatch"
    assert len(_injected_dogfight(package)) == 1


# ---------------------------------------------------------------------------
# The over-fire gate: a weak / generic / non-combat signal must NOT seat
# ---------------------------------------------------------------------------


def test_does_not_force_dispatch_on_weak_single_generic_verb(otel_capture) -> None:
    """"lock the door behind me" hits ONE generic verb (``lock``) and is plainly
    not ship combat. Force-dispatching here would phantom-seat a dogfight on a
    mundane action — the exact over-fire the gate prevents. No dispatch, no span."""
    pack = make_dogfight_pack()
    snapshot = make_empty_snapshot(pc_name=_PILOT)
    package = _empty_package()

    fired = force_dispatch_dogfight_on_verb_miss(
        package,
        snapshot=snapshot,
        pack=pack,
        action="lock the door behind me",
        player_name=_PILOT,
    )

    assert fired is False, "a single generic verb must NOT force-dispatch (over-fire guard)"
    assert not _injected_dogfight(package), "no dogfight dispatch may be injected on a weak signal"
    assert not _spans_named(otel_capture, "dogfight.forced_dispatch"), (
        "no forced-dispatch span may fire on a non-combat action"
    )


def test_does_not_force_dispatch_on_non_combat_action() -> None:
    """An action with NO dogfight verbs at all is never a dogfight miss — the gate
    must be a real discriminator, not trivially always-true."""
    pack = make_dogfight_pack()
    snapshot = make_empty_snapshot(pc_name=_PILOT)
    package = _empty_package()

    fired = force_dispatch_dogfight_on_verb_miss(
        package,
        snapshot=snapshot,
        pack=pack,
        action="examine the dusty navigation console",
        player_name=_PILOT,
    )

    assert fired is False
    assert not _injected_dogfight(package)


# ---------------------------------------------------------------------------
# Not-a-miss guards: a live fight, or an already-routed confrontation
# ---------------------------------------------------------------------------


def test_does_not_force_dispatch_when_a_fight_is_already_live() -> None:
    """If a dogfight is already seated and unresolved, a dogfight-verb hit is an
    IN-FIGHT beat, not an unrouted miss — the injector must refrain (else it would
    re-dispatch a seat over a live duel)."""
    pack = make_dogfight_pack()
    snapshot = make_empty_snapshot(pc_name=_PILOT)
    # Seat a live (frame-default) dogfight through the real seater (Plan 1 §6).
    enc = instantiate_encounter_from_trigger(
        snapshot=snapshot,
        pack=pack,
        encounter_type=DOGFIGHT_TYPE,
        player_name=_PILOT,
        npcs_present=[],
        genre_slug=GENRE_SLUG,
    )
    assert enc is not None and not enc.resolved, "fixture sanity: a live dogfight is seated"
    package = _empty_package()

    fired = force_dispatch_dogfight_on_verb_miss(
        package,
        snapshot=snapshot,
        pack=pack,
        action="intercept and gun the bandit",
        player_name=_PILOT,
    )

    assert fired is False, "a live fight makes the verb hit an in-fight beat, not a miss"
    assert not _injected_dogfight(package)


def test_does_not_force_dispatch_when_router_already_routed_a_confrontation() -> None:
    """If the router already emitted a confrontation dispatch, it is NOT a miss —
    the injector must refrain to avoid a double dispatch."""
    pack = make_dogfight_pack()
    snapshot = make_empty_snapshot(pc_name=_PILOT)
    package = _confrontation_package()

    fired = force_dispatch_dogfight_on_verb_miss(
        package,
        snapshot=snapshot,
        pack=pack,
        action="intercept and gun the bandit",
        player_name=_PILOT,
    )

    assert fired is False, "an already-routed confrontation is not an unrouted miss"
    # The pre-existing confrontation dispatch stays; no dogfight dispatch is added.
    assert not _injected_dogfight(package)


def test_returns_false_when_no_dogfight_type_resolvable() -> None:
    """With no pack (or a pack authoring no sealed-letter dogfight), there is
    nothing to seat — the injector returns False rather than guessing a type."""
    snapshot = make_empty_snapshot(pc_name=_PILOT)
    package = _empty_package()

    fired = force_dispatch_dogfight_on_verb_miss(
        package,
        snapshot=snapshot,
        pack=None,
        action="intercept and gun the bandit",
        player_name=_PILOT,
    )

    assert fired is False
    assert not _injected_dogfight(package)
