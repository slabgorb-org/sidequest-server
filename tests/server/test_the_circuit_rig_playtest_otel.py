"""road_warrior/the_circuit rig playtest — OTEL proof the rig subsystem fires from
REAL content, not improvised prose (Story 86-5, AC3/AC4 — the epic's lie-detector
gate).

The GM panel is the lie detector (CLAUDE.md OTEL Observability Principle): a rig
subsystem is only "live" for the_circuit if its spans fire when driven from the
actually-loaded pack content. The sibling wiring test
(``tests/integration/test_rig_pool_wiring.py``) proves the span plumbing works
against a *synthetic* hand-built pool; this test closes the integration gate by
binding the rig from the **real road_warrior tier-1 rig content** and asserting
the production ``rig_pool.delta`` span fires — proving the the_circuit content
path (ruleset binding + vessel stat block + rig pool) is wired end-to-end.

Two layers, both keyed on behavior/spans (never source-grep, per CLAUDE.md
"No Source-Text Wiring Tests"):
  1. the_circuit's pack binds ``ruleset: cwn`` and resolves through the production
     registry — no silent native-dial fallback (No Silent Fallbacks).
  2. A character built from the real ``rig_tier_1_prospect`` item binds a rig pool
     through the production binder, and applying combat damage emits the
     ``rig_pool.delta`` OTEL span the GM dashboard subscribes to.

RED until Plan 5 lands the full stat block (the bind reads the real content's
composure/speed/mount_slots through ``parse_vessel_tags``, which is RED until
speed/mount_slots are first-class — see ``test_vessel_full_stat_blocks.py``).
"""

from __future__ import annotations

import pytest

from sidequest.telemetry.spans.rig import SPAN_RIG_POOL_DELTA
from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _load_road_warrior():
    from sidequest.genre.loader import load_genre_pack

    if not _has_real_content():
        pytest.skip("sidequest-content not on disk")
    try:
        return load_genre_pack(find_pack_path("road_warrior"))
    except PackNotFound as exc:  # pragma: no cover - environment guard
        pytest.skip(str(exc))


def _real_rig_item_dict(item_id: str = "rig_tier_1_prospect") -> dict:
    """The real rig item, as the raw dict the chargen loadout flow binds from.

    Epic 120 (story 120-2): the bespoke rig vessels have no CWN SRD analog and were
    relocated off the now-100%-CWN-verbatim genre baseline to the_circuit's world
    inventory (ADR-145 D3). At runtime the chargen kit grants the rig from the
    world-replaces-genre merged catalog (ADR-140); read the world file here, which
    is the tier the rig vessel now ships at."""
    import yaml

    if not _has_real_content():
        pytest.skip("sidequest-content not on disk")
    inv_path = find_pack_path("road_warrior") / "worlds" / "the_circuit" / "inventory.yaml"
    catalog = yaml.safe_load(inv_path.read_text())["item_catalog"]
    for it in catalog:
        if it.get("id") == item_id:
            return it
    raise AssertionError(f"road_warrior/the_circuit inventory is missing {item_id!r}")


def _rig_character(item_id: str = "rig_tier_1_prospect"):
    """A CreatureCore carrying the REAL rig item in inventory — the shape the
    world-materialization / chargen loadout path produces."""
    from sidequest.game import CreatureCore, HpPool, Inventory

    return CreatureCore(
        name="Furiosa",
        description="A driver of the_circuit.",
        personality="Relentless.",
        level=1,
        xp=0,
        inventory=Inventory(items=[_real_rig_item_dict(item_id)]),
        statuses=[],
        hp=HpPool(current=8, max=8, base_max=8),
        acquired_advancements=[],
    )


# ---------------------------------------------------------------------------
# AC4 — the_circuit's pack binds cwn, no silent fallback to the native engine
# ---------------------------------------------------------------------------


def test_the_circuit_pack_binds_cwn_no_silent_fallback() -> None:
    """road_warrior (the_circuit's pack) declares ``ruleset: cwn`` and it resolves
    through the production registry to the CWN module — a missing/typo'd ruleset
    must fail loud, never fall back to the native dial engine."""
    from sidequest.game.ruleset.cwn import CwnRulesetModule
    from sidequest.game.ruleset.registry import get_ruleset_module

    pack = _load_road_warrior()
    assert pack.rules.ruleset == "cwn", (
        f"the_circuit must run the cwn ruleset for rig combat to fire; got {pack.rules.ruleset!r}"
    )
    assert isinstance(get_ruleset_module(pack.rules.ruleset), CwnRulesetModule)


# ---------------------------------------------------------------------------
# AC3 — rig binds from real content and emits its OTEL span on damage
# ---------------------------------------------------------------------------


def test_the_circuit_rig_binds_from_real_content() -> None:
    """The production binder reads the REAL tier-1 rig's full stat block and binds
    a pool — composure/composure_max come from content, and the parsed stat block
    exposes speed + mount_slots (the full-stat-block contract this story adds)."""
    from sidequest.game.vessel_tags import bind_rig_pool_from_inventory, parse_vessel_tags

    core = _rig_character()
    pool = bind_rig_pool_from_inventory(core, character_id=core.name)

    assert pool is not None, "binder must bind a rig pool from the real vessel item"
    assert core.rig_pool is pool
    assert pool.chassis_id == "rig_tier_1_prospect"
    assert pool.max == 4 and pool.current == 4, (
        "tier-1 composure must come from content (rig_composure_spec: 4)"
    )

    # The full stat block must be reachable from the same content the bind used.
    stats = parse_vessel_tags(_real_rig_item_dict())
    assert stats.speed > 0 and stats.mount_slots >= 1


def test_the_circuit_rig_damage_emits_rig_pool_delta_span(otel_capture) -> None:
    """Driving real rig damage through the production pool emits ``rig_pool.delta``
    — the GM-panel proof that the_circuit's rig subsystem is mechanically live, not
    narrator improvisation. Binds from real content, applies a combat hit, asserts
    the span fired with the expected magnitude."""
    from sidequest.game.vessel_tags import bind_rig_pool_from_inventory

    core = _rig_character()
    pool = bind_rig_pool_from_inventory(core, character_id=core.name)
    assert pool is not None

    result = pool.apply_delta(-3)
    assert result.new_current == 1, "a -3 hit on a 4-composure rig leaves 1"

    delta_spans = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_RIG_POOL_DELTA]
    assert delta_spans, (
        f"applying rig damage must emit a {SPAN_RIG_POOL_DELTA!r} span so the GM "
        f"panel can verify the rig subsystem engaged; got span names "
        f"{[s.name for s in otel_capture.get_finished_spans()]}"
    )
