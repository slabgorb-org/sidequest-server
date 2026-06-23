"""Tests — Seam 1: faction/zone-scoped creature injection (story 157-2).

The headline fix for the gulliver bleed (session ``2026-06-20-gulliver-e721409c``):
a 4th-voyage **Yahoo** (faction ``the_houyhnhnm_assembly``) must NOT surface on
the 1st-voyage **Lilliput shore** — and MUST surface in Houyhnhnm-land.

Design: ``docs/superpowers/specs/2026-06-20-faction-zone-content-eligibility-design.md``
(ADR-059 amendment). Seam 1 = ``monster_manual_inject`` creature/encounter
injection. The active faction is resolved from the canonical region
(``snapshot.region_for`` → that region's ``controlled_by``), NOT the free-text
``current_location`` string the seam receives.

These tests drive the REAL public ``inject()`` seam and assert on emitted state
+ the ``zone_eligibility.filtered`` OTEL span (CLAUDE.md: fixture-driven behavior
+ span assertions — never grep production source for wiring).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.monster_manual import EntryState, ManualEncounter, MonsterManual
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.models.world import CartographyConfig, Region
from sidequest.server.dispatch import monster_manual_inject
from sidequest.telemetry.spans import FLAT_ONLY_SPANS
from sidequest.telemetry.spans.zone_eligibility import SPAN_ZONE_ELIGIBILITY_FILTERED

LILLIPUT = "the_lilliput_court"
HOUYHNHNM = "the_houyhnhnm_assembly"

WORLD = "gulliver"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _region(controlled_by: str | None) -> Region:
    return Region(name="R", summary="s", description="d", controlled_by=controlled_by)


def _zoned_pack(regions: dict[str, str | None]) -> SimpleNamespace:
    """A pack stand-in exposing ``worlds[WORLD].cartography`` (the established
    ``pregen._seed_authored_npcs`` accessor convention). No ``rules`` attribute →
    ``inject`` defaults ``combat_encounters`` to True (model default)."""
    carto = CartographyConfig(regions={rid: _region(cb) for rid, cb in regions.items()})
    return SimpleNamespace(worlds={WORLD: SimpleNamespace(cartography=carto)})


class _FakeSessionData:
    def __init__(self, genre_pack: object) -> None:
        self.genre_slug = "wry_whimsy"
        self.world_slug = WORLD
        self.genre_pack = genre_pack
        self.monster_manual: MonsterManual | None = None


def _encounter(enemy_name: str, *, factions: list[str]) -> ManualEncounter:
    return ManualEncounter(
        data={"enemies": [{"name": enemy_name, "class": "brute", "tier": 2, "hp": 9}]},
        label=f"1x {enemy_name} (tier 2)",
        tier=2,
        state=EntryState.AVAILABLE,
        factions=factions,
    )


def _manual(*encounters: ManualEncounter) -> MonsterManual:
    return MonsterManual(genre="wry_whimsy", world=WORLD, encounters=list(encounters))


def _snapshot(*, region: str | None) -> GameSnapshot:
    """One seated PC. ``region`` None → no ``pc_regions`` entry (unresolvable)."""
    pc_regions = {"Gulliver": region} if region is not None else {}
    return GameSnapshot(
        genre_slug="wry_whimsy",
        world_slug=WORLD,
        characters=[],
        quest_log={},
        lore_established=[],
        discovered_regions=[],
        turn_manager=TurnManager(),
        player_seats={"seat-1": "Gulliver"},
        pc_regions=pc_regions,
    )


def _names(snap: GameSnapshot) -> list[str]:
    return [n.core.name for n in snap.npcs]


def _filtered_spans(otel_capture: InMemorySpanExporter) -> list[Any]:
    return [
        s for s in otel_capture.get_finished_spans() if s.name == SPAN_ZONE_ELIGIBILITY_FILTERED
    ]


# ---------------------------------------------------------------------------
# Behavior — the headline fix
# ---------------------------------------------------------------------------


def test_inject_drops_wrong_zone_encounter() -> None:
    """A Houyhnhnm-tagged Yahoo is NOT materialized on the Lilliput shore."""
    sd = _FakeSessionData(_zoned_pack({"lilliput_shore": LILLIPUT, "houyhnhnm_land": HOUYHNHNM}))
    sd.monster_manual = _manual(_encounter("Yahoo Brute", factions=[HOUYHNHNM]))
    snap = _snapshot(region="lilliput_shore")

    monster_manual_inject.inject(sd, snap, current_location="The Shore", in_combat=True)

    assert "Yahoo Brute" not in _names(snap), (
        "wrong-zone Yahoo leaked onto the Lilliput shore — Seam 1 did not filter"
    )


def test_inject_includes_in_zone_encounter() -> None:
    """The same Yahoo MUST surface in Houyhnhnm-land (no over-suppression)."""
    sd = _FakeSessionData(_zoned_pack({"lilliput_shore": LILLIPUT, "houyhnhnm_land": HOUYHNHNM}))
    sd.monster_manual = _manual(_encounter("Yahoo Brute", factions=[HOUYHNHNM]))
    snap = _snapshot(region="houyhnhnm_land")

    monster_manual_inject.inject(sd, snap, current_location="The Plain", in_combat=True)

    assert "Yahoo Brute" in _names(snap)


def test_inject_filters_selectively_not_all_or_nothing() -> None:
    """In Lilliput: the Lilliputian content stays, only the wrong-zone Yahoo drops."""
    sd = _FakeSessionData(_zoned_pack({"lilliput_shore": LILLIPUT, "houyhnhnm_land": HOUYHNHNM}))
    sd.monster_manual = _manual(
        _encounter("Yahoo Brute", factions=[HOUYHNHNM]),
        _encounter("Lilliput Guard", factions=[LILLIPUT]),
    )
    snap = _snapshot(region="lilliput_shore")

    monster_manual_inject.inject(sd, snap, current_location="The Shore", in_combat=True)

    names = _names(snap)
    assert "Lilliput Guard" in names
    assert "Yahoo Brute" not in names


def test_inject_untagged_encounter_is_permissive_in_zoned_world() -> None:
    """Untagged content stays eligible at runtime (the load validator, 157-7, is
    what guarantees zoned worlds carry no untagged content). The engine must ship
    before content is tagged WITHOUT vanishing every still-untagged creature."""
    sd = _FakeSessionData(_zoned_pack({"lilliput_shore": LILLIPUT}))
    sd.monster_manual = _manual(_encounter("Untagged Beast", factions=[]))
    snap = _snapshot(region="lilliput_shore")

    monster_manual_inject.inject(sd, snap, current_location="The Shore", in_combat=True)

    assert "Untagged Beast" in _names(snap)


def test_inject_unzoned_world_does_not_filter() -> None:
    """An unzoned world (no ``controlled_by`` on any region) is unaffected even if
    an encounter happens to carry a faction tag — protects the 11 single-zone
    worlds from any behavior change."""
    sd = _FakeSessionData(_zoned_pack({"the_dome": None, "the_wastes": None}))
    sd.monster_manual = _manual(_encounter("Yahoo Brute", factions=[HOUYHNHNM]))
    snap = _snapshot(region="the_dome")

    monster_manual_inject.inject(sd, snap, current_location="The Dome", in_combat=True)

    assert "Yahoo Brute" in _names(snap)


def test_inject_unresolvable_region_does_not_suppress() -> None:
    """No seated PC resolves a region (pre-bind / malformed turn) → active ∅ →
    fail toward SHOWING content, never a silent empty scene."""
    sd = _FakeSessionData(_zoned_pack({"lilliput_shore": LILLIPUT}))
    sd.monster_manual = _manual(_encounter("Yahoo Brute", factions=[HOUYHNHNM]))
    snap = _snapshot(region=None)  # PC has no pc_regions entry

    monster_manual_inject.inject(sd, snap, current_location="Nowhere", in_combat=True)

    assert "Yahoo Brute" in _names(snap)


# ---------------------------------------------------------------------------
# OTEL — the lie-detector (zone_eligibility.filtered fires on every exclusion)
# ---------------------------------------------------------------------------


def test_inject_emits_filtered_span_on_exclusion(otel_capture: InMemorySpanExporter) -> None:
    """Per the OTEL Observability Principle: each exclusion is a subsystem
    decision the GM panel must see — proof the engine engaged, not that the
    narrator merely didn't mention a Yahoo by luck. Pins the FULL forensic
    attribute shape the GM panel depends on, by key (a key rename = test failure)."""
    sd = _FakeSessionData(_zoned_pack({"lilliput_shore": LILLIPUT, "houyhnhnm_land": HOUYHNHNM}))
    sd.monster_manual = _manual(_encounter("Yahoo Brute", factions=[HOUYHNHNM]))
    snap = _snapshot(region="lilliput_shore")

    monster_manual_inject.inject(sd, snap, current_location="The Shore", in_combat=True)

    fired = _filtered_spans(otel_capture)
    assert len(fired) == 1, f"expected exactly one {SPAN_ZONE_ELIGIBILITY_FILTERED!r} span"
    attrs = dict(fired[0].attributes or {})
    assert attrs.get("subsystem") == "creature"
    assert attrs.get("region") == "lilliput_shore"
    assert attrs.get("content_id") == "1x Yahoo Brute (tier 2)"
    # OTEL serializes a list attribute as a tuple — compare by membership/contents.
    assert list(attrs.get("content_factions") or []) == [HOUYHNHNM]
    assert list(attrs.get("active_factions") or []) == [LILLIPUT]


def test_inject_no_filtered_span_when_eligible(otel_capture: InMemorySpanExporter) -> None:
    """An in-zone encounter is NOT an exclusion — no ``zone_eligibility.filtered``
    span should fire (it would be a false positive on the lie-detector)."""
    sd = _FakeSessionData(_zoned_pack({"lilliput_shore": LILLIPUT, "houyhnhnm_land": HOUYHNHNM}))
    sd.monster_manual = _manual(_encounter("Yahoo Brute", factions=[HOUYHNHNM]))
    snap = _snapshot(region="houyhnhnm_land")

    monster_manual_inject.inject(sd, snap, current_location="The Plain", in_combat=True)

    assert _filtered_spans(otel_capture) == []


def test_inject_no_filtered_span_in_unzoned_world(otel_capture: InMemorySpanExporter) -> None:
    """Unzoned worlds never reach the predicate — no exclusion span fires."""
    sd = _FakeSessionData(_zoned_pack({"the_dome": None}))
    sd.monster_manual = _manual(_encounter("Yahoo Brute", factions=[HOUYHNHNM]))
    snap = _snapshot(region="the_dome")

    monster_manual_inject.inject(sd, snap, current_location="The Dome", in_combat=True)

    assert _filtered_spans(otel_capture) == []


# ---------------------------------------------------------------------------
# Coverage added in review rework (round-trip 1): "*" sentinel at the seam,
# out-of-combat filter-before-cap ordering, and the FLAT_ONLY_SPANS routing
# contract.
# ---------------------------------------------------------------------------


def test_inject_star_sentinel_is_eligible_everywhere() -> None:
    """``factions=["*"]`` (world-global content) must surface in ANY zone of a
    zoned world and never trip the exclusion span."""
    sd = _FakeSessionData(_zoned_pack({"lilliput_shore": LILLIPUT, "houyhnhnm_land": HOUYHNHNM}))
    sd.monster_manual = _manual(_encounter("Sea Serpent", factions=["*"]))
    snap = _snapshot(region="lilliput_shore")

    monster_manual_inject.inject(sd, snap, current_location="The Shore", in_combat=True)

    assert "Sea Serpent" in _names(snap)


def test_inject_out_of_combat_limit_applied_after_filter() -> None:
    """The out-of-combat cap (`_OUT_OF_COMBAT_ENCOUNTER_LIMIT`) must apply to the
    POST-filter eligible list, not the raw available list — otherwise wrong-zone
    encounters ahead of an in-zone one would consume the cap and starve it. This
    pins the documented filter-before-cap ordering (a regression to cap-first
    would silently drop the in-zone encounter)."""
    from sidequest.server.dispatch.monster_manual_inject import _OUT_OF_COMBAT_ENCOUNTER_LIMIT

    wrong = [
        _encounter(f"Wrong{i}", factions=[HOUYHNHNM])
        for i in range(_OUT_OF_COMBAT_ENCOUNTER_LIMIT + 1)
    ]
    right = _encounter("Lilliput Guard", factions=[LILLIPUT])
    sd = _FakeSessionData(_zoned_pack({"lilliput_shore": LILLIPUT, "houyhnhnm_land": HOUYHNHNM}))
    sd.monster_manual = _manual(*wrong, right)  # in-zone encounter is LAST
    snap = _snapshot(region="lilliput_shore")

    monster_manual_inject.inject(sd, snap, current_location="The Shore", in_combat=False)

    names = _names(snap)
    assert "Lilliput Guard" in names, (
        "in-zone encounter starved — the out-of-combat cap was applied before the "
        "zone filter (regression to cap-first ordering)"
    )
    assert not any(n.startswith("Wrong") for n in names)


def test_filtered_span_is_flat_only_registered() -> None:
    """The persistence-routing contract: the exclusion span must be registered in
    FLAT_ONLY_SPANS so it persists as a flat game-engine event for forensics. A
    behavior test asserts emission; only this guards the routing if the
    `FLAT_ONLY_SPANS.add(...)` registration is ever dropped."""
    assert SPAN_ZONE_ELIGIBILITY_FILTERED in FLAT_ONLY_SPANS
