"""Wiring tests — production code uses ``Disposition.attitude()`` — Story 50-10.

Unit tests prove ``Disposition.attitude()`` works in isolation. That is
meaningless until the dispatch surface and the state mutator actually
call ``.attitude()`` along the live path. CLAUDE.md ("Every Test Suite
Needs a Wiring Test") is satisfied by the behavioral test at the bottom
of this file; the source-level checks above it are negative-existence
guards intentionally fragile-by-design — they catch a regression that
restores the legacy ``disposition_attitude`` import without exercising
runtime behavior.

Two enforcement paths:

1. Source-level guards — assert that ``opening.py`` and ``session.py``
   no longer import ``disposition_attitude``. These tests fail on the
   pure presence of a legacy import even if behavior is correct; that
   is deliberate. They are NOT a substitute for runtime coverage.

2. Behavioral wiring — apply a real ``WorldStatePatch`` with
   ``npc_attitudes`` and assert the SPAN_DISPOSITION_SHIFT span carries
   string attitudes derived through ``Disposition.attitude()``; assert
   that ``npc.disposition`` remains a ``Disposition`` instance after the
   mutator runs. This is the CLAUDE.md wiring test for the session.py
   call path. ``opening.py``'s runtime path is covered by the existing
   ``tests/server/test_opening_render_*.py`` suites which exercise the
   same rendering code that builds attitude strings.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from opentelemetry.sdk.trace import TracerProvider

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.disposition import Attitude, Disposition
from sidequest.game.session import GameSnapshot, Npc, WorldStatePatch
from sidequest.server.watcher import WatcherSpanProcessor
from sidequest.telemetry import spans as spans_module
from sidequest.telemetry.watcher_hub import watcher_hub

# ---------------------------------------------------------------------------
# Source-level wiring — no production calls to the legacy helper
# ---------------------------------------------------------------------------


_SERVER_PKG = Path(__file__).resolve().parents[2] / "sidequest"
_HELPER_CALL = re.compile(r"\bdisposition_attitude\s*\(")


def _production_py_files() -> list[Path]:
    """All .py files under sidequest/ excluding the disposition module
    itself (defining a regression-guard symbol there would be self-
    referential) and excluding test directories. The legacy
    ``disposition_attitude`` helper was removed in 50-10; this scan is
    retained as a regression guard against a future re-introduction."""
    files: list[Path] = []
    for path in _SERVER_PKG.rglob("*.py"):
        # The helper's defining module is allowed to mention itself.
        if path.name == "disposition.py" and path.parent.name == "game":
            continue
        # Skip vendored / generated / test code.
        parts = set(path.parts)
        if "tests" in parts or "__pycache__" in parts:
            continue
        files.append(path)
    return files


def test_no_production_module_calls_disposition_attitude_helper() -> None:
    """The helper is being replaced by ``Disposition.attitude()``. After
    50-10, no production module may call it. (Tests are allowed; the
    helper's own module is exempt because it defines the symbol.)"""
    offenders: list[str] = []
    for path in _production_py_files():
        source = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(source.splitlines(), 1):
            if _HELPER_CALL.search(line):
                offenders.append(f"{path.relative_to(_SERVER_PKG.parent)}:{lineno}: {line.strip()}")

    assert not offenders, (
        "production code still calls disposition_attitude(); migrate to "
        "Disposition.attitude() instead:\n  " + "\n  ".join(offenders)
    )


def test_opening_dispatch_imports_disposition_class_not_helper() -> None:
    """``opening.py`` rendered the NPC roster using the helper. After
    50-10 it must use ``Disposition.attitude()``. The simplest signal:
    it should no longer import ``disposition_attitude`` from
    ``sidequest.game.disposition``."""
    opening = _SERVER_PKG / "server" / "dispatch" / "opening.py"
    source = opening.read_text(encoding="utf-8")
    assert "from sidequest.game.disposition import disposition_attitude" not in source, (
        "opening.py still imports the legacy disposition_attitude helper — "
        "remove the import and call npc.initial_disposition's Disposition.attitude() instead"
    )


def test_session_apply_patch_does_not_import_disposition_attitude_helper() -> None:
    """``session.apply_world_patch`` previously imported the helper to
    label SPAN_DISPOSITION_SHIFT attributes. After 50-10 it must use the
    Disposition object's ``.attitude()`` method directly."""
    session = _SERVER_PKG / "game" / "session.py"
    source = session.read_text(encoding="utf-8")
    assert "from sidequest.game.disposition import disposition_attitude" not in source, (
        "session.py still imports the legacy disposition_attitude helper — "
        "use npc.disposition.attitude() to derive before_attitude / after_attitude"
    )


# ---------------------------------------------------------------------------
# Behavioral wiring — SPAN_DISPOSITION_SHIFT still emits string attitudes
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


async def _setup(monkeypatch: pytest.MonkeyPatch, label: str) -> list[dict]:
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
async def test_apply_patch_emits_attitude_strings_from_disposition_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SPAN_DISPOSITION_SHIFT span must carry ``before_attitude``
    and ``after_attitude`` as the literal strings ``"neutral"`` and
    ``"friendly"`` — derived through ``Disposition.attitude()``, not the
    legacy helper. The wire contract (50-11) survives the refactor."""
    captured = await _setup(monkeypatch, "test-attitude-strings-via-method")

    npc = _make_npc_with_disposition("Bartender", 10)  # neutral
    snapshot = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="caverns_sunden",
        characters=[_make_pc("Hero")],
        npcs=[npc],
    )
    snapshot.apply_world_patch(WorldStatePatch(npc_attitudes={"Bartender": 5}))
    await asyncio.sleep(0)

    evt = await _wait_for_event(captured, "disposition.shift")
    fields = evt["fields"]

    # String values must match the Attitude enum's literal values.
    assert fields["before_attitude"] == Attitude.NEUTRAL.value == "neutral"
    assert fields["after_attitude"] == Attitude.FRIENDLY.value == "friendly"
    # crossed remains True for band-flip (50-11 invariant).
    assert fields["crossed"] is True


@pytest.mark.asyncio
async def test_apply_patch_npc_disposition_remains_disposition_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After ``apply_world_patch`` mutates ``npc.disposition``, the field
    must still hold a ``Disposition`` object (not a raw int). If the
    mutator naively writes ``npc.disposition = npc.disposition + delta``
    where ``+`` returns an int, the type leaks back to int and every
    downstream ``.attitude()`` call breaks."""
    await _setup(monkeypatch, "test-disposition-stays-disposition")

    npc = _make_npc_with_disposition("Guard", 0)
    snapshot = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="caverns_sunden",
        characters=[_make_pc("Hero")],
        npcs=[npc],
    )
    snapshot.apply_world_patch(WorldStatePatch(npc_attitudes={"Guard": 20}))

    assert isinstance(snapshot.npcs[0].disposition, Disposition), (
        f"after apply_world_patch, npc.disposition leaked to "
        f"{type(snapshot.npcs[0].disposition).__name__} — must remain Disposition"
    )
    assert snapshot.npcs[0].disposition.value == 20
    assert snapshot.npcs[0].disposition.attitude() == Attitude.FRIENDLY
