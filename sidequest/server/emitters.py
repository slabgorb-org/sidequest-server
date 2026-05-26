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
from typing import TYPE_CHECKING

from sidequest.agents.perception_rewriter import rewrite_for_recipient
from sidequest.agents.pov_swap import swap_to_second_person
from sidequest.game.persistence import SAVE_WRITE_LOCK

if TYPE_CHECKING:
    from sidequest.game.projection.view import SessionGameStateView
    from sidequest.game.session import GameSnapshot
    from sidequest.protocol.messages import ScrapbookEntryPayload
    from sidequest.server.session_handler import WebSocketSessionHandler, _SessionData

logger = logging.getLogger(__name__)


def persist_scrapbook_entry(
    handler: WebSocketSessionHandler,
    payload: ScrapbookEntryPayload,
) -> None:
    """Insert a scrapbook row into the dedicated table (schema in
    ``game/persistence.py``). The table allows multiple rows per turn —
    no UNIQUE on turn_id.
    """
    import json as _json

    if handler._event_log is None:
        return  # Legacy non-slug path — no DB to write to
    store = handler._event_log.store
    npcs_json = _json.dumps(
        [
            {"name": ref.name, "role": ref.role, "disposition": ref.disposition}
            for ref in payload.npcs_present
        ]
    )
    facts_json = _json.dumps(list(payload.world_facts))
    with SAVE_WRITE_LOCK, store._conn:
        store._conn.execute(
            "INSERT INTO scrapbook_entries "
            "(turn_id, scene_title, scene_type, location, image_url, "
            " narrative_excerpt, world_facts, npcs_present, render_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                payload.turn_id,
                payload.scene_title,
                payload.scene_type,
                payload.location,
                payload.image_url,
                payload.narrative_excerpt,
                facts_json,
                npcs_json,
                payload.render_status,
            ),
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
    store = handler._event_log.store
    try:
        with SAVE_WRITE_LOCK, store._conn:
            cur = store._conn.execute(
                "UPDATE scrapbook_entries SET image_url = ? "
                "WHERE rowid = ("
                "  SELECT rowid FROM scrapbook_entries "
                "  WHERE turn_id = ? AND image_url IS NULL "
                "  ORDER BY rowid DESC LIMIT 1"
                ")",
                (image_url, turn_id),
            )
            return cur.rowcount > 0
    except Exception as exc:  # noqa: BLE001 — render path must not crash on a backfill miss
        logger.warning(
            "scrapbook.image_url_update_failed turn_id=%d error=%s",
            turn_id,
            exc,
        )
        return False


def _pronouns_for_pc(snapshot: GameSnapshot, pc_name: str) -> str:
    """Return the pronouns string for a PC by name, or empty if not found.

    Story 49-8: drives 2nd-person POV swap for the anchor recipient.
    """
    for c in snapshot.characters:
        if c.core.name == pc_name:
            return c.pronouns or ""
    return ""


def _apply_pov_swap(
    payload_dict: dict,
    *,
    recipient_player_id: str,
    view: SessionGameStateView,
    snapshot: GameSnapshot,
) -> dict:
    """If ``recipient_player_id`` corresponds to the POV anchor in the
    payload's ``_visibility`` sidecar, return a copy of the payload with
    the ``text`` field rewritten in 2nd-person. Otherwise return the
    payload unchanged.

    Story 49-8 — applies only to payloads carrying a pc-anchored
    visibility sidecar. NPCs and atmospheric narration leave prose alone.
    """
    viz = payload_dict.get("_visibility") or {}
    anchor_pc = viz.get("anchor_pc")
    pov_strategy = viz.get("pov_strategy")
    if not anchor_pc or pov_strategy != "pc_anchored":
        return payload_dict
    recipient_pc_name = view.character_of(recipient_player_id)
    if recipient_pc_name is None or recipient_pc_name != anchor_pc:
        return payload_dict
    pronouns = _pronouns_for_pc(snapshot, recipient_pc_name)
    if not pronouns:
        # Cannot safely swap without pronouns — return canonical prose.
        # Genre-side chargen should always populate pronouns; this is a
        # defensive guard against a malformed save.
        return payload_dict
    text = payload_dict.get("text", "")
    if not isinstance(text, str) or not text:
        return payload_dict
    swapped, _ = swap_to_second_person(
        text,
        target_name=anchor_pc,
        pronouns=pronouns,
    )
    return {**payload_dict, "text": swapped}


def emit_event(
    handler: WebSocketSessionHandler,
    kind: str,
    payload_model: object,
    *,
    author_player_id: str | None = None,
) -> object:
    """Persist an event to the EventLog and fan-out to all connected players.

    Invariants (per Plan 03):
    1. EventLog.append fires BEFORE any socket send.
    2. Fan-out consults ProjectionFilter per recipient.
    3. (solo only) The emitter receives the raw, unfiltered event.

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
    from sidequest.game.projection_filter import FilterDecision
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
                )

            if room is not None and projection_filter is not None:
                from sidequest.server import views

                view = views.build_game_state_view(handler)
                envelope = MessageEnvelope(
                    kind=row.kind,
                    payload_json=row.payload_json,
                    origin_seq=row.seq,
                )
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
                        # Story 49-8: 2nd-person POV swap. Fires only
                        # when the recipient's PC matches the sidecar's
                        # anchor_pc and pov_strategy=="pc_anchored".
                        # No-op for atmospheric / non-anchor recipients.
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
                        envelope=envelope, view=view, player_id=emitter_player_id
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
            if isinstance(payload_model, BaseModel):
                payload_cls_emitter = type(payload_model)
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
            for other_pid, decision, filtered_data in fanout:
                if not decision.include:
                    continue
                socket_id = room.socket_for_player(other_pid)
                if socket_id is None:
                    continue
                queue = room.queue_for_socket(socket_id)
                if queue is None:
                    continue
                try:
                    if payload_cls is not None:
                        # C3: rebuild the recipient payload from the
                        # filtered dict alone (plus seq). Do NOT use
                        # model_copy(update=...) — merging leaves fields
                        # absent from the filtered dict at their canonical
                        # values, which would leak any field a future rule
                        # drops entirely.
                        recipient_payload = payload_cls.model_validate(
                            {**filtered_data, "seq": seq}
                        )
                        recipient_msg = message_cls(payload=recipient_payload)
                    else:
                        recipient_msg = message_cls(payload={**filtered_data, "seq": seq})
                except Exception:
                    # Never silently fail fan-out; log and skip this recipient.
                    logger.error(
                        "emit_event.fanout_failed kind=%s other_pid=%s",
                        kind,
                        other_pid,
                    )
                    continue
                queue.put_nowait(recipient_msg)
    else:
        # Legacy path (non-slug connect): no EventLog, no seq
        out_to_self = message_cls(payload=payload_model)

    return out_to_self


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
    npc_refs: list[ScrapbookEntryNpcRef] = []
    for mention in result.npcs_present or []:
        name = (getattr(mention, "name", "") or "").strip()
        if not name:
            continue
        role = getattr(mention, "side", "") or "neutral"
        disposition = getattr(mention, "role", "") or role
        npc_refs.append(
            ScrapbookEntryNpcRef(
                name=name,
                role=role,
                disposition=disposition,
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


async def broadcast_delta(
    *,
    turn_id: str,
    chunk: str,
    seq: int,
    room: object,
) -> None:
    """Broadcast an ephemeral narration delta to all sockets in the room.

    Does NOT call emit_event(). Does NOT touch the projection cache.
    Does NOT run perception_rewriter. Pure presentation channel.

    Room API used (matching SessionRoom):
      room.connected_player_ids() -> list[str]
      room.socket_for_player(pid) -> str | None
      room.queue_for_socket(socket_id) -> asyncio.Queue | None
      queue.put_nowait(msg)

    Per-socket errors (missing socket_id or queue) are logged and skipped so
    one dead/absent player cannot block fan-out to the rest.

    Implementation note: each player gets the same payload — deltas are not
    per-recipient filtered. This is correct today because the perception
    rewriter is a no-op for narration (no kind-tagged spans yet, see
    perception_rewriter.py docstring re: G10 deferral). When G10 ships,
    this fan-out needs revisiting.
    """
    from sidequest.protocol.messages import NarrationDelta, NarrationDeltaPayload

    msg = NarrationDelta(
        payload=NarrationDeltaPayload(
            turn_id=turn_id,
            chunk=chunk,
            seq=seq,
        )
    )
    for pid in room.connected_player_ids():
        socket_id = room.socket_for_player(pid)
        if socket_id is None:
            logger.warning(
                "broadcast_delta.no_socket turn_id=%s seq=%d player_id=%s",
                turn_id,
                seq,
                pid,
            )
            continue
        queue = room.queue_for_socket(socket_id)
        if queue is None:
            logger.warning(
                "broadcast_delta.no_queue turn_id=%s seq=%d player_id=%s socket_id=%s",
                turn_id,
                seq,
                pid,
                socket_id,
            )
            continue
        try:
            queue.put_nowait(msg)
        except Exception:
            # Per-socket errors must not break fan-out to other recipients.
            logger.warning(
                "broadcast_delta.enqueue_failed turn_id=%s seq=%d player_id=%s",
                turn_id,
                seq,
                pid,
            )
