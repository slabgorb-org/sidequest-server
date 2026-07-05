"""Wiring: ensure_loaded derives-don't-caches the Monster Manual (story 162-1).

The model contract (``reconcile_content``, the accumulation cap, fail-loud keys)
is unit-tested in ``tests/game/test_monster_manual_derive.py``. This suite proves
the production load seam (``monster_manual_inject.ensure_loaded``) actually:

1. **skips cleanly when the world is unresolved** — never keys the manual on an
   empty world slug (the ``caverns_and_claudes_.json`` bug), and writes no file;
2. **derives the content key for real and reconciles for real** — the REAL
   ``_content_sha_for`` over a realistic effective-bestiary and the REAL
   ``MonsterManual.reconcile_content``, through the production entry point,
   including the persisted discard cycle (rework finding: the original suite
   faked ``reconcile_content``, leaving the deriver executed by zero tests);
3. **emits the ``monster_manual.pool_discarded`` forensic span on a discard**
   (spec V1-V3) and the ``monster_manual.cap_enforced`` span when a legacy
   over-cap pool is trimmed on reconcile (spec D4);
4. **skips reconcile on no-evidence** — an unresolvable bestiary must never
   masquerade as a content change and nuke a stamped pool (87-4 conservatism);
5. **threads the SessionRoom slug as session_seed** — the real value, not just
   a string-typed anything.

Behavior/span assertions only — no source-text grep (CLAUDE.md "No Source-Text
Wiring Tests").
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sidequest.game.monster_manual import (
    MAX_MANUAL_NPCS,
    ManualEncounter,
    ManualNpc,
    MonsterManual,
)
from sidequest.server.dispatch.monster_manual_inject import (
    _content_sha_for,
    ensure_loaded,
)

SPAN_POOL_DISCARDED = "monster_manual.pool_discarded"
SPAN_CAP_ENFORCED = "monster_manual.cap_enforced"

WORLD = "beneath_sunden"
GENRE = "caverns_and_claudes"


def _stub_pregen(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-op the heavy late-imported seed + authored-backfill helpers."""
    import sidequest.server.dispatch.pregen as pregen

    monkeypatch.setattr(pregen, "seed_manual", lambda **kwargs: None, raising=False)
    monkeypatch.setattr(pregen, "_seed_authored_npcs", lambda *a, **k: 0, raising=False)


def _pack_with_roster(*creature_names: str) -> SimpleNamespace:
    """A pack stand-in whose ``effective_bestiary`` yields a realistic roster.

    Entries carry the identity fields the real deriver hashes
    (name/hp/level/armor_class) so ``_content_sha_for`` runs for real.
    ``source_dir=None`` keeps the seeding block out of the picture.
    """
    bestiary = SimpleNamespace(
        entries=[SimpleNamespace(name=n, hp=6, level=1, armor_class=10) for n in creature_names]
    )
    return SimpleNamespace(
        rules=SimpleNamespace(ruleset="wwn"),
        source_dir=None,
        effective_bestiary=lambda world: (bestiary, "world"),
    )


def _pack_without_bestiary() -> SimpleNamespace:
    """A pack whose bestiary is UNRESOLVABLE for the world (returns None)."""
    return SimpleNamespace(
        rules=SimpleNamespace(ruleset="wwn"),
        source_dir=None,
        effective_bestiary=lambda world: (None, ""),
    )


def _sd(pack: object, *, room_slug: str | None = None) -> SimpleNamespace:
    sd = SimpleNamespace(
        monster_manual=None,
        genre_slug=GENRE,
        world_slug=WORLD,
        genre_pack=pack,
    )
    if room_slug is not None:
        sd._room = SimpleNamespace(slug=room_slug)
    return sd


def _stale_manual_on_disk(*, content_sha: str) -> MonsterManual:
    """A populated, stamped manual persisted to the (isolated) manuals dir."""
    m = MonsterManual(genre=GENRE, world=WORLD, content_sha=content_sha)
    m.add_npc({"name": "Grib", "role": "goblin", "culture": "cave"}, [])
    m.encounters = [
        ManualEncounter(
            data={"enemies": [{"name": "Grib", "class": "creature", "hp": 6}]},
            label="Grib (tier 1)",
            tier=1,
        )
    ]
    m.save()
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
        genre_slug=GENRE,
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


# ── the real deriver, driven for real (rework finding #2) ──────────


def test_content_sha_is_stable_under_roster_reorder_and_changes_on_edit():
    sha_ab = _content_sha_for(_pack_with_roster("Ash Wight", "Bore Worm"), WORLD)
    sha_ba = _content_sha_for(_pack_with_roster("Bore Worm", "Ash Wight"), WORLD)
    sha_edit = _content_sha_for(_pack_with_roster("Ash Wight", "Bore Worm", "Cinder Hulk"), WORLD)

    # Real derivation: non-empty, fixed-width, order-insensitive, edit-sensitive.
    assert sha_ab is not None and len(sha_ab) == 16
    assert sha_ab == sha_ba  # entry order is not a content change
    assert sha_edit != sha_ab  # roster edit IS a content change

    # An empty-but-PRESENT roster is a real state with a stable digest...
    empty_pack = _pack_with_roster()
    sha_empty = _content_sha_for(empty_pack, WORLD)
    assert sha_empty is not None and len(sha_empty) == 16
    # ...while an UNRESOLVABLE bestiary is no-evidence: no digest at all
    # (rework finding: never conflate "can't read the roster" with "roster
    # is empty" — the former must not judge staleness).
    assert _content_sha_for(_pack_without_bestiary(), WORLD) is None
    assert _content_sha_for(None, WORLD) is None


def test_ensure_loaded_real_discard_cycle_persists_and_emits_span(
    monkeypatch, otel_capture, tmp_path
):
    """Roster changed → REAL reconcile discards, saves, spans; then idempotent."""
    monkeypatch.setattr(MonsterManual, "_manuals_dir", staticmethod(lambda: tmp_path))
    _stub_pregen(monkeypatch)

    pack_a = _pack_with_roster("Ash Wight", "Bore Worm")
    pack_b = _pack_with_roster("Ash Wight", "Gloom Adder")  # roster changed
    sha_a = _content_sha_for(pack_a, WORLD)
    assert sha_a is not None
    _stale_manual_on_disk(content_sha=sha_a)

    result = ensure_loaded(_sd(pack_b))  # type: ignore[arg-type]

    # The REAL reconcile discarded the stale pool and re-stamped to pack B's sha.
    assert result is not None
    assert result.npcs == []
    assert result.encounters == []
    sha_b = _content_sha_for(pack_b, WORLD)
    assert result.content_sha == sha_b

    # The discard was PERSISTED — a fresh load from disk sees the new stamp.
    reloaded = MonsterManual.load(GENRE, WORLD)
    assert reloaded.content_sha == sha_b
    assert reloaded.npcs == []

    # Forensic span carried the real counts (V1-V3).
    spans = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_POOL_DISCARDED]
    assert len(spans) == 1
    assert spans[0].attributes["npcs_discarded"] == 1
    assert spans[0].attributes["encounters_discarded"] == 1

    # Same content again → adopt/no-op, no second span (no livelock).
    result2 = ensure_loaded(_sd(pack_b))  # type: ignore[arg-type]
    assert result2 is not None
    spans2 = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_POOL_DISCARDED]
    assert len(spans2) == 1


def test_ensure_loaded_real_reconcile_preserves_pool_on_same_roster(
    monkeypatch, otel_capture, tmp_path
):
    monkeypatch.setattr(MonsterManual, "_manuals_dir", staticmethod(lambda: tmp_path))
    _stub_pregen(monkeypatch)

    pack = _pack_with_roster("Ash Wight", "Bore Worm")
    sha = _content_sha_for(pack, WORLD)
    assert sha is not None
    _stale_manual_on_disk(content_sha=sha)

    result = ensure_loaded(_sd(pack))  # type: ignore[arg-type]

    assert result is not None
    assert len(result.npcs) == 1  # pool preserved — same content reuses the pool
    assert len(result.encounters) == 1
    spans = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_POOL_DISCARDED]
    assert spans == []


def test_ensure_loaded_skips_reconcile_when_bestiary_unresolvable(
    monkeypatch, otel_capture, tmp_path
):
    """No evidence, no discard: an unresolvable bestiary must not nuke the pool.

    The removed foreign-purge refused to act on a None bestiary (87-4: never
    silently empty the pool). The wholesale-discard replacement must keep that
    conservatism: a stamped pool reconciled during a transiently-broken content
    state (bestiary unreadable) is left EXACTLY as loaded — stamp included.
    """
    monkeypatch.setattr(MonsterManual, "_manuals_dir", staticmethod(lambda: tmp_path))
    _stub_pregen(monkeypatch)

    _stale_manual_on_disk(content_sha="sha-from-when-content-was-readable")

    result = ensure_loaded(_sd(_pack_without_bestiary()))  # type: ignore[arg-type]

    assert result is not None
    assert len(result.npcs) == 1  # pool survives
    assert result.content_sha == "sha-from-when-content-was-readable"  # stamp untouched
    spans = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_POOL_DISCARDED]
    assert spans == []


# ── session_seed threads the ROOM slug, by value (rework finding #2) ─


def test_ensure_loaded_threads_room_slug_as_session_seed(monkeypatch, tmp_path):
    monkeypatch.setattr(MonsterManual, "_manuals_dir", staticmethod(lambda: tmp_path))
    _stub_pregen(monkeypatch)

    pack = _pack_with_roster("Ash Wight")
    result = ensure_loaded(_sd(pack, room_slug="sunday-table-42"))  # type: ignore[arg-type]

    # The REAL reconcile recorded the SessionRoom slug — the actual value, not
    # just any string (the original test asserted isinstance(str) only).
    assert result is not None
    assert result.session_seed == "sunday-table-42"


def test_ensure_loaded_session_seed_falls_back_to_world_slug(monkeypatch, tmp_path):
    monkeypatch.setattr(MonsterManual, "_manuals_dir", staticmethod(lambda: tmp_path))
    _stub_pregen(monkeypatch)

    result = ensure_loaded(_sd(_pack_with_roster("Ash Wight")))  # type: ignore[arg-type]

    assert result is not None
    assert result.session_seed == WORLD  # no room bound -> world-slug attribution


# ── legacy over-cap pools trim on reconcile (rework, spec D4) ──────


def test_ensure_loaded_trims_legacy_over_cap_pool_and_emits_cap_span(
    monkeypatch, otel_capture, tmp_path
):
    """A pre-162-1 runaway pool (spec's 310/1,153) is bounded on adoption."""
    monkeypatch.setattr(MonsterManual, "_manuals_dir", staticmethod(lambda: tmp_path))
    _stub_pregen(monkeypatch)

    legacy = MonsterManual(
        genre=GENRE,
        world=WORLD,
        npcs=[
            ManualNpc(
                data={"name": f"legacy-{i:04d}"}, name=f"legacy-{i:04d}", role="r", culture="c"
            )
            for i in range(MAX_MANUAL_NPCS + 20)
        ],
    )
    legacy.save()

    result = ensure_loaded(_sd(_pack_with_roster("Ash Wight")))  # type: ignore[arg-type]

    assert result is not None
    assert len(result.npcs) == MAX_MANUAL_NPCS  # bounded, not grandfathered

    # Trim persisted — a fresh load is bounded too.
    assert len(MonsterManual.load(GENRE, WORLD).npcs) == MAX_MANUAL_NPCS

    # The cap-enforcement decision is GM-panel visible (OTEL principle).
    spans = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_CAP_ENFORCED]
    assert len(spans) == 1
    assert spans[0].attributes["kind"] == "trim"
    assert spans[0].attributes["npcs_trimmed"] == 20
