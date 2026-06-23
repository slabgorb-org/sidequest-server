"""RED (story 126-24, rework round 1) — world-tier narrative-chargen seed-table
override wiring through the PRODUCTION loader + the resolver, end-to-end.

Reviewer (Hermes) REJECT, 2026-06-19: the world-override half of AC2 was half-wired.
`resolve_fate_chargen_seed_table` reads `getattr(world, "chargen_seed_table", {})`, but
`World` has no such field and the loader (`_assemble_world`) never populates one — so the
world tier is always `{}` and a real world can NEVER override. The original AC2 unit test
masked this by injecting pre-built `FateHintSeed` objects via a `_FakeWorld` duck-type;
it never exercised the loader→pydantic-coercion path. If a value DID leak in via `World`'s
`extra="allow"` bag it would arrive as a raw `dict`, and `seed.pyramid` would crash chargen
with `AttributeError`.

This pins the LOAD + RESOLVE halves with the REAL `World` model and the REAL loader — no
fakes, no hand-named raw dicts. Mirrors `tests/genre/test_126_25_world_gear_load.py`
(the world-tier `gear.yaml` → typed `World.gear` precedent the Reviewer cited): a typed
`World.chargen_seed_table: dict[str, FateHintSeed]` field, loaded UNCONDITIONALLY of ruleset
from `worlds/<slug>/chargen_seed_table.yaml`, coerced to `FateHintSeed` by pydantic.

PINNED CONTRACT (Dev implements to these — rework):
- `World.chargen_seed_table: dict[str, FateHintSeed] = Field(default_factory=dict)` — typed,
  so YAML dicts coerce to `FateHintSeed` (no raw-dict crash), mirroring `World.gear`.
- the loader populates it from `worlds/<slug>/chargen_seed_table.yaml` (a `hint -> {pyramid,
  aspects}` map), unconditional of ruleset (mirrors the genre-tier load), `{}` when absent.
- `resolve_fate_chargen_seed_table(loaded_pack, world_slug)` then returns the world's
  `FateHintSeed` (world wins per hint key) with `.pyramid` / `.aspects` accessible.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sidequest.game.ruleset.fate_chargen import resolve_fate_chargen_seed_table
from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.pack import World
from sidequest.genre.models.rules import FateHintSeed

# A world-tier seed-table override authored as RAW YAML (hint -> {pyramid, aspects}).
# The pyramid is a legal [1,2,3,4] @ apex 4 allocation over the test_genre skill space;
# the exact skills don't matter to the load/resolve wiring (legality is the builder's
# present-time concern) — what matters is the raw dict coerces to FateHintSeed.
_SEED_TABLE_YAML = """\
Detective:
  pyramid:
    Investigate: 4
    Notice: 3
    Contacts: 3
    Deceive: 2
    Shoot: 2
    Rapport: 2
    Will: 1
    Stealth: 1
    Fight: 1
    Provoke: 1
  aspects:
    - "A Name Whispered in the Annees Folles"
    - "The Case That Never Closed"
"""


def _arm_world_seed_table(pack: Any, seed_yaml: str) -> Path:
    """Write a world-tier chargen_seed_table.yaml into the MinimalPack clone's
    flickering_reach world (pattern: test_126_25_world_gear_load._arm_world_gear)."""
    root = Path(pack.path)
    world_dir = root / "worlds" / "flickering_reach"
    assert world_dir.is_dir(), f"fixture world missing: {world_dir}"
    (world_dir / "chargen_seed_table.yaml").write_text(seed_yaml, encoding="utf-8")
    return root


# ── Model seam ─────────────────────────────────────────────────────────────────


def test_world_model_has_chargen_seed_table_field() -> None:
    """World grows a typed ``chargen_seed_table`` field — a world-tier override map,
    sibling to ``World.gear``, valued by ``FateHintSeed`` so pydantic coerces YAML
    dicts (no raw-dict crash at ``seed.pyramid``)."""
    assert "chargen_seed_table" in World.model_fields, (
        "World must grow a typed `chargen_seed_table` field (the Reviewer blocker)"
    )
    field = World.model_fields["chargen_seed_table"]
    # dict[str, FateHintSeed] -> __args__ == (str, FateHintSeed)
    args = getattr(field.annotation, "__args__", (field.annotation,))
    assert FateHintSeed in args, "World.chargen_seed_table must be dict[str, FateHintSeed]"


# ── Production loader path (coercion proof — the masked path) ─────────────────────


def test_loader_populates_world_chargen_seed_table_as_fate_hint_seed(
    minimal_pack_factory: Any, tmp_path: Path
) -> None:
    """worlds/<slug>/chargen_seed_table.yaml present → the loader populates
    ``World.chargen_seed_table`` and pydantic COERCES each raw entry into a
    ``FateHintSeed`` (so ``.pyramid`` / ``.aspects`` are real attributes, not dict
    keys). This is the exact path the _FakeWorld unit test bypassed."""
    pack = minimal_pack_factory(tmp_path)
    root = _arm_world_seed_table(pack, _SEED_TABLE_YAML)
    loaded = load_genre_pack(root)
    world = loaded.worlds["flickering_reach"]

    assert "Detective" in world.chargen_seed_table, (
        "loader must populate World.chargen_seed_table from worlds/<slug>/chargen_seed_table.yaml"
    )
    seed = world.chargen_seed_table["Detective"]
    assert isinstance(seed, FateHintSeed), (
        "world seed entries must be coerced to FateHintSeed, not left as raw dicts "
        "(the AttributeError-crash the Reviewer flagged)"
    )
    assert seed.pyramid["Investigate"] == 4
    assert "The Case That Never Closed" in seed.aspects


def test_loader_absent_world_seed_table_is_empty(minimal_pack_factory: Any, tmp_path: Path) -> None:
    """No worlds/<slug>/chargen_seed_table.yaml → ``World.chargen_seed_table == {}``.
    Absence is an authored choice (most worlds ship no override), never an error and
    never a default (No Silent Fallbacks)."""
    pack = minimal_pack_factory(tmp_path)
    loaded = load_genre_pack(pack.path)
    assert loaded.worlds["flickering_reach"].chargen_seed_table == {}


# ── Resolver wiring (end-to-end: loaded World → resolve → world wins) ─────────────


def test_resolve_returns_world_override_from_loaded_world(
    minimal_pack_factory: Any, tmp_path: Path
) -> None:
    """The Reviewer's core demand: prove the world override reaches the resolver via the
    REAL load path (not a _FakeWorld). With a world that authors a chargen_seed_table,
    ``resolve_fate_chargen_seed_table`` returns the world's FateHintSeed (world wins) and
    its ``.pyramid`` is accessible — no raw-dict AttributeError."""
    pack = minimal_pack_factory(tmp_path)
    root = _arm_world_seed_table(pack, _SEED_TABLE_YAML)
    loaded = load_genre_pack(root)

    resolved = resolve_fate_chargen_seed_table(loaded, "flickering_reach")

    assert "Detective" in resolved, "world-tier override must reach the resolver"
    assert isinstance(resolved["Detective"], FateHintSeed)
    assert resolved["Detective"].pyramid["Investigate"] == 4  # .pyramid must not crash
    # No world authored → pure genre baseline (the dial test_genre pack has no fate table).
    assert resolve_fate_chargen_seed_table(loaded, None) == {}
