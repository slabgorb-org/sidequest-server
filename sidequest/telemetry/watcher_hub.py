"""Process-wide pub/sub for WatcherEvent broadcasts.

Lives in `sidequest.telemetry` rather than `sidequest.server` so subsystem
code (orchestrator, game, genre) can publish semantic events without
pulling in FastAPI / uvicorn at import time. Importing
`sidequest.server.watcher` would trigger `sidequest.server.__init__`,
which imports `app.py`, which imports `uvicorn` — and uvicorn
reconfigures logging handlers at import, breaking pytest's caplog
fixture.

The FastAPI-facing pieces (WebSocket endpoint, OTEL SpanProcessor) live
in `sidequest.server.watcher` and import from here.

Reload safety: under ``uvicorn --reload`` the module is re-imported
whenever source files change. A naïve module-level ``watcher_hub =
WatcherHub()`` would create a fresh singleton on every reload, orphaning
any OTEL span processors registered against the previous instance and
turning the dashboard deaf once the first reload fires. We pin the hub
to a builtins attribute so the same instance survives re-imports of
this module within the same interpreter.
"""

from __future__ import annotations

import asyncio
import builtins
import json
import logging
import os
import sqlite3
from collections import deque
from datetime import UTC, datetime
from typing import Any, Protocol

logger = logging.getLogger(__name__)

_BUILTINS_HUB_ATTR = "_sidequest_watcher_hub_singleton"

# When set, every ``publish_event`` call also opens-and-closes a tiny OTEL
# span so the OTLP exporter (e.g. local Jaeger) sees the semantic event
# stream — not just spans started via ``tracer().start_as_current_span``.
# Default off to keep test event counts stable; opt-in via the env var.
# ``WatcherSpanProcessor`` recognizes the synthetic marker attribute and
# skips re-publishing them as ``agent_span_close`` events, so the GM
# dashboard is unaffected.
#
# Read live (not cached at import time) — the first incarnation cached
# at module load, but ``watcher_hub`` is imported very early in the app
# graph, sometimes before pytest sets the env var, sometimes before the
# uvicorn worker shell exports it. A live read costs ~50ns and removes
# an entire class of "the bridge silently isn't on" failures.
WATCHER_SYNTHETIC_ATTR = "sidequest.watcher_synthetic"


def _watcher_as_spans_enabled() -> bool:
    return os.environ.get("SIDEQUEST_WATCHER_AS_SPANS") == "1"


# Diagnostic counter — incremented on every successful synthetic span
# mint. Surfaced via :meth:`WatcherHub.stats` so the GM panel and ad-hoc
# probes can confirm the bridge is firing during gameplay (vs. only at
# resume). The first mint also emits an INFO log so server logs show
# unambiguous proof that the bridge is alive.
_synthetic_spans_minted: int = 0
_first_mint_logged: bool = False


class _Sendable(Protocol):
    """Structural stand-in for ``fastapi.WebSocket`` — anything with
    ``send_json``. Keeps this module free of fastapi."""

    async def send_json(self, data: dict[str, Any]) -> None: ...


class WatcherHub:
    """Thread-safe pub/sub for WatcherEvent broadcasts."""

    def __init__(self) -> None:
        self._subscribers: set[_Sendable] = set()
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        # Health counters so the hub can surface its own liveness via
        # `watcher.health` events (see :func:`publish_event`). A
        # self-observing observability layer is the point of the GM panel
        # — if the bus is silent, the operator needs to see WHY.
        self._published_count: int = 0
        self._dropped_count: int = 0
        # Ring buffer of the last N already-serialized events. Replayed to
        # any new subscriber on connect so a dashboard refresh mid-session
        # doesn't reset every panel to zero. Bounded so a long session
        # can't exhaust memory; oldest events drop on overflow, matching
        # ADR-090's "lossy by design" stance. 2000 entries ≈ 130 turns of
        # fully-instrumented multiplayer at observed event volume.
        self._buffer: deque[dict[str, Any]] = deque(maxlen=2000)

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Remember the FastAPI event loop so background-thread publishers
        can hop onto it. Called once during app startup."""
        self._loop = loop

    async def subscribe(self, ws: _Sendable) -> None:
        async with self._lock:
            self._subscribers.add(ws)
        logger.info("watcher.subscribed total=%d", len(self._subscribers))

    async def unsubscribe(self, ws: _Sendable) -> None:
        async with self._lock:
            self._subscribers.discard(ws)
        logger.info("watcher.unsubscribed total=%d", len(self._subscribers))

    def publish(self, event: dict[str, Any]) -> None:
        """Broadcast an event to all subscribers.

        Safe to call from any thread. If the event loop isn't bound yet
        (process start-up race), drop the event — the dashboard treats
        the stream as lossy by design.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            self._dropped_count += 1
            return
        self._published_count += 1
        asyncio.run_coroutine_threadsafe(self._broadcast(event), loop)

    def stats(self) -> dict[str, int]:
        """Snapshot of broadcast counters. Exposed so the GM dashboard
        can confirm the bus is alive without grepping the server log.

        ``synthetic_spans`` reflects the watcher→OTLP span bridge — useful
        for diagnosing whether semantic events are reaching Jaeger.
        """
        return {
            "subscribers": len(self._subscribers),
            "published": self._published_count,
            "dropped": self._dropped_count,
            "synthetic_spans": _synthetic_spans_minted,
            "watcher_as_spans": int(_watcher_as_spans_enabled()),
            "buffered": len(self._buffer),
        }

    async def _broadcast(self, event: dict[str, Any]) -> None:
        # Pre-serialize once with a tolerant encoder to a JSON-safe dict.
        # This decouples encoding errors (one bad publisher) from
        # delivery errors (one dead subscriber). Without this, a Pydantic
        # ``NonBlankString`` (or any other non-stdlib JSON value) hidden
        # in an event raised ``TypeError`` inside Starlette's
        # ``send_json``; the per-subscriber ``except`` then treated every
        # live WebSocket as dead and evicted the GM dashboard.
        # (Playtest 2026-04-29.)
        try:
            safe_event = json.loads(json.dumps(event, default=_json_default, separators=(",", ":")))
        except (TypeError, ValueError) as exc:
            # One bad event must not kill subscribers. Log loudly so the
            # offending publisher is fixable, then drop the event.
            logger.warning(
                "watcher_hub.serialize_failed event_type=%s err=%r — event dropped, subscribers preserved",
                event.get("event_type", "?"),
                exc,
            )
            self._dropped_count += 1
            return
        # Buffer the serialized event AND snapshot subscribers under the
        # same lock so a concurrent ``replay`` sees a consistent view —
        # either the event is in the buffer and visible to replay, or
        # the subscriber list snapshot doesn't yet include the
        # in-flight subscriber.
        async with self._lock:
            self._buffer.append(safe_event)
            targets = list(self._subscribers)
        if not targets:
            return
        dead: list[_Sendable] = []
        for ws in targets:
            try:
                await ws.send_json(safe_event)
            except Exception:  # noqa: BLE001 — broadcast is best-effort
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._subscribers.discard(ws)
            logger.info(
                "watcher.subscribers_pruned count=%d remaining=%d",
                len(dead),
                len(self._subscribers),
            )

    async def replay(self, ws: _Sendable) -> int:
        """Send every buffered event to ``ws`` in publish order.

        Best-effort: a per-event ``send_json`` failure aborts replay
        with the partial count rather than raising. The hub's internal
        state is never mutated by this call. Used by the watcher
        endpoint after the hello frame and before subscribing the
        socket to live broadcasts, so a dashboard refresh mid-session
        sees prior history before any new event arrives.
        """
        async with self._lock:
            snapshot = list(self._buffer)
        sent = 0
        for event in snapshot:
            try:
                await ws.send_json(event)
            except Exception:  # noqa: BLE001 — replay is best-effort
                return sent
            sent += 1
        return sent


def _json_default(obj: Any) -> Any:
    """Tolerant JSON fallback for watcher events.

    Subsystem code publishes typed values that the standard ``json``
    module can't encode — most commonly Pydantic ``RootModel`` newtypes
    (``NonBlankString``, ``Stat``) and ``datetime``. Coercing to ``str``
    is the right call for the GM dashboard: the dashboard treats event
    values as opaque labels, and a string representation preserves
    every field that any subsystem cares to inspect.

    Falls through to ``TypeError`` for anything else so a genuinely bad
    event surfaces in the per-event ``serialize_failed`` warning rather
    than silently degrading.
    """
    # Pydantic RootModel — covers NonBlankString, Stat, and any future
    # transparent newtype.
    root = getattr(obj, "root", None)
    if root is not None and isinstance(root, (str, int, float, bool)):
        return root
    # ``datetime``/``date``/``UUID``: ``str()`` round-trips. Same for
    # ``Path`` and ``Decimal``.
    if isinstance(obj, (datetime,)):
        return obj.isoformat()
    if hasattr(obj, "__str__"):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


# Module-level singleton. FastAPI runs one app per process, so a single
# hub is correct by construction. Subsystem code that needs to publish
# semantic events (`turn_complete`, `state_transition`, etc.) imports
# this and calls :func:`publish_event` — no dependency injection
# required. Safe to import at module load: `publish` is a no-op until
# :meth:`WatcherHub.bind_loop` runs during FastAPI startup.
#
# Pin the instance to ``builtins`` so ``uvicorn --reload`` — which
# re-imports changed modules — preserves the same hub across reloads.
# Without this, each reload installs a fresh hub, orphaning all OTEL
# span processors registered against the previous instance and turning
# the dashboard deaf. (Playtest 2026-04-23.)
#
# Identity check is by-name, not ``isinstance``: after ``importlib.reload``
# the new ``WatcherHub`` class is a fresh object, so an instance created
# before the reload fails ``isinstance`` against the post-reload class
# even though its interface is identical. ``type(x).__name__`` is stable.
_existing = getattr(builtins, _BUILTINS_HUB_ATTR, None)
if _existing is not None and type(_existing).__name__ == "WatcherHub":
    watcher_hub: WatcherHub = _existing  # type: ignore[assignment]
else:
    watcher_hub = WatcherHub()
    setattr(builtins, _BUILTINS_HUB_ATTR, watcher_hub)


# ---------------------------------------------------------------------------
# Process-wide event store binding — persists encounter state_transition events
# to the SQLite events table. Bound at session-handler startup; global by
# design (one session per process during playtest). See Task 20 doc comment.
# ---------------------------------------------------------------------------

_event_store = None  # bound at session-handler startup; weakref-safe by class id

# Per-write save-DB serialization is now provided by the process-wide
# ``SAVE_WRITE_LOCK`` (RLock) from ``sidequest.game.persistence``. See
# that module's doc block for the acquire-order rule and the consumer
# list. The two persistence helpers below acquire the same lock as
# every other writer (persistence.py, server/emitters.py). The lock is
# imported lazily inside each helper (function-local) to avoid a
# module-scope circular import: persistence.py imports watcher_hub at
# line 27, so a module-scope ``from persistence import SAVE_WRITE_LOCK``
# here would fail when persistence is imported first (partial module).
# ``SAVE_WRITE_LOCK`` is also exposed as a module attribute via
# ``__getattr__`` below so external assertions can verify the binding.


def bind_event_store(store) -> None:
    """Bind a SqliteStore so encounter watcher events persist as rows.

    Multiple binds replace; ``None`` clears (used by tests).
    """
    global _event_store
    _event_store = store


_KIND_BY_OP: dict[str, str] = {
    "started": "ENCOUNTER_STARTED",
    "beat_applied": "ENCOUNTER_BEAT_APPLIED",
    "metric_advance": "ENCOUNTER_METRIC_ADVANCE",
    "beat_skipped": "ENCOUNTER_BEAT_SKIPPED",
    "tag_created": "ENCOUNTER_TAG_CREATED",
    "tag_backfire": "ENCOUNTER_TAG_CREATED",  # backfire is still a tag-creation row
    "status_added": "ENCOUNTER_STATUS_ADDED",
    "yield_received": "ENCOUNTER_YIELD",
    "yield_resolved": "ENCOUNTER_YIELD",
    "resolved": "ENCOUNTER_RESOLVED",
    # Reserved — no current callsite emits this op (would break ENCOUNTER_RESOLVED-last
    # ordering invariant). Future sites that emit signal-creation outside of resolution
    # may use it.
    "resolution_signal_emitted": "ENCOUNTER_RESOLUTION_SIGNAL",
    "resolution_signal_consumed": "ENCOUNTER_RESOLUTION_SIGNAL",
}


def _maybe_persist_encounter_row(event: dict) -> None:
    global _event_store
    if _event_store is None:
        return
    if event.get("event_type") != "state_transition":
        return
    fields = event.get("fields", {})
    if fields.get("field") != "encounter":
        return
    op = str(fields.get("op", ""))
    kind = _KIND_BY_OP.get(op)
    if kind is None:
        return
    payload = json.dumps(fields, default=_json_default)
    try:
        from sidequest.game.persistence import SAVE_WRITE_LOCK

        with SAVE_WRITE_LOCK:
            _event_store._conn.execute(
                "INSERT INTO events (kind, payload_json, created_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                (kind, payload),
            )
            _event_store._conn.commit()
    except sqlite3.ProgrammingError as exc:
        # The bound store's connection has been closed out from under us
        # (typical: session disconnect closed the store but never called
        # ``bind_event_store(None)``; or tests that close the store via
        # ``store.close()`` without unbinding first). Treat the binding
        # as stale and clear it so the next caller doesn't hit the same
        # dead handle. We log loudly (warning, not silent) so the GM
        # panel / OTEL trail records the recovery — silently swallowing
        # would mask a real lifecycle mismatch.
        logger.warning(
            "watcher_hub.event_store_closed — clearing stale binding (kind=%s op=%s err=%s)",
            kind,
            op,
            exc,
        )
        _event_store = None


def _persist_turn_telemetry(event: dict) -> None:
    """Append one raw turn_telemetry row for every watcher publish.

    Reuses the same process-global ``_event_store`` binding that
    ``_maybe_persist_encounter_row`` uses (bound at connect time; its
    ``_conn`` is the same connection the C2 turn transaction writes
    events/projection_cache through).

    Transaction discipline (the load-bearing invariant): under this
    codebase's default *deferred* isolation, ``conn.in_transaction`` is
    True iff a write transaction is already open on the connection. In the
    turn path the first DML is the C2 ``events`` INSERT, so
    ``in_transaction`` True ⟺ this turn's event row already exists ⟺
    ``MAX(seq) FROM events`` is that in-flight row. So:

      * in_transaction  -> join the open turn txn (NO commit); attribute
        ``event_seq = MAX(seq)``; the row commits/rolls back atomically
        with ``events``/``projection_cache``.
      * not in_transaction -> own short ``with conn:`` txn; ``event_seq``
        is NULL (fired outside an event frame — the spec's NULL case).

    Fully wrapped: ANY failure logs loudly (``turn_telemetry.sink_failed``)
    and returns. Never raises, never stalls the turn, never writes to a
    different DB (No-Silent-Fallbacks).
    """
    store = _event_store
    if store is None:
        return  # legacy/in-memory session: no durable save bound (not an error)
    try:
        conn = store._conn
        component = event.get("component", "sidequest-server")
        event_type = event.get("event_type", "")
        fields = event.get("fields", {})
        payload_json = json.dumps(fields, default=_json_default)
        rnd = fields.get("round") if isinstance(fields, dict) else None
        if not isinstance(rnd, int):
            rnd = None
        ts = datetime.now(UTC).isoformat()
        insert = (
            "INSERT INTO turn_telemetry "
            "(event_seq, round, ts, component, event_type, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?)"
        )
        from sidequest.game.persistence import SAVE_WRITE_LOCK

        with SAVE_WRITE_LOCK:
            if conn.in_transaction:
                ev_seq = conn.execute("SELECT MAX(seq) FROM events").fetchone()[0]
                conn.execute(
                    insert, (ev_seq, rnd, ts, component, event_type, payload_json)
                )  # NO commit: rides the open turn (C2) transaction
            else:
                with conn:
                    conn.execute(insert, (None, rnd, ts, component, event_type, payload_json))
    except Exception:  # noqa: BLE001 — telemetry must never crash a turn
        logger.warning(
            "turn_telemetry.sink_failed component=%s event_type=%s",
            event.get("component"),
            event.get("event_type"),
            exc_info=True,
        )
        return


def _coerce_attr_value(value: Any) -> Any:
    """Coerce a watcher field value to an OTEL-attribute-safe primitive.

    OTEL accepts ``str | bool | int | float`` and homogeneous sequences
    of those — those pass through so Jaeger renders them as native arrays.
    Anything else gets JSON-stringified using the same tolerant encoder
    the WebSocket broadcast uses, so Pydantic newtypes, datetimes, etc.
    round-trip the same way the dashboard sees them.
    """
    if isinstance(value, (str, bool, int, float)):
        return value
    if value is None:
        return ""
    if isinstance(value, (list, tuple)) and value:
        first_type = type(value[0])
        if first_type in (str, int, float) and all(type(x) is first_type for x in value):
            return list(value)
    try:
        return json.dumps(value, default=_json_default, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def _emit_watcher_span(
    event_type: str,
    fields: dict[str, Any],
    component: str,
    severity: str,
) -> None:
    """Mint a zero-duration OTEL span describing this watcher event.

    Attaches as a child of any active span, so traces in Jaeger group
    semantic events under the operation that triggered them.
    """
    # Local import keeps watcher_hub fastapi/uvicorn-free at module load
    # (the import-cycle reason this module exists separately from
    # ``sidequest.server.watcher`` — see module docstring).
    from opentelemetry import trace

    global _synthetic_spans_minted, _first_mint_logged

    tracer = trace.get_tracer("sidequest-server.watcher")
    with tracer.start_as_current_span(f"watcher.{event_type}") as span:
        span.set_attribute(WATCHER_SYNTHETIC_ATTR, "1")
        span.set_attribute("watcher.event_type", event_type)
        span.set_attribute("watcher.component", component)
        span.set_attribute("watcher.severity", severity)
        for k, v in fields.items():
            span.set_attribute(f"field.{k}", _coerce_attr_value(v))

    _synthetic_spans_minted += 1
    if not _first_mint_logged:
        _first_mint_logged = True
        logger.info(
            "watcher.span_bridge_first_mint event_type=%s "
            "(watcher→OTLP bridge confirmed live during gameplay)",
            event_type,
        )


def publish_event(
    event_type: str,
    fields: dict[str, Any],
    *,
    component: str = "sidequest-server",
    severity: str = "info",
) -> None:
    """Publish a semantic WatcherEvent to the dashboard.

    Matches the TypeScript ``WatcherEvent`` shape (see
    ``sidequest-ui/src/types/watcher.ts``). Safe to call from any thread;
    drops silently if the hub has no bound event loop yet (process
    startup race) or no subscribers (dashboard closed).

    When ``SIDEQUEST_WATCHER_AS_SPANS=1`` also mints a synthetic OTEL
    span so OTLP exporters (Jaeger) can see semantic events alongside
    real spans. The dashboard is unaffected: ``WatcherSpanProcessor``
    skips synthetic spans rather than double-publishing them.

    :param event_type: One of the ``WatcherEventType`` union members
        (``turn_complete``, ``state_transition``, ``game_state_snapshot``,
        ``prompt_assembled``, ``lore_retrieval``, etc.).
    :param fields: Event-specific fields. Schema per event type is
        defined on the TypeScript side; keep keys stable.
    :param component: Subsystem label — drives the Subsystems tab's
        component grouping. Examples: ``orchestrator``, ``npc_registry``,
        ``state.location``, ``prompt_builder``, ``rag``.
    :param severity: ``info`` | ``warning`` | ``error``.
    """
    watcher_hub.publish(
        {
            "timestamp": datetime.now(UTC).isoformat(),
            "component": component,
            "event_type": event_type,
            "severity": severity,
            "fields": fields,
        }
    )
    _maybe_persist_encounter_row({"event_type": event_type, "fields": fields})
    _persist_turn_telemetry({"event_type": event_type, "fields": fields, "component": component})
    if _watcher_as_spans_enabled():
        _emit_watcher_span(event_type, fields, component, severity)


def synthetic_spans_count() -> int:
    """Live snapshot of the watcher→OTLP synthetic-span counter.

    Lets a turn-level diagnostic capture before/after deltas and prove
    whether the bridge is firing during gameplay (vs. only on resume).
    Cheap (single attribute read); safe from any thread.
    """
    return _synthetic_spans_minted


def __getattr__(name: str):
    """Lazy module attribute access for ``SAVE_WRITE_LOCK``.

    Exposes the process-wide ``SAVE_WRITE_LOCK`` from
    ``sidequest.game.persistence`` as a module attribute without a
    module-scope import (which would cause a circular import because
    ``persistence.py`` imports ``watcher_hub`` at module scope).

    Only triggered on first access of the attribute name; Python caches
    the result in the module's ``__dict__`` so subsequent lookups are
    O(1) dict reads.
    """
    if name == "SAVE_WRITE_LOCK":
        from sidequest.game.persistence import SAVE_WRITE_LOCK

        # Cache in module dict so future accesses bypass __getattr__
        globals()["SAVE_WRITE_LOCK"] = SAVE_WRITE_LOCK
        return SAVE_WRITE_LOCK
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
