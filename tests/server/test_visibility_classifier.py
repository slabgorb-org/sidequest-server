"""Unit tests for the narration visibility classifier (Story 49-8).

Found in the 2026-05-12 playtest: ``websocket_session_handler.py:2955``
ships ``visibility_sidecar=None`` so every player receives every per-PC
narration card identically, third-person. The classifier produces the
sidecar dict that drives both:

  (a) per-recipient include/exclude via the existing ``visibility_tag``
      rule (``sidequest.game.projection.genre_stage``), and
  (b) 2nd-person POV swap on the recipient whose ``player_id`` maps to
      the card's ``anchor_pc`` character.

Lives at ``sidequest.server.visibility_classifier``. Contract:

    classify_narration_visibility(
        result: NarrationTurnResult,
        snapshot: GameSnapshot,
        connected_player_ids: list[str],
        player_id_to_character: dict[str, str],
    ) -> dict

Returns a sidecar dict with the v2 shape:

    {
      "visible_to": "all" | [player_id, ...],
      "fidelity":   {entity_id: fidelity_level},   # preserved from v1
      "anchor_pc":  "Carl" | None,
      "pov_strategy": "pc_anchored" | "atmospheric" | "private",
    }

Anchor inference order:
  1. If ``result.action_rewrite.named`` exists, extract the subject PC
     name from it.
  2. Else scan the first sentence of ``result.narration`` for a known
     PC name from ``snapshot.characters``.
  3. Else ``anchor_pc=None``, ``pov_strategy='atmospheric'``.

These tests RED until ``sidequest.server.visibility_classifier`` is
created. They prove:
  - the sidecar shape conforms,
  - anchor inference prefers structured field over prose,
  - atmospheric (no PC mention) returns None anchor,
  - existing v1 callers (visible_to / fidelity) still produce valid
    output (the new fields are purely additive).
"""

from __future__ import annotations

import pytest

from sidequest.agents.orchestrator import (
    ActionRewrite,
    NarrationTurnResult,
)
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.session import GameSnapshot

# RED until module is created — import at module scope so collection
# itself fails noisily.
from sidequest.server.visibility_classifier import (  # noqa: F401
    classify_narration_visibility,
)


def _pc(name: str, pronouns: str = "he/him") -> Character:
    core = CreatureCore(
        name=name,
        description="Test PC.",
        personality="Stoic.",
        inventory=Inventory(),
    )
    return Character(
        core=core,
        backstory="A wanderer of nominal scope.",
        char_class="Fighter",
        race="Human",
        pronouns=pronouns,
    )


def _snapshot(pcs: list[Character]) -> GameSnapshot:
    snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="sunden")
    snap.characters = pcs
    return snap


# ---------------------------------------------------------------------------
# Shape conformance — v2 sidecar fields are present and well-typed
# ---------------------------------------------------------------------------


def test_sidecar_has_v2_fields_present():
    """The classifier must emit the v2 keys: visible_to, fidelity,
    anchor_pc, pov_strategy. Missing keys would break ComposedFilter
    or the 2nd-person swap downstream."""
    snap = _snapshot([_pc("Carl"), _pc("Donut"), _pc("Katia", pronouns="she/her")])
    result = NarrationTurnResult(
        narration="Carl plants a boot on the moth's thorax.",
        action_rewrite=ActionRewrite(
            you="You plant a boot",
            named="Carl plants a boot",
            intent="plant boot",
        ),
    )
    out = classify_narration_visibility(
        result=result,
        snapshot=snap,
        connected_player_ids=["p1", "p2", "p3"],
        player_id_to_character={"p1": "Carl", "p2": "Donut", "p3": "Katia"},
    )
    assert set(out.keys()) >= {"visible_to", "fidelity", "anchor_pc", "pov_strategy"}, (
        f"missing v2 keys: {set(out.keys())}"
    )


def test_sidecar_visible_to_is_all_when_no_redaction():
    """Default narration is broadcast to all connected players — the
    new sidecar does NOT change that. This story is NOT ADR-028. Filter
    is purely additive (anchor + POV) for now."""
    snap = _snapshot([_pc("Carl"), _pc("Donut")])
    result = NarrationTurnResult(
        narration="Carl plants a boot.",
        action_rewrite=ActionRewrite(
            you="You plant a boot",
            named="Carl plants a boot",
            intent="plant",
        ),
    )
    out = classify_narration_visibility(
        result=result,
        snapshot=snap,
        connected_player_ids=["p1", "p2"],
        player_id_to_character={"p1": "Carl", "p2": "Donut"},
    )
    assert out["visible_to"] == "all"


# ---------------------------------------------------------------------------
# Anchor inference — prefer action_rewrite.named (structured), else
# fallback to the first sentence of narration
# ---------------------------------------------------------------------------


def test_anchor_prefers_action_rewrite_named_over_prose():
    """When the narrator emits action_rewrite, the structured 'named'
    form is authoritative — the prose may invert subject/object or
    open with an environmental sentence."""
    snap = _snapshot([_pc("Carl"), _pc("Donut")])
    result = NarrationTurnResult(
        # Prose opens with environment, not the PC name.
        narration="The corridor narrows. Carl plants a boot.",
        action_rewrite=ActionRewrite(
            you="You plant a boot",
            named="Carl plants a boot on the moth",
            intent="plant boot",
        ),
    )
    out = classify_narration_visibility(
        result=result,
        snapshot=snap,
        connected_player_ids=["p1", "p2"],
        player_id_to_character={"p1": "Carl", "p2": "Donut"},
    )
    assert out["anchor_pc"] == "Carl"
    assert out["pov_strategy"] == "pc_anchored"


def test_anchor_falls_back_to_first_sentence_when_no_action_rewrite():
    """ADR-098 stateless turns may drop action_rewrite occasionally
    (the absent-field warning in orchestrator.py). The classifier must
    still recover the anchor by scanning prose for a known PC name."""
    snap = _snapshot([_pc("Carl"), _pc("Donut")])
    result = NarrationTurnResult(
        narration="Carl plants a boot on the moth's thorax.",
        action_rewrite=None,
    )
    out = classify_narration_visibility(
        result=result,
        snapshot=snap,
        connected_player_ids=["p1", "p2"],
        player_id_to_character={"p1": "Carl", "p2": "Donut"},
    )
    assert out["anchor_pc"] == "Carl"
    assert out["pov_strategy"] == "pc_anchored"


def test_anchor_fallback_only_matches_known_pc_names():
    """The prose scan must only match names of PCs that are actually
    in the snapshot — otherwise an NPC name in the opening sentence
    would be misidentified as the anchor PC. NPCs do NOT get 2nd-person
    swap on anyone's tab."""
    snap = _snapshot([_pc("Carl"), _pc("Donut")])
    result = NarrationTurnResult(
        # Opens with an NPC name (Rickard) that is NOT in the PC roster.
        narration="Rickard stumbles back from the door, blood on his coat.",
        action_rewrite=None,
    )
    out = classify_narration_visibility(
        result=result,
        snapshot=snap,
        connected_player_ids=["p1", "p2"],
        player_id_to_character={"p1": "Carl", "p2": "Donut"},
    )
    assert out["anchor_pc"] is None
    assert out["pov_strategy"] == "atmospheric"


# ---------------------------------------------------------------------------
# Atmospheric narration — no PC anchor, broadcast unchanged
# ---------------------------------------------------------------------------


def test_atmospheric_no_pc_mention_yields_no_anchor():
    """A pure scene-setting narration with no PC named anywhere is
    atmospheric — broadcast to all players with no swap."""
    snap = _snapshot([_pc("Carl"), _pc("Donut")])
    result = NarrationTurnResult(
        narration="Rain hammers the slate roof. The corridor smells of wet iron.",
        action_rewrite=ActionRewrite(you="", named="", intent=""),
    )
    out = classify_narration_visibility(
        result=result,
        snapshot=snap,
        connected_player_ids=["p1", "p2"],
        player_id_to_character={"p1": "Carl", "p2": "Donut"},
    )
    assert out["anchor_pc"] is None
    assert out["pov_strategy"] == "atmospheric"
    assert out["visible_to"] == "all"


def test_atmospheric_empty_action_rewrite_with_no_pc_mention():
    """When action_rewrite is empty strings (no PC acting this turn)
    AND no PC name in prose, classifier yields atmospheric."""
    snap = _snapshot([_pc("Carl"), _pc("Donut")])
    result = NarrationTurnResult(
        narration="The torches gutter. Dust settles.",
        action_rewrite=ActionRewrite(you="", named="", intent=""),
    )
    out = classify_narration_visibility(
        result=result,
        snapshot=snap,
        connected_player_ids=["p1", "p2"],
        player_id_to_character={"p1": "Carl", "p2": "Donut"},
    )
    assert out["anchor_pc"] is None
    assert out["pov_strategy"] == "atmospheric"


# ---------------------------------------------------------------------------
# Solo-session correctness — N=1 sessions still classify correctly
# ---------------------------------------------------------------------------


def test_solo_session_anchor_still_identifies_pc():
    """Single-PC sessions are the dominant path on Keith's playgroup
    saves today. anchor_pc must still resolve so the solo player gets
    'You' on their own action."""
    snap = _snapshot([_pc("Mira", pronouns="she/her")])
    result = NarrationTurnResult(
        narration="Mira reaches for the latch.",
        action_rewrite=ActionRewrite(
            you="You reach for the latch",
            named="Mira reaches for the latch",
            intent="reach latch",
        ),
    )
    out = classify_narration_visibility(
        result=result,
        snapshot=snap,
        connected_player_ids=["solo_player"],
        player_id_to_character={"solo_player": "Mira"},
    )
    assert out["anchor_pc"] == "Mira"
    assert out["pov_strategy"] == "pc_anchored"


# ---------------------------------------------------------------------------
# Negative case — anchor is a PC name not in snapshot.characters
# ---------------------------------------------------------------------------


def test_action_rewrite_named_referencing_unknown_pc_falls_back_to_prose():
    """If action_rewrite.named references a character not in the PC
    roster (NPC narration, orchestrator confusion), the classifier
    must fall back to prose scanning rather than minting a fake anchor.
    This prevents the swap helper from running on a name no recipient
    maps to."""
    snap = _snapshot([_pc("Carl"), _pc("Donut")])
    result = NarrationTurnResult(
        # named references an NPC, prose names Carl
        narration="Carl ducks behind the pillar as Rickard fires.",
        action_rewrite=ActionRewrite(
            you="",
            named="Rickard fires the shotgun",
            intent="fire",
        ),
    )
    out = classify_narration_visibility(
        result=result,
        snapshot=snap,
        connected_player_ids=["p1", "p2"],
        player_id_to_character={"p1": "Carl", "p2": "Donut"},
    )
    # The classifier must prefer the PC-roster-matching name found in
    # prose over the unverified action_rewrite.named referring to an NPC.
    assert out["anchor_pc"] == "Carl", (
        "action_rewrite.named must validate against snapshot.characters; "
        "if it's an NPC name, fall back to prose scanning"
    )


# ---------------------------------------------------------------------------
# Defensive cases — bad inputs must fail loud
# ---------------------------------------------------------------------------


def test_empty_narration_raises():
    """An empty narration text would be unrenderable upstream — the
    classifier must fail loud rather than emit a sidecar with an
    ambiguous shape."""
    snap = _snapshot([_pc("Carl")])
    result = NarrationTurnResult(narration="", action_rewrite=None)
    with pytest.raises(ValueError):
        classify_narration_visibility(
            result=result,
            snapshot=snap,
            connected_player_ids=["p1"],
            player_id_to_character={"p1": "Carl"},
        )


# ---------------------------------------------------------------------------
# ADR-105 B2 — derive the real private-route map from result.secret_routes
# ---------------------------------------------------------------------------

from sidequest.protocol.dispatch import (  # noqa: E402
    NarratorDirective,
    SubsystemDispatch,
    VisibilityTag,
)
from tests.server.conftest import span_attrs_by_name  # noqa: E402


def _redacted_dispatch(
    key: str, visible_to: list[str] | str, *, subsystem: str = "arcane_probe"
) -> SubsystemDispatch:
    return SubsystemDispatch(
        subsystem=subsystem,
        params={"reading": "no ward-heat"},
        idempotency_key=key,
        visibility=VisibilityTag(
            visible_to=visible_to,
            perception_fidelity={},
            secrets_for=visible_to if isinstance(visible_to, list) else [],
            redact_from_narrator_canonical=True,
        ),
    )


def test_secret_routes_derive_private_segments():
    """ADR-105 B2: each redacted SubsystemDispatch becomes a
    private-segment entry carrying its own recipient set; the SHARED
    text stays public ("all")."""
    snap = _snapshot([_pc("Willes"), _pc("Narder")])
    result = NarrationTurnResult(
        narration="Willes closes both eyes and pushes attention through the bars.",
        action_rewrite=ActionRewrite(
            you="You push your senses", named="Willes pushes", intent="probe"
        ),
        secret_routes=[
            _redacted_dispatch("k1", ["p1"]),
            _redacted_dispatch("k2", ["p2", "p1"], subsystem="trap_sense"),
        ],
    )
    out = classify_narration_visibility(
        result=result,
        snapshot=snap,
        connected_player_ids=["p1", "p2"],
        player_id_to_character={"p1": "Willes", "p2": "Narder"},
    )
    # Shared blob remains public — the partition is the segment.
    assert out["visible_to"] == "all"
    segs = out["private_segments"]
    assert len(segs) == 2
    assert segs[0]["visible_to"] == ["p1"]
    assert segs[0]["subsystem"] == "arcane_probe"
    assert segs[0]["idempotency_key"] == "k1"
    # union_visible_to normalizes (sorted, de-duped).
    assert segs[1]["visible_to"] == ["p1", "p2"]


def test_private_segment_all_stopword_collapse():
    """A redacted route whose visible_to is the 'all' sentinel collapses
    to 'all' via the shared union_visible_to stop-word rule (the same
    rule aggregate_visibility uses — no drift)."""
    snap = _snapshot([_pc("Willes")])
    result = NarrationTurnResult(
        narration="Willes listens.",
        action_rewrite=None,
        secret_routes=[_redacted_dispatch("k1", "all")],
    )
    out = classify_narration_visibility(
        result=result,
        snapshot=snap,
        connected_player_ids=["p1"],
        player_id_to_character={"p1": "Willes"},
    )
    assert out["private_segments"][0]["visible_to"] == "all"


def test_no_secret_routes_byte_unchanged():
    """ADR-105 regression obligation: a turn with no redacted routes
    carries the EXACT v2 sidecar shape — no ``private_segments`` key at
    all (solo/atmospheric byte-unchanged)."""
    snap = _snapshot([_pc("Carl"), _pc("Donut")])
    result = NarrationTurnResult(
        narration="Carl plants a boot.",
        action_rewrite=ActionRewrite(you="You plant", named="Carl plants", intent="plant"),
    )
    out = classify_narration_visibility(
        result=result,
        snapshot=snap,
        connected_player_ids=["p1", "p2"],
        player_id_to_character={"p1": "Carl", "p2": "Donut"},
    )
    assert set(out.keys()) == {"visible_to", "fidelity", "anchor_pc", "pov_strategy"}
    assert "private_segments" not in out


def test_non_subsystemdispatch_routes_skipped():
    """Mirror build_secret_note_events: only SubsystemDispatch entries
    route — a NarratorDirective in secret_routes is skipped, not
    crashed."""
    snap = _snapshot([_pc("Willes")])
    directive = NarratorDirective(
        kind="must_not_narrate",
        payload="hidden",
        visibility=VisibilityTag(
            visible_to=["p1"],
            perception_fidelity={},
            secrets_for=["p1"],
            redact_from_narrator_canonical=True,
        ),
    )
    result = NarrationTurnResult(
        narration="Willes waits.",
        action_rewrite=None,
        secret_routes=[directive, _redacted_dispatch("k1", ["p1"])],
    )
    out = classify_narration_visibility(
        result=result,
        snapshot=snap,
        connected_player_ids=["p1"],
        player_id_to_character={"p1": "Willes"},
    )
    # Only the SubsystemDispatch produced a segment.
    assert len(out["private_segments"]) == 1
    assert out["private_segments"][0]["idempotency_key"] == "k1"


def test_span_emits_private_segment_count(otel_capture):
    """The narration.visibility_classified span carries the derived
    private partition — the GM-panel lie-detector for the firewall."""
    snap = _snapshot([_pc("Willes"), _pc("Narder")])
    result = NarrationTurnResult(
        narration="Willes probes the bars.",
        action_rewrite=ActionRewrite(you="You probe", named="Willes probes", intent="probe"),
        secret_routes=[_redacted_dispatch("k1", ["p1"])],
    )
    classify_narration_visibility(
        result=result,
        snapshot=snap,
        connected_player_ids=["p1", "p2"],
        player_id_to_character={"p1": "Willes", "p2": "Narder"},
    )
    attrs = span_attrs_by_name(otel_capture, "narration.visibility_classified")
    assert len(attrs) == 1
    assert attrs[0]["private_segment_count"] == 1
    assert attrs[0]["private_visible_to"] == "p1"


def test_span_surfaces_prose_segment_count(otel_capture):
    """oq-1 VERIFY-FAIL 2026-05-16: private_segment_count counts only
    secret_routes-derived segments and read 0 while a NARRATION_SEGMENT
    from the narrator's PROSE partition was emitted + gated. The span
    must distinctly surface the prose-partition count so the GM panel
    sees the turn DID carry a private prose segment."""
    snap = _snapshot([_pc("Willes"), _pc("Narder")])
    result = NarrationTurnResult(
        narration="Willes kneels at the chalk-cross, eyes closed.",
        action_rewrite=ActionRewrite(
            you="You kneel", named="Willes kneels", intent="probe"
        ),
        # No structured secret_routes (the withheld content came as
        # narrator PROSE) — yet a prose segment WAS partitioned.
        private_prose_segments=[
            {"text": "Two auras, one active.", "anchor_pc": "Willes"}
        ],
    )
    classify_narration_visibility(
        result=result,
        snapshot=snap,
        connected_player_ids=["p1", "p2"],
        player_id_to_character={"p1": "Willes", "p2": "Narder"},
    )
    attrs = span_attrs_by_name(otel_capture, "narration.visibility_classified")
    assert len(attrs) == 1
    # The old (misleading) count is 0 — there were no structured routes.
    assert attrs[0]["private_segment_count"] == 0
    # The new count exposes the prose partition that WAS emitted+gated.
    assert attrs[0]["private_prose_segment_count"] == 1
    assert attrs[0]["visible_to"] == "all"
