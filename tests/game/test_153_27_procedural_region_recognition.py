"""Tests — Story 153-27 [DUNGEON-ZONE-ELIGIBILITY-UNKNOWN-REGION].

The Seam-2 cast-staging frontier observer (story 157-3) was built for *authored
cartography* regions (gulliver/oz). It fires on EVERY region transition, so when a
PC crosses into the runtime-generated ADR-106 megadungeon (`beneath_sunden`) it does
``cartography.regions.get("exp001.r0")`` → ``None`` and logs the alarming
``unknown_region`` warning on *every* generated room. But a procedural region id is
NOT a misconfiguration — it is a legitimate runtime region that has no authored
cartography by design (``beneath_sunden/cartography.yaml``: "the deep is GENERATED,
not authored here"). There is no NPC cast to stage into the deep (0 ``kind: npc``
across all 17 ``rooms/*.yaml``); per-room creatures/features are owned by a separate
pipeline (curate → ``monster_manual.room_bound``, fixed by 153-26).

Architect decision (Neo, 2026-06-22; see session ``## Architect Assessment``): teach
cast-staging to RECOGNIZE procedural region ids so the false warning stops and the
recognition is OTEL-observable — do NOT stage cast into the deep, and PRESERVE the
``unknown_region`` warning for genuinely-unknown (misspelled / narrator-authored)
non-procedural ids.

These drive the REAL ``stage_region_cast`` module + the REAL
``frontier_hook.notify_region_transition`` dispatch (CLAUDE.md: fixture-driven
behavior + OTEL spans, never grep production source for wiring).

**RED today** on three missing symbols Dev must add:
- ``sidequest.dungeon.is_procedural_region_id`` — the shared recognizer (public
  surface; internal home co-located with the id minter ``seed_bootstrap.ENTRANCE_ID``
  / ``region_graph.generator``'s ``f"exp{expansion_id:03d}.r{i}"``).
- ``SPAN_ZONE_ELIGIBILITY_PROCEDURAL_REGION`` — the recognition span (stays in the
  ``zone_eligibility.*`` family per the decision; flat-only like its siblings).
- the recognition branch in ``stage_region_cast`` (procedural miss → quiet skip +
  span, not ``unknown_region``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import sidequest.game.region_cast_staging as region_cast_staging
import sidequest.genre.loader as loader_mod
from sidequest.dungeon import frontier_hook, is_procedural_region_id
from sidequest.game.region_cast_staging import register_cast_staging_observer, stage_region_cast
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.models.world import CartographyConfig, Region
from sidequest.protocol.models import LocationEntity, LocationEntityBinding
from sidequest.telemetry.spans import FLAT_ONLY_SPANS
from sidequest.telemetry.spans.zone_eligibility import (
    SPAN_ZONE_ELIGIBILITY_CAST_STAGED,
    SPAN_ZONE_ELIGIBILITY_PROCEDURAL_REGION,
)

# A real procedural id minted by ``region_graph.generator``: ``f"exp{eid:03d}.r{i}"``.
PROCEDURAL_ROOM = "exp001.r0"
ENTRANCE = "entrance"
LILLIPUT = "the_lilliput_court"


# ---------------------------------------------------------------------------
# Fixtures (mirror tests/game/test_region_cast_staging.py — self-contained)
# ---------------------------------------------------------------------------


@pytest.fixture
def otel_capture() -> Iterator[InMemorySpanExporter]:
    """Capture spans on the live OTEL provider singleton (the project's ``tracer()``
    closes over the global provider, so a SimpleSpanProcessor on the singleton is the
    reliable way to observe production spans)."""
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

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


@pytest.fixture(autouse=True)
def _restore_frontier_observers() -> Iterator[None]:
    """Prevent observer registration leaking into the rest of the suite."""
    before = list(frontier_hook._OBSERVERS)
    try:
        yield
    finally:
        frontier_hook._OBSERVERS[:] = before


def _npc_entity(name: str) -> LocationEntity:
    return LocationEntity(
        id=name.lower().replace(",", "").replace(" ", "_"),
        label=name,
        tier="real_object",
        binding=LocationEntityBinding(kind="npc", ref=name),
    )


def _region(*, controlled_by: str | None, entities: list[LocationEntity]) -> Region:
    return Region(
        name="R", summary="s", description="d", controlled_by=controlled_by, entities=entities
    )


def _pack(world: str, regions: dict[str, Region]) -> SimpleNamespace:
    return SimpleNamespace(
        worlds={world: SimpleNamespace(cartography=CartographyConfig(regions=regions))}
    )


def _snapshot(*, genre: str, world: str) -> GameSnapshot:
    return GameSnapshot(
        genre_slug=genre,
        world_slug=world,
        characters=[],
        quest_log={},
        lore_established=[],
        discovered_regions=[],
        turn_manager=TurnManager(),
        player_seats={"seat-1": "Delver"},
        pc_regions={},
    )


def _patch_loader(monkeypatch: pytest.MonkeyPatch, packs_by_genre: dict[str, Any]) -> None:
    def fake(genre_code: Any, search_paths: Any = None) -> Any:
        return packs_by_genre[str(genre_code)]

    monkeypatch.setattr(loader_mod, "load_genre_pack_cached", fake)
    monkeypatch.setattr(region_cast_staging, "load_genre_pack_cached", fake, raising=False)


def _pool_names(snap: GameSnapshot) -> list[str]:
    return [m.name for m in snap.npc_pool]


def _surface_pack(
    genre: str = "caverns_and_claudes", world: str = "beneath_sunden"
) -> SimpleNamespace:
    """A beneath_sunden-shaped pack: cartography authors ONLY the static surface
    (ropefoot) — NO procedural region exists in it, by design."""
    return _pack(world, {"ropefoot": _region(controlled_by=None, entities=[])})


def _procedural_spans(exporter: InMemorySpanExporter) -> list[Any]:
    return [
        s
        for s in exporter.get_finished_spans()
        if s.name == SPAN_ZONE_ELIGIBILITY_PROCEDURAL_REGION
    ]


# ---------------------------------------------------------------------------
# AC2 — the recognizer predicate (pure unit, matched to the minter)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "region_id",
    [
        "entrance",  # seed_bootstrap.ENTRANCE_ID (Expansion-0 anchor)
        "exp001.r0",  # the canonical generated shape
        "exp001.r3",
        "exp002.r2",
        "exp123.r45",  # multi-digit room index
        "exp1000.r0",  # expansion_id >= 1000 — `:03d` is a MINIMUM width, not a cap
    ],
)
def test_is_procedural_region_id_accepts_generated_ids(region_id: str) -> None:
    """Recognizes the two minted shapes: the ``entrance`` literal and ``expNNN.rN``
    (NNN >= 3 digits per ``:03d``, N any non-negative int per ``range(n)``)."""
    assert is_procedural_region_id(region_id) is True, f"{region_id!r} should be procedural"


@pytest.mark.parametrize(
    "region_id",
    [
        "ropefoot",  # authored cartography surface region
        "the_dropmouth",
        "mildendo",  # authored cartography region (gulliver)
        "deep_descent",  # the seam sentinel — NOT a materialized region id
        "exp1.r0",  # un-padded expansion id (1 digit) — not the minted format
        "exp01.r0",  # 2-digit — still short of `:03d`
        "exp001",  # expansion only, no room axis
        "exp001.r",  # room prefix, no index
        "exp001.rX",  # non-numeric room index
        "entranceway",  # substring trap — must be an exact match, not a prefix
        "the_entrance",
        "entrance.r0",
        "",  # empty
        "   ",  # whitespace only
        "EXP001.R0",  # case-sensitive: minted ids are lowercase
    ],
)
def test_is_procedural_region_id_rejects_non_procedural_ids(region_id: str) -> None:
    """Authored region ids, the seam sentinel, malformed/short expansion ids,
    substring traps, and junk input are all rejected — the recognizer is exact and
    robust on adversarial input (lang-review #11: no crash / no over-match)."""
    assert is_procedural_region_id(region_id) is False, f"{region_id!r} should NOT be procedural"


# ---------------------------------------------------------------------------
# AC1 + AC4 — entering a procedural region: no warning, recognition span fires
# ---------------------------------------------------------------------------


def test_procedural_room_entry_suppresses_unknown_region_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AC1: crossing into a generated room (``exp001.r0``, absent from cartography)
    must NOT log the ``unknown_region`` warning — it is a legitimate procedural
    region, not a misspelled cartography id. Nothing is staged (no deep cast)."""
    _patch_loader(monkeypatch, {"caverns_and_claudes": _surface_pack()})
    snap = _snapshot(genre="caverns_and_claudes", world="beneath_sunden")

    with caplog.at_level(logging.WARNING):
        stage_region_cast(
            snapshot=snap, pc_name="Delver", from_region="ropefoot", to_region=PROCEDURAL_ROOM
        )

    assert "unknown_region" not in caplog.text, (
        "a legitimate procedural region still triggered the unknown_region warning"
    )
    assert _pool_names(snap) == [], "no NPC cast should stage into a procedural room"


def test_entrance_region_entry_suppresses_unknown_region_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AC1: the ``entrance`` anchor (Expansion-0) is also procedural — no warning."""
    _patch_loader(monkeypatch, {"caverns_and_claudes": _surface_pack()})
    snap = _snapshot(genre="caverns_and_claudes", world="beneath_sunden")

    with caplog.at_level(logging.WARNING):
        stage_region_cast(
            snapshot=snap, pc_name="Delver", from_region="the_dropmouth", to_region=ENTRANCE
        )

    assert "unknown_region" not in caplog.text
    assert _pool_names(snap) == []


def test_procedural_room_entry_emits_recognition_span(
    monkeypatch: pytest.MonkeyPatch, otel_capture: InMemorySpanExporter
) -> None:
    """AC4 (OTEL Observability Principle): recognizing a procedural region is a
    subsystem decision the GM panel must see — proof the engine classified the
    region as procedural-by-design, not that it silently skipped. The span carries
    the region id."""
    _patch_loader(monkeypatch, {"caverns_and_claudes": _surface_pack()})
    snap = _snapshot(genre="caverns_and_claudes", world="beneath_sunden")

    stage_region_cast(
        snapshot=snap, pc_name="Delver", from_region="ropefoot", to_region=PROCEDURAL_ROOM
    )

    spans = _procedural_spans(otel_capture)
    assert len(spans) == 1, f"expected exactly one {SPAN_ZONE_ELIGIBILITY_PROCEDURAL_REGION!r} span"
    assert dict(spans[0].attributes or {}).get("region") == PROCEDURAL_ROOM


def test_procedural_region_span_is_flat_only_registered() -> None:
    """AC4 persistence-routing contract: the recognition span is a flat, persisted
    game-engine event (forensics can reconstruct it from a stored session), like its
    ``zone_eligibility.filtered`` / ``cast_staged`` siblings — not a live-only span."""
    assert SPAN_ZONE_ELIGIBILITY_PROCEDURAL_REGION in FLAT_ONLY_SPANS


# ---------------------------------------------------------------------------
# AC3 — the misconfiguration guard is PRESERVED for non-procedural unknown ids
# ---------------------------------------------------------------------------


def test_non_procedural_unknown_region_still_warns(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    otel_capture: InMemorySpanExporter,
) -> None:
    """AC3: a genuinely-unknown, NON-procedural region id (a misspelled /
    narrator-authored cartography id) must STILL emit ``unknown_region`` — the
    operator's misconfiguration signal survives — and must NOT be mistaken for a
    procedural region (no recognition span)."""
    _patch_loader(monkeypatch, {"caverns_and_claudes": _surface_pack()})
    snap = _snapshot(genre="caverns_and_claudes", world="beneath_sunden")

    with caplog.at_level(logging.WARNING):
        stage_region_cast(
            snapshot=snap, pc_name="Delver", from_region="ropefoot", to_region="atlantis_typo"
        )

    assert "unknown_region" in caplog.text, "the misconfiguration guard was lost"
    assert "atlantis_typo" in caplog.text
    assert _pool_names(snap) == []
    assert _procedural_spans(otel_capture) == [], (
        "a non-procedural unknown id was wrongly recognized as procedural"
    )


# ---------------------------------------------------------------------------
# AC5 — no regression: authored cartography cast still stages, no false recognition
# ---------------------------------------------------------------------------


def test_authored_cartography_region_still_stages_cast_without_procedural_span(
    monkeypatch: pytest.MonkeyPatch, otel_capture: InMemorySpanExporter
) -> None:
    """AC5: the recognition branch must not disturb the Seam-2 authored path. A real
    cartography region with ``kind: npc`` entities still stages its cast, fires the
    ``cast_staged`` span, and fires NO procedural-recognition span (it is authored,
    not procedural)."""
    pack = _pack(
        "gulliver",
        {
            "mildendo": _region(
                controlled_by=LILLIPUT, entities=[_npc_entity("the Emperor of Lilliput")]
            )
        },
    )
    _patch_loader(monkeypatch, {"wry_whimsy": pack})
    snap = _snapshot(genre="wry_whimsy", world="gulliver")

    stage_region_cast(
        snapshot=snap, pc_name="Delver", from_region="the_lilliput_shore", to_region="mildendo"
    )

    assert "the Emperor of Lilliput" in _pool_names(snap), "authored cast staging regressed"
    cast_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == SPAN_ZONE_ELIGIBILITY_CAST_STAGED
    ]
    assert len(cast_spans) == 1, "the authored cast_staged span regressed"
    assert _procedural_spans(otel_capture) == [], (
        "an authored cartography region was wrongly flagged procedural"
    )


# ---------------------------------------------------------------------------
# AC6 — wiring: recognition is reachable through the REAL frontier dispatch
# ---------------------------------------------------------------------------


def test_real_frontier_transition_into_procedural_region_is_recognized(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    otel_capture: InMemorySpanExporter,
) -> None:
    """AC6 keystone wiring test (CLAUDE.md: Every Test Suite Needs a Wiring Test):
    after the production registration, a REAL ``frontier_hook.notify_region_transition``
    into a procedural region — the same producer that fires in production — is
    recognized (no warning, recognition span fired, nothing staged). Proves the fix
    is reachable from the live dispatch path, not just from the predicate in
    isolation."""
    _patch_loader(monkeypatch, {"caverns_and_claudes": _surface_pack()})
    snap = _snapshot(genre="caverns_and_claudes", world="beneath_sunden")

    register_cast_staging_observer()
    with caplog.at_level(logging.WARNING):
        frontier_hook.notify_region_transition(
            snap, pc_name="Delver", from_region="ropefoot", to_region=PROCEDURAL_ROOM
        )

    assert "unknown_region" not in caplog.text, (
        "the procedural-recognition branch is not reached through the real frontier dispatch"
    )
    assert len(_procedural_spans(otel_capture)) == 1
    assert _pool_names(snap) == []
