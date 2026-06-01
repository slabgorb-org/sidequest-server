"""Playtest 2026-06-01 — seed-side gate for the combat_encounters capability.

``seed_manual`` pre-generates combat encounters (tier 1 + tier 2) via
encountergen and stores them on the Monster Manual. For a social,
Composure-only pack (``combat_encounters=False``) that generation must be
skipped entirely — no combat enemies should ever enter the Manual, so the
drawing-room mystery cannot inject hostile B/X combatants.

This lives in its own module (NOT ``test_pregen.py``, which is wholesale-skipped
pending the caverns_sunden→genre_workshopping world-binding migration) so the
gate is actually exercised.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from sidequest.game.monster_manual import MonsterManual
from sidequest.server.dispatch import pregen
from sidequest.server.dispatch.pregen import seed_manual


def _stub_pack(*, combat_encounters: bool) -> Any:
    """No-culture stub pack exposing only what seed_manual reads."""
    return SimpleNamespace(
        archetype_constraints=None,
        rules=SimpleNamespace(combat_encounters=combat_encounters),
        effective_cultures=lambda _world: ([], "genre"),
    )


def _run_seed(*, combat_encounters: bool, tmp_path: Path) -> tuple[MonsterManual, list[list[str]]]:
    encounter_argvs: list[list[str]] = []

    def fake_namegen(argv: list[str]) -> int:
        print(json.dumps({"name": f"NPC-{len(encounter_argvs)}", "role": "guest"}))
        return 0

    def fake_encountergen(argv: list[str]) -> int:
        encounter_argvs.append(argv)
        print(json.dumps({"enemies": [{"name": "Foe", "hp": 10}]}))
        return 0

    manual = MonsterManual(genre="tea_and_murder", world="blackthorn_moor")
    stub = _stub_pack(combat_encounters=combat_encounters)
    with (
        mock.patch.object(Path, "home", return_value=tmp_path),
        mock.patch.object(pregen, "load_genre_pack", lambda _dir: stub),
        mock.patch.object(pregen, "namegen_main", fake_namegen),
        mock.patch.object(pregen, "encountergen_main", fake_encountergen),
    ):
        seed_manual(
            genre_packs_path=tmp_path / "packs",
            genre="tea_and_murder",
            world="blackthorn_moor",
            manual=manual,
            rng=random.Random(0),
        )
    return manual, encounter_argvs


def test_seed_manual_skips_encounters_when_combat_disabled(tmp_path: Path) -> None:
    manual, encounter_argvs = _run_seed(combat_encounters=False, tmp_path=tmp_path)
    assert encounter_argvs == [], "encountergen must not run for a no-combat pack"
    assert manual.encounters == [], "no combat encounters may enter the Manual"
    # NPCs are unaffected — the social cast still seeds.
    assert len(manual.npcs) > 0


def test_seed_manual_generates_encounters_when_combat_enabled(tmp_path: Path) -> None:
    manual, encounter_argvs = _run_seed(combat_encounters=True, tmp_path=tmp_path)
    assert len(encounter_argvs) == len(pregen.ENCOUNTER_TIERS)
    assert len(manual.encounters) == len(pregen.ENCOUNTER_TIERS)
