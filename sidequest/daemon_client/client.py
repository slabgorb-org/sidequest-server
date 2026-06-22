"""Async JSON-RPC client for the sidequest media daemon.

Speaks the line-framed JSON protocol hosted by
``sidequest_daemon.media.daemon`` over a Unix domain socket. A connection
is opened per request for simplicity and per-request fault isolation
(one broken render doesn't poison a persistent socket that a dozen other
turns are sharing).

The daemon is an *optional* sidecar: if the socket does not exist, if the
daemon rejects the connection, or if the response times out, the client
surfaces a structured :class:`DaemonUnavailableError` so callers can
emit a clear OTEL span (``render.socket_unavailable`` / ``render.timeout``)
and move on — renders are best-effort, text-only play must continue.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, TypedDict

from opentelemetry import trace

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("sidequest.daemon_client")

DEFAULT_SOCKET_PATH = Path("/tmp/sidequest-renderer.sock")
DEFAULT_TIMEOUT_SECONDS = 600.0
"""Per-call cap for the daemon round-trip.

Sized for high-fidelity Z-Image (base 1.0, 20 steps) at ~108-125s per
render (per ``sidequest_daemon/media/zimage_config.py``) plus daemon
queue depth — in multiplayer the queue is hotter (2+ players → more
``visual_scene`` requests per wall-clock minute), so a render that
arrives behind 3 queued peers waits ~3×125s before its own ~125s job
runs, which the prior 180s cap silently dropped (playtest 2026-05-03
[BUG]: ``render.reply_unavailable error=daemon did not reply within
180.0s`` — the daemon completed and saved the PNG but the server had
already given up, orphaning the asset). 600s covers 4-5 queued
high-fidelity renders before the cap fires; bump again or drop to
``fidelity="turbo"`` per-call if MP throughput grows past that.

Per CLAUDE.md durable-retention principle: timing out here does NOT
cancel the daemon — orphaned PNGs are kept on disk for future repair.
"""

MAX_EMBED_BYTES = 32_768
"""Upper bound on embed() text payload (UTF-8 bytes).

SentenceTransformer MiniLM caps input at 256 tokens (~1 KB of English); 32 KB
is a generous ceiling that keeps a buggy caller from blocking the asyncio
event loop inside ``json.dumps`` / ``writer.write`` while still accommodating
any realistic lore fragment."""


class EmbedResponse(TypedDict):
    """Shape of a successful daemon embed reply.

    The daemon returns this dict verbatim at the ``result`` key of the
    JSON-RPC envelope. Keeping it typed (rather than ``dict[str, Any]``)
    lets mypy catch daemon-side schema drift at the client call site.
    """

    embedding: list[float]
    model: str
    latency_ms: int


class DaemonClientError(Exception):
    """Base class for daemon client errors."""


class DaemonUnavailableError(DaemonClientError):
    """The daemon socket is absent or unreachable. Caller should log a
    span and carry on without a render — the daemon is optional."""


class DaemonRequestError(DaemonClientError):
    """The daemon accepted the request but returned a structured error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class DaemonClient:
    """Async JSON-RPC client speaking the daemon's line-framed protocol."""

    def __init__(
        self,
        socket_path: Path | None = None,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._socket_path = socket_path or DEFAULT_SOCKET_PATH
        self._timeout = timeout_seconds

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    def is_available(self) -> bool:
        """Fast check: does the socket exist on disk? The daemon creates
        the sockfile eagerly on startup. This is cheaper than opening a
        connection for every turn just to detect an absent daemon."""
        return self._socket_path.exists()

    async def render(self, params: dict[str, Any]) -> dict[str, Any]:
        """Send a render request and return the result dict.

        :raises DaemonUnavailableError: socket missing, connection refused,
            or response timed out.
        :raises DaemonRequestError: daemon returned ``{"error": {...}}``.
        """
        return await self._call("render", params)

    async def embed(self, text: str) -> EmbedResponse:
        """Send an embed request and return the daemon's reply dict.

        Each call opens its own socket connection, so callers can await
        ``embed()`` concurrently with ``render()`` without client-side
        serialization. Daemon-side scheduling of embed vs. render is
        daemon-internal (see sidequest-daemon, story 37-23) — the client
        does not model or guarantee that concurrency.

        :raises ValueError: ``text`` exceeds ``MAX_EMBED_BYTES`` (UTF-8).
            The empty-text case is defended upstream —
            :class:`LoreFragment` enforces ``content`` min_length=1 at
            construction, and :func:`retrieve_lore_context` short-circuits
            on an empty query — so this client layer does not re-check.
        :raises DaemonUnavailableError: socket missing, connection refused,
            or response timed out.
        :raises DaemonRequestError: daemon returned ``{"error": {...}}``
            (e.g. ``EMBED_FAILED``), or the daemon returned a structurally
            invalid reply (missing / wrong-typed fields) — the latter
            surfaces as ``INVALID_RESPONSE`` so the worker's retry budget
            applies and the GM panel records a terminal outcome.
        """
        if len(text.encode("utf-8")) > MAX_EMBED_BYTES:
            raise ValueError(f"embed() text exceeds {MAX_EMBED_BYTES}-byte UTF-8 limit")
        result = await self._call("embed", {"text": text})
        # Runtime validation — EmbedResponse is a TypedDict, not a
        # pydantic model, so mypy-only shape checks don't catch a daemon
        # that sends ``{"embedding": null}`` (partial flush mid-crash),
        # a non-list embedding (schema drift), or a missing key (hot
        # reload). Surface the class of failure as DaemonRequestError so
        # the worker's retry budget applies and the GM panel sees a
        # terminal ``INVALID_RESPONSE`` outcome rather than an unlabelled
        # partial span from a bubbled-up KeyError/TypeError.
        try:
            embedding = result["embedding"]
            model = result["model"]
            latency_ms = result["latency_ms"]
        except KeyError as exc:
            raise DaemonRequestError("INVALID_RESPONSE", f"embed reply missing key {exc}") from exc
        if not isinstance(embedding, list):
            raise DaemonRequestError(
                "INVALID_RESPONSE",
                "embed reply 'embedding' is not a list",
            )
        if not embedding:
            # Zero-length embedding would propagate to
            # LoreStore.requeue_dimension_mismatched(0) and wipe every
            # stored vector. Refuse at the boundary.
            raise DaemonRequestError(
                "INVALID_RESPONSE",
                "embed reply 'embedding' is zero-length",
            )
        # ``bool`` is a subclass of ``int`` in Python; exclude it so a
        # daemon returning ``[True, False]`` does not silently pass as
        # a valid embedding of 1.0 / 0.0 floats.
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in embedding):
            raise DaemonRequestError(
                "INVALID_RESPONSE",
                "embed reply 'embedding' contains non-numeric values",
            )
        if (
            not isinstance(model, str)
            or not isinstance(latency_ms, int)
            or isinstance(latency_ms, bool)
        ):
            raise DaemonRequestError(
                "INVALID_RESPONSE",
                "embed reply 'model'/'latency_ms' have wrong types",
            )
        return EmbedResponse(
            embedding=embedding,
            model=model,
            latency_ms=latency_ms,
        )

    async def heartbeat_listener(
        self,
        *,
        poll_interval_seconds: float = 5.0,
        max_idle_seconds: float = 15.0,
    ) -> None:
        """Long-running coroutine that opens a connection to the daemon
        and feeds incoming heartbeat events into
        :func:`sidequest.daemon_client.state_mirror.get_mirror`.

        Story 45-31. Wired into the FastAPI startup hook so the mirror
        starts populating as soon as the server boots. Without this
        wiring, the dispatcher's UNRESPONSIVE branch is unreachable and
        the Felix anti-13-minute-silence fallback never engages.

        Cadence rationale (review H2): the daemon-side
        ``start_periodic_heartbeat`` is *defined* but is NOT wired
        into ``_run_daemon`` — periodic emit broadcast is a deferred
        follow-up. Until that lands, the only sources of fresh
        heartbeats are (a) per-request connections during active
        gameplay and (b) this listener's reconnect cycle. The default
        ``max_idle_seconds=15s`` keeps the per-connection cadence at
        roughly 4× faster than the default unresponsive threshold
        (``2 × 30s = 60s``), so the mirror sees a fresh receive ts
        well inside the staleness window even during quiet idle
        stretches.

        :param poll_interval_seconds: How long to wait between
            re-connection attempts when the daemon is unreachable.
        :param max_idle_seconds: After this many seconds without any
            line on the socket, the listener forces a reconnect — the
            daemon may have hung mid-byte and silently stopped emitting.
        """
        from sidequest.daemon_client.state_mirror import get_mirror

        mirror = get_mirror()

        # Story 153-8 (ADR-131): track whether the daemon has been unavailable
        # since the last successful connection so a genuine unavailable->available
        # edge can be surfaced loudly. Starts True: a server that boots while the
        # daemon is down must announce the daemon's *first* arrival, not just
        # silently begin embedding/rendering once the socket appears.
        seen_unavailable = True

        while True:
            if not self.is_available():
                seen_unavailable = True
                await asyncio.sleep(poll_interval_seconds)
                continue
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_unix_connection(path=str(self._socket_path)),
                    timeout=poll_interval_seconds,
                )
            except (FileNotFoundError, ConnectionRefusedError, OSError, TimeoutError):
                # Socket file present but the daemon isn't accepting yet — still
                # unavailable from the dispatch path's point of view.
                seen_unavailable = True
                await asyncio.sleep(poll_interval_seconds)
                continue

            # Story 153-8 (ADR-131): the socket just became reachable after a
            # stretch of unavailability. Surface the recovery so the GM panel and
            # the server log confirm the daemon client reconnected — the
            # embed/render dispatch resumes on the next turn. Per CLAUDE.md "No
            # Silent Fallbacks" the unavailable->available flip must never be a
            # silent recovery.
            if seen_unavailable:
                seen_unavailable = False
                logger.info(
                    "daemon.reconnected socket=%s — embed/render dispatch resumes",
                    self._socket_path,
                )
                from sidequest.telemetry.watcher_hub import (
                    publish_event as _watcher_publish,
                )

                _watcher_publish(
                    "state_transition",
                    {
                        "field": "daemon",
                        "op": "reconnected",
                        "socket": str(self._socket_path),
                    },
                    component="daemon",
                )

            # Send a `status` request to keep the connection active past
            # the daemon's read loop. The accept-time heartbeat fires
            # unconditionally before any request is read; the status
            # reply itself is harmless and we discard it. Keeping the
            # connection open lets the daemon emit additional heartbeats
            # if a render or embed lock cycles while we're listening.
            try:
                req = (
                    json.dumps({"id": "heartbeat-listener", "method": "status", "params": {}})
                    + "\n"
                )
                writer.write(req.encode())
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError):
                # Best-effort close on a writer we've already lost.
                # The connection is broken; close() may itself raise
                # OSError on the dead transport. Eating the close
                # error here is safe — the next reconnect attempt
                # establishes a fresh socket.
                with contextlib.suppress(Exception):
                    writer.close()
                await asyncio.sleep(poll_interval_seconds)
                continue

            try:
                while True:
                    try:
                        raw = await asyncio.wait_for(reader.readline(), timeout=max_idle_seconds)
                    except TimeoutError:
                        # No heartbeat or reply for the full window —
                        # the mirror's is_unresponsive() will fire from
                        # the absence of fresh heartbeats. Reconnect.
                        break
                    if not raw:
                        break
                    line = raw.decode().strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError as exc:
                        # No silent fallback: surface daemon protocol
                        # corruption (post-port shape drift, partial
                        # flush mid-crash) loudly so the GM panel can
                        # see it. Continue reading — one bad line must
                        # not crash the listener.
                        logger.warning(
                            "heartbeat_listener.invalid_json line=%r error=%s",
                            line[:200],
                            exc,
                        )
                        continue
                    if obj.get("event") == "heartbeat":
                        try:
                            mirror.record_heartbeat(
                                queue=str(obj.get("queue", "")),
                                state=str(obj.get("state", "")),
                                queue_depth=int(obj.get("queue_depth") or 0),
                                ts_monotonic=float(obj.get("ts_monotonic") or 0.0),
                            )
                        except (ValueError, TypeError):
                            # Malformed heartbeat — log and keep
                            # reading; never let one bad event crash
                            # the listener.
                            logger.warning("heartbeat_listener.invalid_event %s", obj)
            finally:
                writer.close()
                # ``wait_closed()`` after ``close()`` can raise
                # CancelledError (when the listener task is being
                # cancelled at shutdown) or OSError (when the
                # transport is already gone). Both are expected
                # teardown outcomes — suppress to keep the listener
                # exit clean.
                with contextlib.suppress(Exception):
                    await writer.wait_closed()

    async def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = uuid.uuid4().hex[:12]
        with tracer.start_as_current_span("daemon_client.request") as span:
            span.set_attribute("daemon.method", method)
            span.set_attribute("daemon.request_id", request_id)
            if "tier" in params:
                span.set_attribute("daemon.tier", str(params["tier"]))
            if method == "embed":
                span.set_attribute("daemon.text_len", len(str(params.get("text", ""))))
            if not self.is_available():
                span.set_attribute("daemon.outcome", "socket_missing")
                raise DaemonUnavailableError(f"daemon socket not found at {self._socket_path}")
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_unix_connection(path=str(self._socket_path)),
                    timeout=self._timeout,
                )
            except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
                span.set_attribute("daemon.outcome", "connection_failed")
                span.set_attribute("daemon.error", type(exc).__name__)
                raise DaemonUnavailableError(str(exc)) from exc
            except TimeoutError as exc:
                span.set_attribute("daemon.outcome", "connect_timeout")
                raise DaemonUnavailableError(
                    f"timed out opening daemon socket after {self._timeout}s"
                ) from exc

            try:
                req_line = json.dumps({"id": request_id, "method": method, "params": params}) + "\n"
                writer.write(req_line.encode())
                await writer.drain()
                # Story 45-31: the daemon emits per-queue heartbeat
                # lines on connection accept and around the lock
                # acquire/release. Drain those into the mirror, then
                # keep reading until we hit the actual reply (id +
                # result/error). Heartbeats and replies are
                # mutually-exclusive shapes: heartbeats have an
                # ``event`` key, replies have ``id`` + ``result`` /
                # ``error``.
                from sidequest.daemon_client.state_mirror import get_mirror

                _mirror = get_mirror()
                reply: dict[str, Any] | None = None
                while reply is None:
                    try:
                        raw = await asyncio.wait_for(reader.readline(), timeout=self._timeout)
                    except TimeoutError as exc:
                        span.set_attribute("daemon.outcome", "reply_timeout")
                        raise DaemonUnavailableError(
                            f"daemon did not reply within {self._timeout}s"
                        ) from exc
                    if not raw:
                        span.set_attribute("daemon.outcome", "eof_before_reply")
                        raise DaemonUnavailableError("daemon closed socket before sending a reply")
                    line = raw.decode().strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError as exc:
                        span.set_attribute("daemon.outcome", "invalid_json")
                        raise DaemonUnavailableError(f"daemon sent non-JSON reply: {exc}") from exc
                    if obj.get("event") == "heartbeat":
                        try:
                            _mirror.record_heartbeat(
                                queue=str(obj.get("queue", "")),
                                state=str(obj.get("state", "")),
                                queue_depth=int(obj.get("queue_depth") or 0),
                                ts_monotonic=float(obj.get("ts_monotonic") or 0.0),
                            )
                        except (ValueError, TypeError):
                            logger.warning("daemon_client.invalid_heartbeat %s", obj)
                        continue
                    reply = obj
                if "error" in reply and reply["error"] is not None:
                    code = str(reply["error"].get("code", "UNKNOWN"))
                    msg = str(reply["error"].get("message", ""))
                    span.set_attribute("daemon.outcome", "error")
                    span.set_attribute("daemon.error_code", code)
                    raise DaemonRequestError(code, msg)
                result = reply.get("result") or {}
                span.set_attribute("daemon.outcome", "ok")
                return dict(result)
            finally:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()


def render_enabled() -> bool:
    """``SIDEQUEST_RENDER_ENABLED=1`` opts a process into daemon dispatch.

    Default off so the test suite doesn't accidentally spin up a socket
    connection on every narration turn. The production server sets this
    in its systemd / launchd unit.
    """
    return os.environ.get("SIDEQUEST_RENDER_ENABLED", "").strip() in {
        "1",
        "true",
        "yes",
        "on",
    }
