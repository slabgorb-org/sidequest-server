"""RED-phase contract tests — Story 100-4.

Phase 1, **POI section** slice of the reference-pages → React migration
(spec: ``docs/superpowers/specs/2026-06-08-reference-pages-react-migration-design.md``).

Slice A (map, PR #762), 100-2 (generic-YAML), and 100-3 (Cast) already project
public-only JSON that React renders. This story adds the **POI** section: the
JSON projection of the world's ``points_of_interest`` (authored in
``history.yaml``), the data-shaping analog of the HTML ``present_renderable_landscapes``
presenter in ``reference_presenters.py``.

One firewall and one gate govern this section — both load-bearing:

  1. **R2 landscape gate, server-side.** A POI is projected only when its
     world-scoped landscape key (``poi_image_key`` over the **verbatim** authored
     slug) is present in ``r2_manifest.json``. This mirrors the HTML
     "Renderable Landscapes" gallery, which shows ONLY POIs with rendered art.
     The ``image_url`` is resolved on the server via ``resolve_asset_url`` and
     emitted as a finished URL — the client never sees a raw R2 key, manifest
     path, or filesystem path, and never builds the URL itself.

  2. **Keeper firewall (spec C1).** Only the public allowlist keys cross the JSON
     boundary; keeper POI fields (gm notes, secrets, internal flags) are never
     splatted in.

**Two TEA decisions pin this RED phase (logged as deviations in the session
file — read them before changing a test):**

  * **Exclusion model.** A POI whose landscape is NOT on R2 is OMITTED from the
    section entirely (never a member with ``image_url: null``); the section is
    ``None`` when no POI survives the gate. This intentionally DIVERGES from the
    100-3 Cast section (which includes non-R2 members with ``portrait_url:
    null``) — POI follows its own HTML analog, the gallery
    ``present_renderable_landscapes``, which excludes art-less POIs. An excluded
    POI still fires a ``not_found`` span (observable skip), IMPROVING on the HTML
    gallery, which ``continue``s past art-less POIs silently.

  * **Span reuse.** The gate decision is observed via the SHIPPED Story 63-8
    spans ``reference_poi_image_resolved_span`` /
    ``reference_poi_image_not_found_span`` (``SPAN_REFERENCE_POI_IMAGE_*``), NOT
    new POI spans — "Don't Reinvent", and parity with 100-3 (which reused the
    existing ``reference_portrait_*`` spans). These carry ``reference.slug`` /
    ``reference.pack`` / ``reference.world``.

Contract this RED phase pins (Dev implements — ``build_poi_section`` does not
exist yet, so the import fails and every test below is RED):

  - ``build_poi_section(entries, *, pack, world, poi_on_r2_slugs) -> dict | None``
    Projects the R2-gated POIs into the public ``poi`` section dict, or ``None``
    when no POI survives the gate. Mirrors ``build_cast_section``'s signature (a
    pre-gated R2 **anchor**-slug set is passed in; the function does not load the
    manifest itself). ``poi_on_r2_slugs`` holds the slugify **anchor** form (the
    output of ``_gate_poi_slugs_on_manifest``); the R2 object key is built from
    the **verbatim** authored slug (the Story 71-38 decouple). Section shape:
        {"id": "poi", "label": "Points of Interest",
         "entries": [{"slug": str, "name": str, "region": str | None,
                      "description": str | None, "image_url": str}, ...]}

  - ``build_lore_projection`` (extended) appends a ``poi`` section built from
    ``load_points_of_interest`` + ``load_poi_slug_map`` + the
    ``_gate_poi_slugs_on_manifest`` R2 gate, AFTER the map section.

The member-dict shape itself is a Dev/Architect call; if Dev lands a different
but equally public-only shape, the SECURITY assertions (no keeper field in the
JSON, the R2 gate actually gates membership, image_url resolved server-side
never raw) are the non-negotiable ones — do not weaken them to fit a shape
change. Shape-only assertions are flagged ``[shape]``.
"""

from __future__ import annotations

import json as _json
from pathlib import Path

# RED: this import fails until Dev adds the POI-section projection builder.
from sidequest.server.reference_projection import (
    build_lore_projection,
    build_poi_section,
)
from sidequest.server.reference_slug import slugify
from sidequest.telemetry.spans.reference import (
    SPAN_REFERENCE_POI_IMAGE_NOT_FOUND,
    SPAN_REFERENCE_POI_IMAGE_RESOLVED,
)
from tests.server.conftest import span_attrs_by_name

# ---------------------------------------------------------------------------
# Fixtures — points_of_interest[] entry dicts (the public POI source). Each POI
# carries name/slug/region/type/description; ``slug`` is the VERBATIM authored
# slug (the R2 object-key form), and ``slugify(slug)`` is the ANCHOR form the
# gate returns and the section keys membership on.
# ---------------------------------------------------------------------------


def _poi(name: str, slug: str, **extra: object) -> dict:
    return {"name": name, "slug": slug, **extra}


def _pois() -> list[dict]:
    return [
        _poi(
            "The Salt Quay",
            "salt_quay",
            region="Harbor District",
            type="harbor",
            description="Brine-stained docks where the smuggling barges tie up.",
        ),
        _poi(
            "Rust Fields",
            "rust_fields",
            region="The Margins",
            type="wasteland",
            description="A plain of corroded machinery stretching to the horizon.",
        ),
    ]


def _anchor(slug: str) -> str:
    return slugify(slug)


def _member_by_name(section: dict, name: str) -> dict:
    return next(m for m in section["entries"] if m["name"] == name)


# The public allowlist — the maximal set of keys that may cross the boundary.
# ``type`` is included as a plausibly-public chip field (the HTML gallery renders
# it) so the subset assertion does not force Dev to drop it; the 5 context keys
# are the ones actually asserted present.
_PUBLIC_KEYS = {"slug", "name", "region", "description", "image_url", "type"}


# ===========================================================================
# Group 1 — The R2 landscape gate is the door (membership). A POI with art on
#           R2 projects; a POI without art is EXCLUDED (not null), section None
#           when none survive.
# ===========================================================================


def test_poi_on_manifest_appears_in_output():
    section = build_poi_section(
        _pois(),
        pack="p",
        world="w",
        poi_on_r2_slugs=frozenset({_anchor("salt_quay"), _anchor("rust_fields")}),
    )
    assert section is not None
    # [shape] section envelope.
    assert section["id"] == "poi"
    assert section["label"] == "Points of Interest"
    names = {m["name"] for m in section["entries"]}
    assert names == {"The Salt Quay", "Rust Fields"}
    quay = _member_by_name(section, "The Salt Quay")
    assert quay["region"] == "Harbor District"
    assert quay["description"] == "Brine-stained docks where the smuggling barges tie up."


def test_poi_not_on_manifest_is_excluded():
    # Only Salt Quay's landscape is on R2 → Rust Fields is omitted ENTIRELY
    # (exclusion model — parity with the HTML "Renderable Landscapes" gallery,
    # not the Cast section's include-text-only). The excluded POI's data must
    # not survive anywhere in the JSON.
    section = build_poi_section(
        _pois(),
        pack="p",
        world="w",
        poi_on_r2_slugs=frozenset({_anchor("salt_quay")}),
    )
    assert section is not None
    names = {m["name"] for m in section["entries"]}
    assert "The Salt Quay" in names
    assert "Rust Fields" not in names, (
        "a POI whose landscape is not on R2 must be excluded from the section "
        "(exclusion model), never projected with image_url: null"
    )
    blob = _json.dumps(section)
    assert "Rust Fields" not in blob
    assert "A plain of corroded machinery stretching to the horizon." not in blob


def test_every_projected_poi_has_a_resolved_image_url():
    # Corollary of exclusion: a projected member NEVER carries a null image_url —
    # if it survived the gate it has art, and image_url is the resolved URL.
    section = build_poi_section(
        _pois(),
        pack="p",
        world="w",
        poi_on_r2_slugs=frozenset({_anchor("salt_quay")}),
    )
    for member in section["entries"]:
        assert member["image_url"] is not None, (
            "exclusion model: a projected POI always has a resolved image_url"
        )
        assert isinstance(member["image_url"], str)


def test_all_poi_off_manifest_projects_to_none():
    section = build_poi_section(
        _pois(),
        pack="p",
        world="w",
        poi_on_r2_slugs=frozenset(),  # nothing on R2
    )
    assert section is None, "no POI survives the R2 gate → omit the POI section (None)"


def test_empty_entries_projects_to_none():
    assert build_poi_section([], pack="p", world="w", poi_on_r2_slugs=frozenset()) is None


def test_entry_without_slug_or_name_is_skipped():
    # An entry with neither slug nor name cannot key a landscape (parity with
    # load_poi_slug_map, which keys on slug or name). It is silently skipped — it
    # was never a candidate, so it does not even reach the gate.
    entries = [
        _poi("The Salt Quay", "salt_quay"),
        {"region": "nowhere", "description": "an unnamed blur"},
    ]
    section = build_poi_section(
        entries,
        pack="p",
        world="w",
        poi_on_r2_slugs=frozenset({_anchor("salt_quay")}),
    )
    assert section is not None
    assert [m["name"] for m in section["entries"]] == ["The Salt Quay"]
    assert "an unnamed blur" not in _json.dumps(section)


def test_poi_keyed_by_name_when_no_slug():
    # load_poi_slug_map falls back to ``name`` when ``slug`` is absent. A POI
    # authored with only a name must gate on slugify(name) and resolve a
    # name-derived R2 key.
    entries = [{"name": "Old Harbor", "region": "Docks", "description": "Weathered piers."}]
    section = build_poi_section(
        entries,
        pack="p",
        world="w",
        poi_on_r2_slugs=frozenset({_anchor("Old Harbor")}),
    )
    assert section is not None
    member = section["entries"][0]
    assert member["name"] == "Old Harbor"
    assert "assets/poi/Old Harbor.png" in member["image_url"]


# ===========================================================================
# Group 2 — image_url resolved SERVER-SIDE over the VERBATIM slug (load-bearing
#           Story 71-38 decouple). The client gets a finished URL, never a raw
#           key/path, and the underscore R2 key is addressed, not the anchor.
# ===========================================================================


def test_landscape_url_resolved_when_on_r2():
    section = build_poi_section(
        _pois(),
        pack="p",
        world="w",
        poi_on_r2_slugs=frozenset({_anchor("salt_quay")}),
    )
    quay = _member_by_name(section, "The Salt Quay")
    url = quay["image_url"]
    # Server-side resolution: a URL, not a raw R2 object key.
    assert isinstance(url, str)
    assert url.startswith("http"), f"image_url must be a resolved URL, got {url!r}"
    # The world-scoped key segment is embedded in the resolved URL.
    assert "genre_packs/p/worlds/w/assets/poi/salt_quay.png" in url


def test_verbatim_underscore_slug_addresses_underscore_r2_key():
    # THE load-bearing Story 71-38 decouple. The authored slug is underscore
    # (``salt_quay``); the anchor (gate membership + card id) is hyphen
    # (``salt-quay``); the R2 object key is the VERBATIM underscore form. The
    # resolved URL must address ``salt_quay.png`` (verbatim), NOT ``salt-quay.png``
    # (anchor) — conflating the two is the bug that emitted 0 POI images for every
    # underscore-slug world.
    section = build_poi_section(
        [_poi("The Salt Quay", "salt_quay", region="Harbor", description="Docks.")],
        pack="p",
        world="w",
        poi_on_r2_slugs=frozenset({"salt-quay"}),  # anchor (hyphen) form, as the gate returns
    )
    assert section is not None, "anchor 'salt-quay' is in the gated set → POI projects"
    member = section["entries"][0]
    assert "assets/poi/salt_quay.png" in member["image_url"], (
        "the R2 key must use the VERBATIM underscore slug"
    )
    assert "salt-quay.png" not in member["image_url"], (
        "the anchor (hyphen) slug must NOT be used as the R2 object key (71-38 bug)"
    )


def test_client_never_sees_raw_r2_key_or_path():
    section = build_poi_section(
        _pois(),
        pack="p",
        world="w",
        poi_on_r2_slugs=frozenset({_anchor("salt_quay")}),
    )
    quay = _member_by_name(section, "The Salt Quay")
    # The resolved URL is present...
    assert quay["image_url"].startswith("http")
    # ...and the manifest gate slug set is an INPUT, never echoed as a field.
    assert "poi_on_r2_slugs" not in quay
    assert "r2_manifest" not in _json.dumps(section)


# ===========================================================================
# Group 3 — Keeper firewall: no keeper field from POI data crosses the JSON
#           boundary (spec C1). Load-bearing. Do not weaken.
# ===========================================================================


# Keeper-side POI fields — authored alongside the public name/region/description
# but must NEVER reach the player.
_KEEPER_POI_FIELDS = {
    "gm_notes": "the quay floods on the third night — strand the party here",
    "secret": "a smuggler's cache is buried under the third piling",
    "trap": "pressure plate at the dock gate, 2d6 on a failed save",
    "hidden_exit": "a tunnel behind the bait shop leads to the keeper's lair",
    "draft": True,
}


def test_keeper_poi_fields_never_cross_the_boundary():
    entry = _poi(
        "The Salt Quay",
        "salt_quay",
        region="Harbor District",
        description="Brine-stained docks.",
        **_KEEPER_POI_FIELDS,
    )
    section = build_poi_section(
        [entry], pack="p", world="w", poi_on_r2_slugs=frozenset({_anchor("salt_quay")})
    )
    assert section is not None
    member = section["entries"][0]
    blob = _json.dumps(section)

    # No keeper KEY survives onto the projected member.
    for key in _KEEPER_POI_FIELDS:
        assert key not in member, f"keeper field {key!r} leaked onto the POI member"

    # No keeper VALUE survives anywhere in the JSON.
    assert "the quay floods on the third night" not in blob
    assert "a smuggler's cache is buried under the third piling" not in blob
    assert "pressure plate at the dock gate" not in blob
    assert "a tunnel behind the bait shop leads to the keeper's lair" not in blob

    # The PUBLIC fields did survive (the section is not just empty).
    assert "The Salt Quay" in blob
    assert "Harbor District" in blob
    assert "Brine-stained docks." in blob


def test_poi_member_carries_only_allowlisted_keys():
    # An allowlist projection (not a denylist) is the robust firewall: only the
    # known-public keys cross. This pins that the projected member dict does not
    # blindly splat the authored entry.
    entry = _poi(
        "The Salt Quay",
        "salt_quay",
        region="Harbor",
        description="Docks.",
        secret="the cache",
        gm_notes="floods nightly",
    )
    section = build_poi_section(
        [entry], pack="p", world="w", poi_on_r2_slugs=frozenset({_anchor("salt_quay")})
    )
    member = section["entries"][0]
    # [shape] the public key set — reconcilable if Dev's allowlist differs, but
    # it must be a SUBSET that excludes every keeper field.
    assert set(member.keys()) <= _PUBLIC_KEYS
    # The 5 context-named public keys are present.
    assert {"slug", "name", "region", "description", "image_url"} <= set(member.keys())
    assert "secret" not in member
    assert "gm_notes" not in member


# ===========================================================================
# Group 4 — OTEL wiring: the R2 landscape gate fires the SHIPPED Story 63-8 POI
#           image spans (reuse, not new spans). CLAUDE.md OTEL Observability
#           Principle — every gate decision emits a span, including the
#           exclusion (improving on the silent HTML gallery skip).
# ===========================================================================


def test_resolved_poi_fires_resolved_span(otel_capture) -> None:
    build_poi_section(
        _pois(),
        pack="p",
        world="w",
        poi_on_r2_slugs=frozenset({_anchor("salt_quay")}),
    )
    resolved = span_attrs_by_name(otel_capture, SPAN_REFERENCE_POI_IMAGE_RESOLVED)
    assert resolved, "an on-R2 POI must fire a poi_image_resolved span"
    # The 63-8 span carries the anchor slug under the ``reference.slug`` attr.
    assert any(a.get("reference.slug") == _anchor("salt_quay") for a in resolved)


def test_excluded_poi_fires_not_found_span(otel_capture) -> None:
    # Rust Fields is authored but not on R2 → excluded, AND it fires a
    # not_found span so the skip is OBSERVABLE (the HTML gallery skips it
    # silently; the projection must not).
    build_poi_section(
        _pois(),
        pack="p",
        world="w",
        poi_on_r2_slugs=frozenset({_anchor("salt_quay")}),
    )
    not_found = span_attrs_by_name(otel_capture, SPAN_REFERENCE_POI_IMAGE_NOT_FOUND)
    assert not_found, "an authored-but-not-on-R2 POI must fire poi_image_not_found"
    assert any(a.get("reference.slug") == _anchor("rust_fields") for a in not_found)


# ===========================================================================
# Group 5 — Wiring: the POI section appears in the full lore document, built
#           from history.yaml end-to-end through the real R2 gate (CLAUDE.md:
#           every test suite needs a wiring test).
# ===========================================================================


def _world_dir_with_poi(
    tmp_path: Path, *, on_r2: bool = True, with_keeper: bool = False
) -> Path:
    """Seed a minimal world with a single POI in history.yaml and an
    r2_manifest.json at the gate's discovery path (``pack_dir.parent.parent``,
    with ``pack_dir=tmp_path``). When ``on_r2`` the manifest carries the POI's
    verbatim landscape key; otherwise it is empty (authored-but-no-art)."""
    world_dir = tmp_path / "worlds" / "w"
    world_dir.mkdir(parents=True)
    history = (
        "points_of_interest:\n"
        "  - name: The Salt Quay\n"
        "    slug: salt_quay\n"
        "    region: Harbor District\n"
        "    description: Brine-stained docks.\n"
    )
    if with_keeper:
        history += "    secret: a cache is buried under the third piling\n"
    (world_dir / "history.yaml").write_text(history, encoding="utf-8")

    # The gate (``_gate_poi_slugs_on_manifest``) discovers ``r2_manifest.json``
    # at ``pack_dir.parent.parent`` and fails loud on its absence for a
    # POI-bearing world (No Silent Fallbacks). Seed it with — or without — the
    # verbatim landscape key.
    from sidequest.server.reference_renderer import load_r2_manifest_keys

    manifest_path = tmp_path.parent.parent / "r2_manifest.json"
    if on_r2:
        manifest_path.write_text(
            '[{"key": "genre_packs/p/worlds/w/assets/poi/salt_quay.png"}]',
            encoding="utf-8",
        )
    else:
        manifest_path.write_text("[]", encoding="utf-8")
    load_r2_manifest_keys.cache_clear()
    return world_dir


def test_lore_projection_includes_poi_section(tmp_path: Path):
    world_dir = _world_dir_with_poi(tmp_path, on_r2=True)
    doc = build_lore_projection("p", "w", pack_dir=tmp_path, world_dir=world_dir)
    section_ids = [s["id"] for s in doc["sections"]]
    assert "poi" in section_ids, "the assembled lore document must carry a poi section"
    poi = next(s for s in doc["sections"] if s["id"] == "poi")
    assert [m["name"] for m in poi["entries"]] == ["The Salt Quay"]
    quay = poi["entries"][0]
    assert "genre_packs/p/worlds/w/assets/poi/salt_quay.png" in quay["image_url"]


def test_lore_projection_poi_after_map_section(tmp_path: Path):
    # AC5: the POI section is appended AFTER the map section. (This world authors
    # no cartography, so map is absent and poi is simply present; when both
    # exist, poi must follow map — assert the ordering invariant holds whenever
    # a map section is present.)
    world_dir = _world_dir_with_poi(tmp_path, on_r2=True)
    doc = build_lore_projection("p", "w", pack_dir=tmp_path, world_dir=world_dir)
    section_ids = [s["id"] for s in doc["sections"]]
    assert "poi" in section_ids
    if "map" in section_ids:
        assert section_ids.index("poi") > section_ids.index("map"), (
            "the POI section must be appended after the map section (AC5)"
        )


def test_lore_projection_omits_poi_when_no_art_on_r2(tmp_path: Path):
    # A world that authors POIs but has NO landscape art on R2 → exclusion model
    # collapses the section to None → no poi section in the document.
    world_dir = _world_dir_with_poi(tmp_path, on_r2=False)
    doc = build_lore_projection("p", "w", pack_dir=tmp_path, world_dir=world_dir)
    section_ids = [s["id"] for s in doc["sections"]]
    assert "poi" not in section_ids, (
        "a POI-bearing world with no landscape art on R2 omits the POI section"
    )


def test_lore_projection_omits_poi_when_no_history(tmp_path: Path):
    world_dir = tmp_path / "worlds" / "w"
    world_dir.mkdir(parents=True)
    doc = build_lore_projection("p", "w", pack_dir=tmp_path, world_dir=world_dir)
    section_ids = [s["id"] for s in doc["sections"]]
    assert "poi" not in section_ids


def test_lore_projection_poi_keeper_field_never_crosses(tmp_path: Path):
    world_dir = _world_dir_with_poi(tmp_path, on_r2=True, with_keeper=True)
    doc = build_lore_projection("p", "w", pack_dir=tmp_path, world_dir=world_dir)
    blob = _json.dumps(doc)
    assert "a cache is buried under the third piling" not in blob
    poi = next(s for s in doc["sections"] if s["id"] == "poi")
    assert "secret" not in {k for m in poi["entries"] for k in m}
