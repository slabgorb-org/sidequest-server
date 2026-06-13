"""Story 107-2 (RED) — beneath_sunden low-band creatures need renderable image specs.

The combat playtest (2026-06-13, sq-playtest-pingpong) found the dungeon's
early opponent surfaced as "the creature of animal musk" with only a "T" letter
chip — no portrait. Diagnosis (context-story-107-2 §"why ... appeared", cause 2):
the 6 LOW-BAND shaft creatures that are the actual early-game opponents have NO
entry in ``creatures.yaml`` (the Z-Image manifest), so there is no asset to
render. Only the 7 mid/deep capstones have specs.

This file is the CONTENT half of AC4 (+ AC1 content): every low-band combat
opponent must have a render-ready ``creatures.yaml`` image spec following the
established capstone template — non-proper-noun ``name``, STYLE-FREE camera-prose
``description`` (medium/style auto-layers from ``visual_style.yaml``, NOT the
description — context Technical Guardrails, Keith emphatic), ``threat_level``,
``tags``, ending with the no-text cleanup clause.

Reskin ruling (Keith, 2026-06-13): author specs for the EXISTING low-band
bestiary roster — no net-new entities. So the assertion is keyed by the 6
existing bestiary ids.

GATED: skipped when ``sidequest-content`` is not on disk (the seam logic is unit
-tested elsewhere; this is a shipped-content assertion). Mirrors
``tests/genre/test_world_bestiary_content.py``.
"""

from __future__ import annotations

import pytest
import yaml

from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

pytestmark = pytest.mark.skipif(
    not GENRE_PACKS_DIR.is_dir(), reason="sidequest-content not on disk"
)

# The six low-band (level 1-2) shaft creatures from
# worlds/beneath_sunden/bestiary.yaml — the actual early-combat opponents.
# These are the entries with NO creatures.yaml image spec today (RED).
LOW_BAND_IDS = (
    "gnaw_swarm",
    "rope_spider",
    "hold_skeleton",
    "shaft_goblin",
    "grave_ghoul",
    "harrier_pack_leader",
)

# Medium/style tokens that MUST NOT appear in a creature `description` — they
# auto-layer from visual_style.yaml positive_suffix; duplicating them flattens
# the render (context Technical Guardrails, explicit list).
STYLE_TOKENS = ("pen-and-ink", "engraving", "crosshatch", "b&w", "black-and-white")


def _world_dir():
    try:
        pack_dir = find_pack_path("caverns_and_claudes")
    except PackNotFound:
        pytest.skip("caverns_and_claudes not on disk")
    return pack_dir / "worlds" / "beneath_sunden"


def _creature_specs_by_id() -> dict[str, dict]:
    path = _world_dir() / "creatures.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    creatures = data.get("creatures") if isinstance(data, dict) else None
    assert isinstance(creatures, list) and creatures, (
        "creatures.yaml must define a non-empty top-level `creatures:` list"
    )
    return {c["id"]: c for c in creatures if isinstance(c, dict) and c.get("id")}


def _bestiary_entries_by_id() -> dict[str, dict]:
    path = _world_dir() / "bestiary.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = data.get("entries") if isinstance(data, dict) else None
    assert isinstance(entries, list) and entries
    return {e["id"]: e for e in entries if isinstance(e, dict) and e.get("id")}


def test_all_low_band_creatures_have_image_specs() -> None:
    """RED: each of the 6 low-band shaft creatures has a creatures.yaml spec.

    Today only the 7 mid/deep capstones are authored, so every low-band id is
    missing — which is exactly why the early fight rendered a 'T' chip."""
    specs = _creature_specs_by_id()
    missing = [cid for cid in LOW_BAND_IDS if cid not in specs]
    assert not missing, (
        f"low-band combat opponents with no creatures.yaml image spec: {missing} "
        "— these are the actual early-game fights and currently render no portrait"
    )


def test_low_band_specs_have_required_fields() -> None:
    specs = _creature_specs_by_id()
    for cid in LOW_BAND_IDS:
        spec = specs.get(cid)
        assert spec is not None, f"{cid}: no image spec (see prior test)"
        assert isinstance(spec.get("name"), str) and spec["name"].strip(), (
            f"{cid}: image spec needs a non-empty descriptive `name`"
        )
        assert isinstance(spec.get("description"), str) and spec["description"].strip(), (
            f"{cid}: image spec needs a non-empty `description`"
        )
        assert isinstance(spec.get("threat_level"), int), f"{cid}: `threat_level` must be int"
        assert isinstance(spec.get("tags"), list) and spec["tags"], f"{cid}: needs non-empty `tags`"


def test_low_band_descriptions_are_style_free() -> None:
    """The description carries SUBJECT only — anatomy/posture/scale/what the dark
    does. Medium/style auto-layers from visual_style.yaml; putting it here fights
    the suffix (context Technical Guardrails, Keith emphatic)."""
    specs = _creature_specs_by_id()
    for cid in LOW_BAND_IDS:
        spec = specs.get(cid)
        assert spec is not None, (
            f"{cid}: no image spec (see test_all_low_band_creatures_have_image_specs)"
        )
        desc = spec["description"].lower()
        leaked = [tok for tok in STYLE_TOKENS if tok in desc]
        assert not leaked, (
            f"{cid}: style/medium token(s) {leaked} in `description` — these belong "
            "in visual_style.yaml positive_suffix, not the subject prose"
        )


def test_low_band_descriptions_end_with_no_text_clause() -> None:
    """guidance_scale=0 means negative_prompt is ignored, so each description must
    carry the no-text/no-caption cleanup clause inline (capstone template)."""
    specs = _creature_specs_by_id()
    for cid in LOW_BAND_IDS:
        spec = specs.get(cid)
        assert spec is not None, (
            f"{cid}: no image spec (see test_all_low_band_creatures_have_image_specs)"
        )
        desc = spec["description"].lower()
        assert "no text" in desc and "no caption" in desc, (
            f"{cid}: description missing the no-text/no-caption cleanup clause"
        )


def test_low_band_spec_names_are_non_proper_nouns() -> None:
    """Z-Image paints proper nouns from subject AND clip; this world's conceit is
    that nothing is named. Each `name` is a descriptive phrase (it slugifies to
    the PNG filename); the SRD linkage stays in `id`. So the spec name must not be
    the bestiary's proper name and must carry no quotes/digits."""
    specs = _creature_specs_by_id()
    bestiary = _bestiary_entries_by_id()
    for cid in LOW_BAND_IDS:
        spec = specs.get(cid)
        assert spec is not None, (
            f"{cid}: no image spec (see test_all_low_band_creatures_have_image_specs)"
        )
        name = spec["name"]
        proper = bestiary[cid]["name"]
        assert name != proper, (
            f"{cid}: image spec `name` is the bestiary proper noun {proper!r} — "
            "Z-Image would paint the name; use a descriptive phrase"
        )
        assert not any(ch.isdigit() for ch in name), f"{cid}: spec name has digits: {name!r}"
        assert '"' not in name and "'" not in name, f"{cid}: spec name has quotes: {name!r}"


def test_every_low_tagged_bestiary_entry_is_renderable() -> None:
    """Cross-check via the bestiary's own `tags`: every entry tagged 'low' must
    have a renderable image spec. Pins the closure to the data (not a hand-kept
    list) so a future low-band addition can't silently regress to a 'T' chip."""
    specs = _creature_specs_by_id()
    bestiary = _bestiary_entries_by_id()
    low_tagged = [eid for eid, e in bestiary.items() if "low" in (e.get("tags") or [])]
    assert low_tagged, "precondition: bestiary tags its low band"
    unrenderable = [eid for eid in low_tagged if eid not in specs]
    assert not unrenderable, (
        f"low-band bestiary entries with no image spec: {unrenderable} — every "
        "reachable early opponent must have a portrait asset spec (AC4)"
    )
