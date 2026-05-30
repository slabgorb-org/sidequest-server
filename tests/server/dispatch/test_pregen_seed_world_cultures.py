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

    from sidequest.telemetry import spans as spans_module

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    local_tracer = provider.get_tracer("test")
    monkeypatch.setattr(spans_module, "tracer", lambda: local_tracer)

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
