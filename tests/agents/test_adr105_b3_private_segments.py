"""ADR-105 B3 — SDK-narrator public-safe output contract.

The shared NARRATION ``text`` is public-safe by the amended output
contract; PC-private perception is partitioned by the narrator at
generation time into ``private_segments`` and travels its own
NARRATION_SEGMENT channel, firewalled by the visibility-gated
CoreInvariant (B1).

These prove the durable plumbing:
  - the game_patch extractor parses + sanitizes ``private_segments``
  - both result assemblers carry ``private_prose_segments`` (the
    firewall must hold on the SDK path AND the streaming path — they
    build the result differently)
  - NARRATION_SEGMENT round-trips through the replay rebuilder
  - a NARRATION_SEGMENT projected through the PRODUCTION ComposedFilter
    excludes a non-owner (the new kind is firewalled end-to-end by B1)
"""

from __future__ import annotations

import json

from sidequest.agents.orchestrator import (
    extract_structured_from_response,
)
from sidequest.game.projection.composed import ComposedFilter
from sidequest.game.projection.envelope import MessageEnvelope
from sidequest.game.projection.rules import load_rules_from_yaml_str
from sidequest.game.projection.view import SessionGameStateView
from sidequest.protocol.messages import NarrationSegmentMessage, NarrationSegmentPayload
from sidequest.server.session_handler import _KIND_TO_MESSAGE_CLS, _build_message_for_kind

# ---------------------------------------------------------------------------
# game_patch extraction
# ---------------------------------------------------------------------------


def _raw(patch: dict) -> str:
    return f"Public prose everyone sees.\n\n```game_patch\n{json.dumps(patch)}\n```"


def test_extract_parses_well_formed_private_segments():
    raw = _raw(
        {
            "private_segments": [
                {"text": "The stone gives nothing back. No ward-heat.", "anchor_pc": "Willes"},
                {"text": "  trimmed  ", "anchor_pc": "  Narder  "},
            ]
        }
    )
    out = extract_structured_from_response(raw)
    segs = out["private_segments"]
    assert len(segs) == 2
    assert segs[0] == {
        "text": "The stone gives nothing back. No ward-heat.",
        "anchor_pc": "Willes",
    }
    # whitespace is stripped on both fields
    assert segs[1] == {"text": "trimmed", "anchor_pc": "Narder"}
    # public prose is the stripped body — the private text is NOT in it
    assert "ward-heat" not in out["prose"]
    assert "Public prose everyone sees." in out["prose"]


def test_extract_drops_malformed_segments():
    raw = _raw(
        {
            "private_segments": [
                {"text": "", "anchor_pc": "Willes"},  # empty text → dropped
                {"anchor_pc": "Willes"},  # no text → dropped
                "not a dict",  # wrong type → dropped
                {"text": "valid", "anchor_pc": None},  # anchor optional → kept
            ]
        }
    )
    out = extract_structured_from_response(raw)
    assert out["private_segments"] == [{"text": "valid", "anchor_pc": None}]


def test_extract_no_private_segments_key_is_empty_list():
    out = extract_structured_from_response(_raw({}))
    assert out["private_segments"] == []


# ---------------------------------------------------------------------------
# Assembly — the firewall field must flow on BOTH backends
# ---------------------------------------------------------------------------


def test_both_assemblers_carry_private_prose_segments():
    """The SDK path uses _presentation_and_untooled_fields (shared); the
    streaming path builds NarrationTurnResult by hand. Source-level proof
    that neither silently drops the firewall field — a regression here
    re-opens the leak on one backend only (the hardest kind to catch).
    """
    import inspect

    from sidequest.agents import orchestrator as orch

    shared = inspect.getsource(orch.Orchestrator._presentation_and_untooled_fields)
    assert '"private_prose_segments": extraction["private_segments"]' in shared

    src = inspect.getsource(orch.Orchestrator)
    # The streaming assembler builds the result by hand — it must pass
    # the field explicitly (the shared helper does not cover it there).
    assert 'private_prose_segments=extraction["private_segments"]' in src


# ---------------------------------------------------------------------------
# Replay round-trip — the new kind is wired into the rebuilder
# ---------------------------------------------------------------------------


def test_narration_segment_registered_and_replay_round_trips():
    assert _KIND_TO_MESSAGE_CLS["NARRATION_SEGMENT"] is NarrationSegmentMessage
    payload = NarrationSegmentPayload(
        text="Only Willes hears the whisper.",
        anchor_pc="Willes",
        turn_id="g:w:p:7",
        visibility_sidecar={"visible_to": ["player:Willes"], "fidelity": {}},
    )
    wire = payload.model_dump_json(exclude={"seq"})
    msg = _build_message_for_kind(kind="NARRATION_SEGMENT", payload_json=wire, seq=12)
    assert isinstance(msg, NarrationSegmentMessage)
    assert msg.payload.seq == 12
    # text is a NonBlankString RootModel (same type as NarrationPayload.text)
    assert str(msg.payload.text) == "Only Willes hears the whisper."
    # _visibility round-trips under its wire alias
    assert msg.payload.visibility_sidecar == {
        "visible_to": ["player:Willes"],
        "fidelity": {},
    }


# ---------------------------------------------------------------------------
# Firewall — the new kind is excluded for a non-owner via the PRODUCTION
# ComposedFilter (B1's visibility-gated CoreInvariant covers it)
# ---------------------------------------------------------------------------


def _view() -> SessionGameStateView:
    return SessionGameStateView(
        player_id_to_character={"willes": "Willes", "narder": "Narder"},
    )


def test_narration_segment_firewalled_for_non_owner():
    payload = NarrationSegmentPayload(
        text="The stone gives nothing back. No ward-heat. No binding-pressure.",
        anchor_pc="Willes",
        turn_id="g:w:p:7",
        visibility_sidecar={"visible_to": ["willes"], "fidelity": {}},
    )
    env = MessageEnvelope(
        kind="NARRATION_SEGMENT",
        payload_json=payload.model_dump_json(exclude={"seq"}),
        origin_seq=9,
    )
    filt = ComposedFilter(rules=load_rules_from_yaml_str("rules: []"))

    willes = filt.project(envelope=env, view=_view(), player_id="willes")
    assert willes.include is True
    assert "ward-heat" in willes.payload_json

    # The 2026-05-16 leak victim: Narder must NOT receive the withheld
    # arcane-probe prose. Empty payload, structurally excluded.
    narder = filt.project(envelope=env, view=_view(), player_id="narder")
    assert narder.include is False
    assert narder.payload_json == ""

    # No GM seat (71-35): the narrator is the lie-detector and reads
    # canonical state server-side, NOT as a projection recipient — so a
    # "gm" player_id gets no special treatment and is excluded like any
    # other non-recipient.
    gm = filt.project(envelope=env, view=_view(), player_id="gm")
    assert gm.include is False
    assert gm.payload_json == ""
