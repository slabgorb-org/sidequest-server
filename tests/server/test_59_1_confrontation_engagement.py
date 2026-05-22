"""Story 59-1 RED — confrontation ENGAGEMENT apply path + no-emission watcher.

See sprint/context/context-story-59-1.md (Architecture Decision). Engagement =
the narrator setting result.confrontation, consumed at narration_apply.py:2531
to create the StructuredEncounter. These tests use synthetic fixtures only —
never a live genre_packs/* pack (project rule).

AC1 (consumer) is a regression ANCHOR: the field->encounter substrate already
works; we pin it so a fix to the WRITER side can't silently break the consumer.
AC5 (no-emission watcher) and AC6 (end-to-end) drive the corrected behavior.
"""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import MagicMock

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.agents.orchestrator import NarrationTurnResult, NpcMention
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import (
    BeatDef,
    ConfrontationDef,
    MetricDef,
    RulesConfig,
)
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from tests._helpers.session_room import room_for

# Proposed watcher span for AC5. Dev may rename; if so, update this constant
# and log a Design Deviation. The BEHAVIOR (fires on a confrontation-shaped,
# unengaged, no-intent turn; silent on an engaged turn) is the contract.
_UNENGAGED_SPAN = "confrontation.unengaged_turn"


def _negotiation_pack() -> GenrePack:
    """Synthetic social pack with a single ``negotiation`` ConfrontationDef.
    Mirrors tests/server/conftest.py::synthetic_two_dial_pack but social-typed.
    """
    cdef = ConfrontationDef(
        type="negotiation",
        label="Negotiation",
        category="social",
        player_metric=MetricDef(name="leverage", starting=0, threshold=10),
        opponent_metric=MetricDef(name="leverage", starting=0, threshold=10),
        beats=[
            BeatDef.model_validate(
                {
                    "id": "press",
                    "label": "Press the Point",
                    "kind": "strike",
                    "base": 1,
                    "stat_check": "CHA",
                }
            )
        ],
    )
    pack = MagicMock(spec=GenrePack)
    pack.rules = RulesConfig(confrontations=[cdef])
    return pack


def _snapshot() -> GameSnapshot:
    return GameSnapshot(
        genre_slug="test_pack",
        world_slug="test_world",
        turn_manager=TurnManager(),
    )


@pytest.fixture
def otel_capture() -> Generator[InMemorySpanExporter, None, None]:
    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider), (
        f"expected SDK TracerProvider, got {type(provider)!r}"
    )
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


# ---------------------------------------------------------------------------
# AC1 — consumer: result.confrontation -> StructuredEncounter (regression anchor)
# ---------------------------------------------------------------------------


def test_confrontation_field_creates_structured_encounter() -> None:
    """AC1 (anchor): setting result.confrontation to a social type, with no
    active encounter, makes the apply step create a StructuredEncounter of that
    type. This is the consumer side and should PASS today — it pins the substrate
    a working SDK writer must feed.
    """
    snap = _snapshot()
    pack = _negotiation_pack()
    snap.character_locations["Neil"] = "The Bridge"
    result = NarrationTurnResult(
        narration="Neil steps into the solicitor's path and names his terms.",
        confrontation="negotiation",
        npcs_present=[
            NpcMention(name="Neil", role="investigator", side="player"),
            NpcMention(name="Solicitor Ewan Forbes", role="opposition", side="opponent"),
        ],
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        player_name="Neil",
        room=room_for(snapshot=snap),
    )
    assert snap.encounter is not None, (
        "result.confrontation='negotiation' must create a StructuredEncounter "
        "(narration_apply.py:2531). If None, the consumer regressed."
    )
    assert snap.encounter.encounter_type == "negotiation"
    assert snap.encounter.resolved is False


def test_no_confrontation_field_creates_no_encounter() -> None:
    """AC1 (negative): a plain turn with confrontation=None must not create an
    encounter — guards against spurious engagement.
    """
    snap = _snapshot()
    pack = _negotiation_pack()
    snap.character_locations["Neil"] = "The Bridge"
    result = NarrationTurnResult(
        narration="Neil thanks the publican and steps back into the rain.",
        confrontation=None,
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        player_name="Neil",
        room=room_for(snapshot=snap),
    )
    assert snap.encounter is None


# ---------------------------------------------------------------------------
# AC6 — end-to-end engagement regression (fixture-based)
# ---------------------------------------------------------------------------


def test_social_engagement_end_to_end_non_none_state() -> None:
    """AC6: drive the engagement path for a social type and assert non-None
    confrontation state with the correct type and unresolved status — the exact
    regression the playtest hit (confrontation=None every turn).
    """
    snap = _snapshot()
    pack = _negotiation_pack()
    snap.character_locations["Neil"] = "The Bridge"
    result = NarrationTurnResult(
        narration="'Name your client, Mr Forbes, or I'll have the Sergeant ask you.'",
        confrontation="negotiation",
        npcs_present=[
            NpcMention(name="Neil", role="investigator", side="player"),
            NpcMention(name="Solicitor Ewan Forbes", role="opposition", side="opponent"),
        ],
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        player_name="Neil",
        room=room_for(snapshot=snap),
    )
    assert snap.encounter is not None and snap.encounter.encounter_type == "negotiation"
    assert snap.encounter.resolved is False


# ---------------------------------------------------------------------------
# AC5 — non-keyword lie-detector covers the NO-EMISSION case
# ---------------------------------------------------------------------------


def test_unengaged_confrontation_turn_emits_watcher_span(
    otel_capture: InMemorySpanExporter,
) -> None:
    """AC5: a confrontation-shaped turn that engages NOTHING — no confrontation
    field, no beats, and no structured intent — must emit a non-keyword watcher
    span so the GM panel sees the miss. The existing confrontation_intent_validator
    only fires when an action_rewrite intent is present to tokenize; the playtest
    failure was the narrator emitting nothing, which slips through silently.

    FAILS today: no such span fires on a no-emission turn. Proposed span name is
    ``confrontation.unengaged_turn`` (Dev may rename — update _UNENGAGED_SPAN +
    log a Design Deviation; the behavior is the contract). Must NOT be implemented
    by resurrecting prose keyword-scanning (_CONFRONTATION_TRIGGER_PATTERNS).
    """
    snap = _snapshot()
    pack = _negotiation_pack()
    snap.character_locations["Neil"] = "The Bridge"
    # Confrontation-shaped: the narrator NAMED AN OPPONENT (the structural
    # confrontation-shape signal) but emitted NO confrontation field, NO beats,
    # and NO structured intent — the exact silent-miss case.
    result = NarrationTurnResult(
        narration=(
            "Neil blocks the young man's path and calls the bluff; the solicitor "
            "makes no move to go around him, but answers nothing further."
        ),
        confrontation=None,
        npcs_present=[
            NpcMention(name="Neil", role="investigator", side="player"),
            NpcMention(name="Solicitor Ewan Forbes", role="opposition", side="opponent"),
        ],
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        player_name="Neil",
        room=room_for(snapshot=snap),
    )
    names = [s.name for s in otel_capture.get_finished_spans()]
    assert _UNENGAGED_SPAN in names, (
        f"Expected a non-keyword watcher span {_UNENGAGED_SPAN!r} on a "
        f"confrontation-shaped (opponent named), unengaged, no-intent turn. "
        f"Finished spans: {names!r}"
    )


def test_quiet_turn_does_not_emit_unengaged_watcher_span(
    otel_capture: InMemorySpanExporter,
) -> None:
    """AC5 (precision, no false-positive storm): a plain narrative turn with no
    opponent actor, no confrontation, and no intent must NOT fire the watcher.
    The structural confrontation-shape signal is an opponent-side NPC; an
    ordinary travel/dialogue/rest turn has none. Guards against the watcher
    firing on every quiet turn."""
    snap = _snapshot()
    pack = _negotiation_pack()
    snap.character_locations["Neil"] = "The Bridge"
    result = NarrationTurnResult(
        narration="Neil thanks the publican and steps back into the rain.",
        confrontation=None,
        npcs_present=[
            NpcMention(name="The Publican", role="bystander", side="neutral"),
        ],
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        player_name="Neil",
        room=room_for(snapshot=snap),
    )
    names = [s.name for s in otel_capture.get_finished_spans()]
    assert _UNENGAGED_SPAN not in names, (
        f"{_UNENGAGED_SPAN!r} fired on a quiet, opponent-free turn (false-positive "
        f"storm). Finished spans: {names!r}"
    )


def test_reprompt_reapply_does_not_double_emit_unengaged_span(
    otel_capture: InMemorySpanExporter,
) -> None:
    """AC5 (C2): the reprompt-loop second apply (already_reprompted=True) must
    NOT re-emit the watcher for the same logical player turn — even on a
    confrontation-shaped, unengaged, no-intent result."""
    snap = _snapshot()
    pack = _negotiation_pack()
    snap.character_locations["Neil"] = "The Bridge"
    result = NarrationTurnResult(
        narration="Neil blocks the path again; the solicitor still says nothing.",
        confrontation=None,
        npcs_present=[
            NpcMention(name="Neil", role="investigator", side="player"),
            NpcMention(name="Solicitor Ewan Forbes", role="opposition", side="opponent"),
        ],
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        player_name="Neil",
        room=room_for(snapshot=snap),
        already_reprompted=True,
    )
    names = [s.name for s in otel_capture.get_finished_spans()]
    assert _UNENGAGED_SPAN not in names, (
        f"{_UNENGAGED_SPAN!r} fired on a reprompt re-apply (double-emit for one "
        f"turn). Finished spans: {names!r}"
    )


def test_engaged_turn_does_not_emit_unengaged_watcher_span(
    otel_capture: InMemorySpanExporter,
) -> None:
    """AC5 (no false positive): when the turn properly engages (confrontation
    field set -> encounter created), the unengaged watcher must NOT fire.
    """
    snap = _snapshot()
    pack = _negotiation_pack()
    snap.character_locations["Neil"] = "The Bridge"
    result = NarrationTurnResult(
        narration="Neil names his terms; the negotiation is joined.",
        confrontation="negotiation",
        npcs_present=[
            NpcMention(name="Neil", role="investigator", side="player"),
            NpcMention(name="Solicitor Ewan Forbes", role="opposition", side="opponent"),
        ],
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        player_name="Neil",
        room=room_for(snapshot=snap),
    )
    names = [s.name for s in otel_capture.get_finished_spans()]
    assert _UNENGAGED_SPAN not in names, (
        f"{_UNENGAGED_SPAN!r} fired on a properly-engaged turn (false positive). "
        f"Finished spans: {names!r}"
    )


# ---------------------------------------------------------------------------
# C1 (rework) — clobber-free round-trip: narration_apply creates the encounter
# on the CANONICAL snapshot, and persisting that canonical (what room.save does)
# keeps it. This is the path begin_confrontation routes engagement through,
# instead of a tool ctx.store write that room.save would clobber.
# ---------------------------------------------------------------------------


def test_confrontation_engagement_survives_canonical_persist() -> None:
    """Setting result.confrontation drives narration_apply to create the
    encounter on the in-place canonical snapshot; saving THAT canonical (the
    object room.save persists) keeps the encounter. Proves engagement is not
    clobbered — the failure mode that a tool-only ctx.store write would hit."""
    from sidequest.game.persistence import SqliteStore

    canonical = _snapshot()
    canonical.character_locations["Neil"] = "The Bridge"
    pack = _negotiation_pack()

    store = SqliteStore.open_in_memory()
    store.initialize()
    store.init_session(genre_slug=canonical.genre_slug, world_slug=canonical.world_slug)
    store.save(canonical)  # disk == canonical at turn start

    result = NarrationTurnResult(
        narration="'Name your client, Mr Forbes.'",
        confrontation="negotiation",
        npcs_present=[
            NpcMention(name="Neil", role="investigator", side="player"),
            NpcMention(name="Solicitor Ewan Forbes", role="opposition", side="opponent"),
        ],
    )
    # narration_apply mutates the CANONICAL snapshot in place (this is what the
    # session handler passes; room.save persists this same object).
    _apply_narration_result_to_snapshot(
        snapshot=canonical,
        result=result,
        pack=pack,
        player_name="Neil",
        room=room_for(snapshot=canonical),
    )
    assert canonical.encounter is not None
    assert canonical.encounter.encounter_type == "negotiation"

    # End-of-turn persistence (room.save -> store.save(canonical)) keeps it.
    store.save(canonical)
    reloaded = store.load()
    assert reloaded is not None and reloaded.snapshot.encounter is not None, (
        "the engaged encounter must survive persisting the canonical snapshot; "
        "if None, the engagement was clobbered (the tool-store-write failure mode)."
    )
    assert reloaded.snapshot.encounter.encounter_type == "negotiation"
