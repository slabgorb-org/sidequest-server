"""Story 59-27 (RED): the Intent Router must dispatch verbal/social
confrontations, not only ``escape`` (movement) after physical escalation.

Playtest 2026-06-02 (wry_whimsy/oz solo): across a full session the ONLY
confrontation type that ever fired was ``escape`` — and only after extreme
player escalation. The verbal/social types (``persuasion``, ``audience``,
``wit_duel``, ``wonder_shock``) NEVER fired, even on direct committed prompts
("I'm not moving until I get a straight answer… convince me he's worth the
walk" — a textbook Persuasion / Audience-Trial).

Root cause (this RED pins it): the router's state summary projects each
confrontation type as only ``{type, category}`` (Story 59-10 deliberately
dropped the verb lists), and the shared ``CONFRONTATION_TRIGGER_CORE``
recognition rules enumerate social-trigger TYPE NAMES drawn from other packs
(``negotiation``, ``trial``, ``auction``, ``social_duel``, ``scandal``) — none
of which match wry_whimsy's ``persuasion`` / ``audience`` / ``wit_duel`` /
``wonder_shock``. So Haiku has no lexical bridge from natural verbal prose to a
wry_whimsy social type. The combat/pursuit side fires ``escape`` because the
core's pursuit rules mention chase/flee.

AC mapping (context-story-59-27.md):
- AC1/AC2 — the router must be TOLD wry_whimsy's verbal confrontation
  vocabulary so a committed verbal demand routes a social type. Driven by
  ``test_state_summary_social_types_carry_intent_verbs`` (projection layer) and
  ``test_router_prompt_carries_verbal_vocabulary`` (prompt layer).
- AC1(OTEL)/AC5 — a dispatched social confrontation survives the gates and the
  ``intent_router.decompose`` span fires on a verbal-only turn (wiring guard).

This module deliberately reverses the Story 59-10 projection decision (that
test asserts ``set(entry.keys()) == {"type", "category"}`` in
``test_intent_router_confrontation_vocabulary.py``). Widening the projection to
carry ``intent_verbs`` is the documented verb-set change AC2 calls for; the Dev
phase updates that 59-10 assertion.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.agents.intent_router import IntentRouter
from sidequest.game.session import GameSnapshot
from sidequest.genre.loader import load_genre_pack
from sidequest.protocol.dispatch import (
    DispatchPackage,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)
from sidequest.server.intent_router_pass import (
    _build_state_summary,
    execute_intent_router_pre_narrator_pass,
)

_CONTENT_PACKS = Path(__file__).resolve().parents[2].parent / "sidequest-content" / "genre_packs"
_WRY_WHIMSY = _CONTENT_PACKS / "wry_whimsy"

pytestmark = pytest.mark.skipif(
    not _WRY_WHIMSY.exists(),
    reason="wry_whimsy content pack not available",
)

# wry_whimsy's verbal/social confrontation types and a sample of their authored
# intent verbs (from genre_packs/wry_whimsy/rules.yaml). The router must carry
# at least these so Haiku can bridge natural verbal prose to a social type.
_SOCIAL_TYPES = ("persuasion", "audience", "wit_duel", "wonder_shock")
_PERSUASION_VERBS = {
    "persuade",
    "convince",
    "reassure",
    "befriend",
    "coax",
    "charm",
    "entreat",
}

# The committed verbal demand from the turn-10 playtest evidence — a textbook
# Persuasion / Audience-Trial that resolved as prose with confrontation=None.
_TURN_10_DEMAND = (
    "I'm not moving until I get a straight answer from the Guardian. "
    "Convince me he's worth the walk."
)


@pytest.fixture
def wry_whimsy_pack():
    return load_genre_pack(_WRY_WHIMSY)


# ``otel_capture`` is provided by tests/server/conftest.py — it installs the
# in-memory exporter with the processor-accumulation reset (Story 45-36) that a
# local redefinition would omit. Inherit it rather than duplicate it.


def _wry_snapshot() -> GameSnapshot:
    snap = GameSnapshot(genre="wry_whimsy")
    snap.genre_slug = "wry_whimsy"
    return snap


# ---------------------------------------------------------------------------
# AC2 — projection layer: the router's confrontation_types projection must
# carry the verbal vocabulary for social types (reverses 59-10's {type,
# category}-only projection).
# ---------------------------------------------------------------------------


def test_state_summary_social_types_carry_intent_verbs(wry_whimsy_pack) -> None:
    """RED: each social/verbal confrontation type the router is shown must
    carry its ``intent_verbs`` so Haiku can match natural verbal prose
    ("convince me…") to ``persuasion``/``audience``/``wit_duel``/``wonder_shock``.

    Currently the projection is ``{type, category}`` only — verbal intent has
    no lexical anchor and only the movement/combat types ever fire.
    """
    summary = _build_state_summary(_wry_snapshot(), pack=wry_whimsy_pack)
    assert "confrontation_types" in summary
    by_type = {t["type"]: t for t in summary["confrontation_types"]}

    for tname in _SOCIAL_TYPES:
        assert tname in by_type, f"wry_whimsy must project its {tname!r} type"
        entry = by_type[tname]
        assert "intent_verbs" in entry, (
            f"the {tname!r} projection must carry intent_verbs so the router can "
            f"bridge natural verbal prose to a social confrontation; got keys "
            f"{sorted(entry.keys())}"
        )
        assert entry["intent_verbs"], f"{tname!r} intent_verbs must be non-empty"


def test_persuasion_projection_includes_authored_verbs(wry_whimsy_pack) -> None:
    """RED: the ``persuasion`` projection must surface its authored verbs
    (persuade/convince/coax/charm…), the exact lexical bridge the turn-10
    demand needs."""
    summary = _build_state_summary(_wry_snapshot(), pack=wry_whimsy_pack)
    by_type = {t["type"]: t for t in summary["confrontation_types"]}
    persuasion_verbs = set(by_type["persuasion"].get("intent_verbs", []))
    assert persuasion_verbs & _PERSUASION_VERBS, (
        "the persuasion projection must include its authored verbs so the router "
        f"recognizes a committed verbal demand; got {sorted(persuasion_verbs)}"
    )


# ---------------------------------------------------------------------------
# AC1/AC2 — prompt layer: the verbal vocabulary must actually reach the model.
# Mechanism-agnostic: passes whether Dev widens the state-summary projection
# OR generalizes CONFRONTATION_TRIGGER_CORE, as long as the verbs reach the
# combined (system + user) prompt.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_router_prompt_carries_verbal_vocabulary(wry_whimsy_pack) -> None:
    """RED: the prompt the router sends to Haiku for a committed verbal demand
    must carry wry_whimsy's persuasion vocabulary. Today neither the system
    prompt (trigger core names other packs' types) nor the user prompt
    (``{type, category}`` projection) contains a single persuasion verb, so the
    model has no bridge and routes nothing (or only ``escape``)."""
    captured: dict[str, str] = {}

    class _CapturingLLM:
        async def emit_tool(self, *, system, user, tool_name, tool_description, tool_schema):
            captured["system"] = system
            captured["user"] = user
            return {
                "turn_id": "t-verbal",
                "per_player": [],
                "cross_player": [],
                "confidence_global": 0.5,
            }

    # Use an action that does NOT echo any authored persuasion verb, so a match
    # can ONLY come from the steering vocabulary — not from the player's own
    # words. (A player rarely uses the exact authored verb; the router must
    # bridge regardless, which is the whole point of carrying the vocabulary.)
    action = (
        "I plant myself at the gate and make my case to the Guardian — he has "
        "to hear me out before I take another step."
    )
    summary = _build_state_summary(_wry_snapshot(), pack=wry_whimsy_pack)
    router = IntentRouter(llm=_CapturingLLM())
    await router.decompose(action=action, state_summary=summary)

    # Excise the echoed raw action from the haystack so the assertion measures
    # the steering vocabulary, not the player's words round-tripped through the
    # <raw_action> block.
    haystack = (captured["system"] + "\n" + captured["user"]).lower().replace(action.lower(), "")
    assert any(verb in haystack for verb in _PERSUASION_VERBS), (
        "the router prompt must carry wry_whimsy's verbal confrontation "
        "vocabulary so Haiku can route a committed verbal demand to a social "
        "type; none of persuade/convince/coax/charm/… reached the prompt outside "
        "the echoed player action"
    )


# ---------------------------------------------------------------------------
# AC1(OTEL)/AC5 — wiring guard: a dispatched social confrontation survives the
# unregistered + precondition gates and the decompose span fires on a verbal
# turn. This is the integration test the suite needs: it proves the pipeline
# carries a wry_whimsy social confrontation end-to-end (not just that the
# router could, in principle, emit one).
# ---------------------------------------------------------------------------


def _persuasion_package() -> DispatchPackage:
    """A DispatchPackage a correctly-steered Haiku would emit for the turn-10
    demand: a high-confidence ``persuasion`` confrontation naming the Other."""
    return DispatchPackage(
        turn_id="t-verbal",
        per_player=[
            PlayerDispatch(
                player_id="Dorothy",
                raw_action=_TURN_10_DEMAND,
                dispatch=[
                    SubsystemDispatch(
                        subsystem="confrontation",
                        params={
                            "type": "persuasion",
                            "opponent": {
                                "name": "Guardian of the Gates",
                                "description": "the gatekeeper barring the road",
                            },
                        },
                        idempotency_key="t-verbal:persuasion:0",
                        visibility=VisibilityTag(visible_to="all"),
                        confidence=0.92,
                    )
                ],
            )
        ],
        cross_player=[],
        confidence_global=0.9,
    )


@pytest.mark.asyncio
async def test_dispatched_social_confrontation_survives_gates_and_emits_span(
    wry_whimsy_pack, otel_capture
) -> None:
    """AC1(OTEL)/AC5 wiring guard: when the router emits a ``persuasion``
    confrontation, it survives the gates (subsystem ``confrontation`` is
    registered, the type is valid), the ``intent_router.decompose`` span fires
    on the verbal-only turn (the GM-panel evidence AC1 asks for), AND the bank
    instantiates the encounter on the snapshot — the AC5 regression literal:
    a committed verbal demand engages a confrontation, NOT prose-only with
    confrontation=None."""

    class _PersuasionLLM:
        async def emit_tool(self, **_kw):
            return _persuasion_package().model_dump(mode="json")

    snap = _wry_snapshot()
    router = IntentRouter(llm=_PersuasionLLM())

    package, _bank = await execute_intent_router_pre_narrator_pass(
        intent_router=router,
        snapshot=snap,
        pack=wry_whimsy_pack,
        action=_TURN_10_DEMAND,
        player_name="Dorothy",
    )

    subsystems = [d.subsystem for pd in package.per_player for d in pd.dispatch]
    assert "confrontation" in subsystems, (
        "a wry_whimsy social confrontation was dropped by a gate before the bank"
    )

    decompose_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "intent_router.decompose"
    ]
    assert decompose_spans, "intent_router.decompose span must fire on a verbal turn"
    assert decompose_spans[-1].attributes["dispatch_count"] >= 1, (
        "the verbal turn dispatched a confrontation — dispatch_count must be >= 1"
    )

    # AC5 literal: the bank engaged the confrontation on the snapshot — an
    # encounter is present (the verbal demand became a real mechanical contest),
    # not prose-only with confrontation=None.
    assert snap.encounter is not None, (
        "a dispatched verbal confrontation must instantiate snapshot.encounter "
        "(AC5: engages a confrontation, not prose-only with confrontation=None)"
    )
    assert snap.encounter.encounter_type == "persuasion", (
        "the instantiated encounter must be the dispatched social type, not a "
        f"movement/escape fallback; got {snap.encounter.encounter_type!r}"
    )
