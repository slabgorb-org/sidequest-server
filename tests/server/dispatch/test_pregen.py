"""Tests for ``sidequest.server.dispatch.pregen``.

Covers ``seed_manual``, the diverse pairing selector, the JSON-capturing
CLI runner, and the partial-failure fallbacks. The end-to-end test runs the
real namegen + encountergen subprocesses against a **dedicated test fixture**
pack (``tests/fixtures/packs/test_genre`` / world ``flickering_reach``) so the
integration covers actual content shape (the ``worlds/{world}/creatures.yaml``
schema flows through encountergen into the Manual) without coupling to a live
genre-pack world (operator decision 2026-06-03, story 72-15 — live content is
mid-migration per 71-31; the fixture's world cultures use ``word_list`` given
names so namegen runs hermetically with no corpus files).
"""

from __future__ import annotations

import json
import logging
import random
from pathlib import Path
from typing import Any

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

# Dedicated test fixture pack — always present in-repo, so the e2e never skips.
# (Replaces the retired caverns_sunden live-world binding; story 72-15.)
FIXTURE_PACKS = Path(__file__).resolve().parents[2] / "fixtures" / "packs"


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

    culture_objs = [SimpleNamespace(name=name) for name in cultures]
    from types import SimpleNamespace as _SN

    pack = SimpleNamespace(
        cultures=culture_objs,
        archetype_constraints=constraints,
        rules=_SN(combat_encounters=True, ruleset="dial"),
    )
    # seed_manual resolves cultures via ``pack.effective_cultures(world)``
    # (world-over-genre replacement — ADR-121 / story 72-11), which returns a
    # 2-tuple ``(effective_list, source_tag)`` where each element exposes
    # ``.name``. The stub has no world layer, so it ignores ``world`` and
    # returns its own culture list tagged ``"stub"``.
    pack.effective_cultures = lambda _world: (culture_objs, "stub")
    # seed_manual gates namegen on ``spawnable_archetypes(pack.
    # effective_archetypes(world))`` (playtest 2026-06-07, blackthorn_moor):
    # an all-named_individual (or empty) pool skips minting entirely. Give
    # the stub one spawnable archetype so the mint loop stays exercised.
    spawnable = SimpleNamespace(name="Drifter", named_individual=False)
    pack.effective_archetypes = lambda _world: ([spawnable], "stub")
    # seed_manual resolves the world bestiary via ``pack.effective_bestiary(world)``
    # (epic-157) so each seeded encounter inherits its source creatures' faction
    # tags. The stub has no bestiary layer → (None, "stub") → encounters seed
    # untagged (eligible everywhere), the correct shape for a dial stub pack.
    pack.effective_bestiary = lambda _world: (None, "stub")
    return pack


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

    # (162-1 rework) inert Path.home mock removed — the autouse
    # _isolate_monster_manuals fixture redirects _manuals_dir directly.
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

    # (162-1 rework) inert Path.home mock removed — the autouse
    # _isolate_monster_manuals fixture redirects _manuals_dir directly.
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

    # (162-1 rework) inert Path.home mock removed — the autouse
    # _isolate_monster_manuals fixture redirects _manuals_dir directly.
    with caplog.at_level(logging.WARNING):
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

    # (162-1 rework) inert Path.home mock removed — the autouse
    # _isolate_monster_manuals fixture redirects _manuals_dir directly.
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

    # (162-1 rework) inert Path.home mock removed — the autouse
    # _isolate_monster_manuals fixture redirects _manuals_dir directly.
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

    # The autouse _isolate_monster_manuals fixture (tests/conftest.py, story
    # 162-1) points _manuals_dir at a private tmp dir — assert via _file_path
    # instead of a hand-built Path.home()-based path.
    manual = MonsterManual(genre="testgenre", world="testworld")
    seed_manual(
        genre_packs_path=tmp_path / "packs",
        genre="testgenre",
        world="testworld",
        manual=manual,
        rng=random.Random(0),
    )

    save_path = MonsterManual._file_path("testgenre", "testworld")
    assert save_path.exists()


# ---------------------------------------------------------------------------
# End-to-end against real content — namegen + encountergen actually run
# ---------------------------------------------------------------------------


def test_e2e_seed_fixture_world_populates_manual(tmp_path: Path) -> None:
    """Real-subprocess integration against the dedicated test fixture pack:
    connect-time seeding fills the Manual end-to-end (namegen + encountergen
    actually run). Bound to ``test_genre``/``flickering_reach`` — a fixture, not a
    live genre-pack world (story 72-15). The fixture world's cultures use
    ``word_list`` given names, so namegen needs no corpus files."""
    # (162-1 rework) inert Path.home mock removed — the autouse
    # _isolate_monster_manuals fixture redirects _manuals_dir directly.
    manual = MonsterManual(genre="test_genre", world="flickering_reach")
    seed_manual(
        genre_packs_path=FIXTURE_PACKS,
        genre="test_genre",
        world="flickering_reach",
        manual=manual,
        rng=random.Random(0),
    )

    # The fixture world's cultures should have produced at least one NPC
    assert len(manual.npcs) >= 1
    assert all(n.state == EntryState.AVAILABLE for n in manual.npcs)
    # Both tier-1 and tier-2 encounters seeded
    assert len(manual.encounters) == 2
    assert {e.tier for e in manual.encounters} == {1, 2}
    # The encounter data carries an "enemies" list (encountergen output shape)
    for enc in manual.encounters:
        assert "enemies" in enc.data
        assert isinstance(enc.data["enemies"], list)


# ---------------------------------------------------------------------------
# _encounter_factions — the faction/zone union-stamp (epic-157, ADR-059
# amendment). This is the seed-time mechanism that POPULATES
# ``ManualEncounter.factions`` (the data Seam 1 filters on). The join key is the
# enemy NAME (encountergen carries no creature_id), so a regression here silently
# untags content and re-opens the cross-zone bleed — pin it directly.
# ---------------------------------------------------------------------------


def _bestiary(*entries: tuple[str, list[str]]):  # type: ignore[no-untyped-def]
    """Build a Bestiary from (name, factions) pairs with minimal valid stat blocks."""
    from sidequest.genre.models.bestiary import Bestiary, BestiaryEntry

    return Bestiary(
        entries=[
            BestiaryEntry(
                id=name.lower().replace(" ", "_"),
                name=name,
                level=1,
                hp=4,
                armor_class=12,
                attack_bonus=1,
                factions=list(factions),
            )
            for name, factions in entries
        ]
    )


def _enc_data(*enemy_names: str) -> dict[str, Any]:
    return {"enemies": [{"name": n, "class": "creature", "hp": 4} for n in enemy_names]}


def test_encounter_factions_includes_matched_entry_factions() -> None:
    from sidequest.server.dispatch.pregen import _encounter_factions

    bestiary = _bestiary(("Yahoo Brute", ["the_houyhnhnm_assembly"]))
    assert _encounter_factions(_enc_data("Yahoo Brute"), bestiary) == ["the_houyhnhnm_assembly"]


def test_encounter_factions_match_is_case_insensitive() -> None:
    """encountergen copies entry.name verbatim, but the join lowercases defensively."""
    from sidequest.server.dispatch.pregen import _encounter_factions

    bestiary = _bestiary(("Yahoo Brute", ["the_houyhnhnm_assembly"]))
    assert _encounter_factions(_enc_data("yahoo brute"), bestiary) == ["the_houyhnhnm_assembly"]


def test_encounter_factions_unions_across_enemies_and_sorts() -> None:
    from sidequest.server.dispatch.pregen import _encounter_factions

    bestiary = _bestiary(
        ("Yahoo Brute", ["the_houyhnhnm_assembly", "no_one"]),
        ("Lilliput Guard", ["the_lilliput_court"]),
    )
    result = _encounter_factions(_enc_data("Yahoo Brute", "Lilliput Guard"), bestiary)
    # Union of both enemies' factions, sorted (stable on-disk Manual JSON).
    assert result == ["no_one", "the_houyhnhnm_assembly", "the_lilliput_court"]


def test_encounter_factions_empty_when_no_name_match() -> None:
    """An enemy whose name matches no bestiary entry contributes nothing — and
    must NOT silently inherit some other entry's factions."""
    from sidequest.server.dispatch.pregen import _encounter_factions

    bestiary = _bestiary(("Yahoo Brute", ["the_houyhnhnm_assembly"]))
    assert _encounter_factions(_enc_data("Unknown Beast"), bestiary) == []


def test_encounter_factions_empty_for_none_bestiary() -> None:
    """Native packs (no bestiary) → empty → eligible everywhere (permissive)."""
    from sidequest.server.dispatch.pregen import _encounter_factions

    assert _encounter_factions(_enc_data("Anything"), None) == []


def test_encounter_factions_tolerates_malformed_enemy_rows() -> None:
    from sidequest.server.dispatch.pregen import _encounter_factions

    bestiary = _bestiary(("Yahoo Brute", ["the_houyhnhnm_assembly"]))
    data = {"enemies": ["not-a-dict", {"no_name": True}, {"name": 123}, {"name": "Yahoo Brute"}]}
    # Only the well-formed matching row contributes; the junk rows are skipped.
    assert _encounter_factions(data, bestiary) == ["the_houyhnhnm_assembly"]


def test_encounter_factions_empty_when_entry_untagged() -> None:
    """A matched entry with no factions contributes nothing (not a crash)."""
    from sidequest.server.dispatch.pregen import _encounter_factions

    bestiary = _bestiary(("Yahoo Brute", []))
    assert _encounter_factions(_enc_data("Yahoo Brute"), bestiary) == []


def test_add_encounter_threads_factions_onto_manual_encounter() -> None:
    """The seed path passes the union through ``add_encounter(factions=...)`` →
    it must land on ``ManualEncounter.factions`` (the value Seam 1 reads)."""
    manual = MonsterManual(genre="wry_whimsy", world="gulliver")
    manual.add_encounter(
        _enc_data("Yahoo Brute"), tier=2, terrain_tags=[], factions=["the_houyhnhnm_assembly"]
    )
    assert manual.encounters[0].factions == ["the_houyhnhnm_assembly"]


def test_add_encounter_defaults_factions_empty_when_omitted() -> None:
    """Backward-compat: existing callers omit ``factions`` → empty (eligible everywhere)."""
    manual = MonsterManual(genre="wry_whimsy", world="gulliver")
    manual.add_encounter(_enc_data("Field Mouse"), tier=1, terrain_tags=[])
    assert manual.encounters[0].factions == []


def test_seed_manual_emits_cap_enforced_span_when_pool_is_full(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, otel_capture
) -> None:
    """Rework (162-1): a cap-dropped generated NPC is GM-panel visible.

    The cap refusal is a subsystem decision (OTEL Observability Principle —
    "inventory mutations ... with source"); the model returns the event, the
    pregen call site emits ``monster_manual.cap_enforced``.
    """
    from sidequest.game.monster_manual import MAX_MANUAL_NPCS

    monkeypatch.setattr(pregen, "load_genre_pack", lambda _dir: _stub_pack([]))
    monkeypatch.setattr(
        pregen,
        "namegen_main",
        lambda _argv: print(json.dumps({"name": "X", "role": "r", "culture": "c"})) or 0,  # type: ignore[func-returns-value]
    )
    monkeypatch.setattr(pregen, "encountergen_main", lambda _argv: print("{}") or 0)  # type: ignore[func-returns-value]

    manual = MonsterManual(genre="testgenre", world="testworld")
    for i in range(MAX_MANUAL_NPCS):
        manual.add_npc({"name": f"walkon-{i:04d}", "role": "r", "culture": "c"}, [])

    seed_manual(
        genre_packs_path=tmp_path / "packs",
        genre="testgenre",
        world="testworld",
        manual=manual,
        rng=random.Random(0),
    )

    assert len(manual.npcs) == MAX_MANUAL_NPCS  # cap held
    spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "monster_manual.cap_enforced"
    ]
    assert spans, "cap drop during seeding must emit monster_manual.cap_enforced"
    assert spans[0].attributes["kind"] == "npc_dropped"
    assert spans[0].attributes["incoming"] == "X"
    assert spans[0].attributes["genre"] == "testgenre"


def test_seed_manual_emits_cap_enforced_span_for_encounter_drop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, otel_capture
) -> None:
    """Coverage gap (162-9): the ENCOUNTER-side cap drop through ``seed_manual``
    was never driven — only the NPC-side (test above) and the model-level
    ``add_encounter`` drop were. Fill the encounter pool to the cap, then let
    ``seed_manual`` generate more, so ``add_encounter`` refuses with
    kind="encounter_dropped" and the pregen call site emits ``cap_enforced``.
    """
    from sidequest.game.monster_manual import MAX_MANUAL_ENCOUNTERS

    monkeypatch.setattr(pregen, "load_genre_pack", lambda _dir: _stub_pack([]))
    # A single unique NPC name dedups to one insert — the NPC side never caps, so
    # only encounter-side spans fire.
    monkeypatch.setattr(
        pregen,
        "namegen_main",
        lambda _argv: print(json.dumps({"name": "X", "role": "r", "culture": "c"})) or 0,  # type: ignore[func-returns-value]
    )
    # A non-empty encounter so ``_generate_encounter`` yields data and
    # ``add_encounter`` is actually attempted (then refused at the cap).
    monkeypatch.setattr(
        pregen,
        "encountergen_main",
        lambda _argv: print(json.dumps({"enemies": [{"name": "Overflow Beast"}]})) or 0,  # type: ignore[func-returns-value]
    )

    manual = MonsterManual(genre="testgenre", world="testworld")
    for i in range(MAX_MANUAL_ENCOUNTERS):
        assert manual.add_encounter(_enc_data(f"Beast-{i:04d}"), 1, []) is None

    seed_manual(
        genre_packs_path=tmp_path / "packs",
        genre="testgenre",
        world="testworld",
        manual=manual,
        rng=random.Random(0),
    )

    assert len(manual.encounters) == MAX_MANUAL_ENCOUNTERS  # cap held
    cap_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "monster_manual.cap_enforced"
    ]
    enc_spans = [s for s in cap_spans if s.attributes.get("kind") == "encounter_dropped"]
    assert enc_spans, (
        "encounter cap drop during seeding must emit cap_enforced (kind=encounter_dropped)"
    )
    assert enc_spans[0].attributes["genre"] == "testgenre"
    # The single deduped NPC name never caps, so NO npc-side cap span fires — makes
    # the "only encounter-side spans fire" claim above an actual assertion (162-9
    # review hardening).
    assert not [s for s in cap_spans if str(s.attributes.get("kind", "")).startswith("npc_")], (
        "NPC side must not cap in this scenario (single deduped name)"
    )
