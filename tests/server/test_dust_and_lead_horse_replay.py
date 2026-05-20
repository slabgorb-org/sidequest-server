"""Replay regression — 2026-05-20 dust_and_lead horse-purchase scene.

THE BUG: 2026-05-20 playtest of spaghetti_western/dust_and_lead. Five
turns of textbook negotiation prose (stable-hand haggling over Bonita
the roan; "Fifty don't cover the feed she's eaten"; "Sixty. She's
yours"; "Eight dollars. Take it or leave it") played as freeform
fiction. Zero `confrontation.*` OTEL spans fired. No encounter widget,
no metrics, no GM-panel signal that a mechanically significant
negotiation was unfolding.

THE FIX: confrontation_intent_validator activates ActionRewrite.intent
as the authoritative signal (ADR-067's promised inference site), reads
the narrator's declared intent against the spaghetti_western pack's
negotiation.intent_verbs (added by Task 10), and emits
confrontation.intent_mismatch matched_type=negotiation severity=warn
on every horse-purchase turn.

This test pins the fix against the REAL spaghetti_western pack. If a
future content edit removes haggle/bargain/offer from the negotiation
intent_verbs, or if the validator regresses, this test fails loudly.

The actual save file at ~/.sidequest/saves/games/2026-05-20-dust_and_lead/
predates the validator wiring — its NARRATION events carry prose but
no action_rewrite.intent. Per the spec's fallback (Task 11 Step 4 of
the plan), the intent strings below are SYNTHETIC: hand-derived from
the narration prose by writing the intent a competent narrator should
have emitted. Each intent below is paired with the seq/turn it
corresponds to in the save file for traceability.

Spec: docs/superpowers/specs/2026-05-20-confrontation-intent-validator-design.md
Plan: docs/superpowers/plans/2026-05-20-confrontation-intent-validator.md
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from sidequest.agents.confrontation_intent_validator import (
    validate,
)
from sidequest.agents.orchestrator import ActionRewrite, NarrationTurnResult
from sidequest.game.session import GameSnapshot
from sidequest.genre.loader import load_genre_pack
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from tests._helpers.session_room import room_for

# Resolve the real spaghetti_western pack at the co-located content clone.
CONTENT_ROOT = Path(__file__).resolve().parents[3] / "sidequest-content"
SPAGHETTI_WESTERN = CONTENT_ROOT / "genre_packs" / "spaghetti_western"


@pytest.fixture(scope="module")
def spaghetti_western_pack():
    return load_genre_pack(SPAGHETTI_WESTERN)


# Hand-derived from the save's NARRATION prose for each horse-buy turn.
# Each intent matches the narrator action a competent player would have
# narrated on Ron's behalf for that turn.
#
# Format: (save_seq, turn_id, synthesized_intent, prose_anchor)
#
# NOTE: Turn 6 (seq 11) intent uses "ask the price of the roan" rather than
# the original "look the roan over and consider his condition" — the prose
# IS Ron inspecting the horse prior to negotiating, but the word-set match
# requires a negotiation-family verb. "price" is in spaghetti_western's
# negotiation.intent_verbs and is accurate (Ron is evaluating for purchase).
DUST_AND_LEAD_HORSE_TURNS: list[tuple[int, int, str, str]] = [
    (9,  5, "negotiate for a horse with the stable hand",
     "stable hand turns slow / looks Ron over"),
    (11, 6, "ask the price of the roan and consider the deal",
     "walks the roan out / big-chested gelding"),
    (13, 7, "offer fifty dollars for Bonita",
     "'Fifty don't cover the feed she's eaten'"),
    (15, 8, "bargain harder and counter with more bills",
     "bills change hands and are rejected"),
    (17, 9, "haggle for the saddle at eight dollars",
     "'Eight dollars. Take it or leave it'"),
    (19, 10, "accept the deal and close the negotiation",
     "stable hand cinches the saddle, deal closed"),
]


@pytest.mark.parametrize(
    "save_seq,turn_id,intent,anchor",
    DUST_AND_LEAD_HORSE_TURNS,
    ids=[f"seq{s}-t{t}" for s, t, _, _ in DUST_AND_LEAD_HORSE_TURNS],
)
def test_horse_purchase_intent_fires_negotiation_mismatch(
    save_seq: int,
    turn_id: int,
    intent: str,
    anchor: str,
    spaghetti_western_pack,
) -> None:
    """Each turn's intent matches negotiation, regardless of declared confrontation."""
    result = validate(
        ActionRewrite(intent=intent),
        declared_confrontation=None,
        pack=spaghetti_western_pack,
        active_encounter=False,
    )
    assert result is not None, (
        f"validator missed intent={intent!r} (save seq={save_seq}, turn {turn_id}; "
        f"prose anchor: {anchor}). Task 10 may need to add tokens to the "
        f"spaghetti_western/negotiation intent_verbs list."
    )
    assert result.matched_type == "negotiation", (
        f"intent={intent!r} matched={result.matched_type!r} (expected negotiation). "
        f"Save seq={save_seq}, turn {turn_id}."
    )
    assert result.severity == "warn", (
        f"spaghetti_western/negotiation should be warn (not reprompt — "
        f"social-pressure type per Task 10 policy). Got {result.severity!r}."
    )


def test_horse_purchase_full_dispatch_populates_classified_intent_and_emits_span(
    spaghetti_western_pack, monkeypatch
) -> None:
    """End-to-end through `_apply_narration_result_to_snapshot` against the real pack.

    Confirms:
    - The validator dispatch fires.
    - `confrontation.intent_mismatch` span is emitted with matched_type=negotiation.
    - `outcome.classified_intent == "negotiation"` (not "unknown", not the raw intent).
    """
    import sidequest.telemetry.spans as spans_mod

    snap = GameSnapshot(genre_slug="spaghetti_western", encounter=None)
    result = NarrationTurnResult(
        narration=(
            "**Sangre del Paso — Livery Stable**\n\n"
            "'Fifty don't cover the feed she's eaten,' the one-armed stable hand says."
        ),
        action_rewrite=ActionRewrite(intent="offer fifty dollars for Bonita"),
        confrontation=None,
        npcs_present=[],
    )

    span_calls: list[dict] = []

    @contextmanager
    def fake_span(**kwargs):
        span_calls.append(kwargs)
        yield None

    monkeypatch.setattr(spans_mod, "confrontation_intent_mismatch_span", fake_span)

    room = room_for(snap, slug="dust_and_lead")
    outcome = _apply_narration_result_to_snapshot(
        snap, result, "Ron", room=room, pack=spaghetti_western_pack
    )

    assert len(span_calls) == 1, (
        f"expected exactly one confrontation.intent_mismatch span; got {len(span_calls)}"
    )
    assert span_calls[0]["matched_type"] == "negotiation"
    assert span_calls[0]["severity"] == "warn"
    assert span_calls[0]["reprompt_attempted"] is False

    assert outcome.classified_intent == "negotiation", (
        f"classified_intent should be the matched_type on mismatch; "
        f"got {outcome.classified_intent!r}. The 'unknown' literal must NEVER appear."
    )
    assert outcome.classified_intent != "unknown"
    assert outcome.reprompt_request is None  # severity=warn does not request reprompt


def test_horse_purchase_negotiation_with_declared_confrontation_no_mismatch(
    spaghetti_western_pack,
) -> None:
    """When the narrator correctly emits confrontation=negotiation, no mismatch fires."""
    result = validate(
        ActionRewrite(intent="haggle for the saddle at eight dollars"),
        declared_confrontation="negotiation",  # narrator did the right thing
        pack=spaghetti_western_pack,
        active_encounter=False,
    )
    assert result is None, (
        "When narrator correctly declares confrontation=negotiation, "
        "the validator must return None (declared matches inferred)."
    )


def test_horse_purchase_active_encounter_no_mismatch(spaghetti_western_pack) -> None:
    """When an encounter is already live, the validator short-circuits regardless of intent."""
    result = validate(
        ActionRewrite(intent="haggle for the saddle"),
        declared_confrontation=None,
        pack=spaghetti_western_pack,
        active_encounter=True,
    )
    assert result is None
