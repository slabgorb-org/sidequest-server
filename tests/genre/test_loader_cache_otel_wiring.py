"""End-to-end OTEL wiring for genre-pack loading.

Sprint 3 cold-subsystem audit: ``genre/loader.py`` and ``genre/cache.py``
emitted zero watcher events. Failed/incomplete pack loads were invisible,
and cache hits looked indistinguishable from "the loader was never
called this session."

Tests pin three watcher signals:
- ``genre_pack:loaded`` from ``load_genre_pack`` on success
- ``genre_pack:cache_miss`` from ``GenreCache.get_or_load`` on first access
- ``genre_pack:cache_hit`` from ``GenreCache.get_or_load`` on repeat access

Monkeypatches ``watcher_hub.publish_event`` (the indirection seam used
by the genre layer's lazy import) the same way other unit-level wiring
tests do. The hub is process-global; tests must capture into a local
list rather than mutate the global subscriber set.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from sidequest.genre.cache import GenreCache


class _FakePack:
    def __init__(self, code: str) -> None:
        self.code = code


class _FakeLoader:
    def __init__(self) -> None:
        self.call_count = 0

    def load(self, code: str) -> _FakePack:
        self.call_count += 1
        return _FakePack(code)


@pytest.fixture
def captured_watcher_events(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    """Intercept watcher_hub.publish_event calls.

    Genre layer imports ``publish_event`` lazily inside the cache method
    via ``from sidequest.telemetry.watcher_hub import publish_event``.
    Monkeypatching the source attribute (rather than a re-exported alias)
    is the right seam — the lazy import resolves to it on every call.
    """
    captured: list[dict[str, Any]] = []

    def _capture(event_type, fields, *, component="sidequest-server", severity="info"):
        captured.append(
            {
                "event_type": event_type,
                "fields": fields,
                "component": component,
                "severity": severity,
            }
        )

    from sidequest.telemetry import watcher_hub as hub_mod

    monkeypatch.setattr(hub_mod, "publish_event", _capture)
    yield captured


def _genre_events(captured: list[dict], op: str) -> list[dict]:
    return [
        e
        for e in captured
        if e["component"] == "genre"
        and e["event_type"] == "state_transition"
        and e["fields"].get("op") == op
    ]


def test_cache_miss_publishes_state_transition(captured_watcher_events: list[dict]) -> None:
    """First access to a code: cache_miss event with cache_size=1
    (counted AFTER insertion)."""
    cache = GenreCache()
    cache.get_or_load("caverns_and_claudes", _FakeLoader())

    misses = _genre_events(captured_watcher_events, "cache_miss")
    assert len(misses) == 1
    fields = misses[0]["fields"]
    assert fields["genre_slug"] == "caverns_and_claudes"
    assert fields["cache_size"] == 1
    assert _genre_events(captured_watcher_events, "cache_hit") == []


def test_repeated_access_publishes_cache_hit_state_transition(
    captured_watcher_events: list[dict],
) -> None:
    """Second access to the same code emits cache_hit, not cache_miss."""
    cache = GenreCache()
    loader = _FakeLoader()
    cache.get_or_load("caverns_and_claudes", loader)
    cache.get_or_load("caverns_and_claudes", loader)

    assert loader.call_count == 1  # cache prevented second load
    assert len(_genre_events(captured_watcher_events, "cache_miss")) == 1
    hits = _genre_events(captured_watcher_events, "cache_hit")
    assert len(hits) == 1
    assert hits[0]["fields"]["genre_slug"] == "caverns_and_claudes"


def test_load_genre_pack_publishes_loaded_event_on_success(
    captured_watcher_events: list[dict],
) -> None:
    """A real ``load_genre_pack`` invocation against a shipped genre
    pack must emit a ``genre_pack:loaded`` event with world/scenario
    counts so the GM panel can prove the pack actually loaded vs.
    serving stale state."""
    from pathlib import Path

    from sidequest.genre.loader import load_genre_pack

    # Use the elemental_harmony pack — small and stable shape; the test
    # is interested in OTEL wiring, not pack contents.
    pack_dir = (
        Path(__file__).parent.parent.parent.parent
        / "sidequest-content"
        / "genre_packs"
        / "elemental_harmony"
    )
    if not pack_dir.is_dir():
        pytest.skip(f"genre pack not present at {pack_dir}")

    load_genre_pack(pack_dir)

    loaded = _genre_events(captured_watcher_events, "loaded")
    assert len(loaded) == 1, (
        f"expected exactly one genre_pack:loaded event, got {len(loaded)}: "
        f"{[e['fields'] for e in captured_watcher_events]}"
    )
    fields = loaded[0]["fields"]
    assert fields["genre_slug"] == "elemental_harmony"
    assert isinstance(fields["world_count"], int)
    assert isinstance(fields["scenario_count"], int)
    assert isinstance(fields["archetype_count"], int)
    assert isinstance(fields["trope_count"], int)
    assert fields["source_dir"] == str(pack_dir)
