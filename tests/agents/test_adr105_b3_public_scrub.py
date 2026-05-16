"""ADR-105 B3 ENFORCEMENT — mechanical public-prose scrub.

oq-1 VERIFY-FAIL 2026-05-16: B1/B2/segment-gating are Jaeger-proven, but
the live SDK narrator duplicates the withheld reading into the public
PART-1 blob — including a self-labelled "⚠ Aside — Private (X only):"
block — *in parallel* with the correctly-gated NARRATION_SEGMENT. The
shared blob is visible_to:"all", so the duplicate bypasses the firewall.
Prompt adherence is unreliable (the "Claude wings it" failure the OTEL
principle exists to catch). These prove the MECHANICAL backstop:

  - the labelled private-aside block is stripped from the public blob
  - a near-duplicate of a private segment copied into PART 1 is stripped
  - a clean fully-public turn is byte-unchanged (no false positives)
  - the scrub fires the narration.public_scrub lie-detector
  - end-to-end through extract_structured_from_response the public
    `prose` no longer carries the withheld content (the exact oq-1
    verify-fail repro shape) while private_segments is still parsed
"""

from __future__ import annotations

import json

import pytest

from sidequest.agents.orchestrator import (
    _scrub_public_prose,
    extract_structured_from_response,
)

# The exact failure oq-1 evidenced (screenshots 305/306), Narder's tab.
# Per oq-1: the leaked PART-1 content was "byte-identical modulo POV/
# label only" to the private segment — i.e. the narrator near-copies its
# own private text into PART 1, NOT a loose paraphrase. The fixture
# models that real behavior (interwoven near-verbatim duplicate + a
# self-labelled aside + the structured game_patch segment).
_SEG_TEXT = (
    "Two distinct magical auras answer beyond the grate. One is large "
    "and diffuse, old binding-work; the second is smaller, brighter, "
    "recent, and it is active, not dormant — it is being drawn."
)
_LEAK_RAW = (
    "**The Grate**\n\n"
    "Willes kneels at the chalk-cross, eyes closed, breathing slow. "
    "Narder sets his back to the wall, blade up, watching the dark.\n\n"
    # Interwoven near-verbatim duplicate of the private segment (POV/
    # tense shifted only — exactly oq-1's "byte-identical modulo POV").
    "Two distinct magical auras answer beyond the grate; one large and "
    "diffuse, old binding-work, the second smaller, brighter, recent, "
    "active not dormant, and it is being drawn.\n\n"
    "⚠ Aside — Private (Willes only): " + _SEG_TEXT + "\n\n"
    "```game_patch\n"
    + json.dumps({"private_segments": [{"text": _SEG_TEXT, "anchor_pc": "Willes"}]})
    + "\n```"
)


def test_labelled_private_aside_block_is_stripped():
    prose = (
        "Willes kneels at the chalk-cross, eyes closed.\n\n"
        "⚠ Aside — Private (Willes only): Two auras beyond the grate, "
        "one old and one active."
    )
    out, report = _scrub_public_prose(prose, [])
    assert "Aside" not in out
    assert "Private (Willes only)" not in out
    assert "two auras" not in out.lower()
    assert "Willes kneels at the chalk-cross" in out
    assert report["labelled_blocks_removed"] == 1
    # No structured segment carried it → loud orphan signal.
    assert report["orphan_private_block"] is True


def test_you_only_label_variant_stripped():
    prose = (
        "You kneel at the chalk-cross.\n\n"
        "Privately (you only): the second aura is active, not dormant."
    )
    out, _ = _scrub_public_prose(prose, [])
    assert "active, not dormant" not in out
    assert "You kneel at the chalk-cross" in out


def test_near_duplicate_segment_sentence_stripped_from_public():
    seg = {
        "text": (
            "Two magical auras lie beyond the grate. One is large and "
            "diffuse, old binding-work; the second is smaller, brighter, "
            "recent, and active, not dormant."
        ),
        "anchor_pc": "Willes",
    }
    prose = (
        "Willes kneels at the chalk-cross, eyes closed, breathing slow. "
        "Two magical auras lie beyond the grate, one large and diffuse "
        "old binding-work, the second smaller brighter recent and active "
        "not dormant."
    )
    out, report = _scrub_public_prose(prose, [seg])
    # The public scene-setting survives; the duplicated reading does not.
    assert "Willes kneels at the chalk-cross" in out
    assert "magical auras" not in out.lower()
    assert report["dup_sentences_removed"] >= 1


def test_clean_public_turn_is_byte_unchanged():
    prose = (
        "Willes kneels at the chalk-cross, eyes closed. Narder sets his "
        "back to the wall, blade up. The torch gutters in the draft."
    )
    out, report = _scrub_public_prose(prose, [])
    assert out == prose
    assert report["labelled_blocks_removed"] == 0
    assert report["dup_sentences_removed"] == 0
    assert report["chars_removed"] == 0


def test_incidental_private_word_not_a_false_positive():
    """A public sentence that merely contains the word 'private' (no
    privacy qualifier) must NOT be scrubbed — the marker requires an
    '(X only)'/'kept to …self'/'no outward sign' qualifier."""
    prose = "Marya keeps a private ledger behind the bar, in plain view of the room."
    out, report = _scrub_public_prose(prose, [])
    assert out == prose
    assert report["labelled_blocks_removed"] == 0


def test_degrades_safely_when_pass2_would_empty_prose():
    """Pathological: narrator wrote ZERO public content, only the
    duplicated private reading. Pass 2 is skipped so the turn still
    surfaces some text (pass 1 still applies) rather than an
    unrenderable empty blob."""
    seg = {"text": "the second aura is active not dormant", "anchor_pc": "W"}
    prose = "The second aura is active, not dormant."
    out, report = _scrub_public_prose(prose, [seg])
    assert out  # not emptied
    assert report["degraded"] is True


def test_scrub_emits_public_scrub_watcher(monkeypatch: pytest.MonkeyPatch):
    events: list[dict] = []

    import sidequest.telemetry.watcher_hub as wh

    monkeypatch.setattr(
        wh,
        "publish_event",
        lambda et, fields, **kw: events.append({"et": et, "fields": fields, "kw": kw}),
    )
    _scrub_public_prose(
        "Public.\n\n⚠ Aside — Private (Willes only): leaked secret here.", []
    )
    scrub = [e for e in events if e["fields"].get("field") == "narration.public_scrub"]
    assert len(scrub) == 1
    assert scrub[0]["fields"]["labelled_blocks_removed"] == 1
    assert scrub[0]["kw"].get("component") == "projection"


def test_end_to_end_extract_public_prose_is_firewalled():
    """The exact oq-1 verify-fail repro: a raw narrator response whose
    PART 1 carries both an interwoven reading and a labelled private
    aside, plus a game_patch.private_segments duplicate. After
    extraction the public `prose` must carry NEITHER, while the
    structured private_segments is still parsed for the gated channel."""
    out = extract_structured_from_response(_LEAK_RAW)

    public = out["prose"]
    # The public scene the whole table may see survives.
    assert "Willes kneels at the chalk-cross" in public
    assert "Narder sets his back to the wall" in public
    # The withheld reading + the self-labelled private block do NOT.
    assert "Aside" not in public
    assert "Private (Willes only)" not in public
    assert "two distinct auras" not in public.lower()
    assert "active, not dormant" not in public.lower()

    # The structured private channel is intact (B1 gates it to Willes).
    assert len(out["private_segments"]) == 1
    assert out["private_segments"][0]["anchor_pc"] == "Willes"
    assert "auras" in out["private_segments"][0]["text"].lower()


def test_extract_clean_turn_unaffected_by_scrub():
    raw = (
        "**The Hall**\n\nWilles opens the door. Dust drifts in the lamplight.\n\n"
        "```game_patch\n" + json.dumps({"mood": "tense"}) + "\n```"
    )
    out = extract_structured_from_response(raw)
    assert "Willes opens the door" in out["prose"]
    assert "Dust drifts in the lamplight" in out["prose"]
    assert out["private_segments"] == []
