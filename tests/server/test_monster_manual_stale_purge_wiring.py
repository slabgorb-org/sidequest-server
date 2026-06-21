"""Wiring: ensure_loaded purges a stale native-era Monster Manual cache.

The model method ``MonsterManual.purge_ruleset_incoherent_encounters`` is unit-
tested in ``tests/game/test_monster_manual_stale_purge.py``. This proves it is
actually CALLED from the production load seam (``ensure_loaded``) for a
ruleset-module pack, that the purge mutates the loaded manual, and that the
``monster_manual.stale_encounter_purged`` OTEL span fires (GM-panel lie-detector
per the OTEL Observability Principle). See [BUG / CWN-OTHER-SEATING].
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sidequest.game.monster_manual import ManualEncounter, MonsterManual
from sidequest.server.dispatch.monster_manual_inject import ensure_loaded
from sidequest.telemetry.spans.monster_manual import SPAN_MONSTER_MANUAL_STALE_PURGED


def _stale_cwn_manual() -> MonsterManual:
    m = MonsterManual(genre="road_warrior", world="the_circuit")
    m.encounters = [
        ManualEncounter(
            data={"enemies": [{"name": "Shadow", "class": "Wheelman", "hp": 48}]},
            label="2x Wheelman (tier 6)",
            tier=6,
        )
    ]
    return m


def test_ensure_loaded_purges_stale_native_cache_and_emits_span(
    monkeypatch: pytest.MonkeyPatch, otel_capture, tmp_path
):
    stale = _stale_cwn_manual()

    # Redirect the manuals dir so save() never touches the real shared cache.
    monkeypatch.setattr(MonsterManual, "_manuals_dir", staticmethod(lambda: tmp_path))
    # Load returns our stale manual (a plain function replaces the classmethod;
    # ensure_loaded calls MonsterManual.load(genre, world)).
    monkeypatch.setattr(MonsterManual, "load", lambda genre, world: stale)

    # Stub the heavy late-imported pregen helpers so the post-purge re-seed and
    # the authored backfill are no-ops in this unit.
    import sidequest.server.dispatch.pregen as pregen

    monkeypatch.setattr(pregen, "seed_manual", lambda **kwargs: None)
    monkeypatch.setattr(pregen, "_seed_authored_npcs", lambda *a, **k: 0)

    pack = SimpleNamespace(
        rules=SimpleNamespace(ruleset="cwn"),
        source_dir=tmp_path / "genre_packs" / "road_warrior",
    )
    sd = SimpleNamespace(
        monster_manual=None,
        genre_slug="road_warrior",
        world_slug="the_circuit",
        genre_pack=pack,
    )

    result = ensure_loaded(sd)  # type: ignore[arg-type]

    assert result is stale
    # The stale native-typed encounter is gone.
    assert result.encounters == []
    # The span fired.
    spans = [
        s for s in otel_capture.get_finished_spans() if s.name == SPAN_MONSTER_MANUAL_STALE_PURGED
    ]
    assert len(spans) == 1
    assert spans[0].attributes["purged"] == 1
    assert spans[0].attributes["ruleset"] == "cwn"


def test_ensure_loaded_does_not_purge_coherent_cache(
    monkeypatch: pytest.MonkeyPatch, otel_capture, tmp_path
):
    coherent = MonsterManual(genre="road_warrior", world="the_circuit")
    enc = ManualEncounter(
        data={"enemies": [{"name": "Harbor Rat", "class": "creature", "hp": 4}]},
        label="Harbor Rat (tier 1)",
        tier=1,
    )
    coherent.encounters = [enc]
    for i in range(5):
        coherent.add_npc({"name": f"N{i}", "role": "r", "culture": "c"}, [])

    monkeypatch.setattr(MonsterManual, "_manuals_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(MonsterManual, "load", lambda genre, world: coherent)
    import sidequest.server.dispatch.pregen as pregen

    monkeypatch.setattr(pregen, "seed_manual", lambda **kwargs: None)
    monkeypatch.setattr(pregen, "_seed_authored_npcs", lambda *a, **k: 0)

    pack = SimpleNamespace(
        rules=SimpleNamespace(ruleset="cwn"),
        source_dir=tmp_path / "genre_packs" / "road_warrior",
    )
    sd = SimpleNamespace(
        monster_manual=None,
        genre_slug="road_warrior",
        world_slug="the_circuit",
        genre_pack=pack,
    )

    result = ensure_loaded(sd)  # type: ignore[arg-type]
    assert result.encounters == [enc]
    # No purge span when the cache is coherent.
    spans = [
        s for s in otel_capture.get_finished_spans() if s.name == SPAN_MONSTER_MANUAL_STALE_PURGED
    ]
    assert spans == []
