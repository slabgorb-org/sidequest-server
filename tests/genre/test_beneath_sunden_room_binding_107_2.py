"""Story 107-2 (RED) — per-room creature binding DATA for beneath_sunden.

The injection seam materializes the whole Available encounter pool with no
per-room filter (``monster_manual_inject._npc_patches_for_encounters`` injects
every available encounter in combat). There is no structured tie from a room to
the bestiary creature(s) it should field — entrance.yaml only carries the
affordance PROSE "Disturbing the drifts wakes the Gnaw-Swarm" (entrance.yaml:30),
which the narrator can ignore. Cause 1 of the "creature of animal musk" bug
(context-story-107-2 §diagnosis).

This file is the CONTENT half of AC3: rooms declare a STRUCTURED per-room creature
binding the server can filter on. TEA-defined schema (logged as a deviation):
a top-level ``encounter_creatures: [<bestiary_id>, ...]`` list on each combat
room, referencing world bestiary ids. The entrance binds the documented first
fight (gnaw_swarm); distinct rooms bind distinct creatures so the narrator draws
the room's authored opponent rather than a flat pool.

GATED on shipped content. The server-side resolver + injection + OTEL live in
``tests/server/dispatch/test_room_creature_binding_107_2.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

pytestmark = pytest.mark.skipif(
    not GENRE_PACKS_DIR.is_dir(), reason="sidequest-content not on disk"
)


def _rooms_dir() -> Path:
    try:
        pack_dir = find_pack_path("caverns_and_claudes")
    except PackNotFound:
        pytest.skip("caverns_and_claudes not on disk")
    return pack_dir / "worlds" / "beneath_sunden" / "rooms"


def _bestiary_ids() -> set[str]:
    path = _rooms_dir().parent / "bestiary.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {e["id"] for e in data["entries"] if isinstance(e, dict) and e.get("id")}


def _low_band_image_spec_ids() -> set[str]:
    path = _rooms_dir().parent / "creatures.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    creatures = data.get("creatures") if isinstance(data, dict) else []
    return {c["id"] for c in creatures if isinstance(c, dict) and c.get("id")}


def _room_bindings() -> dict[str, list[str]]:
    """Map room_id -> encounter_creatures list, across all on-disk room files."""
    out: dict[str, list[str]] = {}
    for path in sorted(_rooms_dir().glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            continue
        bound = data.get("encounter_creatures")
        out[path.stem] = list(bound) if isinstance(bound, list) else []
    return out


def test_entrance_room_binds_the_gnaw_swarm_first_fight() -> None:
    """RED: entrance.yaml must structurally bind gnaw_swarm — the documented
    'easy first fight, on purpose' (entrance.yaml:30) — not just describe it in
    affordance prose the narrator can ignore."""
    bindings = _room_bindings()
    assert "entrance" in bindings, "precondition: entrance.yaml present"
    assert "gnaw_swarm" in bindings["entrance"], (
        "entrance room must declare `encounter_creatures: [gnaw_swarm]` so the "
        "first fight resolves to the authored Gnaw-Swarm, not an improvised label"
    )


def test_some_rooms_declare_creature_bindings() -> None:
    """RED: at least one room declares a structured binding. Today no room file
    has an `encounter_creatures` field, so the whole dungeon is one flat pool."""
    all_bound = {cid for bound in _room_bindings().values() for cid in bound}
    assert all_bound, (
        "no room declares `encounter_creatures` — there is no per-room binding, "
        "so the narrator receives an unfiltered pool and improvises (cause 1)"
    )


def test_distinct_rooms_bind_distinct_creatures() -> None:
    """RED + AC3: the binding must distinguish rooms — entrance fields its band,
    a deeper room fields a different one. Proves it is NOT the same flat pool
    everywhere (the precise thing the server filter is tested against)."""
    bindings = {rid: b for rid, b in _room_bindings().items() if b}
    distinct_sets = {tuple(sorted(b)) for b in bindings.values()}
    assert len(distinct_sets) >= 2, (
        "fewer than two distinct room→creature bindings — a per-room binding that "
        f"is identical everywhere is a flat pool by another name (got {bindings})"
    )


def test_all_room_bindings_reference_real_bestiary_ids() -> None:
    """Referential integrity (No Silent Fallbacks): every bound id must resolve to
    a real bestiary entry. A dangling ref must be caught at author time, not
    silently surface an empty pool at runtime."""
    valid = _bestiary_ids()
    dangling: list[str] = []
    for rid, bound in _room_bindings().items():
        dangling += [f"{rid}:{cid}" for cid in bound if cid not in valid]
    assert not dangling, f"room bindings reference unknown bestiary ids: {dangling}"


def test_bound_creatures_are_renderable() -> None:
    """A bound opponent must have an image spec — otherwise binding it just moves
    the 'T' chip from an improvised creature to an authored-but-portraitless one.
    Ties AC3 (binding) to AC4 (renderable)."""
    renderable = _low_band_image_spec_ids()
    bound = {cid for b in _room_bindings().values() for cid in b}
    if not bound:
        pytest.fail("no bindings exist yet (see test_some_rooms_declare_creature_bindings)")
    missing = sorted(cid for cid in bound if cid not in renderable)
    assert not missing, f"bound creatures with no creatures.yaml image spec: {missing}"
