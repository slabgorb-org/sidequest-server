"""Regression for [RENDER-NO-SUBJECT] — playtest gulliver/glenross 150-7, 2026-06-20.

The ADR-150 bucket-B cutover (Story 151-5) demoted ``visual_scene`` (a GENERATIVE
art-direction directive) and ``footnotes`` (the player's knowledge feed) into the
post-narration *never-invent* reader. A reader told "report only what the prose
states; never invent" structurally CANNOT produce a render subject (composing one
IS invention) — so it returned them empty every turn → ``render.eligible_no_subject``
→ zero scrapbook illustrations on every world, and (footnotes) ``known_facts=0``.

ADR-150 amendment (2026-06-20): bucket-B is EXTRACTIVE-only. Generative/authorial
fields (``visual_scene``, ``footnotes``) stay narrator-owned, the same exception
already granted to ``private_segments``. Only genuinely-extractive facts (items,
gold, companions, npcs_present, scene_mood) are extractor-sourced.
"""

from __future__ import annotations

from sidequest.agents.orchestrator import (
    NarrationTurnResult,
    VisualScene,
    extract_structured_from_response,
)
from sidequest.agents.sidecar_extractor import BUCKET_B_FIELDS, SidecarExtraction


def _raw_with_patch(patch_json: str) -> str:
    return f"**Scene**\n\nProse.\n\n```game_patch\n{patch_json}\n```\n"


def test_extract_structured_restores_narrator_visual_scene() -> None:
    """The narrator's authorial ``visual_scene`` survives parsing (not hard-set None)."""
    raw = _raw_with_patch(
        '{"visual_scene": {"subject": "a foggy kirkyard at dusk", '
        '"tier": "landscape", "mood": "ominous", "tags": ["location"]}}'
    )
    result = extract_structured_from_response(raw)
    assert result["visual_scene"] is not None
    assert result["visual_scene"]["subject"] == "a foggy kirkyard at dusk"


def test_extract_structured_restores_narrator_footnotes() -> None:
    """The narrator's knowledge-feed ``footnotes`` survive parsing (not hard-set [])."""
    raw = _raw_with_patch(
        '{"footnotes": [{"summary": "The vicar named a hidden cope-press.", '
        '"category": "Place", "is_new": true}]}'
    )
    result = extract_structured_from_response(raw)
    assert len(result["footnotes"]) == 1
    assert result["footnotes"][0]["summary"].startswith("The vicar")


def test_generative_fields_are_not_extractor_sourced() -> None:
    """Generative/authorial fields must NOT ride the never-invent reader, while
    genuinely-extractive ``scene_mood`` stays."""
    assert "visual_scene" not in BUCKET_B_FIELDS
    assert "footnotes" not in BUCKET_B_FIELDS
    # The extractor model no longer carries them (the tool schema derives from it).
    assert not hasattr(SidecarExtraction(), "visual_scene")
    assert not hasattr(SidecarExtraction(), "footnotes")
    assert "scene_mood" in BUCKET_B_FIELDS


def test_cosmetic_merge_preserves_narrator_visual_scene_and_footnotes() -> None:
    """The cosmetic merge sources only ``scene_mood`` now — it must NOT clobber the
    narrator-owned ``visual_scene`` / ``footnotes`` back to empty."""
    from sidequest.server.narration_apply import merge_sidecar_extraction_cosmetic

    result = NarrationTurnResult(narration="prose")
    result.visual_scene = VisualScene.from_dict(
        {"subject": "a foggy kirkyard", "tier": "landscape", "tags": []}
    )
    result.footnotes = [
        {"summary": "The vicar named a cope-press.", "category": "Place", "is_new": True}
    ]
    extraction = SidecarExtraction(scene_mood="ominous")
    merge_sidecar_extraction_cosmetic(result, extraction)

    # scene_mood still sourced from the extractor...
    assert result.scene_mood == "ominous"
    # ...but the narrator's generative fields survive the merge.
    assert isinstance(result.visual_scene, VisualScene)
    assert result.visual_scene.subject == "a foggy kirkyard"
    assert result.footnotes and result.footnotes[0]["summary"].startswith("The vicar")
