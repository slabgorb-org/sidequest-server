"""Story 162-5 — flickering_reach content reconciliation (GATED — regression guards).

Two hard content-drift findings from the 2026-07-05 NPC-generation inventory
(§4.8) plus the V4 gate:

  AC1  encounter_tables.yaml references **18/20 phantom creature ids** — only
       ``silo_eye`` and ``glass_touched_mount`` resolve against the world's
       bestiary; ~90% of authored encounters are unspawnable. Every ``creature:``
       ref MUST resolve to an id in the world's ``effective_bestiary`` (the same
       roster accessor the MM injection / opponent seater / materializer use).

  AC2  creatures.yaml carries a full parallel stat block that DIVERGES from the
       bestiary (``silo_eye`` bestiary L8/hp36/ac16/2d6 vs creatures.yaml
       tier[3,4]/hp30/ac14/1d8+2). Per ADR-155 the bestiary is the single source
       of truth and creatures.yaml is an OPTIONAL render override. A render-only
       creatures.yaml must not be a *divergent* runtime stat source: for every id
       it shares with the bestiary, each combat field must be either ABSENT
       (truly render-only) or EQUAL to the bestiary (reconciled) — never a third,
       conflicting value. (The V4 companion test — tests/cli/
       test_162_5_encountergen_v4.py — proves the divergence is live ammunition
       at the spawned-EnemyBlock layer, which forces the reconcile over the strip.)

  AC4  content-referential invariant: every encounter ref must resolve to a real
       ``BestiaryEntry`` in the world's ``effective_bestiary`` (the roster the
       engine reads). NOT a full wiring test — encounter_tables.yaml has no
       runtime consumer; the genuine spawn-path wiring test is the V4 tie
       (tests/cli/test_162_5_encountergen_v4.py, drives creature_to_enemy_block).

Home rationale: content has no test runner; content-referential tests live in
sidequest-server gated on content-on-disk, mirroring story 162-3's
tests/genre/test_162_3_generics_content.py. seaboard_of_saints resolves 100%
today (spec §4.8) and serves as the positive control that proves this test's
methodology flags real phantoms rather than always failing.

Status: GREEN as of the 162-5 reconciliation (bestiary expanded 10→16, 18 refs
remapped, creatures.yaml stats reconciled). Kept as regression guards — they were
RED pre-fix (18 phantom refs; 9/10 shared ids diverged on hp).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from sidequest.genre.loader import load_genre_pack
from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

pytestmark = pytest.mark.skipif(
    not GENRE_PACKS_DIR.is_dir(), reason="sidequest-content not on disk"
)

PACK = "mutant_wasteland"
WORLD = "flickering_reach"
CONTROL_WORLD = "seaboard_of_saints"  # spec §4.8: resolves 100% — clean
KNOWN_GOOD_REFS = {"silo_eye", "glass_touched_mount"}  # the 2/20 that resolve today

_DICE_RE = re.compile(r"\s*(\d+\s*d\s*\d+(?:\s*[+-]\s*\d+)?)")


def _load_pack(slug: str) -> Any:
    try:
        return load_genre_pack(find_pack_path(slug))
    except PackNotFound as exc:  # pragma: no cover — pytestmark guards this
        pytest.skip(str(exc))


def _world_dir(pack_slug: str, world_slug: str) -> Path:
    return find_pack_path(pack_slug) / "worlds" / world_slug


def _load_yaml(path: Path) -> dict[str, Any]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    return doc if isinstance(doc, dict) else {}


def _encounter_creature_refs(world_dir: Path) -> set[str]:
    """Every ``creature:`` id referenced across all region encounter tables."""
    doc = _load_yaml(world_dir / "encounter_tables.yaml")
    refs: set[str] = set()
    tables = doc.get("tables")
    if isinstance(tables, dict):
        for table in tables.values():
            if not isinstance(table, dict):
                continue
            encounters = table.get("encounters")
            if not isinstance(encounters, list):
                continue
            for enc in encounters:
                if isinstance(enc, dict) and isinstance(enc.get("creature"), str):
                    refs.add(enc["creature"])
    return refs


def _creatures_by_id(world_dir: Path) -> dict[str, dict[str, Any]]:
    """Flatten creatures.yaml keyed by id, mirroring the runtime loader
    (``encountergen._collect_creatures_from_yaml``: top-level ``creatures:``
    list and/or ``regions.*.creatures``)."""
    path = world_dir / "creatures.yaml"
    if not path.exists():
        return {}
    doc = _load_yaml(path)
    out: dict[str, dict[str, Any]] = {}
    legacy = doc.get("creatures")
    if isinstance(legacy, list):
        for c in legacy:
            if isinstance(c, dict) and isinstance(c.get("id"), str):
                out[c["id"]] = c
    regions = doc.get("regions")
    if isinstance(regions, dict):
        for region in regions.values():
            if not isinstance(region, dict):
                continue
            rc = region.get("creatures")
            if isinstance(rc, list):
                for c in rc:
                    if isinstance(c, dict) and isinstance(c.get("id"), str):
                        out[c["id"]] = c
    return out


def _dice(value: Any) -> str | None:
    """Leading dice expression (``"1d8+2"`` from ``"1d8+2 (glass tendril...)"``),
    whitespace-normalized; ``None`` when no dice token is present."""
    if not isinstance(value, str):
        return None
    m = _DICE_RE.match(value)
    if not m:
        return None
    return re.sub(r"\s+", "", m.group(1))


# ---------------------------------------------------------------------------
# AC1 — phantom encounter refs
# ---------------------------------------------------------------------------


def test_ac1_every_encounter_ref_resolves_to_bestiary() -> None:
    """Every ``creature:`` in flickering_reach/encounter_tables.yaml must exist
    in the world's effective bestiary. GREEN post-fix (was RED pre-fix: 18/20 phantom)."""
    pack = _load_pack(PACK)
    bestiary, source = pack.effective_bestiary(WORLD)
    assert bestiary is not None, f"{PACK}/{WORLD} resolves no bestiary (source tier {source!r})"

    bestiary_ids = {e.id for e in bestiary.entries}
    refs = _encounter_creature_refs(_world_dir(PACK, WORLD))

    # Non-vacuous guards: the test must be reading real content on both sides.
    assert refs, "encounter_tables.yaml declares no creature refs — test would be vacuous"
    assert bestiary_ids, f"{PACK}/{WORLD} bestiary has no entries — test would be vacuous"
    # Positive control: the two refs that resolve today must still resolve, or
    # the roster/parse has broken and any 'green' below would be meaningless.
    assert bestiary_ids >= KNOWN_GOOD_REFS, (
        f"known-good refs missing from bestiary {sorted(bestiary_ids)!r} — "
        "content or accessor changed shape"
    )

    phantom = sorted(r for r in refs if r not in bestiary_ids)
    assert not phantom, (
        f"{len(phantom)}/{len(refs)} encounter refs are phantom (no bestiary id) "
        f"in {PACK}/{WORLD}: {phantom}. Remap each to a real bestiary id or remove "
        f"the encounter (AC1). Resolvable roster: {sorted(bestiary_ids)!r}"
    )


def test_ac1_control_world_resolves_fully() -> None:
    """Positive control: seaboard_of_saints resolves 100% today (spec §4.8).
    Proves the AC1 methodology flags flickering_reach's phantoms rather than
    failing on every world. Stays GREEN across this story."""
    pack = _load_pack(PACK)
    bestiary, source = pack.effective_bestiary(CONTROL_WORLD)
    assert bestiary is not None, f"{PACK}/{CONTROL_WORLD} resolves no bestiary ({source!r})"
    bestiary_ids = {e.id for e in bestiary.entries}
    refs = _encounter_creature_refs(_world_dir(PACK, CONTROL_WORLD))
    assert refs, f"{CONTROL_WORLD} has no encounter refs — control is vacuous"
    phantom = sorted(r for r in refs if r not in bestiary_ids)
    assert not phantom, (
        f"control world {CONTROL_WORLD} unexpectedly has phantom refs {phantom} — "
        "the spec's clean baseline regressed or the test methodology is wrong"
    )


# ---------------------------------------------------------------------------
# AC2 — creatures.yaml must not diverge from the bestiary (ADR-155)
# ---------------------------------------------------------------------------


def test_ac2_creatures_yaml_does_not_diverge_from_bestiary() -> None:
    """For every id shared between creatures.yaml and the bestiary, each combat
    field (hp, ac↔armor_class, damage dice) must be ABSENT (render-only) or
    EQUAL to the bestiary (reconciled). GREEN post-fix (was RED: 9/10 shared ids diverged on hp)."""
    pack = _load_pack(PACK)
    bestiary, _source = pack.effective_bestiary(WORLD)
    assert bestiary is not None
    by_id = {e.id: e for e in bestiary.entries}
    creatures = _creatures_by_id(_world_dir(PACK, WORLD))

    shared = sorted(set(creatures) & set(by_id))
    assert shared, (
        "no ids shared between creatures.yaml and bestiary — AC2 test would be "
        "vacuous; expected the parallel stat block the spec flagged"
    )

    divergences: list[str] = []
    for cid in shared:
        c = creatures[cid]
        b = by_id[cid]
        # creatures.yaml keys: hp / ac / damage ; bestiary keys: hp / armor_class / damage
        if c.get("hp") is not None and c.get("hp") != b.hp:
            divergences.append(f"{cid}.hp: creatures={c.get('hp')} bestiary={b.hp}")
        if c.get("ac") is not None and c.get("ac") != b.armor_class:
            divergences.append(f"{cid}.ac: creatures={c.get('ac')} bestiary={b.armor_class}")
        # Mirror the hp/ac gating: a dice value ASSERTED by creatures.yaml must
        # match the bestiary. If creatures.yaml asserts a die the bestiary lacks
        # (b_dice is None), that is still creatures.yaml carrying a divergent
        # runtime stat the bestiary doesn't own — flag it.
        c_dice, b_dice = _dice(c.get("damage")), _dice(b.damage)
        if c_dice is not None and c_dice != b_dice:
            divergences.append(f"{cid}.damage: creatures={c_dice} bestiary={b_dice}")

    assert not divergences, (
        f"{PACK}/{WORLD} creatures.yaml diverges from the bestiary (ADR-155 render-only "
        f"violation — bestiary is the single source of truth):\n  " + "\n  ".join(divergences)
    )


# ---------------------------------------------------------------------------
# AC4 — wiring: reconciled encounters spawn through the runtime accessor
# ---------------------------------------------------------------------------


def test_ac4_encounters_resolve_through_effective_bestiary() -> None:
    """Content-referential invariant (AC4): every encounter ref must resolve to a
    real ``BestiaryEntry`` in the world's ``effective_bestiary`` — the same roster
    accessor the live MM injection / opponent seater / materializer read.

    NOTE: this is NOT a full end-to-end wiring test. ``encounter_tables.yaml`` has
    no runtime consumer today (nothing in the server parses it), so this asserts
    the refs *would* resolve against the roster the engine uses — not that an
    encounter is driven live. The genuine spawn-path wiring test is the V4 tie in
    ``test_162_5_encountergen_v4.py`` (drives the real ``creature_to_enemy_block``).
    Was RED pre-fix (18 phantom refs); GREEN now — a regression guard against
    re-introducing an unresolvable ref. (A resolved entry always carries real
    combat numbers: ``BestiaryEntry`` enforces ``hp``/``armor_class`` ``ge=1`` at
    parse, so resolution is the only failure mode worth checking here.)"""
    pack = _load_pack(PACK)
    bestiary, _source = pack.effective_bestiary(WORLD)
    assert bestiary is not None
    by_id = {e.id: e for e in bestiary.entries}
    refs = _encounter_creature_refs(_world_dir(PACK, WORLD))
    assert refs, "no encounter refs — invariant would be vacuous"

    unspawnable = sorted(ref for ref in refs if ref not in by_id)

    assert not unspawnable, (
        f"{len(unspawnable)}/{len(refs)} {PACK}/{WORLD} encounters are unspawnable "
        f"through effective_bestiary:\n  " + "\n  ".join(unspawnable)
    )
