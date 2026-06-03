"""Story 75-10 — wire the player-NPC reference signal into the brief tier (RED).

75-2 carryover (ADR-118 §D4). ``build_npc_working_set`` already implements a
three-tier roster (full / brief / compact); the BRIEF tier (off-stage NPC →
name+role) is unit-tested and reachable via the API but **never fires in
production**, because the live turn-build path supplies no reference signal:

* ``retrieval_orchestration.retrieve_turn_context`` (75-5) accepts a
  ``player_referenced_npcs`` parameter but ``universal_retrieval.retrieve_for_turn``
  (the production caller) never passes one — so ``RetrievedEntities.floor`` is
  built with ``None``;
* ``session_helpers._build_turn_context`` then RE-computes the working set at its
  ``npc_working_set=build_npc_working_set(..., player_referenced_npcs=None)`` call
  and that recomputed set — not ``entity_retrieval.floor`` — is what populates
  ``TurnContext.npc_working_set`` (what the narrator prompt renders).

Net effect today: a player can say "find Joran the smith" and, if Joran is
off-stage, the narrator receives a bare name (compact) with no role context. This
story derives a real per-turn reference signal from the player's action text and
threads it through so referenced off-stage NPCs render BRIEF.

CONTRACT PINNED BY THESE TESTS (see the TEA deviation in the session file)
--------------------------------------------------------------------------
The signal is derived where the action text already lives — the production
retrieval delegate (``handler._retrieve_entities_for_turn`` →
``retrieve_for_turn`` → ``retrieve_turn_context``, which already takes
``action_text``) — and ``RetrievedEntities.floor`` carries the resulting tiered
set; ``_build_turn_context`` consumes ``entity_retrieval.floor`` for
``npc_working_set`` rather than recomputing with ``None``. The legacy
``entity_retrieval is None`` path (fixtures, opening bootstrap before retrieval)
keeps its reference-free recompute, so the 75-2 floor-only wiring test stays
green. Matching is case-insensitive and word-bounded (a name must not match as a
substring of an unrelated word — that would inflate the budget the floor exists
to bound).

Test discipline (server CLAUDE.md "No Source-Text Wiring Tests"): every test
drives the REAL production delegate ``handler._retrieve_entities_for_turn`` and
asserts on observed behaviour — the returned ``RetrievedEntities.floor``, the
populated ``TurnContext.npc_working_set``, and the ``npc.working_set`` OTEL span —
never on source patterns. The autouse ``_mock_daemon_client`` conftest guard
forces the daemon down, so the semantic FILL degrades to ``query_failed`` while
the FLOOR (computed before the daemon call) is exercised exactly as in prod.

Symbols asserted already exist (``build_npc_working_set``, ``NpcWorkingSet``,
``SPAN_NPC_WORKING_SET``, ``TurnContext.npc_working_set``); what fails RED is the
WIRING — the reference signal does not reach the floor on current ``develop``.
"""

from __future__ import annotations

from sidequest.game.creature_core import CreatureCore
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.session import Npc
from sidequest.server.session_helpers import _build_turn_context
from sidequest.telemetry.spans import SPAN_NPC_WORKING_SET

# current_turn=10, window=2 → scene-present threshold is last_seen >= 8.
_TURN = 10


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _present_npc(name: str) -> Npc:
    """A stateful NPC stamped scene-present at ``_TURN`` — lands in the floor."""
    return Npc(
        core=CreatureCore(name=name, description=f"{name} stands here.", personality="stoic"),
        last_seen_turn=_TURN,
    )


def _offstage_npc(name: str, *, role: str = "smith") -> Npc:
    """A stateful NPC last seen long ago — off-stage (brief when referenced)."""
    return Npc(
        core=CreatureCore(name=name, description=f"{name} the {role}.", personality="gruff"),
        last_seen_turn=1,
    )


def _names(entries: list) -> set[str]:
    """Names from a tier holding ``Npc`` (``.core.name``) and/or ``NpcPoolMember``
    (``.name``)."""
    out: set[str] = set()
    for e in entries:
        core = getattr(e, "core", None)
        out.add(core.name if core is not None else e.name)
    return out


def _seed_roster(sd, *, present: list[Npc], offstage: list[Npc], pool: list[NpcPoolMember]) -> None:
    sd.snapshot.turn_manager.interaction = _TURN
    sd.snapshot.npcs.extend(present)
    sd.snapshot.npcs.extend(offstage)
    sd.snapshot.npc_pool.extend(pool)


def _working_set_spans(otel_capture):
    return [s for s in otel_capture.get_finished_spans() if s.name == SPAN_NPC_WORKING_SET]


# ===========================================================================
# AC1 / AC5 — derivation wired into the production retrieval delegate
# ===========================================================================


async def test_referenced_offstage_npc_carried_brief_in_retrieval_floor(
    session_handler_factory,
) -> None:
    """The headline wiring (AC1 + AC5). Driving the LIVE delegate with an action
    that names an off-stage NPC must produce a ``RetrievedEntities.floor`` whose
    BRIEF tier holds that NPC — i.e. the reference signal is derived from the
    action and reaches ``build_npc_working_set`` in production.

    RED on ``develop``: ``retrieve_for_turn`` passes no ``player_referenced_npcs``,
    so the floor is built with ``None`` and Joran collapses to ``compact_names``.
    """
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    _seed_roster(sd, present=[_present_npc("Borin")], offstage=[_offstage_npc("Joran")], pool=[])

    result = await handler._retrieve_entities_for_turn(sd, "I go looking for Joran the smith.")

    floor = result.floor
    assert "Joran" in _names(floor.brief_entries), (
        "a player-referenced off-stage NPC must land in the BRIEF tier of the "
        f"production retrieval floor; got brief={_names(floor.brief_entries)} "
        f"compact={floor.compact_names}"
    )
    assert "Joran" not in floor.compact_names, (
        "the referenced off-stage NPC must NOT also (or instead) sit in compact"
    )
    assert "Borin" in _names(floor.full_profiles), "scene-present NPC stays in the full floor"


async def test_referenced_pool_member_carried_brief(session_handler_factory) -> None:
    """Pool members are part of the off-stage tier and a player can name them.
    Referencing a pool member by name must promote it compact→brief too — the
    signal must consider ``snapshot.npc_pool`` names, not only stateful ``npcs``.

    RED on ``develop`` (no signal → pool member stays compact).
    """
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    _seed_roster(
        sd,
        present=[_present_npc("Borin")],
        offstage=[],
        pool=[NpcPoolMember(name="Reeve", role="guard", drawn_from="test")],
    )

    result = await handler._retrieve_entities_for_turn(sd, "I wave Reeve over to the gate.")

    floor = result.floor
    assert "Reeve" in _names(floor.brief_entries), (
        "a referenced pool member must render brief; "
        f"got brief={_names(floor.brief_entries)} compact={floor.compact_names}"
    )
    assert "Reeve" not in floor.compact_names


# ===========================================================================
# AC2 — the brief tier reaches TurnContext.npc_working_set (the prompt seam)
# ===========================================================================


async def test_referenced_offstage_npc_renders_brief_in_turn_context(
    session_handler_factory,
) -> None:
    """AC2: the signal must survive into ``TurnContext.npc_working_set`` — the
    field the narrator prompt actually renders — via the real
    ``_build_turn_context``, not merely into the retrieval result.

    RED on ``develop``: ``_build_turn_context`` recomputes the working set with
    ``player_referenced_npcs=None``, so the prompt-bound set has Joran compact
    even if the retrieval floor were correct.
    """
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    _seed_roster(sd, present=[_present_npc("Borin")], offstage=[_offstage_npc("Joran")], pool=[])

    result = await handler._retrieve_entities_for_turn(sd, "Where can I find Joran?")
    context = _build_turn_context(sd, entity_retrieval=result)

    ws = context.npc_working_set
    assert ws is not None, "the production turn-build path must populate npc_working_set"
    assert "Joran" in _names(ws.brief_entries), (
        "the referenced off-stage NPC must reach the prompt-bound working set as "
        f"brief; got brief={_names(ws.brief_entries)} compact={ws.compact_names}"
    )
    assert "Joran" not in ws.compact_names


# ===========================================================================
# AC3 — the scene-present FULL floor is reference-independent
# ===========================================================================


async def test_scene_present_floor_holds_when_offstage_npc_referenced(
    session_handler_factory,
) -> None:
    """AC3: referencing an off-stage NPC must promote ONLY that NPC — the
    scene-present floor is untouched. Pins that the new signal never demotes a
    full-profile NPC.

    The floor assertion (Borin full) holds on develop; the brief assertion
    (Joran brief) is the RED discriminator that proves the new wiring fired
    without disturbing the floor.
    """
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    _seed_roster(
        sd,
        present=[_present_npc("Borin"), _present_npc("Sable")],
        offstage=[_offstage_npc("Joran")],
        pool=[],
    )

    result = await handler._retrieve_entities_for_turn(sd, "I shout for Joran across the yard.")
    ws = _build_turn_context(sd, entity_retrieval=result).npc_working_set

    assert _names(ws.full_profiles) == {"Borin", "Sable"}, (
        "the scene-present floor must be reference-independent — both present NPCs "
        f"stay full when an off-stage NPC is referenced; got {_names(ws.full_profiles)}"
    )
    assert "Joran" in _names(ws.brief_entries), "the referenced off-stage NPC promoted to brief"
    assert "Borin" not in _names(ws.brief_entries), "a floor NPC must never appear in brief"


# ===========================================================================
# AC4 — OTEL: references_present=True / brief_count>0 on the production path
# ===========================================================================


async def test_working_set_span_records_reference_on_production_path(
    session_handler_factory, otel_capture
) -> None:
    """AC4 (the GM-panel lie detector): when the player references an off-stage
    NPC, the production turn must emit an ``npc.working_set`` span with
    ``references_present=True`` and ``brief_count>0``. Today every prod-path
    emission carries ``references_present=False`` / ``brief_count=0``.

    Robust to the recompute question: asserts that AT LEAST ONE emission across
    the real path (retrieval floor and/or ``_build_turn_context``) records the
    reference — so it passes whether or not the Dev removes the line-1241
    recompute.
    """
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    _seed_roster(sd, present=[_present_npc("Borin")], offstage=[_offstage_npc("Joran")], pool=[])

    result = await handler._retrieve_entities_for_turn(sd, "I track down Joran the smith.")
    _build_turn_context(sd, entity_retrieval=result)

    fired = _working_set_spans(otel_capture)
    assert fired, f"{SPAN_NPC_WORKING_SET!r} must fire on the production turn path"
    referencing = [
        s
        for s in fired
        if dict(s.attributes or {}).get("references_present") is True
        and int(dict(s.attributes or {}).get("brief_count", 0)) > 0
    ]
    assert referencing, (
        "at least one npc.working_set span must record references_present=True with "
        "brief_count>0 when the player referenced an off-stage NPC; saw "
        f"{[dict(s.attributes or {}) for s in fired]}"
    )


# ===========================================================================
# Negative / test-paranoia — the signal must not over-trigger
# ===========================================================================


async def test_unreferenced_offstage_npc_stays_compact(
    session_handler_factory, otel_capture
) -> None:
    """An action that names NO roster NPC must leave off-stage NPCs COMPACT and
    the span ``references_present=False`` — the brief tier is a precision signal,
    not "always on". Guards against a degenerate always-true implementation.

    (Green on develop — a regression guard for the new wiring.)
    """
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    _seed_roster(sd, present=[_present_npc("Borin")], offstage=[_offstage_npc("Joran")], pool=[])

    result = await handler._retrieve_entities_for_turn(sd, "I climb the north stairs alone.")
    ws = _build_turn_context(sd, entity_retrieval=result).npc_working_set

    assert "Joran" in ws.compact_names, (
        f"an unreferenced off-stage NPC must stay compact; got brief="
        f"{_names(ws.brief_entries)} compact={ws.compact_names}"
    )
    assert _names(ws.brief_entries) == set(), "no NPC was referenced — brief tier must be empty"
    assert "Borin" in _names(ws.full_profiles), "floor still holds"

    referencing = [
        s
        for s in _working_set_spans(otel_capture)
        if dict(s.attributes or {}).get("references_present") is True
    ]
    assert not referencing, "no reference was made — no span may claim references_present=True"


async def test_reference_match_is_case_insensitive(session_handler_factory) -> None:
    """Players type names however they like. A lower-cased mention ("joran") of an
    NPC named "Joran" must still promote it to brief — a case-sensitive match
    would make the feature near-useless in real play.

    RED on develop (no signal at all).
    """
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    _seed_roster(sd, present=[_present_npc("Borin")], offstage=[_offstage_npc("Joran")], pool=[])

    result = await handler._retrieve_entities_for_turn(sd, "i need to talk to joran right now")
    ws = _build_turn_context(sd, entity_retrieval=result).npc_working_set

    assert "Joran" in _names(ws.brief_entries), (
        "name matching must be case-insensitive; lower-case 'joran' did not promote "
        f"'Joran'; got brief={_names(ws.brief_entries)} compact={ws.compact_names}"
    )


async def test_reference_does_not_false_match_name_inside_unrelated_word(
    session_handler_factory, otel_capture
) -> None:
    """Budget integrity: a name must match as a WORD, not as a substring buried in
    an unrelated word. NPC "Art" must NOT be promoted by "I start the forge" —
    a naive ``name.lower() in action.lower()`` would (``art`` ⊂ ``start``),
    inflating the brief tier the floor exists to bound.

    (Green on develop — guards the NEW matcher against false positives.)
    """
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    _seed_roster(
        sd, present=[_present_npc("Borin")], offstage=[_offstage_npc("Art", role="cook")], pool=[]
    )

    result = await handler._retrieve_entities_for_turn(sd, "I start the forge and stoke the coals.")
    ws = _build_turn_context(sd, entity_retrieval=result).npc_working_set

    assert "Art" not in _names(ws.brief_entries), (
        "'Art' must not match as a substring of 'start' — that false positive "
        f"inflates the brief tier; got brief={_names(ws.brief_entries)}"
    )
    assert "Art" in ws.compact_names, "with no real reference, Art stays compact"

    referencing = [
        s
        for s in _working_set_spans(otel_capture)
        if dict(s.attributes or {}).get("references_present") is True
    ]
    assert not referencing, "a substring false-match must not flip references_present"
