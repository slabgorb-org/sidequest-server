"""Tests for ``sidequest.server.dispatch.pregen``.

Covers ``seed_manual``, the diverse pairing selector, the JSON-capturing
CLI runner, and the partial-failure fallbacks. Uses the real
``caverns_and_claudes`` genre pack so the integration covers actual
content shape (the post-fold ``regions.{region}.creatures`` schema flows
through encountergen into the Manual).
"""

from __future__ import annotations

import json
import logging
import random
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from sidequest.game.monster_manual import EntryState, MonsterManual
from sidequest.genre.models.archetype_constraints import (
    ArchetypeConstraints,
    GenreFlavor,
    ValidPairings,
)
from sidequest.server.dispatch import pregen
from sidequest.server.dispatch.pregen import (
    _run_cli_capturing_json,
    _select_diverse_pairings,
    seed_manual,
)

CONTENT_ROOT = Path(__file__).resolve().parents[3].parent / "sidequest-content" / "genre_packs"


def _real_content_available() -> bool:
    return (CONTENT_ROOT / "caverns_and_claudes" / "pack.yaml").exists()


# ---------------------------------------------------------------------------
# _select_diverse_pairings
# ---------------------------------------------------------------------------


def _constraints(
    *,
    common: list[list[str]] | None = None,
    uncommon: list[list[str]] | None = None,
    rare: list[list[str]] | None = None,
    npc_roles: list[str] | None = None,
) -> ArchetypeConstraints:
    return ArchetypeConstraints(
        genre_flavor=GenreFlavor(),
        valid_pairings=ValidPairings(
            common=common or [],
            uncommon=uncommon or [],
            rare=rare or [],
            forbidden=[],
        ),
        npc_roles_available=npc_roles or [],
    )


def test_select_diverse_pairings_distributes_60_30_10() -> None:
    cons = _constraints(
        common=[["sage", "healer"]],
        uncommon=[["outlaw", "stealth"]],
        rare=[["hero", "tank"]],
        npc_roles=["mentor", "mook"],
    )
    pairings = _select_diverse_pairings(cons, count=10, rng=random.Random(0))
    assert len(pairings) == 10
    jungians = [p[0] for p in pairings]
    # 60% common (6) + 30% uncommon (3) + 10% rare (1)
    assert jungians.count("sage") == 6
    assert jungians.count("outlaw") == 3
    assert jungians.count("hero") == 1


def test_select_diverse_pairings_cycles_npc_roles() -> None:
    cons = _constraints(
        common=[["sage", "healer"]],
        npc_roles=["mentor", "mook"],
    )
    # With only `common` populated and count=10, the function yields ceil(10*0.6)=6
    # entries — uncommon and rare buckets produce nothing because their pools are
    # empty. npc_role cycles round-robin over those 6.
    pairings = _select_diverse_pairings(cons, count=10, rng=random.Random(0))
    npc_roles = [p[2] for p in pairings]
    assert npc_roles == ["mentor", "mook", "mentor", "mook", "mentor", "mook"]


def test_select_diverse_pairings_empty_npc_roles_yields_blank_third() -> None:
    cons = _constraints(common=[["sage", "healer"]])
    pairings = _select_diverse_pairings(cons, count=10, rng=random.Random(0))
    assert pairings  # non-empty so we're actually checking something
    assert all(p[2] == "" for p in pairings)


# ---------------------------------------------------------------------------
# _run_cli_capturing_json
# ---------------------------------------------------------------------------


def test_run_cli_capturing_json_happy_path() -> None:
    def stub(argv: list[str]) -> int:
        print(json.dumps({"hello": "world"}))
        return 0

    out = _run_cli_capturing_json(stub, [], label="stub")
    assert out == {"hello": "world"}


def test_run_cli_capturing_json_nonzero_exit_returns_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def stub(argv: list[str]) -> int:
        print('{"data": "anything"}')
        return 1

    with caplog.at_level(logging.WARNING):
        assert _run_cli_capturing_json(stub, [], label="stub") is None
    assert any("stub_failed" in r.message for r in caplog.records)


def test_run_cli_capturing_json_handles_sys_exit() -> None:
    def stub(argv: list[str]) -> int:
        raise SystemExit(2)

    assert _run_cli_capturing_json(stub, [], label="stub") is None


def test_run_cli_capturing_json_invalid_json_returns_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def stub(argv: list[str]) -> int:
        print("not json")
        return 0

    with caplog.at_level(logging.WARNING):
        assert _run_cli_capturing_json(stub, [], label="stub") is None
    assert any("invalid_json" in r.message for r in caplog.records)


def test_run_cli_capturing_json_empty_stdout_returns_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def stub(argv: list[str]) -> int:
        return 0

    with caplog.at_level(logging.WARNING):
        assert _run_cli_capturing_json(stub, [], label="stub") is None
    assert any("empty_output" in r.message for r in caplog.records)


def test_run_cli_capturing_json_unexpected_exception_returns_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def stub(argv: list[str]) -> int:
        raise RuntimeError("boom")

    with caplog.at_level(logging.WARNING):
        assert _run_cli_capturing_json(stub, [], label="stub") is None
    assert any("stub_failed" in r.message for r in caplog.records)


def test_run_cli_capturing_json_rejects_non_object_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def stub(argv: list[str]) -> int:
        print('["array", "not", "object"]')
        return 0

    with caplog.at_level(logging.WARNING):
        assert _run_cli_capturing_json(stub, [], label="stub") is None
    assert any("invalid_shape" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# seed_manual — fully mocked subprocesses for unit-level coverage
# ---------------------------------------------------------------------------


def _stub_pack(cultures: list[str], *, constraints: ArchetypeConstraints | None = None) -> Any:
    """Build a minimal stand-in pack with the fields seed_manual reads."""
    from types import SimpleNamespace

    return SimpleNamespace(
        cultures=[SimpleNamespace(name=name) for name in cultures],
        archetype_constraints=constraints,
    )


def test_seed_manual_with_cultures_generates_3_per_culture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two cultures × 3 NPCs = 6 namegen invocations + 2 encounter tiers."""
    monkeypatch.setattr(
        pregen,
        "load_genre_pack",
        lambda _dir: _stub_pack(["Scrapborn", "Vaultborn"]),
    )

    npc_calls: list[dict[str, object]] = []
    encounter_calls: list[dict[str, object]] = []

    def fake_namegen(argv: list[str]) -> int:
        # Echo culture back into the payload so we can assert dedup-safe names
        culture = argv[argv.index("--culture") + 1] if "--culture" in argv else "?"
        name = f"NPC-{culture}-{len(npc_calls)}"
        payload = {"name": name, "role": "scout", "culture": culture}
        npc_calls.append(payload)
        print(json.dumps(payload))
        return 0

    def fake_encountergen(argv: list[str]) -> int:
        tier = int(argv[argv.index("--tier") + 1]) if "--tier" in argv else 1
        payload = {"enemies": [{"name": f"Foe-tier-{tier}", "hp": tier * 10}]}
        encounter_calls.append(payload)
        print(json.dumps(payload))
        return 0

    monkeypatch.setattr(pregen, "namegen_main", fake_namegen)
    monkeypatch.setattr(pregen, "encountergen_main", fake_encountergen)

    with mock.patch.object(Path, "home", return_value=tmp_path):
        manual = MonsterManual(genre="mutant_wasteland", world="flickering_reach")
        seed_manual(
            genre_packs_path=tmp_path / "packs",
            genre="mutant_wasteland",
            world="flickering_reach",
            manual=manual,
            rng=random.Random(0),
        )

    # 2 cultures × 3 NPCs = 6 namegen invocations
    assert len(npc_calls) == 6
    # 2 tiers × 1 call each
    assert len(encounter_calls) == 2

    assert len(manual.npcs) == 6
    assert {n.culture for n in manual.npcs} == {"Scrapborn", "Vaultborn"}
    assert all(n.state == EntryState.AVAILABLE for n in manual.npcs)
    assert len(manual.encounters) == 2
    assert {e.tier for e in manual.encounters} == {1, 2}


def test_seed_manual_no_cultures_falls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Empty culture list → ``DEFAULT_NPC_FALLBACK_COUNT`` namegen invocations."""
    monkeypatch.setattr(pregen, "load_genre_pack", lambda _dir: _stub_pack([]))

    npc_calls: list[list[str]] = []

    def fake_namegen(argv: list[str]) -> int:
        npc_calls.append(argv)
        payload = {"name": f"NoCulture-{len(npc_calls)}", "role": "drifter", "culture": ""}
        print(json.dumps(payload))
        return 0

    monkeypatch.setattr(pregen, "namegen_main", fake_namegen)
    monkeypatch.setattr(pregen, "encountergen_main", lambda _argv: print("{}") or 0)  # type: ignore[func-returns-value]

    with mock.patch.object(Path, "home", return_value=tmp_path):
        manual = MonsterManual(genre="g", world="w")
        seed_manual(
            genre_packs_path=tmp_path / "packs",
            genre="g",
            world="w",
            manual=manual,
            rng=random.Random(0),
        )

    assert len(npc_calls) == pregen.DEFAULT_NPC_FALLBACK_COUNT
    # No --culture flag in any of the invocations
    assert not any("--culture" in argv for argv in npc_calls)


def test_seed_manual_pack_load_failure_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If the pack fails to load, we still try to seed without cultures."""

    def boom(_dir: Path) -> Any:
        raise RuntimeError("pack load exploded")

    monkeypatch.setattr(pregen, "load_genre_pack", boom)

    def fake_namegen(argv: list[str]) -> int:
        payload = {"name": "X", "role": "r", "culture": "c"}
        print(json.dumps(payload))
        return 0

    monkeypatch.setattr(pregen, "namegen_main", fake_namegen)
    monkeypatch.setattr(pregen, "encountergen_main", lambda _argv: print("{}") or 0)  # type: ignore[func-returns-value]

    with mock.patch.object(Path, "home", return_value=tmp_path), caplog.at_level(logging.WARNING):
        manual = MonsterManual(genre="g", world="w")
        seed_manual(
            genre_packs_path=tmp_path / "packs",
            genre="g",
            world="w",
            manual=manual,
            rng=random.Random(0),
        )

    assert any("pack_load_failed" in r.message for r in caplog.records)
    # Fallback fired — at least one NPC came through
    assert len(manual.npcs) >= 1


def test_seed_manual_dedup_keeps_unique_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Duplicate namegen output collapses via ``MonsterManual.add_npc`` dedup."""
    monkeypatch.setattr(pregen, "load_genre_pack", lambda _dir: _stub_pack(["Solo"]))

    def fake_namegen(argv: list[str]) -> int:
        # Always returns the same name — the Manual must dedup
        payload = {"name": "Krag", "role": "mechanic", "culture": "Solo"}
        print(json.dumps(payload))
        return 0

    monkeypatch.setattr(pregen, "namegen_main", fake_namegen)
    monkeypatch.setattr(pregen, "encountergen_main", lambda _argv: print("{}") or 0)  # type: ignore[func-returns-value]

    with mock.patch.object(Path, "home", return_value=tmp_path):
        manual = MonsterManual(genre="g", world="w")
        seed_manual(
            genre_packs_path=tmp_path / "packs",
            genre="g",
            world="w",
            manual=manual,
            rng=random.Random(0),
        )

    assert len(manual.npcs) == 1


def test_seed_manual_partial_failure_skips_npc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A namegen that returns rc=1 produces no Manual entry — the others still land."""
    monkeypatch.setattr(pregen, "load_genre_pack", lambda _dir: _stub_pack(["A", "B"]))

    invocation = {"n": 0}

    def fake_namegen(argv: list[str]) -> int:
        invocation["n"] += 1
        if invocation["n"] == 2:
            return 1
        culture = argv[argv.index("--culture") + 1]
        payload = {"name": f"OK-{invocation['n']}", "role": "r", "culture": culture}
        print(json.dumps(payload))
        return 0

    monkeypatch.setattr(pregen, "namegen_main", fake_namegen)
    monkeypatch.setattr(pregen, "encountergen_main", lambda _argv: print("{}") or 0)  # type: ignore[func-returns-value]

    with mock.patch.object(Path, "home", return_value=tmp_path):
        manual = MonsterManual(genre="g", world="w")
        seed_manual(
            genre_packs_path=tmp_path / "packs",
            genre="g",
            world="w",
            manual=manual,
            rng=random.Random(0),
        )

    # 2 cultures × 3 NPCs = 6 attempts; 1 failed → 5 entries
    assert len(manual.npcs) == 5


def test_seed_manual_writes_save_to_disk(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``manual.save()`` is called at the end — file lands under tmp_path."""
    monkeypatch.setattr(pregen, "load_genre_pack", lambda _dir: _stub_pack([]))
    monkeypatch.setattr(
        pregen,
        "namegen_main",
        lambda _argv: print(json.dumps({"name": "X", "role": "r", "culture": "c"})) or 0,  # type: ignore[func-returns-value]
    )
    monkeypatch.setattr(pregen, "encountergen_main", lambda _argv: print("{}") or 0)  # type: ignore[func-returns-value]

    with mock.patch.object(Path, "home", return_value=tmp_path):
        manual = MonsterManual(genre="testgenre", world="testworld")
        seed_manual(
            genre_packs_path=tmp_path / "packs",
            genre="testgenre",
            world="testworld",
            manual=manual,
            rng=random.Random(0),
        )

        save_path = tmp_path / ".sidequest" / "manuals" / "testgenre_testworld.json"
        assert save_path.exists()


# ---------------------------------------------------------------------------
# End-to-end against real content — namegen + encountergen actually run
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _real_content_available(), reason="sidequest-content not checked out")
def test_e2e_seed_caverns_sunden_populates_manual(tmp_path: Path) -> None:
    """Real-pack integration: connect-time seeding fills the Manual end-to-end."""
    with mock.patch.object(Path, "home", return_value=tmp_path):
        manual = MonsterManual(genre="caverns_and_claudes", world="caverns_sunden")
        seed_manual(
            genre_packs_path=CONTENT_ROOT,
            genre="caverns_and_claudes",
            world="caverns_sunden",
            manual=manual,
            rng=random.Random(0),
        )

    # Cultures present in C&C should have produced at least one NPC
    assert len(manual.npcs) >= 1
    assert all(n.state == EntryState.AVAILABLE for n in manual.npcs)
    # Both tier-1 and tier-2 encounters seeded
    assert len(manual.encounters) == 2
    assert {e.tier for e in manual.encounters} == {1, 2}
    # The encounter data carries an "enemies" list (encountergen output shape)
    for enc in manual.encounters:
        assert "enemies" in enc.data
        assert isinstance(enc.data["enemies"], list)
