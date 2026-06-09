"""RED-phase contract tests — Story 100-6.

Phase 1, **Rules page** slice of the reference-pages → React migration
(spec: ``docs/superpowers/specs/2026-06-08-reference-pages-react-migration-design.md``).

Stories 100-2 (generic-YAML) / 100-3 (Cast) / 100-4 (POI) / 100-5 (Timeline)
project public-only JSON that React renders for the **lore** page — a per-world
surface (``/reference/api/lore/{pack}/{world}``). This story adds the JSON
projection for the **Rules** page, the data-shaping analog of the HTML
``assemble_rules_page`` presenter in ``reference_renderer.py``.

The defining difference from every prior 100-* slice: the Rules page is
**per-PACK** (the genre-tier rulebook), not per-world. There is no ``{world}``
path segment, no ``world.yaml``, no cartography/cast/POI/timeline — just the
pack-tier ``RULES_FILES`` (archetypes, classes, rules, progression, magic,
power_tiers, achievements, equipment_tables, inventory, beat_vocabulary)
projected through the SAME ``build_generic_yaml_section`` node-tree firewall the
lore generic sections already use. So the projector is a thin pack-tier loop;
the security comes from reusing the proven ``classify()`` gate, not from new
allowlist code.

----------------------------------------------------------------------------
TEA decisions pinning this RED phase (logged as deviations in the session file —
read them before changing a test):

  * **The keeper rules fields ALREADY have carves** — unlike 100-5, which had to
    *add* a ``classify()`` KEEPER carve for ``related_tropes``, the rules-tier
    spoiler fields are already carved KEEPER in ``reference_visibility.py`` from
    prior stories:
        - ``("rules", ("confrontations","*","beats","*","narrator_hint"))``
          + the edge_config / resources ``narrator_hint`` variants — narrator-only
          escalation cues.
        - ``("power_tiers", ("*","*","npc"))`` — the narrator's NPC sizing for
          each class ability tier.
        - ``("beat_vocabulary", ("obstacles",))`` — narrator-only obstacle stats.
    So this story needs **no** ``classify()`` change. The firewall tests are
    therefore meaningful in a different way than 100-5: they guard against a Dev
    implementing the projector with a **raw splat** (``yaml.safe_load`` →
    ``JSONResponse``) that bypasses ``build_generic_yaml_section`` entirely. A raw
    splat leaks every keeper field; routing through the firewall does not. The
    non-vacuity is proven directly by ``test_raw_splat_would_leak_*`` (positive
    control: the keeper value IS in the raw YAML, so a naive impl would leak it).

  * **No live-pack assertions.** AC5 says "verify end-to-end against a live pack
    (e.g. space_opera)". The project rule "no assertions against live
    genre_packs" + the prod-rows-in-tests prohibition OVERRIDE the literal AC:
    the end-to-end test points the real FastAPI router at a SYNTHETIC tmp pack
    whose rules.yaml / power_tiers.yaml / beat_vocabulary.yaml mirror
    space_opera's real keeper-field SHAPES (confrontations→beats→narrator_hint,
    class→tiers→npc, obstacles subtree). This is exactly the pattern 100-2..100-5
    used (real slug names, synthetic tmp content). The keeper STRINGS asserted
    absent are lifted verbatim from space_opera so the shape is faithful.

----------------------------------------------------------------------------
Contract this RED phase pins (Dev implements — ``build_rules_projection`` and the
``/reference/api/rules/{pack}`` route do not exist yet, so the import fails and
every test below is RED):

  - ``build_rules_projection(pack: str, *, pack_dir: Path) -> dict``
    Projects the pack-tier ``RULES_FILES`` into a public-only document. Mirrors
    ``build_lore_projection`` but pack-tier (no ``world``). For each present
    ``RULES_FILES`` file (skipping ``EXCLUDED_FILES``), reads the YAML and
    projects it via ``build_generic_yaml_section``; sections with no public
    content are omitted. Document shape:
        {"schema_version": 1, "pack": str, "sections": [<section>, ...]}
    where each <section> is the ``build_generic_yaml_section`` node-tree dict
    ({"id": slug(stem), "label": humanized, "node": {...}}). NOTE: NO "world" key
    (pack-tier).

  - ``GET /reference/api/rules/{pack}`` (new route in ``reference_routes.py``) →
    ``build_rules_projection`` → ``JSONResponse``. Public (no session/auth).
    404 on an unknown pack (reuses ``_resolve_pack_dir``).

The member/section dict shape is a Dev/Architect call; if Dev lands a different
but equally public-only shape, the SECURITY assertions (no keeper value in the
JSON, public siblings survive) are the non-negotiable ones — do not weaken them
to fit a shape change. Shape-only assertions are flagged ``[shape]``.
"""

from __future__ import annotations

import json as _json
from pathlib import Path

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sidequest.server.reference_projection import (
    build_generic_yaml_section,
    # RED: this import fails until Dev adds the Rules-page projection builder.
    build_rules_projection,
)
from sidequest.server.reference_routes import create_reference_router
from sidequest.server.reference_slug import slugify

# ---------------------------------------------------------------------------
# Keeper strings — lifted verbatim from space_opera so the synthetic fixtures
# mirror real content. Each is a GM-only value that must NEVER cross the
# JSON boundary.
# ---------------------------------------------------------------------------
_KEEPER_NARRATOR_HINT = "Show the NPC weighing the player's words."
_KEEPER_POWER_TIER_NPC = "a junior officer fresh from the academy, still saluting people"
_KEEPER_OBSTACLE_STAT = "DC 18 vs Resolve — the airlock cycles in three rounds"

# Public siblings that MUST survive the projection (proves the section is not
# just emptied wholesale).
_PUBLIC_BEAT_LABEL = "Make Your Case"
_PUBLIC_BEAT_EFFECT = "opponent considers your argument"
_PUBLIC_POWER_TIER_PLAYER = "a uniform that still creases where the factory pressed it"


def _blob(obj: object) -> str:
    """Serialize to JSON with ``ensure_ascii=False`` so non-ASCII characters
    appear VERBATIM in the search string.

    Load-bearing for the keeper firewall: ``_KEEPER_OBSTACLE_STAT`` carries an
    em-dash (— U+2014). Under the ``json.dumps`` default ``ensure_ascii=True`` the
    em-dash escapes to ``\\u2014``, so a raw-substring ``KEEPER not in dumps(...)``
    check is VACUOUSLY True even when the field fully leaks (Reviewer round 1: the
    obstacle carve broke 0 tests when removed). ``ensure_ascii=False`` keeps the
    literal em-dash, so the substring check is sound for any keeper string,
    ASCII or not.
    """
    return _json.dumps(obj, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Fixture builders — synthetic YAML mirroring the real space_opera shapes.
# ---------------------------------------------------------------------------

_RULES_YAML = (
    "confrontations:\n"
    "  - type: negotiation\n"
    '    label: "Diplomatic Negotiation"\n'
    "    category: social\n"
    "    beats:\n"
    "      - id: persuade\n"
    f'        label: "{_PUBLIC_BEAT_LABEL}"\n'
    "        kind: strike\n"
    f'        effect: "{_PUBLIC_BEAT_EFFECT}"\n'
    f'        narrator_hint: "{_KEEPER_NARRATOR_HINT}"\n'
)

_POWER_TIERS_YAML = (
    "Officer:\n"
    "  - level_range: [1, 3]\n"
    "    label: ensign\n"
    f"    player: >-\n      {_PUBLIC_POWER_TIER_PLAYER}\n"
    f"    npc: >-\n      {_KEEPER_POWER_TIER_NPC}\n"
)

_BEAT_VOCABULARY_YAML = (
    "moves:\n"
    "  - id: press_forward\n"
    '    label: "Press Forward"\n'
    "obstacles:\n"
    "  - id: airlock\n"
    '    name: "Sealing Airlock"\n'
    f'    stat_check: "{_KEEPER_OBSTACLE_STAT}"\n'
)


def _seed_pack(root: Path, pack: str = "space_opera") -> Path:
    """Write a synthetic pack dir carrying the three keeper-bearing rules files
    plus a benign classes.yaml. Returns the pack dir."""
    pack_dir = root / pack
    pack_dir.mkdir(parents=True)
    (pack_dir / "rules.yaml").write_text(_RULES_YAML, encoding="utf-8")
    (pack_dir / "power_tiers.yaml").write_text(_POWER_TIERS_YAML, encoding="utf-8")
    (pack_dir / "beat_vocabulary.yaml").write_text(_BEAT_VOCABULARY_YAML, encoding="utf-8")
    (pack_dir / "classes.yaml").write_text(
        "Officer:\n  description: Commands a ship and crew.\n", encoding="utf-8"
    )
    return pack_dir


def _client(root: Path) -> TestClient:
    """Real FastAPI app with the production reference router, pointed at a tmp
    pack root."""
    app = FastAPI()
    app.state.genre_pack_search_paths = [str(root)]
    app.include_router(create_reference_router())
    return TestClient(app)


# ===========================================================================
# Group 1 — Projector basics: the document envelope, public fields, omission.
# ===========================================================================


def test_rules_projection_envelope_shape(tmp_path: Path):
    pack_dir = _seed_pack(tmp_path)
    doc = build_rules_projection("space_opera", pack_dir=pack_dir)
    # [shape] pack-tier envelope.
    assert doc["schema_version"] == 1
    assert doc["pack"] == "space_opera"
    assert isinstance(doc["sections"], list)
    # [shape] pack-tier doc carries NO world key (the Rules page is per-pack).
    assert "world" not in doc, "the Rules projection is per-pack — it must not carry a world key"
    for section in doc["sections"]:
        assert "id" in section, "[shape] every section must carry an id"


def test_rules_projection_includes_rules_files_sections(tmp_path: Path):
    pack_dir = _seed_pack(tmp_path)
    doc = build_rules_projection("space_opera", pack_dir=pack_dir)
    ids = {s["id"] for s in doc["sections"]}
    # Each seeded RULES_FILES file projects to its slugified-stem section.
    assert slugify("rules") in ids
    assert slugify("power_tiers") in ids
    assert slugify("beat_vocabulary") in ids
    assert slugify("classes") in ids


def test_rules_projection_public_fields_survive(tmp_path: Path):
    pack_dir = _seed_pack(tmp_path)
    doc = build_rules_projection("space_opera", pack_dir=pack_dir)
    blob = _blob(doc)
    # Public beat fields cross; public power-tier player prose crosses.
    assert _PUBLIC_BEAT_LABEL in blob
    assert _PUBLIC_BEAT_EFFECT in blob
    assert _PUBLIC_POWER_TIER_PLAYER in blob


def test_rules_projection_omits_absent_files(tmp_path: Path):
    # An empty pack (no RULES_FILES present) projects to an empty section list,
    # not an error — the projection is purely additive per present file.
    pack_dir = tmp_path / "space_opera"
    pack_dir.mkdir(parents=True)
    doc = build_rules_projection("space_opera", pack_dir=pack_dir)
    assert doc["pack"] == "space_opera"
    assert doc["sections"] == []


# ===========================================================================
# Group 2 — Keeper firewall: no keeper rules field crosses the JSON boundary.
#           The carves already exist in classify(); these tests guard that the
#           projector ROUTES THROUGH it (vs. a raw splat that bypasses it).
#           Load-bearing. Do not weaken.
# ===========================================================================


def test_generic_yaml_rules_blocks_narrator_hint():
    # Isolate the classify() gate for the rules stem: a confrontations→beats→
    # narrator_hint tree must drop the keeper field while keeping public siblings.
    data = yaml.safe_load(_RULES_YAML)
    section = build_generic_yaml_section(data, file_stem="rules", pack="space_opera", world="")
    assert section is not None
    blob = _blob(section)
    assert _KEEPER_NARRATOR_HINT not in blob, (
        "rules.yaml confrontations.*.beats.*.narrator_hint is narrator-only — "
        "it must not cross via the generic-YAML rules path (classify() KEEPER)"
    )
    assert _PUBLIC_BEAT_LABEL in blob
    assert _PUBLIC_BEAT_EFFECT in blob


def test_generic_yaml_power_tiers_blocks_npc():
    data = yaml.safe_load(_POWER_TIERS_YAML)
    section = build_generic_yaml_section(
        data, file_stem="power_tiers", pack="space_opera", world=""
    )
    assert section is not None
    blob = _blob(section)
    assert _KEEPER_POWER_TIER_NPC not in blob, (
        "power_tiers.*.*.npc is the narrator's NPC sizing — it must not cross "
        "via the generic-YAML power_tiers path (classify() KEEPER)"
    )
    assert _PUBLIC_POWER_TIER_PLAYER in blob


def test_generic_yaml_beat_vocabulary_blocks_obstacles():
    data = yaml.safe_load(_BEAT_VOCABULARY_YAML)
    section = build_generic_yaml_section(
        data, file_stem="beat_vocabulary", pack="space_opera", world=""
    )
    # The obstacles subtree is wholly KEEPER; the public `moves` subtree survives,
    # so the section is not None.
    assert section is not None
    blob = _blob(section)
    assert _KEEPER_OBSTACLE_STAT not in blob, (
        "beat_vocabulary.obstacles is narrator-only obstacle stats — it must not "
        "cross via the generic-YAML beat_vocabulary path (classify() KEEPER)"
    )
    assert "Press Forward" in blob, "the public moves subtree must still project"


def test_rules_projection_whole_doc_no_keeper_leak(tmp_path: Path):
    # Whole-document firewall: NONE of the three keeper values survive ANY section.
    pack_dir = _seed_pack(tmp_path)
    doc = build_rules_projection("space_opera", pack_dir=pack_dir)
    blob = _blob(doc)
    assert _KEEPER_NARRATOR_HINT not in blob
    assert _KEEPER_POWER_TIER_NPC not in blob
    assert _KEEPER_OBSTACLE_STAT not in blob
    # …while the public content still projects (the doc is not just empty).
    assert _PUBLIC_BEAT_LABEL in blob
    assert _PUBLIC_POWER_TIER_PLAYER in blob


def test_raw_splat_would_leak_narrator_hint_but_projection_does_not(tmp_path: Path):
    # Non-vacuity positive control. A naive raw-splat implementation
    # (yaml.safe_load → JSONResponse) WOULD leak the keeper field: prove the
    # field is genuinely in the source, then prove the firewalled projection
    # scrubs it. This is what makes the firewall assertions meaningful even
    # though the classify() carves pre-exist.
    pack_dir = _seed_pack(tmp_path)
    raw_blob = _blob(yaml.safe_load((pack_dir / "rules.yaml").read_text()))
    assert _KEEPER_NARRATOR_HINT in raw_blob, (
        "sanity: the keeper field IS in the raw YAML, so a raw splat would leak it"
    )
    projected_blob = _blob(build_rules_projection("space_opera", pack_dir=pack_dir))
    assert _KEEPER_NARRATOR_HINT not in projected_blob, (
        "the firewalled projection must scrub what the raw splat would leak"
    )


# ===========================================================================
# Group 3 — Wiring: the projection is reachable through the production HTTP
#           endpoint end-to-end (CLAUDE.md: every test suite needs a wiring test
#           that hits a production code path, not just the unit-isolated builder).
# ===========================================================================


def test_rules_api_endpoint_returns_sections(tmp_path: Path):
    # The production HTTP path: GET /reference/api/rules/{pack} → rules_api →
    # build_rules_projection.
    _seed_pack(tmp_path)
    resp = _client(tmp_path).get("/reference/api/rules/space_opera")
    assert resp.status_code == 200
    doc = resp.json()
    assert doc["schema_version"] == 1
    assert doc["pack"] == "space_opera"
    ids = {s["id"] for s in doc["sections"]}
    assert slugify("rules") in ids, (
        "the Rules projection must be reachable through the production "
        "/reference/api/rules endpoint"
    )


def test_rules_api_404_unknown_pack(tmp_path: Path):
    resp = _client(tmp_path).get("/reference/api/rules/no_such_pack")
    assert resp.status_code == 404


def test_rules_api_endpoint_scrubs_keeper_fields(tmp_path: Path):
    # AC5 (firewall end-to-end): the production endpoint, pointed at a synthetic
    # pack mirroring space_opera's keeper-field shapes, returns JSON with EVERY
    # keeper value scrubbed and the public siblings intact.
    _seed_pack(tmp_path)
    resp = _client(tmp_path).get("/reference/api/rules/space_opera")
    assert resp.status_code == 200
    blob = _blob(resp.json())
    assert _KEEPER_NARRATOR_HINT not in blob, "narrator_hint leaked through the live HTTP path"
    assert _KEEPER_POWER_TIER_NPC not in blob, "power_tiers npc leaked through the live HTTP path"
    assert _KEEPER_OBSTACLE_STAT not in blob, "obstacle stat leaked through the live HTTP path"
    assert _PUBLIC_BEAT_LABEL in blob
    assert _PUBLIC_POWER_TIER_PLAYER in blob
