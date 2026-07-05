"""Wiring: ensure_loaded derives-don't-caches the Monster Manual (story 162-1).

The model contract (``reconcile_content``, the accumulation cap, fail-loud keys)
is unit-tested in ``tests/game/test_monster_manual_derive.py``. This suite proves
the production load seam (``monster_manual_inject.ensure_loaded``) actually:

1. **skips cleanly when the world is unresolved** — never keys the manual on an
   empty world slug (the ``caverns_and_claudes_.json`` bug), and writes no file;
2. **reconciles content on load** — calls ``MonsterManual.reconcile_content`` with
   the session's content_sha + session_seed, REPLACING the two ``purge_*``
   tourniquets; and
3. **emits the ``monster_manual.pool_discarded`` forensic span on a discard** —
   the GM-panel lie-detector that a stale, multi-clone-written pool was dropped
   (spec V2), carrying the per-pool discard counts incl. authored deletions
   (spec V1/V3). Per the OTEL Observability Principle, a subsystem decision the
   GM panel can see.

RED until 162-1 lands. Behavior/span assertions only — no source-text grep
(CLAUDE.md "No Source-Text Wiring Tests").
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sidequest.game.monster_manual import ManualEncounter, MonsterManual
from sidequest.server.dispatch.monster_manual_inject import ensure_loaded

SPAN_POOL_DISCARDED = "monster_manual.pool_discarded"


def _stub_pregen(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-op the heavy late-imported seed + authored-backfill helpers."""
    import sidequest.server.dispatch.pregen as pregen

    monkeypatch.setattr(pregen, "seed_manual", lambda **kwargs: None, raising=False)
    monkeypatch.setattr(pregen, "_seed_authored_npcs", lambda *a, **k: 0, raising=False)


def _stale_manual() -> MonsterManual:
    m = MonsterManual(genre="caverns_and_claudes", world="beneath_sunden")
    m.add_npc({"name": "Grib", "role": "goblin", "culture": "cave"}, [])
    m.encounters = [
        ManualEncounter(
            data={"enemies": [{"name": "Grib", "class": "creature", "hp": 6}]},
            label="Grib (tier 1)",
            tier=1,
        )
    ]
    return m


def test_ensure_loaded_skips_when_world_unresolved(monkeypatch, tmp_path):
    monkeypatch.setattr(MonsterManual, "_manuals_dir", staticmethod(lambda: tmp_path))
    _stub_pregen(monkeypatch)

    load_calls: list[tuple[str, str]] = []
    orig_load = MonsterManual.load.__func__  # underlying function of the classmethod

    def spy_load(genre, world):
        load_calls.append((genre, world))
        return orig_load(MonsterManual, genre, world)

    monkeypatch.setattr(MonsterManual, "load", spy_load)

    pack = SimpleNamespace(rules=SimpleNamespace(ruleset="wwn"), source_dir=None)
    sd = SimpleNamespace(
        monster_manual=None,
        genre_slug="caverns_and_claudes",
        world_slug=None,  # bound pre-world-resolution
        genre_pack=pack,
    )

    result = ensure_loaded(sd)  # type: ignore[arg-type]

    # No manual without a world — skip cleanly, exactly like the no-genre case.
    assert result is None
    # Never attempted to key the manual on an empty world slug.
    assert load_calls == []
    # ...and nothing was written to the cache dir (no `caverns_and_claudes_.json`).
    assert list(tmp_path.glob("*.json")) == []


def test_ensure_loaded_reconciles_content_and_emits_discard_span(
    monkeypatch, otel_capture, tmp_path
):
    monkeypatch.setattr(MonsterManual, "_manuals_dir", staticmethod(lambda: tmp_path))
    _stub_pregen(monkeypatch)

    stale = _stale_manual()
    monkeypatch.setattr(MonsterManual, "load", lambda genre, world: stale)

    calls: dict[str, object] = {"count": 0}

    def spy_reconcile(self, *, content_sha, session_seed):
        calls["count"] = int(calls["count"]) + 1  # type: ignore[arg-type]
        calls["content_sha"] = content_sha
        calls["session_seed"] = session_seed
        # Simulate the discard: whole pool dropped.
        self.npcs = []
        self.encounters = []
        return SimpleNamespace(npcs_discarded=1, encounters_discarded=1, authored_discarded=0)

    monkeypatch.setattr(MonsterManual, "reconcile_content", spy_reconcile, raising=False)

    pack = SimpleNamespace(
        rules=SimpleNamespace(ruleset="wwn"),
        source_dir=tmp_path / "genre_packs" / "caverns_and_claudes",
    )
    sd = SimpleNamespace(
        monster_manual=None,
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        genre_pack=pack,
    )

    result = ensure_loaded(sd)  # type: ignore[arg-type]

    # ensure_loaded reconciled the loaded manual against current content.
    assert calls["count"] == 1
    # The stale pool was discarded wholesale (no targeted purge left it half-full).
    assert result is not None
    assert result.npcs == []
    assert result.encounters == []
    # The forensic span fired with per-pool discard counts (GM-panel lie-detector).
    spans = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_POOL_DISCARDED]
    assert len(spans) == 1
    assert spans[0].attributes["npcs_discarded"] == 1
    assert spans[0].attributes["encounters_discarded"] == 1


def test_ensure_loaded_no_discard_span_when_content_matches(monkeypatch, otel_capture, tmp_path):
    monkeypatch.setattr(MonsterManual, "_manuals_dir", staticmethod(lambda: tmp_path))
    _stub_pregen(monkeypatch)

    fresh = _stale_manual()  # content that MATCHES current -> reconcile is a no-op
    monkeypatch.setattr(MonsterManual, "load", lambda genre, world: fresh)

    def spy_reconcile(self, *, content_sha, session_seed):
        return None  # keys match -> no discard

    monkeypatch.setattr(MonsterManual, "reconcile_content", spy_reconcile, raising=False)

    pack = SimpleNamespace(
        rules=SimpleNamespace(ruleset="wwn"),
        source_dir=tmp_path / "genre_packs" / "caverns_and_claudes",
    )
    sd = SimpleNamespace(
        monster_manual=None,
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        genre_pack=pack,
    )

    ensure_loaded(sd)  # type: ignore[arg-type]

    spans = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_POOL_DISCARDED]
    assert spans == []


def test_ensure_loaded_threads_content_sha_and_session_seed(monkeypatch, tmp_path):
    monkeypatch.setattr(MonsterManual, "_manuals_dir", staticmethod(lambda: tmp_path))
    _stub_pregen(monkeypatch)

    monkeypatch.setattr(MonsterManual, "load", lambda genre, world: _stale_manual())

    captured: dict[str, object] = {}

    def spy_reconcile(self, *, content_sha, session_seed):
        captured["content_sha"] = content_sha
        captured["session_seed"] = session_seed
        return None

    monkeypatch.setattr(MonsterManual, "reconcile_content", spy_reconcile, raising=False)

    pack = SimpleNamespace(
        rules=SimpleNamespace(ruleset="wwn"),
        source_dir=tmp_path / "genre_packs" / "caverns_and_claudes",
    )
    sd = SimpleNamespace(
        monster_manual=None,
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        genre_pack=pack,
    )

    ensure_loaded(sd)  # type: ignore[arg-type]

    # ensure_loaded derives both keys from the session and threads them through.
    assert "content_sha" in captured
    assert "session_seed" in captured
    assert isinstance(captured["content_sha"], str)
    assert isinstance(captured["session_seed"], str)
