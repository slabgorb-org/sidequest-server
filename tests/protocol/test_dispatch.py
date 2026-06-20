"""Tests for DispatchPackage types (Group B, Local DM decomposer output contract).

Story 153-1 (output-slim): the contract was slimmed to the fields consumed
pre-narration. ``Referent`` / ``PlayerDispatch.resolved`` / ``PlayerDispatch.
lethality`` / ``ActionRewrite.you`` are gone; ``VisibilityTag`` is server-
defaulted (omittable); ``DispatchPackage`` leads with ``per_player`` and
``PlayerDispatch`` leads with ``dispatch`` (generation-order latency lever);
and a straggler-strip validator drops a removed key Haiku still emits instead
of letting ``extra="forbid"`` reject the whole package.
"""

from __future__ import annotations

import json
import logging

import pytest
from pydantic import ValidationError

from sidequest.protocol.dispatch import (
    ActionRewrite,
    CrossAction,
    DispatchPackage,
    LethalityVerdict,
    NarratorDirective,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)


def test_dispatch_package_minimal_valid():
    """A package with no actions and no cross-player events is valid."""
    pkg = DispatchPackage(
        turn_id="turn-001",
        per_player=[],
        cross_player=[],
        confidence_global=1.0,
    )
    assert pkg.per_player == []


def test_dispatch_package_full_roundtrip():
    """A package containing every field type serializes and round-trips."""
    pkg = DispatchPackage(
        turn_id="turn-042",
        per_player=[
            PlayerDispatch(
                player_id="player:Alice",
                raw_action="Let's attack him!",
                dispatch=[
                    SubsystemDispatch(
                        subsystem="distinctive_detail_hint",
                        params={"target": "npc:goblin_2", "hint": "broken tooth"},
                        depends_on=[],
                        idempotency_key="idem:turn-042:alice:0",
                        confidence=1.0,
                        visibility=VisibilityTag(
                            visible_to="all",
                            perception_fidelity={},
                            secrets_for=[],
                            redact_from_narrator_canonical=False,
                        ),
                    ),
                    SubsystemDispatch(
                        subsystem="reflect_absence",
                        params={"addressee_hint": "no party"},
                        depends_on=[],
                        idempotency_key="idem:turn-042:alice:1",
                        confidence=1.0,
                        visibility=VisibilityTag(
                            visible_to="all",
                            perception_fidelity={},
                            secrets_for=[],
                            redact_from_narrator_canonical=False,
                        ),
                    ),
                ],
                narrator_instructions=[
                    NarratorDirective(
                        kind="must_not_narrate",
                        payload="inventing an NPC follower",
                        visibility=VisibilityTag(
                            visible_to="all",
                            perception_fidelity={},
                            secrets_for=[],
                            redact_from_narrator_canonical=False,
                        ),
                    ),
                ],
            ),
        ],
        cross_player=[],
        confidence_global=0.78,
    )
    serialized = pkg.model_dump_json()
    parsed = DispatchPackage.model_validate_json(serialized)
    assert parsed == pkg


def test_visibility_tag_defaults_are_explicit():
    """Visibility tags require explicit visible_to — no implicit fallback."""
    # 'all' is a conscious choice; model should accept it.
    tag = VisibilityTag(
        visible_to="all",
        perception_fidelity={},
        secrets_for=[],
        redact_from_narrator_canonical=False,
    )
    assert tag.visible_to == "all"
    # Player-list is also accepted.
    tag2 = VisibilityTag(
        visible_to=["player:Alice"],
        perception_fidelity={"player:Alice": "full"},
        secrets_for=[],
        redact_from_narrator_canonical=False,
    )
    assert tag2.visible_to == ["player:Alice"]


def test_lethality_verdict_captures_witness_scope():
    """Spec §4.2 — verdict carries witness_scope for Group G consumption."""
    verdict = LethalityVerdict(
        entity="player:Alice",
        verdict="dead",
        cause="Salt Burrower mandible crush, 34 dmg, HP -8",
        reversibility="permanent",
        narrator_directive="Alice is dead. Compose a genre-true death.",
        soul_md_constraint="genre_truth:lethal_for_this_genre",
        witness_scope={
            "direct_witnesses": ["player:Alice"],
            "indirect_witnesses": ["player:Bob"],
            "unaware": ["player:Cass"],
            "perception_fidelity": {"player:Alice": "full", "player:Bob": "audio_only_muffled"},
        },
    )
    assert verdict.witness_scope["direct_witnesses"] == ["player:Alice"]


def test_cross_action_names_participants_and_witnesses():
    """Spec §5 — cross_player entries distinguish participants from witnesses."""
    ca = CrossAction(
        participants=["player:Alice", "player:Bob"],
        witnesses=["player:Alice", "player:Bob", "player:Cass"],
        dispatch=[],
    )
    assert set(ca.witnesses) >= set(ca.participants)


def test_dispatch_package_parses_from_llm_style_json():
    """The decomposer emits raw JSON; parser must accept it.

    Story 59-2 removed the legacy ``degraded`` / ``degraded_reason`` fields
    per ADR-113. Producer failure now raises ``IntentRouterFailure`` instead
    of returning a degraded shape; ``tests/agents/test_intent_router.py``
    covers the fail-loud retry semantics.
    """
    raw = json.dumps(
        {
            "turn_id": "turn-x",
            "per_player": [],
            "cross_player": [],
            "confidence_global": 0.9,
        }
    )
    pkg = DispatchPackage.model_validate_json(raw)
    assert pkg.turn_id == "turn-x"


def test_dispatch_package_coerces_stringified_per_player():
    """Coercion: a JSON-encoded-STRING per_player parses as the list it encodes.

    Regression (sq-playtest 2026-06-07, 5× ``intent_router.failed
    reason=schema_invalid`` across spaghetti_western + heavy_metal): Haiku
    emits ``per_player`` as ``'[{"player_id": ...}]'`` — a string — instead of
    a list. One turn (five_points-4 t22) failed BOTH attempts and the whole
    mechanical spine dropped. The content is well-formed; only the encoding
    is wrong — coerce instead of burning a retry.
    """
    stringified = json.dumps(
        [
            {
                "player_id": "player:John",
                "raw_action": "I draw on the deacon",
                "resolved": [],
                "dispatch": [],
                "lethality": [],
                "narrator_instructions": [],
            }
        ]
    )
    pkg = DispatchPackage.model_validate(
        {
            "turn_id": "turn-22",
            "per_player": stringified,
            "cross_player": [],
            "confidence_global": 0.8,
        }
    )
    assert len(pkg.per_player) == 1
    assert pkg.per_player[0].player_id == "player:John"


def test_dispatch_package_coerces_stringified_cross_player():
    """Same coercion covers cross_player — both fields hit the failure mode."""
    stringified = json.dumps(
        [
            {
                "participants": ["player:Alice", "npc:bandit"],
                "witnesses": ["player:Alice"],
                "dispatch": [],
            }
        ]
    )
    pkg = DispatchPackage.model_validate(
        {
            "turn_id": "turn-23",
            "per_player": [],
            "cross_player": stringified,
            "confidence_global": 0.7,
        }
    )
    assert len(pkg.cross_player) == 1
    # The downstream after-validator still runs on the coerced value.
    assert set(pkg.cross_player[0].witnesses) >= set(pkg.cross_player[0].participants)


def test_dispatch_package_rejects_unparseable_string_per_player():
    """A string that is not valid JSON still fails loudly — no silent default."""
    with pytest.raises(ValidationError):
        DispatchPackage.model_validate(
            {
                "turn_id": "turn-24",
                "per_player": "not json at all",
                "cross_player": [],
                "confidence_global": 0.5,
            }
        )


def test_dispatch_package_rejects_string_encoding_a_non_list():
    """A JSON string that parses to a dict/scalar is NOT coerced — reject loudly."""
    with pytest.raises(ValidationError):
        DispatchPackage.model_validate(
            {
                "turn_id": "turn-25",
                "per_player": json.dumps({"player_id": "player:X"}),
                "cross_player": [],
                "confidence_global": 0.5,
            }
        )


def test_dispatch_package_repairs_stringified_per_player_with_swallowed_confidence_global():
    """Repair the phantom-wound failure mode (sq-playtest 2026-06-14, heavy_metal/barsoom).

    CRITICAL regression: on the arena-entry turn Haiku returned the
    ``emit_dispatch_package`` tool input with ``per_player`` as a stringified
    JSON array that ALSO mashed the required sibling ``confidence_global`` field
    INTO the same string — so ``per_player`` is a string (not a list) AND
    ``confidence_global`` never appears as a top-level key. Two validation
    errors, the whole DispatchPackage dropped, the confrontation dispatch never
    fired, and the narrator improvised a sword wound with zero mechanical
    backing (no encounter, no dice, no HP delta).

    The plain ``json.loads`` repair cannot help — the value (array + trailing
    field) is not valid JSON. The before-validator must parse the leading array
    via ``raw_decode`` and recover the swallowed ``confidence_global`` so the
    confrontation dispatch SURVIVES instead of the turn going mechanically dark.
    """
    inner = json.dumps(
        [
            {
                "player_id": "Pipster",
                "raw_action": "I return to the arena for my next fight",
                "resolved": [],
                "dispatch": [
                    {
                        "subsystem": "confrontation",
                        "params": {"type": "arena_bout", "opponent": {"name": "Zodangan"}},
                        "idempotency_key": "k1",
                        "visibility": {"visible_to": "all"},
                        "confidence": 0.8,
                    }
                ],
                "lethality": [],
                "narrator_instructions": [],
            }
        ]
    )
    # The model stringified the array and swallowed `confidence_global` into it.
    swallowed = inner + ', "confidence_global": 0.72'
    pkg = DispatchPackage.model_validate(
        {
            "turn_id": "16",
            "per_player": swallowed,
            # NOTE: no top-level confidence_global — it was eaten by the string.
        }
    )
    assert len(pkg.per_player) == 1
    assert pkg.per_player[0].dispatch[0].subsystem == "confrontation"
    assert pkg.confidence_global == pytest.approx(0.72)


def test_dispatch_package_repairs_swallowed_confidence_global_messy_separator():
    """Same repair tolerates the messy `">` separator seen in the live log
    (``confidence_global">0.92``) — the recovery scrape must not depend on a
    clean ``": "`` JSON separator surviving the model's mangling."""
    inner = json.dumps(
        [
            {
                "player_id": "Pipster",
                "raw_action": "I attack",
                "resolved": [],
                "dispatch": [],
                "lethality": [],
                "narrator_instructions": [],
            }
        ]
    )
    swallowed = inner + '\n"confidence_global">0.92'
    pkg = DispatchPackage.model_validate({"turn_id": "17", "per_player": swallowed})
    assert len(pkg.per_player) == 1
    assert pkg.confidence_global == pytest.approx(0.92)


def test_cross_action_normalizes_participants_into_witnesses():
    """Validator NORMALIZES (does not reject) when a participant is missing
    from witnesses — every participant witnesses their own interaction.

    Regression (playtest 2026-05-27, coyote_star turns 3/4/5): on a shared-
    target MP turn the Intent Router emits participants=[acting_pc, npc] with
    witnesses omitting the other PC. The old reject sank the whole
    DispatchPackage → dispatch_package=None → mechanical spine dark while
    narration read fine (Illusionism). Auto-union fixes it without breaching
    the ADR-104/105 firewall (it only ADDS witnesses).
    """
    ca = CrossAction(
        participants=["player:Alice", "player:Bob"],
        witnesses=["player:Alice"],  # Bob omitted by the LLM producer
        dispatch=[],
    )
    # Bob is unioned in; Alice not duplicated; participant order preserved.
    assert ca.witnesses == ["player:Alice", "player:Bob"]
    assert set(ca.witnesses) >= set(ca.participants)


def test_cross_action_normalize_preserves_extra_witnesses():
    """Non-participant witnesses (e.g. a bystander PC) survive the union."""
    ca = CrossAction(
        participants=["player:Alice", "npc:officer"],
        witnesses=["player:Cass"],  # bystander only; both participants missing
        dispatch=[],
    )
    assert ca.witnesses == ["player:Cass", "player:Alice", "npc:officer"]
    assert set(ca.witnesses) >= set(ca.participants)


def test_dispatch_package_rejects_duplicate_idempotency_keys_within_player():
    """Validator: idempotency_keys must be unique across per_player dispatches."""
    tag = VisibilityTag(
        visible_to="all",
        perception_fidelity={},
        secrets_for=[],
        redact_from_narrator_canonical=False,
    )
    dup = SubsystemDispatch(
        subsystem="reflect_absence",
        params={},
        depends_on=[],
        idempotency_key="idem:same",
        confidence=1.0,
        visibility=tag,
    )
    with pytest.raises(ValidationError):
        DispatchPackage(
            turn_id="t",
            per_player=[
                PlayerDispatch(
                    player_id="p",
                    raw_action="",
                    dispatch=[dup, dup],
                    narrator_instructions=[],
                ),
            ],
            cross_player=[],
            confidence_global=1.0,
        )


def test_dispatch_package_rejects_duplicate_idempotency_keys_across_per_and_cross_player():
    """Validator: same idempotency_key in per_player and cross_player must fail (Fix 2)."""
    tag = VisibilityTag(
        visible_to="all",
        perception_fidelity={},
        secrets_for=[],
        redact_from_narrator_canonical=False,
    )
    d_per = SubsystemDispatch(
        subsystem="reflect_absence",
        params={},
        depends_on=[],
        idempotency_key="idem:collision",
        confidence=1.0,
        visibility=tag,
    )
    d_cross = SubsystemDispatch(
        subsystem="npc_agency",
        params={"npc_name": "x"},
        depends_on=[],
        idempotency_key="idem:collision",
        confidence=1.0,
        visibility=tag,
    )
    with pytest.raises(ValidationError):
        DispatchPackage(
            turn_id="t",
            per_player=[
                PlayerDispatch(
                    player_id="p",
                    raw_action="",
                    dispatch=[d_per],
                    narrator_instructions=[],
                )
            ],
            cross_player=[CrossAction(participants=["p"], witnesses=["p"], dispatch=[d_cross])],
            confidence_global=1.0,
        )


# ---------------------------------------------------------------------------
# Story 153-1 (output-slim) — the slimmed contract
# ---------------------------------------------------------------------------
# AC1: resolved[] / lethality[] / action_rewrite.you / Referent are removed;
#      LethalityVerdict/LethalityVerdictKind/Reversibility are RETAINED.


def test_player_dispatch_has_no_resolved_or_lethality_fields():
    """AC1: the two dead per-player fields are gone from the schema — the
    router stops generating a prose Referent list and prose lethality verdicts
    on every turn (the 0.94-with-output-tokens latency lever)."""
    fields = set(PlayerDispatch.model_fields)
    assert "resolved" not in fields
    assert "lethality" not in fields
    # The fields that survive are exactly the pre-narration-consumed ones.
    assert fields == {"dispatch", "narrator_instructions", "player_id", "raw_action"}


def test_action_rewrite_has_no_you_field():
    """AC1: action_rewrite collapses to {named, intent}. ``you`` (the
    second-person rewrite) was generated every acting turn and is cut."""
    assert "you" not in ActionRewrite.model_fields
    assert set(ActionRewrite.model_fields) == {"named", "intent"}


def test_referent_is_removed():
    """AC1: the Referent class itself is deleted (its sole consumer was the
    cut ``resolved`` list)."""
    import sidequest.protocol.dispatch as dispatch_module

    assert not hasattr(dispatch_module, "Referent")


def test_referent_removed_from_protocol_package_exports():
    """AC1: the ``sidequest.protocol`` re-export of Referent is also gone, so
    no importer can resurrect the dead type through the package surface."""
    import sidequest.protocol as protocol_pkg

    assert not hasattr(protocol_pkg, "Referent")
    assert "Referent" not in getattr(protocol_pkg, "__all__", [])


def test_lethality_verdict_types_are_retained():
    """AC1 (the explicit KEEP): LethalityVerdict + its enums survive the cut —
    the arbiter and genre/models/lethality.py still import them. Only the
    router's per-player ``lethality`` FIELD is removed, not the type."""
    from sidequest.protocol.dispatch import (
        LethalityVerdict,
        LethalityVerdictKind,
        Reversibility,
    )

    # The class is still constructible (the arbiter builds these).
    v = LethalityVerdict(
        entity="npc:goblin",
        verdict="defeated",
        cause="HP 0",
        reversibility="permanent",
        narrator_directive="render it",
        soul_md_constraint="genre_truth",
    )
    assert v.verdict == "defeated"
    assert LethalityVerdictKind is not None
    assert Reversibility is not None


# AC2: straggler tolerance — a removed key Haiku still emits is stripped+logged,
#      not rejected by extra="forbid".


def test_straggler_resolved_lethality_are_stripped_not_rejected(caplog):
    """AC2: Haiku may still emit the removed ``resolved``/``lethality`` keys out
    of habit. With ``extra="forbid"`` an un-handled removed key would reject the
    WHOLE package (dropping every dispatch — the exact spine-goes-dark failure
    the existing coercion validators guard against). They must be stripped and
    logged instead (normalize-don't-reject doctrine)."""
    raw = {
        "player_id": "player:alice",
        "raw_action": "hit the goblin",
        "resolved": [{"token": "the goblin", "resolved_to": "npc:g1", "confidence": 0.9}],
        "lethality": [
            {
                "entity": "npc:g1",
                "verdict": "defeated",
                "cause": "x",
                "reversibility": "permanent",
                "narrator_directive": "y",
                "soul_md_constraint": "z",
            }
        ],
        "dispatch": [],
    }
    with caplog.at_level(logging.INFO):
        pd = PlayerDispatch.model_validate(raw)

    assert pd.player_id == "player:alice"
    assert not hasattr(pd, "resolved")
    assert not hasattr(pd, "lethality")
    # Loud about the normalization, never silent (CLAUDE.md No Silent Fallbacks).
    assert "stripped_deprecated" in caplog.text


def test_straggler_action_rewrite_you_is_stripped():
    """AC2: the same strip-don't-reject treatment for a leftover
    ``action_rewrite.you``. named/intent survive; the package is NOT rejected
    and ``you`` does not come back as an attribute."""
    ar = ActionRewrite.model_validate(
        {"you": "You draw", "named": "Alice draws", "intent": "draw sword"}
    )
    assert ar.named == "Alice draws"
    assert ar.intent == "draw sword"
    assert not hasattr(ar, "you")


# AC3: VisibilityTag is server-defaulted (omittable) on dispatches + directives.


def test_subsystem_dispatch_visibility_defaults_to_all_when_omitted():
    """AC3: a dispatch that omits ``visibility`` validates and gets the
    server default ``visible_to="all"`` — the model only emits a tag for a
    genuine secret. Consumers reading ``.visibility.*`` still get a tag, never
    ``None``."""
    d = SubsystemDispatch.model_validate(
        {"subsystem": "confrontation", "idempotency_key": "k1", "confidence": 0.9}
    )
    assert d.visibility.visible_to == "all"
    assert d.visibility.redact_from_narrator_canonical is False


def test_narrator_directive_visibility_defaults_to_all_when_omitted():
    """AC3: same server-default for a narrator directive that omits visibility."""
    n = NarratorDirective.model_validate({"kind": "must_narrate", "payload": "x"})
    assert n.visibility.visible_to == "all"


def test_visibility_tag_still_emittable_for_secrets():
    """AC3 (the firewall stays intact): asymmetric visibility is still
    expressible — the redaction/ADR-104/105 path is preserved, the field is
    only OPTIONAL, not gone."""
    d = SubsystemDispatch.model_validate(
        {
            "subsystem": "confrontation",
            "idempotency_key": "k2",
            "confidence": 0.9,
            "visibility": {"visible_to": ["player:Alice"], "redact_from_narrator_canonical": True},
        }
    )
    assert d.visibility.visible_to == ["player:Alice"]
    assert d.visibility.redact_from_narrator_canonical is True


# AC6: field order IS generation order (the latency lever) — output is serial,
#      so the narrator-blocking fields must come off the front of the call.


def test_dispatch_package_field_order_per_player_first():
    """AC6: ``DispatchPackage`` declares ``per_player`` first and
    ``action_rewrite`` later; ``PlayerDispatch`` declares ``dispatch`` first.
    Declaration order drives JSON-schema property order, which drives the
    autoregressive generation order — so the bytes the narrator blocks on are
    generated before the trailing mechanical-only fields."""
    pkg_fields = list(DispatchPackage.model_fields)
    assert pkg_fields[0] == "per_player"
    assert pkg_fields.index("action_rewrite") > pkg_fields.index("per_player")

    pd_fields = list(PlayerDispatch.model_fields)
    assert pd_fields[0] == "dispatch"
