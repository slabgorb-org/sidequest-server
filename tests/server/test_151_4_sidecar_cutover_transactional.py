"""RED tests for Story 151-4 — Sidecar cutover I: transactional fields (ADR-150 step 4).

The first *real* bucket-B cutover. Seven **transactional** sidecar fields —
``items_gained`` / ``items_lost`` / ``items_discarded`` / ``items_consumed``,
``gold_change``, ``companions_added`` / ``companions_dismissed`` — move OFF the
narrator's post-narration ``game_patch`` sidecar and ONTO the post-narration
Haiku extractor built in Story 151-2 (``SidecarExtractor`` →
``SidecarExtraction``). The extractor stops being the *backup* and becomes the
*primary*; the per-field catch-loops stay as the loud net (ADR-150 §E, Decision).

Design — mirrors the established sibling cutover (Story 151-3, ``action_rewrite``):
a cutover *retires* the field at the game_patch parse boundary and *sources* it
from the new producer, leaving the apply machinery untouched. 151-3 sourced
``action_rewrite`` from the PRE-narrator IntentRouter, so it merged inside the
result assembler (``orchestrator._build_shared_result_kwargs``). The sidecar
extractor is POST-narration, so its output is not available at assembly time —
the merge happens *after* the extractor runs, *before* ``narration_apply``.

RED-phase interface pins (resolved by TEA; rationale in ``.session/151-4-session.md``).
The story context fixed the seven field names, the extractor-as-source contract,
the retirement guard, the kept catch-loops, and the preserved attribution; TEA
pins the cutover seam:

  * ``orchestrator.extract_structured_from_response`` no longer surfaces the seven
    transactional fields out of the game_patch (the retirement; the exact shape
    151-3 used for ``action_rewrite`` at the same function). ``npcs_present`` /
    ``scene_mood`` / ``visual_scene`` / ``footnotes`` STAY — they are 151-5.
  * ``output_only.md`` no longer instructs the narrator to emit the seven fields.
  * NEW seam ``narration_apply.merge_sidecar_extraction_transactional(result,
    extraction)`` copies the seven transactional fields from a ``SidecarExtraction``
    onto a ``NarrationTurnResult`` (the extraction is the SOLE source — No Silent
    Fallbacks: no fall-back to ``result``'s own transactional fields). It is the
    seam the WS handler calls between the (relocated, pre-apply) extractor and
    ``_apply_narration_result_to_snapshot``. The apply itself is UNCHANGED, so its
    existing tests (``test_gold_change_apply`` / ``test_companion_recruit_apply`` /
    ``test_mp_item_recipient_attribution``) stay valid — they exercise the apply
    mechanics, which the cutover reuses.

Fixture-based only (epic-151 discipline; project memory
``feedback_no_content_coupled_tests``) — synthetic ``SidecarExtraction`` /
``NarrationTurnResult`` / game_patch fixtures drive the REAL functions; we assert
behaviour + OTEL spans, never a source-text grep of production code (CLAUDE.md
*No Source-Text Wiring Tests*). The ``output_only.md`` assertion is on the CONTRACT
ARTIFACT (the prompt template that IS this AC's deliverable), the same blessed
exception 151-3 used.

Project-rule coverage (CLAUDE.md / SOUL / python.md):
- "Every Test Suite Needs a Wiring Test" + "No Source-Text Wiring Tests" —
  ``test_merge_seam_wired_into_session_handler`` uses reflection on the handler
  module namespace (``__dict__``), never a source grep.
- "No Silent Fallbacks" / python.md #1 — ``test_merge_overwrites_stale_result_fields``
  proves the extraction is the sole source (no result fall-back).
- "OTEL Observability" / python.md #4 — the loud-net catch-loops still fire from
  the extraction-sourced path (``inventory.narrator_extracted`` /
  ``party.recruit_duplicate``).
- python.md #6 (test quality) — every test asserts a specific value, never a bare
  truthy / vacuous check.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from sidequest.agents.orchestrator import NarrationTurnResult, extract_structured_from_response
from sidequest.agents.sidecar_extractor import BUCKET_B_FIELDS, SidecarExtraction
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.session import Companion, GameSnapshot
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from tests._helpers.session_room import room_for

# ---------------------------------------------------------------------------
# Contract data — the seven TRANSACTIONAL bucket-B fields cut over by 151-4.
# (151-5 owns the remaining four: npcs_present, scene_mood, visual_scene,
# footnotes.) Kept as one source of truth so the retirement + merge tests
# cannot drift apart.
# ---------------------------------------------------------------------------

TRANSACTIONAL_FIELDS: tuple[str, ...] = (
    "items_gained",
    "items_lost",
    "items_discarded",
    "items_consumed",
    "gold_change",
    "companions_added",
    "companions_dismissed",
)

# The bucket-B fields that DEFER to 151-5 — they must survive 151-4 untouched.
DEFERRED_FIELDS: tuple[str, ...] = (
    "npcs_present",
    "scene_mood",
    "visual_scene",
    "footnotes",
)


# ---------------------------------------------------------------------------
# Harness — mirrors tests/server/test_gold_change_apply.py and
# test_companion_recruit_apply.py (the apply tests for the very fields cut
# over here) so the cutover path is exercised the same way.
# ---------------------------------------------------------------------------


def _core(name: str, *, gold: int = 0) -> CreatureCore:
    inv = Inventory()
    inv.gold = gold
    return CreatureCore(name=name, description="X.", personality="Y.", inventory=inv)


def _pc(name: str, *, gold: int = 0) -> Character:
    return Character(
        core=_core(name, gold=gold),
        backstory="A wanderer.",
        char_class="adventurer",
        race="human",
    )


class _RecordingHub:
    """Drop-in for the watcher hub that records every ``_watcher_publish``."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any], str]] = []

    def __call__(self, event_type: str, payload: dict[str, Any], *, component: str) -> None:
        self.events.append((event_type, payload, component))


def _patch_watcher(monkeypatch: pytest.MonkeyPatch) -> _RecordingHub:
    hub = _RecordingHub()
    monkeypatch.setattr("sidequest.server.narration_apply._watcher_publish", hub)
    return hub


@pytest.fixture
def otel_capture() -> Iterator[Any]:
    """In-memory OTEL exporter (the canonical fixture; the item catch-loop fires
    an ``inventory.narrator_extracted`` span, not a direct ``_watcher_publish``)."""
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


def _game_patch_raw(patch: dict[str, Any]) -> str:
    """A raw narrator response carrying a ```game_patch``` fence (the shape
    ``extract_structured_from_response`` parses)."""
    return "You act. The world responds.\n\n```game_patch\n" + json.dumps(patch) + "\n```"


def _full_transactional_patch() -> dict[str, Any]:
    """A game_patch populated on every transactional field PLUS two deferred
    fields, so a retirement test can assert the seven drop and the deferred stay."""
    return {
        "items_gained": [{"name": "silver ring", "recipient": "Carl"}],
        "items_lost": [{"name": "old key", "recipient": "Carl"}],
        "items_discarded": [{"name": "torch", "recipient": "Carl"}],
        "items_consumed": [{"name": "ration", "recipient": "Carl"}],
        "gold_change": -19,
        "companions_added": [{"name": "Donut", "role": "torchbearer", "recruited_by": "Carl"}],
        "companions_dismissed": ["Ghost"],
        # deferred (151-5) — must survive untouched:
        "npcs_present": [{"name": "Harlan"}],
        "scene_mood": "tense",
    }


# ===========================================================================
# Scope lock — the seven transactional fields are a subset of bucket-B.
# ===========================================================================


def test_transactional_fields_are_a_subset_of_bucket_b() -> None:
    """The seven fields 151-4 cuts over are exactly the transactional members of
    the canonical ``BUCKET_B_FIELDS`` set — guards against a field-name typo that
    would silently un-test a lane (the extractor and the cutover share one
    vocabulary, ADR-150)."""
    assert set(TRANSACTIONAL_FIELDS).issubset(set(BUCKET_B_FIELDS))
    assert set(TRANSACTIONAL_FIELDS).isdisjoint(set(DEFERRED_FIELDS))


# ===========================================================================
# AC3 — Retired: the game_patch parse no longer surfaces the seven fields.
#
# The behavioural retirement guard ADR-150 §Testing strategy requires:
# "narration_apply no longer reads the migrated field from the game_patch
# sidecar once its cutover lands." The field never reaches the result from the
# game_patch — even when a (non-compliant) narrator still emits it.
# ===========================================================================


@pytest.mark.parametrize("field", TRANSACTIONAL_FIELDS)
def test_extract_structured_retires_transactional_field_from_game_patch(field: str) -> None:
    """A game_patch carrying each transactional field is parsed, but the field is
    NOT surfaced onto the structured result — it now belongs to the post-narration
    extractor (the exact retirement 151-3 applied to ``action_rewrite`` at this
    same function). RED until the cutover stops surfacing it here."""
    parsed = extract_structured_from_response(_game_patch_raw(_full_transactional_patch()))

    surfaced = parsed.get(field)
    # gold_change is a scalar (int|None); the rest are lists. "Retired" means
    # the narrator's game_patch value did not reach the result: None / empty.
    assert not surfaced, (
        f"{field!r} from the game_patch sidecar must NOT be surfaced onto the "
        f"narration result after 151-4 — it is sourced from the post-narration "
        f"sidecar extractor now (ADR-150 step 4); got {surfaced!r}"
    )


def test_extract_structured_keeps_deferred_bucket_b_fields() -> None:
    """Scope guard: 151-4 retires ONLY the seven transactional fields. The four
    deferred bucket-B fields (``npcs_present`` enrichment, ``scene_mood``,
    ``visual_scene``, ``footnotes``) belong to 151-5 and MUST still flow from the
    game_patch — over-retiring them here would silently break the un-migrated
    lanes."""
    parsed = extract_structured_from_response(_game_patch_raw(_full_transactional_patch()))

    assert parsed.get("npcs_present") == [{"name": "Harlan"}], (
        "npcs_present is 151-5 — it must still flow from the game_patch in 151-4"
    )
    assert parsed.get("scene_mood") == "tense", (
        "scene_mood is 151-5 — it must still flow from the game_patch in 151-4"
    )


def test_output_only_md_no_longer_instructs_transactional_fields() -> None:
    """``output_only.md`` no longer teaches the narrator to emit the seven
    transactional fields — they are extracted post-narration now. Asserts on the
    CONTRACT ARTIFACT (the prompt template that IS this AC's deliverable), not a
    wiring grep of production source. Plain substring, no regex (no catastrophic
    backtracking, per CLAUDE.md). RED until the items/gold/companions block is cut."""
    from pathlib import Path

    import sidequest.agents as agents_pkg

    output_only = (
        Path(agents_pkg.__file__).parent / "narrator_prompts" / "output_only.md"
    ).read_text(encoding="utf-8")

    for field in TRANSACTIONAL_FIELDS:
        assert field not in output_only, (
            f"output_only.md still instructs the narrator to emit {field!r}; "
            f"ADR-150 step 4 retires it from PART 2 (extracted post-narration now)"
        )


def test_output_only_md_keeps_deferred_field_instructions() -> None:
    """Scope guard on the artifact side: the deferred bucket-B fields (151-5) are
    still taught in ``output_only.md`` after 151-4 — the narrator must keep emitting
    them until their own cutover lands."""
    from pathlib import Path

    import sidequest.agents as agents_pkg

    output_only = (
        Path(agents_pkg.__file__).parent / "narrator_prompts" / "output_only.md"
    ).read_text(encoding="utf-8")

    assert "npcs_present" in output_only, (
        "npcs_present is 151-5 — output_only.md must still instruct it in 151-4"
    )


# ===========================================================================
# AC2 — Applied from extractor: the merge seam sources the result from the
# SidecarExtraction (the apply itself is unchanged).
# ===========================================================================


def test_merge_copies_all_seven_transactional_fields_from_extraction() -> None:
    """The cutover seam copies every transactional field from the post-narration
    ``SidecarExtraction`` onto the ``NarrationTurnResult``. RED until the seam
    exists."""
    from sidequest.server.narration_apply import merge_sidecar_extraction_transactional

    result = NarrationTurnResult(narration="prose")  # post-retirement: all empty
    extraction = SidecarExtraction(
        items_gained=[{"name": "silver ring", "recipient": "Carl"}],
        items_lost=[{"name": "old key", "recipient": "Carl"}],
        items_discarded=[{"name": "torch", "recipient": "Carl"}],
        items_consumed=[{"name": "ration", "recipient": "Carl"}],
        gold_change=-19,
        companions_added=[{"name": "Donut", "role": "torchbearer", "recruited_by": "Carl"}],
        companions_dismissed=["Ghost"],
    )

    merge_sidecar_extraction_transactional(result, extraction)

    assert result.items_gained == [{"name": "silver ring", "recipient": "Carl"}]
    assert result.items_lost == [{"name": "old key", "recipient": "Carl"}]
    assert result.items_discarded == [{"name": "torch", "recipient": "Carl"}]
    assert result.items_consumed == [{"name": "ration", "recipient": "Carl"}]
    assert result.gold_change == -19
    assert result.companions_added == [
        {"name": "Donut", "role": "torchbearer", "recruited_by": "Carl"}
    ]
    assert result.companions_dismissed == ["Ghost"]


def test_merge_overwrites_stale_result_fields_no_fallback() -> None:
    """No Silent Fallbacks / ADR-150 'one mechanism, not both producers in
    parallel': the extraction is the SOLE source. An empty extraction field
    OVERWRITES a stale game_patch value on the result — it does not fall back to
    it. RED until the seam exists."""
    from sidequest.server.narration_apply import merge_sidecar_extraction_transactional

    # A result still carrying stale transactional values (e.g. a non-compliant
    # narrator that slipped a game_patch field past the retired contract).
    result = NarrationTurnResult(
        narration="prose",
        items_gained=[{"name": "STALE phantom blade"}],
        gold_change=999,
        companions_added=[{"name": "STALE ghost"}],
    )
    extraction = SidecarExtraction()  # extractor read nothing transactional

    merge_sidecar_extraction_transactional(result, extraction)

    assert result.items_gained == [], "stale game_patch item must be overwritten, not kept"
    assert result.gold_change is None, "stale game_patch gold must be overwritten, not kept"
    assert result.companions_added == [], "stale game_patch companion must be overwritten"


def test_extraction_gold_change_applied_via_merge_then_apply(monkeypatch) -> None:
    """End-to-end (AC2, gold lane): an extraction-sourced ``gold_change`` flows
    merge → apply → the acting PC's purse, firing the existing
    ``economy.gold_change`` watcher. The narrator's game_patch carried nothing."""
    from sidequest.server.narration_apply import merge_sidecar_extraction_transactional

    hub = _patch_watcher(monkeypatch)
    snap = GameSnapshot(characters=[_pc("Carl", gold=50)])
    snap.turn_manager.interaction = 5

    result = NarrationTurnResult(narration="Nineteen silver buys all three.")
    merge_sidecar_extraction_transactional(result, SidecarExtraction(gold_change=-19))

    _apply_narration_result_to_snapshot(
        snap,
        result,
        player_name="Carl",
        room=room_for(snap),
        pack=None,
        acting_character_name="Carl",
    )

    assert snap.characters[0].core.inventory.gold == 31
    gold_events = [e for e in hub.events if e[1].get("kind") == "economy.gold_change"]
    assert len(gold_events) == 1
    assert gold_events[0][1]["applied_delta"] == -19


def test_extraction_items_gained_applied_via_merge_then_apply() -> None:
    """End-to-end (AC2, items lane): an extraction-sourced ``items_gained`` flows
    merge → apply → the PC's inventory."""
    from sidequest.server.narration_apply import merge_sidecar_extraction_transactional

    snap = GameSnapshot(characters=[_pc("Carl")])
    snap.turn_manager.record_interaction()

    result = NarrationTurnResult(narration="You pocket a silver ring.")
    merge_sidecar_extraction_transactional(
        result, SidecarExtraction(items_gained=[{"name": "Silver Ring", "category": "treasure"}])
    )

    _apply_narration_result_to_snapshot(
        snap, result, player_name="Carl", room=room_for(snap), acting_character_name="Carl"
    )

    names = [str(it.get("name", "")) for it in snap.characters[0].core.inventory.items]
    assert "Silver Ring" in names


def test_extraction_companions_added_applied_via_merge_then_apply(monkeypatch) -> None:
    """End-to-end (AC2, companions lane): an extraction-sourced ``companions_added``
    flows merge → apply → ``snapshot.companions``, firing the existing
    ``party.recruit`` watcher."""
    from sidequest.server.narration_apply import merge_sidecar_extraction_transactional

    hub = _patch_watcher(monkeypatch)
    snap = GameSnapshot(characters=[_pc("Carl")])
    snap.turn_manager.interaction = 4

    result = NarrationTurnResult(narration="Donut falls in beside you.")
    merge_sidecar_extraction_transactional(
        result,
        SidecarExtraction(
            companions_added=[{"name": "Donut", "role": "torchbearer", "recruited_by": "Carl"}]
        ),
    )

    _apply_narration_result_to_snapshot(
        snap,
        result,
        player_name="Carl",
        room=room_for(snap),
        pack=None,
        acting_character_name="Carl",
    )

    assert [c.name for c in snap.companions] == ["Donut"]
    recruit_events = [e for e in hub.events if e[1].get("kind") == "party.recruit"]
    assert len(recruit_events) == 1
    assert recruit_events[0][1]["name"] == "Donut"


# ===========================================================================
# AC5 — Attribution preserved: recipient / recruited_by survive the merge,
# including multi-recipient splits.
# ===========================================================================


def _two_seat_snapshot() -> GameSnapshot:
    snap = GameSnapshot(
        genre_slug="space_opera",
        world_slug="coyote_star",
        characters=[_pc("Ritali Veer"), _pc("Catalina Valentine")],
    )
    snap.player_seats = {"p1": "Ritali Veer", "p2": "Catalina Valentine"}
    snap.turn_manager.record_interaction()
    return snap


def test_merge_preserves_item_recipient_attribution() -> None:
    """AC5: the per-item ``recipient`` tag survives the merge → apply path and the
    item lands on the named seated PC, never ``characters[0]`` (ADR-108)."""
    from sidequest.server.narration_apply import merge_sidecar_extraction_transactional

    snap = _two_seat_snapshot()
    result = NarrationTurnResult(narration="The chip lands in Catalina's palm.")
    merge_sidecar_extraction_transactional(
        result,
        SidecarExtraction(
            items_gained=[
                {"name": "Station Map Chip", "category": "quest", "recipient": "Catalina Valentine"}
            ]
        ),
    )

    _apply_narration_result_to_snapshot(
        snap, result, player_name="p1", room=room_for(snap), acting_character_name="Ritali Veer"
    )

    ritali, catalina = snap.characters
    cat_names = [str(it.get("name", "")) for it in catalina.core.inventory.items]
    rit_names = [str(it.get("name", "")) for it in ritali.core.inventory.items]
    assert "Station Map Chip" in cat_names
    assert "Station Map Chip" not in rit_names, (
        "recipient attribution lost — landed on characters[0]"
    )


def test_merge_preserves_multi_recipient_item_split() -> None:
    """AC5: the narrator contract splits a multi-recipient hand-off into one entry
    per recipient; the merge copies the per-recipient dicts verbatim, so each item
    lands on its own seated PC."""
    from sidequest.server.narration_apply import merge_sidecar_extraction_transactional

    snap = _two_seat_snapshot()
    result = NarrationTurnResult(narration="One charm each.")
    merge_sidecar_extraction_transactional(
        result,
        SidecarExtraction(
            items_gained=[
                {"name": "Red Charm", "category": "treasure", "recipient": "Ritali Veer"},
                {"name": "Blue Charm", "category": "treasure", "recipient": "Catalina Valentine"},
            ]
        ),
    )

    _apply_narration_result_to_snapshot(
        snap, result, player_name="p1", room=room_for(snap), acting_character_name="Ritali Veer"
    )

    ritali, catalina = snap.characters
    rit_names = [str(it.get("name", "")) for it in ritali.core.inventory.items]
    cat_names = [str(it.get("name", "")) for it in catalina.core.inventory.items]
    assert rit_names == ["Red Charm"]
    assert cat_names == ["Blue Charm"]


def test_merge_preserves_companion_recruited_by_attribution() -> None:
    """AC5: the ``recruited_by`` bond tag survives the merge → apply path onto the
    new ``Companion`` (ADR-125 bond ledger reads it)."""
    from sidequest.server.narration_apply import merge_sidecar_extraction_transactional

    snap = GameSnapshot(characters=[_pc("Carl")])
    snap.turn_manager.record_interaction()

    result = NarrationTurnResult(narration="Donut swears to Carl.")
    merge_sidecar_extraction_transactional(
        result,
        SidecarExtraction(
            companions_added=[{"name": "Donut", "role": "torchbearer", "recruited_by": "Carl"}]
        ),
    )

    _apply_narration_result_to_snapshot(
        snap, result, player_name="Carl", room=room_for(snap), acting_character_name="Carl"
    )

    assert snap.companions[0].recruited_by == "Carl"


# ===========================================================================
# AC4 — Loud net intact: the catch-loops still fire from the extraction-sourced
# path (the catch-loops are the kept safety net, ADR-150 Decision §E).
# ===========================================================================


def test_extraction_unmatched_discard_fires_catch_loop_span(otel_capture) -> None:
    """AC4 (items): an extraction-sourced ``items_discarded`` for an item the PC
    does not hold still surfaces the loud ``inventory.narrator_extracted`` span
    with ``unmatched_discards_count >= 1`` — the catch-loop fires from the new
    source, never a silent miss."""
    from sidequest.server.narration_apply import merge_sidecar_extraction_transactional

    snap = GameSnapshot(characters=[_pc("Carl")])  # empty inventory
    snap.turn_manager.record_interaction()

    result = NarrationTurnResult(narration="Carl drops a torch he never had.")
    merge_sidecar_extraction_transactional(
        result, SidecarExtraction(items_discarded=[{"name": "Phantom Torch", "recipient": "Carl"}])
    )

    _apply_narration_result_to_snapshot(
        snap, result, player_name="Carl", room=room_for(snap), acting_character_name="Carl"
    )

    spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "inventory.narrator_extracted"
    ]
    assert len(spans) >= 1, (
        "the inventory catch-loop span must fire from the extraction-sourced path"
    )
    assert any(
        int(dict(s.attributes or {}).get("unmatched_discards_count", 0)) >= 1 for s in spans
    ), "an unmatched discard must surface unmatched_discards_count >= 1 (the loud net)"


def test_extraction_duplicate_companion_fires_dedup_catch_loop(monkeypatch) -> None:
    """AC4 (companions): an extraction-sourced ``companions_added`` duplicating an
    existing companion (case-insensitive) still fires the loud
    ``party.recruit_duplicate`` watcher and adds no second roster row."""
    from sidequest.server.narration_apply import merge_sidecar_extraction_transactional

    hub = _patch_watcher(monkeypatch)
    snap = GameSnapshot(
        characters=[_pc("Carl")],
        companions=[Companion(name="Donut", role="torchbearer", recruited_turn=1)],
    )
    snap.turn_manager.record_interaction()

    result = NarrationTurnResult(narration="Donut is already here.")
    merge_sidecar_extraction_transactional(
        result, SidecarExtraction(companions_added=[{"name": "donut", "role": "torchbearer"}])
    )

    _apply_narration_result_to_snapshot(
        snap,
        result,
        player_name="Carl",
        room=room_for(snap),
        pack=None,
        acting_character_name="Carl",
    )

    assert len(snap.companions) == 1, "a duplicate recruit must not add a second roster row"
    dup_events = [e for e in hub.events if e[1].get("kind") == "party.recruit_duplicate"]
    assert len(dup_events) == 1, "the companion dedup catch-loop must fire from the new source"


# ===========================================================================
# AC2 wiring — the merge seam is reached on the live post-narration turn path.
# ===========================================================================


def test_merge_seam_wired_into_session_handler() -> None:
    """Pipeline wiring (reflection, NOT source-grep): the WS turn handler — the
    live post-narration seam that already runs the extractor shadow runner and
    ``_apply_narration_result_to_snapshot`` — must import the merge seam so the
    extraction's transactional fields are applied in production. A missing import
    means the extractor is built but its output never reaches apply (the
    half-wired failure CLAUDE.md forbids)."""
    from sidequest.server import websocket_session_handler as wsh

    assert "merge_sidecar_extraction_transactional" in wsh.__dict__, (
        "websocket_session_handler must import merge_sidecar_extraction_transactional "
        "so the post-narration extractor's transactional fields reach narration_apply "
        "(ADR-150 step 4 cutover)"
    )
