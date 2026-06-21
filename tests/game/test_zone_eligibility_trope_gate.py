"""RED — Story 157-4, Seam 3: faction/zone-scoped trope activation gate.

The bleed (design spec
``docs/superpowers/specs/2026-06-20-faction-zone-content-eligibility-design.md``,
ADR-059 amendment): in a multi-region world a dormant trope tagged for the
``the_houyhnhnm_assembly`` faction must NOT activate while the party stands on
the Lilliput shore — and MUST become eligible once the party reaches
Houyhnhnm-land.

Seam 3 lives in ``game/trope_tick.py`` ``_gate_activations()`` (whose own comment
already flags this as "a future seam"). A dormant trope is a candidate only if
``is_eligible(trope.factions, active_factions(perspective=None), zoned)`` — the
party-global resolver, so a split party gets the UNION of its zones.

These tests drive the REAL public ``tick_tropes(snapshot, pack, *, now_turn)``
seam (NOT the private gate signature) and assert on observable state (which
dormant tropes flipped to ``progressing``) plus the ``zone_eligibility.filtered``
OTEL span — fixture-driven behavior + span assertions, never a source grep
(CLAUDE.md "No Source-Text Wiring Tests").

Test isolation: every dormant trope starts with ``fire_cooldown_until=None`` (→0)
and ``now_turn=10``, so the cooldown gate is inactive; the would-activate count
stays ≤ ``MAX_SIMULTANEOUS_ACTIVE`` (3), so the cap gate never fires. What's left
to observe is the faction gate alone.

RED until 157-4's GREEN threads the snapshot/pack/cartography into
``_gate_activations`` and adds the ``factions`` candidate filter + span.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from sidequest.game.session import GameSnapshot, TropeState
from sidequest.game.trope_tick import tick_tropes
from sidequest.game.turn import TurnManager
from sidequest.genre.models.tropes import (
    PassiveProgression,
    TropeDefinition,
    TropeEscalation,
)
from sidequest.genre.models.world import CartographyConfig, Region
from sidequest.telemetry.spans.zone_eligibility import SPAN_ZONE_ELIGIBILITY_FILTERED

LILLIPUT = "the_lilliput_court"
HOUYHNHNM = "the_houyhnhnm_assembly"
BROBDINGNAG = "the_brobdingnag_crown"

WORLD = "gulliver"
NOW = 10  # > 0 so a fresh dormant trope's cooldown (0) is inactive.


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _region(controlled_by: str | None) -> Region:
    return Region(name="R", summary="s", description="d", controlled_by=controlled_by)


def _cartography(regions: dict[str, str | None]) -> CartographyConfig:
    return CartographyConfig(regions={rid: _region(cb) for rid, cb in regions.items()})


def _trope_def(trope_id: str, *, factions: list[str] | None = None) -> TropeDefinition:
    """A dormant-able TropeDefinition carrying ``factions`` and a real escalation
    ladder (so it is structurally identical to authored content)."""
    return TropeDefinition(
        id=trope_id,
        name=trope_id.replace("_", " ").title(),
        category="tension",
        escalation=[TropeEscalation(at=t, event=f"beat {t}") for t in (0.25, 0.5, 0.75, 1.0)],
        passive_progression=PassiveProgression(rate_per_turn=0.1),
        factions=factions or [],
    )


def _pack(
    tropes: list[TropeDefinition],
    regions: dict[str, str | None] | None = None,
    *,
    with_worlds: bool = True,
) -> Any:
    """A duck-typed pack exposing ``tropes`` (the tick reads it today) plus
    ``worlds[WORLD].cartography`` (Seam 3 reads it for ``active_factions``).

    ``with_worlds=False`` omits ``worlds`` entirely to pin the duck-typed
    permissive fallback (``cartography_for`` → None → unzoned → no filtering),
    which protects the existing ``tests/game/test_trope_tick.py`` packs.
    """
    if not with_worlds:
        return SimpleNamespace(tropes=tropes)
    carto = _cartography(regions or {"lilliput_shore": LILLIPUT})
    return SimpleNamespace(
        tropes=tropes,
        worlds={WORLD: SimpleNamespace(cartography=carto)},
    )


def _snapshot(*, pc_regions: dict[str, str], dormant: list[str]) -> GameSnapshot:
    """A snapshot with one seat per pc_region and the given dormant trope ids."""
    player_seats = {f"seat-{i}": name for i, name in enumerate(pc_regions)}
    snap = GameSnapshot(
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
    for trope_id in dormant:
        snap.active_tropes.append(TropeState(id=trope_id, status="dormant", progress=0.0))
    snap.turn_manager.interaction = NOW
    return snap


def _status(snap: GameSnapshot, trope_id: str) -> str:
    return next(t.status for t in snap.active_tropes if t.id == trope_id)


def _filtered_spans(otel_capture: Any) -> list[Any]:
    return [
        s for s in otel_capture.get_finished_spans() if s.name == SPAN_ZONE_ELIGIBILITY_FILTERED
    ]


# ---------------------------------------------------------------------------
# AC-3.2 / AC-3.3 — the headline gate
# ---------------------------------------------------------------------------


def test_wrong_zone_dormant_trope_is_not_activated() -> None:
    """A Houyhnhnm-tagged dormant trope must stay dormant on the Lilliput shore."""
    snap = _snapshot(pc_regions={"Gulliver": "lilliput_shore"}, dormant=["houyhnhnm_unrest"])
    pack = _pack(
        [_trope_def("houyhnhnm_unrest", factions=[HOUYHNHNM])],
        {"lilliput_shore": LILLIPUT, "houyhnhnm_land": HOUYHNHNM},
    )

    tick_tropes(snap, pack, now_turn=NOW)

    assert _status(snap, "houyhnhnm_unrest") == "dormant", (
        "wrong-zone trope activated on the Lilliput shore — Seam 3 did not gate it"
    )


def test_in_zone_dormant_trope_activates() -> None:
    """The same trope MUST activate in Houyhnhnm-land (no over-suppression)."""
    snap = _snapshot(pc_regions={"Gulliver": "houyhnhnm_land"}, dormant=["houyhnhnm_unrest"])
    pack = _pack(
        [_trope_def("houyhnhnm_unrest", factions=[HOUYHNHNM])],
        {"lilliput_shore": LILLIPUT, "houyhnhnm_land": HOUYHNHNM},
    )

    tick_tropes(snap, pack, now_turn=NOW)

    assert _status(snap, "houyhnhnm_unrest") == "progressing"


def test_gate_is_selective_not_all_or_nothing() -> None:
    """In Lilliput, the Lilliputian trope activates; only the wrong-zone one is held."""
    snap = _snapshot(
        pc_regions={"Gulliver": "lilliput_shore"},
        dormant=["lilliput_intrigue", "houyhnhnm_unrest"],
    )
    pack = _pack(
        [
            _trope_def("lilliput_intrigue", factions=[LILLIPUT]),
            _trope_def("houyhnhnm_unrest", factions=[HOUYHNHNM]),
        ],
        {"lilliput_shore": LILLIPUT, "houyhnhnm_land": HOUYHNHNM},
    )

    tick_tropes(snap, pack, now_turn=NOW)

    assert _status(snap, "lilliput_intrigue") == "progressing"
    assert _status(snap, "houyhnhnm_unrest") == "dormant"


# ---------------------------------------------------------------------------
# AC-3.4 — permissive cases (untagged, "*", unzoned, unresolvable)
# ---------------------------------------------------------------------------


def test_untagged_dormant_trope_is_permissive() -> None:
    """Untagged content stays eligible at runtime — the load validator (157-7),
    not the gate, guarantees a zoned world ships no untagged tropes."""
    snap = _snapshot(pc_regions={"Gulliver": "lilliput_shore"}, dormant=["generic_tension"])
    pack = _pack([_trope_def("generic_tension", factions=[])])

    tick_tropes(snap, pack, now_turn=NOW)

    assert _status(snap, "generic_tension") == "progressing"


def test_star_dormant_trope_is_world_global() -> None:
    """``factions=["*"]`` is world-global — activates in any zone."""
    snap = _snapshot(pc_regions={"Gulliver": "lilliput_shore"}, dormant=["world_event"])
    pack = _pack([_trope_def("world_event", factions=["*"])])

    tick_tropes(snap, pack, now_turn=NOW)

    assert _status(snap, "world_event") == "progressing"


def test_unzoned_world_does_not_gate_tropes() -> None:
    """An unzoned world (no ``controlled_by`` on any region) is unaffected even if
    a trope happens to carry a faction tag — protects the 11 single-zone worlds."""
    snap = _snapshot(pc_regions={"Gulliver": "the_dome"}, dormant=["houyhnhnm_unrest"])
    pack = _pack(
        [_trope_def("houyhnhnm_unrest", factions=[HOUYHNHNM])],
        {"the_dome": None, "the_wastes": None},
    )

    tick_tropes(snap, pack, now_turn=NOW)

    assert _status(snap, "houyhnhnm_unrest") == "progressing"


def test_pack_without_worlds_attr_is_permissive() -> None:
    """Duck-typed safety: a pack with no ``worlds`` attribute (the existing
    ``tests/game/test_trope_tick.py`` shape) → ``cartography_for`` None → unzoned
    → no gating, no crash. Guards every legacy trope-tick fixture from regression."""
    snap = _snapshot(pc_regions={"Gulliver": "lilliput_shore"}, dormant=["houyhnhnm_unrest"])
    pack = _pack([_trope_def("houyhnhnm_unrest", factions=[HOUYHNHNM])], with_worlds=False)

    tick_tropes(snap, pack, now_turn=NOW)

    assert _status(snap, "houyhnhnm_unrest") == "progressing"


def test_unresolvable_region_does_not_suppress() -> None:
    """No seated PC resolves a region (pre-bind / malformed turn) → active ∅ →
    fail toward activating, never a silent empty world."""
    snap = _snapshot(pc_regions={}, dormant=["houyhnhnm_unrest"])
    # One seat, but no pc_regions entry → region_for → None → active ∅.
    snap.player_seats = {"seat-0": "Gulliver"}
    pack = _pack(
        [_trope_def("houyhnhnm_unrest", factions=[HOUYHNHNM])],
        {"lilliput_shore": LILLIPUT},
    )

    tick_tropes(snap, pack, now_turn=NOW)

    assert _status(snap, "houyhnhnm_unrest") == "progressing"


# ---------------------------------------------------------------------------
# AC-3.5 — split-party union
# ---------------------------------------------------------------------------


def test_split_party_uses_union_of_zones() -> None:
    """Split party (PCs in different regions) → active is the UNION; a trope tagged
    for EITHER seated PC's zone activates."""
    snap = _snapshot(
        pc_regions={"Gulliver": "lilliput_shore", "Glumdalclitch": "houyhnhnm_land"},
        dormant=["houyhnhnm_unrest"],
    )
    pack = _pack(
        [_trope_def("houyhnhnm_unrest", factions=[HOUYHNHNM])],
        {"lilliput_shore": LILLIPUT, "houyhnhnm_land": HOUYHNHNM},
    )

    tick_tropes(snap, pack, now_turn=NOW)

    assert _status(snap, "houyhnhnm_unrest") == "progressing"


def test_split_party_still_excludes_zone_outside_union() -> None:
    """A trope tagged for a faction in NEITHER seated PC's zone is still held."""
    snap = _snapshot(
        pc_regions={"Gulliver": "lilliput_shore", "Glumdalclitch": "houyhnhnm_land"},
        dormant=["brobdingnag_court"],
    )
    pack = _pack(
        [_trope_def("brobdingnag_court", factions=[BROBDINGNAG])],
        {
            "lilliput_shore": LILLIPUT,
            "houyhnhnm_land": HOUYHNHNM,
            "brobdingnag_keep": BROBDINGNAG,
        },
    )

    tick_tropes(snap, pack, now_turn=NOW)

    assert _status(snap, "brobdingnag_court") == "dormant"


# ---------------------------------------------------------------------------
# AC-5.3 — re-eligible when the party moves
# ---------------------------------------------------------------------------


def test_held_trope_activates_once_party_moves_into_its_zone() -> None:
    """The untaken-bait promise: a trope held in Lilliput becomes eligible the
    moment the party reaches Houyhnhnm-land (same trope, two ticks)."""
    pack = _pack(
        [_trope_def("houyhnhnm_unrest", factions=[HOUYHNHNM])],
        {"lilliput_shore": LILLIPUT, "houyhnhnm_land": HOUYHNHNM},
    )

    held = _snapshot(pc_regions={"Gulliver": "lilliput_shore"}, dormant=["houyhnhnm_unrest"])
    tick_tropes(held, pack, now_turn=NOW)
    assert _status(held, "houyhnhnm_unrest") == "dormant"

    moved = _snapshot(pc_regions={"Gulliver": "houyhnhnm_land"}, dormant=["houyhnhnm_unrest"])
    tick_tropes(moved, pack, now_turn=NOW)
    assert _status(moved, "houyhnhnm_unrest") == "progressing"


# ---------------------------------------------------------------------------
# AC-3.6 — the OTEL lie-detector
# ---------------------------------------------------------------------------


def test_gate_emits_filtered_span_on_exclusion(otel_capture: Any) -> None:
    """Each exclusion is a subsystem decision the GM panel must see — proof the
    engine actively held the trope, not that it merely never rolled. Pins the full
    forensic attribute shape by key (a key rename = test failure)."""
    snap = _snapshot(pc_regions={"Gulliver": "lilliput_shore"}, dormant=["houyhnhnm_unrest"])
    pack = _pack(
        [_trope_def("houyhnhnm_unrest", factions=[HOUYHNHNM])],
        {"lilliput_shore": LILLIPUT, "houyhnhnm_land": HOUYHNHNM},
    )

    tick_tropes(snap, pack, now_turn=NOW)

    fired = _filtered_spans(otel_capture)
    assert len(fired) == 1, f"expected exactly one {SPAN_ZONE_ELIGIBILITY_FILTERED!r} span"
    attrs = dict(fired[0].attributes or {})
    assert attrs.get("subsystem") == "trope"
    assert attrs.get("content_id") == "houyhnhnm_unrest"
    assert attrs.get("region") == "lilliput_shore"
    # OTEL serializes a list attribute as a tuple — compare by contents.
    assert list(attrs.get("content_factions") or []) == [HOUYHNHNM]
    assert list(attrs.get("active_factions") or []) == [LILLIPUT]


def test_gate_no_filtered_span_when_eligible(otel_capture: Any) -> None:
    """An in-zone trope is not an exclusion — no span fires (a false positive would
    poison the lie-detector)."""
    snap = _snapshot(pc_regions={"Gulliver": "houyhnhnm_land"}, dormant=["houyhnhnm_unrest"])
    pack = _pack(
        [_trope_def("houyhnhnm_unrest", factions=[HOUYHNHNM])],
        {"lilliput_shore": LILLIPUT, "houyhnhnm_land": HOUYHNHNM},
    )

    tick_tropes(snap, pack, now_turn=NOW)

    assert _filtered_spans(otel_capture) == []


def test_gate_no_filtered_span_in_unzoned_world(otel_capture: Any) -> None:
    """Unzoned worlds never reach the predicate — no exclusion span fires."""
    snap = _snapshot(pc_regions={"Gulliver": "the_dome"}, dormant=["houyhnhnm_unrest"])
    pack = _pack(
        [_trope_def("houyhnhnm_unrest", factions=[HOUYHNHNM])],
        {"the_dome": None},
    )

    tick_tropes(snap, pack, now_turn=NOW)

    assert _filtered_spans(otel_capture) == []


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
