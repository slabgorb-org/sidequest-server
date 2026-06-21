"""RED — Story 157-4, Seam 4: faction/zone-scoped seed-deck draw.

The bleed (design spec
``docs/superpowers/specs/2026-06-20-faction-zone-content-eligibility-design.md``,
ADR-059 amendment): a seed tagged for ``the_houyhnhnm_assembly`` must never be
dealt while the party stands on the Lilliput shore — and must become drawable
once they reach Houyhnhnm-land.

Seam 4 filters seed candidates by ``is_eligible(seed.factions,
active_factions(perspective=None), zoned)`` BEFORE the draw, in
``game/seed_deck.py`` ``draw()`` / ``game/seed_tick.py`` ``draw_engaged_seed()``.

THE load-bearing invariant (AC-4.3): the deterministic, session-keyed shuffle and
its resume-safety are **untouched** — we filter the candidate set, NOT the
ordering. The filter is a *skip* over the already-shuffled deck (exactly like the
existing ``drawn_ids`` skip), never a pre-filter of the input seed list (that
would re-key the shuffle of the survivors). ``test_zoned_draw_order_is_baseline
_minus_excluded`` is the trap: a pre-filter implementation produces a *different*
order for the survivors and fails it.

These tests drive the REAL public ``draw_engaged_seed(snapshot, pack, *,
session_id, engagement_signal, now_turn)`` seam (the stable signature — not the
deck's internal filter parameter) and assert on observable state (what landed in
``snapshot.active_seeds``, in what order) plus the ``zone_eligibility.filtered``
OTEL span. Fixture-driven behavior + span assertions, never a source grep.

RED until 157-4's GREEN adds the candidate filter + span.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from sidequest.game.seed_tick import draw_engaged_seed
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.models.tropes import SeedTrope
from sidequest.genre.models.world import CartographyConfig, Region
from sidequest.telemetry.spans.zone_eligibility import SPAN_ZONE_ELIGIBILITY_FILTERED

LILLIPUT = "the_lilliput_court"
HOUYHNHNM = "the_houyhnhnm_assembly"
BROBDINGNAG = "the_brobdingnag_crown"

WORLD = "gulliver"
SESSION = "fixed-session-157-4"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _region(controlled_by: str | None) -> Region:
    return Region(name="R", summary="s", description="d", controlled_by=controlled_by)


def _cartography(regions: dict[str, str | None]) -> CartographyConfig:
    return CartographyConfig(regions={rid: _region(cb) for rid, cb in regions.items()})


def _seed(seed_id: str, *, factions: list[str] | None = None) -> SeedTrope:
    return SeedTrope(id=seed_id, name=f"Seed {seed_id}", lifespan_turns=5, factions=factions or [])


def _pack(
    seeds: list[SeedTrope],
    regions: dict[str, str | None] | None = None,
    *,
    with_worlds: bool = True,
) -> Any:
    """Duck-typed pack: ``seed_tropes`` (read by the draw engine) + ``tropes`` +
    ``worlds[WORLD].cartography`` (read by Seam 4 for ``active_factions``).

    ``with_worlds=False`` omits ``worlds`` — pins the permissive fallback that
    protects the existing ``tests/game/test_seed_draw_engagement.py`` packs.
    """
    if not with_worlds:
        return SimpleNamespace(seed_tropes=seeds, tropes=[])
    carto = _cartography(regions or {"lilliput_shore": LILLIPUT})
    return SimpleNamespace(
        seed_tropes=seeds,
        tropes=[],
        worlds={WORLD: SimpleNamespace(cartography=carto)},
    )


def _snapshot(pc_regions: dict[str, str]) -> GameSnapshot:
    player_seats = {f"seat-{i}": name for i, name in enumerate(pc_regions)}
    return GameSnapshot(
        genre_slug="wry_whimsy",
        world_slug=WORLD,
        characters=[],
        quest_log={},
        lore_established=[],
        discovered_regions=[],
        turn_manager=TurnManager(),
        player_seats=player_seats,
        pc_regions=dict(pc_regions),
    )


def _draw_once(snap: GameSnapshot, pack: Any) -> None:
    draw_engaged_seed(
        snap, pack, session_id=SESSION, engagement_signal="mechanical", now_turn=5
    )


def _drain(snap: GameSnapshot, pack: Any) -> list[str]:
    """Repeatedly engagement-draw until a call adds nothing; return drawn ids in
    draw order (``active_seeds`` is appended in draw order)."""
    for _ in range(len(pack.seed_tropes) + 2):
        before = len(snap.active_seeds)
        _draw_once(snap, pack)
        if len(snap.active_seeds) == before:
            break
    return [s.id for s in snap.active_seeds]


def _active_ids(snap: GameSnapshot) -> list[str]:
    return [s.id for s in snap.active_seeds]


def _filtered_spans(otel_capture: Any) -> list[Any]:
    return [
        s for s in otel_capture.get_finished_spans() if s.name == SPAN_ZONE_ELIGIBILITY_FILTERED
    ]


# ---------------------------------------------------------------------------
# AC-4.2 — the headline exclusion
# ---------------------------------------------------------------------------


def test_wrong_zone_seed_is_not_drawn() -> None:
    """A Houyhnhnm seed (the only candidate) is never dealt on the Lilliput shore
    → the engagement draw is a no-op, not a leak."""
    snap = _snapshot({"Gulliver": "lilliput_shore"})
    pack = _pack(
        [_seed("sH", factions=[HOUYHNHNM])],
        {"lilliput_shore": LILLIPUT, "houyhnhnm_land": HOUYHNHNM},
    )

    _draw_once(snap, pack)

    assert _active_ids(snap) == [], "wrong-zone seed leaked into the opening engagement draw"


def test_in_zone_seed_is_drawn() -> None:
    """The same seed MUST be drawable in Houyhnhnm-land (no over-suppression)."""
    snap = _snapshot({"Gulliver": "houyhnhnm_land"})
    pack = _pack(
        [_seed("sH", factions=[HOUYHNHNM])],
        {"lilliput_shore": LILLIPUT, "houyhnhnm_land": HOUYHNHNM},
    )

    _draw_once(snap, pack)

    assert _active_ids(snap) == ["sH"]


def test_draw_is_selective_not_all_or_nothing() -> None:
    """In Lilliput, the Lilliputian seed is drawable; only the wrong-zone one is
    skipped — the draw does not stall on the ineligible candidate."""
    snap = _snapshot({"Gulliver": "lilliput_shore"})
    pack = _pack(
        [_seed("sH", factions=[HOUYHNHNM]), _seed("sL", factions=[LILLIPUT])],
        {"lilliput_shore": LILLIPUT, "houyhnhnm_land": HOUYHNHNM},
    )

    drawn = _drain(snap, pack)

    assert "sL" in drawn
    assert "sH" not in drawn


# ---------------------------------------------------------------------------
# AC-4.4 — permissive cases
# ---------------------------------------------------------------------------


def test_untagged_seed_is_permissive() -> None:
    snap = _snapshot({"Gulliver": "lilliput_shore"})
    pack = _pack([_seed("sU", factions=[])])

    _draw_once(snap, pack)

    assert _active_ids(snap) == ["sU"]


def test_star_seed_is_world_global() -> None:
    snap = _snapshot({"Gulliver": "lilliput_shore"})
    pack = _pack([_seed("sStar", factions=["*"])])

    _draw_once(snap, pack)

    assert _active_ids(snap) == ["sStar"]


def test_unzoned_world_does_not_filter_seeds() -> None:
    """All regions ``controlled_by=None`` → unzoned → a faction-tagged seed is
    still drawn (protects the 11 single-zone worlds)."""
    snap = _snapshot({"Gulliver": "the_dome"})
    pack = _pack([_seed("sH", factions=[HOUYHNHNM])], {"the_dome": None, "the_wastes": None})

    _draw_once(snap, pack)

    assert _active_ids(snap) == ["sH"]


def test_pack_without_worlds_attr_is_permissive() -> None:
    """Duck-typed safety: a pack with no ``worlds`` attribute (the existing
    ``tests/game/test_seed_draw_engagement.py`` shape) → unzoned → no filtering,
    no crash. Guards every legacy seed-draw fixture from regression."""
    snap = _snapshot({"Gulliver": "lilliput_shore"})
    pack = _pack([_seed("sH", factions=[HOUYHNHNM])], with_worlds=False)

    _draw_once(snap, pack)

    assert _active_ids(snap) == ["sH"]


# ---------------------------------------------------------------------------
# AC-4.5 — split-party union
# ---------------------------------------------------------------------------


def test_split_party_draws_from_union() -> None:
    """Split party → active is the UNION; a seed tagged for EITHER seated PC's zone
    is drawable."""
    snap = _snapshot({"Gulliver": "lilliput_shore", "Glumdalclitch": "houyhnhnm_land"})
    pack = _pack(
        [_seed("sH", factions=[HOUYHNHNM])],
        {"lilliput_shore": LILLIPUT, "houyhnhnm_land": HOUYHNHNM},
    )

    _draw_once(snap, pack)

    assert _active_ids(snap) == ["sH"]


def test_split_party_excludes_seed_outside_union() -> None:
    """A seed tagged for a faction in NEITHER seated PC's zone is still skipped."""
    snap = _snapshot({"Gulliver": "lilliput_shore", "Glumdalclitch": "houyhnhnm_land"})
    pack = _pack(
        [_seed("sB", factions=[BROBDINGNAG])],
        {
            "lilliput_shore": LILLIPUT,
            "houyhnhnm_land": HOUYHNHNM,
            "brobdingnag_keep": BROBDINGNAG,
        },
    )

    _draw_once(snap, pack)

    assert _active_ids(snap) == []


# ---------------------------------------------------------------------------
# AC-4.3 — determinism / resume-safety: filter the candidates, NOT the ordering
# ---------------------------------------------------------------------------


def test_zoned_draw_order_is_baseline_minus_excluded() -> None:
    """THE load-bearing invariant. With a fixed session_id the shuffle is fixed.

    Baseline (unzoned) draws the full deck in shuffle order. A zoned world that
    excludes exactly one mid-deck seed must draw the SAME order with only that one
    removed — proving the filter is a *skip* over the already-shuffled deck, not a
    re-shuffle of the survivors. A pre-filter-then-shuffle implementation re-keys
    the survivors' order and fails this assertion.
    """
    # Identical seed list (same ids, order, tags) for both runs — only the world's
    # zoned-ness differs, so the deck's shuffle is identical in both.
    seeds = [_seed(f"s{i}", factions=[LILLIPUT]) for i in range(6)]
    seeds[3] = _seed("s3", factions=[HOUYHNHNM])  # the one excluded in Lilliput

    base_snap = _snapshot({"Gulliver": "open_sea"})
    base_pack = _pack(list(seeds), {"open_sea": None})  # unzoned → all eligible
    baseline = _drain(base_snap, base_pack)
    assert set(baseline) == {f"s{i}" for i in range(6)}, "baseline must deal every seed"

    zoned_snap = _snapshot({"Gulliver": "lilliput_shore"})
    zoned_pack = _pack(list(seeds), {"lilliput_shore": LILLIPUT, "houyhnhnm_land": HOUYHNHNM})
    zoned = _drain(zoned_snap, zoned_pack)

    assert "s3" not in zoned, "the Houyhnhnm seed should be excluded in Lilliput"
    assert zoned == [s for s in baseline if s != "s3"], (
        "survivors changed order — Seam 4 re-shuffled the candidate set instead of "
        "skipping the excluded seed over the fixed deck order (AC-4.3 regression)"
    )


def test_excluded_seed_drawable_after_party_moves() -> None:
    """A seed held in Lilliput becomes drawable once the party reaches its zone —
    the skip must NOT mark it drawn (else it could never resurface)."""
    pack_regions = {"lilliput_shore": LILLIPUT, "houyhnhnm_land": HOUYHNHNM}

    held = _snapshot({"Gulliver": "lilliput_shore"})
    _draw_once(held, _pack([_seed("sH", factions=[HOUYHNHNM])], pack_regions))
    assert _active_ids(held) == [], "held seed should not have been dealt in Lilliput"

    moved = _snapshot({"Gulliver": "houyhnhnm_land"})
    _draw_once(moved, _pack([_seed("sH", factions=[HOUYHNHNM])], pack_regions))
    assert _active_ids(moved) == ["sH"]


# ---------------------------------------------------------------------------
# AC-4.6 — the OTEL lie-detector
# ---------------------------------------------------------------------------


def test_draw_emits_filtered_span_on_exclusion(otel_capture: Any) -> None:
    """Each skipped seed is a subsystem decision the GM panel must see. The
    only-candidate-is-wrong-zone setup guarantees the filter examines (and rejects)
    sH deterministically, regardless of shuffle order. Pins the full attribute
    shape by key."""
    snap = _snapshot({"Gulliver": "lilliput_shore"})
    pack = _pack(
        [_seed("sH", factions=[HOUYHNHNM])],
        {"lilliput_shore": LILLIPUT, "houyhnhnm_land": HOUYHNHNM},
    )

    _draw_once(snap, pack)

    fired = _filtered_spans(otel_capture)
    assert len(fired) == 1, f"expected exactly one {SPAN_ZONE_ELIGIBILITY_FILTERED!r} span"
    attrs = dict(fired[0].attributes or {})
    assert attrs.get("subsystem") == "seed"
    assert attrs.get("content_id") == "sH"
    assert attrs.get("region") == "lilliput_shore"
    assert list(attrs.get("content_factions") or []) == [HOUYHNHNM]
    assert list(attrs.get("active_factions") or []) == [LILLIPUT]


def test_draw_no_filtered_span_when_eligible(otel_capture: Any) -> None:
    """An in-zone seed is not an exclusion — no span fires."""
    snap = _snapshot({"Gulliver": "houyhnhnm_land"})
    pack = _pack(
        [_seed("sH", factions=[HOUYHNHNM])],
        {"lilliput_shore": LILLIPUT, "houyhnhnm_land": HOUYHNHNM},
    )

    _draw_once(snap, pack)

    assert _filtered_spans(otel_capture) == []


def test_draw_no_filtered_span_in_unzoned_world(otel_capture: Any) -> None:
    """Unzoned worlds never reach the predicate — no exclusion span fires."""
    snap = _snapshot({"Gulliver": "the_dome"})
    pack = _pack([_seed("sH", factions=[HOUYHNHNM])], {"the_dome": None})

    _draw_once(snap, pack)

    assert _filtered_spans(otel_capture) == []


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
