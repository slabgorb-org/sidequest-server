"""Pack-load wiring + OTEL continuity for genre thresholds — Story 50-13 (RED).

CLAUDE.md: "Every Test Suite Needs a Wiring Test." The unit suite in
``test_disposition_genre_thresholds.py`` proves the model + module config
work in isolation. That is meaningless until ``load_genre_pack`` actually
applies a pack's declared thresholds to the live ``Disposition.attitude()``
path AND the SPAN_DISPOSITION_SHIFT OTEL span reflects the configured
bands with zero rework of the session.py callsite (the 50-12 claim:
"already wired Disposition.attitude() into the narrator roster so
corrected thresholds flow through with zero roster rework").

Three live paths are exercised here:

1. AC-2 — ``load_genre_pack`` on a pack declaring ``disposition_thresholds``
   reconfigures the process-level bands as a side effect of load.
2. AC-3 — a pack that does NOT declare the block loads with byte-identical
   ±10 behavior (existing packs unaffected).
3. AC-4 — a pack with inverted thresholds fails loudly at load
   (``GenreLoadError``), not a silent clamp to a working default.
4. AC-5 — after loading a ±5 pack, a real ``apply_world_patch`` emits
   SPAN_DISPOSITION_SHIFT with attitudes derived through the *configured*
   band, via the unchanged ``Disposition(before).attitude()`` callsite.

Synthetic fixture pack only (``minimal_pack_factory`` clones
``tests/fixtures/packs/test_genre``). No live genre slug is referenced —
tests must not point at real content.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from opentelemetry.sdk.trace import TracerProvider

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.disposition import (
    Attitude,
    Disposition,
    reset_attitude_thresholds,
)
from sidequest.game.session import GameSnapshot, Npc, WorldStatePatch
from sidequest.genre.error import GenreLoadError
from sidequest.genre.loader import load_genre_pack
from sidequest.server.watcher import WatcherSpanProcessor
from sidequest.telemetry import spans as spans_module
from sidequest.telemetry.watcher_hub import watcher_hub


@pytest.fixture(autouse=True)
def _isolate_threshold_state() -> Iterator[None]:
    """Pack loads mutate process-level threshold state. Reset around every
    test so a ±5 pack-load test cannot poison a later default-pack test
    (or the ±10-assuming enum-lock suite) in the same process."""
    reset_attitude_thresholds()
    yield
    reset_attitude_thresholds()


def _patch_rules_yaml(pack_path: Path, **overrides: Any) -> None:
    """Read the cloned pack's rules.yaml, merge top-level overrides, write
    it back. Used to inject (or malform) the ``disposition_thresholds``
    block without rewriting the whole file (which ``set_rules_yaml`` would
    do, dropping the fixture's confrontations)."""
    rules_path = pack_path / "rules.yaml"
    with rules_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    data.update(overrides)
    with rules_path.open("w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


# ---------------------------------------------------------------------------
# AC-2 — declared thresholds applied as a side effect of load
# ---------------------------------------------------------------------------


def test_loading_pack_with_thresholds_reconfigures_attitude(
    minimal_pack_factory: Any, tmp_path: Path
) -> None:
    """AC-2: a pack declaring ``disposition_thresholds: {friendly_at: 5,
    hostile_at: -5}`` must, purely by being loaded, make
    ``Disposition(6).attitude()`` read FRIENDLY (neutral under default
    ±10). This is the load-time wiring the story asks for."""
    pack = minimal_pack_factory(tmp_path)
    _patch_rules_yaml(pack.path, disposition_thresholds={"friendly_at": 5, "hostile_at": -5})

    load_genre_pack(pack.path)

    assert Disposition(6).attitude() == Attitude.FRIENDLY
    assert Disposition(5).attitude() == Attitude.NEUTRAL
    assert Disposition(-6).attitude() == Attitude.HOSTILE


def test_loaded_threshold_is_reachable_on_the_pack_model(
    minimal_pack_factory: Any, tmp_path: Path
) -> None:
    """AC-1/AC-2: the parsed pack also exposes the typed thresholds on
    ``pack.rules.disposition_thresholds`` so the loader has a typed value
    to hand to ``configure_attitude_thresholds`` (not a raw dict)."""
    pack = minimal_pack_factory(tmp_path)
    _patch_rules_yaml(pack.path, disposition_thresholds={"friendly_at": 7, "hostile_at": -3})

    loaded = load_genre_pack(pack.path)

    assert loaded.rules.disposition_thresholds is not None
    assert loaded.rules.disposition_thresholds.friendly_at == 7
    assert loaded.rules.disposition_thresholds.hostile_at == -3


# ---------------------------------------------------------------------------
# AC-3 — a pack without the block loads byte-identical to pre-50-13
# ---------------------------------------------------------------------------


def test_loading_pack_without_thresholds_keeps_default_bands(
    minimal_pack_factory: Any, tmp_path: Path
) -> None:
    """AC-3: the fixture pack ships no ``disposition_thresholds``. Loading
    it must leave the ±10 strict contract intact (11 friendly, 10 neutral,
    -11 hostile). Existing packs are byte-identical — the loader maps the
    absent block to DEFAULT, it does not leave stale state from a prior
    load."""
    pack = minimal_pack_factory(tmp_path)
    # No _patch_rules_yaml call — pack opts out entirely.

    loaded = load_genre_pack(pack.path)

    assert loaded.rules.disposition_thresholds is None
    assert Disposition(11).attitude() == Attitude.FRIENDLY
    assert Disposition(10).attitude() == Attitude.NEUTRAL
    assert Disposition(-10).attitude() == Attitude.NEUTRAL
    assert Disposition(-11).attitude() == Attitude.HOSTILE


def test_default_pack_load_clears_prior_pack_thresholds(
    minimal_pack_factory: Any, tmp_path: Path
) -> None:
    """AC-3 cross-pack no-leak (the multiplayer hazard): load a ±5 pack,
    then load a second pack with NO thresholds. The second load must
    restore ±10 — not inherit the first pack's ±5. Without this, two
    sessions on different packs in one server process would cross-
    contaminate NPC attitudes."""
    pack_a = minimal_pack_factory(tmp_path / "a")
    _patch_rules_yaml(pack_a.path, disposition_thresholds={"friendly_at": 5, "hostile_at": -5})
    load_genre_pack(pack_a.path)
    assert Disposition(6).attitude() == Attitude.FRIENDLY  # ±5 active

    pack_b = minimal_pack_factory(tmp_path / "b")  # no thresholds block
    load_genre_pack(pack_b.path)

    assert Disposition(6).attitude() == Attitude.NEUTRAL, (
        "second pack (no thresholds) inherited pack A's ±5 — load must "
        "reset to ±10 default, not accumulate module state"
    )
    assert Disposition(11).attitude() == Attitude.FRIENDLY


# ---------------------------------------------------------------------------
# AC-4 — malformed thresholds fail loudly at load (No Silent Fallbacks)
# ---------------------------------------------------------------------------


def test_inverted_thresholds_in_rules_yaml_fail_pack_load(
    minimal_pack_factory: Any, tmp_path: Path
) -> None:
    """AC-4: an inverted pair in rules.yaml must abort the load with
    ``GenreLoadError`` (the loader wraps RulesConfig validation errors).
    It must NOT silently swap/clamp to a working band and load anyway —
    that would mask a pack authoring bug for the whole session."""
    pack = minimal_pack_factory(tmp_path)
    _patch_rules_yaml(pack.path, disposition_thresholds={"friendly_at": -5, "hostile_at": 5})

    with pytest.raises(GenreLoadError):
        load_genre_pack(pack.path)


def test_failed_threshold_load_does_not_mutate_global_state(
    minimal_pack_factory: Any, tmp_path: Path
) -> None:
    """AC-4 + No-Silent-Fallbacks corollary: a load that raises on bad
    thresholds must not have already half-applied them. After the failed
    load the bands are still the ±10 default — config is applied only on
    the success path, never before validation completes."""
    pack = minimal_pack_factory(tmp_path)
    _patch_rules_yaml(pack.path, disposition_thresholds={"friendly_at": 0, "hostile_at": 0})

    with pytest.raises(GenreLoadError):
        load_genre_pack(pack.path)

    # Bands untouched — the bad pack did not leak a partial config.
    assert Disposition(11).attitude() == Attitude.FRIENDLY
    assert Disposition(10).attitude() == Attitude.NEUTRAL


# ---------------------------------------------------------------------------
# AC-5 — OTEL SPAN_DISPOSITION_SHIFT reflects configured bands, no rework
# ---------------------------------------------------------------------------


def _make_pc(name: str) -> Character:
    return Character(
        core=CreatureCore(
            name=name,
            description="x",
            personality="x",
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        char_class="Fighter",
        race="Human",
        backstory=f"{name} test",
    )


def _make_npc_with_disposition(name: str, value: int) -> Npc:
    return Npc(
        core=CreatureCore(
            name=name,
            description="x",
            personality="x",
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        disposition=Disposition(value),
    )


async def _setup_watcher(monkeypatch: pytest.MonkeyPatch, label: str) -> list[dict]:
    watcher_hub.bind_loop(asyncio.get_running_loop())
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001

    captured: list[dict] = []

    class _Sock:
        async def send_json(self, data: dict) -> None:
            captured.append(data)

    await watcher_hub.subscribe(_Sock())  # type: ignore[arg-type]

    provider = TracerProvider()
    provider.add_span_processor(WatcherSpanProcessor(watcher_hub))
    local_tracer = provider.get_tracer(label)
    monkeypatch.setattr(spans_module, "tracer", lambda: local_tracer)
    return captured


async def _wait_for_event(
    captured: list[dict], field_value: str, *, timeout_s: float = 1.0
) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        for evt in captured:
            if (
                evt.get("event_type") == "state_transition"
                and evt.get("fields", {}).get("field") == field_value
            ):
                return evt
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"Expected state_transition with field={field_value!r} within {timeout_s}s"
    )


@pytest.mark.asyncio
async def test_span_disposition_shift_uses_configured_band(
    minimal_pack_factory: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-5: after loading a ±5 pack, a +6 shift from 0 crosses the
    *configured* friendly boundary (it would NOT cross under default ±10,
    where 6 is still neutral). The SPAN_DISPOSITION_SHIFT span must carry
    ``after_attitude="friendly"`` and ``crossed=True`` — proving the
    genre-configured band reaches OTEL through the UNCHANGED
    ``Disposition(before).attitude()`` callsite in session.apply_world_patch
    (50-12 wired the callsite; 50-13 must not need to touch it)."""
    pack = minimal_pack_factory(tmp_path)
    _patch_rules_yaml(pack.path, disposition_thresholds={"friendly_at": 5, "hostile_at": -5})
    load_genre_pack(pack.path)

    captured = await _setup_watcher(monkeypatch, "test-50-13-configured-span")

    npc = _make_npc_with_disposition("Quartermaster", 0)  # neutral under any band
    snapshot = GameSnapshot(
        genre_slug="test_pack",
        world_slug="test_world",
        characters=[_make_pc("Hero")],
        npcs=[npc],
    )
    snapshot.apply_world_patch(WorldStatePatch(npc_attitudes={"Quartermaster": 6}))
    await asyncio.sleep(0)

    evt = await _wait_for_event(captured, "disposition.shift")
    fields = evt["fields"]

    assert fields["before_attitude"] == "neutral"
    assert fields["after_attitude"] == "friendly", (
        "shift to +6 under a ±5 pack must read friendly in the OTEL span; "
        "got neutral — configured threshold did not reach the span callsite"
    )
    assert fields["crossed"] is True


@pytest.mark.asyncio
async def test_span_no_crossing_when_configured_band_not_reached(
    minimal_pack_factory: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-5 negative: under a WIDE ±25 pack, a +6 shift from 0 stays
    neutral. ``crossed`` must be False and both attitudes ``neutral`` —
    the span must not report a band flip the configured thresholds did
    not actually produce (guards a regression that ignores config and
    flips at the hardcoded ±10)."""
    pack = minimal_pack_factory(tmp_path)
    _patch_rules_yaml(pack.path, disposition_thresholds={"friendly_at": 25, "hostile_at": -25})
    load_genre_pack(pack.path)

    captured = await _setup_watcher(monkeypatch, "test-50-13-wide-band-span")

    npc = _make_npc_with_disposition("Sentry", 0)
    snapshot = GameSnapshot(
        genre_slug="test_pack",
        world_slug="test_world",
        characters=[_make_pc("Hero")],
        npcs=[npc],
    )
    snapshot.apply_world_patch(WorldStatePatch(npc_attitudes={"Sentry": 6}))
    await asyncio.sleep(0)

    evt = await _wait_for_event(captured, "disposition.shift")
    fields = evt["fields"]

    assert fields["before_attitude"] == "neutral"
    assert fields["after_attitude"] == "neutral"
    assert fields["crossed"] is False
