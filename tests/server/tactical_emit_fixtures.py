"""Shared fixtures for tactical-grid emit population tests.

Build a synthetic _SessionData + GameSnapshot whose dungeon_store returns a
mask dict with a ``tactical`` block, and whose snapshot places "Rux" in the
region. The two callers (test_tactical_grid_emit_population.py) differ only in
whether an opponent EncounterActor is seated.

Mirror the fixture pattern from tests/integration/test_tactical_grid_runtime_wiring.py.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[3]
CAVERNS_PACK = REPO_ROOT / "sidequest-content" / "genre_packs" / "caverns_and_claudes"
BENEATH_SUNDEN_WORLD = CAVERNS_PACK / "worlds" / "beneath_sunden"


def _packs_available() -> bool:
    return CAVERNS_PACK.exists() and BENEATH_SUNDEN_WORLD.exists()


def _runtime_mask_dict(rows: list[str], *, cell_width: int = 28) -> dict[str, Any]:
    """Build a persisted-mask dict matching RegionMask.to_dict() shape."""
    mask_bytes = ("\n".join(rows)).encode("ascii")
    return {
        "mask_bytes_b64": base64.b64encode(mask_bytes).decode("ascii"),
        "mask_sha": hashlib.sha256(mask_bytes).hexdigest(),
        "block": {
            "cell_width": cell_width,
            "grid_width": len(rows[0]) if rows else 0,
            "grid_height": len(rows),
            "origin_x": 0,
            "origin_y": 0,
        },
    }


class _FakeDungeonStore:
    """Minimal stand-in for DungeonStore: only load_masks() is called."""

    def __init__(self, masks: dict[str, dict[str, Any]]) -> None:
        self._masks = masks

    def load_masks(self) -> dict[str, dict[str, Any]]:
        return dict(self._masks)


def build_sd_with_tactical_region(
    creature_revealed: bool = True,
    creature_withdrawn: bool = False,
    tmp_path: Path | None = None,
) -> tuple[Any, Any, str]:
    """Build (_SessionData, GameSnapshot, room_id) with a persisted tactical block.

    The mask dict includes a ``tactical`` key so _maybe_build_runtime_cavern_payload
    can populate features/tokens/pois from it.

    ``creature_revealed=True``  → encounter has one opponent EncounterActor ("rope-spider")
    ``creature_revealed=False`` → encounter is None (pre-ambush; actor not yet seated)
    ``creature_withdrawn=True`` → only valid when creature_revealed=True; actor exists in
                                  the encounter but has withdrawn=True (mid-combat yield).
                                  The concealment gate must suppress it from the map.
    """
    from sidequest.agents.orchestrator import Orchestrator
    from sidequest.dungeon.tactical import RegionTactical, TacticalFeatureCell, TokenAnchor
    from sidequest.game.encounter import EncounterActor, StructuredEncounter
    from sidequest.game.repository import SaveRepository
    from sidequest.game.session import GameSnapshot
    from sidequest.genre.loader import load_genre_pack
    from sidequest.server.session_handler import _SessionData

    if not _packs_available():
        import pytest

        pytest.skip("caverns_and_claudes content pack not present")

    # The PNG sidecar is written to SIDEQUEST_OUTPUT_DIR at emit time.
    # We need the env var set; callers that use monkeypatch will set it;
    # for tests that call _maybe_build_runtime_cavern_payload directly without
    # the full emit pipeline, we can use tmp_path if provided.
    # (The tests using this fixture set SIDEQUEST_OUTPUT_DIR via monkeypatch
    # or via tmp_path parameter.)

    region_id = "region_test_alpha"

    # 5×5 grid: wall border, 3×3 floor interior.
    # Floor cells (1-based col, 0-based row in mask): (1,1),(2,1),(3,1),
    #   (1,2),(2,2),(3,2),(1,3),(2,3),(3,3) — 9 floor cells.
    rows = [
        "#####",
        "#...#",
        "#.#.#",
        "#...#",
        "#####",
    ]

    # Build a RegionTactical with known features/anchors/pois so the test
    # can assert non-empty results. Cells must be FLOOR cells in the mask above.
    tactical = RegionTactical(
        region_id=region_id,
        features=[TacticalFeatureCell("water", (1, 1), "black water")],
        anchors=[
            TokenAnchor((1, 1), "entrance"),
            TokenAnchor((3, 1), "creature"),
        ],
        pois=[(1, 1)],
        exit_thresholds={},
    )

    mask = _runtime_mask_dict(rows)
    mask["tactical"] = tactical.to_dict()

    pack = load_genre_pack(CAVERNS_PACK)
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
    )
    snap.character_locations["Rux"] = region_id
    snap.discovered_rooms = [region_id]

    # Real Character with a live HpPool and armor_class so find_creature_core resolves hp/ac.
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, HpPool

    rux_core = CreatureCore(
        name="Rux",
        description="A stalwart explorer.",
        personality="Curious and cautious.",
        hp=HpPool(current=18, max=22, base_max=22),
        armor_class=14,
    )
    rux_char = Character(
        core=rux_core,
        backstory="Descended from the rock-wardens of old Sünden.",
        char_class="Fighter",
        race="Human",
    )
    snap.characters.append(rux_char)

    if creature_revealed:
        # One opponent actor — concealment gate lets live actors through, suppresses withdrawn.
        from sidequest.game.encounter import EncounterMetric

        actor = EncounterActor(
            name="rope-spider",
            role="combatant",
            side="opponent",
            withdrawn=creature_withdrawn,
        )
        snap.encounter = StructuredEncounter(
            encounter_type="combat",
            player_metric=EncounterMetric(name="tension", threshold=10),
            opponent_metric=EncounterMetric(name="fear", threshold=10),
            actors=[actor],
        )

        # Matching Npc in snapshot.npcs so the creature token gets hp/ac.
        from sidequest.game.session import Npc

        spider_core = CreatureCore(
            name="rope-spider",
            description="A large spider that hunts with silk ropes.",
            personality="Predatory and patient.",
            hp=HpPool(current=8, max=12, base_max=12),
            armor_class=13,
        )
        snap.npcs.append(Npc(core=spider_core))
    else:
        # No encounter (pre-ambush) — concealment gate produces no creature tokens.
        snap.encounter = None

    orchestrator = Orchestrator.__new__(Orchestrator)

    sd = _SessionData(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        player_name="Rux",
        player_id="player-tactical-emit-test",
        snapshot=snap,
        repository=MagicMock(spec=SaveRepository),
        dungeon_repository=MagicMock(),
        telemetry_sink=MagicMock(),
        genre_pack=pack,
        orchestrator=orchestrator,
    )
    sd.dungeon_store = _FakeDungeonStore({region_id: mask})  # type: ignore[attr-defined]

    return sd, snap, region_id
