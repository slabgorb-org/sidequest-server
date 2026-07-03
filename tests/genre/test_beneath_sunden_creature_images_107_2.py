"""Story 107-2 invariant, retuned by 158-52 — bestiary is the render source.

History: the 2026-06-13 combat playtest surfaced the early opponent as "the
creature of animal musk" with a bare "T" letter chip because the low-band
shaft creatures had no ``creatures.yaml`` image spec. Story 107-2 authored
the 6 shaft specs and pinned "every low-tagged bestiary entry must appear in
``creatures.yaml``" — the per-world-manifest model.

Story 158-52 (Keith, 2026-07-01) replaces that model: ``bestiary.yaml`` is
the single source of truth for creature-image production; the render
pipeline DERIVES the prompt from it, and ``creatures.yaml`` is demoted to an
OPTIONAL per-world override (naming conceits, bespoke marquee plates).
"Renderable" therefore means "resolvable to a render prompt":

  1. the entry has a non-empty bestiary ``description`` (the render subject),
  2. its NAMING is handled for this world's "nothing is named" conceit —
     either the world declares top-level ``name_is_secret: true`` in
     ``creatures.yaml`` (the pipeline de-proper-nouns every derived name) or
     the entry ships a per-id override whose ``name`` is not the bestiary
     proper noun. Z-Image paints proper nouns from subject AND clip.

The no-text/no-caption cleanup clause is NOT a per-description obligation
any more: it lives in the world's ``visual_style.yaml positive_suffix``
(which REPLACES the genre suffix for this world) and auto-layers at render
time. A guard below pins that clause where it now lives.

GATED: skipped when ``sidequest-content`` is not on disk (shipped-content
assertion, mirrors ``tests/genre/test_world_bestiary_content.py``).
"""

from __future__ import annotations

import pytest
import yaml

from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

pytestmark = pytest.mark.skipif(
    not GENRE_PACKS_DIR.is_dir(), reason="sidequest-content not on disk"
)

# The six low-band (level 1-2) shaft creatures 107-2 authored bespoke specs
# for — the actual early-combat opponents. Their non-proper-noun guard is
# kept under the derived-source model (158-52).
LOW_BAND_IDS = (
    "gnaw_swarm",
    "rope_spider",
    "hold_skeleton",
    "shaft_goblin",
    "grave_ghoul",
    "harrier_pack_leader",
)

# Medium/style tokens that MUST NOT appear in an override `description` — they
# auto-layer from visual_style.yaml positive_suffix; duplicating them flattens
# the render (context-story-107-2 Technical Guardrails, explicit list).
STYLE_TOKENS = ("pen-and-ink", "engraving", "crosshatch", "b&w", "black-and-white")


def _world_dir():
    try:
        pack_dir = find_pack_path("caverns_and_claudes")
    except PackNotFound:
        pytest.skip("caverns_and_claudes not on disk")
    return pack_dir / "worlds" / "beneath_sunden"


def _creatures_manifest() -> tuple[dict[str, dict], bool]:
    """Return (specs-by-id, name_is_secret flag) from creatures.yaml.

    Under the derived-source model the file is an OPTIONAL override manifest;
    for this world it must exist because it is where the naming conceit is
    declared (flag or per-id naming overrides).
    """
    path = _world_dir() / "creatures.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "creatures.yaml must be a mapping"
    creatures = data.get("creatures") or []
    assert isinstance(creatures, list), "`creatures:` must be a list when present"
    specs = {c["id"]: c for c in creatures if isinstance(c, dict) and c.get("id")}
    return specs, data.get("name_is_secret") is True


def _bestiary_entries_by_id() -> dict[str, dict]:
    path = _world_dir() / "bestiary.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = data.get("entries") if isinstance(data, dict) else None
    assert isinstance(entries, list) and entries
    return {e["id"]: e for e in entries if isinstance(e, dict) and e.get("id")}


def _naming_handled(spec: dict | None, secret_flag: bool, proper: str) -> bool:
    """The 158-52 naming contract for a 'nothing is named' world.

    A per-id override name that differs from the bestiary proper noun handles
    it; otherwise the world-level ``name_is_secret: true`` flag must be set so
    the pipeline de-proper-nouns the derived name.
    """
    if spec is not None:
        name = spec.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip().lower() != proper.strip().lower()
    return secret_flag


def test_every_low_tagged_bestiary_entry_is_renderable() -> None:
    """RETUNED (158-52): every low-tagged bestiary entry resolves to a render
    prompt — non-empty bestiary description + naming handled. Presence in
    creatures.yaml is no longer the bar; the bestiary is the source of truth.
    Keyed to the bestiary's own `tags` so a future low-band addition can't
    silently regress to a 'T' chip."""
    specs, secret_flag = _creatures_manifest()
    bestiary = _bestiary_entries_by_id()
    low_tagged = {
        eid: e for eid, e in bestiary.items() if "low" in (e.get("tags") or [])
    }
    assert low_tagged, "precondition: bestiary tags its low band"

    no_subject = [
        eid
        for eid, e in low_tagged.items()
        if not (e.get("description") or "").strip()
    ]
    assert not no_subject, (
        f"low-band bestiary entries with no description: {no_subject} — a "
        "derived render prompt needs the bestiary description as its subject"
    )

    unhandled = sorted(
        eid
        for eid, e in low_tagged.items()
        if not _naming_handled(specs.get(eid), secret_flag, e["name"])
    )
    assert not unhandled, (
        f"{len(unhandled)} low-band entries would derive their bestiary "
        f"proper noun into the CLIP prompt (first 10: {unhandled[:10]}) — "
        "this world's conceit is that nothing is named. Declare top-level "
        "`name_is_secret: true` in creatures.yaml or ship per-id naming "
        "overrides."
    )


def test_low_band_shaft_ids_keep_non_proper_noun_guard() -> None:
    """The historical 107-2 guard for the 6 bespoke shaft ids, kept under the
    derived model: where a bespoke spec names one of them, that name is a
    descriptive phrase (it slugifies to the PNG filename) — never the bestiary
    proper noun, no digits, no quotes. An id whose spec was dropped in the
    demotion must instead be covered by the world naming flag."""
    specs, secret_flag = _creatures_manifest()
    bestiary = _bestiary_entries_by_id()
    for cid in LOW_BAND_IDS:
        assert cid in bestiary, f"{cid}: shaft id vanished from bestiary.yaml"
        spec = specs.get(cid)
        proper = bestiary[cid]["name"]
        if spec is None or not (spec.get("name") or "").strip():
            assert secret_flag, (
                f"{cid}: no bespoke naming override and no world "
                "`name_is_secret: true` flag — the derived CLIP would paint "
                f"the proper noun {proper!r}"
            )
            continue
        name = spec["name"]
        assert name.strip().lower() != proper.strip().lower(), (
            f"{cid}: override `name` is the bestiary proper noun {proper!r}"
        )
        assert not any(ch.isdigit() for ch in name), (
            f"{cid}: spec name has digits: {name!r}"
        )
        assert '"' not in name and "'" not in name, (
            f"{cid}: spec name has quotes: {name!r}"
        )


def test_override_specs_are_well_formed_and_style_free() -> None:
    """Every override entry that remains in the demoted creatures.yaml must be
    a valid per-field override: a non-empty id, and any field it does declare
    is non-empty. A declared `description` stays SUBJECT-only camera prose —
    medium/style tokens auto-layer from visual_style.yaml and must not appear
    (context-story-107-2 Technical Guardrails, Keith emphatic)."""
    specs, _ = _creatures_manifest()
    assert specs, (
        "creatures.yaml carries no override entries at all — this world needs "
        "at least its bespoke capstone plates or naming overrides"
    )
    for cid, spec in specs.items():
        for field in ("name", "description"):
            if field in spec:
                assert isinstance(spec[field], str) and spec[field].strip(), (
                    f"{cid}: declared override field `{field}` is empty — "
                    "omit the field to inherit from the bestiary instead"
                )
        desc = (spec.get("description") or "").lower()
        leaked = [tok for tok in STYLE_TOKENS if tok in desc]
        assert not leaked, (
            f"{cid}: style/medium token(s) {leaked} in override `description` "
            "— these belong in visual_style.yaml positive_suffix"
        )


def test_world_suffix_carries_no_text_clause() -> None:
    """The no-text/no-caption cleanup clause moved out of per-creature
    descriptions and lives in the world visual_style positive_suffix (which
    REPLACES the genre suffix for this world; guidance_scale=0 means the
    negative prompt is ignored, so the positive suffix is the only place the
    clause can act). If this guard trips, every derived creature plate loses
    its caption protection at once."""
    path = _world_dir() / "visual_style.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    suffix = (data.get("positive_suffix") or "").lower()
    assert suffix.strip(), "beneath_sunden must ship a world positive_suffix"
    assert "no text" in suffix and "no caption" in suffix, (
        "world positive_suffix lost the no-text/no-caption cleanup clause — "
        "under the derived-source model this is the ONLY place it layers in"
    )
