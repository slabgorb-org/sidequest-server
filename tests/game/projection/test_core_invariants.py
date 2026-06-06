"""CoreInvariantStage — player-identity structural filtering (no GM seat)."""

from __future__ import annotations

import json

from sidequest.game.projection.envelope import MessageEnvelope
from sidequest.game.projection.invariants import CoreInvariantStage
from sidequest.game.projection.view import SessionGameStateView


def _view() -> SessionGameStateView:
    return SessionGameStateView(
        player_id_to_character={"alice": "alice_char"},
    )


def test_plain_narration_is_non_terminal() -> None:
    """A non-targeted NARRATION matches no core invariant — the stage
    yields to GenreRuleStage. (Previously this also confirmed a player
    fell through the now-deleted GM short-circuit; that gm_sees_all
    branch is gone, so every viewer falls through here.)
    """
    stage = CoreInvariantStage()
    env = MessageEnvelope(kind="NARRATION", payload_json='{"text":"hi"}', origin_seq=2)
    outcome = stage.evaluate(envelope=env, view=_view(), player_id="alice")
    assert outcome.terminal is False
    assert outcome.decision is None


def test_secret_note_visibility_gated_routes_only_to_recipient() -> None:
    """ADR-105 B1: SECRET_NOTE recipient set lives in
    ``_visibility.visible_to`` (NOT a top-level ``to`` field —
    ``SecretNotePayload`` never carries one; that was the dead-channel
    bug). The exclusion decision is a structural CoreInvariant.
    """
    stage = CoreInvariantStage()
    # Realistic SecretNotePayload wire shape (see build_secret_note_events).
    payload = json.dumps(
        {
            "turn_id": "g:w:p:1",
            "idempotency_key": "k1",
            "subsystem": "arcane_probe",
            "params": {},
            "_visibility": {"visible_to": ["alice"], "fidelity": {}},
        }
    )
    env = MessageEnvelope(kind="SECRET_NOTE", payload_json=payload, origin_seq=3)

    out_alice = stage.evaluate(envelope=env, view=_view(), player_id="alice")
    assert out_alice.terminal is True
    assert out_alice.decision is not None
    assert out_alice.decision.include is True
    assert out_alice.source == "invariant:visibility_gated"

    out_bob = stage.evaluate(envelope=env, view=_view(), player_id="bob")
    assert out_bob.terminal is True
    assert out_bob.decision is not None
    assert out_bob.decision.include is False
    assert out_bob.decision.payload_json == ""
    assert out_bob.source == "invariant:visibility_gated"


def test_visibility_gated_all_sentinel_includes_everyone() -> None:
    stage = CoreInvariantStage()
    payload = json.dumps({"subsystem": "x", "_visibility": {"visible_to": "all"}})
    env = MessageEnvelope(kind="SECRET_NOTE", payload_json=payload, origin_seq=3)
    out = stage.evaluate(envelope=env, view=_view(), player_id="alice")
    assert out.terminal is True
    assert out.decision.include is True
    assert out.source == "invariant:visibility_gated"


def test_visibility_gated_malformed_fails_closed() -> None:
    """A secret kind with no usable ``_visibility.visible_to`` FAILS
    CLOSED (leaking is catastrophic; dropping recoverable).
    """
    stage = CoreInvariantStage()
    # No _visibility at all.
    env = MessageEnvelope(kind="SECRET_NOTE", payload_json='{"subsystem": "x"}', origin_seq=3)
    out = stage.evaluate(envelope=env, view=_view(), player_id="alice")
    assert out.terminal is True
    assert out.decision.include is False
    assert out.source == "invariant:visibility_gated"

    # _visibility present but visible_to missing.
    env2 = MessageEnvelope(kind="SECRET_NOTE", payload_json='{"_visibility": {}}', origin_seq=3)
    out2 = stage.evaluate(envelope=env2, view=_view(), player_id="alice")
    assert out2.terminal is True
    assert out2.decision.include is False


def test_narration_segment_shares_visibility_gate() -> None:
    """ADR-105 B3's per-PC private-prose channel rides the same
    structural gate as SECRET_NOTE.
    """
    stage = CoreInvariantStage()
    payload = json.dumps({"text": "you alone hear it", "_visibility": {"visible_to": ["alice"]}})
    env = MessageEnvelope(kind="NARRATION_SEGMENT", payload_json=payload, origin_seq=9)
    assert stage.evaluate(envelope=env, view=_view(), player_id="alice").decision.include is True
    bob = stage.evaluate(envelope=env, view=_view(), player_id="bob")
    assert bob.decision.include is False
    assert bob.source == "invariant:visibility_gated"


def test_dice_request_to_field_with_list() -> None:
    stage = CoreInvariantStage()
    payload = json.dumps({"to": ["alice", "bob"], "dice": "d20"})
    env = MessageEnvelope(kind="DICE_REQUEST", payload_json=payload, origin_seq=4)

    assert stage.evaluate(envelope=env, view=_view(), player_id="alice").decision.include is True
    assert stage.evaluate(envelope=env, view=_view(), player_id="bob").decision.include is True
    assert stage.evaluate(envelope=env, view=_view(), player_id="carol").decision.include is False


def test_non_targeted_kind_has_no_to_field_invariant() -> None:
    stage = CoreInvariantStage()
    env = MessageEnvelope(kind="NARRATION", payload_json='{"text":"hi"}', origin_seq=5)
    outcome = stage.evaluate(envelope=env, view=_view(), player_id="alice")
    assert outcome.terminal is False


SELF_AUTHORED_PAYLOAD = json.dumps({"author_player_id": "alice", "action": "jump"})


def test_self_authored_kind_echoes_to_author_only() -> None:
    stage = CoreInvariantStage()
    env = MessageEnvelope(kind="PLAYER_ACTION", payload_json=SELF_AUTHORED_PAYLOAD, origin_seq=6)

    out_alice = stage.evaluate(envelope=env, view=_view(), player_id="alice")
    assert out_alice.terminal is True
    assert out_alice.decision.include is True

    out_bob = stage.evaluate(envelope=env, view=_view(), player_id="bob")
    assert out_bob.terminal is True
    assert out_bob.decision.include is False


def test_self_authored_missing_author_field_omits_for_all_viewers() -> None:
    stage = CoreInvariantStage()
    env = MessageEnvelope(kind="DICE_THROW", payload_json='{"dice": "d20"}', origin_seq=7)
    outcome = stage.evaluate(envelope=env, view=_view(), player_id="alice")
    assert outcome.terminal is True
    assert outcome.decision.include is False


def test_thinking_is_player_excluded_never_routed_to_players() -> None:
    stage = CoreInvariantStage()
    env = MessageEnvelope(kind="THINKING", payload_json='{"thought":"hmm"}', origin_seq=8)
    outcome = stage.evaluate(envelope=env, view=_view(), player_id="alice")
    assert outcome.terminal is True
    assert outcome.decision.include is False
    # Pin the OTEL source label the GM panel reads — a typo here would
    # silently mislabel the firewall decision (CLAUDE.md OTEL mandate).
    assert outcome.source == "invariant:player_excluded_kind"


# ---------------------------------------------------------------------------
# 1c — NARRATION explicit visible_to list (pingpong 2026-06-05 [BAR-1]:
# solo cold-open seed leaked second-person prose to an MP joiner).
# ---------------------------------------------------------------------------


def _narration_with_visible_to(visible_to: object) -> MessageEnvelope:
    payload = json.dumps(
        {
            "text": "The wind is thin and hard at this height.",
            "_visibility": {
                "visible_to": visible_to,
                "fidelity": {},
                "anchor_pc": "Groucho",
                "pov_strategy": "private",
            },
        }
    )
    return MessageEnvelope(kind="NARRATION", payload_json=payload, origin_seq=9)


def test_narration_visible_to_list_excludes_non_member_terminally() -> None:
    """A NARRATION carrying an explicit player_id LIST is structurally
    withheld from non-members — pack projection.yaml cannot weaken it by
    omission (four packs ship none)."""
    stage = CoreInvariantStage()
    env = _narration_with_visible_to(["alice"])
    out_bob = stage.evaluate(envelope=env, view=_view(), player_id="bob")
    assert out_bob.terminal is True
    assert out_bob.decision is not None
    assert out_bob.decision.include is False
    assert out_bob.decision.payload_json == ""
    assert out_bob.source == "invariant:narration_visibility_list"


def test_narration_visible_to_list_member_falls_through_to_genre_rules() -> None:
    """ASYMMETRY is load-bearing: a member is NOT terminally included —
    the stage yields so GenreRuleStage redact/fidelity rules still run."""
    stage = CoreInvariantStage()
    env = _narration_with_visible_to(["alice"])
    out_alice = stage.evaluate(envelope=env, view=_view(), player_id="alice")
    assert out_alice.terminal is False
    assert out_alice.decision is None


def test_narration_visible_to_all_sentinel_falls_through() -> None:
    """The classifier's standing ``"all"`` sentinel keeps today's
    behavior — no structural decision, broadcast prose."""
    stage = CoreInvariantStage()
    env = _narration_with_visible_to("all")
    outcome = stage.evaluate(envelope=env, view=_view(), player_id="bob")
    assert outcome.terminal is False
    assert outcome.decision is None


def test_narration_without_sidecar_falls_through() -> None:
    """NARRATION is NOT fail-closed (unlike SECRET_NOTE): no sidecar is
    ordinary broadcast prose."""
    stage = CoreInvariantStage()
    env = MessageEnvelope(kind="NARRATION", payload_json='{"text":"hi"}', origin_seq=10)
    outcome = stage.evaluate(envelope=env, view=_view(), player_id="bob")
    assert outcome.terminal is False
    assert outcome.decision is None
