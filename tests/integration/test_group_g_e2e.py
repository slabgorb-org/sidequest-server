"""End-to-end test contract from decomposer-spec §10 G.

Each test names its spec assertion letter in the docstring.

Scope note:
  Tests (a), (b), (d), (e), (g) are MP-ship required.
  Tests (c) guest-NPC inversion and (f) reconnect parity are deferred
  post-MP (Tasks G9, not in scope for this plan).
"""

import json

from sidequest.agents.perception_rewriter import rewrite_for_recipient
from sidequest.agents.prompt_redaction import redact_dispatch_package
from sidequest.game.projection.composed import ComposedFilter
from sidequest.game.projection.envelope import MessageEnvelope
from sidequest.game.projection.rules import load_rules_from_yaml_str
from sidequest.game.projection.view import SessionGameStateView
from sidequest.protocol.dispatch import (
    DispatchPackage,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)
from sidequest.telemetry.leak_audit import audit_canonical_prose

RULES = load_rules_from_yaml_str("""
rules:
  - kind: NARRATION
    visibility_tag: {}
  - kind: SECRET_NOTE
    visibility_tag: {}
""")


def _view(pids: list[str], zones: dict[str, str] | None = None) -> SessionGameStateView:
    return SessionGameStateView(
        gm_player_id=None,
        player_id_to_character={p: f"char_{p}" for p in pids},
        character_zones={f"char_{p}": z for p, z in (zones or {}).items()},
    )


# ---------------------------------------------------------------------------
# (a) Assassination redaction
# ---------------------------------------------------------------------------


def test_a_assassination_hidden_from_non_actor_players():
    """P1 assassinates NPC in shadows. P2-P4 streams carry no bytes of the kill.
    Alice's SECRET_NOTE gets the kill detail. All players see the cover narration.
    """
    pkg = DispatchPackage(
        turn_id="t1",
        per_player=[
            PlayerDispatch(
                player_id="player:Alice",
                raw_action="kill guard silently",
                dispatch=[
                    SubsystemDispatch(
                        subsystem="lethal_strike",
                        params={"target": "guard_A"},
                        idempotency_key="k1",
                        visibility=VisibilityTag(
                            visible_to=["player:Alice"],
                            perception_fidelity={},
                            secrets_for=["player:Alice"],
                            redact_from_narrator_canonical=True,
                        ),
                    )
                ],
            )
        ],
        confidence_global=1.0,
    )
    redacted_pkg, removed = redact_dispatch_package(pkg)
    assert len(removed) == 1

    # Narrator would now compose canonical prose *without* the kill.
    canonical = "Alice pauses at the door; the inn's evening goes on."
    # VisibilityTagFilter sees only non-redacted tags on the canonical NARRATION,
    # so canonical NARRATION goes to everybody (visible_to: all).
    filter = ComposedFilter(rules=RULES)
    narration_env = MessageEnvelope(
        kind="NARRATION",
        payload_json=json.dumps(
            {
                "text": canonical,
                "_visibility": {"visible_to": "all", "fidelity": {}},
            }
        ),
        origin_seq=1,
    )
    view = _view(["player:Alice", "player:Bob", "player:Cass"])
    for pid in ["player:Alice", "player:Bob", "player:Cass"]:
        assert filter.project(envelope=narration_env, view=view, player_id=pid).include

    # The SECRET_NOTE built from `removed` goes only to Alice.
    # SECRET_NOTE is a TARGETED_KIND — routed by the `to` field via the
    # core invariant (Task 6), so the filter short-circuits before genre
    # rules. `_visibility` is still kept for symmetry with other payloads.
    secret_env = MessageEnvelope(
        kind="SECRET_NOTE",
        payload_json=json.dumps(
            {
                "turn_id": "t1",
                "idempotency_key": "k1",
                "subsystem": "lethal_strike",
                "params": {"target": "guard_A"},
                "to": ["player:Alice"],
                "_visibility": {"visible_to": ["player:Alice"], "fidelity": {}},
            }
        ),
        origin_seq=2,
    )
    assert filter.project(envelope=secret_env, view=view, player_id="player:Alice").include
    assert not filter.project(envelope=secret_env, view=view, player_id="player:Bob").include
    assert not filter.project(envelope=secret_env, view=view, player_id="player:Cass").include


# ---------------------------------------------------------------------------
# (b) Blind fidelity
# ---------------------------------------------------------------------------


def test_b_blinded_recipient_receives_no_visual_spans():
    """P1 blinded. Narration to P1 has visual-tagged spans stripped."""
    payload = {
        "text": "A dim thing moves.",
        "spans": [
            {"id": "s1", "kind": "visual_only", "text": "a glint of steel"},
            {"id": "s2", "kind": "audio_only", "text": "boots on gravel"},
        ],
        "_visibility": {"visible_to": "all", "fidelity": {}},
    }
    out = rewrite_for_recipient(
        canonical_payload=payload,
        viewer_player_id="p1",
        status_effects={"p1": ["blinded"]},
    )
    kinds = [s["kind"] for s in out["spans"]]
    assert "visual_only" not in kinds
    assert "audio_only" in kinds


# ---------------------------------------------------------------------------
# (d) Structural hiding (prompt-builder unit test)
# ---------------------------------------------------------------------------


def test_d_structural_hiding_strips_redacted_entries():
    """Redacted dispatches never enter the DispatchPackage the prompt builder consumes."""
    pkg = DispatchPackage(
        turn_id="t1",
        per_player=[
            PlayerDispatch(
                player_id="player:Alice",
                raw_action="sneak",
                dispatch=[
                    SubsystemDispatch(
                        subsystem="lethal_strike",
                        params={"target": "guard_A"},
                        idempotency_key="k1",
                        visibility=VisibilityTag(
                            visible_to=["player:Alice"],
                            perception_fidelity={},
                            secrets_for=["player:Alice"],
                            redact_from_narrator_canonical=True,
                        ),
                    )
                ],
            )
        ],
        confidence_global=1.0,
    )
    redacted, removed = redact_dispatch_package(pkg)
    assert redacted.per_player[0].dispatch == []
    assert len(removed) == 1


# ---------------------------------------------------------------------------
# (e) Canonical-leak audit — zero leaks on clean prose
# ---------------------------------------------------------------------------


def test_e_leak_audit_zero_on_clean_prose():
    """Audit fires zero leaks when canonical prose is clean of redacted entity tokens."""
    pkg = DispatchPackage(
        turn_id="t1",
        per_player=[
            PlayerDispatch(
                player_id="player:Alice",
                raw_action="sneak",
                dispatch=[
                    SubsystemDispatch(
                        subsystem="lethal_strike",
                        params={"target": "guard_A"},
                        idempotency_key="k1",
                        visibility=VisibilityTag(
                            visible_to=["player:Alice"],
                            perception_fidelity={},
                            secrets_for=["player:Alice"],
                            redact_from_narrator_canonical=True,
                        ),
                    )
                ],
            )
        ],
        confidence_global=1.0,
    )
    result = audit_canonical_prose(
        prose="Alice pauses at the door; the inn's evening goes on.",
        package=pkg,
        entity_tokens_by_id={"guard_A": ["Rickard", "the guard"]},
    )
    assert result.leaks_detected == 0


def test_e_leak_audit_fires_nonzero_when_leak_present():
    """Audit fires a leak when canonical prose contains a redacted entity token.
    This is the lie-detector firing when structural hiding has a hole."""
    pkg = DispatchPackage(
        turn_id="t1",
        per_player=[
            PlayerDispatch(
                player_id="player:Alice",
                raw_action="sneak",
                dispatch=[
                    SubsystemDispatch(
                        subsystem="lethal_strike",
                        params={"target": "guard_A"},
                        idempotency_key="k1",
                        visibility=VisibilityTag(
                            visible_to=["player:Alice"],
                            perception_fidelity={},
                            secrets_for=["player:Alice"],
                            redact_from_narrator_canonical=True,
                        ),
                    )
                ],
            )
        ],
        confidence_global=1.0,
    )
    result = audit_canonical_prose(
        prose="Rickard slumps against the crate.",
        package=pkg,
        entity_tokens_by_id={"guard_A": ["Rickard", "the guard"]},
    )
    assert result.leaks_detected >= 1
    assert "guard_A" in result.leaked_entities


# ---------------------------------------------------------------------------
# (g) VisibilityTagFilter wiring — integration smoke
# ---------------------------------------------------------------------------


def test_g_visibility_tag_filter_excludes_non_recipient():
    """visibility_tag rule excludes a recipient not in visible_to."""
    filter = ComposedFilter(rules=RULES, pack_slug="test_pack")
    view = _view(["player:Alice", "player:Bob"])
    env = MessageEnvelope(
        kind="NARRATION",
        payload_json=json.dumps(
            {
                "text": "secret note text",
                "_visibility": {"visible_to": ["player:Alice"], "fidelity": {}},
            }
        ),
        origin_seq=1,
    )
    d_alice = filter.project(envelope=env, view=view, player_id="player:Alice")
    d_bob = filter.project(envelope=env, view=view, player_id="player:Bob")
    assert d_alice.include is True
    assert d_bob.include is False


def test_g_visibility_tag_filter_all_means_all():
    """visibility_tag rule with visible_to=all includes every viewer."""
    filter = ComposedFilter(rules=RULES, pack_slug="test_pack")
    view = _view(["player:Alice", "player:Bob", "player:Cass"])
    env = MessageEnvelope(
        kind="NARRATION",
        payload_json=json.dumps(
            {
                "text": "Dawn breaks.",
                "_visibility": {"visible_to": "all", "fidelity": {}},
            }
        ),
        origin_seq=1,
    )
    for pid in ["player:Alice", "player:Bob", "player:Cass"]:
        assert filter.project(envelope=env, view=view, player_id=pid).include
