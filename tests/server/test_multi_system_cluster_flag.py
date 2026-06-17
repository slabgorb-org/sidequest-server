"""Story 104-1 (M-A) — server multi-system ``is_cluster`` flag (RED).

Spec: docs/superpowers/specs/2026-06-11-space-opera-map-playtest-addendum.md §5
Story M-A, as amended live by the operator (2026-06-11): the single-vs-cluster
signal is a **system COUNT**, not the mere presence of a ``systems/`` dir. Under
the unified model every space-opera world declares its systems; a world with one
system collapses to its single orrery, a world with more than one is a cluster.

    is_cluster := (system_count > 1)

Count precedence (the detection contract these tests pin):
  1. a sector graph (``*.sector.json`` with a ``system`` node dict) → count its
     system nodes. This is perseus_cloud's authoritative source — its
     ``systems/`` dir is intentionally sparse (only ``yula.yaml`` authored; the
     rest are Diamonds-and-Coal, regenerable from the sector graph), so counting
     ``systems/*.yaml`` files would falsely read perseus as single. The sector
     graph is authoritative when present.
  2. else a ``systems/`` dir → count its ``*.yaml`` entries (the one-entry file
     coyote_star / aureate_span get is count==1 → single; add a second entry and
     they become a cluster with no code change).
  3. else (no sector graph, no ``systems/`` dir) → a definite single system
     (count 1, ``is_cluster=False``). This is an explicit classification, NOT a
     silent fallback / "unknown" (spec AC4).

The flag rides BOTH payloads:
  * the in-game MAP_UPDATE ``cartography`` dict (``_build_cartography_map_message``),
    read from the loader-cached ``World.is_cluster``;
  * the session-free reference lore ``map`` section (``build_lore_map_section`` /
    ``build_lore_projection``), detected on disk.

A decision span (``sidequest.cartography.cluster_detected``) carries world +
signal source + system_count + result so the GM panel can verify the flag (spec
AC2; CLAUDE.md OTEL principle).

The real-world truth table (perseus_cloud→cluster, coyote_star/aureate_span→
single) is a CONTENT invariant and belongs in the pack validator, not here —
these tests drive the engine with synthetic fixtures only.
"""

from __future__ import annotations

import json
from pathlib import Path

from sidequest.genre.models.world import CartographyConfig, NavigationMode, Region
from sidequest.server.reference_projection import build_lore_map_section, build_lore_projection

# The decision span name the GM panel keys on (spec AC2). Pinned here as the
# wire contract; if Dev relocates it, this constant moves with it.
CLUSTER_DECISION_SPAN = "sidequest.cartography.cluster_detected"

_CARTOGRAPHY_YAML = (
    "starting_region: harbor\n"
    "regions:\n"
    "  harbor: {name: The Harbor, summary: Salt docks., description: Fog and hulls., adjacent: [market]}\n"
    "  market: {name: Night Market, summary: Lit stalls., description: Spice and smoke., adjacent: [harbor]}\n"
)


def _world_dir(tmp_path: Path, slug: str = "w") -> Path:
    world_dir = tmp_path / "worlds" / slug
    world_dir.mkdir(parents=True)
    (world_dir / "cartography.yaml").write_text(_CARTOGRAPHY_YAML, encoding="utf-8")
    return world_dir


def _write_sector_graph(world_dir: Path, *, system_count: int) -> None:
    """Write a ``*.sector.json`` whose ``system`` dict has ``system_count`` nodes."""
    systems = {f"sys{i:02d}": {"name": f"System {i}"} for i in range(system_count)}
    (world_dir / f"{world_dir.name}.sector.json").write_text(
        json.dumps({"system": systems, "routes": {}}), encoding="utf-8"
    )


def _write_systems_dir(world_dir: Path, *, file_count: int) -> None:
    """Write a ``systems/`` dir with ``file_count`` per-system orrery files."""
    systems_dir = world_dir / "systems"
    systems_dir.mkdir()
    for i in range(file_count):
        (systems_dir / f"sys{i:02d}.yaml").write_text(
            f"bodies:\n  sys{i:02d}:\n    type: star\n", encoding="utf-8"
        )


def _map_section(doc: dict) -> dict:
    return next(s for s in doc["sections"] if s["id"] == "map")


# ---------------------------------------------------------------------------
# AC1 — reference map section carries is_cluster, keyed on system count
# ---------------------------------------------------------------------------


def test_cluster_flag_true_for_multi_system_sector_graph(tmp_path: Path):
    """A world whose sector graph has >1 system node → is_cluster True."""
    world_dir = _world_dir(tmp_path)
    _write_sector_graph(world_dir, system_count=3)

    doc = build_lore_projection("p", "w", pack_dir=tmp_path, world_dir=world_dir)
    section = _map_section(doc)

    assert section["is_cluster"] is True, (
        f"a multi-system world (3 sector-graph systems) must flag is_cluster=True; "
        f"got {section.get('is_cluster')!r}"
    )


def test_cluster_flag_false_for_single_entry_systems_dir(tmp_path: Path):
    """A world with a one-entry systems/ dir (coyote/aureate shape) → False.

    This is the growth affordance: count==1 today, add a second entry later and
    the same code flips it to a cluster.
    """
    world_dir = _world_dir(tmp_path)
    _write_systems_dir(world_dir, file_count=1)

    doc = build_lore_projection("p", "w", pack_dir=tmp_path, world_dir=world_dir)
    section = _map_section(doc)

    assert section["is_cluster"] is False, (
        f"a single-entry systems/ dir is one system → is_cluster=False; "
        f"got {section.get('is_cluster')!r}"
    )


def test_cluster_flag_false_when_no_systems_and_no_sector(tmp_path: Path):
    """No systems/ dir and no sector graph → a DEFINITE single system.

    Spec AC4 — absence is a definite single-system classification, never an
    "unknown". The key must be present and explicitly False (the UI reads it
    unconditionally), not omitted / None.
    """
    world_dir = _world_dir(tmp_path)

    doc = build_lore_projection("p", "w", pack_dir=tmp_path, world_dir=world_dir)
    section = _map_section(doc)

    assert "is_cluster" in section, (
        "the map section must always carry is_cluster (no silent omission); "
        f"keys were {sorted(section)}"
    )
    assert section["is_cluster"] is False, (
        f"absence of systems/ + sector graph is a definite single system → False, "
        f"never None/unknown; got {section['is_cluster']!r}"
    )


def test_cluster_count_comes_from_sector_graph_not_systems_file_count(tmp_path: Path):
    """PARANOID (the perseus trap): a sparse systems/ dir + multi-node sector graph.

    perseus_cloud authors only ``yula.yaml`` under systems/ but its sector graph
    has many systems. Counting systems/ FILES would read it as single (1 file)
    and ship the wrong flag. The sector graph is authoritative when present.
    """
    world_dir = _world_dir(tmp_path)
    _write_systems_dir(world_dir, file_count=1)  # only one orrery authored...
    _write_sector_graph(world_dir, system_count=4)  # ...but four systems exist

    doc = build_lore_projection("p", "w", pack_dir=tmp_path, world_dir=world_dir)
    section = _map_section(doc)

    assert section["is_cluster"] is True, (
        "is_cluster must count sector-graph systems, NOT systems/ files; a world "
        "with 1 authored orrery file but a 4-system sector graph is a cluster"
    )


def test_build_lore_map_section_carries_is_cluster_directly():
    """``build_lore_map_section`` itself surfaces the flag (it is the section
    builder M-C will consume), not only the full-projection wrapper."""
    cart = CartographyConfig.model_validate(
        {
            "starting_region": "harbor",
            "regions": {
                "harbor": {
                    "name": "The Harbor",
                    "summary": "Docks.",
                    "description": "Fog.",
                    "adjacent": ["market"],
                },
                "market": {
                    "name": "Night Market",
                    "summary": "Stalls.",
                    "description": "Smoke.",
                    "adjacent": ["harbor"],
                },
            },
        }
    )
    section = build_lore_map_section(
        cart, pack="p", world="w", portrait_on_r2_slugs=frozenset(), is_cluster=True
    )
    assert section["is_cluster"] is True


# ---------------------------------------------------------------------------
# AC2 — OTEL decision span
# ---------------------------------------------------------------------------


def test_cluster_decision_emits_otel_span(tmp_path: Path, otel_capture):
    """The cluster decision must be observable on the GM panel (spec AC2): a span
    carrying world, signal source, system_count, and the result."""
    world_dir = _world_dir(tmp_path)
    _write_sector_graph(world_dir, system_count=3)

    build_lore_projection("p", "w", pack_dir=tmp_path, world_dir=world_dir)

    spans = [s for s in otel_capture.get_finished_spans() if s.name == CLUSTER_DECISION_SPAN]
    assert spans, (
        f"the cluster decision must fire a {CLUSTER_DECISION_SPAN} span; "
        f"got {[s.name for s in otel_capture.get_finished_spans()]}"
    )
    attrs = dict(spans[-1].attributes or {})
    assert attrs.get("is_cluster") is True, f"span must record the result; got {attrs}"
    assert attrs.get("system_count") == 3, f"span must record the system count; got {attrs}"
    # Signal source distinguishes sector_graph vs systems_dir vs none for the panel.
    assert attrs.get("signal_source") == "sector_graph", (
        f"span must record which signal drove the count; got {attrs}"
    )


def test_cluster_decision_span_records_single_system(tmp_path: Path, otel_capture):
    """The single-system decision is observable too — a silent skip on the
    common case would blind the GM panel to which worlds collapse."""
    world_dir = _world_dir(tmp_path)

    build_lore_projection("p", "w", pack_dir=tmp_path, world_dir=world_dir)

    spans = [s for s in otel_capture.get_finished_spans() if s.name == CLUSTER_DECISION_SPAN]
    assert spans, f"the single-system decision must ALSO fire {CLUSTER_DECISION_SPAN}"
    attrs = dict(spans[-1].attributes or {})
    assert attrs.get("is_cluster") is False
    assert attrs.get("signal_source") == "none", (
        f"no systems/ + no sector graph → signal_source 'none'; got {attrs}"
    )


# ---------------------------------------------------------------------------
# AC3 — in-game cartography payload carries the flag (supersedes regionCount>1)
# ---------------------------------------------------------------------------


def _ingame_payload(*, is_cluster_attr) -> dict:
    """Build the in-game MAP_UPDATE payload for a region-mode world whose loaded
    World reports ``is_cluster``. ``is_cluster_attr`` is a sentinel: pass the
    bool to set the attribute, or ``...`` (Ellipsis) to omit it entirely."""
    from types import SimpleNamespace

    from sidequest.server.session_helpers import _build_cartography_map_message

    cart = CartographyConfig(
        navigation_mode=NavigationMode.region,
        starting_region="edo",
        regions={
            "edo": Region(
                name="Edo", summary="Capital.", description="Capital.", adjacent=["hakone"]
            ),
            "hakone": Region(name="Hakone", summary="Pass.", description="Pass.", adjacent=["edo"]),
        },
    )
    world_kwargs = {"cartography": cart}
    if is_cluster_attr is not ...:
        world_kwargs["is_cluster"] = is_cluster_attr
    world_obj = SimpleNamespace(**world_kwargs)
    pack = SimpleNamespace(worlds={"burning_peace": world_obj})

    msg = _build_cartography_map_message(
        pack, "burning_peace", "edo", player_id="p1", discovered_regions=["edo"]
    )
    assert msg is not None, "region-mode world with a resolvable location must emit a payload"
    return msg.payload.cartography


def test_ingame_cartography_payload_carries_is_cluster_true():
    cart = _ingame_payload(is_cluster_attr=True)
    assert cart["is_cluster"] is True, (
        f"the in-game cartography payload must surface the multi-system flag; got {cart.get('is_cluster')!r}"
    )


def test_ingame_cartography_payload_carries_is_cluster_false():
    cart = _ingame_payload(is_cluster_attr=False)
    assert "is_cluster" in cart, (
        f"the in-game cartography payload must always carry is_cluster; keys were {sorted(cart)}"
    )
    assert cart["is_cluster"] is False


def test_ingame_payload_is_cluster_is_always_a_bool_never_missing():
    """The UI (M-B) reads ``cartography.is_cluster`` unconditionally to replace the
    ``regionCount > 1`` heuristic. Even a World the loader never classified must
    not ship a missing/None flag (no silent fallback)."""
    cart = _ingame_payload(is_cluster_attr=...)
    assert "is_cluster" in cart, (
        f"is_cluster must never be absent from the wire payload; keys were {sorted(cart)}"
    )
    assert isinstance(cart["is_cluster"], bool), (
        f"is_cluster must be a concrete bool, never None/unknown; got {cart['is_cluster']!r}"
    )
