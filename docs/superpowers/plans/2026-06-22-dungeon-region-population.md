# Dungeon Region Population — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the procedural dungeon's generated creature rosters into runtime so procedural rooms auto-populate, and make confrontation seating co-locate by the engine-owned region id instead of the narrator-owned free-text scene.

**Architecture:** The curate stage already builds `region_creatures`/`region_big_bad` per region and discards them. We (1) stamp a derived `threat_level` on each `CuratedCreature`, (2) freeze the roster per-region via the existing `record_mutation(region_id, "region_population", …)` seam (no new table), (3) inject it by `region_id` as NPC patches stamped with a new `Npc.region`, and (4) change seating co-location to prefer `npc.region == region_for(pc)` when the NPC carries a region stamp — self-gating, so all narrator NPCs and non-procedural worlds keep today's free-text behavior unchanged. The new `Npc.region` stamp also fixes the stale-location seating bug (Gap B): co-location stops depending on the free-text scene that drifts across seams/turns.

**Tech Stack:** Python 3 / FastAPI (uv-managed), pydantic v2, Postgres (psycopg3) via `DungeonRepository`, OTEL spans via `Span.open`.

**Design of record:** `docs/superpowers/specs/2026-06-22-dungeon-region-population-wiring-design.md` (read it first). This plan supersedes the spec's Part D: stamping `npc.region` makes the free-text location stamp cosmetic, so no separate "post-dispatch location stamp" change is needed.

## Global Constraints

- Run tests with `uv run pytest`; lint `uv run ruff check .`; format `uv run ruff format .`; types `uv run pyright`. All from `sidequest-server/`.
- **No stubs, no silent fallbacks.** A missing/None required input fails loud.
- **No source-text wiring tests.** Assertions are OTEL-span or fixture-behavior driven only (CLAUDE.md "No Source-Text Wiring Tests").
- **Every subsystem decision emits OTEL** (CLAUDE.md OTEL Observability Principle).
- `Npc`, `NpcPatch`, `CrBand` are `model_config = {"extra": "forbid"}` — new fields must be declared explicitly.
- **ADR-114 discipline:** `CuratedCreature` carries NO raw `cr`/`xp`. Store the *derived* integer `threat_level` tier, never raw cr.
- Procedural creatures persist in the per-save dungeon store (Postgres), **never** in the content-repo room YAML (that tree is authored content + freeze-invariant).
- **Self-gating co-location:** region comparison fires only when `npc.region` is set. An NPC with no region stamp takes the exact free-text path it takes today.
- Branch off `develop`; PRs target `develop`.

---

### Task 1: Add `region` to the NPC model and carry it through materialization

**Files:**
- Modify: `sidequest/game/session.py` (`Npc` class ~130-198; `NpcPatch` ~370-398; `_npc_from_patch` ~1905-1962; `_merge_npc_patch` ~1832-1903)
- Test: `tests/game/test_npc_region.py`

**Interfaces:**
- Produces: `Npc.region: str | None`, `NpcPatch.region: str | None`. `Session._npc_from_patch` copies `patch.region → npc.region`; `Session._merge_npc_patch` updates `npc.region` when `patch.region is not None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/game/test_npc_region.py
from sidequest.game.session import GameSnapshot, NpcPatch


def _snap() -> GameSnapshot:
    return GameSnapshot(genre="caverns_and_claudes", world="beneath_sunden")


def test_npc_from_patch_carries_region():
    snap = _snap()
    snap.apply_npc_patches([NpcPatch(name="Gnaw-Swarm", hp=6, threat_level=1, region="exp002.r3")])
    npc = next(n for n in snap.npcs if n.core.name == "Gnaw-Swarm")
    assert npc.region == "exp002.r3"


def test_merge_npc_patch_updates_region():
    snap = _snap()
    snap.apply_npc_patches([NpcPatch(name="Gnaw-Swarm", hp=6, threat_level=1, region="exp002.r3")])
    snap.apply_npc_patches([NpcPatch(name="Gnaw-Swarm", region="exp004.r1")])
    npc = next(n for n in snap.npcs if n.core.name == "Gnaw-Swarm")
    assert npc.region == "exp004.r1"
```

> Note: confirm the upsert entry point name. `apply_npc_patches` is the public batch upsert that routes to `_npc_from_patch` (spawn) / `_merge_npc_patch` (merge) — grep `def apply_npc_patches` in `session.py`; if the name differs, use the real one. The two helper methods under test are fixed.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/game/test_npc_region.py -v`
Expected: FAIL — `NpcPatch` rejects unexpected keyword `region` (`extra: forbid`).

- [ ] **Step 3: Add the model fields**

In `sidequest/game/session.py`, in `class Npc`, right after the `location: str | None = None` line (~150):

```python
    # Engine-owned region id (ADR-106 procedural dungeon / region-mode worlds).
    # Set ONLY by the region-population inject (Task 5); narrator-declared NPCs
    # and non-region worlds leave it None. Co-location seating prefers this over
    # the free-text scene when present (ADR-116, region-keyed seating).
    region: str | None = None
```

In `class NpcPatch`, right after `location: str | None = None` (~398):

```python
    region: str | None = None
    """Engine-owned region id (ADR-106). Set by the region-population inject;
    narrator/encountergen patches leave it None."""
```

- [ ] **Step 4: Carry it through `_npc_from_patch`**

In `_npc_from_patch`, add `region` to the `Npc(...)` constructor (after `location=patch.location,` ~1935):

```python
            location=patch.location,
            region=patch.region,
```

- [ ] **Step 5: Carry it through `_merge_npc_patch`**

In `_merge_npc_patch`, after the `if patch.location is not None:` block (~1849-1850):

```python
        if patch.location is not None:
            npc.location = patch.location
        if patch.region is not None:
            npc.region = patch.region
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/game/test_npc_region.py -v`
Expected: PASS (both tests).

- [ ] **Step 7: Commit**

```bash
git add sidequest/game/session.py tests/game/test_npc_region.py
git commit -m "feat(npc): add region stamp to Npc/NpcPatch for region-keyed seating"
```

---

### Task 2: Derive `threat_level` on `CuratedCreature` from the CR band

**Files:**
- Modify: `sidequest/dungeon/materializer.py` (`CuratedCreature` ~458-476; new `_threat_from_band` helper; 4 `CuratedCreature(...)` construction sites: ~986-993 and ~1013-1018 in `_creatures_from_manifest`; ~1467-1474 and ~1501-1506 in `_stage_curate`)
- Test: `tests/dungeon/test_region_population.py`

**Interfaces:**
- Produces: `CuratedCreature.threat_level: int` (1-4). `_threat_from_band(bundle: CookbookBundle, cr_band: str) -> int` returns `clamp(band_order()[cr_band] + 1, 1, 4)`.
- Consumes: `bundle.affinities.band_order()` (`cookbook/models.py:127`), `manifest.cr_band`.

- [ ] **Step 1: Write the failing test**

```python
# tests/dungeon/test_region_population.py
from sidequest.dungeon.materializer import CuratedCreature, _threat_from_band
from sidequest.game.cookbook.loader import load_cookbook
from pathlib import Path


def _bundle():
    # The cookbook ships with beneath_sunden; resolve via the genre loader the
    # same way session_integration does. Adjust the path if the fixture cookbook
    # lives elsewhere — grep `load_cookbook(` for a test precedent.
    from sidequest.genre.loader import DEFAULT_GENRE_PACK_SEARCH_PATHS, GenreLoader
    root = GenreLoader(search_paths=DEFAULT_GENRE_PACK_SEARCH_PATHS).find("caverns_and_claudes")
    return load_cookbook(root / "worlds" / "beneath_sunden")


def test_threat_from_band_shallow_is_tier_one():
    bundle = _bundle()
    shallow = bundle.affinities.cr_bands[0].id
    assert _threat_from_band(bundle, shallow) == 1


def test_threat_from_band_clamps_to_four():
    bundle = _bundle()
    deepest = bundle.affinities.cr_bands[-1].id
    assert 1 <= _threat_from_band(bundle, deepest) <= 4


def test_curated_creature_has_threat_level_field():
    from sidequest.game.creature_core import hp_pool_from_hp
    c = CuratedCreature(name="x", creature_type="t", telegraph="g", hp=hp_pool_from_hp(4), threat_level=2)
    assert c.threat_level == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/dungeon/test_region_population.py -v`
Expected: FAIL — `_threat_from_band` does not exist / `CuratedCreature` has no `threat_level`.

- [ ] **Step 3: Add the `threat_level` field**

In `materializer.py`, in `class CuratedCreature`, after `hp: HpPool` (~476):

```python
    hp: HpPool
    # ADR-114 discipline: NO raw cr. ``threat_level`` is the DERIVED B/X tier
    # (1-4) from the region's CR band — the legible difficulty signal the
    # inject stamps onto the runtime Npc (Keith ruling 2026-06-22: derive from
    # CR band). Big-bad gets the region tier +1 (capped at 4).
    threat_level: int
```

- [ ] **Step 4: Add the `_threat_from_band` helper**

In `materializer.py`, near `_hp_from_cr` (~278), add:

```python
def _threat_from_band(bundle: CookbookBundle, cr_band: str) -> int:
    """Derive the 1-4 B/X threat tier from a region's CR band (Keith ruling
    2026-06-22). ``band_order`` is the shallow<mid<deep ordinal; tier is the
    1-based ordinal clamped to [1, 4]. An unknown band is a loud bug, not a
    silent default (No Silent Fallbacks)."""
    order = bundle.affinities.band_order()
    if cr_band not in order:
        raise CurationError(
            f"cr_band {cr_band!r} is not in affinities.cr_bands {sorted(order)} "
            f"— cannot derive a threat tier"
        )
    return min(4, max(1, order[cr_band] + 1))
```

- [ ] **Step 5: Stamp `threat_level` at all four construction sites**

In `_creatures_from_manifest` — the wandering-row `CuratedCreature(...)` (~986):

```python
        creatures.append(
            CuratedCreature(
                name=str(row["name"]),
                creature_type=str(row.get("type", "")),
                telegraph=str(row.get("telegraph", "")),
                hp=_hp_from_cr(float(row["cr"])),
                threat_level=_threat_from_band(bundle, manifest.cr_band),
            )
        )
```

The big_bad in the same function (~1013):

```python
        big_bad = CuratedCreature(
            name=str(bb_src.get("name", "")),
            creature_type="big_bad",
            telegraph=str(bb_src.get("min_band", "")),
            hp=_hp_from_cr(float(bb_cr)),
            threat_level=min(4, _threat_from_band(bundle, manifest.cr_band) + 1),
        )
```

In `_stage_curate` — the curated wandering-row site (~1467):

```python
                creatures.append(
                    CuratedCreature(
                        name=str(row["name"]),
                        creature_type=str(row.get("type", "")),
                        telegraph=str(row.get("telegraph", "")),
                        hp=_hp_from_cr(float(row["cr"])),
                        threat_level=_threat_from_band(bundle, manifest.cr_band),
                    )
                )
```

The curated big_bad site (~1501):

```python
                region_big_bad[region_id] = CuratedCreature(
                    name=str(bb_v.get("name", "")),
                    creature_type="big_bad",
                    telegraph=str(bb_v.get("min_band", "")),
                    hp=_hp_from_cr(float(bb_cr)),
                    threat_level=min(4, _threat_from_band(bundle, manifest.cr_band) + 1),
                )
```

Also update `_append_authored_creatures` (~1107) where it builds a `CuratedCreature` for an authored bestiary id — stamp `threat_level=int(getattr(entry, "level", 1) or 1)` (the authored bestiary carries its own level):

```python
        merged.append(
            CuratedCreature(
                name=entry.name,
                creature_type=str(getattr(entry, "role", "") or "authored"),
                telegraph=str(getattr(entry, "description", "") or ""),
                hp=hp_pool_from_hp(int(entry.hp)),
                threat_level=int(getattr(entry, "level", 1) or 1),
            )
        )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/dungeon/test_region_population.py -v`
Expected: PASS. Also run the existing materializer suite to catch the new required field:
Run: `uv run pytest tests/dungeon/test_materializer.py -v`
Expected: PASS (fix any in-test `CuratedCreature(...)` constructions that now need `threat_level`).

- [ ] **Step 7: Commit**

```bash
git add sidequest/dungeon/materializer.py tests/dungeon/test_region_population.py
git commit -m "feat(dungeon): derive CuratedCreature.threat_level from CR band"
```

---

### Task 3: Freeze the curated roster per-region at commit

**Files:**
- Modify: `sidequest/dungeon/materializer.py` (add `_curated_to_payload`; extend `_stage_commit` ~2014-2026, after the `setpiece_state` loop)
- Test: `tests/dungeon/test_region_population.py` (append)

**Interfaces:**
- Produces: per-region Postgres mutation rows `kind="region_population"`, payload `{"region_id": str, "creatures": [<curated payload>...], "big_bad": <curated payload>|None}`.
- Curated payload shape (the read/write contract): `{"name", "creature_type", "telegraph", "hp": <HpPool.model_dump()>, "threat_level": int}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/dungeon/test_region_population.py  (append)
from sidequest.dungeon.materializer import _curated_to_payload, CuratedCreature
from sidequest.game.creature_core import hp_pool_from_hp


def test_curated_to_payload_round_trips_shape():
    c = CuratedCreature(name="Gnaw-Swarm", creature_type="swarm", telegraph="chittering",
                        hp=hp_pool_from_hp(6), threat_level=1)
    p = _curated_to_payload(c)
    assert p == {
        "name": "Gnaw-Swarm",
        "creature_type": "swarm",
        "telegraph": "chittering",
        "hp": c.hp.model_dump(),
        "threat_level": 1,
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/dungeon/test_region_population.py::test_curated_to_payload_round_trips_shape -v`
Expected: FAIL — `_curated_to_payload` undefined.

- [ ] **Step 3: Add the serializer**

In `materializer.py`, near `CuratedCreature` (~477):

```python
def _curated_to_payload(c: CuratedCreature) -> dict:
    """JSON-safe payload for the per-region ``region_population`` mutation
    (Task 3). HpPool is a pydantic model → ``model_dump`` is JSON-safe."""
    return {
        "name": c.name,
        "creature_type": c.creature_type,
        "telegraph": c.telegraph,
        "hp": c.hp.model_dump(),
        "threat_level": c.threat_level,
    }
```

- [ ] **Step 4: Persist the roster in `_stage_commit`**

In `_stage_commit`, immediately after the `setpiece_state` `record_mutation` loop (after `rolled_persisted += 1`, ~2025) and before `for fe in new_frontier:` (~2027):

```python
        # Story 153-x: freeze each generated region's curated population on the
        # SAME txn (save-is-truth). Reuses the Plan-5 append-only primitive — no
        # new table. The entrance (Expansion 0) is authored content, not in
        # ``expansion.new_nodes``, so it is never given a procedural roster.
        pop_persisted = 0
        for node in expansion.new_nodes:
            roster = curation.region_creatures.get(node.id, [])
            big_bad = curation.region_big_bad.get(node.id)
            if not roster and big_bad is None:
                continue
            tx.record_mutation(
                node.id,
                "region_population",
                {
                    "region_id": node.id,
                    "creatures": [_curated_to_payload(c) for c in roster],
                    "big_bad": _curated_to_payload(big_bad) if big_bad is not None else None,
                },
            )
            pop_persisted += 1
```

Then add to the commit-span success summary (near ~2041 `span.set_attribute("regions_committed", ...)`):

```python
    span.set_attribute("region_populations_committed", pop_persisted)
```

- [ ] **Step 5: Write the persistence wiring test**

```python
# tests/dungeon/test_region_population.py  (append)
# End-to-end: materialize the beneath_sunden seed and assert region_population
# rows landed for the generated regions. Model this on the existing
# tests/dungeon/test_materializer_wiring.py harness (it already builds a
# MaterializationRequest + seed graph + a DungeonRepository fixture). Copy that
# fixture setup; then:
#
#   await materialize(request, graph=seed_graph, bundle=bundle, palette=palette,
#                     dungeon_repository=repo, snapshot=snap, pack_tropes=pack,
#                     claude_client=fake_client, pack=pack)
#   pops = [m for m in repo.load_mutations() if m.kind == "region_population"]
#   assert pops, "no region_population rows persisted"
#   sample = pops[0].payload
#   assert "creatures" in sample and isinstance(sample["creatures"], list)
#   assert all("threat_level" in c for c in sample["creatures"])
```

> Implementer: lift the exact fixture wiring (request, seed graph, repo, fake claude client) from `tests/dungeon/test_materializer_wiring.py` — do not hand-roll a new harness. The assertion block above is the new behavior.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/dungeon/test_region_population.py tests/dungeon/test_materializer_wiring.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add sidequest/dungeon/materializer.py tests/dungeon/test_region_population.py
git commit -m "feat(dungeon): freeze curated region population to the dungeon store at commit"
```

---

### Task 4: Region-population loader

**Files:**
- Create: `sidequest/server/dispatch/region_population.py`
- Test: `tests/server/dispatch/test_region_population.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class RegionCreature: name: str; creature_type: str; telegraph: str; hp: int; threat_level: int`
  - `load_region_population(dungeon_repository, region_id: str) -> tuple[list[RegionCreature], RegionCreature | None]`
- Consumes: `dungeon_repository.load_mutations() -> list[DungeonMutation]` (each has `.region_id`, `.kind`, `.payload`).

This module is intentionally decoupled from `materializer.CuratedCreature` (heavy import). The read side parses the JSON payload contract from Task 3 into a light local dataclass — keeping the inject hot path free of the materializer import.

- [ ] **Step 1: Write the failing test**

```python
# tests/server/dispatch/test_region_population.py
from sidequest.server.dispatch.region_population import RegionCreature, load_region_population


class _FakeMutation:
    def __init__(self, region_id, kind, payload):
        self.region_id, self.kind, self.payload = region_id, kind, payload


class _FakeRepo:
    def __init__(self, muts):
        self._muts = muts

    def load_mutations(self):
        return self._muts


def _pop_payload(region_id):
    return {
        "region_id": region_id,
        "creatures": [
            {"name": "Gnaw-Swarm", "creature_type": "swarm", "telegraph": "chittering",
             "hp": {"current": 6, "max": 6, "base_max": 6}, "threat_level": 1}
        ],
        "big_bad": {"name": "Pale Mother", "creature_type": "big_bad", "telegraph": "deep band",
                    "hp": {"current": 24, "max": 24, "base_max": 24}, "threat_level": 2},
    }


def test_load_region_population_parses_roster_and_big_bad():
    repo = _FakeRepo([
        _FakeMutation("exp002.r3", "setpiece_state", {"x": 1}),
        _FakeMutation("exp002.r3", "region_population", _pop_payload("exp002.r3")),
    ])
    roster, big_bad = load_region_population(repo, "exp002.r3")
    assert [c.name for c in roster] == ["Gnaw-Swarm"]
    assert roster[0].hp == 6 and roster[0].threat_level == 1
    assert big_bad is not None and big_bad.name == "Pale Mother" and big_bad.threat_level == 2


def test_load_region_population_empty_for_unknown_region():
    repo = _FakeRepo([_FakeMutation("exp002.r3", "region_population", _pop_payload("exp002.r3"))])
    roster, big_bad = load_region_population(repo, "exp999.r9")
    assert roster == [] and big_bad is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/server/dispatch/test_region_population.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Write the loader**

```python
# sidequest/server/dispatch/region_population.py
"""Read side of the procedural region population (Task 4, ADR-106 / ADR-059).

The materializer freezes each generated region's curated roster as a
``region_population`` dungeon mutation (Task 3). This module reads it back by
region id for the Monster-Manual inject seam — decoupled from the heavy
``materializer.CuratedCreature`` import so the per-turn inject path stays light.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RegionCreature:
    """One frozen procedural creature, parsed from the region_population payload."""

    name: str
    creature_type: str
    telegraph: str
    hp: int
    threat_level: int


def _parse(d: dict[str, Any]) -> RegionCreature:
    hp = d["hp"]
    return RegionCreature(
        name=str(d["name"]),
        creature_type=str(d.get("creature_type", "")),
        telegraph=str(d.get("telegraph", "")),
        hp=int(hp["max"] if isinstance(hp, dict) else hp),
        threat_level=int(d["threat_level"]),
    )


def load_region_population(
    dungeon_repository: Any, region_id: str
) -> tuple[list[RegionCreature], RegionCreature | None]:
    """Return ``(roster, big_bad)`` for ``region_id``. Empty roster + None for a
    region with no frozen population (a region materialized before this feature,
    or a non-procedural world) — an absent binding, not a silent fallback."""
    roster: list[RegionCreature] = []
    big_bad: RegionCreature | None = None
    for m in dungeon_repository.load_mutations():
        if m.kind != "region_population" or m.region_id != region_id:
            continue
        roster = [_parse(c) for c in m.payload.get("creatures", [])]
        bb = m.payload.get("big_bad")
        big_bad = _parse(bb) if bb is not None else None
    return roster, big_bad
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/server/dispatch/test_region_population.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sidequest/server/dispatch/region_population.py tests/server/dispatch/test_region_population.py
git commit -m "feat(dungeon): region-population loader over the dungeon mutation store"
```

---

### Task 5: Inject the region population (region-stamped) into the snapshot

**Files:**
- Modify: `sidequest/server/dispatch/monster_manual_inject.py` (add `_creature_patch_from_region_creature`, `_npc_patches_for_region_population`; wire into `inject()` ~601-624 next to the room-binding call)
- Modify: `sidequest/telemetry/spans/monster_manual.py` (add `SPAN_MONSTER_MANUAL_REGION_POPULATION`)
- Test: `tests/server/dispatch/test_monster_manual_inject.py` (append)

**Interfaces:**
- Consumes: `load_region_population` (Task 4), `NpcPatch.region` (Task 1), `sd.dungeon_repository`, `_OUT_OF_COMBAT_ENCOUNTER_LIMIT` (existing).
- Produces: region-stamped `NpcPatch`es in `snapshot.npcs`; span `monster_manual.region_population` `{region_id, creature_count, big_bad}`.

- [ ] **Step 1: Add the span constant**

In `sidequest/telemetry/spans/monster_manual.py`, after `SPAN_MONSTER_MANUAL_STALE_PURGED` (~39):

```python
# Story 153-x (ADR-106 region population): emitted when a generated region's
# frozen procedural roster (Task 3) is injected into snapshot.npcs, region-
# stamped for region-keyed seating. The GM-panel lie-detector that procedural
# rooms field real, statted creatures instead of leaving the narrator to improvise.
SPAN_MONSTER_MANUAL_REGION_POPULATION = "monster_manual.region_population"
```

And register it (~45):

```python
FLAT_ONLY_SPANS.add(SPAN_MONSTER_MANUAL_REGION_POPULATION)
```

- [ ] **Step 2: Write the failing test**

```python
# tests/server/dispatch/test_monster_manual_inject.py  (append)
# Reuses the file's existing _snapshot() helper and _FakeSessionData stand-in.

from sidequest.server.dispatch import region_population as _rp


def test_inject_region_population_stamps_region(monkeypatch):
    snap = _snapshot()
    snap.seed_pc_regions("exp002.r3")  # PC seated in the region
    sd = _session_data_with_manual()   # the file's existing helper that seeds a Manual

    def _fake_load(repo, region_id):
        assert region_id == "exp002.r3"
        return ([_rp.RegionCreature(name="Gnaw-Swarm", creature_type="swarm",
                                    telegraph="chittering", hp=6, threat_level=1)], None)

    monkeypatch.setattr(_rp, "load_region_population", _fake_load)
    monster_manual_inject.inject(sd, snap, current_location="The Winding Catacomb",
                                 in_combat=False, room_id="exp002.r3")
    gnaw = next(n for n in snap.npcs if n.core.name == "Gnaw-Swarm")
    assert gnaw.region == "exp002.r3"
    assert gnaw.location == "The Winding Catacomb"


def test_inject_region_population_emits_span(otel_capture, monkeypatch):
    from sidequest.telemetry.spans.monster_manual import SPAN_MONSTER_MANUAL_REGION_POPULATION
    snap = _snapshot()
    snap.seed_pc_regions("exp002.r3")
    sd = _session_data_with_manual()
    monkeypatch.setattr(_rp, "load_region_population",
                        lambda repo, rid: ([_rp.RegionCreature("Gnaw-Swarm", "swarm", "g", 6, 1)], None))
    monster_manual_inject.inject(sd, snap, current_location="The Winding Catacomb",
                                 in_combat=False, room_id="exp002.r3")
    assert any(s.name == SPAN_MONSTER_MANUAL_REGION_POPULATION for s in otel_capture.spans)
```

> Implementer: the file already has helpers named like `_snapshot()` and a `_FakeSessionData`. Use the real helper names from the top of `test_monster_manual_inject.py` (grep `_SessionData`/`_snapshot`). `_session_data_with_manual()` stands for "an sd whose `monster_manual` is seeded and whose `dungeon_repository` is a fake" — build it from the existing stand-in; `dungeon_repository` can be any object since `load_region_population` is monkeypatched here. `otel_capture` is the project's existing span-capture fixture (used elsewhere in this same file).

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/server/dispatch/test_monster_manual_inject.py -k region_population -v`
Expected: FAIL — region patches not injected; span not emitted.

- [ ] **Step 4: Add the patch translator + the region-population patch builder**

In `monster_manual_inject.py`, after `_creature_patch_from_bestiary_entry` (~509):

```python
def _creature_patch_from_region_creature(
    rc: Any, *, location: str | None, region: str
) -> NpcPatch:
    """Translate one frozen ``RegionCreature`` (Task 4) into a region-stamped
    creature patch. ``region`` is the engine-owned key co-location seats on
    (ADR-116); ``location`` is the free-text scene for narrator/UI display."""
    return NpcPatch(
        name=rc.name,
        description=rc.telegraph or None,
        role=rc.creature_type or None,
        creature_id=rc.creature_type or None,
        threat_level=rc.threat_level,
        hp=rc.hp,
        location=location,
        region=region,
        manual_origin=True,
    )


def _npc_patches_for_region_population(
    sd: _SessionData, region_id: str, *, current_location: str, in_combat: bool
) -> list[NpcPatch]:
    """Build region-stamped creature patches from the region's frozen procedural
    roster (Task 3/4). Out of combat the roster is capped at
    ``_OUT_OF_COMBAT_ENCOUNTER_LIMIT`` (Keith ruling 2026-06-22); the big-bad is
    always present-but-telegraphed (it only SEATS when the player engages).
    Emits ``monster_manual.region_population``. Returns ``[]`` for a region with
    no frozen population (absent binding, not a fallback)."""
    repo = getattr(sd, "dungeon_repository", None)
    if repo is None:
        return []
    from sidequest.server.dispatch.region_population import load_region_population

    roster, big_bad = load_region_population(repo, region_id)
    if not roster and big_bad is None:
        return []
    creature_location = current_location or None
    capped = roster if in_combat else roster[:_OUT_OF_COMBAT_ENCOUNTER_LIMIT]
    patches = [
        _creature_patch_from_region_creature(rc, location=creature_location, region=region_id)
        for rc in capped
    ]
    if big_bad is not None:
        patches.append(
            _creature_patch_from_region_creature(
                big_bad, location=creature_location, region=region_id
            )
        )
    with Span.open(
        SPAN_MONSTER_MANUAL_REGION_POPULATION,
        {
            "region_id": region_id,
            "creature_count": len(capped),
            "big_bad": big_bad is not None,
        },
    ):
        pass
    return patches
```

Add the imports at the top of the file if missing: `from sidequest.telemetry.spans import Span` and `from sidequest.telemetry.spans.monster_manual import SPAN_MONSTER_MANUAL_REGION_POPULATION` (the file already imports other `SPAN_MONSTER_MANUAL_*` names ~42 — extend that import).

- [ ] **Step 5: Wire it into `inject()`**

In `inject()`, find where the authored room-binding patches are gathered (the `room_id is not None` branch that calls `_npc_patches_for_room_binding`, ~601-624). Right after the authored binding patches are appended to `all_patches`, add the procedural population — de-duped so an authored creature always wins (matches `_append_authored_creatures` precedence):

```python
        if room_id is not None and combat_encounters:
            authored = _npc_patches_for_room_binding(sd, room_id, current_location)
            all_patches.extend(authored)
            authored_names = {p.name for p in authored}
            region_pop = _npc_patches_for_region_population(
                sd, room_id, current_location=current_location, in_combat=in_combat
            )
            all_patches.extend(p for p in region_pop if p.name not in authored_names)
```

> Implementer: the exact existing lines that call `_npc_patches_for_room_binding` may differ slightly — preserve the existing authored-binding call and its span; the ONLY additions are the `authored_names` set and the `region_pop` extend. Do not duplicate the authored call.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/server/dispatch/test_monster_manual_inject.py -v`
Expected: PASS (new region tests + existing tests).

- [ ] **Step 7: Commit**

```bash
git add sidequest/server/dispatch/monster_manual_inject.py sidequest/telemetry/spans/monster_manual.py tests/server/dispatch/test_monster_manual_inject.py
git commit -m "feat(dungeon): inject frozen region population, region-stamped, into snapshot"
```

---

### Task 6: Region-aware confrontation co-location

**Files:**
- Modify: `sidequest/server/dispatch/encounter_lifecycle.py` (new `_co_located` helper; 3 predicate sites: `_npc_fallback_at_location` ~942-948, `_resolve_opponent_from_roster` ~1016-1026, `_friendly_fallback_at_location` ~1122-1127; `confrontation.colocation` span before the no-opponent guard ~1597)
- Modify: `sidequest/telemetry/spans/encounter.py` (add `SPAN_CONFRONTATION_COLOCATION`)
- Test: `tests/server/dispatch/test_encounter_colocation.py`

**Interfaces:**
- Produces: `_co_located(npc, *, pc_region: str | None, scene_match: bool) -> bool`; span `confrontation.colocation` `{encounter_type, pc_region, match_mode, opponent_count}`.
- Behavior: when `npc.region` and `pc_region` are both truthy, co-location == `npc.region == pc_region`; otherwise == `scene_match` (caller's existing free-text result — unchanged for region-less NPCs).

- [ ] **Step 1: Write the failing test**

```python
# tests/server/dispatch/test_encounter_colocation.py
from sidequest.server.dispatch.encounter_lifecycle import _co_located
from sidequest.game.session import Npc
from sidequest.game.creature_core import CreatureCore, HpPool


def _npc(name, *, region=None, last_seen=None):
    n = Npc(core=CreatureCore(name=name, description="d", personality="p",
                              level=1, xp=0, hp=HpPool(current=6, max=6, base_max=6)))
    n.region = region
    n.last_seen_location = last_seen
    return n


def test_region_match_seats_when_regions_equal():
    npc = _npc("Gnaw-Swarm", region="exp002.r3", last_seen="Under the Rope")
    # Scene mismatch ("Under the Rope" != PC scene) but region matches → co-located.
    assert _co_located(npc, pc_region="exp002.r3", scene_match=False) is True


def test_region_mismatch_blocks_even_if_scene_would_match():
    npc = _npc("Gnaw-Swarm", region="exp004.r1")
    assert _co_located(npc, pc_region="exp002.r3", scene_match=True) is False


def test_regionless_npc_falls_back_to_scene_match():
    npc = _npc("Innkeeper", region=None)
    assert _co_located(npc, pc_region="exp002.r3", scene_match=True) is True
    assert _co_located(npc, pc_region="exp002.r3", scene_match=False) is False


def test_no_pc_region_falls_back_to_scene_match():
    npc = _npc("Gnaw-Swarm", region="exp002.r3")
    assert _co_located(npc, pc_region=None, scene_match=True) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/server/dispatch/test_encounter_colocation.py -v`
Expected: FAIL — `_co_located` undefined.

- [ ] **Step 3: Add the helper**

In `encounter_lifecycle.py`, above `_npc_fallback_at_location` (~882):

```python
def _co_located(npc: Npc, *, pc_region: str | None, scene_match: bool) -> bool:
    """ADR-116 co-location for seating. Prefer the engine-owned region id when
    the candidate carries a region stamp (procedural-dungeon creatures, Task 1);
    otherwise fall back to the caller's free-text scene match — narrator NPCs and
    all non-procedural worlds carry no ``region``, so their behavior is unchanged.
    This is the fix for region-keyed seating: co-location no longer depends on
    the narrator-owned scene string that drifts across seams/turns (Gap B)."""
    if npc.region and pc_region:
        return npc.region == pc_region
    return scene_match
```

- [ ] **Step 4: Apply at the opponent fallback (`_npc_fallback_at_location`)**

After `location = snapshot.party_location(perspective=acting_character_name)` (~942) add:

```python
    pc_region = snapshot.region_for(perspective=acting_character_name)
```

Change the loop guard (~948) from:

```python
        if npc.last_seen_location != location:
            continue
```
to:
```python
        if not _co_located(npc, pc_region=pc_region, scene_match=(npc.last_seen_location == location)):
            continue
```

- [ ] **Step 5: Apply at the roster resolver (`_resolve_opponent_from_roster`)**

After `location = snapshot.party_location(perspective=acting_character_name)` (~1016) add:

```python
    pc_region = snapshot.region_for(perspective=acting_character_name)
```

Change the candidate comprehension condition (~1023) from:

```python
        and (n.last_seen_location == location or n.location == location)
```
to:
```python
        and _co_located(
            n, pc_region=pc_region,
            scene_match=(n.last_seen_location == location or n.location == location),
        )
```

> This is exactly the over-reach the ~1006-1010 comment said was unsafe "because NPCs carry no region key." With Task 1's `npc.region`, region matching IS now safe — update that comment to note region matching is gated on the stamp.

- [ ] **Step 6: Apply at the friendly seater (`_friendly_fallback_at_location`)**

After `location = snapshot.party_location(perspective=acting_character_name)` (~1122) add:

```python
    pc_region = snapshot.region_for(perspective=acting_character_name)
```

Change the loop guard (~1127) from:

```python
        if npc.last_seen_location != location:
            continue
```
to:
```python
        if not _co_located(npc, pc_region=pc_region, scene_match=(npc.last_seen_location == location)):
            continue
```

- [ ] **Step 7: Run the helper tests to verify they pass**

Run: `uv run pytest tests/server/dispatch/test_encounter_colocation.py -v`
Expected: PASS.

- [ ] **Step 8: Add the `confrontation.colocation` span constant**

In `sidequest/telemetry/spans/encounter.py`, near the other encounter span constants:

```python
# Story 153-x (ADR-116 region-keyed seating): emitted once per seating attempt
# in instantiate_encounter_from_trigger recording which co-location mode decided
# whether an Other was found. ``match_mode=region`` means the engine-owned region
# id seated (or declined) the opponent; ``scene`` means the legacy free-text path.
SPAN_CONFRONTATION_COLOCATION = "confrontation.colocation"
```

Register it in that file's `FLAT_ONLY_SPANS.add(...)` block (mirror the existing `encounter.*` registrations in the same file).

- [ ] **Step 9: Emit the span in `instantiate_encounter_from_trigger`**

Immediately before the no-opponent guard (`if (_requires_opponent(cdef)` ~1597), where `npcs_present`, `player_name`, `snapshot`, `encounter_type` are in scope:

```python
    _pc_region = snapshot.region_for(perspective=player_name)
    from sidequest.telemetry.spans import Span
    from sidequest.telemetry.spans.encounter import SPAN_CONFRONTATION_COLOCATION

    with Span.open(
        SPAN_CONFRONTATION_COLOCATION,
        {
            "encounter_type": encounter_type,
            "pc_region": _pc_region or "",
            "match_mode": "region" if _pc_region else "scene",
            "opponent_count": len(npcs_present),
        },
    ):
        pass
```

- [ ] **Step 10: Write the span-emission test**

```python
# tests/server/dispatch/test_encounter_colocation.py  (append)
# Drive instantiate_encounter_from_trigger through a fixture snapshot where the
# PC is region-seated and a region-stamped hostile creature shares the region;
# assert the confrontation seats (no NoOpponentAvailableError) and the
# confrontation.colocation span fired with match_mode == "region".
#
# Model the trigger/fixture setup on an existing
# tests/server/dispatch/test_encounter_lifecycle*.py test (grep
# instantiate_encounter_from_trigger in tests/). The new assertions:
#   assert any(s.name == "confrontation.colocation" and s.attributes["match_mode"] == "region"
#              for s in otel_capture.spans)
```

> Implementer: lift the `instantiate_encounter_from_trigger` invocation + fixture from the nearest existing lifecycle test. The behavioral assertions (seats + span match_mode=region) are the new coverage.

- [ ] **Step 11: Run tests to verify they pass**

Run: `uv run pytest tests/server/dispatch/test_encounter_colocation.py -v`
Expected: PASS.

- [ ] **Step 12: Commit**

```bash
git add sidequest/server/dispatch/encounter_lifecycle.py sidequest/telemetry/spans/encounter.py tests/server/dispatch/test_encounter_colocation.py
git commit -m "feat(confrontation): region-keyed co-location seating (ADR-116), self-gated by npc.region"
```

---

### Task 7: End-to-end wiring test + full gate

**Files:**
- Test: `tests/dungeon/test_region_population_wiring.py`

**Interfaces:**
- Consumes: everything above. This is the integration test the CLAUDE.md "Every Test Suite Needs a Wiring Test" rule requires — proves generate → freeze → load → inject → seat is connected end-to-end via spans/behavior, not source-text.

- [ ] **Step 1: Write the end-to-end wiring test**

```python
# tests/dungeon/test_region_population_wiring.py
# Integration: materialize the beneath_sunden seed (lift the harness from
# tests/dungeon/test_materializer_wiring.py), then exercise the inject seam for a
# generated region and assert a region-stamped creature lands in snapshot.npcs.
#
#   await materialize(... pack=pack)                      # Task 3 freezes rosters
#   region_id = next(n.id for n in repo.load_map(entrance_id="entrance").nodes
#                    if n.id != "entrance")               # a generated region
#   snap.seed_pc_regions(region_id)
#   monster_manual_inject.ensure_loaded(sd)
#   monster_manual_inject.inject(sd, snap, current_location="probe",
#                                in_combat=True, room_id=region_id)
#   injected = [n for n in snap.npcs if n.region == region_id]
#   assert injected, "no region-stamped creatures injected for a generated region"
#   assert all(n.threat_level is not None for n in injected)
```

> Implementer: `sd` here must carry the real `dungeon_repository` (the materialize target) and a seeded `monster_manual` — build it from the wiring-test harness, not the unit fakes, so `load_region_population` reads the rows Task 3 actually wrote.

- [ ] **Step 2: Run the wiring test**

Run: `uv run pytest tests/dungeon/test_region_population_wiring.py -v`
Expected: PASS.

- [ ] **Step 3: Run the full gate**

Run: `uv run ruff format . && uv run ruff check . && uv run pyright && uv run pytest`
Expected: clean format, no lint errors, no type errors, all tests PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/dungeon/test_region_population_wiring.py
git commit -m "test(dungeon): end-to-end region-population wiring (generate→freeze→inject→seat)"
```

---

## Self-Review

**1. Spec coverage:**
- Spec Part A (persist roster) → Task 3 (+ serializer) ✓
- Spec Part B (inject by region_id) → Task 5 ✓
- Spec Part C (co-location by region) → Task 6 ✓
- Spec Part D (stamp consistency / Gap B) → folded into Task 5's `region` stamp + Task 6's region-keyed predicate (documented in Architecture) ✓
- Model changes (`Npc.region`, `NpcPatch.region`, `CuratedCreature` threat) → Task 1, Task 2 ✓
- OTEL (`dungeon.region_population_injected`, `confrontation.colocation`) → Task 5 (named `monster_manual.region_population`), Task 6 ✓
- Crunch rulings: big-bad present+telegraphed → Task 5 (big_bad always injected, uncapped, seats only on engage); threat from CR band → Task 2; out-of-combat cap → Task 5 (`_OUT_OF_COMBAT_ENCOUNTER_LIMIT`) ✓
- Freeze/resume → Task 3 (one frozen write on the commit txn); migration (old saves empty) → loader returns empty, documented in Task 4 ✓

**2. Placeholder scan:** No "TBD"/"add error handling"/"similar to". Where a test must reuse an existing harness, the harness file is named and the *new* assertions are shown in full. Implementation steps all carry real code.

**3. Type consistency:** `RegionCreature` (Task 4) has `hp: int` (parsed from the payload's `hp.max`); `_creature_patch_from_region_creature` (Task 5) passes `hp=rc.hp` into `NpcPatch.hp: int | None` ✓. `CuratedCreature.threat_level: int` (Task 2) → payload `threat_level` (Task 3) → `RegionCreature.threat_level: int` (Task 4) → `NpcPatch.threat_level: int | None` (Task 5) ✓. `_co_located(npc, *, pc_region, scene_match)` signature identical across Task 6 sites ✓. Span name `monster_manual.region_population` consistent between Task 5 def and test ✓.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-06-22-dungeon-region-population.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**
