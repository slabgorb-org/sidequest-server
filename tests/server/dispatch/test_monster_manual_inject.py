"""Tests for ``sidequest.server.dispatch.monster_manual_inject``.

Covers the per-turn injection seam that materializes Monster Manual
entries into ``snapshot.npcs`` instead of appending text to the narrator
prompt (gaslighting doctrine — the Python deviation from the Rust port).

Includes a wiring test that asserts the module is actually called from
``_execute_narration_turn`` (CLAUDE.md: every test suite needs a wiring
test).
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest

from sidequest.game.monster_manual import EntryState, ManualEncounter, ManualNpc, MonsterManual
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.server.dispatch import monster_manual_inject
from sidequest.server.dispatch import region_population as _rp
from sidequest.server.dispatch.pregen import EncounterSeedError


def _snapshot() -> GameSnapshot:
    return GameSnapshot(
        genre_slug="mutant_wasteland",
        world_slug="flickering_reach",
        characters=[],
        quest_log={},
        lore_established=[],
        discovered_regions=[],
        turn_manager=TurnManager(),
    )


def _manual_with(
    npcs: list[ManualNpc] | None = None, encounters: list[ManualEncounter] | None = None
) -> MonsterManual:
    return MonsterManual(
        genre="mutant_wasteland",
        world="flickering_reach",
        npcs=list(npcs or []),
        encounters=list(encounters or []),
    )


def _human(
    name: str,
    *,
    state: EntryState = EntryState.AVAILABLE,
    activated_location: str | None = None,
    location_tags: list[str] | None = None,
) -> ManualNpc:
    return ManualNpc(
        data={
            "name": name,
            "role": "scavenger",
            "culture": "Scrapborn",
            "ocean_summary": "blunt and competitive",
            "dialogue_quirks": ["shouts prices"],
        },
        name=name,
        role="scavenger",
        culture="Scrapborn",
        location_tags=list(location_tags or []),
        state=state,
        activated_location=activated_location,
    )


def _creature_encounter(*, enemy_name: str, tier: int = 2, hp: int = 9) -> ManualEncounter:
    return ManualEncounter(
        data={
            "enemies": [
                {
                    "name": enemy_name,
                    "class": "salt_burrower",
                    "tier": tier,
                    "hp": hp,
                    "role": "burrowing ambusher",
                    "abilities": ["Burrow — emerges from a tile within 5m"],
                    "morale": "steady",
                }
            ]
        },
        label=f"1x {enemy_name} (tier {tier})",
        tier=tier,
        state=EntryState.AVAILABLE,
    )


class _FakeSessionData:
    """Minimal stand-in for ``_SessionData`` used by ``ensure_loaded``.

    Only the attributes ``ensure_loaded`` and ``inject`` touch are
    populated; everything else stays unset so the test fails loudly if
    the helper grows new dependencies without us noticing.
    """

    def __init__(
        self,
        *,
        genre_slug: str = "mutant_wasteland",
        world_slug: str = "flickering_reach",
        genre_pack: object | None = None,
    ) -> None:
        self.genre_slug = genre_slug
        self.world_slug = world_slug
        self.genre_pack = genre_pack
        self.monster_manual: MonsterManual | None = None


# ---------------------------------------------------------------------------
# ensure_loaded
# ---------------------------------------------------------------------------


def test_ensure_loaded_returns_none_without_genre() -> None:
    sd = _FakeSessionData(genre_slug="")
    assert monster_manual_inject.ensure_loaded(sd) is None
    assert sd.monster_manual is None


def test_ensure_loaded_is_idempotent() -> None:
    manual = _manual_with(
        npcs=[_human("Krag"), _human("Vex"), _human("Mab"), _human("Tess")],
        encounters=[_creature_encounter(enemy_name="Salt Burrower")],
    )
    sd = _FakeSessionData()
    sd.monster_manual = manual
    # No seeding (already populated) — second call returns the same instance.
    first = monster_manual_inject.ensure_loaded(sd)
    second = monster_manual_inject.ensure_loaded(sd)
    assert first is manual
    assert second is manual


def test_ensure_loaded_skips_seed_without_source_dir(tmp_path: Path) -> None:
    sd = _FakeSessionData(genre_slug="ghost_genre", world_slug="ghost_world", genre_pack=None)
    # Redirect manuals dir to tmp so the test doesn't touch ~/.sidequest.
    with mock.patch(
        "sidequest.game.monster_manual.MonsterManual._manuals_dir", return_value=tmp_path
    ):
        loaded = monster_manual_inject.ensure_loaded(sd)
    assert loaded is not None
    assert loaded.needs_seeding()  # would-have-seeded but no source_dir → empty pool
    assert sd.monster_manual is loaded


def test_ensure_loaded_swallows_seed_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Pack:
        source_dir = tmp_path / "packs" / "mutant_wasteland"

    sd = _FakeSessionData(genre_pack=_Pack())

    def _explode(**_kwargs: object) -> None:
        raise RuntimeError("encountergen unavailable")

    monkeypatch.setattr(
        "sidequest.server.dispatch.pregen.seed_manual",
        _explode,
    )
    with mock.patch(
        "sidequest.game.monster_manual.MonsterManual._manuals_dir", return_value=tmp_path
    ):
        loaded = monster_manual_inject.ensure_loaded(sd)
    # Seed crashed; helper continues with whatever was on disk (empty Manual).
    assert loaded is not None
    assert sd.monster_manual is loaded


def test_ensure_loaded_reraises_encounter_seed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED (story 90-5, item 6): a ruleset-module pack with no bestiary makes
    ``seed_manual`` raise ``EncounterSeedError`` — and ``ensure_loaded`` must let
    it PROPAGATE, crashing the session bind loud, rather than swallowing it into a
    warning and binding an empty Monster Manual pool.

    Keith's policy decision (2026-06-10): strict fail-loud. The broad
    ``except Exception`` at ``monster_manual_inject.py:~95`` currently catches
    ``EncounterSeedError`` (it subclasses ``RuntimeError``), so a missing-bestiary
    misconfiguration degrades to a silently-empty pool at runtime — behaviorally
    the 87-4 bug, with better logs. This test fails on current ``develop`` (the
    error is swallowed and ``ensure_loaded`` returns the empty Manual); it goes
    green once the except special-cases ``EncounterSeedError`` to re-raise.

    Contrast ``test_ensure_loaded_swallows_seed_errors`` above: a GENERIC
    ``RuntimeError`` (a transient encountergen outage) still degrades gracefully
    per ADR-006 — only the typed contract violation is fatal."""

    class _Pack:
        source_dir = tmp_path / "packs" / "heavy_metal"

    sd = _FakeSessionData(genre_slug="heavy_metal", world_slug="evropi", genre_pack=_Pack())

    def _raise_seed_error(**_kwargs: object) -> None:
        raise EncounterSeedError(
            "encounter seeding failed for ruleset-module pack 'heavy_metal' "
            "(ruleset=wwn): bestiary.yaml (REQUIRED for ruleset-module packs) missing"
        )

    monkeypatch.setattr(
        "sidequest.server.dispatch.pregen.seed_manual",
        _raise_seed_error,
    )
    with (
        mock.patch(
            "sidequest.game.monster_manual.MonsterManual._manuals_dir", return_value=tmp_path
        ),
        pytest.raises(EncounterSeedError),
    ):
        monster_manual_inject.ensure_loaded(sd)


def _pack_with_authored(world_slug: str, *authored: object) -> object:
    """A pack stand-in exposing only ``worlds`` for the backfill path."""
    from types import SimpleNamespace

    return SimpleNamespace(worlds={world_slug: SimpleNamespace(authored_npcs=list(authored))})


def test_ensure_loaded_backfills_authored_into_seeded_manual(tmp_path: Path) -> None:
    """H1: an existing, fully-seeded on-disk Manual (``needs_seeding()`` False)
    must STILL backfill the world's authored cast.

    The original bug: ``_seed_authored_npcs`` only ran inside ``seed_manual``,
    which ``ensure_loaded`` gates behind ``needs_seeding()``. A Manual already
    holding >=4 NPCs + an encounter never re-seeded, so the authored companions
    (Scarecrow, Tin Woodman, Cowardly Lion) never entered the pool on an existing
    save — the wry_whimsy/oz bug recurs on every prior save. Backfill must be
    unconditional.
    """
    from sidequest.genre.models.authored_npc import AuthoredNpc

    with mock.patch(
        "sidequest.game.monster_manual.MonsterManual._manuals_dir", return_value=tmp_path
    ):
        # Pre-seed a healthy on-disk Manual: 4 generated walk-ons + an encounter
        # so needs_seeding() is False (the old gate would skip authored seeding).
        seeded = _manual_with(
            npcs=[_human("Walkon1"), _human("Walkon2"), _human("Walkon3"), _human("Walkon4")],
            encounters=[_creature_encounter(enemy_name="Salt Burrower")],
        )
        assert not seeded.needs_seeding()
        seeded.save()

        scarecrow = AuthoredNpc(
            id="scarecrow", name="Scarecrow", role="companion", location_tags=["yellow brick road"]
        )
        sd = _FakeSessionData(genre_pack=_pack_with_authored("flickering_reach", scarecrow))

        loaded = monster_manual_inject.ensure_loaded(sd)

    assert loaded is not None
    names = {n.name for n in loaded.npcs}
    assert "Scarecrow" in names, "authored cast must backfill even when needs_seeding() is False"
    # And it was persisted (not just held in memory) so the next process sees it.
    with mock.patch(
        "sidequest.game.monster_manual.MonsterManual._manuals_dir", return_value=tmp_path
    ):
        reloaded = MonsterManual.load("mutant_wasteland", "flickering_reach")
    assert "Scarecrow" in {n.name for n in reloaded.npcs}


def test_ensure_loaded_backfill_emits_span(tmp_path: Path, otel_capture) -> None:
    """H1 (OTEL): the unconditional authored backfill is a subsystem decision, so
    it fires a span the GM panel can read — proof the authored cast entered the
    pool on an existing save, not silent improvisation."""
    from sidequest.genre.models.authored_npc import AuthoredNpc
    from sidequest.telemetry.spans.monster_manual import SPAN_MONSTER_MANUAL_AUTHORED_BACKFILL

    with mock.patch(
        "sidequest.game.monster_manual.MonsterManual._manuals_dir", return_value=tmp_path
    ):
        seeded = _manual_with(
            npcs=[_human("Walkon1"), _human("Walkon2"), _human("Walkon3"), _human("Walkon4")],
            encounters=[_creature_encounter(enemy_name="Salt Burrower")],
        )
        seeded.save()
        # Placed NPC (location_tags) so we also prove the placement-critical
        # field survives the backfill — not just that the count incremented.
        scarecrow = AuthoredNpc(
            id="scarecrow", name="Scarecrow", role="companion", location_tags=["yellow brick road"]
        )
        sd = _FakeSessionData(genre_pack=_pack_with_authored("flickering_reach", scarecrow))
        loaded = monster_manual_inject.ensure_loaded(sd)

    fired = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == SPAN_MONSTER_MANUAL_AUTHORED_BACKFILL
    ]
    assert len(fired) == 1
    assert dict(fired[0].attributes or {}).get("authored_backfilled") == 1
    # location_tags threaded through the backfill, not dropped.
    assert loaded is not None
    backfilled_scarecrow = next(n for n in loaded.npcs if n.name == "Scarecrow")
    assert backfilled_scarecrow.location_tags == ["yellow brick road"]


def test_ensure_loaded_no_backfill_span_when_nothing_added(tmp_path: Path, otel_capture) -> None:
    """The backfill span fires only when the Manual actually changes — a Manual
    that already holds the authored cast (or a pack with no authored roster) is a
    quiet no-op (no spurious span, no needless save)."""
    from sidequest.telemetry.spans.monster_manual import SPAN_MONSTER_MANUAL_AUTHORED_BACKFILL

    with mock.patch(
        "sidequest.game.monster_manual.MonsterManual._manuals_dir", return_value=tmp_path
    ):
        seeded = _manual_with(
            npcs=[_human("Walkon1"), _human("Walkon2"), _human("Walkon3"), _human("Walkon4")],
            encounters=[_creature_encounter(enemy_name="Salt Burrower")],
        )
        seeded.save()
        # Pack with an empty authored roster → nothing to backfill.
        sd = _FakeSessionData(genre_pack=_pack_with_authored("flickering_reach"))
        monster_manual_inject.ensure_loaded(sd)

    fired = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == SPAN_MONSTER_MANUAL_AUTHORED_BACKFILL
    ]
    assert fired == []


def test_ensure_loaded_world_slug_absent_from_pack_warns_and_no_backfill(
    tmp_path: Path, otel_capture, caplog
) -> None:  # type: ignore[no-untyped-def]
    """H3 at the ensure_loaded seam: when the session's ``world_slug`` is not a
    key in ``pack.worlds`` (a config/wiring mismatch), the backfill must surface
    a WARNING and add nothing — it must NOT crash session bind, and the backfill
    span must NOT fire (its absence proves the world-miss path was taken)."""
    import logging

    from sidequest.genre.models.authored_npc import AuthoredNpc
    from sidequest.telemetry.spans.monster_manual import SPAN_MONSTER_MANUAL_AUTHORED_BACKFILL

    with (
        mock.patch(
            "sidequest.game.monster_manual.MonsterManual._manuals_dir", return_value=tmp_path
        ),
        caplog.at_level(logging.WARNING),
    ):
        seeded = _manual_with(
            npcs=[_human("Walkon1"), _human("Walkon2"), _human("Walkon3"), _human("Walkon4")],
            encounters=[_creature_encounter(enemy_name="Salt Burrower")],
        )
        seeded.save()
        scarecrow = AuthoredNpc(
            id="scarecrow", name="Scarecrow", location_tags=["yellow brick road"]
        )
        # Pack only knows "oz"; the session is bound to "flickering_reach".
        sd = _FakeSessionData(
            world_slug="flickering_reach", genre_pack=_pack_with_authored("oz", scarecrow)
        )
        loaded = monster_manual_inject.ensure_loaded(sd)

    assert loaded is not None
    assert "Scarecrow" not in {n.name for n in loaded.npcs}  # nothing backfilled
    assert any("world_not_found" in r.message for r in caplog.records)
    fired = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == SPAN_MONSTER_MANUAL_AUTHORED_BACKFILL
    ]
    assert fired == []


def test_ensure_loaded_trims_over_cap_pool_without_bestiary(tmp_path: Path, otel_capture) -> None:
    """162-9 regression: trim_to_caps must run even when the bestiary is
    UNRESOLVABLE (content_sha None).

    reconcile_content is (correctly) skipped when ``_content_sha_for`` returns
    None — judging staleness against a roster we cannot read would nuke a valid
    pool. But trimming is a pure size-bound op that needs no content evidence, and
    the pre-162-9 code nested the trim call inside the ``content_sha is not None``
    branch. Result: a bestiary-less world's legacy over-cap pool was never bounded
    (repro'd in 162-1 review: a 220-NPC pool stayed 220). Assert the over-cap pool
    IS trimmed to the cap and the ``cap_enforced`` span (kind="trim") fires, with
    NO ``pool_discarded`` span (reconcile stays skipped)."""
    from sidequest.game.monster_manual import MAX_MANUAL_NPCS
    from sidequest.telemetry.spans.monster_manual import (
        SPAN_MONSTER_MANUAL_CAP_ENFORCED,
        SPAN_MONSTER_MANUAL_POOL_DISCARDED,
    )

    excess = 20
    with mock.patch(
        "sidequest.game.monster_manual.MonsterManual._manuals_dir", return_value=tmp_path
    ):
        over_cap = _manual_with(
            npcs=[_human(f"walkon-{i:04d}") for i in range(MAX_MANUAL_NPCS + excess)]
        )
        over_cap.save()
        # A pack with no ``effective_bestiary`` accessor → _content_sha_for None →
        # reconcile skipped. Empty authored roster → the H1 backfill is a no-op. No
        # ``source_dir`` → seeding is skipped. Only the trim path is exercised.
        sd = _FakeSessionData(genre_pack=_pack_with_authored("flickering_reach"))

        loaded = monster_manual_inject.ensure_loaded(sd)

    assert loaded is not None
    assert len(loaded.npcs) == MAX_MANUAL_NPCS, "bestiary-less over-cap pool must be trimmed"
    # Oldest generated dropped first; the youngest survive.
    names = {n.name for n in loaded.npcs}
    assert "walkon-0000" not in names
    assert f"walkon-{MAX_MANUAL_NPCS + excess - 1:04d}" in names

    spans = otel_capture.get_finished_spans()
    trim_spans = [s for s in spans if s.name == SPAN_MONSTER_MANUAL_CAP_ENFORCED]
    assert trim_spans, "trim must emit monster_manual.cap_enforced even without a bestiary"
    assert trim_spans[0].attributes["kind"] == "trim"
    assert trim_spans[0].attributes["npcs_trimmed"] == excess
    # reconcile never ran (no content evidence) → no discard span.
    assert not [s for s in spans if s.name == SPAN_MONSTER_MANUAL_POOL_DISCARDED]

    # Persisted, not just held in memory: the next process sees the bounded pool.
    with mock.patch(
        "sidequest.game.monster_manual.MonsterManual._manuals_dir", return_value=tmp_path
    ):
        reloaded = MonsterManual.load("mutant_wasteland", "flickering_reach")
    assert len(reloaded.npcs) == MAX_MANUAL_NPCS


# ---------------------------------------------------------------------------
# inject — patch generation
# ---------------------------------------------------------------------------


def test_inject_no_manual_is_noop() -> None:
    sd = _FakeSessionData()
    snap = _snapshot()
    assert monster_manual_inject.inject(sd, snap, current_location="The Dome", in_combat=False) == 0
    assert snap.npcs == []


def test_inject_materializes_available_humans_top_three() -> None:
    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(
        npcs=[_human(f"Person{i}") for i in range(5)],
    )
    snap = _snapshot()
    count = monster_manual_inject.inject(sd, snap, current_location="The Dome", in_combat=False)
    assert count == 3
    names = [n.core.name for n in snap.npcs]
    assert names == ["Person0", "Person1", "Person2"]
    # Humans default to neutral disposition (creature fields absent).
    assert all(int(n.disposition) == 0 for n in snap.npcs)


def test_inject_filters_active_humans_by_location_substring() -> None:
    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(
        npcs=[
            _human(
                "Anchored", state=EntryState.ACTIVE, activated_location="The Collapsed Transit Hub"
            ),
            _human("Elsewhere", state=EntryState.ACTIVE, activated_location="Far Plateau"),
            _human("FloatAvail"),
        ],
    )
    snap = _snapshot()
    monster_manual_inject.inject(sd, snap, current_location="Collapsed Transit", in_combat=False)
    names = [n.core.name for n in snap.npcs]
    assert "Anchored" in names
    assert "Elsewhere" not in names
    assert "FloatAvail" in names  # available NPCs always surface (top 3 cap)


def test_inject_surfaces_placed_available_npc_at_matching_location() -> None:
    """A placed AVAILABLE NPC (``location_tags``) materializes into the snapshot
    where its tags match — the production-path mirror of
    ``MonsterManual.available_at_location``. This is the wry_whimsy/oz fix at the
    seam that actually feeds the narrator (``snapshot.npcs``)."""
    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(
        npcs=[
            _human("Scarecrow", location_tags=["yellow brick road", "cornfield"]),
            _human("Throne Guard", location_tags=["emerald city"]),
        ],
    )
    snap = _snapshot()
    monster_manual_inject.inject(
        sd, snap, current_location="The Yellow Brick Road — Morning", in_combat=False
    )
    names = [n.core.name for n in snap.npcs]
    assert "Scarecrow" in names  # placement matches the road
    assert "Throne Guard" not in names  # placed for the Emerald City, gated out


def test_inject_unplaced_available_npc_still_surfaces_anywhere() -> None:
    """An AVAILABLE NPC with no ``location_tags`` keeps the legacy
    everywhere-eligible behavior (generated walk-ons)."""
    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(npcs=[_human("Field Mouse")])
    snap = _snapshot()
    monster_manual_inject.inject(sd, snap, current_location="Some Unrelated Place", in_combat=False)
    assert "Field Mouse" in [n.core.name for n in snap.npcs]


def test_inject_caps_active_at_location_humans() -> None:
    """sq-playtest 2026-06-13 (oz entourage): the Active-at-location loop was
    uncapped and re-surfaced every named NPC every turn. It is now bounded to
    ``_ACTIVE_NPC_INJECT_LIMIT`` so a scene where the narrator named many NPCs
    does not dangle the whole roster as "nearby" candidates each turn."""
    from sidequest.server.dispatch.monster_manual_inject import _ACTIVE_NPC_INJECT_LIMIT

    n_active = _ACTIVE_NPC_INJECT_LIMIT + 3
    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(
        npcs=[
            _human(f"Active{i}", state=EntryState.ACTIVE, activated_location="The Dome")
            for i in range(n_active)
        ],
    )
    snap = _snapshot()
    count = monster_manual_inject.inject(sd, snap, current_location="The Dome", in_combat=False)
    # Only the cap's worth of Active-at-location humans inject (no Available NPCs
    # exist in this manual, so the total equals the active cap exactly).
    assert count == _ACTIVE_NPC_INJECT_LIMIT, (
        f"active-at-location injection must be capped at {_ACTIVE_NPC_INJECT_LIMIT}; got {count}"
    )
    assert len(snap.npcs) == _ACTIVE_NPC_INJECT_LIMIT


def test_inject_span_reports_active_capped_count(otel_capture) -> None:
    """The cap is surfaced in the injection span (No Silent Fallbacks — the GM
    panel sees the bench was bounded, not silently truncated)."""
    from sidequest.server.dispatch.monster_manual_inject import _ACTIVE_NPC_INJECT_LIMIT
    from sidequest.telemetry.spans import SPAN_MONSTER_MANUAL_INJECTED

    dropped = 2
    n_active = _ACTIVE_NPC_INJECT_LIMIT + dropped
    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(
        npcs=[
            _human(f"Active{i}", state=EntryState.ACTIVE, activated_location="The Dome")
            for i in range(n_active)
        ],
    )
    snap = _snapshot()
    monster_manual_inject.inject(sd, snap, current_location="The Dome", in_combat=False)

    fired = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_MONSTER_MANUAL_INJECTED]
    assert len(fired) == 1, f"expected one {SPAN_MONSTER_MANUAL_INJECTED!r} span; got {len(fired)}"
    attrs = dict(fired[0].attributes or {})
    assert attrs.get("active_npcs_capped") == dropped, (
        f"span must report {dropped} active humans capped; got {attrs.get('active_npcs_capped')}"
    )
    assert attrs.get("npcs_injected") == _ACTIVE_NPC_INJECT_LIMIT


def test_inject_span_reports_placement_match_count(otel_capture) -> None:
    """The injection span surfaces placement-aware selection so the GM panel can
    see authored ``location_tags`` actually firing (wry_whimsy/oz fix)."""
    from sidequest.telemetry.spans import SPAN_MONSTER_MANUAL_INJECTED

    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(
        npcs=[
            _human("Scarecrow", location_tags=["yellow brick road"]),  # matches → surfaces
            _human("Throne Guard", location_tags=["emerald city"]),  # placed elsewhere → gated
            _human("Field Mouse"),  # unplaced → surfaces as fallback
        ],
    )
    snap = _snapshot()
    monster_manual_inject.inject(
        sd, snap, current_location="The Yellow Brick Road — Morning", in_combat=False
    )

    fired = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_MONSTER_MANUAL_INJECTED]
    assert len(fired) == 1
    attrs = dict(fired[0].attributes or {})
    # One placed NPC (Scarecrow) matched and surfaced; Field Mouse was an
    # unplaced fallback (not counted as placed-matched).
    assert attrs.get("available_placed_matched") == 1
    # Only Scarecrow is placement-eligible at this location (Throne Guard's tag
    # does not overlap the road).
    assert attrs.get("available_placed_eligible") == 1
    # Nothing dropped — eligible (1) all surfaced (matched 1).
    assert attrs.get("available_placed_dropped") == 0


def test_inject_surfaces_all_placed_npcs_above_cap(otel_capture) -> None:
    """Story 158-11: placed NPCs are the location's intended authored cast and
    every one surfaces — ``_AVAILABLE_NPC_INJECT_LIMIT`` bounds only unplaced
    walk-ons. A roster larger than the cap (the oz road's 4 companions /
    beneath_sunden's 4-NPC Ropefoot camp) no longer drops its tail member: all
    surface and the span reports ``available_placed_dropped == 0``.

    Supersedes the earlier ``..._reports_placed_dropped_by_cap`` test, which
    pinned the buggy behavior (4 eligible, 3 matched, 1 dropped) that this story
    removes; the ``available_placed_dropped`` attribute stays as a regression
    tripwire that must read 0.
    """
    from sidequest.server.dispatch.monster_manual_inject import _AVAILABLE_NPC_INJECT_LIMIT
    from sidequest.telemetry.spans import SPAN_MONSTER_MANUAL_INJECTED

    n_placed = _AVAILABLE_NPC_INJECT_LIMIT + 1  # one more than the legacy slice surfaced
    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(
        npcs=[
            _human(f"Companion{i}", location_tags=["yellow brick road"]) for i in range(n_placed)
        ],
    )
    snap = _snapshot()
    monster_manual_inject.inject(
        sd, snap, current_location="The Yellow Brick Road — Morning", in_combat=False
    )

    # Every placed NPC materialized into the snapshot (none guillotined by the cap).
    surfaced = [n.core.name for n in snap.npcs if n.core.name.startswith("Companion")]
    assert len(surfaced) == n_placed

    fired = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_MONSTER_MANUAL_INJECTED]
    assert len(fired) == 1
    attrs = dict(fired[0].attributes or {})
    assert attrs.get("available_placed_eligible") == n_placed
    assert attrs.get("available_placed_matched") == n_placed
    assert attrs.get("available_placed_dropped") == 0


def test_inject_unplaced_walkons_still_capped_with_no_placed(otel_capture) -> None:
    """The cap still bounds *unplaced* walk-ons when no placed NPCs compete —
    story 158-11 lifts the cap only for placed authored cast, not for generic
    everywhere-eligible walk-ons (which would re-flood a calm scene)."""
    from sidequest.server.dispatch.monster_manual_inject import _AVAILABLE_NPC_INJECT_LIMIT

    n_unplaced = _AVAILABLE_NPC_INJECT_LIMIT + 2
    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(npcs=[_human(f"Walkon{i}") for i in range(n_unplaced)])
    snap = _snapshot()
    monster_manual_inject.inject(sd, snap, current_location="The Dome", in_combat=False)
    assert len(snap.npcs) == _AVAILABLE_NPC_INJECT_LIMIT


def test_inject_full_camp_roster_all_seed_authored() -> None:
    """Story 158-11 regression (beneath_sunden Ropefoot): a 4-NPC authored camp
    roster, all anchored to the same location, must ALL materialize with
    ``manual_origin=True`` and a non-None location — not 3-authored-and-1-default.

    Measured ground truth (save session 16097): Brecca/Ondre/Salla seeded
    ``manual_origin=True`` + ``location='Ropefoot — The Kept Fire'`` while Harmund
    Fuel-Count (4th in roster order) seeded ``manual_origin=False`` / ``None`` —
    the inject cap dropped him before he could be patched. All four now seed
    identically.
    """
    roster = [
        "Brecca Half-Hand",
        "Ondre Drumhand",
        "Salla Who Came Back Thin",
        "Harmund Fuel-Count",
    ]
    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(
        npcs=[_human(name, location_tags=["ropefoot"]) for name in roster],
    )
    snap = _snapshot()
    monster_manual_inject.inject(
        sd, snap, current_location="Ropefoot — The Kept Fire", in_combat=False
    )

    by_name = {n.core.name: n for n in snap.npcs}
    for name in roster:
        assert name in by_name, f"{name} dropped from the snapshot roster"
        npc = by_name[name]
        assert npc.manual_origin is True, f"{name} seeded manual_origin={npc.manual_origin}"
        assert npc.location is not None, f"{name} seeded location=None"


def test_inject_skips_dormant_humans() -> None:
    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(
        npcs=[
            _human("DormantOne", state=EntryState.DORMANT, activated_location="The Dome"),
            _human("DormantTwo", state=EntryState.DORMANT, activated_location="The Dome"),
        ],
    )
    snap = _snapshot()
    count = monster_manual_inject.inject(sd, snap, current_location="The Dome", in_combat=False)
    assert count == 0
    assert snap.npcs == []


def test_inject_materializes_encounter_creatures_with_hostile_disposition() -> None:
    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(
        encounters=[_creature_encounter(enemy_name="Salt Burrower", tier=2, hp=12)],
    )
    snap = _snapshot()
    count = monster_manual_inject.inject(sd, snap, current_location="The Dome", in_combat=True)
    assert count == 1
    npc = snap.npcs[0]
    assert npc.core.name == "Salt Burrower"
    # Hostile default from _npc_from_patch when creature fields are present.
    assert int(npc.disposition) == -20
    assert npc.threat_level == 2
    assert npc.creature_id == "salt_burrower"
    # HP seeded into HpPool (ADR-114).
    assert npc.core.hp.current == 12
    assert npc.core.hp.max == 12


def _pack_with_combat(enabled: bool) -> object:
    """Minimal stand-in pack exposing only ``rules.combat_encounters``."""
    from types import SimpleNamespace

    return SimpleNamespace(rules=SimpleNamespace(combat_encounters=enabled))


def test_inject_suppresses_encounter_creatures_when_combat_disabled() -> None:
    """Playtest 2026-06-01: a social pack (combat_encounters=False) must NOT
    inject combat-encounter enemies — no hostile -20 NPCs with combat stats in
    a drawing-room mystery. Human NPCs still surface; the Manual's stale combat
    encounters are skipped at the injection seam (defense-in-depth for cached
    manuals seeded before the flag landed)."""
    sd = _FakeSessionData(genre_pack=_pack_with_combat(False))
    sd.monster_manual = _manual_with(
        npcs=[_human("Lady Catherine")],
        encounters=[_creature_encounter(enemy_name="Captain Macaskill", tier=2, hp=24)],
    )
    snap = _snapshot()
    count = monster_manual_inject.inject(
        sd, snap, current_location="The Drawing Room", in_combat=True
    )
    names = [n.core.name for n in snap.npcs]
    assert count == 1
    assert names == ["Lady Catherine"]
    assert "Captain Macaskill" not in names
    # No hostile combatants leaked in.
    assert all(int(n.disposition) == 0 for n in snap.npcs)


def test_inject_keeps_encounter_creatures_when_combat_enabled_pack() -> None:
    """A combat-enabled pack (explicit True) still injects encounter creatures —
    the flag only suppresses when explicitly False."""
    sd = _FakeSessionData(genre_pack=_pack_with_combat(True))
    sd.monster_manual = _manual_with(
        encounters=[_creature_encounter(enemy_name="Salt Burrower", tier=2, hp=12)],
    )
    snap = _snapshot()
    count = monster_manual_inject.inject(sd, snap, current_location="The Dome", in_combat=True)
    assert count == 1
    assert snap.npcs[0].core.name == "Salt Burrower"


def test_inject_out_of_combat_caps_encounters() -> None:
    encs = [_creature_encounter(enemy_name=f"Mob{i}") for i in range(5)]
    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(encounters=encs)
    snap = _snapshot()
    count = monster_manual_inject.inject(sd, snap, current_location="The Dome", in_combat=False)
    # Out of combat: only the first 2 encounters' enemies materialize.
    assert count == 2
    assert [n.core.name for n in snap.npcs] == ["Mob0", "Mob1"]


def test_inject_in_combat_materializes_all_available_encounters() -> None:
    encs = [_creature_encounter(enemy_name=f"Mob{i}") for i in range(5)]
    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(encounters=encs)
    snap = _snapshot()
    count = monster_manual_inject.inject(sd, snap, current_location="The Dome", in_combat=True)
    assert count == 5


def test_inject_is_idempotent_across_turns() -> None:
    """Re-injecting the same Manual into a snapshot merges, doesn't duplicate."""
    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(
        encounters=[_creature_encounter(enemy_name="Salt Burrower", hp=9)],
    )
    snap = _snapshot()
    monster_manual_inject.inject(sd, snap, current_location="The Dome", in_combat=True)
    monster_manual_inject.inject(sd, snap, current_location="The Dome", in_combat=True)
    # _merge_npc_patch path — same name lands once.
    assert len(snap.npcs) == 1


def test_reinjection_across_combat_turns_preserves_damaged_hp() -> None:
    """BUG 2b (eh-opp-damage) WIRING: drive the REAL per-turn inject path twice,
    damaging the materialized creature between turns, and assert the second
    inject does NOT heal it back to full.

    This is the exact playtest shape: ``monster_manual.injected ... in_combat=True``
    fires every combat turn; before the fix the merge leg reset core.hp to a full
    pool from the patch's hp claim, so a damaged (or dead) opponent sprang back.
    Proves the session.py merge-seam fix is wired into the production injection
    seam, not just unit-correct in isolation."""
    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(
        encounters=[_creature_encounter(enemy_name="Salt Burrower", hp=9)],
    )
    snap = _snapshot()

    # Turn 1: creature materializes at full HP.
    monster_manual_inject.inject(sd, snap, current_location="The Dome", in_combat=True)
    npc = next(n for n in snap.npcs if n.core.name == "Salt Burrower")
    assert npc.core.hp.current == 9

    # Player damages it mid-combat to 2/9.
    npc.core.hp.current = 2

    # Turn 2: the per-turn inject re-emits the SAME creature with hp=9.
    monster_manual_inject.inject(sd, snap, current_location="The Dome", in_combat=True)
    npc = next(n for n in snap.npcs if n.core.name == "Salt Burrower")
    assert npc.core.hp.current == 2, (
        f"the per-turn re-injection must PRESERVE the damaged HP (2/9), not heal "
        f"the enemy back to full; got {npc.core.hp.current}/{npc.core.hp.max}"
    )
    assert npc.core.hp.max == 9


# ---------------------------------------------------------------------------
# Playtest 2026-06-10 — junk-name cleansing at the injection boundary.
#
# The Monster Manual is a long-lived on-disk cache. A name minted by older
# generator code (``Vesper (version)`` in a stale coyote_star manual)
# survives every reload and reaches the player-facing snapshot verbatim.
# inject() must strip never-valid junk (bracketed annotations, digits)
# without touching intentional stylistic punctuation.
# ---------------------------------------------------------------------------


def test_inject_strips_parenthetical_annotation_from_creature_name() -> None:
    """A stale ``Vesper (version)`` enemy must reach the snapshot as ``Vesper``."""
    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(
        encounters=[_creature_encounter(enemy_name="Vesper (version)", tier=1, hp=24)],
    )
    snap = _snapshot()
    monster_manual_inject.inject(sd, snap, current_location="The Dome", in_combat=True)
    assert [n.core.name for n in snap.npcs] == ["Vesper"]


def test_inject_strips_parenthetical_annotation_from_human_name() -> None:
    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(npcs=[_human("Demiloslava (npc)")])
    snap = _snapshot()
    monster_manual_inject.inject(sd, snap, current_location="The Dome", in_combat=False)
    assert [n.core.name for n in snap.npcs] == ["Demiloslava"]


def test_inject_preserves_intentional_callsign_and_drift_marker() -> None:
    """Broken Drift mints quoted callsigns and comma drift-markers on purpose —
    the cleanser must NOT mangle them (only brackets/digits are junk)."""
    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(
        npcs=[_human("Quija 'Salt'"), _human("Hush, off Tether")],
    )
    snap = _snapshot()
    monster_manual_inject.inject(sd, snap, current_location="The Dome", in_combat=False)
    names = [n.core.name for n in snap.npcs]
    assert "Quija 'Salt'" in names
    assert "Hush, off Tether" in names


def test_inject_drops_unsalvageable_name() -> None:
    """A name that is nothing but a bracketed token sanitizes to empty — drop
    the patch rather than inject a nameless NPC (and never crash on the
    NpcPatch non-blank validator)."""
    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(
        encounters=[_creature_encounter(enemy_name="(version)", tier=1, hp=9)],
    )
    snap = _snapshot()
    count = monster_manual_inject.inject(sd, snap, current_location="The Dome", in_combat=True)
    assert count == 0
    assert snap.npcs == []


def test_inject_emits_names_sanitized_span_count() -> None:
    """The registry decision is observable: the injected span reports how many
    names were cleansed so the GM panel (lie detector) can see it fire."""
    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(
        encounters=[_creature_encounter(enemy_name="Vesper (version)", tier=1, hp=24)],
    )
    snap = _snapshot()
    with mock.patch.object(monster_manual_inject.Span, "open") as span_open:
        monster_manual_inject.inject(sd, snap, current_location="The Dome", in_combat=True)
    # Span.open(SPAN, {attrs}) — find the injected-span attrs payload.
    attrs = next(
        call.args[1]
        for call in span_open.call_args_list
        if len(call.args) >= 2 and "names_sanitized" in call.args[1]
    )
    assert attrs["names_sanitized"] == 1


# ---------------------------------------------------------------------------
# Playtest 2026-05-11 regression — location stamp on injected NPCs.
#
# Manual-injected NPCs (humans + encounter creatures) were materialized
# into ``snapshot.npcs`` with ``location=None``, ``last_seen_location=None``,
# ``pool_origin=None``. Downstream, ``in_same_zone()`` masked every
# co-located target, the narrator received ``npcs_present=0`` for the
# entire dive, and the monster manual was effectively dormant. Inject
# must stamp ``location=current_location`` (or the Active anchor) onto
# every patch so the projection layer can match them to the party.
# ---------------------------------------------------------------------------


def test_inject_stamps_current_location_on_available_humans() -> None:
    """Available (non-Active) humans materialize at the party's current location
    so ``in_same_zone()`` matches them — otherwise the monster manual is
    invisible to the narrator (playtest 2026-05-11)."""
    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(npcs=[_human("Hob")])
    snap = _snapshot()

    monster_manual_inject.inject(sd, snap, current_location="Sünden Square", in_combat=False)

    assert len(snap.npcs) == 1
    assert snap.npcs[0].core.name == "Hob"
    assert snap.npcs[0].location == "Sünden Square", (
        "Available human injected with location=None; in_same_zone() will "
        "mask this NPC from every co-located visibility query."
    )


def test_inject_uses_activated_location_for_active_humans() -> None:
    """Active humans carry an explicit anchor — prefer that over the
    party's current_location (the anchor is the substring-match seam that
    gated their inclusion, so it's the canonical location)."""
    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(
        npcs=[_human("Kern", state=EntryState.ACTIVE, activated_location="The Recruiter's Post")],
    )
    snap = _snapshot()

    monster_manual_inject.inject(
        sd, snap, current_location="Sünden Square — The Recruiter's Post", in_combat=False
    )

    npc = next(n for n in snap.npcs if n.core.name == "Kern")
    assert npc.location == "The Recruiter's Post"


def test_inject_stamps_current_location_on_encounter_creatures() -> None:
    """Creature patches from encounters must carry the party's current
    location too — otherwise creatures are invisible to in_same_zone()."""
    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(
        encounters=[_creature_encounter(enemy_name="Chalk Moth", tier=1, hp=1)],
    )
    snap = _snapshot()

    monster_manual_inject.inject(sd, snap, current_location="Grimvault — Receiving", in_combat=True)

    creature = next(n for n in snap.npcs if n.core.name == "Chalk Moth")
    assert creature.location == "Grimvault — Receiving", (
        "Creature injected with location=None — invisible to in_same_zone()."
    )


def test_inject_leaves_location_none_when_current_location_blank() -> None:
    """Empty current_location is not a valid zone — don't stamp it.

    Pre-chargen and pre-opening turns sometimes call inject() without a
    bound location. An empty string would be no better than None for
    in_same_zone() matching and would pollute the projection. Leave
    location=None in this case (loud failure shape — the warning fires
    upstream)."""
    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(
        encounters=[_creature_encounter(enemy_name="Driftling", tier=1, hp=2)],
    )
    snap = _snapshot()

    monster_manual_inject.inject(sd, snap, current_location="", in_combat=True)

    assert snap.npcs[0].location is None


def test_inject_threat_tier_falls_back_to_encounter_tier() -> None:
    enc = ManualEncounter(
        data={
            "enemies": [
                {"name": "Tier-less Foe", "class": "shade", "hp": 3, "role": "lurker"},
            ]
        },
        label="1x Tier-less Foe (tier 3)",
        tier=3,
        state=EntryState.AVAILABLE,
    )
    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(encounters=[enc])
    snap = _snapshot()
    monster_manual_inject.inject(sd, snap, current_location="", in_combat=True)
    assert snap.npcs[0].threat_level == 3


# ---------------------------------------------------------------------------
# mark_active_from_narration
# ---------------------------------------------------------------------------


def test_mark_active_from_narration_flips_state_for_named_npcs() -> None:
    manual = _manual_with(npcs=[_human("Krag Dustwelder"), _human("Vex")])
    activated = monster_manual_inject.mark_active_from_narration(
        manual,
        "Krag Dustwelder waves you over to the workbench.",
        current_location="The Workbench",
    )
    assert activated == ["Krag Dustwelder"]
    krag = next(n for n in manual.npcs if n.name == "Krag Dustwelder")
    assert krag.state == EntryState.ACTIVE
    assert krag.activated_location == "The Workbench"
    vex = next(n for n in manual.npcs if n.name == "Vex")
    assert vex.state == EntryState.AVAILABLE


def test_mark_active_from_narration_skips_already_active() -> None:
    """Only AVAILABLE → ACTIVE transitions are returned (Rust parity)."""
    manual = _manual_with(
        npcs=[_human("Krag", state=EntryState.ACTIVE, activated_location="Older Scene")],
    )
    activated = monster_manual_inject.mark_active_from_narration(
        manual,
        "Krag turns toward the door.",
        current_location="New Scene",
    )
    assert activated == []
    # activated_location stays anchored to the first scene — mark_active
    # only re-anchors if previously None.
    assert manual.npcs[0].activated_location == "Older Scene"


def test_mark_active_from_narration_empty_narration_returns_empty() -> None:
    manual = _manual_with(npcs=[_human("Krag")])
    assert monster_manual_inject.mark_active_from_narration(manual, "", "any") == []


# ---------------------------------------------------------------------------
# mark_all_dormant
# ---------------------------------------------------------------------------


def test_mark_all_dormant_none_safe() -> None:
    monster_manual_inject.mark_all_dormant(None)  # must not raise


def test_mark_all_dormant_transitions_active_entries() -> None:
    manual = _manual_with(
        npcs=[
            _human("ActiveOne", state=EntryState.ACTIVE, activated_location="loc"),
            _human("AvailableOne"),
        ],
        encounters=[_creature_encounter(enemy_name="ActiveCreature")],
    )
    manual.encounters[0].state = EntryState.ACTIVE
    monster_manual_inject.mark_all_dormant(manual)
    assert manual.npcs[0].state == EntryState.DORMANT
    assert manual.npcs[1].state == EntryState.AVAILABLE  # unchanged
    assert manual.encounters[0].state == EntryState.DORMANT


# ---------------------------------------------------------------------------
# Wiring — production path actually calls the helper
# ---------------------------------------------------------------------------


def test_websocket_session_handler_wires_monster_manual_inject() -> None:
    """CLAUDE.md wiring rule: production code must actually call inject.

    Tests can pass in isolation while the helper sits in a vestigial
    module nobody imports. Read the handler source and assert the
    inject + ensure_loaded calls survive any future refactor that
    silently severs the wiring.
    """
    handler_src = (
        Path(__file__).resolve().parents[3]
        / "sidequest"
        / "server"
        / "websocket_session_handler.py"
    )
    text = handler_src.read_text(encoding="utf-8")
    assert "monster_manual_inject" in text, (
        "websocket_session_handler.py no longer imports monster_manual_inject"
    )
    assert "monster_manual_inject.ensure_loaded" in text, (
        "_execute_narration_turn no longer calls ensure_loaded"
    )
    assert "monster_manual_inject.inject" in text, "_execute_narration_turn no longer calls inject"
    assert "monster_manual_inject.mark_active_from_narration" in text, (
        "post-narration mark_active_from_narration wire missing"
    )
    assert "monster_manual_inject.mark_all_dormant" in text, (
        "location-change mark_all_dormant wire missing"
    )
    assert "sd.monster_manual.save()" in text, (
        "Manual.save() is not called after each turn — lifecycle won't persist"
    )


# ---------------------------------------------------------------------------
# Regression — turn_context.monster_manual staleness (playtest 2026-05-17)
#
# _build_turn_context (session_helpers.py) snapshots monster_manual off
# sd.monster_manual at the *caller* (player_action.py / dice_throw.py /
# ws_handler), which runs BEFORE _execute_narration_turn's lazy
# ensure_loaded(sd). The handler refreshes turn_context.npcs from the
# post-inject snapshot but historically NOT turn_context.monster_manual,
# so the orchestrator received context.monster_manual=None on every
# session's turn 1 and every MP turn where the acting player's per-player
# _SessionData had not yet narrated. Consequence: ToolContext.monster_manual
# was None → lookup_monster returned nothing → narrator improvised stats,
# and the context_wired OTEL flag logged a false negative (~47% of turns
# in the 2026-05-17 logs).
# ---------------------------------------------------------------------------


def _fake_local_dm() -> MagicMock:
    """Dormant LocalDM stub (offline-only design, 2026-04-28 spec) — keeps
    _execute_narration_turn off the decompose path so the test isolates
    the monster_manual refresh seam."""
    from sidequest.protocol.dispatch import DispatchPackage

    fake_dm = MagicMock()
    fake_dm.decompose = AsyncMock(
        return_value=DispatchPackage(
            turn_id="t-mm-refresh",
            per_player=[],
            cross_player=[],
            confidence_global=0.0,
        )
    )
    return fake_dm


@pytest.mark.asyncio
async def test_execute_narration_turn_refreshes_stale_monster_manual(
    session_fixture,
) -> None:
    """The orchestrator must receive turn_context.monster_manual refreshed
    from sd.monster_manual after ensure_loaded — not the stale None the
    caller snapshotted before the lazy load."""
    from tests.server.conftest import (
        _build_turn_context_for_test,
        _make_minimal_narration_turn_result,
    )

    sd, handler = session_fixture

    # Manual is on the session. ensure_loaded short-circuits on its first
    # line (sd.monster_manual is not None) — no disk, no seeding.
    manual = _manual_with(npcs=[_human("Krag")])
    sd.monster_manual = manual

    captured: dict[str, object] = {}

    async def _capture(action: str, turn_context: object, *, room: object = None) -> object:
        captured["turn_context"] = turn_context
        return _make_minimal_narration_turn_result()

    sd.orchestrator.run_narration_turn = AsyncMock(side_effect=_capture)
    sd.local_dm = _fake_local_dm()
    handler._validator = MagicMock()
    handler._validator.submit = AsyncMock()
    handler._validator.is_running = MagicMock(return_value=True)

    # Production ordering: the caller built the context BEFORE the Manual
    # was visible, so monster_manual defaults to None.
    turn_context = _build_turn_context_for_test(sd)
    assert turn_context.monster_manual is None, "precondition: context built stale"

    await handler._execute_narration_turn(sd, "I look around.", turn_context)

    received = captured["turn_context"]
    assert received.monster_manual is manual, (
        "turn_context.monster_manual not refreshed from sd.monster_manual "
        "after ensure_loaded — orchestrator gets None, lookup_monster is "
        "dead, and context_wired logs a false negative."
    )


# ---------------------------------------------------------------------------
# Task 5 — region population inject (ADR-106 / ADR-059 / Story 153-x)
#
# The frozen procedural roster (Task 3 ``region_population`` mutation, Task 4
# ``load_region_population`` reader) is injected into snapshot.npcs as
# region-stamped NpcPatches so the narrator sees real statted creatures
# instead of improvising. The region stamp is the co-location key Task 6
# seats on (ADR-116).
# ---------------------------------------------------------------------------


def _sd_with_manual_and_repo() -> _FakeSessionData:
    """A _FakeSessionData seeded with a MonsterManual (for combat_encounters
    gate to pass) and a non-None dungeon_repository (load_region_population
    is monkeypatched in each test, so any object suffices)."""
    from types import SimpleNamespace

    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(
        encounters=[_creature_encounter(enemy_name="Grue", tier=1, hp=5)],
    )
    sd.dungeon_repository = SimpleNamespace()  # placeholder; loader is patched
    return sd


def test_inject_region_population_stamps_region(monkeypatch: pytest.MonkeyPatch) -> None:
    """A frozen RegionCreature injected via room_id arrives in snapshot.npcs
    with ``region == room_id`` and ``location == current_location``.

    This is the co-location key Task 6 seats on (ADR-116 region-keyed seating).
    The region stamp must be the raw room_id string passed into inject(), not
    a free-text scene label."""
    snap = _snapshot()
    snap.pc_regions["TestPC"] = "exp002.r3"  # PC is in the region
    sd = _sd_with_manual_and_repo()

    def _fake_load(repo: object, region_id: str) -> tuple[list[_rp.RegionCreature], None]:
        assert region_id == "exp002.r3"
        return (
            [
                _rp.RegionCreature(
                    name="Gnaw-Swarm",
                    creature_type="swarm",
                    telegraph="chittering",
                    hp=6,
                    threat_level=1,
                )
            ],
            None,
        )

    monkeypatch.setattr(_rp, "load_region_population", _fake_load)
    monster_manual_inject.inject(
        sd,
        snap,
        current_location="The Winding Catacomb",
        in_combat=False,
        room_id="exp002.r3",
    )
    gnaw = next((n for n in snap.npcs if n.core.name == "Gnaw-Swarm"), None)
    assert gnaw is not None, "Gnaw-Swarm must be materialized from the region population"
    assert gnaw.region == "exp002.r3", (
        f"region stamp must equal the room_id ('exp002.r3'); got {gnaw.region!r}"
    )
    assert gnaw.location == "The Winding Catacomb", (
        f"location must equal current_location; got {gnaw.location!r}"
    )


def test_inject_region_population_emits_span(
    otel_capture: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The region-population inject emits ``monster_manual.region_population``
    so the GM panel can verify procedural rooms field real creatures."""
    from sidequest.telemetry.spans.monster_manual import SPAN_MONSTER_MANUAL_REGION_POPULATION

    snap = _snapshot()
    snap.pc_regions["TestPC"] = "exp002.r3"
    sd = _sd_with_manual_and_repo()

    monkeypatch.setattr(
        _rp,
        "load_region_population",
        lambda repo, rid: (
            [_rp.RegionCreature("Gnaw-Swarm", "swarm", "chittering", 6, 1)],
            None,
        ),
    )
    monster_manual_inject.inject(
        sd,
        snap,
        current_location="The Winding Catacomb",
        in_combat=False,
        room_id="exp002.r3",
    )
    fired = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == SPAN_MONSTER_MANUAL_REGION_POPULATION
    ]
    assert len(fired) == 1, (
        f"expected one {SPAN_MONSTER_MANUAL_REGION_POPULATION!r} span; got {len(fired)}"
    )
    attrs = dict(fired[0].attributes or {})
    assert attrs.get("region_id") == "exp002.r3"
    assert attrs.get("creature_count") == 1
    assert attrs.get("big_bad") is False


def test_inject_region_population_ooc_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Out of combat, the region roster is capped at _OUT_OF_COMBAT_ENCOUNTER_LIMIT.
    The big-bad is always present regardless of the cap (Keith ruling 2026-06-22)."""
    from sidequest.server.dispatch.monster_manual_inject import _OUT_OF_COMBAT_ENCOUNTER_LIMIT

    snap = _snapshot()
    snap.pc_regions["TestPC"] = "exp002.r3"
    sd = _sd_with_manual_and_repo()

    roster = [
        _rp.RegionCreature(f"Mob{i}", "mob", "g", 4, 1)
        for i in range(_OUT_OF_COMBAT_ENCOUNTER_LIMIT + 2)
    ]
    big_bad = _rp.RegionCreature("Boss", "boss", "ominous", 20, 3)

    monkeypatch.setattr(_rp, "load_region_population", lambda repo, rid: (roster, big_bad))
    monster_manual_inject.inject(
        sd,
        snap,
        current_location="The Catacomb",
        in_combat=False,
        room_id="exp002.r3",
    )
    region_npcs = [n for n in snap.npcs if getattr(n, "region", None) == "exp002.r3"]
    region_names = [n.core.name for n in region_npcs]
    # Capped roster + always-present big-bad.
    assert "Boss" in region_names, "big-bad must always be injected out of combat"
    mob_names = [nm for nm in region_names if nm.startswith("Mob")]
    assert len(mob_names) == _OUT_OF_COMBAT_ENCOUNTER_LIMIT, (
        f"roster must be capped at {_OUT_OF_COMBAT_ENCOUNTER_LIMIT} OOC; got {len(mob_names)}"
    )


def test_inject_region_population_in_combat_uncapped(monkeypatch: pytest.MonkeyPatch) -> None:
    """In combat, the full roster is injected (no cap)."""
    from sidequest.server.dispatch.monster_manual_inject import _OUT_OF_COMBAT_ENCOUNTER_LIMIT

    snap = _snapshot()
    snap.pc_regions["TestPC"] = "exp002.r3"
    sd = _sd_with_manual_and_repo()

    full_count = _OUT_OF_COMBAT_ENCOUNTER_LIMIT + 3
    roster = [_rp.RegionCreature(f"Mob{i}", "mob", "g", 4, 1) for i in range(full_count)]

    monkeypatch.setattr(_rp, "load_region_population", lambda repo, rid: (roster, None))
    monster_manual_inject.inject(
        sd,
        snap,
        current_location="The Catacomb",
        in_combat=True,
        room_id="exp002.r3",
    )
    region_npcs = [n for n in snap.npcs if getattr(n, "region", None) == "exp002.r3"]
    mob_names = [n.core.name for n in region_npcs if n.core.name.startswith("Mob")]
    assert len(mob_names) == full_count, (
        f"in-combat roster must be uncapped; expected {full_count}, got {len(mob_names)}"
    )


def test_inject_region_population_authored_name_wins_dedup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When an authored room-bound creature and a region-population creature share
    a name, the authored patch wins (region-population is de-duped out).

    This mirrors the authored-creature precedence established in
    _append_authored_creatures and ensures authored content always dominates
    procedural content at the injection seam."""
    from types import SimpleNamespace

    # Set up an sd with a genre_pack so the authored room-binding path fires.
    # We monkeypatch resolve_room_creatures to return a fake binding and also
    # monkeypatch effective_bestiary to return a fake bestiary entry.
    snap = _snapshot()
    snap.pc_regions["TestPC"] = "exp002.r3"
    sd = _sd_with_manual_and_repo()

    from sidequest.genre.models.bestiary import BestiaryEntry

    authored_entry = BestiaryEntry(
        id="gnaw_swarm",
        name="Gnaw-Swarm",
        level=2,
        hp=10,
        armor_class=12,
        attack_bonus=2,
        role="authored role",
        abilities=[],
        description="authored desc",
    )

    fake_pack = SimpleNamespace(
        worlds={"flickering_reach": SimpleNamespace(authored_npcs=[])},
        rules=SimpleNamespace(combat_encounters=True),
    )

    def _fake_bestiary(world_slug: str) -> tuple[object, object]:
        bs = SimpleNamespace(entries=[authored_entry])
        return bs, None

    fake_pack.effective_bestiary = _fake_bestiary

    sd.genre_pack = fake_pack
    sd.world_slug = "flickering_reach"

    monkeypatch.setattr(
        "sidequest.server.dispatch.room_creature_binding.resolve_room_creatures",
        lambda pack, world, rid: ["gnaw_swarm"],
    )

    # Region population also returns a creature with the SAME name.
    monkeypatch.setattr(
        _rp,
        "load_region_population",
        lambda repo, rid: (
            [_rp.RegionCreature("Gnaw-Swarm", "swarm", "chittering", 6, 1)],
            None,
        ),
    )

    monster_manual_inject.inject(
        sd,
        snap,
        current_location="The Winding Catacomb",
        in_combat=True,
        room_id="exp002.r3",
    )

    gnaw_npcs = [n for n in snap.npcs if n.core.name == "Gnaw-Swarm"]
    assert len(gnaw_npcs) == 1, (
        f"Gnaw-Swarm must appear exactly once (authored wins dedup); got {len(gnaw_npcs)}"
    )
    # The authored creature has manual_origin=True but region=None; the
    # region-pop duplicate must not overwrite it.
    assert gnaw_npcs[0].region is None, (
        "authored creature (no region stamp) must win over the region-pop duplicate"
    )
