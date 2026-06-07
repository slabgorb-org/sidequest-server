"""Per-connection WebSocket session lifecycle.

Port of sidequest-server/src/session.rs + the connect/playing dispatch in
dispatch/connect.rs and dispatch/mod.rs (Phase 1 narration path only).

State machine: AwaitingConnect → Creating → Playing.
- SESSION_EVENT{connect}: bind genre/world, load or create GameSnapshot, emit
  SESSION_EVENT{connected}.
- PLAYER_ACTION (in Playing state): sanitize → orchestrator → NARRATION +
  NarrationEnd + persist.
- Unsupported message in wrong state: emit ERROR, do not crash.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

from opentelemetry import trace

from sidequest.game.projection.envelope import MessageEnvelope
from sidequest.game.projection_filter import FilterDecision, ProjectionFilter
from sidequest.game.session import (
    GameSnapshot,
)
from sidequest.protocol.messages import (
    ConfrontationMessage,
    ConfrontationPayload,
    DungeonMapMessage,
    NarrationMessage,
    NarrationSegmentMessage,
    NarrationSegmentPayload,
    ScrapbookEntryMessage,
    ScrapbookEntryPayload,
    SecretNoteMessage,
    SecretNotePayload,
    TacticalGridMessage,
)
from sidequest.server.session_state import (  # noqa: F401 — back-compat re-export; defs moved to leaf module (64-6), consumed by external importers + mock.patch targets
    _AUDIO_INTERPRETER,
    _build_pc_descriptor,
    _hash_snapshot,
    _SessionData,
    _shared_world_delta_to_state_delta,
    _State,
)
from sidequest.telemetry.spans import (
    SPAN_ORCHESTRATOR_PROCESS_ACTION,  # noqa: F401 — re-exported for OTEL catalog consumers
)
from sidequest.telemetry.watcher_hub import (
    publish_event as _watcher_publish,  # noqa: F401 — back-compat re-export consumed by emitters.py
)

logger = logging.getLogger(__name__)


tracer = trace.get_tracer("sidequest.server.session_handler")

# ---------------------------------------------------------------------------
# Event-kind → message class mapping (MP-03 Task 3)
# Extend this dict as additional kinds are routed through _emit_event.
# ---------------------------------------------------------------------------

_KIND_TO_MESSAGE_CLS: dict[str, type] = {
    "NARRATION": NarrationMessage,
    "NARRATION_SEGMENT": NarrationSegmentMessage,
    "CONFRONTATION": ConfrontationMessage,
    "SECRET_NOTE": SecretNoteMessage,
    "SCRAPBOOK_ENTRY": ScrapbookEntryMessage,
    # Cavern renderer revival (ADR-096 Task 20b). Emitted on room entry; not
    # event-sourced (no replay on reconnect — room payloads are re-emitted on
    # the next room transition; the initial room is emitted at chargen time).
    "TACTICAL_GRID": TacticalGridMessage,
    # Beneath Sünden BETTER fix (seam 3). Procedural megadungeon map
    # frame; not event-sourced (re-emitted every narration turn — the UI
    # just replaces its MapState, so reconnect repopulates on the next
    # turn). The NEW ADR-055 map message (ADR-019 MAP_UPDATE is dead).
    "DUNGEON_MAP": DungeonMapMessage,
    # ADR-136 (RELATIONSHIPS) is deliberately ABSENT here. Like its transient
    # sibling LOCATION_DESCRIPTION, the relationship roster is emitted via the
    # non-durable _emit_shared_world_frame broadcast path (not _emit_event), so
    # it is never written to the events table and never replayed by
    # _build_message_for_kind. On reconnect the resume site re-runs
    # _maybe_emit_relationships, which rebroadcasts a fresh roster from live
    # snapshot state. Registering it here would be a latent reconnect crash: a
    # stray persisted RELATIONSHIPS row would fall through _build_message_for_kind's
    # per-kind branches to the terminal ValueError (no reconstructor exists).
}

# Kinds persisted to the events table by side-channel writers (e.g.
# ``telemetry.watcher_hub._maybe_persist_encounter_row``) for OTEL replay
# but never fanned out to clients via ``_emit_event``. Replay must skip these
# rather than crash — pingpong 2026-04-26 [S3-BUG] Reconnect crash on
# ENCOUNTER_STARTED. Add new internal kinds here when their producers land.
_REPLAY_SKIP_KINDS: frozenset[str] = frozenset(
    {
        "ENCOUNTER_STARTED",
        "ENCOUNTER_BEAT_APPLIED",
        "ENCOUNTER_METRIC_ADVANCE",
        "ENCOUNTER_BEAT_SKIPPED",
        "ENCOUNTER_TAG_CREATED",
        "ENCOUNTER_STATUS_ADDED",
        "ENCOUNTER_YIELD",
        "ENCOUNTER_RESOLVED",
        "ENCOUNTER_RESOLUTION_SIGNAL",
        "ENCOUNTER_OPPONENT_ATTACK",
    }
)


# ---------------------------------------------------------------------------
# Replay helper (MP-03 Task 4)
# Reconstructs a typed protocol message from a persisted EventRow on reconnect.
# Distinct from _emit_event (live fan-out) but reuses _KIND_TO_MESSAGE_CLS as
# the single source of truth for kind → message class mapping.
# ---------------------------------------------------------------------------

# Canonical 8-4-4-4-12 hex UUID pattern. Used to detect saves that wrote
# ``core.name`` as the opaque player UUID before the with_lobby_name fix
# landed. Anchored to reject partial matches (e.g. a UUID embedded inside
# a legitimate name like "Rux-116f74b2-...").
_UUID_HEX_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _looks_like_uuid(value: str) -> bool:
    """True when ``value`` is shaped like a canonical UUID hex string.

    Case-insensitive, anchored at both ends. ``uuid.UUID(...)`` would be
    stricter (rejects weird variants), but for rename-on-resume we want
    the naive pattern match — if the saved name looks like a UUID, it's
    almost certainly the player_id that leaked in pre-fix.
    """
    return bool(_UUID_HEX_RE.match(value))


def _rename_resumed_character_if_uuid(
    *,
    snapshot: GameSnapshot,
    display_name: str,
    player_id: str,
) -> bool:
    """Rename characters whose ``core.name`` matches a UUID pattern.

    Walks ``snapshot.characters`` and, for each one whose ``core.name`` either
    equals the active ``player_id`` OR matches the canonical UUID regex,
    replaces the name with the ``display_name`` the client sent on connect.
    Returns True iff at least one character was renamed, so the caller can
    persist the snapshot. When ``display_name`` is blank or itself looks
    like a UUID we decline to rename — better to leave the UUID than to
    replace one opaque identifier with another.

    Context: pingpong 2026-04-24 — "Resumed character shows UUID as name".
    Pre-fix chargen used ``CharacterBuilder`` without ``with_lobby_name``,
    so the committed Character carried the player_id as its display name.
    The chargen path is fixed, but existing saves persist the UUID until
    the player is renamed. This function patches them on resume.

    Pydantic lets us assign through ``core.name``; the field_validator
    rejects blank strings, which is why we guard on ``display_name``.
    """
    if not display_name.strip():
        return False
    if _looks_like_uuid(display_name):
        return False
    renamed = False
    for character in snapshot.characters:
        current = character.core.name
        if current == player_id or _looks_like_uuid(current):
            character.core.name = display_name
            renamed = True
    return renamed


def _build_message_for_kind(*, kind: str, payload_json: str, seq: int) -> object | None:
    """Build a typed protocol message from a persisted event row for replay.

    Returns ``None`` for journal-only telemetry kinds (``_REPLAY_SKIP_KINDS``)
    so replay can step over them without crashing the entire reconnect — these
    rows live in the events table for OTEL persistence (see
    ``telemetry.watcher_hub._maybe_persist_encounter_row``) but were never
    intended for the client wire. Pingpong 2026-04-26 [S3-BUG]: dropping the
    fail-loud here is intentional; the caller logs the skip via OTEL.

    Raises ValueError for kinds that are neither in the live-emit map nor on
    the explicit skip-list — that's a real schema-drift bug worth surfacing.
    """
    import json

    if kind in _REPLAY_SKIP_KINDS:
        return None

    message_cls = _KIND_TO_MESSAGE_CLS.get(kind)
    if message_cls is None:
        raise ValueError(f"_build_message_for_kind: unknown event kind {kind!r}")

    data = json.loads(payload_json)
    data["seq"] = seq

    if kind == "NARRATION":
        from sidequest.protocol.messages import NarrationPayload as _NarrationPayload

        return message_cls(payload=_NarrationPayload(**data))

    if kind == "NARRATION_SEGMENT":
        return message_cls(payload=NarrationSegmentPayload(**data))

    if kind == "CONFRONTATION":
        return message_cls(payload=ConfrontationPayload(**data))

    if kind == "SECRET_NOTE":
        return message_cls(payload=SecretNotePayload(**data))

    if kind == "SCRAPBOOK_ENTRY":
        return message_cls(payload=ScrapbookEntryPayload(**data))

    # Unreachable: _KIND_TO_MESSAGE_CLS guard above catches unknowns.
    # Kept as a belt-and-suspenders hard fail.
    raise ValueError(f"_build_message_for_kind: no payload constructor for kind {kind!r}")


# ---------------------------------------------------------------------------
# Per-turn write-split: canonical save + per-peer filtered frames (G8)
# ---------------------------------------------------------------------------
#
# MP spec 2026-04-22: the canonical save on the narrator-host holds the union
# of every event as appended to EventLog (unfiltered). Each peer save holds
# only the per-peer filtered subset — the frames whose FilterDecision.include
# is True.
#
# `_project_frames` is the single shared core: given one envelope + filter +
# list of connected players, compute the per-recipient decisions. Both the
# production turn driver (`_emit_event`) and the test-facing helper
# `apply_turn_writes_for_test` route through this function so the invariant
# is tested in the same code path production exercises.


@dataclass
class SentFrame:
    """One outbound frame to one peer after projection filter."""

    player_id: str
    payload_json: str


def _project_frames(
    *,
    envelope: MessageEnvelope,
    projection_filter: ProjectionFilter,
    connected_players: list[str],
    view: object = None,
    on_decision: Callable[[str, FilterDecision], None] | None = None,
    tx: object = None,
    event_seq: int | None = None,
) -> list[tuple[str, FilterDecision]]:
    """Run the projection filter once per connected player.

    Returns every (player_id, decision) pair — caller decides what to do with
    excluded decisions (e.g. production still writes them to the projection
    cache via ``on_decision`` before discarding the frame).

    The canonical EventLog append is the caller's responsibility; this helper
    is purely the filter fan-out step.

    ``tx`` / ``event_seq`` are threaded down to the filter ONLY when this
    fan-out runs inside emit_event's open turn transaction. They let the
    visibility-gated invariant's ``invariant.secret_routed`` telemetry ride
    the turn tx (same connection) rather than opening a competing pooled
    connection that would self-deadlock on the per-session ``FOR UPDATE`` row
    lock under Postgres (ADR-115). The test-facing helper and lazy-fill caller
    omit them.
    """
    decisions: list[tuple[str, FilterDecision]] = []
    for pid in connected_players:
        decision = projection_filter.project(
            envelope=envelope,
            view=view,
            player_id=pid,
            tx=tx,  # type: ignore[arg-type]
            event_seq=event_seq,
        )
        if on_decision is not None:
            on_decision(pid, decision)
        decisions.append((pid, decision))
    return decisions


def apply_turn_writes_for_test(
    *,
    event_log: object,
    filter: ProjectionFilter,
    envelope: dict,
    connected_players: list[str],
    view: object = None,
) -> list[SentFrame]:
    """Test-facing write-split helper: canonical append + per-peer filter.

    Exercises the same core (`_project_frames`) as the production turn driver.
    The test fake ``event_log`` accepts a single positional MessageEnvelope on
    ``append``; production uses ``repo.transaction()`` + ``tx.append_event(kind=...,
    payload_json=...)`` inside a DB transaction — both converge on
    `_project_frames` for the per-peer decision loop.

    Canonical save receives the raw envelope exactly once. Each peer frame is
    emitted only when ``FilterDecision.include`` is True.
    """
    import json as _json

    canonical_env = MessageEnvelope(
        kind=envelope["kind"],
        payload_json=_json.dumps(envelope["payload"]),
        origin_seq=getattr(event_log, "next_seq", 0),
    )
    event_log.append(canonical_env)  # type: ignore[attr-defined]

    decisions = _project_frames(
        envelope=canonical_env,
        projection_filter=filter,
        connected_players=connected_players,
        view=view,
    )
    return [
        SentFrame(player_id=pid, payload_json=decision.payload_json)
        for pid, decision in decisions
        if decision.include
    ]


# ---------------------------------------------------------------------------
# Session state machine
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Module-level helpers — extracted to session_helpers.py and narration_apply.py.
# Re-exported here so existing imports (tests, external callers) keep working.
# ---------------------------------------------------------------------------

from sidequest.server.narration_apply import (  # noqa: E402 — back-compat re-export
    _apply_narration_result_to_snapshot,
)
from sidequest.server.session_helpers import (  # noqa: E402 — back-compat re-export
    _build_turn_context,
    _detect_missed_recurring_npcs,
    _detect_npc_identity_drift,
    _error_msg,
    _find_confrontation_def,
    _presence_msg,
    _render_url_from_path,
    _resolve_acting_character_name,
    _resolve_location_display,
    _sfx_ids_from_genre,
    _world_history_value,
    aggregate_visibility,
    build_secret_note_events,
    emit_secret_notes,
)
from sidequest.server.websocket_handlers.opening_helpers import (  # noqa: E402 — back-compat re-export
    _populate_opening_directive_on_chargen_complete,
)
from sidequest.server.websocket_session_handler import (  # noqa: E402 — back-compat re-export
    WebSocketSessionHandler,
)

__all__ = [
    # Top-level types defined in this module
    "SentFrame",
    "WebSocketSessionHandler",
    "_SessionData",
    "_State",
    # Module-level helpers re-exported from session_helpers / narration_apply
    "_apply_narration_result_to_snapshot",
    "_build_turn_context",
    "_detect_missed_recurring_npcs",
    "_detect_npc_identity_drift",
    "_error_msg",
    "_find_confrontation_def",
    "_populate_opening_directive_on_chargen_complete",
    "_presence_msg",
    "_render_url_from_path",
    "_resolve_acting_character_name",
    "_resolve_location_display",
    "_sfx_ids_from_genre",
    "_world_history_value",
    "aggregate_visibility",
    "apply_turn_writes_for_test",
    "build_secret_note_events",
    "emit_secret_notes",
]
