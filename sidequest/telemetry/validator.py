"""Layer-3 narrative validator — consumes TurnRecord, emits typed events.

Lifecycle: started by FastAPI's startup event (wired in Task 20), drained
on shutdown. A single asyncio.Task processes one TurnRecord at a time;
the queue is bounded and oldest-record-drops on QueueFull (faithful to
ADR-031's "lossy by design" intent).

The validator never raises into the dispatch hot path. Each check is
wrapped in try/except — a check exception fires a validation_warning
with severity=error rather than crashing the task.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable

from sidequest.telemetry.turn_record import TurnRecord
from sidequest.telemetry.watcher_hub import publish_event

logger = logging.getLogger(__name__)

CheckFn = Callable[[TurnRecord], Awaitable[None]]

# Capitalized two-word noun phrases — heuristic for "named entity in
# narration." Matches "Sir Reginald", "The Ironwood", "Lady Ashes" etc.
# False positives are fine — entity_check is a hint, not an oracle.
_NAMED_ENTITY_RE = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b")


async def entity_check(record: TurnRecord) -> None:
    """Warn when narration names an NPC / region / item absent from the
    snapshot.

    Reads:
      - narration
      - snapshot_after.npc_pool (iterable of NpcPoolMember)
      - snapshot_after.npcs (iterable of stateful Npc)
      - snapshot_after.discovered_regions (iterable of region names)
      - snapshot_after.inventory.items (iterable of item names)

    Wave 2A / story 45-52: reads from ``npc_pool`` + ``npcs`` rather than
    the dropped ``npc_registry``. Both canonical stores expose a ``name``
    (pool member: ``.name``; stateful npc: ``.core.name``) so the
    name-presence audit just unions both.
    """
    snap = record.snapshot_after
    known_names: set[str] = set()
    for member in getattr(snap, "npc_pool", None) or ():
        name = getattr(member, "name", None)
        if name:
            known_names.add(str(name))
    for npc in getattr(snap, "npcs", None) or ():
        core = getattr(npc, "core", None)
        name = getattr(core, "name", None) if core is not None else None
        if name:
            known_names.add(str(name))
    regions = getattr(snap, "discovered_regions", None) or ()
    known_names.update(str(r) for r in regions)
    inventory = getattr(snap, "inventory", None)
    if inventory is not None:
        items = getattr(inventory, "items", None) or ()
        for it in items:
            name = getattr(it, "name", None) or str(it)
            known_names.add(name)

    if not record.narration:
        return

    for match in _NAMED_ENTITY_RE.finditer(record.narration):
        candidate = match.group(1)
        if candidate not in known_names:
            publish_event(
                "validation_warning",
                {
                    "check": "entity",
                    "turn_id": record.turn_id,
                    "candidate": candidate,
                    "rationale": "narration names an entity not in snapshot",
                },
                component="validator",
                severity="warning",
            )
            # One warning per turn is sufficient; don't spam.
            return


_GRAB_VERBS = (
    "grab",
    "take",
    "pick up",
    "pocket",
    "stash",
    "loot",
    "snatch",
    "scoop",
    "lift",
    "claim",
)


async def inventory_check(record: TurnRecord) -> None:
    """Cross-check narration against inventory deltas."""
    narration = (record.narration or "").lower()
    delta = record.delta
    inv_changes = getattr(delta, "inventory_changes", None) or []
    has_inventory_patch = bool(inv_changes) or any(
        any("inventory" in f for f in p.fields_changed) for p in record.patches_applied
    )

    grabbed_in_narration = any(v in narration for v in _GRAB_VERBS)

    if grabbed_in_narration and not has_inventory_patch:
        publish_event(
            "validation_warning",
            {
                "check": "inventory",
                "turn_id": record.turn_id,
                "rationale": "narration describes a grab but no inventory patch",
            },
            component="validator",
            severity="warning",
        )

    for change in inv_changes:
        item = change.get("item") if isinstance(change, dict) else getattr(change, "item", None)
        if not item:
            continue
        if str(item).lower() not in narration:
            publish_event(
                "validation_warning",
                {
                    "check": "inventory",
                    "turn_id": record.turn_id,
                    "item": item,
                    "rationale": "patch added item but narration is silent",
                },
                component="validator",
                severity="warning",
            )


def _iter_owned(coll: object) -> Iterable[tuple[str, object]]:
    """Yield ``(owner_name, entry)`` for snapshot collections.

    ``GameSnapshot.characters`` and ``GameSnapshot.npcs`` are typed
    as ``list`` (each entry holds its own name on ``.core.name`` or
    ``.name``), but this validator was authored against the legacy
    ``dict[name, entry]`` shape. This helper accepts either: list entries
    are keyed by their ``core.name`` / ``name`` / index fallback so the
    rest of the check is shape-agnostic.
    """
    if isinstance(coll, dict):
        for k, v in coll.items():
            yield (str(k), v)
        return
    if isinstance(coll, list):
        for i, v in enumerate(coll):
            core = getattr(v, "core", None)
            name = getattr(core, "name", None) or getattr(v, "name", None) or f"[{i}]"
            yield (str(name), v)


async def patch_legality_check(record: TurnRecord) -> None:
    """Detect illegal post-patch state.

    Checks (per ADR-031 §"Patch legality"):
      - HP > max for any character or NPC
      - Dead NPC (hp <= 0) appears in patches_applied as an actor
    """
    snap = record.snapshot_after
    characters = getattr(snap, "characters", None) or []
    # Wave 2A / story 45-52: post-Wave-2A canonical store for stateful
    # NPCs (with edge pools) is ``snap.npcs``; the legacy ``npc_registry``
    # is gone. Edge lives on ``Npc.core.edge`` (current / max) per ADR-078.
    npcs = getattr(snap, "npcs", None) or []

    def _check_hp(label: str, owner: str, ch: object) -> None:
        hp = getattr(ch, "hp", None)
        hp_max = getattr(ch, "hp_max", None)
        if isinstance(hp, int) and isinstance(hp_max, int) and hp > hp_max:
            publish_event(
                "validation_warning",
                {
                    "check": "patch_legality",
                    "turn_id": record.turn_id,
                    "subject": owner,
                    "subject_kind": label,
                    "hp": hp,
                    "hp_max": hp_max,
                    "rationale": "HP exceeds maximum",
                },
                component="validator",
                severity="error",
            )

    def _edge_current(entry: object) -> int | None:
        core = getattr(entry, "core", None)
        if core is None:
            return None
        edge = getattr(core, "edge", None)
        if edge is None:
            return None
        return getattr(edge, "current", None)

    for owner, ch in _iter_owned(characters):
        _check_hp("character", owner, ch)
    for owner, npc in _iter_owned(npcs):
        _check_hp("npc", owner, npc)

    # Dead-actor check — post-Wave-2A Npc liveness reads from the edge pool
    # (``current <= 0``). Entries without an edge pool are treated as alive
    # (no claim) to preserve the pre-Wave-2A "absent = no claim" contract.
    dead_npcs = {
        name
        for name, npc in _iter_owned(npcs)
        if isinstance(_edge_current(npc), int) and (_edge_current(npc) or 0) <= 0
    }
    for patch in record.patches_applied:
        if patch.patch_type != "combat":
            continue
        for field in patch.fields_changed:
            for dead in dead_npcs:
                if dead in field and "hp" not in field:
                    publish_event(
                        "validation_warning",
                        {
                            "check": "patch_legality",
                            "turn_id": record.turn_id,
                            "actor": dead,
                            "rationale": "dead NPC referenced as actor in combat patch",
                        },
                        component="validator",
                        severity="error",
                    )
                    return


# Per-trope keyword sources — populated lazily from genre packs.
# Tests can monkeypatch this dict directly.
TROPE_KEYWORDS_SOURCE: dict[str, list[str]] = {}


def _trope_keywords(trope: str) -> list[str]:
    if trope in TROPE_KEYWORDS_SOURCE:
        return TROPE_KEYWORDS_SOURCE[trope]
    # Lazy load — sidequest.game.trope import deferred to avoid cycles.
    try:
        from sidequest.game import trope as trope_mod  # noqa: PLC0415

        keywords = getattr(trope_mod, "keywords_for", lambda _t: [])(trope)
        TROPE_KEYWORDS_SOURCE[trope] = list(keywords)
        return TROPE_KEYWORDS_SOURCE[trope]
    except Exception:  # noqa: BLE001
        return []


async def trope_alignment_check(record: TurnRecord) -> None:
    """For each beat that fired, warn if none of the trope's keywords
    appear in narration."""
    if not record.beats_fired:
        return
    narration_lower = (record.narration or "").lower()
    for trope, _threshold in record.beats_fired:
        keywords = _trope_keywords(trope)
        if not keywords:
            continue
        if not any(kw.lower() in narration_lower for kw in keywords):
            publish_event(
                "validation_warning",
                {
                    "check": "trope_alignment",
                    "turn_id": record.turn_id,
                    "trope": trope,
                    "expected_any_of": keywords,
                    "rationale": "trope beat fired but no keywords in narration",
                },
                component="validator",
                severity="warning",
            )


_SUBSYSTEM_WINDOW: deque[tuple[int, str]] = deque(maxlen=50)
_KNOWN_SUBSYSTEMS = {
    "narrator",
    "combat",
    "merchant",
    "world_builder",
    "scenario",
    "encounter",
    "chargen",
    "trope",
    "barrier",
}
_COVERAGE_GAP_THRESHOLD_TURNS = 10


def _reset_subsystem_window() -> None:
    """Test helper — clears the sliding window."""
    _SUBSYSTEM_WINDOW.clear()


async def subsystem_exercise_check(record: TurnRecord) -> None:
    """Per-turn rollup of which subsystem ran, plus periodic coverage_gap
    when a subsystem hasn't been exercised in N turns."""
    _SUBSYSTEM_WINDOW.append((record.turn_id, record.agent_name))

    publish_event(
        "subsystem_exercise_summary",
        {
            "turn_id": record.turn_id,
            "agent_name": record.agent_name,
            "window_depth": len(_SUBSYSTEM_WINDOW),
        },
        component="validator",
        severity="info",
    )

    if len(_SUBSYSTEM_WINDOW) < _COVERAGE_GAP_THRESHOLD_TURNS:
        return

    recent_agents = {
        agent for _t, agent in list(_SUBSYSTEM_WINDOW)[-_COVERAGE_GAP_THRESHOLD_TURNS:]
    }
    silent = _KNOWN_SUBSYSTEMS - recent_agents
    for sub in silent:
        publish_event(
            "coverage_gap",
            {
                "turn_id": record.turn_id,
                "subsystem": sub,
                "silent_turns": _COVERAGE_GAP_THRESHOLD_TURNS,
                "rationale": "no agent invocation in sliding window",
            },
            component="validator",
            severity="info",
        )


class Validator:
    """Single-consumer narrative validator pipeline."""

    def __init__(self, queue_maxsize: int = 32) -> None:
        self._queue: asyncio.Queue[TurnRecord] = asyncio.Queue(maxsize=queue_maxsize)
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._checks: list[CheckFn] = []
        # Health counters
        self.dropped_records: int = 0
        self._check_durations_ms: deque[tuple[str, float]] = deque(maxlen=200)
        self._heartbeat_interval: float = 30.0
        self._heartbeat_task: asyncio.Task[None] | None = None
        self.register_check(entity_check)
        self.register_check(inventory_check)
        self.register_check(patch_legality_check)
        self.register_check(trope_alignment_check)
        self.register_check(subsystem_exercise_check)

    def register_check(self, fn: CheckFn) -> None:
        """Register a check coroutine. Called once per TurnRecord."""
        self._checks.append(fn)

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def submit(self, record: TurnRecord) -> None:
        """Enqueue a record. On QueueFull, drop the oldest record."""
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
                self.dropped_records += 1
                publish_event(
                    "validation_warning",
                    {
                        "check": "validator.queue",
                        "reason": "queue_full",
                        "dropped_total": self.dropped_records,
                    },
                    component="validator",
                    severity="warning",
                )
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(record)
            except asyncio.QueueFull:
                self.dropped_records += 1

    async def start(self) -> None:
        if self.is_running():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="sidequest.validator")
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat(), name="sidequest.validator.heartbeat"
        )
        logger.info("validator.started")

    async def shutdown(self, grace_seconds: float = 2.0) -> None:
        self._stopping.set()
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task
            self._heartbeat_task = None
        if self._task is None:
            return
        # Drain remaining records up to the grace window.
        try:
            await asyncio.wait_for(self._queue.join(), timeout=grace_seconds)
        except TimeoutError:
            logger.warning(
                "validator.shutdown_grace_exceeded queued=%d",
                self._queue.qsize(),
            )
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        logger.info("validator.stopped")

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                record = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except TimeoutError:
                continue
            try:
                await self._validate(record)
            finally:
                self._queue.task_done()

    async def _validate(self, record: TurnRecord) -> None:
        # Build the Timeline `spans` array from `phase_durations_ms`.
        # Pre-fix the dashboard fell back to a single `agent_llm` bar
        # filling the row at full width, so a reader couldn't tell
        # what fraction of the turn was spent in prompt assembly /
        # narrator subprocess / state apply / persistence / broadcast
        # (playtest 2026-04-30 #7). Phases are laid out monotonically
        # in dict iteration order — the timings collector
        # (`sidequest/telemetry/turn_timings.py`) inserts them in
        # observed order, so the bars line up with the actual
        # pipeline sequence.
        phase_spans: list[dict] = []
        running = 0
        for phase_name, duration_ms in record.phase_durations_ms.items():
            phase_spans.append(
                {
                    "name": phase_name,
                    "component": "pipeline",
                    "start_ms": running,
                    "duration_ms": int(duration_ms),
                }
            )
            running += int(duration_ms)
        # Tail-add `agent_llm` if it isn't already broken out into
        # phases (e.g. degraded turns missing per-phase data) so the
        # Timeline always has at least one bar.
        if not phase_spans:
            phase_spans.append(
                {
                    "name": "agent_llm",
                    "component": record.agent_name or "narrator",
                    "start_ms": 0,
                    "duration_ms": int(record.agent_duration_ms),
                }
            )

        publish_event(
            "turn_complete",
            {
                # Identity
                "turn_id": record.turn_id,
                "turn_number": record.turn_id,  # alias for legacy dashboard consumers
                "player_id": record.player_id,
                "player_input": record.player_input,
                "agent_name": record.agent_name,
                # Timing & metering
                "agent_duration_ms": record.agent_duration_ms,
                "total_duration_ms": record.total_duration_ms,
                "phase_durations_ms": dict(record.phase_durations_ms),
                "phase_call_counts": dict(record.phase_call_counts),
                "_unaccounted_ms": max(
                    0,
                    record.total_duration_ms - sum(record.phase_durations_ms.values()),
                ),
                "token_count_in": record.token_count_in,
                "token_count_out": record.token_count_out,
                "extraction_tier": record.extraction_tier,  # int
                "is_degraded": record.is_degraded,
                # Timeline flame chart input — one bar per pipeline phase
                # so the GM panel sees the actual sequence rather than a
                # single agent_llm bar (playtest 2026-04-30 #7).
                "spans": phase_spans,
                # Mechanical detail
                "patches": [
                    {"patch_type": p.patch_type, "fields_changed": list(p.fields_changed)}
                    for p in record.patches_applied
                ],
                "patches_applied": [
                    p.patch_type for p in record.patches_applied
                ],  # legacy short-form
                "beats_fired": [{"trope": t, "threshold": th} for t, th in record.beats_fired],
                # Knowledge entries surfaced as footnotes this turn. The
                # narrator's footnote pipeline is independent of the
                # `patch.discovered_facts` path, so a turn that introduces
                # new Knowledge Gained chips on the UI but no
                # location/quest/lore patches used to read as
                # `delta_empty: true` on the dashboard — the lie-detector
                # missed real subsystem activity (playtest 2026-04-30 #8).
                "footnotes_count": record.footnotes_count,
                "delta_empty": (
                    not record.patches_applied
                    and not record.beats_fired
                    and record.footnotes_count == 0
                ),
            },
            component="validator",
            severity="warning" if record.is_degraded else "info",
        )
        for check in self._checks:
            t0 = time.perf_counter()
            try:
                await check(record)
            except Exception as exc:  # noqa: BLE001
                logger.exception("validator.check_failed check=%s", check.__name__)
                publish_event(
                    "validation_warning",
                    {
                        "check": check.__name__,
                        "error": str(exc),
                        "turn_id": record.turn_id,
                    },
                    component="validator",
                    severity="error",
                )
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            self._check_durations_ms.append((check.__name__, elapsed_ms))

    async def _heartbeat(self) -> None:
        while not self._stopping.is_set():
            try:
                await asyncio.sleep(self._heartbeat_interval)
            except asyncio.CancelledError:
                return
            durations = list(self._check_durations_ms)
            p50 = _percentile([d for _, d in durations], 50)
            p99 = _percentile([d for _, d in durations], 99)
            publish_event(
                "state_transition",
                {
                    "field": "validator.heartbeat",
                    "queue_depth": self._queue.qsize(),
                    "queue_max": self._queue.maxsize,
                    "dropped_records": self.dropped_records,
                    "check_p50_ms": p50,
                    "check_p99_ms": p99,
                },
                component="validator",
                severity="info",
            )


def _percentile(values: list[float], pct: int) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = max(0, min(len(s) - 1, int(len(s) * pct / 100)))
    return round(s[idx], 2)
