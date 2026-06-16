"""init_chassis_registry materializes ChassisInstance + projects to npc_registry."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SPACE_OPERA = REPO_ROOT / "sidequest-content" / "genre_packs" / "space_opera"


def _make_snapshot(genre_slug: str, world_slug: str):
    """Build a minimal GameSnapshot for chassis-init tests."""
    from sidequest.game.session import GameSnapshot

    return GameSnapshot(
        genre_slug=genre_slug,
        world_slug=world_slug,
        location="Unknown",
    )


def test_init_chassis_registry_loads_kestrel() -> None:
    if not SPACE_OPERA.exists():
        pytest.skip("space_opera content pack not present")
    from sidequest.game.chassis import init_chassis_registry
    from sidequest.genre.loader import load_genre_pack
    from sidequest.magic.state import MagicState

    pack = load_genre_pack(SPACE_OPERA)
    snap = _make_snapshot("space_opera", "coyote_star")
    # S1 invariant (2026-05-04): magic_state initialized first.
    snap.magic_state = MagicState.from_config(_make_coyote_star_magic_config())
    init_chassis_registry(snap, pack)

    assert "kestrel" in snap.chassis_registry
    kestrel = snap.chassis_registry["kestrel"]
    assert kestrel.name == "Kestrel"
    assert kestrel.class_id == "voidborn_freighter"
    assert len(kestrel.bond_ledger) == 1
    assert kestrel.bond_ledger[0].character_id == "player_character"
    assert kestrel.bond_ledger[0].bond_tier_chassis == "trusted"
    # voice block carried over from the world layer
    assert kestrel.voice is not None
    assert "dry as bonemeal" in kestrel.voice.vocal_tics


def test_init_chassis_registry_does_not_project_into_npc_pool() -> None:
    """Wave 2A (story 45-47): the chassis-into-npc-registry projection has
    been REMOVED. Chassis surface in the narrator prompt via the dedicated
    ``register_chassis_voice_section`` (covered by
    ``tests/agents/test_chassis_voice_section.py``), NOT by being injected
    into the NPC roster zone. This test is the inverse-of-the-original
    guard: chassis names must NOT appear in ``snapshot.npc_pool``.
    """
    if not SPACE_OPERA.exists():
        pytest.skip("space_opera content pack not present")
    from sidequest.game.chassis import init_chassis_registry
    from sidequest.genre.loader import load_genre_pack
    from sidequest.magic.state import MagicState

    pack = load_genre_pack(SPACE_OPERA)
    snap = _make_snapshot("space_opera", "coyote_star")
    snap.magic_state = MagicState.from_config(_make_coyote_star_magic_config())
    init_chassis_registry(snap, pack)

    # Chassis is in chassis_registry…
    assert "Kestrel" in {c.name for c in snap.chassis_registry.values()}
    # …but NOT in the NPC pool. Voice section handles its narrator surfacing.
    pool_names = {member.name for member in snap.npc_pool}
    assert "Kestrel" not in pool_names


def test_init_chassis_registry_world_without_rigs_is_noop() -> None:
    """Worlds without rigs.yaml load without error; chassis_registry stays empty."""
    if not SPACE_OPERA.exists():
        pytest.skip("space_opera content pack not present")
    from sidequest.game.chassis import init_chassis_registry
    from sidequest.genre.loader import load_genre_pack

    pack = load_genre_pack(SPACE_OPERA)
    # aureate_span exists in space_opera but has no rigs.yaml authored.
    snap = _make_snapshot("space_opera", "aureate_span")
    init_chassis_registry(snap, pack)
    assert snap.chassis_registry == {}


def test_init_chassis_registry_genre_without_chassis_classes_is_noop() -> None:
    """Genres without chassis_classes.yaml — no-op even if a rigs.yaml existed.

    For the slice, the function gracefully no-ops when pack.chassis_classes is None,
    matching the slice spec's graceful-absence pattern.
    """
    from sidequest.game.chassis import init_chassis_registry
    from sidequest.game.session import GameSnapshot

    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="any_world",
        location="Unknown",
    )

    # Build a minimal mock that mirrors GenrePack.chassis_classes is None.
    class _FakePack:
        chassis_classes = None
        source_dir = None

    init_chassis_registry(snap, _FakePack())
    assert snap.chassis_registry == {}


def _make_coyote_star_magic_config():
    """Plan deviation 2026-05-04 (TEA): the plan snippet
    ``WorldMagicConfig(world_slug="coyote_star", ledger_bars=[])`` is
    missing required fields. Build a minimum-valid config to keep
    pydantic happy."""
    from sidequest.magic.models import WorldKnowledge, WorldMagicConfig

    return WorldMagicConfig(
        world_slug="coyote_star",
        genre_slug="space_opera",
        allowed_sources=[],
        active_plugins=[],
        intensity=0.0,
        world_knowledge=WorldKnowledge(primary="classified", local_register="folkloric"),
        visibility={"primary": "feared", "local_register": "dismissed"},
        hard_limits=[],
        cost_types=[],
        ledger_bars=[],
        narrator_register="test",
    )


def test_init_chassis_registry_appends_confrontations_to_magic_state() -> None:
    """S1 step 2 — confrontations land on magic_state, not world_confrontations."""
    if not SPACE_OPERA.exists():
        pytest.skip("space_opera content pack not present")
    from sidequest.game.chassis import init_chassis_registry
    from sidequest.genre.loader import load_genre_pack
    from sidequest.magic.state import MagicState

    pack = load_genre_pack(SPACE_OPERA)
    snap = _make_snapshot("space_opera", "coyote_star")
    # Initialize magic_state BEFORE chassis registry — the new invariant.
    snap.magic_state = MagicState.from_config(_make_coyote_star_magic_config())

    init_chassis_registry(snap, pack)

    conf_ids = {c.id for c in snap.magic_state.confrontations}
    assert "the_tea_brew" in conf_ids


def test_init_chassis_registry_raises_when_magic_state_absent() -> None:
    """S1 step 2 — calling init_chassis_registry without magic_state, when the
    world ships a confrontations.yaml, must fail loudly. The legacy 'silent
    stash on world_confrontations' path is gone (CLAUDE.md no silent fallback).
    """
    if not SPACE_OPERA.exists():
        pytest.skip("space_opera content pack not present")
    from sidequest.game.chassis import init_chassis_registry
    from sidequest.genre.loader import load_genre_pack

    pack = load_genre_pack(SPACE_OPERA)
    snap = _make_snapshot("space_opera", "coyote_star")
    # magic_state remains None — coyote_star has confrontations.yaml so this
    # is the failure mode, not the no-confrontations no-op branch.
    assert snap.magic_state is None

    with pytest.raises(RuntimeError, match="magic_state must be initialized"):
        init_chassis_registry(snap, pack)
