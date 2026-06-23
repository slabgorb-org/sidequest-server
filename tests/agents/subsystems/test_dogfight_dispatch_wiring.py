"""RED wiring tests — Story 153-6 — the ``dogfight`` (ADR-077 ship-combat) subsystem.

Playtest finding ``[SWN-DOGFIGHT-UNREACHABLE]`` (session
``2026-06-21-coyote_star-2cb11877``, world ``space_opera/coyote_star``): the
ADR-077 sealed-letter dogfight engine already exists end-to-end —
``game/dogfight_shot.py`` (shot resolution), the ``sealed_letter`` resolver
(``server/dispatch/sealed_letter.py`` → ``dogfight.confrontation_started`` /
``maneuver_committed`` / ``cell_resolved`` spans), and the SWN ``dogfight``
ConfrontationDef (``resolution_mode: sealed_letter_lookup``). It is seated by the
same production primitive a confrontation uses,
``instantiate_encounter_from_trigger(encounter_type="dogfight", ...)``. But there
is **no pre-narrator IntentRouter dispatch-bank route to it**: ``intent_router.py``
names no ship-combat dispatch key and ``agents/subsystems/`` has no dogfight
handler. So a player who paints a hostile contact and brings weapons hot
("targeting lock", "combat attitude", "weapons hot") never deterministically
engages the dogfight engine — the router stays silent, the narrator improvises
the contact breaking off, and ``encounter_events == 0``.

This is the direct sibling of Story 153-5 ([SWN-ORBITAL-COURSE-INERT], already
shipped): same implemented-but-unreachable shape, same wiring gap against the
ADR-113 mechanical-engagement spine, same SWN space-scale scope. As with 153-5
(``course``), 117-3 (``quest_offer``) and 105-2 (the movement seam), we add ONE
subsystem, ``dogfight``, registered in the dispatch ``_REGISTRY`` and reachable
through the REAL ``run_dispatch_bank``. On a high-confidence ship-combat intent it
SEATS the ADR-077 dogfight by reusing the existing
``instantiate_encounter_from_trigger`` seating primitive (Don't Reinvent — the
engine exists; the wiring is what's missing) and emits a dispatch-level
``dogfight.dispatch`` OTEL span the GM panel can read.

**Router is STUBBED.** A real intent-router LLM pass is flaky (project lore,
``feedback_no_content_coupled_tests``); the router's classification *is* the input
to this layer, so we inject it deterministically by constructing the
``SubsystemDispatch`` (``subsystem="dogfight"``, ``params={"type": "dogfight",
"opponent": {...}}``, ``confidence``) and driving the REAL bank / pre-pass. No LLM
runs. The router-PROMPT half (teaching the Haiku pass to classify ship-combat
prose → a ``dogfight`` dispatch) is the Dev's to add in ``intent_router.py`` and
is validated by playtest, NOT unit-tested here (same precedent as 153-5's
``course``: no prompt-text assertion — that would be a forbidden source-text
wiring test, CLAUDE.md "No Source-Text Wiring Tests").

Contract under test (TEA-defined for Dev), from ADR-077 + ADR-113/-116/-130-sibling:

* ``sidequest/agents/subsystems/dogfight.py`` exports ``run_dogfight_dispatch`` and
  it is registered in ``_REGISTRY`` under ``"dogfight"`` (``_register_defaults``).
* THE WIRING TEST (mandatory): a dogfight dispatch through the REAL bank SEATS a
  dogfight on ``snapshot.encounter`` (``encounter_type == "dogfight"``, the red+blue
  sealed-letter pairing, still live) AND emits a ``dogfight.dispatch`` span. Proves
  the subsystem is connected end-to-end, not that the seating primitive works in
  isolation (it already had its own tests). The engine's own
  ``encounter.confrontation_initiated`` span fires too — the real-seating
  anti-illusionism proof (a fake handler could emit ``dogfight.dispatch`` without
  seating anything).
* The per-subsystem confidence gate (default 0.6) protects the engine: a
  below-threshold ship-combat intent degrades to a narrator hint and seats nothing.
* No Silent Fallbacks (AC-5): a dogfight dispatch that cannot seat an Other (no
  ``opponent`` and no NPC in scene to seat as blue) is rejected LOUD via a
  ``dogfight.dispatch.rejected`` span; it never silently hands control back to the
  narrator with no indication of why the engine did not engage.
* The engagement watcher has a ``dogfight`` witness (added to ``_WITNESSES`` AND
  ``_DISPATCHED_TYPE_KEY``) that flags ``dispatch_engagement.dogfight.mismatch``
  when the router dispatched ``dogfight`` but no dogfight encounter seated
  (router-claimed-but-engine-idle).
* The PRODUCTION pre-pass (``execute_intent_router_pre_narrator_pass``) reaches the
  handler in real play. Unlike ``course`` (which needed a NET-NEW ``orbital_content``
  param), the dogfight handler needs only ``snapshot`` / ``pack`` / ``player_name`` /
  ``npcs_present`` — context the pre-pass ALREADY threads for ``confrontation`` — so
  the wiring proof is the registration surviving the unregistered-subsystem gate and
  the handler seating through the real pass (anti-trap: a hand-built bank context
  alone would mask a registration gap, since the gate drops an unregistered dogfight
  dispatch BEFORE the bank).
* Adding ``dogfight`` does not displace the ``course`` sibling (AC-4 regression
  guard): both stay registered and the course witness stays live.

These import / look up names that do not exist yet (the handler, the registry
entry, the two ``dogfight.dispatch*`` spans, the witness). Collection passes but
the assertions fail until Dev (153-6 GREEN) lands every wiring connection. That is
the intended RED. Wiring is proven by behavior + OTEL spans + the registry — never
a source-text grep (CLAUDE.md "No Source-Text Wiring Tests").
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.session import GameSnapshot
from sidequest.protocol.dispatch import (
    DispatchPackage,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)
from tests._helpers.fixture_packs import (
    SWN_TEST_PACK,
    TEST_WORLD,
    load_fixture_pack,
)

# The ADR-077 sealed-letter dogfight ConfrontationDef in swn_test_pack
# (rules.yaml: ``type: dogfight``, ``resolution_mode: sealed_letter_lookup``).
# Story 96-1: fixture pack, not live sidequest-content — content-only changes
# must never turn server tests red.
_DOGFIGHT_TYPE = "dogfight"
_PLAYER_PILOT = "Maverick"
_OPPONENT_PILOT = "Vulture"

# Dispatch-level spans the handler must emit (AC-2 success / AC-5 loud reject).
# These mirror ``course.plot`` / ``course.plot.rejected`` (Story 153-5): a
# handler-level accepted/rejected pair, distinct from the engine's own
# ``encounter.confrontation_initiated`` (seating) and ``dogfight.confrontation_started``
# (which fires later, at sealed-letter RESOLUTION, not at seating).
SPAN_DOGFIGHT_DISPATCH = "dogfight.dispatch"
SPAN_DOGFIGHT_DISPATCH_REJECTED = "dogfight.dispatch.rejected"
# Engine seating span (already exists) — the anti-illusionism proof that a real
# dogfight was seated, not just a dispatch span faked.
SPAN_ENCOUNTER_CONFRONTATION_INITIATED = "encounter.confrontation_initiated"


# ---------------------------------------------------------------------------
# Builders — synthetic only (the STUBBED router output)
# ---------------------------------------------------------------------------


def _open_viz() -> VisibilityTag:
    return VisibilityTag(visible_to="all")


def _dogfight_dispatch(
    *,
    opponent: dict | None = None,
    confidence: float = 0.9,
    idempotency_key: str = "k-dogfight-1",
    include_type: bool = True,
) -> SubsystemDispatch:
    """The STUBBED router output: a deterministic ``dogfight`` classification.

    Stands in for the IntentRouter's Haiku pass on an unambiguous ship-combat
    intent ("paint the contact — full lock", "weapons hot, engage"). ADR-116 (a
    confrontation requires an Other): ``params["opponent"]`` names the hostile
    ship/pilot the engine seats as the blue actor — the same ``{"name",
    "description"}`` shape the ``confrontation`` subsystem materializes from. The
    optional ``type`` hint keeps the test robust to either handler design
    (read-from-params vs resolve-the-pack's-dogfight-type). No LLM runs.
    """
    params: dict = {}
    if include_type:
        params["type"] = _DOGFIGHT_TYPE
    if opponent is not None:
        params["opponent"] = opponent
    return SubsystemDispatch(
        subsystem="dogfight",
        params=params,
        idempotency_key=idempotency_key,
        confidence=confidence,
        visibility=_open_viz(),
    )


def _opponent(name: str = _OPPONENT_PILOT) -> dict:
    return {"name": name, "description": "A dark strike fighter, drives hot."}


def _package_with(*dispatches: SubsystemDispatch, turn_id: str = "turn-1") -> DispatchPackage:
    return DispatchPackage(
        turn_id=turn_id,
        per_player=[
            PlayerDispatch(
                player_id="player:Maverick",
                raw_action="Paint the contact with targeting radar — full lock, weapons hot.",
                dispatch=list(dispatches),
            )
        ],
        confidence_global=1.0,
    )


def _load_pack():
    return load_fixture_pack(SWN_TEST_PACK)


def _snap_with_pilot(*, pilot_name: str = _PLAYER_PILOT) -> GameSnapshot:
    """A fresh snapshot with a seeded player pilot and NO active encounter.

    Mirrors the seeding in ``tests/fixtures/dogfight_playtest_encounter.py``
    (genre/world bound for SWN weapon resolution; Reflex/Intellect at 10 so the
    to-hit arithmetic is deterministic) but WITHOUT pre-seating the encounter —
    the whole point is that the DISPATCH seats it.
    """
    snap = GameSnapshot(genre=SWN_TEST_PACK)
    snap.genre_slug = SWN_TEST_PACK
    snap.world_slug = TEST_WORLD
    snap.characters = [
        Character(
            core=CreatureCore(
                name=pilot_name,
                description="Playtest pilot.",
                personality="Calm.",
            ),
            backstory="A pilot.",
            char_class="Pilot",
            race="Human",
            stats={"Reflex": 10, "Intellect": 10},
        )
    ]
    return snap


def _bank_context(snap: GameSnapshot, pack, *, player_name: str = _PLAYER_PILOT) -> dict:
    """The context the PRODUCTION caller threads for a confrontation/dogfight.

    These exact keys already flow into the bank for ``confrontation`` today
    (``intent_router_pass.py``) — the dogfight handler reuses the same seating
    primitive, so (unlike ``course``) it needs NO net-new context key.
    """
    return {
        "snapshot": snap,
        "pack": pack,
        "player_name": player_name,
        "npcs_present": [],
        "additional_player_names": None,
    }


def _spans_named(otel_capture, name: str) -> list:
    return [s for s in otel_capture.get_finished_spans() if s.name == name]


def _dogfight_seated(snap: GameSnapshot) -> bool:
    """The dogfight engaged: a live (unresolved) ``dogfight`` encounter with the
    sealed-letter red+blue pairing is on the snapshot."""
    enc = snap.encounter
    if enc is None or enc.encounter_type != _DOGFIGHT_TYPE or enc.resolved:
        return False
    roles = sorted(a.role for a in enc.actors)
    return roles == ["blue", "red"]


# ---------------------------------------------------------------------------
# Registration — the handler is reachable from the dispatch bank
# ---------------------------------------------------------------------------


def test_dogfight_handler_is_registered() -> None:
    """The bank's _REGISTRY must carry dogfight → run_dogfight_dispatch.

    Without registration, ``_REGISTRY.get('dogfight')`` is None: the pre-pass
    unregistered-gate (``run_unregistered_subsystem_gate``) drops the dispatch
    BEFORE the bank — the dogfight engine never seats and the narrator silently
    improvises the contact breaking off.
    """
    from sidequest.agents.subsystems import get_registered

    registry = get_registered()
    assert "dogfight" in registry, (
        f"dogfight handler not registered; bank has {sorted(registry)}. "
        "Story 153-6 must add the registration in "
        "sidequest/agents/subsystems/__init__.py:_register_defaults()."
    )
    fn = registry["dogfight"]
    assert callable(fn) and getattr(fn, "__name__", "") == "run_dogfight_dispatch", (
        f"registered dogfight handler should be run_dogfight_dispatch; got {fn!r}"
    )


# ---------------------------------------------------------------------------
# THE WIRING TEST (mandatory) — ship-combat intent → dogfight seated + span
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dogfight_intent_through_bank_seats_dogfight(otel_capture) -> None:
    """End-to-end through the REAL dispatch bank: a high-confidence ship-combat
    intent SEATS the ADR-077 dogfight and emits the dispatch span.

    This is the load-bearing wiring proof (AC-1 + AC-2 + AC-3). It asserts:
      * the bank ENGAGED the subsystem (decision == "engaged", not
        "unknown_subsystem");
      * a ``dogfight.dispatch`` span fired (AC-2 — the GM-panel lie-detector
        confirmation that the engine was engaged, not the narrator improvising);
      * a dogfight encounter SEATED on the canonical snapshot — ``encounter_type
        == "dogfight"``, still live, with the sealed-letter red+blue pairing
        (AC-1: encounter started, not narrator de-escalation);
      * the engine's own ``encounter.confrontation_initiated`` span fired — the
        real-seating proof (a fake handler emitting only ``dogfight.dispatch``
        without seating an encounter would fail this).
    """
    from sidequest.agents.subsystems import run_dispatch_bank

    pack = _load_pack()
    snap = _snap_with_pilot()
    assert snap.encounter is None, "precondition: no encounter before the dispatch"

    package = _package_with(_dogfight_dispatch(opponent=_opponent(), confidence=0.9))
    result = await run_dispatch_bank(package, context=_bank_context(snap, pack))

    # The bank engaged the subsystem (not gated, not unknown).
    dogfight_decisions = [d for d in result.decisions if d["subsystem"] == "dogfight"]
    assert dogfight_decisions, "bank recorded no decision for the dogfight dispatch"
    assert dogfight_decisions[-1]["decision"] == "engaged", (
        "the dogfight dispatch did not engage the engine "
        f"(decision={dogfight_decisions[-1]['decision']!r}) — handler unregistered, "
        "signature mismatch, or no-op'd silently"
    )

    # AC-2: the dispatch-level span fired.
    assert _spans_named(otel_capture, SPAN_DOGFIGHT_DISPATCH), (
        "dogfight.dispatch span did not fire — the GM panel cannot confirm the "
        "engine engaged vs the narrator improvising ship combat"
    )

    # AC-1: a live dogfight encounter seated, with the sealed-letter pairing.
    assert _dogfight_seated(snap), (
        "no live dogfight seated on the snapshot after the dispatch "
        f"(encounter={snap.encounter!r}) — the narrator would have to improvise "
        "ship combat with zero mechanical backing"
    )

    # Real-seating anti-illusionism proof: the engine's seating span fired.
    assert _spans_named(otel_capture, SPAN_ENCOUNTER_CONFRONTATION_INITIATED), (
        "encounter.confrontation_initiated did not fire — the handler reported a "
        "dispatch but never drove the real instantiate_encounter_from_trigger "
        "seating path (a dogfight.dispatch span with no engine behind it)"
    )


# ---------------------------------------------------------------------------
# Confidence gate — a below-threshold ship-combat intent seats nothing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_low_confidence_dogfight_degrades_and_does_not_engage(otel_capture) -> None:
    """The bank's per-subsystem confidence gate (default 0.6) protects the
    dogfight engine: an ambiguous turn below threshold degrades to a narrator
    hint and seats NO encounter — no dogfight.dispatch span, snapshot.encounter
    untouched (ADR-113 confidence gate).
    """
    from sidequest.agents.subsystems import run_dispatch_bank

    pack = _load_pack()
    snap = _snap_with_pilot()
    package = _package_with(_dogfight_dispatch(opponent=_opponent(), confidence=0.2))

    result = await run_dispatch_bank(package, context=_bank_context(snap, pack))

    assert not _spans_named(otel_capture, SPAN_DOGFIGHT_DISPATCH), (
        "a below-threshold dogfight intent must NOT emit the dispatch span"
    )
    assert snap.encounter is None, "a below-threshold dogfight intent must NOT seat an encounter"
    # The degrade produced a narrator hint directive (the player's intent still
    # reaches the narrator), and the decision is recorded as degraded_to_hint.
    assert any(d.kind == "must_narrate" for d in result.directives), (
        "a gated dogfight must still degrade to a narrator hint (intent not dropped)"
    )
    dogfight_decisions = [d for d in result.decisions if d["subsystem"] == "dogfight"]
    assert dogfight_decisions and dogfight_decisions[-1]["decision"] == "degraded_to_hint", (
        "a below-threshold dogfight must record decision=degraded_to_hint"
    )


# ---------------------------------------------------------------------------
# No Silent Fallbacks (AC-5) — an un-seatable dogfight is rejected LOUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dogfight_no_opponent_rejects_loud(otel_capture) -> None:
    """A dogfight dispatch with no Other to seat (no ``opponent`` param AND no NPC
    in scene) cannot start a sealed-letter duel — ADR-116 requires an Other. It
    must fail LOUD via a ``dogfight.dispatch.rejected`` span (a logged event with
    a reason the GM panel can read), never silently hand control back to the
    narrator with no indication of why the engine did not engage (No Silent
    Fallbacks; CLAUDE.md; AC-5).
    """
    from sidequest.agents.subsystems import run_dispatch_bank

    pack = _load_pack()
    snap = _snap_with_pilot()  # no NPCs in scene → no location-fallback opponent
    # High confidence so the gate does NOT mask the rejection as a degrade.
    package = _package_with(_dogfight_dispatch(opponent=None, confidence=0.95))

    await run_dispatch_bank(package, context=_bank_context(snap, pack))

    assert _spans_named(otel_capture, SPAN_DOGFIGHT_DISPATCH_REJECTED), (
        "an un-seatable dogfight (no Other) must emit a dogfight.dispatch.rejected "
        "span (loud failure with reason), not a silent no-op"
    )
    assert not _spans_named(otel_capture, SPAN_DOGFIGHT_DISPATCH), (
        "a rejected dogfight must NOT also fire the accepted dogfight.dispatch span"
    )
    assert snap.encounter is None, "a rejected dogfight must not seat a phantom encounter"


# ---------------------------------------------------------------------------
# Engagement watcher — dogfight witness (lie-detector)
# ---------------------------------------------------------------------------


def test_dogfight_witness_registered() -> None:
    """The dispatch engagement watcher must have a ``dogfight`` witness in
    ``_WITNESSES`` (and a ``_DISPATCHED_TYPE_KEY['dogfight']`` entry) so the GM
    panel can detect a router-claimed-but-engine-idle ship-combat turn.
    """
    from sidequest.agents.dispatch_engagement_watcher import (
        _DISPATCHED_TYPE_KEY,
        _WITNESSES,
    )

    assert "dogfight" in _WITNESSES, (
        f"dogfight has no engagement witness; _WITNESSES has {sorted(_WITNESSES)}. "
        "Story 153-6 must add it (dispatch_engagement_watcher.py:_WITNESSES "
        "+ _DISPATCHED_TYPE_KEY)."
    )
    assert "dogfight" in _DISPATCHED_TYPE_KEY, (
        "dogfight has no _DISPATCHED_TYPE_KEY entry — the mismatch span would carry "
        "an empty dispatched_type"
    )


def test_watcher_flags_dogfight_dispatch_that_did_not_seat() -> None:
    """Router dispatched ``dogfight`` but no dogfight encounter seated → a real
    mismatch (``dispatch_engagement.dogfight.mismatch``). Drives the pure
    detection function against a post-turn snapshot with no encounter — the exact
    router-claimed-but-engine-idle shape the playtest finding exhibited.
    """
    from sidequest.agents.dispatch_engagement_watcher import (
        detect_dispatch_engagement_mismatch,
    )

    snap = _snap_with_pilot()
    assert snap.encounter is None
    package = _package_with(_dogfight_dispatch(opponent=_opponent(), confidence=0.9))

    mismatches = detect_dispatch_engagement_mismatch(package=package, snapshot=snap)

    assert any(m.subsystem == "dogfight" for m in mismatches), (
        "watcher must flag a dogfight dispatch that seated no encounter "
        "(router-claimed-but-engine-idle), got: "
        f"{[(m.subsystem, m.evidence) for m in mismatches]}"
    )


def test_watcher_silent_when_dogfight_seated() -> None:
    """The inverse: when the dogfight actually seated (a live ``dogfight``
    encounter on the snapshot), the witness sees it and reports NO mismatch
    (honest engagement). Uses the production seating fixture so the snapshot
    carries a real sealed-letter dogfight."""
    from sidequest.agents.dispatch_engagement_watcher import (
        detect_dispatch_engagement_mismatch,
    )
    from tests.fixtures.dogfight_playtest_encounter import make_dogfight_playtest_state

    snap, _cdef, _pack = make_dogfight_playtest_state(
        player_pilot_name=_PLAYER_PILOT,
        opponent_pilot_name=_OPPONENT_PILOT,
    )
    assert _dogfight_seated(snap), "fixture sanity: a live dogfight must be seated"
    package = _package_with(_dogfight_dispatch(opponent=_opponent(), confidence=0.9))

    mismatches = detect_dispatch_engagement_mismatch(package=package, snapshot=snap)

    assert not any(m.subsystem == "dogfight" for m in mismatches), (
        "watcher false-flagged a dogfight that actually seated: "
        f"{[(m.subsystem, m.evidence) for m in mismatches]}"
    )


# ---------------------------------------------------------------------------
# Production wiring — the real pre-pass reaches the dogfight handler (anti-trap)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_pass_seats_dogfight_through_real_pass(otel_capture) -> None:
    """Anti-trap wiring proof: drive the REAL pre-narrator pass with a stubbed
    router that dispatches ``dogfight``, and prove the handler SEATS the dogfight
    — i.e. the dispatch survived the unregistered-subsystem gate and the pass
    threaded the (already-existing) confrontation context to the handler.

    Driving only a hand-built bank context (the wiring test above) would mask a
    registration gap: the pre-pass ``run_unregistered_subsystem_gate`` drops an
    UNREGISTERED dogfight dispatch BEFORE the bank ever runs, so the dogfight
    would never seat in real play even if the handler were perfect (the
    opposed-check wiring trap, project memory ``project_opposed_check_wiring_trap``).
    """
    from sidequest.server.intent_router_pass import execute_intent_router_pre_narrator_pass

    pack = _load_pack()
    snap = _snap_with_pilot()

    router = MagicMock()
    router.decompose = AsyncMock(
        return_value=_package_with(_dogfight_dispatch(opponent=_opponent(), confidence=0.9))
    )

    await execute_intent_router_pre_narrator_pass(
        intent_router=router,
        snapshot=snap,
        pack=pack,
        action="Bring the Kestrel to combat attitude, drives hot — lock the contact and engage.",
        player_name=_PLAYER_PILOT,
    )

    assert _dogfight_seated(snap), (
        "the dogfight did not seat through the REAL pre-pass "
        f"(encounter={snap.encounter!r}) — the dispatch was dropped by the "
        "unregistered-subsystem gate, or the pass did not thread the seating "
        "context to the handler"
    )
    assert _spans_named(otel_capture, SPAN_DOGFIGHT_DISPATCH), (
        "dogfight.dispatch did not fire from the real pre-pass — the handler was "
        "never reached through production wiring"
    )


# ---------------------------------------------------------------------------
# AC-4 regression guard — the orbital-course sibling is not displaced
# ---------------------------------------------------------------------------


def test_dogfight_registration_does_not_displace_course_sibling() -> None:
    """Adding the ``dogfight`` subsystem must not regress the 153-5 ``course``
    wiring (AC-4). Both handlers stay registered and the course engagement witness
    stays live — a cheap structural guard that catches a registration edit that
    clobbers a sibling. (The full course behavior is covered by
    ``test_course_clock_dispatch_wiring.py`` staying green.)
    """
    from sidequest.agents.dispatch_engagement_watcher import _WITNESSES
    from sidequest.agents.subsystems import get_registered

    registry = get_registered()
    assert "course" in registry, (
        "the course sibling was displaced from the registry by the dogfight wiring "
        f"(registry: {sorted(registry)})"
    )
    assert "dogfight" in registry, "dogfight must be registered alongside course"
    assert "course" in _WITNESSES, (
        "the course engagement witness was displaced by the dogfight witness"
    )
