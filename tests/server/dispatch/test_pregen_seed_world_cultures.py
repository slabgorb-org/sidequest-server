"""pregen.seed_manual must seed NPCs against the WORLD's cultures, not the
genre's (perseus_cloud Monster-Manual seeding bug, 2026-05-29, session 894).

Root cause: ``seed_manual`` read ``pack.cultures`` (genre: Hegemonic/Frontier/
Voidborn/Synthetic) and passed those names to namegen as ``--culture``, but
namegen resolves the culture name against the WORLD set (perseus_cloud:
spacer/thari/yulan). Every seed therefore failed with ``Culture 'Hegemonic'
not found`` → 0 MM NPCs seeded → every adversary became a narrator invention
(the downstream namegen-bypass / born-hostile / MM-provenance cluster).

These tests drive the REAL ``seed_manual`` (the production wiring) with
``_generate_npc`` spied so no corpus/subprocess is needed, and assert:
  - the cultures handed to the generator are the WORLD's, never the genre's
  - the ``pregen.seed_manual`` OTEL span fires with ``cultures_source=world``
    so the GM panel can confirm MM seeding consulted the world layer.
"""

from __future__ import annotations

import random

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.monster_manual import MonsterManual
from sidequest.genre.models.character import NpcArchetype
from sidequest.genre.models.culture import Culture
from sidequest.genre.models.pack import GenrePack, World
from sidequest.genre.models.rules import RulesConfig
from sidequest.server.dispatch import pregen


def _culture(name: str) -> Culture:
    return Culture.model_construct(name=name, summary="", description="")


def _perseus_like_pack() -> GenrePack:
    """Genre cultures REPLACED by the world's — the perseus_cloud shape."""
    return GenrePack.model_construct(
        cultures=[_culture("Hegemonic"), _culture("Frontier")],
        archetypes=[NpcArchetype.model_construct(name="Soldier")],
        worlds={"perseus": World.model_construct(cultures=[_culture("Spacer")], archetypes=[])},
        archetype_constraints=None,
        rules=RulesConfig(ruleset="dial"),
    )


def _install_spies(monkeypatch) -> list[str | None]:
    """Patch load_genre_pack + the two CLI shells; return the captured cultures."""
    captured: list[str | None] = []

    monkeypatch.setattr(pregen, "load_genre_pack", lambda _dir: _perseus_like_pack())

    def _spy_generate_npc(genre_packs_path, genre, *, culture, axes, world):  # noqa: ANN001, ARG001
        captured.append(culture)
        return None  # skip add_npc — we only care which culture was requested

    monkeypatch.setattr(pregen, "_generate_npc", _spy_generate_npc)
    monkeypatch.setattr(pregen, "_generate_encounter", lambda *a, **k: None)
    # Keep the test hermetic — don't write ~/.sidequest/manuals/*.json.
    monkeypatch.setattr(MonsterManual, "save", lambda self: None)
    return captured


def _manual() -> MonsterManual:
    return MonsterManual(genre="space_opera", world="perseus")


def _local_span_exporter(monkeypatch) -> InMemorySpanExporter:
    """Route ``Span.open`` through an in-memory exporter so a test can read the
    emitted ``pregen.seed_manual`` attributes."""
    from sidequest.telemetry import spans as spans_module

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(spans_module, "tracer", lambda: provider.get_tracer("test"))
    return exporter


def test_seed_manual_requests_world_cultures_not_genre(monkeypatch, tmp_path) -> None:
    captured = _install_spies(monkeypatch)

    seed_manual_world = "perseus"
    pregen.seed_manual(
        genre_packs_path=tmp_path,
        genre="space_opera",
        world=seed_manual_world,
        manual=_manual(),
        rng=random.Random(0),
    )

    assert captured, "seed_manual should have requested at least one NPC"
    assert set(captured) == {"Spacer"}, (
        "seed_manual must request the WORLD's cultures; got genre names instead: "
        f"{sorted(set(c or '<none>' for c in captured))}"
    )
    assert "Hegemonic" not in captured and "Frontier" not in captured


def test_seed_manual_emits_otel_with_world_culture_source(monkeypatch, tmp_path) -> None:
    _install_spies(monkeypatch)

    exporter = _local_span_exporter(monkeypatch)

    pregen.seed_manual(
        genre_packs_path=tmp_path,
        genre="space_opera",
        world="perseus",
        manual=_manual(),
        rng=random.Random(0),
    )

    spans = [s for s in exporter.get_finished_spans() if s.name == "pregen.seed_manual"]
    assert len(spans) == 1, (
        f"expected one pregen.seed_manual span; got {[s.name for s in exporter.get_finished_spans()]}"
    )
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("cultures_source") == "world"
    assert attrs.get("world") == "perseus"
    assert attrs.get("culture_count") == 1
    # 72-11: the new pre-seed truth attribute is present and correct in the
    # under-old-cap (1-culture) case too, not only the >=5-culture test.
    assert attrs.get("effective_culture_count") == 1


# ===========================================================================
# Story 72-11: MAX_CULTURES=4 cap silently drops world cultures.
#
# pregen.seed_manual sliced the resolved culture list `effective[:MAX_CULTURES]`
# (MAX_CULTURES=4, a Rust-port artifact), so any world declaring >4 cultures had
# its tail silently dropped from the seeded roster. coyote_star's 5th culture
# (voidborn) never seeded an NPC. The fix: seed ALL effective cultures, delete the
# constant, and make the pregen.seed_manual span report the world's true culture
# count so this silent drop is never invisible again.
#
# These tests use a coyote_star-SHAPED stub (5 declared world cultures, voidborn
# last) rather than the real coyote_star pack: per Story 71-31, coyote_star's live
# culture resolution is mid-migration (world cultures are currently visual-only and
# fall back to the genre set), so coupling here would be fragile. The bug is in
# pregen.py's cap, not in content — the stub pins the mechanism deterministically.
# The real coyote_star culture inventory is governed by tests/genre/
# test_71_31_space_opera_culture_doctrine.py.
# ===========================================================================

# coyote_star's five world cultures, voidborn declared LAST so the [:4] cap drops it.
_COYOTE_CULTURES = ["broken_drift", "free_miners", "hegemonic", "tsveri", "voidborn"]


def _world_pack(culture_names: list[str], *, world: str) -> GenrePack:
    """A pack whose named world declares ``culture_names`` (world-over-genre)."""
    return GenrePack.model_construct(
        cultures=[_culture("GenreOnly")],
        archetypes=[NpcArchetype.model_construct(name="Soldier")],
        worlds={
            world: World.model_construct(
                cultures=[_culture(n) for n in culture_names], archetypes=[]
            )
        },
        archetype_constraints=None,
        rules=RulesConfig(ruleset="dial"),
    )


def _install_capture(monkeypatch, pack: GenrePack) -> list[str | None]:
    """Spy _generate_npc so we capture the culture requested for each NPC seed."""
    captured: list[str | None] = []
    monkeypatch.setattr(pregen, "load_genre_pack", lambda _dir: pack)

    def _spy_generate_npc(genre_packs_path, genre, *, culture, axes, world):  # noqa: ANN001, ARG001
        captured.append(culture)
        return None

    monkeypatch.setattr(pregen, "_generate_npc", _spy_generate_npc)
    monkeypatch.setattr(pregen, "_generate_encounter", lambda *a, **k: None)
    monkeypatch.setattr(MonsterManual, "save", lambda self: None)
    return captured


def test_seed_manual_seeds_all_five_world_cultures_uncapped(monkeypatch, tmp_path) -> None:
    """AC1/AC4: a 5-culture world seeds ALL five (5 × NPCS_PER_CULTURE), voidborn
    included. RED today: the [:MAX_CULTURES] cap keeps the first 4 and drops the
    5th, so voidborn never seeds and only 12 NPCs are requested."""
    pack = _world_pack(_COYOTE_CULTURES, world="coyote_star")
    captured = _install_capture(monkeypatch, pack)

    pregen.seed_manual(
        genre_packs_path=tmp_path,
        genre="space_opera",
        world="coyote_star",
        manual=MonsterManual(genre="space_opera", world="coyote_star"),
        rng=random.Random(0),
    )

    # 5 cultures × 3 NPCs each = 15 seed requests, none dropped.
    assert len(captured) == 5 * pregen.NPCS_PER_CULTURE
    assert set(captured) == set(_COYOTE_CULTURES)
    # The load-bearing assertion: the culture the cap silently dropped IS seeded.
    assert "voidborn" in captured
    assert captured.count("voidborn") == pregen.NPCS_PER_CULTURE


def test_seed_manual_does_not_cap_at_five_either(monkeypatch, tmp_path) -> None:
    """AC2 (anti-cheat): the fix must seed the WORLD's full culture count, not a
    new hardcoded ceiling. A 6-culture world seeds all six. RED today: capped to 4.
    This kills the 'just bump MAX_CULTURES to 5' non-fix."""
    six = [*_COYOTE_CULTURES, "synthetics"]
    pack = _world_pack(six, world="coyote_star")
    captured = _install_capture(monkeypatch, pack)

    pregen.seed_manual(
        genre_packs_path=tmp_path,
        genre="space_opera",
        world="coyote_star",
        manual=MonsterManual(genre="space_opera", world="coyote_star"),
        rng=random.Random(0),
    )

    assert len(captured) == 6 * pregen.NPCS_PER_CULTURE
    assert set(captured) == set(six)


def test_seed_manual_two_cultures_under_cap_unaffected(monkeypatch, tmp_path) -> None:
    """AC5 (no regression): a world under the old cap still seeds exactly its
    cultures — 2 × NPCS_PER_CULTURE. Green now and after the fix; guards against a
    fix that over-expands or reorders the common under-threshold case."""
    pack = _world_pack(["tsveri", "free_miners"], world="coyote_star")
    captured = _install_capture(monkeypatch, pack)

    pregen.seed_manual(
        genre_packs_path=tmp_path,
        genre="space_opera",
        world="coyote_star",
        manual=MonsterManual(genre="space_opera", world="coyote_star"),
        rng=random.Random(0),
    )

    assert len(captured) == 2 * pregen.NPCS_PER_CULTURE
    assert set(captured) == {"tsveri", "free_miners"}


def test_seed_manual_span_reports_effective_and_seeded_culture_counts(
    monkeypatch, tmp_path
) -> None:
    """AC3 (OTEL lie-detector, load-bearing): the pregen.seed_manual span exposes
    the world's TRUE culture count (effective_culture_count) alongside the seeded
    count, so a silent truncation is observable on the GM panel. For a 5-culture
    world both read 5. RED today: effective_culture_count is absent and the seeded
    count reports the post-cap 4."""
    pack = _world_pack(_COYOTE_CULTURES, world="coyote_star")
    captured = _install_capture(monkeypatch, pack)

    exporter = _local_span_exporter(monkeypatch)

    pregen.seed_manual(
        genre_packs_path=tmp_path,
        genre="space_opera",
        world="coyote_star",
        manual=MonsterManual(genre="space_opera", world="coyote_star"),
        rng=random.Random(0),
    )

    spans = [s for s in exporter.get_finished_spans() if s.name == "pregen.seed_manual"]
    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    # The world declares 5 cultures — the span must say so (pre-seed truth).
    assert attrs.get("effective_culture_count") == 5
    # And all 5 were seeded — the post-cap 4 is the bug.
    assert attrs.get("culture_count") == 5
    assert attrs.get("cultures_source") == "world"
    # Cross-check the span's seeded count against the real seed attempts: 5
    # cultures × 3 NPCs = 15. Guards the "counted but never seeded" path — the
    # span could read culture_count=5 while the loop produced no NPCs.
    assert len(captured) == 5 * pregen.NPCS_PER_CULTURE


def test_max_cultures_constant_is_removed() -> None:
    """AC2 (dead-code removal): the MAX_CULTURES cap constant must be DELETED, not
    bumped — its existence is the bug. Reflection tripwire (runtime attribute, not a
    source-text grep, per CLAUDE.md 'No Source-Text Wiring Tests'). RED today: the
    module still defines MAX_CULTURES = 4."""
    assert not hasattr(pregen, "MAX_CULTURES"), (
        "pregen.MAX_CULTURES must be removed, not raised — the cap itself is the defect"
    )
