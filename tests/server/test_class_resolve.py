"""Tests for ``resolve_classes`` and the connect-handler class wiring.

Epic 94 (genre/world boundary correction): a world's classes/callings are a
world-tier CAST surface. The chargen builder must read the roster world-first.
Mirrors ``tests/server/test_char_creation_resolve.py`` for char_creation.

Covers:
  - resolver semantics (world override replaces genre roster; fall-through to
    genre for world-empty / unknown / no-slug),
  - the world-tier OTEL ``state_transition`` span fires with ``tier=world``,
  - the connect handler reaches the resolver (the import/call-site wiring is
    asserted indirectly via the resolver being the sole class source — see the
    ``connect.py`` repoint).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import pytest

from sidequest.genre.models.character import ClassDef
from sidequest.genre.models.pack import GenrePack, World
from sidequest.server.dispatch.class_resolve import resolve_classes


@pytest.fixture
def captured_watcher_events(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    captured: list[dict[str, Any]] = []

    def _capture(event_type, fields, *, component="sidequest-server", severity="info"):
        captured.append(
            {
                "event_type": event_type,
                "fields": fields,
                "component": component,
            }
        )

    from sidequest.telemetry import watcher_hub

    monkeypatch.setattr(watcher_hub, "publish_event", _capture)
    yield captured


def _cls(class_id: str, display: str | None = None) -> ClassDef:
    # The resolver only reads ``.id`` / ``.classes`` membership; model_construct
    # skips validation of the unrelated required ClassDef fields.
    return cast(
        ClassDef,
        ClassDef.model_construct(id=class_id, display_name=display or class_id.title()),
    )


def _make_pack(
    *,
    genre_classes: list[ClassDef],
    worlds: dict[str, list[ClassDef]],
) -> GenrePack:
    world_objs: dict[str, World] = {}
    for slug, classes in worlds.items():
        world_objs[slug] = cast(World, World.model_construct(classes=classes))
    return cast(
        GenrePack,
        GenrePack.model_construct(classes=genre_classes, worlds=world_objs),
    )


class TestResolveClasses:
    def test_world_override_replaces_genre_roster(self) -> None:
        pack = _make_pack(
            genre_classes=[_cls("g_fighter")],
            worlds={"blackthorn_moor": [_cls("doctor"), _cls("clergyman")]},
        )

        result = resolve_classes(pack, "blackthorn_moor")

        assert [c.id for c in result] == ["doctor", "clergyman"], (
            "world roster must replace, not merge with, the genre roster"
        )

    def test_world_without_classes_falls_back_to_genre(self) -> None:
        pack = _make_pack(genre_classes=[_cls("g_fighter")], worlds={"w": []})
        assert [c.id for c in resolve_classes(pack, "w")] == ["g_fighter"]

    def test_missing_slug_returns_genre_roster(self) -> None:
        pack = _make_pack(genre_classes=[_cls("g_fighter")], worlds={"w": [_cls("doctor")]})
        assert [c.id for c in resolve_classes(pack, None)] == ["g_fighter"]
        assert [c.id for c in resolve_classes(pack, "")] == ["g_fighter"]

    def test_unknown_world_returns_genre_roster(self) -> None:
        pack = _make_pack(genre_classes=[_cls("g_fighter")], worlds={"w": [_cls("doctor")]})
        assert [c.id for c in resolve_classes(pack, "nowhere")] == ["g_fighter"]

    def test_returns_fresh_list(self) -> None:
        pack = _make_pack(genre_classes=[], worlds={"w": [_cls("doctor")]})
        result = resolve_classes(pack, "w")
        result.clear()
        assert [c.id for c in pack.worlds["w"].classes] == ["doctor"]


class TestResolveClassesOtel:
    def test_world_resolution_emits_world_tier_span(
        self, captured_watcher_events: list[dict]
    ) -> None:
        pack = _make_pack(
            genre_classes=[_cls("g_fighter")],
            worlds={"blackthorn_moor": [_cls("doctor"), _cls("clergyman")]},
        )

        resolve_classes(pack, "blackthorn_moor")

        spans = [
            e
            for e in captured_watcher_events
            if e["event_type"] == "state_transition"
            and e["fields"].get("field") == "chargen_classes"
        ]
        assert spans, "no chargen_classes resolution span emitted"
        fields = spans[-1]["fields"]
        assert fields["tier"] == "world", "world-tier resolution must stamp tier=world"
        assert fields["world_slug"] == "blackthorn_moor"
        assert fields["class_count"] == 2
        assert spans[-1]["component"] == "genre"

    def test_genre_fallback_stamps_genre_tier(self, captured_watcher_events: list[dict]) -> None:
        pack = _make_pack(genre_classes=[_cls("g_fighter")], worlds={"w": []})

        resolve_classes(pack, "w")

        spans = [
            e for e in captured_watcher_events if e["fields"].get("field") == "chargen_classes"
        ]
        assert spans and spans[-1]["fields"]["tier"] == "genre"
