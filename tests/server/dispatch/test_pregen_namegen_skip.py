"""Playtest 2026-06-07 (blackthorn_moor) — roster-only namegen skip gate.

A world whose effective archetype pool contains only ``named_individual``
templates (a murder-mystery cast of specific people) has nothing namegen may
random-mint. ``seed_manual`` used to invoke namegen once per NPC slot anyway
— 9× contiguous ``pregen.namegen_failed (exit_code=1)`` WARN spam per session
start. The gate detects the empty mint pool ONCE, logs a single INFO skip
line, and never invokes the CLI (mirroring ``pregen.encounters_skipped``).

Lives in its own module (NOT ``test_pregen.py``, which is wholesale-skipped
pending the caverns_sunden→genre_workshopping world-binding migration) so the
gate is actually exercised — same placement rationale as
``test_pregen_combat_gate.py``.
"""

from __future__ import annotations

import json
import logging
import random
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

from sidequest.game.monster_manual import MonsterManual
from sidequest.server.dispatch import pregen
from sidequest.server.dispatch.pregen import seed_manual


def _stub_pack(archetypes: list[Any], cultures: list[str]) -> Any:
    culture_objs = [SimpleNamespace(name=name) for name in cultures]
    return SimpleNamespace(
        archetype_constraints=None,
        rules=SimpleNamespace(combat_encounters=False),
        effective_cultures=lambda _world: (culture_objs, "world"),
        effective_archetypes=lambda _world: (archetypes, "world"),
    )


def _run_seed(pack: Any, *, tmp_path: Path) -> tuple[MonsterManual, list[list[str]]]:
    namegen_argvs: list[list[str]] = []

    def fake_namegen(argv: list[str]) -> int:
        namegen_argvs.append(argv)
        print(json.dumps({"name": f"NPC-{len(namegen_argvs)}", "role": "guest"}))
        return 0

    manual = MonsterManual(genre="tea_and_murder", world="blackthorn_moor")
    with (
        mock.patch.object(Path, "home", return_value=tmp_path),
        mock.patch.object(pregen, "load_genre_pack", lambda _dir: pack),
        mock.patch.object(pregen, "namegen_main", fake_namegen),
    ):
        seed_manual(
            genre_packs_path=tmp_path / "packs",
            genre="tea_and_murder",
            world="blackthorn_moor",
            manual=manual,
            rng=random.Random(0),
        )
    return manual, namegen_argvs


def test_all_named_individual_pool_skips_namegen_entirely(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Roster-only world: zero namegen invocations, zero warnings, one INFO."""
    cast = [
        SimpleNamespace(name="Lady Blackthorn", named_individual=True),
        SimpleNamespace(name="The Vicar", named_individual=True),
    ]
    pack = _stub_pack(cast, cultures=["highland", "lowland", "estate"])
    with caplog.at_level(logging.INFO):
        manual, namegen_argvs = _run_seed(pack, tmp_path=tmp_path)

    assert namegen_argvs == [], "namegen must never be invoked for a roster-only world"
    assert manual.npcs == []
    skip_lines = [r for r in caplog.records if "pregen.namegen_skipped" in r.message]
    assert len(skip_lines) == 1, "exactly one INFO skip line, not per-slot spam"
    assert "no_spawnable_archetypes" in skip_lines[0].getMessage()
    assert not [
        r for r in caplog.records if r.levelno >= logging.WARNING and "namegen" in r.message
    ], "the old behavior WARN-spammed pregen.namegen_failed once per NPC slot"


def test_empty_archetype_pool_also_skips(tmp_path: Path) -> None:
    """An empty effective pool would make namegen exit 2 — same skip."""
    pack = _stub_pack([], cultures=["highland"])
    _manual, namegen_argvs = _run_seed(pack, tmp_path=tmp_path)
    assert namegen_argvs == []


def test_spawnable_pool_still_mints(tmp_path: Path) -> None:
    """A pool with one spawnable archetype keeps the mint loop engaged."""
    pool = [
        SimpleNamespace(name="Lady Blackthorn", named_individual=True),
        SimpleNamespace(name="Constable", named_individual=False),
    ]
    pack = _stub_pack(pool, cultures=["highland"])
    manual, namegen_argvs = _run_seed(pack, tmp_path=tmp_path)
    assert len(namegen_argvs) == pregen.NPCS_PER_CULTURE
    assert len(manual.npcs) == pregen.NPCS_PER_CULTURE
