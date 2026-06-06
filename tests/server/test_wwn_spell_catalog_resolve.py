"""Tests for ``resolve_wwn_spell_catalog`` — world-first spell-catalog resolution.

Epic 94 (genre/world boundary correction, supersedes ADR-120
"mechanics-in-genre"): a world's WWN spell catalog is a world-tier CAST/CATALOG
surface. The cast pipeline (``narration_apply._resolve_wwn_cast_for_beat``) and
the long_rest reprepare tool must read the catalog world-first.

Covers:
  - resolver semantics (world catalog replaces genre catalog; fall-through to
    genre for world-empty / unknown / no-slug; None when neither tier ships one),
  - the world-tier OTEL ``state_transition`` resolve span fires with tier=world,
  - the genre-fallback span stamps tier=genre,
  - WIRING: the cast pipeline reaches the resolver — driving a real cast against
    a pack whose bound world ships its OWN catalog resolves a spell that exists
    ONLY in the world catalog (not the genre catalog), proving the cast read the
    world tier.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import pytest

from sidequest.genre.models.pack import GenrePack, World
from sidequest.genre.models.wwn_spell import WwnSpell, WwnSpellCatalog
from sidequest.server.dispatch.wwn_spell_catalog_resolve import resolve_wwn_spell_catalog


@pytest.fixture
def captured_watcher_events(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    captured: list[dict[str, Any]] = []

    def _capture(event_type, fields, *, component="sidequest-server", severity="info"):
        captured.append({"event_type": event_type, "fields": fields, "component": component})

    from sidequest.telemetry import watcher_hub

    monkeypatch.setattr(watcher_hub, "publish_event", _capture)
    yield captured


def _spell(spell_id: str) -> WwnSpell:
    return WwnSpell(
        id=spell_id,
        name=spell_id.replace("_", " ").title(),
        level=1,
        genre_description="x",
        mechanical_effect="x",
    )


def _catalog(*ids: str) -> WwnSpellCatalog:
    return WwnSpellCatalog(spells=[_spell(i) for i in ids])


def _make_pack(
    *,
    genre_catalog: WwnSpellCatalog | None,
    worlds: dict[str, WwnSpellCatalog | None],
) -> GenrePack:
    world_objs: dict[str, World] = {}
    for slug, catalog in worlds.items():
        world_objs[slug] = cast(World, World.model_construct(wwn_spell_catalog=catalog))
    return cast(
        GenrePack,
        GenrePack.model_construct(wwn_spell_catalog=genre_catalog, worlds=world_objs),
    )


class TestResolveWwnSpellCatalog:
    def test_world_catalog_replaces_genre_catalog(self) -> None:
        pack = _make_pack(
            genre_catalog=_catalog("g_spell"),
            worlds={"burning_peace": _catalog("cinder_lance", "river_step")},
        )
        result = resolve_wwn_spell_catalog(pack, "burning_peace")
        assert result is not None
        assert {s.id for s in result.spells} == {"cinder_lance", "river_step"}

    def test_world_without_catalog_falls_back_to_genre(self) -> None:
        pack = _make_pack(genre_catalog=_catalog("g_spell"), worlds={"w": None})
        result = resolve_wwn_spell_catalog(pack, "w")
        assert result is not None
        assert {s.id for s in result.spells} == {"g_spell"}

    def test_missing_slug_returns_genre_catalog(self) -> None:
        pack = _make_pack(genre_catalog=_catalog("g_spell"), worlds={"w": _catalog("w_spell")})
        assert resolve_wwn_spell_catalog(pack, None) is not None
        assert {s.id for s in resolve_wwn_spell_catalog(pack, None).spells} == {"g_spell"}
        assert {s.id for s in resolve_wwn_spell_catalog(pack, "").spells} == {"g_spell"}

    def test_unknown_world_returns_genre_catalog(self) -> None:
        pack = _make_pack(genre_catalog=_catalog("g_spell"), worlds={"w": _catalog("w_spell")})
        assert {s.id for s in resolve_wwn_spell_catalog(pack, "nowhere").spells} == {"g_spell"}

    def test_none_when_neither_tier_ships_catalog(self) -> None:
        pack = _make_pack(genre_catalog=None, worlds={"w": None})
        assert resolve_wwn_spell_catalog(pack, "w") is None


class TestResolveWwnSpellCatalogOtel:
    def test_world_resolution_emits_world_tier_span(
        self, captured_watcher_events: list[dict]
    ) -> None:
        pack = _make_pack(
            genre_catalog=_catalog("g_spell"),
            worlds={"burning_peace": _catalog("cinder_lance", "river_step")},
        )
        resolve_wwn_spell_catalog(pack, "burning_peace")

        spans = [
            e
            for e in captured_watcher_events
            if e["event_type"] == "state_transition"
            and e["fields"].get("field") == "wwn_spell_catalog"
            and e["fields"].get("op") == "resolved"
        ]
        assert spans, "no wwn_spell_catalog resolve span emitted"
        fields = spans[-1]["fields"]
        assert fields["tier"] == "world"
        assert fields["world_slug"] == "burning_peace"
        assert fields["spell_count"] == 2
        assert spans[-1]["component"] == "genre"

    def test_genre_fallback_stamps_genre_tier(self, captured_watcher_events: list[dict]) -> None:
        pack = _make_pack(genre_catalog=_catalog("g_spell"), worlds={"w": None})
        resolve_wwn_spell_catalog(pack, "w")

        spans = [
            e
            for e in captured_watcher_events
            if e["fields"].get("field") == "wwn_spell_catalog"
            and e["fields"].get("op") == "resolved"
        ]
        assert spans and spans[-1]["fields"]["tier"] == "genre"
        assert spans[-1]["fields"]["spell_count"] == 1
