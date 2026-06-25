"""Event emission helpers extracted from WebSocketSessionHandler.

Phase 1 of the session_handler.py decomposition (see
docs/superpowers/specs/2026-04-27-session-handler-decomposition-design.md).

Each function takes `handler: WebSocketSessionHandler` as its first
argument and operates on the handler's mutable state. No new abstractions
introduced — this is pure extraction with byte-identical behavior to the
original methods on WebSocketSessionHandler.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from opentelemetry import trace

from sidequest.agents.perception_rewriter import rewrite_for_recipient
from sidequest.agents.pov_swap import (
    project_to_canonical_pronouns,
    swap_to_second_person,
)

if TYPE_CHECKING:
    from sidequest.game.projection.envelope import MessageEnvelope
    from sidequest.game.projection.view import SessionGameStateView
    from sidequest.game.projection_filter import FilterDecision
    from sidequest.game.session import GameSnapshot
    from sidequest.protocol.messages import ScrapbookEntryPayload
    from sidequest.server.session_handler import WebSocketSessionHandler, _SessionData
    from sidequest.server.session_room import SessionRoom

logger = logging.getLogger(__name__)
_tracer = trace.get_tracer("sidequest.server.emitters")

# Story 158-14 (AC3, [SEC]): player-supplied pronoun strings are freeform at
# chargen. Bound the value before it is written to an OTEL attribute so a player
# cannot stuff an unbounded essay into telemetry.
_MAX_PRONOUN_ATTR_LEN = 64


def _emit_recipient_dropped(kind: str, player_id: str, reason: str) -> None:
    """Surface — loudly — a recipient that was INCLUDED in the fan-out set
    but lost its outbound transport before the frame could be enqueued.

    This is almost always a client that dropped mid-broadcast: the socket
    was unregistered (``socket_for_player`` → ``None``) or its outbound queue
    was detached (``queue_for_socket`` → ``None``, exactly what
    ``room.detach_outbound`` leaves behind). The turn itself is NOT lost —
    fan-out runs only after the committed C2 transaction, so the event +
    projection cache are already durable and the recipient replays on
    reconnect. But a SILENT ``continue`` here was the exact blind spot behind
    the 2026-05-27 orphaned-turn playtest loop: the GM panel saw a clean turn
    while a player received nothing. Emit a WARNING log + a watcher event so
    the dashboard shows the delivery gap (No Silent Fallbacks + the OTEL
    lie-detector mandate). Telemetry must never crash the rest of the fan-out.
    """
    logger.warning(
        "emit_event.recipient_dropped kind=%s player_id=%s reason=%s",
        kind,
        player_id,
        reason,
    )
    try:
        from sidequest.server.session_handler import _watcher_publish

        _watcher_publish(
            "state_transition",
            {
                "field": "broadcast.recipient_dropped",
                "kind": kind,
                "recipient_player_id": player_id,
                "reason": reason,
            },
            component="broadcast",
            severity="warning",
        )
    except Exception:  # noqa: BLE001 — telemetry must never crash a turn
        logger.warning("emit_event.recipient_dropped watcher publish failed kind=%s", kind)


def expand_visibility_for_companions(
    envelope: MessageEnvelope, room: SessionRoom | None
) -> MessageEnvelope:
    """Widen an owner-private event's ``_visibility.visible_to`` to include the
    owner's bonded PETs, BEFORE projection runs (Story 159-3).

    This is the ONLY companion change to perception: the firewall in
    ``CoreInvariantStage`` is untouched; we only add an authorized recipient (a
    pet shares its owner's view). Only a list-valued ``visible_to`` on a
    visibility-gated kind (NARRATION_SEGMENT / SECRET_NOTE) is widened — the
    ``"all"`` sentinel and non-gated kinds pass through unchanged. Returns the
    same envelope object when nothing is added.
    """
    import json

    from sidequest.game.projection.envelope import MessageEnvelope as _Envelope
    from sidequest.game.projection.invariants import VISIBILITY_GATED_KINDS
    from sidequest.server.session_handler import _watcher_publish

    if room is None or envelope.kind not in VISIBILITY_GATED_KINDS:
        return envelope
    payload = json.loads(envelope.payload_json)
    viz = payload.get("_visibility")
    visible_to = viz.get("visible_to") if isinstance(viz, dict) else None
    if not isinstance(visible_to, list):
        return envelope

    added: list[str] = []
    widened = list(visible_to)
    for owner_pid in visible_to:
        for pet_pid in room.pets_of(owner_pid):
            if pet_pid not in widened:
                widened.append(pet_pid)
                added.append(pet_pid)
                _watcher_publish(
                    "companion.routed_as_pet",
                    {
                        "field": "companion.routed_as_pet",
                        "pet_player_id": pet_pid,
                        "owner_player_id": owner_pid,
                        "kind": envelope.kind,
                    },
                    component="companion",
                )
    if not added:
        return envelope
    payload["_visibility"]["visible_to"] = widened
    return _Envelope(
        kind=envelope.kind,
        payload_json=json.dumps(payload),
        origin_seq=envelope.origin_seq,
    )


def _deliver_to_connected_recipients(
    room: Any,
    recipients: Any,
    *,
    message_builder: Callable[[str], Any],
    kind: str,
) -> dict[str, Any]:
    """Shared recipient-delivery dispatch (Story 59-22).

    Walk ``recipients`` in order; for each, ask ``message_builder(pid)`` for the
    frame to send. A ``None`` return means "send nothing to this socket" — the
    single skip rule each caller folds its own gate into (``decision.include``
    for the projection fan-out; the supplier returning ``None`` for the
    CONFRONTATION path). A recipient whose socket/queue has gone mid-broadcast
    is surfaced via :func:`_emit_recipient_dropped` rather than skipped
    silently (the orphaned-turn blind spot — No Silent Fallbacks + the OTEL
    lie-detector mandate).

    Returns ``{pid: built_msg}`` for every recipient the builder produced a
    non-``None`` frame for — INCLUDING recipients whose delivery was then
    dropped — so a caller that needs a specific recipient's own frame (the
    emitter, for ``emit_event``'s return value) can recover it without invoking
    the builder a second time (a double call would double-fire the supplier's
    per-recipient OTEL spans).
    """
    built: dict[str, Any] = {}
    for pid in recipients:
        msg = message_builder(pid)
        if msg is None:
            continue
        built[pid] = msg
        socket_id = room.socket_for_player(pid)
        if socket_id is None:
            _emit_recipient_dropped(kind, pid, "socket_gone")
            continue
        queue = room.queue_for_socket(socket_id)
        if queue is None:
            _emit_recipient_dropped(kind, pid, "queue_detached")
            continue
        queue.put_nowait(msg)
    return built


def _deliver_fanout(
    room: Any,
    fanout: list[tuple[str, FilterDecision, dict]],
    *,
    message_cls: Any,
    payload_cls: Any,
    kind: str,
    seq: int,
) -> None:
    """Enqueue each included recipient's projected frame onto their outbound
    queue. A recipient whose socket/queue has gone (mid-broadcast drop) is
    surfaced via :func:`_emit_recipient_dropped` rather than skipped silently.

    Extracted from :func:`emit_event` so the mid-broadcast drop window is
    testable against a real ``SessionRoom`` with synthetic fan-out tuples — no
    genre pack, no projection, no DB. ``emit_event`` is the sole production
    caller.

    Story 59-22: the socket/queue/drop/``put_nowait`` dispatch is shared with
    the CONFRONTATION supplier path via :func:`_deliver_to_connected_recipients`.
    The per-recipient frame construction (the ``decision.include`` gate, the C3
    ``model_validate`` rebuild, the ``_visibility`` egress-strip, and the
    fail-loud ``fanout_failed`` log) is this path's own concern, so it lives in
    the builder handed to the shared helper.
    """
    by_pid = {other_pid: (decision, filtered_data) for other_pid, decision, filtered_data in fanout}

    def _build(other_pid: str) -> Any:
        decision, filtered_data = by_pid[other_pid]
        if not decision.include:
            return None
        try:
            if payload_cls is not None:
                # C3: rebuild the recipient payload from the filtered dict
                # alone (plus seq). Do NOT use model_copy(update=...) —
                # merging leaves fields absent from the filtered dict at their
                # canonical values, which would leak any field a future rule
                # drops entirely.
                # ADR-105 / Story 71-13: strip _visibility from the wire.
                # _visibility is a server-side sidecar consumed by the
                # projection pipeline; it must never appear in the serialised
                # frame sent to any client.  Universal egress-strip here
                # closes the pre-existing leak for ALL narration recipients.
                filtered_data.pop("_visibility", None)
                recipient_payload = payload_cls.model_validate({**filtered_data, "seq": seq})
                return message_cls(payload=recipient_payload)
            return message_cls(payload={**filtered_data, "seq": seq})
        except Exception:
            # Never silently fail fan-out; log and skip this recipient.
            logger.error(
                "emit_event.fanout_failed kind=%s other_pid=%s",
                kind,
                other_pid,
            )
            return None

    _deliver_to_connected_recipients(
        room,
        [other_pid for other_pid, _decision, _filtered_data in fanout],
        message_builder=_build,
        kind=kind,
    )


def persist_scrapbook_entry(
    handler: WebSocketSessionHandler,
    payload: ScrapbookEntryPayload,
) -> None:
    """Insert a scrapbook row into the dedicated table (schema in
    ``game/persistence.py``). The table allows multiple rows per turn —
    no UNIQUE on turn_id.
    """
    if handler._event_log is None:
        return  # Legacy non-slug path — no DB to write to
    handler._event_log.repository.append_scrapbook_entry(
        turn_id=payload.turn_id,
        scene_title=payload.scene_title,
        scene_type=payload.scene_type,
        location=payload.location,
        image_url=payload.image_url,
        narrative_excerpt=payload.narrative_excerpt,
        world_facts=list(payload.world_facts),
        npcs_present=[
            {
                "name": ref.name,
                "role": ref.role,
                "disposition": ref.disposition,
                # Story 65-6: persist the world-scoped portrait URL so the
                # post-game gallery + forensic export carry it losslessly
                # (None when the NPC has no authored portrait).
                "portrait_url": ref.portrait_url,
            }
            for ref in payload.npcs_present
        ],
        render_status=payload.render_status,
    )


def update_scrapbook_image_url(
    handler: WebSocketSessionHandler,
    turn_id: int,
    image_url: str,
) -> bool:
    """Backfill the ``image_url`` for the most recent scrapbook entry at
    ``turn_id``. Returns ``True`` when a row was updated, ``False`` when
    no matching row exists yet (rare — would mean render.completed
    arrived before the SCRAPBOOK_ENTRY emit, which we never dispatch in
    that order) or when the handler has no event log (legacy non-slug
    path).

    Playtest 2026-05-02: scrapbook state vanishes on browser reload
    because the IMAGE message is broadcast live but never persisted, so
    `slug_connect.replay` rebuilds SCRAPBOOK_ENTRY events with their
    original ``image_url=None`` payload. Updating the table here lets
    the replay path JOIN and inject the URL into rebuilt SCRAPBOOK_ENTRY
    payloads (see `connect.py:_inject_scrapbook_image_urls`).

    Multiple scrapbook rows can share a turn_id in principle (the table
    has no UNIQUE constraint), but in practice the narrator emits one
    entry per turn. We update by ``rowid DESC LIMIT 1`` for the given
    turn — the most recent entry — since render.completed arrives
    after that turn's emit.
    """
    if handler._event_log is None:
        return False
    if not image_url:
        return False
    return handler._event_log.repository.update_scrapbook_image_url(
        turn_id=turn_id,
        image_url=image_url,
    )


def _pronouns_for_pc(snapshot: GameSnapshot, pc_name: str) -> str | None:
    """Return the pronouns string for a PC by name.

    Story 49-8: drives 2nd-person POV swap for the anchor recipient.

    Story 158-14: returns ``None`` when ``pc_name`` is absent from the snapshot
    entirely (a view/snapshot desync), distinct from ``""`` for a PC that IS
    present but has no pronouns. The caller emits different skip reasons for the
    two cases — previously both collapsed to ``""`` and were indistinguishable.
    """
    for c in snapshot.characters:
        if c.core.name == pc_name:
            return c.pronouns or ""
    return None


def _apply_pov_swap(
    payload_dict: dict,
    *,
    recipient_player_id: str,
    view: SessionGameStateView,
    snapshot: GameSnapshot,
) -> dict:
    """Rewrite the ``text`` field so the RECIPIENT reads their OWN PC in
    2nd-person ("you"), re-anchored per recipient — not per card.

    Story 49-8 stamped a single ``anchor_pc`` (the card's primary actor) and
    swapped only for the recipient whose PC == anchor_pc, so a non-anchor
    recipient read their own PC in 3rd person on their own screen (the 158-8
    playtest defect). The swap target is now the recipient's own PC: on each
    recipient's frame their name becomes "you" (carrying gendered-pronoun
    agreement, the 153-29 machinery), while every other PC stays a 3rd-person
    name. The swap is a no-op when the recipient's PC is absent from the prose,
    so an anchor-only card still reaches a not-mentioned recipient unchanged.

    Applies only to payloads carrying a pc-anchored visibility sidecar;
    atmospheric narration (no anchor) leaves prose alone.
    """
    viz = payload_dict.get("_visibility") or {}
    anchor_pc = viz.get("anchor_pc")
    pov_strategy = viz.get("pov_strategy")
    if not anchor_pc or pov_strategy != "pc_anchored":
        return payload_dict
    recipient_pc_name = view.character_of(recipient_player_id)
    if recipient_pc_name is None:
        return payload_dict
    raw_pronouns = _pronouns_for_pc(snapshot, recipient_pc_name)
    if raw_pronouns is None:
        # The view maps this recipient to a PC the snapshot does not contain
        # (a view/snapshot desync). Cannot swap; fail open to canonical prose
        # and emit a distinct skip span so the GM panel sees WHICH failure
        # (Story 158-14 AC2 — split from the empty-pronoun case below; both
        # previously collapsed to a silent return).
        with _tracer.start_as_current_span("narration.pov_swap_skipped") as span:
            span.set_attribute("recipient_pc", recipient_pc_name)
            span.set_attribute("reason", "pc_not_in_snapshot")
        return payload_dict
    # Story 158-14: chargen permits freeform pronouns (builder.py
    # pronouns_allow_freeform). Project the player's DISPLAY pronouns to a
    # canonical grammatical set so the localizer (which only knows the three
    # canonical sets) always receives a valid value from ANY caller, while the
    # player's freeform choice is preserved for display. A non-blank value
    # projects to a canonical set and the swap PROCEEDS — the freeform-pronoun
    # player reads "you" like everyone else, superseding 158-8's
    # fail-open-for-freeform.
    grammatical = project_to_canonical_pronouns(raw_pronouns)
    if grammatical is None:
        # Blank/whitespace-only pronouns — no grammar to derive. Fail open to
        # canonical prose and emit the skip span (Story 158-14 AC2 — this guard
        # was previously a silent return).
        with _tracer.start_as_current_span("narration.pov_swap_skipped") as span:
            span.set_attribute("recipient_pc", recipient_pc_name)
            span.set_attribute("reason", "pronouns_empty")
        return payload_dict
    if grammatical != raw_pronouns:
        # A non-canonical (freeform) value was projected — record the decision
        # for the GM panel (OTEL Observability Principle; the panel is the lie
        # detector). The player-supplied display value is bounded before it is
        # written (Story 158-14 AC3 — [SEC] PII/length).
        with _tracer.start_as_current_span("narration.pov_swap_projected") as span:
            span.set_attribute("recipient_pc", recipient_pc_name)
            span.set_attribute("display_pronouns", raw_pronouns[:_MAX_PRONOUN_ATTR_LEN])
            span.set_attribute("grammatical_pronouns", grammatical)
    text = payload_dict.get("text", "")
    if not isinstance(text, str) or not text:
        # Nothing to rewrite — fail open and emit the skip span (Story 158-14
        # AC2 — the previously-silent invalid-text guard).
        with _tracer.start_as_current_span("narration.pov_swap_skipped") as span:
            span.set_attribute("recipient_pc", recipient_pc_name)
            span.set_attribute("reason", "text_missing_or_invalid")
        return payload_dict
    swapped, _ = swap_to_second_person(
        text,
        target_name=recipient_pc_name,
        pronouns=grammatical,
    )
    return {**payload_dict, "text": swapped}


def _clear_confrontation_like(payload_model: object) -> object:
    """A cleared CONFRONTATION frame derived from a union payload.

    Story 59-20: when a ``per_recipient_payload`` supplier yields ``None`` for
    the emitter, ``emit_event`` must still hand the caller a frame — but never
    the canonical union. ``per_recipient_payload`` is a CONFRONTATION-only
    contract, so build the overlay-unmount payload (``active=False``, empty
    beats) from the union's encounter type + genre slug.
    """
    from sidequest.protocol.messages import ConfrontationPayload
    from sidequest.server.dispatch.confrontation import build_clear_confrontation_payload

    encounter_type = getattr(payload_model, "type", "") or ""
    genre_slug = getattr(payload_model, "genre_slug", "") or ""
    return ConfrontationPayload(
        **build_clear_confrontation_payload(encounter_type=encounter_type, genre_slug=genre_slug)
    )


def emit_event(
    handler: WebSocketSessionHandler,
    kind: str,
    payload_model: object,
    *,
    author_player_id: str | None = None,
    per_recipient_payload: Callable[[str], object] | None = None,
) -> object:
    """Persist an event to the EventLog and fan-out to all connected players.

    Invariants (per Plan 03):
    1. EventLog.append fires BEFORE any socket send.
    2. Fan-out consults ProjectionFilter per recipient.
    3. (solo only) The emitter receives the raw, unfiltered event.

    ``per_recipient_payload`` (Story 59-16 — single filtered CONFRONTATION
    delivery): when supplied, the projection/perception/POV machinery is
    bypassed and the canonical ``payload_model`` is persisted to the
    EventLog ONLY. A per-recipient frame — ``per_recipient_payload(pid)`` —
    is delivered to EVERY connected socket INCLUDING the emitter (overrides
    Invariant 3 for this emit: the emitter is no longer raw-bypassed). The
    supplier returns ``None`` for a socket that must receive nothing — an
    unseated/lobby socket, or a seated PC the supplier has already surfaced
    as unresolved (it owns the fail-loud ERROR span). The canonical union is
    therefore never sent to a client socket. This replaces the Story 49-7
    union-broadcast + per-PC overlay race. Returns the emitter's own frame.

    ``author_player_id`` (ADR-105 Track A): in merged-MP dispatch the
    driving handler is whichever player submitted *last* — NOT the sole
    author of a shared narration covering every seated PC. Invariant 3's
    raw-bypass is a solo assumption; applied to the merged-MP driver it
    makes that one player the only recipient with ZERO
    ``projection.filter.decide`` spans and the unfiltered shared blob
    (the confirmed ADR-105 firewall breach). When ``author_player_id`` is
    set, the emitter is projected/perception-rewritten/POV-swapped like
    any other recipient — one decide+rewrite+swap per DISTINCT connected
    player. When ``None`` (solo / legacy callers) Invariant 3 is
    preserved byte-identical (raw bypass; reconnect lazy_fill compensates
    — see test_projection_end_to_end_wiring). Content redaction of the
    shared blob is ADR-105 Track B, not this change.

    Returns the outbound message object for the calling player (the emitter).
    Falls back to a plain message without seq when EventLog is unavailable
    (legacy non-slug connect path doesn't initialize _event_log).
    """
    import json

    from pydantic import BaseModel

    from sidequest.game.projection.envelope import MessageEnvelope
    from sidequest.server.session_handler import (
        _KIND_TO_MESSAGE_CLS,
        _project_frames,
        logger,
    )

    message_cls = _KIND_TO_MESSAGE_CLS.get(kind)
    if message_cls is None:
        raise ValueError(f"emit_event: unknown kind {kind!r}")

    event_log = handler._event_log
    projection_filter = handler._projection_filter

    # Serialize payload excluding seq (seq is assigned from the DB row)
    if isinstance(payload_model, BaseModel):
        payload_json = payload_model.model_dump_json(exclude={"seq"})
    else:
        payload_json = json.dumps(payload_model)  # type: ignore[arg-type]

    if event_log is not None:
        room = handler._room
        _rotated_session_player_id = (
            handler._session_data.player_id if handler._session_data else None
        )
        # ADR-105 Track A: an explicit author_player_id means a shared
        # merged-MP turn — the driver is not the sole author and must be
        # projected, not raw-bypassed. None = solo/legacy (raw bypass).
        emitter_player_id = (
            author_player_id if author_player_id is not None else _rotated_session_player_id
        )
        project_emitter = author_player_id is not None
        # The driver frame, once projected (Track A). None ⇒ solo/legacy
        # raw-bypass path runs unchanged. Bound here so it is always
        # defined regardless of the room/projection_filter guard below.
        emitter_projected_dict: dict | None = None

        # OTEL lie-detector (CLAUDE.md): the emitter-authorship line was
        # silently wrong for 5 merged-MP turns because nothing surfaced
        # author≠rotated divergence. Emit it explicitly so the GM panel
        # can catch any regression of this exact binding. Never crash a
        # turn on telemetry.
        try:
            from sidequest.server.session_handler import _watcher_publish

            _watcher_publish(
                "state_transition",
                {
                    "field": "emit.author_resolved",
                    "kind": kind,
                    "emitter_player_id": emitter_player_id or "",
                    "rotated_session_player_id": _rotated_session_player_id or "",
                    "project_emitter": project_emitter,
                },
                component="projection",
            )
        except Exception:  # noqa: BLE001 — telemetry must never crash a turn
            logger.warning("emit.author_resolved watcher publish failed kind=%s", kind)

        # Story 59-16: single filtered delivery path. Persist the canonical
        # (union) payload to the EventLog only, then deliver a per-recipient
        # frame to every connected socket including the emitter. The union is
        # never enqueued onto a client socket. A supplier returning None means
        # "send nothing to this socket" (unseated, or a seated PC the supplier
        # already surfaced as unresolved). Bypasses projection/perception/POV:
        # CONFRONTATION is structured data whose per-PC class filtering is
        # computed by the supplier in the encounter layer (ADR-105: the
        # projection firewall has no class context).
        if (
            per_recipient_payload is not None
            and room is not None
            and callable(getattr(room, "connected_player_ids", None))
        ):
            repo = event_log.repository
            with repo.transaction() as tx:
                row = tx.append_event(kind=kind, payload_json=payload_json)
                seq = row.seq

            def _frame_for(pid: str) -> object | None:
                recipient_payload = per_recipient_payload(pid)
                if recipient_payload is None:
                    return None
                if isinstance(recipient_payload, BaseModel):
                    recipient_payload = recipient_payload.model_copy(update={"seq": seq})
                return message_cls(payload=recipient_payload)

            # Story 59-22: delivery dispatch is the shared helper; this path's
            # per-recipient frame construction is `_frame_for`. The helper
            # returns every built (non-None) frame keyed by pid, so the
            # emitter's own frame is recovered without calling `_frame_for`
            # (hence the supplier) a second time. A spy/no-op helper returns a
            # falsy value → emitter_msg falls through to the back-compat block.
            _recipients = room.connected_player_ids()
            built = _deliver_to_connected_recipients(
                room,
                _recipients,
                message_builder=_frame_for,
                kind=kind,
            )
            # Ping-pong 2026-06-07 ("MP confrontation DESYNC"): per-recipient
            # DELIVERY evidence. ``confrontation.peer_projection_broadcast``
            # logs room PRESENCE — it cannot distinguish "frame enqueued to
            # this seat" from "supplier silently returned None" (unseated /
            # unresolved-class skip). The desync was undiagnosable from the
            # text log for exactly this reason. ``skipped`` = connected pids
            # the supplier produced no frame for; gone-socket drops are
            # surfaced separately via emit_event.recipient_dropped.
            # ``built`` is falsy when a test spy/no-op helper replaced the
            # delivery dispatch (see emitter_msg fallback below) — guard so
            # the evidence line never crashes a turn.
            _delivered = sorted(built.keys()) if built else []
            logger.info(
                "confrontation.delivery kind=%s delivered=%s skipped=%s",
                kind,
                _delivered,
                sorted(set(_recipients) - set(_delivered)),
            )
            emitter_msg: object | None = built.get(emitter_player_id) if built else None

            # Return the emitter's own frame for caller back-compat. If the
            # emitter was not connected (or the supplier returned None for
            # them), build a frame from their supplied payload, falling back
            # to the canonical only as the function's return value (never a
            # socket delivery).
            if emitter_msg is None:
                fallback = per_recipient_payload(emitter_player_id) if emitter_player_id else None
                if fallback is None:
                    # Story 59-20: the supplier said "nothing for the emitter"
                    # (unseated, or a seated PC it surfaced as unresolved). NEVER
                    # return the canonical union — hand back a cleared frame so
                    # the emitter's tab unmounts instead of painting the union.
                    fallback = _clear_confrontation_like(payload_model)
                if isinstance(fallback, BaseModel):
                    fallback = fallback.model_copy(update={"seq": seq})
                emitter_msg = message_cls(payload=fallback)
            return emitter_msg

        # C2: event append + all cache writes share a single transaction.
        # Projections are computed inside the block so the cache row's
        # event_seq is the freshly-assigned one. If the server crashes
        # mid-block, sqlite rolls back both the event row and any partial
        # cache rows — either the event is fully persisted with its
        # projection cache, or not at all.
        repo = event_log.repository
        fanout: list[tuple[str, FilterDecision, dict]] = []
        with repo.transaction() as tx:
            row = tx.append_event(kind=kind, payload_json=payload_json)
            seq = row.seq

            if kind == "NARRATION" and event_log is not None:
                # Phase 2: photograph every seated PC's mechanical state
                # while the C2 turn txn is open (R1) so it rides the turn.
                # Gated to the single canonical NARRATION emit so it fires
                # once per turn, not per segment/confrontation frame.
                from sidequest.game.mechanical_census import (
                    emit_mechanical_census,
                )

                emit_mechanical_census(
                    room,
                    handler._session_data.snapshot if handler._session_data else None,
                    tx=tx,
                    event_seq=seq,
                )

            if room is not None and projection_filter is not None:
                from sidequest.server import views

                view = views.build_game_state_view(handler)
                envelope = MessageEnvelope(
                    kind=row.kind,
                    payload_json=row.payload_json,
                    origin_seq=row.seq,
                )
                # Story 159-3: widen owner-private visibility to bonded pets
                # BEFORE projection, so a pet recipient passes the existing
                # CoreInvariantStage gate (the firewall is untouched).
                envelope = expand_visibility_for_companions(envelope, room)
                # G6: status-effect perception overlay. Built once per
                # event (not per recipient) — snapshot statuses don't
                # change mid-fanout.
                status_effects = views.status_effects_by_player(handler)

                # G8: route through the shared write-split helper so the
                # per-peer filter loop is a single code path (test and
                # production exercise `_project_frames`).
                recipients = [
                    pid for pid in room.connected_player_ids() if pid != emitter_player_id
                ]

                def _cache_decision(pid: str, decision: FilterDecision) -> None:
                    if handler._projection_cache is not None:
                        tx.write_projection(
                            event_seq=seq,
                            player_id=pid,
                            decision=decision,
                        )

                decisions = _project_frames(
                    envelope=envelope,
                    projection_filter=projection_filter,
                    connected_players=recipients,
                    view=view,
                    on_decision=_cache_decision,
                    tx=tx,
                    event_seq=seq,
                )
                # Story 49-8: per-recipient POV swap snapshot for the
                # emitter path below. Captured here so the emitter and
                # peer paths share one view/snapshot binding.
                _snapshot_for_swap = (
                    handler._session_data.snapshot if handler._session_data else None
                )

                for other_pid, decision in decisions:
                    filtered_data: dict = {}
                    if decision.include:
                        filtered_data = json.loads(decision.payload_json)
                        # G6: PerceptionRewriter — strip spans whose kind
                        # is incompatible with the recipient's effective
                        # fidelity (base fidelity + status effects like
                        # blinded/deafened). Runs on the already-filtered
                        # payload, before WS send. Deterministic only;
                        # LLM re-voicing is deferred to post-MP.
                        filtered_data = rewrite_for_recipient(
                            canonical_payload=filtered_data,
                            viewer_player_id=other_pid,
                            status_effects=status_effects,
                        )
                        # Story 49-8 / 158-8: 2nd-person POV swap, re-anchored
                        # PER RECIPIENT. On each recipient's frame their OWN PC
                        # becomes "you" (with gendered-pronoun agreement) when
                        # pov_strategy=="pc_anchored"; every other PC stays a
                        # name. No-op for atmospheric narration, for a recipient
                        # whose PC is absent from the prose, and (158-8 rework)
                        # for a recipient with non-canonical/freeform pronouns
                        # (fails open to canonical prose + a skip span).
                        if _snapshot_for_swap is not None:
                            filtered_data = _apply_pov_swap(
                                filtered_data,
                                recipient_player_id=other_pid,
                                view=view,
                                snapshot=_snapshot_for_swap,
                            )
                    fanout.append((other_pid, decision, filtered_data))

                # ADR-105 Track A: project the merged-MP driver too.
                # Invariant 3's raw bypass is a solo assumption — in a
                # shared merged turn the driving (last-submitter) handler
                # is not the sole author, so the driver gets their own
                # per-recipient projected + perception-rewritten + POV-
                # swapped frame, plus a projection.filter.decide span and
                # a cache row in THIS same transaction (so reconnect
                # replays from cache consistently with peers rather than
                # depending on lazy_fill). visible_to:"all" today means
                # include=True for everyone — content redaction of the
                # shared blob is ADR-105 Track B, not this change.
                if project_emitter and emitter_player_id is not None:
                    _e_decision = projection_filter.project(
                        envelope=envelope,
                        view=view,
                        player_id=emitter_player_id,
                        tx=tx,
                        event_seq=seq,
                    )
                    _cache_decision(emitter_player_id, _e_decision)
                    if _e_decision.include:
                        _e_data = json.loads(_e_decision.payload_json)
                        _e_data = rewrite_for_recipient(
                            canonical_payload=_e_data,
                            viewer_player_id=emitter_player_id,
                            status_effects=status_effects,
                        )
                        if _snapshot_for_swap is not None:
                            _e_data = _apply_pov_swap(
                                _e_data,
                                recipient_player_id=emitter_player_id,
                                view=view,
                                snapshot=_snapshot_for_swap,
                            )
                        emitter_projected_dict = _e_data
                        # include=False under project_emitter (a participant
                        # excluded from their own shared narration) is a
                        # Track B concern; leaving emitter_projected_dict None
                        # falls through to the existing path so a frame is
                        # still returned rather than silently emitting empty.

        # Build emitter's message. Solo/legacy: raw, unfiltered payload +
        # seq (Invariant 3 — visibility filter bypassed for the emitter).
        # Merged-MP (ADR-105 Track A): the projected driver frame built
        # above is used instead of the raw bypass.
        #
        # Story 49-8 amendment: when the emitter is the POV anchor of
        # their own narration card, the emitter's frame is rewritten to
        # 2nd-person so their tab reads "You plant a boot..." instead
        # of "Carl plants a boot...". Other field-level filtering
        # remains bypassed (Invariant 3 still holds for non-POV fields);
        # only the prose surface is rewritten to match perspective.
        emitter_payload: object
        swap_eligible = (
            room is not None
            and projection_filter is not None
            and emitter_player_id is not None
            and _snapshot_for_swap is not None
        )
        if emitter_projected_dict is not None:
            # ADR-105 Track A — merged-MP driver receives the projected
            # frame (projection + perception_rewrite + POV swap), NOT the
            # solo Invariant-3 raw bypass. C3 rule applies: rebuild from
            # the filtered dict alone (+ seq) so no canonical field a
            # future (Track B) rule drops can leak back via model merge.
            # ADR-105 / Story 71-13: strip _visibility from the driver
            # frame too — consistent with the universal egress-strip in
            # _deliver_fanout for peers.
            if isinstance(payload_model, BaseModel):
                payload_cls_emitter = type(payload_model)
                emitter_projected_dict.pop("_visibility", None)
                emitter_payload = payload_cls_emitter.model_validate(
                    {**emitter_projected_dict, "seq": seq}
                )
            else:
                emitter_payload = {**emitter_projected_dict, "seq": seq}
        elif swap_eligible and isinstance(payload_model, BaseModel):
            raw_dict = json.loads(payload_model.model_dump_json(exclude={"seq"}))
            swapped_dict = _apply_pov_swap(
                raw_dict,
                recipient_player_id=emitter_player_id,
                view=view,
                snapshot=_snapshot_for_swap,
            )
            if swapped_dict is raw_dict:
                # No swap applied — preserve the existing model_copy
                # path so non-narration payloads (which carry richer
                # Pydantic-only state) round-trip without serialization.
                emitter_payload = payload_model.model_copy(update={"seq": seq})
            else:
                payload_cls_emitter = type(payload_model)
                emitter_payload = payload_cls_emitter.model_validate({**swapped_dict, "seq": seq})
        elif swap_eligible and isinstance(payload_model, dict):
            # Dict payload (test fixtures + legacy raw-dict callers). The
            # swap operates on dicts directly, so just apply and return
            # the message constructed from the swapped dict.
            swapped_dict = _apply_pov_swap(
                payload_model,
                recipient_player_id=emitter_player_id,
                view=view,
                snapshot=_snapshot_for_swap,
            )
            emitter_payload = swapped_dict
        elif isinstance(payload_model, BaseModel):
            emitter_payload = payload_model.model_copy(update={"seq": seq})
        else:
            emitter_payload = payload_model  # type: ignore[assignment]
        out_to_self = message_cls(payload=emitter_payload)

        # Socket fan-out happens AFTER the DB transaction commits. A
        # crash between commit and send is recoverable via the cache on
        # reconnect; sending before commit would risk a client observing
        # an event that never hit disk.
        if room is not None:
            payload_cls = type(payload_model) if isinstance(payload_model, BaseModel) else None
            _deliver_fanout(
                room,
                fanout,
                message_cls=message_cls,
                payload_cls=payload_cls,
                kind=kind,
                seq=seq,
            )
    else:
        # Legacy path (non-slug connect): no EventLog, no seq. Story 59-16:
        # honor a per-recipient supplier for the emitter so stub-room / legacy
        # callers still return a class-filtered frame (never the union) for
        # their outbound list.
        if per_recipient_payload is not None:
            _legacy_emitter = handler._session_data.player_id if handler._session_data else None
            _legacy_payload = per_recipient_payload(_legacy_emitter) if _legacy_emitter else None
            # Solo / non-slug-connect path: a SINGLE socket, so there is no
            # cross-player leak — when the emitter doesn't resolve to a seated PC
            # (supplier → None) the player still sees their full confrontation via
            # the canonical payload. The clear-frame fallback is reserved for the
            # multiplayer per-recipient branch above, where the union must never
            # reach a peer socket. (Story 59-20: the firewall is per-socket, not
            # here.)
            out_to_self = message_cls(
                payload=_legacy_payload if _legacy_payload is not None else payload_model
            )
        else:
            out_to_self = message_cls(payload=payload_model)

    return out_to_self


def _world_portrait_slugs(pack: Any, world_slug: str | None) -> frozenset[str]:
    """The set of portrait-manifest slugs for ``world_slug`` in ``pack``.

    Slugs are derived with ``slugify_player_name`` — the exact mirror of the
    daemon's ``CharacterCatalog._slugify_name`` that the render script
    (``scripts/generate_portrait_images._slugify_name``) uses to name the
    on-disk ``<slug>.png``. Output equality on the same input is the
    load-bearing contract: a mismatched slug means the resolved URL 404s.

    Returns an empty set when the pack/world is unbound or has no manifest —
    the caller treats that as "no portrait" (an observable not-found), never
    a crash.
    """
    from sidequest.server.utils import slugify_player_name

    if pack is None or not world_slug:
        return frozenset()
    world = pack.worlds.get(world_slug)
    if world is None:
        return frozenset()
    manifest = getattr(world, "portrait_manifest", None) or []
    slugs: set[str] = set()
    for entry in manifest:
        name = getattr(entry, "name", "") or ""
        if name:
            slugs.add(slugify_player_name(name))
    return frozenset(slugs)


def _resolve_npc_portrait_url(
    *,
    pack: Any,
    genre_slug: str,
    world_slug: str | None,
    npc_name: str,
    manifest_slugs: frozenset[str] | None = None,
) -> str | None:
    """World-scoped portrait URL for an invoked NPC, or ``None`` (Story 65-6).

    Attaches a portrait IFF ``npc_name`` slugifies to a slug present in the
    current world's ``portrait_manifest``. The URL points at the world-scoped
    asset path that the render script writes
    (``genre_packs/<g>/worlds/<w>/assets/portraits/<slug>.png``), so URL ==
    filename by construction. Both outcomes emit an OTEL span so the GM/dev
    panel can confirm the lookup ran — a should-resolve-but-didn't is the
    classic slug skew, and the not-found span proves the lookup happened
    rather than being silently skipped (CLAUDE.md OTEL principle).

    ``manifest_slugs`` may be precomputed by the caller to avoid rebuilding
    the set per NPC in a multi-NPC turn; when omitted it is derived here.
    """
    from sidequest.foundation.asset_urls import resolve_asset_url
    from sidequest.server.utils import slugify_player_name
    from sidequest.telemetry.spans.scrapbook import (
        scrapbook_npc_portrait_not_found_span,
        scrapbook_npc_portrait_resolved_span,
    )

    slug = slugify_player_name(npc_name)
    slugs = (
        manifest_slugs if manifest_slugs is not None else _world_portrait_slugs(pack, world_slug)
    )
    world_for_span = world_slug or ""

    if slug and slug in slugs:
        url = resolve_asset_url(
            f"genre_packs/{genre_slug}/worlds/{world_slug}/assets/portraits/{slug}.png"
        )
        with scrapbook_npc_portrait_resolved_span(
            npc_name=npc_name, genre=genre_slug, world=world_for_span, slug=slug
        ):
            pass
        return url

    with scrapbook_npc_portrait_not_found_span(
        npc_name=npc_name, genre=genre_slug, world=world_for_span, slug=slug
    ):
        pass
    return None


def emit_scrapbook_entry(
    handler: WebSocketSessionHandler,
    *,
    sd: _SessionData,
    snapshot,  # GameSnapshot — avoid circular import in TYPE_CHECKING
    result: object,
    render_status: str = "rendered",
) -> None:
    """Persist a scrapbook row + emit a SCRAPBOOK_ENTRY event for one turn.

    Called immediately after the NARRATION emit so the entry's seq lands
    adjacent to its narration in the journal. The IMAGE that may follow
    from the daemon is async — its URL arrives later and the UI gallery
    merges by ``turn_id``. We never block on the daemon here.

    Pure reuse: location from snapshot, excerpt from the narrator's prose,
    NPCs from the orchestrator's structured extraction. No new LLM calls.

    ``render_status`` carries the unified Story 45-30 + Story 45-31
    discriminator: ``"rendered"`` (happy path), ``"skipped_policy"``
    (45-30 — trigger policy returned NONE_POLICY for banter / no
    narrative weight), ``"failed"`` (daemon refused synchronously),
    ``"unavailable"`` (45-31 — daemon-state mirror reported UNRESPONSIVE
    before this emit fired so the dispatcher will skip the round-trip).
    The discriminator lands on the SCRAPBOOK_ENTRY event on first emit
    so clients see the right badge live and replay rebuilds it from
    the event payload — no separate row, no JOIN.
    """
    from sidequest.agents.orchestrator import NarrationTurnResult
    from sidequest.protocol.messages import ScrapbookEntryNpcRef, ScrapbookEntryPayload
    from sidequest.server.session_handler import (
        _resolve_location_display,
        _watcher_publish,
        logger,
    )

    if not isinstance(result, NarrationTurnResult):
        return

    narration_text = (result.narration or "").strip()
    if not narration_text:
        # The UI requires a non-empty excerpt; skip cleanly when the turn
        # produced no prose (only happens in degraded edge cases).
        return

    # UI contract: ``location`` must be non-empty. Wave 2B (story 45-48):
    # the scrapbook entry is a party-frame artifact, so use the consensus
    # accessor — solo always returns the only PC's location; MP returns
    # the shared location when seated PCs agree, None when split. Fall
    # back to "Unknown" rather than silently drop the entry.
    raw_location = snapshot.party_location()
    loc_display = _resolve_location_display(sd.genre_pack, sd.world_slug, raw_location) or (
        raw_location or "Unknown"
    )

    # Trim the excerpt to a reasonable length for caption rendering. The
    # narrator's full prose lives on the NarrationMessage; the scrapbook
    # caption is a short teaser.
    excerpt = narration_text
    if len(excerpt) > 320:
        excerpt = excerpt[:317].rstrip() + "..."

    # NPCs from the orchestrator's structured extraction — no new
    # inference. ``role`` is the side flag (player/opponent/neutral);
    # ``disposition`` falls back to role when no behavioral string was
    # extracted.
    # Story 65-6: precompute the world's portrait-manifest slug set once per
    # turn so a multi-NPC turn doesn't rebuild it per ref. The loaded pack +
    # world are reachable from the session data (sd.genre_pack / sd.world_slug)
    # — same accessors the location resolver above uses.
    portrait_slugs = _world_portrait_slugs(sd.genre_pack, sd.world_slug)

    npc_refs: list[ScrapbookEntryNpcRef] = []
    for mention in result.npcs_present or []:
        name = (getattr(mention, "name", "") or "").strip()
        if not name:
            continue
        role = getattr(mention, "side", "") or "neutral"
        disposition = getattr(mention, "role", "") or role
        # Attach a world-scoped portrait IFF the invoked NPC is in the
        # manifest. Resolves to None (an observable not-found span) otherwise.
        portrait_url = _resolve_npc_portrait_url(
            pack=sd.genre_pack,
            genre_slug=sd.genre_slug,
            world_slug=sd.world_slug,
            npc_name=name,
            manifest_slugs=portrait_slugs,
        )
        npc_refs.append(
            ScrapbookEntryNpcRef(
                name=name,
                role=role,
                disposition=disposition,
                portrait_url=portrait_url,
            )
        )

    # World facts: lift the narrator's footnote summaries when present.
    world_facts: list[str] = []
    for fn in result.footnotes or []:
        if not isinstance(fn, dict):
            continue
        summary = fn.get("summary") or fn.get("text") or ""
        if isinstance(summary, str) and summary.strip():
            world_facts.append(summary.strip())

    scene_type: str | None = None
    scene_title: str | None = None
    visual = getattr(result, "visual_scene", None)
    if visual is not None:
        tier = (getattr(visual, "tier", None) or "").strip()
        scene_type = tier or None
        subject = (getattr(visual, "subject", None) or "").strip()
        if subject:
            scene_title = subject[:120]

    turn_id = int(snapshot.turn_manager.interaction)

    payload = ScrapbookEntryPayload(
        turn_id=turn_id,
        location=loc_display,
        narrative_excerpt=excerpt,
        scene_title=scene_title,
        scene_type=scene_type,
        image_url=None,  # Async — IMAGE frame follows from the daemon
        world_facts=world_facts,
        npcs_present=npc_refs,
        render_status=render_status,
    )

    # Persist to the dedicated scrapbook_entries table — keeps the
    # gallery queryable post-game without walking the events journal.
    try:
        persist_scrapbook_entry(handler, payload)
    except Exception as exc:  # noqa: BLE001 — persistence failure must not block emit
        logger.warning("scrapbook.persist_failed turn=%d error=%s", turn_id, exc)

    # Route through emit_event so the journal gets a row + reconnect
    # replay surfaces prior entries to fresh sockets.
    emit_event(handler, "SCRAPBOOK_ENTRY", payload)

    # OTEL lie-detector: GM panel sees per-turn confirmation that the
    # scrapbook subsystem fired. Without this, regression #2 was
    # invisible for two stories.
    _watcher_publish(
        "state_transition",
        {
            "field": "scrapbook",
            "op": "entry_emitted",
            "turn_id": turn_id,
            "image_url": None,
            "location": loc_display,
            "npc_count": len(npc_refs),
            "world_fact_count": len(world_facts),
            "player_id": sd.player_id,
        },
        component="scrapbook",
    )
