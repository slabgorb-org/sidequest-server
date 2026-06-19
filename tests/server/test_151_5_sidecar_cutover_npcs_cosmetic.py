"""RED tests for Story 151-5 — Sidecar cutover II: npcs_present (engine-owned side)
+ async cosmetic fields (ADR-150 step 4, the LAST bucket-B field group).

151-4 cut over the seven *transactional* fields. 151-5 cuts over the remaining
four bucket-B fields, completing the bucket-B migration:

  * ``npcs_present`` — its descriptive ENRICHMENT (``pronouns`` / ``role`` /
    ``appearance``) is sourced from the post-narration extractor, but the
    load-bearing combatant-membership ``side`` is **engine-owned**: it comes from
    the confrontation the IntentRouter already seated pre-narrator
    (``snapshot.encounter.actors``), NOT from the extractor's prose-read. ADR-150
    §Decision: "The extractor enriches; the engine adjudicates membership." This
    removes the "wrong side breaks momentum routing" bug class at its source.
  * ``scene_mood`` / ``visual_scene`` / ``footnotes`` — purely cosmetic / feed
    fields (ADR-150 §Ordering: async, off the critical path). Sourced verbatim
    from the extractor; no engine ownership.

Design — mirrors the established sibling cutover (151-4, transactional): a cutover
*retires* the field at the game_patch parse boundary
(``orchestrator.extract_structured_from_response``), stops instructing it in
``output_only.md``, and *sources* it from the post-narration extractor via a NEW
merge seam called between the (relocated, pre-apply) extractor and
``_apply_narration_result_to_snapshot``. The downstream apply machinery
(``_apply_npc_mentions`` / the image-render + journal consumers) is UNCHANGED.

RED-phase interface pins (resolved by TEA; rationale in ``.session/151-5-session.md``).
The story context fixed the field split, the engine-owned ``side`` contract, the
retirement, and the kept private_segments; TEA pins the cutover seams:

  * ``orchestrator.extract_structured_from_response`` no longer surfaces
    ``npcs_present`` / ``visual_scene`` / ``scene_mood`` / ``footnotes`` out of the
    game_patch (the retirement — the exact shape 151-3/151-4 applied at this same
    function). After 151-5 NO bucket-B field is surfaced — the migration is
    complete.
  * ``output_only.md`` no longer instructs the narrator to emit the four fields.
    ``private_segments`` STAYS instructed — it is the one irreducible field
    (ADR-105 firewall); its final prompt shrink is 151-6, not here.
  * NEW seam ``narration_apply.merge_sidecar_extraction_npcs_present(result,
    extraction, snapshot)`` — builds ``result.npcs_present`` (a list of
    ``NpcMention``) from the extractor's enrichment dicts, resolving each mention's
    ``side`` from ``snapshot.encounter`` (engine-owned), and emitting a
    ``sidecar_extraction.mismatch`` span when the extractor's CLAIMED side
    disagrees with the engine (the relocated lie-detector). The extraction is the
    SOLE enrichment source (No Silent Fallbacks — a stale ``result.npcs_present``
    is overwritten).
  * NEW seam ``narration_apply.merge_sidecar_extraction_cosmetic(result,
    extraction)`` — sources ``scene_mood`` / ``visual_scene`` / ``footnotes`` from
    the extraction (``visual_scene`` dict → ``VisualScene`` model). Distinct from
    the npcs seam so the cosmetic fields can be scheduled async (ADR-150
    §Ordering); also SOLE source.
  * Both seams are imported by ``websocket_session_handler`` so the extractor's
    output reaches apply in production (the half-wired failure CLAUDE.md forbids).

Fixture-based only (epic-151 discipline; project memory
``feedback_no_content_coupled_tests``) — synthetic ``SidecarExtraction`` /
``NarrationTurnResult`` / ``StructuredEncounter`` / game_patch fixtures drive the
REAL functions; we assert behaviour + OTEL spans, never a source-text grep of
production code (CLAUDE.md *No Source-Text Wiring Tests*). The ``output_only.md``
assertion is on the CONTRACT ARTIFACT (the prompt template that IS this AC's
deliverable), the same blessed exception 151-3/151-4 used.

Project-rule coverage (CLAUDE.md / SOUL / python.md):
- "Every Test Suite Needs a Wiring Test" + "No Source-Text Wiring Tests" —
  ``test_merge_seams_wired_into_session_handler`` uses reflection on the handler
  module namespace (``__dict__``), never a source grep.
- "No Silent Fallbacks" / python.md #1 — ``test_merge_npcs_present_overwrites_stale_no_fallback``
  and ``test_merge_cosmetic_overwrites_stale_no_fallback`` prove the extraction is
  the sole source.
- "OTEL Observability" — ``test_merge_npcs_present_emits_mismatch_span_on_side_override``
  proves the engine-override decision is visible to the GM panel.
- SOUL "Bind the Ruleset" / ADR-150 engine-owned membership —
  ``test_merge_npcs_present_side_from_engine_seated_opponent`` proves the engine,
  not the extractor, adjudicates ``side``.
- python.md #6 (test quality) — every test asserts a specific value, never a bare
  truthy / vacuous check.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from sidequest.agents.orchestrator import (
    NarrationTurnResult,
    NpcMention,
    VisualScene,
    extract_structured_from_response,
)
from sidequest.agents.sidecar_extractor import BUCKET_B_FIELDS, SidecarExtraction
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.session import GameSnapshot

# ---------------------------------------------------------------------------
# Contract data — the four DEFERRED bucket-B fields cut over by 151-5. Together
# with 151-4's seven TRANSACTIONAL fields they are exactly BUCKET_B_FIELDS: after
# 151-5 the bucket-B migration is complete. Kept as one source of truth so the
# retirement + merge tests cannot drift apart.
# ---------------------------------------------------------------------------

DEFERRED_FIELDS: tuple[str, ...] = (
    "npcs_present",
    "scene_mood",
    "visual_scene",
    "footnotes",
)

# 151-4's lane — must NOT regress here (already retired before 151-5).
TRANSACTIONAL_FIELDS: tuple[str, ...] = (
    "items_gained",
    "items_lost",
    "items_discarded",
    "items_consumed",
    "gold_change",
    "companions_added",
    "companions_dismissed",
)


# ---------------------------------------------------------------------------
# Harness — mirrors tests/server/test_151_4_sidecar_cutover_transactional.py so
# the two cutover suites read the same way.
# ---------------------------------------------------------------------------


def _core(name: str) -> CreatureCore:
    return CreatureCore(name=name, description="X.", personality="Y.", inventory=Inventory())


def _pc(name: str) -> Character:
    return Character(
        core=_core(name), backstory="A wanderer.", char_class="adventurer", race="human"
    )


def _snapshot_with_encounter(*, actors: list[EncounterActor]) -> GameSnapshot:
    """A snapshot whose engine-seated confrontation owns the membership ``side``.
    The IntentRouter seats these pre-narrator (ADR-150); the merge reads them."""
    enc = StructuredEncounter(
        encounter_type="duel",
        category="combat",
        player_metric=EncounterMetric(name="p", threshold=10),
        opponent_metric=EncounterMetric(name="o", threshold=10),
        actors=actors,
    )
    return GameSnapshot(characters=[_pc("Carl")], encounter=enc)


@pytest.fixture
def otel_capture() -> Iterator[Any]:
    """In-memory OTEL exporter (the canonical fixture; the merge fires a
    ``sidecar_extraction.mismatch`` span when it overrides a claimed side)."""
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


def _full_bucket_b_patch() -> dict[str, Any]:
    """A game_patch populated on ALL eleven bucket-B fields — drives the
    epic-completion sweep (after 151-5 none of them surface)."""
    return {
        # transactional (151-4, already retired)
        "items_gained": [{"name": "silver ring", "recipient": "Carl"}],
        "items_lost": [{"name": "old key", "recipient": "Carl"}],
        "items_discarded": [{"name": "torch", "recipient": "Carl"}],
        "items_consumed": [{"name": "ration", "recipient": "Carl"}],
        "gold_change": -19,
        "companions_added": [{"name": "Donut", "role": "torchbearer", "recruited_by": "Carl"}],
        "companions_dismissed": ["Ghost"],
        # deferred (151-5)
        "npcs_present": [{"name": "Harlan", "side": "opponent", "role": "thug"}],
        "scene_mood": "tense",
        "visual_scene": {"subject": "a dim cellar", "tier": "scene_illustration", "tags": []},
        "footnotes": [{"summary": "The vault is sealed.", "category": "Place", "is_new": True}],
    }


# ===========================================================================
# Scope lock — the four deferred fields complete the bucket-B cutover.
# ===========================================================================


def test_deferred_fields_complete_bucket_b_cutover() -> None:
    """151-5's four fields are exactly the bucket-B members NOT cut over by 151-4,
    and the two lanes UNION to the whole of ``BUCKET_B_FIELDS`` — after 151-5 the
    migration is complete, with no field left in neither lane (a field-name typo
    that silently un-tests a lane would break this)."""
    assert set(DEFERRED_FIELDS).issubset(set(BUCKET_B_FIELDS))
    assert set(DEFERRED_FIELDS).isdisjoint(set(TRANSACTIONAL_FIELDS))
    assert set(DEFERRED_FIELDS) | set(TRANSACTIONAL_FIELDS) == set(BUCKET_B_FIELDS)


# ===========================================================================
# AC — Retired: the game_patch parse no longer surfaces the four fields.
#
# The behavioural retirement guard (ADR-150 §Testing strategy): "narration_apply
# no longer reads the migrated field from the game_patch sidecar once its cutover
# lands." The field never reaches the result from the game_patch — even when a
# (non-compliant) narrator still emits it.
# ===========================================================================


@pytest.mark.parametrize("field", DEFERRED_FIELDS)
def test_extract_structured_retires_deferred_field_from_game_patch(field: str) -> None:
    """A game_patch carrying each deferred field is parsed, but the field is NOT
    surfaced onto the structured result — it now belongs to the post-narration
    extractor (the exact retirement 151-4 applied to the transactional fields at
    this same function). RED until the cutover stops surfacing it here."""
    parsed = extract_structured_from_response(_game_patch_raw(_full_bucket_b_patch()))

    surfaced = parsed.get(field)
    # visual_scene / scene_mood are scalars (None when retired); npcs_present /
    # footnotes are lists (empty when retired). "Retired" means the narrator's
    # game_patch value did not reach the result.
    assert not surfaced, (
        f"{field!r} from the game_patch sidecar must NOT be surfaced onto the "
        f"narration result after 151-5 — it is sourced from the post-narration "
        f"sidecar extractor now (ADR-150 step 4); got {surfaced!r}"
    )


def test_extract_structured_retires_all_bucket_b_after_151_5() -> None:
    """Epic-completion sweep: with a game_patch populated on ALL eleven bucket-B
    fields, NONE of them surface onto the result after 151-5. The narrator's
    sidecar accounting is fully off the parse boundary — only the extractor
    produces bucket-B now (ADR-150 §Decision)."""
    parsed = extract_structured_from_response(_game_patch_raw(_full_bucket_b_patch()))

    for field in BUCKET_B_FIELDS:
        assert not parsed.get(field), (
            f"{field!r} must be retired from the game_patch after 151-5 — the whole "
            f"of bucket-B is extractor-sourced now; got {parsed.get(field)!r}"
        )


def test_extract_structured_still_surfaces_private_segments() -> None:
    """Scope guard: ``private_segments`` is the one irreducible field (ADR-105
    firewall) — it must STILL flow from the game_patch after 151-5. Over-retiring
    it here would silently reopen the perception-firewall leak (its prompt shrink
    is 151-6, and even then the field stays narrator-owned)."""
    patch = _full_bucket_b_patch()
    patch["private_segments"] = [{"text": "Only Carl sees the trap.", "anchor_pc": "Carl"}]
    parsed = extract_structured_from_response(_game_patch_raw(patch))

    assert parsed.get("private_segments") == [
        {"text": "Only Carl sees the trap.", "anchor_pc": "Carl"}
    ], "private_segments must survive the 151-5 cutover — it is the irreducible field"


# ===========================================================================
# AC — Contract artifact: output_only.md no longer instructs the four fields,
# but keeps the irreducible private_segments brief.
# ===========================================================================


def _output_only_md() -> str:
    from pathlib import Path

    import sidequest.agents as agents_pkg

    return (Path(agents_pkg.__file__).parent / "narrator_prompts" / "output_only.md").read_text(
        encoding="utf-8"
    )


def test_output_only_md_no_longer_instructs_deferred_fields() -> None:
    """``output_only.md`` no longer teaches the narrator to emit ``npcs_present`` /
    ``visual_scene`` / ``footnotes`` or the top-level scene-mood field — they are
    extracted post-narration now. Asserts on the CONTRACT ARTIFACT (the prompt
    template that IS this AC's deliverable), not a wiring grep of production
    source. Plain substring, no regex (no catastrophic backtracking, per
    CLAUDE.md). RED until the four instruction blocks are cut."""
    output_only = _output_only_md()

    for field in ("npcs_present", "visual_scene", "footnotes"):
        assert field not in output_only, (
            f"output_only.md still instructs the narrator to emit {field!r}; "
            f"ADR-150 step 4 retires it from PART 2 (extracted post-narration now)"
        )
    # scene_mood travels as the top-level "mood:" instruction (extract reads
    # patch['scene_mood'] or patch['mood']); its distinctive instruction phrase
    # must be gone, asserted precisely so a stray "mood" inside prose-craft
    # guidance does not false-match.
    assert "scene-mood signal" not in output_only, (
        "output_only.md still instructs the narrator to emit the scene-mood field; "
        "ADR-150 step 4 retires it (extracted post-narration now)"
    )


def test_output_only_md_keeps_private_segments() -> None:
    """Scope guard on the artifact side: ``private_segments`` is still taught in
    ``output_only.md`` after 151-5 — the narrator keeps authoring it (ADR-105
    firewall, the irreducible field). 151-6 trims the rest of the contract; 151-5
    must not over-remove this block."""
    assert "private_segments" in _output_only_md(), (
        "private_segments is the irreducible field — output_only.md must still "
        "instruct it after 151-5 (its shrink is 151-6, and it stays narrator-owned)"
    )


# ===========================================================================
# AC — npcs_present: enrichment from the extractor, ``side`` from the engine.
# ===========================================================================


def test_merge_npcs_present_sources_enrichment_from_extraction() -> None:
    """The cutover seam builds ``result.npcs_present`` from the extractor's
    enrichment dicts — name + pronouns + role + appearance carry through onto the
    ``NpcMention``. RED until the seam exists."""
    from sidequest.server.narration_apply import merge_sidecar_extraction_npcs_present

    result = NarrationTurnResult(narration="prose")  # post-retirement: empty
    extraction = SidecarExtraction(
        npcs_present=[
            {
                "name": "Harlan",
                "pronouns": "he/him",
                "role": "dock foreman",
                "appearance": "a scarred man in oilskins",
            }
        ]
    )
    snapshot = GameSnapshot(characters=[_pc("Carl")])  # no encounter → neutral side

    merge_sidecar_extraction_npcs_present(result, extraction, snapshot)

    assert len(result.npcs_present) == 1
    mention = result.npcs_present[0]
    assert isinstance(mention, NpcMention)
    assert mention.name == "Harlan"
    assert mention.pronouns == "he/him"
    assert mention.role == "dock foreman"
    assert mention.appearance == "a scarred man in oilskins"


def test_merge_npcs_present_side_from_engine_seated_opponent() -> None:
    """ADR-150 engine-owned membership: the merged mention's ``side`` reflects the
    ENGINE's seated actor (``snapshot.encounter.actors``), NOT the extractor's
    prose-read. Here the engine seated 'Grix' as an opponent; even though the
    extractor read 'neutral', the mention is an OPPONENT — the engine adjudicates.
    RED until the seam resolves side from the engine."""
    from sidequest.server.narration_apply import merge_sidecar_extraction_npcs_present

    snapshot = _snapshot_with_encounter(
        actors=[
            EncounterActor(name="Carl", role="lead", side="player"),
            EncounterActor(name="Grix", role="foe", side="opponent"),
        ]
    )
    result = NarrationTurnResult(narration="Grix bars the door.")
    extraction = SidecarExtraction(
        npcs_present=[{"name": "Grix", "side": "neutral", "role": "thug"}]
    )

    merge_sidecar_extraction_npcs_present(result, extraction, snapshot)

    assert result.npcs_present[0].side == "opponent", (
        "side is engine-owned: the engine seated Grix as an opponent, so the "
        "extractor's 'neutral' claim must NOT win (ADR-150 — the engine adjudicates "
        "membership; the extractor only enriches)"
    )
    # enrichment still flows through alongside the engine-owned side.
    assert result.npcs_present[0].role == "thug"


def test_merge_npcs_present_side_neutral_when_engine_unseated() -> None:
    """The converse: the extractor CLAIMS ``side='opponent'`` but the engine seated
    no such actor (no confrontation engaged). The merged mention defaults to the
    engine's view — ``neutral`` — so a prose-only 'opponent' the router never seated
    cannot spoof combatant membership (the "wrong side breaks momentum routing"
    bug ADR-150 closes). RED until side is engine-sourced."""
    from sidequest.server.narration_apply import merge_sidecar_extraction_npcs_present

    snapshot = GameSnapshot(characters=[_pc("Carl")])  # encounter is None
    result = NarrationTurnResult(narration="A stranger watches.")
    extraction = SidecarExtraction(npcs_present=[{"name": "Stranger", "side": "opponent"}])

    merge_sidecar_extraction_npcs_present(result, extraction, snapshot)

    assert result.npcs_present[0].side == "neutral", (
        "the engine seated no opponent, so the extractor's 'opponent' claim must "
        "not survive — membership defaults to the engine's neutral view"
    )


def test_merge_npcs_present_overwrites_stale_no_fallback() -> None:
    """No Silent Fallbacks / ADR-150 'one mechanism, not both producers in
    parallel': the extraction is the SOLE enrichment source. An empty extraction
    OVERWRITES a stale ``result.npcs_present`` (e.g. a non-compliant narrator that
    slipped a game_patch mention past the retired contract) — it does not fall back
    to it. RED until the seam exists."""
    from sidequest.server.narration_apply import merge_sidecar_extraction_npcs_present

    result = NarrationTurnResult(
        narration="prose",
        npcs_present=[NpcMention(name="STALE phantom", side="opponent")],
    )
    extraction = SidecarExtraction()  # extractor read no NPCs
    snapshot = GameSnapshot(characters=[_pc("Carl")])

    merge_sidecar_extraction_npcs_present(result, extraction, snapshot)

    assert result.npcs_present == [], (
        "a stale game_patch npcs_present must be OVERWRITTEN by the empty "
        "extraction, never kept as a silent fall-back"
    )


def test_merge_npcs_present_emits_mismatch_span_on_side_override(otel_capture) -> None:
    """OTEL discipline (CLAUDE.md): the engine-override DECISION is visible to the
    GM panel. When the extractor's claimed ``side`` disagrees with the engine's
    resolved side, the merge fires a ``sidecar_extraction.mismatch`` span for
    ``npcs_present`` — the relocated lie-detector. RED until the seam emits it."""
    from sidequest.server.narration_apply import merge_sidecar_extraction_npcs_present

    snapshot = GameSnapshot(characters=[_pc("Carl")])  # no opponent seated
    result = NarrationTurnResult(narration="A stranger lurks.")
    extraction = SidecarExtraction(
        npcs_present=[{"name": "Stranger", "side": "opponent"}]  # disagrees with engine
    )

    merge_sidecar_extraction_npcs_present(result, extraction, snapshot)

    mismatches = [
        s for s in otel_capture.get_finished_spans() if s.name == "sidecar_extraction.mismatch"
    ]
    assert len(mismatches) >= 1, (
        "the merge must fire a sidecar_extraction.mismatch span when it overrides "
        "the extractor's claimed side with the engine's (the GM-panel lie-detector)"
    )
    assert any(dict(s.attributes or {}).get("field") == "npcs_present" for s in mismatches), (
        "the mismatch span must name field=npcs_present"
    )


def test_merge_npcs_present_no_mismatch_span_when_side_agrees(otel_capture) -> None:
    """Paranoid negative: when the extractor's claimed side AGREES with the engine
    (both opponent), the merge fires NO mismatch span — the lie-detector only beeps
    on real divergence, never on every turn (no false-positive storm)."""
    from sidequest.server.narration_apply import merge_sidecar_extraction_npcs_present

    snapshot = _snapshot_with_encounter(
        actors=[
            EncounterActor(name="Carl", role="lead", side="player"),
            EncounterActor(name="Grix", role="foe", side="opponent"),
        ]
    )
    result = NarrationTurnResult(narration="Grix attacks.")
    extraction = SidecarExtraction(
        npcs_present=[{"name": "Grix", "side": "opponent"}]  # agrees with engine
    )

    merge_sidecar_extraction_npcs_present(result, extraction, snapshot)

    mismatches = [
        s for s in otel_capture.get_finished_spans() if s.name == "sidecar_extraction.mismatch"
    ]
    assert mismatches == [], (
        "no mismatch span may fire when the extractor's side agrees with the "
        "engine — the lie-detector beeps on divergence only"
    )


# ===========================================================================
# AC — cosmetic fields: scene_mood / visual_scene / footnotes from the extractor.
# ===========================================================================


def test_merge_cosmetic_sources_all_three_fields() -> None:
    """The cosmetic seam sources ``scene_mood`` / ``visual_scene`` / ``footnotes``
    from the post-narration extraction onto the result. RED until the seam
    exists."""
    from sidequest.server.narration_apply import merge_sidecar_extraction_cosmetic

    result = NarrationTurnResult(narration="prose")  # post-retirement: empty
    extraction = SidecarExtraction(
        scene_mood="ominous",
        visual_scene={
            "subject": "a dim cellar",
            "tier": "scene_illustration",
            "tags": ["location"],
        },
        footnotes=[{"summary": "The vault is sealed.", "category": "Place", "is_new": True}],
    )

    merge_sidecar_extraction_cosmetic(result, extraction)

    assert result.scene_mood == "ominous"
    assert result.footnotes == [
        {"summary": "The vault is sealed.", "category": "Place", "is_new": True}
    ]
    assert isinstance(result.visual_scene, VisualScene)
    assert result.visual_scene.subject == "a dim cellar"


def test_merge_cosmetic_visual_scene_dict_becomes_model() -> None:
    """``SidecarExtraction.visual_scene`` is a raw dict (the ``emit_tool`` shape);
    the merge converts it to a ``VisualScene`` model (the type ``NarrationTurnResult``
    holds), carrying subject / tier / mood / tags — the same conversion the result
    assembler does. RED until the seam converts it."""
    from sidequest.server.narration_apply import merge_sidecar_extraction_cosmetic

    result = NarrationTurnResult(narration="prose")
    extraction = SidecarExtraction(
        visual_scene={
            "subject": "a storm over the spires",
            "tier": "landscape",
            "mood": "dramatic",
            "tags": ["location", "atmosphere"],
        }
    )

    merge_sidecar_extraction_cosmetic(result, extraction)

    assert isinstance(result.visual_scene, VisualScene)
    assert result.visual_scene.subject == "a storm over the spires"
    assert result.visual_scene.tier == "landscape"
    assert result.visual_scene.mood == "dramatic"
    assert result.visual_scene.tags == ["location", "atmosphere"]


def test_merge_cosmetic_overwrites_stale_no_fallback() -> None:
    """No Silent Fallbacks: the extraction is the SOLE source. An empty extraction
    OVERWRITES stale cosmetic values on the result (a non-compliant narrator's
    game_patch leak) — scene_mood→None, visual_scene→None, footnotes→[]. RED until
    the seam exists."""
    from sidequest.server.narration_apply import merge_sidecar_extraction_cosmetic

    result = NarrationTurnResult(
        narration="prose",
        scene_mood="STALE tense",
        visual_scene=VisualScene(subject="STALE scene"),
        footnotes=[{"summary": "STALE footnote"}],
    )
    extraction = SidecarExtraction()  # extractor read nothing cosmetic

    merge_sidecar_extraction_cosmetic(result, extraction)

    assert result.scene_mood is None, "stale scene_mood must be overwritten, not kept"
    assert result.visual_scene is None, "stale visual_scene must be overwritten, not kept"
    assert result.footnotes == [], "stale footnotes must be overwritten, not kept"


# ===========================================================================
# Wiring — both merge seams are reached on the live post-narration turn path.
# ===========================================================================


def test_merge_seams_wired_into_session_handler() -> None:
    """Pipeline wiring (reflection, NOT source-grep): the WS turn handler — the
    live post-narration seam that already runs the extractor shadow runner and
    ``_apply_narration_result_to_snapshot`` — must import BOTH 151-5 merge seams so
    the extraction's npcs_present + cosmetic fields are applied in production. A
    missing import means the extractor is built but its output never reaches apply
    (the half-wired failure CLAUDE.md forbids)."""
    from sidequest.server import websocket_session_handler as wsh

    for seam in (
        "merge_sidecar_extraction_npcs_present",
        "merge_sidecar_extraction_cosmetic",
    ):
        assert seam in wsh.__dict__, (
            f"websocket_session_handler must import {seam} so the post-narration "
            f"extractor's fields reach narration_apply (ADR-150 step 4 cutover II)"
        )
