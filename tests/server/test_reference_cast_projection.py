"""RED-phase contract tests — Story 100-3.

Phase 1, **Cast section** slice of the reference-pages → React migration
(spec: ``docs/superpowers/specs/2026-06-08-reference-pages-react-migration-design.md``).

Slice A (map, PR #762) ships ``build_lore_map_section`` and Slice B (100-2,
PR #764) ships ``build_generic_yaml_section``; both project public-only JSON
that React renders. This story adds the **Cast** section: the JSON projection
of the named NPC cast (``portrait_manifest.yaml``), the data-shaping analog of
the HTML ``present_lore_cast`` presenter in ``reference_presenters.py``.

Two firewalls gate this section — and BOTH are load-bearing security
invariants per spec C1 ("no keeper field crosses the JSON boundary"):

  1. **is_projectable gate** (ADR-138 §D4 / Story 75-13). An NPC entry that is
     NOT ratified (``observation_pending``) is a phantom the world has not
     committed to — it must be withheld from the public projection, exactly as
     the HTML path withholds it via ``_cast_entry_is_projectable`` →
     :func:`sidequest.game.npc_pool.is_projectable`. The shared single-source
     predicate must be REUSED, not re-derived.

  2. **R2 portrait resolution, server-side.** The portrait URL is resolved on
     the server (via ``resolve_asset_url`` over the world-scoped
     ``portrait_image_key``) and emitted as a fully-formed URL string — the
     client never sees a raw R2 key, manifest path, or filesystem path, and
     never builds the URL itself. An NPC whose portrait is not on R2 projects a
     ``portrait_url`` of ``None`` (text-only card downstream), never a broken
     link.

Contract this RED phase pins (Dev implements — these symbols do not exist yet,
so the import fails and every test below is RED):

  - ``build_cast_section(entries, *, pack, world, portrait_on_r2_slugs) -> dict | None``
    Projects the RATIFIED NPC cast into the public ``cast`` section dict, or
    ``None`` when no projectable member survives. Mirrors
    ``build_lore_map_section``'s signature (a pre-gated R2 slug set is passed
    in; the function does not load the manifest itself). Section shape:
        {"id": "cast", "label": "Cast",
         "members": [{"slug": str, "name": str, "role": str | None,
                      "appearance": str | None, "portrait_url": str | None}, ...]}

  - ``build_lore_projection`` (extended) appends a ``cast`` section built from
    ``load_cast_entries`` + the ``is_projectable`` gate + the R2 portrait gate,
    so the assembled lore document carries the Cast section.

The member-dict shape itself is a Dev/Architect call; if Dev lands a different
but equally public-only shape, the SECURITY assertions (no keeper field in the
JSON, ``is_projectable`` actually gates membership, portrait URL resolved
server-side never raw) are the non-negotiable ones — do not weaken them to fit
a shape change. Shape-only assertions are flagged ``[shape]``.
"""

from __future__ import annotations

import json as _json
from pathlib import Path

# RED: this import fails until Dev adds the Cast-section projection builder.
from sidequest.server.reference_projection import (
    build_cast_section,
    build_lore_projection,
)
from sidequest.telemetry.spans.reference import (
    SPAN_REFERENCE_NPC_UNRATIFIED_SKIPPED,
    SPAN_REFERENCE_PORTRAIT_NOT_FOUND,
    SPAN_REFERENCE_PORTRAIT_RESOLVED,
)
from tests.server.conftest import span_attrs_by_name

# ---------------------------------------------------------------------------
# Fixtures — portrait_manifest.yaml entry dicts (the public Cast source).
# ``observation_pending`` is the ratification flag; absent/False == ratified.
# ---------------------------------------------------------------------------


def _ratified_entry(name: str, **extra: object) -> dict:
    return {"name": name, **extra}


def _cast_entries() -> list[dict]:
    return [
        _ratified_entry(
            "Old Sten",
            role="Dockmaster",
            appearance="A weathered man with salt-grey stubble.",
        ),
        _ratified_entry(
            "Mara Quill",
            role="Smuggler",
            appearance="Lean, quick-eyed, always near the exit.",
        ),
    ]


def _member_by_name(section: dict, name: str) -> dict:
    return next(m for m in section["members"] if m["name"] == name)


# ===========================================================================
# Group 1 — Ratified members project; the is_projectable gate is the door.
# ===========================================================================


def test_ratified_npc_appears_in_output():
    section = build_cast_section(
        _cast_entries(),
        pack="p",
        world="w",
        portrait_on_r2_slugs=frozenset(),
    )
    assert section is not None
    # [shape] section envelope.
    assert section["id"] == "cast"
    assert section["label"] == "Cast"
    names = {m["name"] for m in section["members"]}
    assert names == {"Old Sten", "Mara Quill"}
    sten = _member_by_name(section, "Old Sten")
    assert sten["role"] == "Dockmaster"
    assert sten["appearance"] == "A weathered man with salt-grey stubble."


def test_unratified_npc_is_excluded():
    # observation_pending == True means the world has NOT committed to this NPC
    # (an auto-minted phantom). is_projectable() must withhold it from the
    # public projection — the load-bearing ADR-138 §D4 gate (Story 75-13 parity).
    entries = [
        _ratified_entry("Old Sten", role="Dockmaster"),
        _ratified_entry("Phantom Whisper", role="???", observation_pending=True),
    ]
    section = build_cast_section(entries, pack="p", world="w", portrait_on_r2_slugs=frozenset())
    assert section is not None
    names = {m["name"] for m in section["members"]}
    assert "Old Sten" in names
    assert "Phantom Whisper" not in names, (
        "unratified (observation_pending) NPC must be withheld by the "
        "is_projectable gate — it must never cross the public JSON boundary"
    )
    # The phantom's data must not survive anywhere in the JSON.
    blob = _json.dumps(section)
    assert "Phantom Whisper" not in blob
    assert "???" not in blob


def test_quoted_string_observation_pending_still_withholds():
    # A quoted-string authoring slip (observation_pending: "true") must STILL
    # withhold — bool("true") is truthy but so is bool("false"), so the gate
    # cannot lean on Python truthiness. The HTML path (_cast_entry_is_projectable)
    # routes this through pydantic coercion; the JSON path must match.
    entries = [_ratified_entry("Phantom", observation_pending="true")]
    section = build_cast_section(entries, pack="p", world="w", portrait_on_r2_slugs=frozenset())
    # Only the unratified entry exists → nothing projectable → None.
    assert section is None or all(m["name"] != "Phantom" for m in section["members"])


def test_all_unratified_projects_to_none():
    entries = [
        _ratified_entry("Phantom A", observation_pending=True),
        _ratified_entry("Phantom B", observation_pending=True),
    ]
    section = build_cast_section(entries, pack="p", world="w", portrait_on_r2_slugs=frozenset())
    assert section is None, "no projectable member → omit the Cast section (None)"


def test_empty_entries_projects_to_none():
    assert build_cast_section([], pack="p", world="w", portrait_on_r2_slugs=frozenset()) is None


def test_entry_without_name_is_skipped():
    # An entry with no usable name is not a renderable card (parity with
    # present_lore_cast, which skips empty-name entries).
    entries = [_ratified_entry("Old Sten"), {"role": "Nobody", "appearance": "blur"}]
    section = build_cast_section(entries, pack="p", world="w", portrait_on_r2_slugs=frozenset())
    assert section is not None
    assert [m["name"] for m in section["members"]] == ["Old Sten"]
    assert "Nobody" not in _json.dumps(section)


# ===========================================================================
# Group 2 — R2 portrait URL resolved SERVER-SIDE (load-bearing). The client
#           gets a finished URL or null — never a raw key/path.
# ===========================================================================


def test_portrait_url_resolved_when_on_r2():
    # Old Sten's portrait slug (slugify_player_name) is on R2 → portrait_url is a
    # fully-resolved URL the client can <img src> directly.
    section = build_cast_section(
        _cast_entries(),
        pack="p",
        world="w",
        portrait_on_r2_slugs=frozenset({"old_sten"}),
    )
    sten = _member_by_name(section, "Old Sten")
    assert sten["portrait_url"] is not None
    url = sten["portrait_url"]
    # Server-side resolution: the value is a URL, not a raw R2 object key. The
    # default asset base is an absolute CDN URL; the resolver wraps the key.
    assert isinstance(url, str)
    assert url.startswith("http"), f"portrait_url must be a resolved URL, got {url!r}"
    # The world-scoped key segment is embedded in the resolved URL.
    assert "genre_packs/p/worlds/w/assets/portraits/old_sten.png" in url
    # A member not on R2 carries a null portrait, never a broken link.
    mara = _member_by_name(section, "Mara Quill")
    assert mara["portrait_url"] is None


def test_portrait_url_is_null_when_not_on_r2():
    section = build_cast_section(
        _cast_entries(),
        pack="p",
        world="w",
        portrait_on_r2_slugs=frozenset(),  # nothing on R2
    )
    for member in section["members"]:
        assert member["portrait_url"] is None


def test_client_never_sees_raw_r2_key_or_path():
    # The raw R2 object key form (no scheme) must NOT appear as a member value —
    # only the resolved URL. This is the "resolved server-side, not raw data
    # passed to client" invariant. We assert the portrait_url is the resolved
    # URL and that no member field carries the bare ".png" key without a scheme.
    section = build_cast_section(
        _cast_entries(),
        pack="p",
        world="w",
        portrait_on_r2_slugs=frozenset({"old_sten"}),
    )
    sten = _member_by_name(section, "Old Sten")
    # The resolved URL is present...
    assert sten["portrait_url"].startswith("http")
    # ...and the manifest gate slug set is an INPUT, never echoed as a field.
    assert "portrait_on_r2_slugs" not in sten
    assert "r2_manifest" not in _json.dumps(section)


def test_portrait_slug_uses_explicit_id_when_present():
    # cast_portrait_slug parity: an explicit `id` keys the portrait, while the
    # display `name` stays human. Gating on the id-derived slug must resolve the
    # portrait even when slugify(name) would differ.
    entries = [
        {
            "id": "witch_of_the_west",
            "name": "The Wicked Witch of the West",
            "role": "Antagonist",
        }
    ]
    section = build_cast_section(
        entries,
        pack="p",
        world="w",
        portrait_on_r2_slugs=frozenset({"witch_of_the_west"}),
    )
    member = section["members"][0]
    assert member["name"] == "The Wicked Witch of the West"  # human heading preserved
    assert member["slug"] == "witch_of_the_west"
    assert member["portrait_url"] is not None
    assert "witch_of_the_west.png" in member["portrait_url"]


# ===========================================================================
# Group 3 — Keeper firewall: no keeper field from NPC data crosses the JSON
#           boundary (spec C1). Load-bearing. Do not weaken.
# ===========================================================================


# Keeper-side NPC fields per ADR-138 / the C1 keeper list. These are authored
# alongside the public name/role/appearance but must NEVER reach the player.
_KEEPER_NPC_FIELDS = {
    "ocean": {"openness": 0.8, "neuroticism": 0.2},
    "initial_disposition": "secretly hostile",
    "history_seeds": ["betrayed the harbor guild in secret"],
    "distinguishing_features": "the brand of the smuggling ring on her wrist",
    "secret": "is the mole the party is hunting",
    "gm_notes": "reveal the double-cross only after the vault heist",
}


def test_keeper_npc_fields_never_cross_the_boundary():
    entry = _ratified_entry(
        "Mara Quill",
        role="Smuggler",
        appearance="Lean, quick-eyed.",
        **_KEEPER_NPC_FIELDS,
    )
    section = build_cast_section([entry], pack="p", world="w", portrait_on_r2_slugs=frozenset())
    assert section is not None
    member = section["members"][0]
    blob = _json.dumps(section)

    # No keeper KEY survives onto the projected member.
    for key in _KEEPER_NPC_FIELDS:
        assert key not in member, f"keeper field {key!r} leaked onto the Cast member"

    # No keeper VALUE survives anywhere in the JSON.
    assert "secretly hostile" not in blob
    assert "betrayed the harbor guild in secret" not in blob
    assert "the brand of the smuggling ring on her wrist" not in blob
    assert "is the mole the party is hunting" not in blob
    assert "reveal the double-cross only after the vault heist" not in blob
    assert "neuroticism" not in blob

    # The PUBLIC fields did survive (the section is not just empty).
    assert "Mara Quill" in blob
    assert "Smuggler" in blob
    assert "Lean, quick-eyed." in blob


def test_cast_member_carries_only_allowlisted_keys():
    # An allowlist projection (not a denylist) is the robust firewall: only the
    # known-public keys cross. This pins that the projected member dict does not
    # blindly splat the authored entry.
    entry = _ratified_entry(
        "Old Sten",
        role="Dockmaster",
        appearance="Salt-grey stubble.",
        secret="the mole",
        ocean={"openness": 0.9},
    )
    section = build_cast_section(
        [entry], pack="p", world="w", portrait_on_r2_slugs=frozenset({"old_sten"})
    )
    member = section["members"][0]
    # [shape] the public key set — reconcilable if Dev's allowlist differs, but
    # it must be a SUBSET that excludes every keeper field.
    assert set(member.keys()) <= {"slug", "name", "role", "appearance", "portrait_url"}
    assert "secret" not in member
    assert "ocean" not in member


# ===========================================================================
# Group 4 — is_projectable is REUSED, not reimplemented (story requirement,
#           mirrors the 100-2 classify()-reuse test).
# ===========================================================================


def test_is_projectable_is_the_gate_not_a_reimplementation(monkeypatch):
    """The Cast projection must consult the shared
    :func:`sidequest.game.npc_pool.is_projectable` predicate (via the module's
    ratification helper) rather than re-deriving the ratification rule. We patch
    the symbol the projection module imports and assert it is consulted.

    Catches a Dev who inlines ``not entry.get("observation_pending")`` instead
    of reusing the shipped gate — a security regression because the two would
    drift (the gate also handles quoted-string / null coercion).
    """

    # The projection module must reference is_projectable by name so the patch
    # lands. (Dev may import it directly or via _cast_entry_is_projectable from
    # reference_renderer, which itself calls npc_pool.is_projectable — either
    # way the npc_pool symbol is the single source of truth.)
    import sidequest.game.npc_pool as npc_pool

    real = npc_pool.is_projectable
    calls: list[object] = []

    def _spy(entity):
        calls.append(entity)
        return real(entity)

    # Patch at the single-source module so whichever import path Dev uses, the
    # spy is hit (reference_renderer and reference_projection both ultimately
    # call npc_pool.is_projectable).
    monkeypatch.setattr(npc_pool, "is_projectable", _spy)

    build_cast_section(
        [
            _ratified_entry("Old Sten"),
            _ratified_entry("Phantom", observation_pending=True),
        ],
        pack="p",
        world="w",
        portrait_on_r2_slugs=frozenset(),
    )
    assert calls, "is_projectable() was never called — ratification gate not reused"


# ===========================================================================
# Group 5 — OTEL wiring: the portrait gate + ratification gate fire spans
#           (CLAUDE.md OTEL Observability Principle; every subsystem decision
#           emits a span so the GM/dev panel is the lie-detector).
# ===========================================================================


def test_resolved_portrait_fires_resolved_span(otel_capture) -> None:
    build_cast_section(
        _cast_entries(),
        pack="p",
        world="w",
        portrait_on_r2_slugs=frozenset({"old_sten"}),
    )
    resolved = span_attrs_by_name(otel_capture, SPAN_REFERENCE_PORTRAIT_RESOLVED)
    assert resolved, "an on-R2 portrait must fire a portrait_resolved span"
    assert any(a.get("slug") == "old_sten" for a in resolved)


def test_missing_portrait_fires_not_found_span(otel_capture) -> None:
    build_cast_section(
        _cast_entries(),
        pack="p",
        world="w",
        portrait_on_r2_slugs=frozenset(),  # nothing on R2
    )
    not_found = span_attrs_by_name(otel_capture, SPAN_REFERENCE_PORTRAIT_NOT_FOUND)
    assert not_found, "an authored-but-not-on-R2 portrait must fire portrait_not_found"


# ===========================================================================
# Group 6 — Wiring: the Cast section appears in the full lore document, built
#           from portrait_manifest.yaml end-to-end (CLAUDE.md: every test suite
#           needs a wiring test).
# ===========================================================================


def _world_dir_with_cast(tmp_path: Path, *, with_phantom: bool = False) -> Path:
    world_dir = tmp_path / "worlds" / "w"
    world_dir.mkdir(parents=True)
    manifest = (
        "characters:\n"
        "  - name: Old Sten\n"
        "    role: Dockmaster\n"
        "    appearance: Salt-grey stubble.\n"
        "    secret: is the harbor mole\n"  # keeper field — must not project
    )
    if with_phantom:
        manifest += (
            "  - name: Phantom Whisper\n"
            "    role: '???'\n"
            "    observation_pending: true\n"  # unratified — must be withheld
        )
    (world_dir / "portrait_manifest.yaml").write_text(manifest, encoding="utf-8")
    # The portrait gate (``_gate_cast_slugs_on_manifest``) discovers
    # ``r2_manifest.json`` at ``pack_dir.parent.parent`` and fails loud on its
    # absence (Cast-bearing world; No Silent Fallbacks). These wiring tests pass
    # ``pack_dir=tmp_path`` and assert text-only Cast cards, so seed an empty
    # manifest at the gate's discovery path (no portraits on R2).
    from sidequest.server.reference_renderer import load_r2_manifest_keys

    manifest_path = tmp_path.parent.parent / "r2_manifest.json"
    manifest_path.write_text("[]", encoding="utf-8")
    load_r2_manifest_keys.cache_clear()
    return world_dir


def test_lore_projection_includes_cast_section(tmp_path: Path):
    world_dir = _world_dir_with_cast(tmp_path)
    doc = build_lore_projection("p", "w", pack_dir=tmp_path, world_dir=world_dir)
    section_ids = [s["id"] for s in doc["sections"]]
    assert "cast" in section_ids, "the assembled lore document must carry a cast section"
    cast = next(s for s in doc["sections"] if s["id"] == "cast")
    assert [m["name"] for m in cast["members"]] == ["Old Sten"]
    # The keeper field never crosses into the assembled document.
    blob = _json.dumps(doc)
    assert "is the harbor mole" not in blob
    assert "secret" not in {k for m in cast["members"] for k in m}


def test_lore_projection_cast_withholds_unratified(tmp_path: Path):
    world_dir = _world_dir_with_cast(tmp_path, with_phantom=True)
    doc = build_lore_projection("p", "w", pack_dir=tmp_path, world_dir=world_dir)
    blob = _json.dumps(doc)
    assert "Phantom Whisper" not in blob
    assert "???" not in blob
    cast = next((s for s in doc["sections"] if s["id"] == "cast"), None)
    assert cast is not None
    assert [m["name"] for m in cast["members"]] == ["Old Sten"]


def test_lore_projection_omits_cast_when_no_manifest(tmp_path: Path):
    world_dir = tmp_path / "worlds" / "w"
    world_dir.mkdir(parents=True)
    doc = build_lore_projection("p", "w", pack_dir=tmp_path, world_dir=world_dir)
    section_ids = [s["id"] for s in doc["sections"]]
    assert "cast" not in section_ids


def test_lore_projection_cast_fires_unratified_skipped_span(tmp_path: Path, otel_capture) -> None:
    # ADR-138 §D4: a world that authors a Cast fires the unratified-skipped span
    # once, carrying the withheld count — the lie-detector that proves the gate
    # ran even when the count is non-zero.
    world_dir = _world_dir_with_cast(tmp_path, with_phantom=True)
    build_lore_projection("p", "w", pack_dir=tmp_path, world_dir=world_dir)
    spans = span_attrs_by_name(otel_capture, SPAN_REFERENCE_NPC_UNRATIFIED_SKIPPED)
    assert spans, "a Cast-bearing world must fire the npc_unratified_skipped span"
    assert any(a.get("reference.npc_unratified_skipped_count") == 1 for a in spans), (
        "the span must record the one withheld phantom (count == 1)"
    )
