"""View projection helpers extracted from WebSocketSessionHandler.

Phase 2 of the session_handler.py decomposition (see
docs/superpowers/specs/2026-04-27-session-handler-decomposition-design.md).

Each function takes ``handler: WebSocketSessionHandler`` as its first
argument (or operates on read-only inputs in the case of
``is_hidden_status_list``). No new abstractions introduced — this is pure
extraction with byte-identical behavior to the original methods on
WebSocketSessionHandler.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sidequest.game.status import Status

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sidequest.game.character import Character
    from sidequest.game.projection.view import SessionGameStateView
    from sidequest.genre.models.rules import BeatDef, ConfrontationDef
    from sidequest.protocol.messages import PartyStatusMessage
    from sidequest.protocol.models import ClassMove, PartyMember
    from sidequest.server.session_handler import WebSocketSessionHandler, _SessionData


logger = logging.getLogger(__name__)


_HIDDEN_STATUS_TOKENS: frozenset[str] = frozenset(
    {
        "hidden",
        "invisible",
        "stealth",
        "concealed",
    }
)

_UNIVERSAL_BEATS: frozenset[str] = frozenset({"attack", "defend", "flee"})


def _filter_class_moves(raw: list[str]) -> list[str]:
    """Drop universal beats and any 'auto-filled' scaffolding from a raw
    encounter_beat_choices list before sending to the UI.

    Spec: 2026-05-10 class-mechanical-surface §7.1 — UI receives a clean list.
    """
    return [b for b in raw if b not in _UNIVERSAL_BEATS and "auto-filled" not in b]


def _index_beats(confrontations: list[ConfrontationDef]) -> dict[str, BeatDef]:
    """Flatten every confrontation's beats into an id → BeatDef lookup.

    First definition wins on a duplicate id — the pack loader already
    guarantees beat ids are unique within the selectable pool, so the
    ``setdefault`` is defensive, not load-bearing.
    """
    index: dict[str, BeatDef] = {}
    for conf in confrontations:
        for beat in conf.beats:
            index.setdefault(beat.id, beat)
    return index


def _resolve_class_moves(ids: list[str], beat_index: Mapping[str, BeatDef]) -> list[ClassMove]:
    """Resolve filtered beat ids to player-facing ClassMoves.

    ``description`` prefers the most player-facing text available on the
    BeatDef: ``flavor`` (the BeatTile italic hint) → ``narrator_hint`` →
    ``effect``. An id with no matching BeatDef degrades to ``label == id``
    and is logged loudly (no silent fallback) — this should not happen
    because the loader validates ``encounter_beat_choices`` against the
    beat pool, so it signals a real pack/wiring bug rather than being
    swallowed.
    """
    from sidequest.protocol.models import ClassMove

    moves: list[ClassMove] = []
    for beat_id in ids:
        beat = beat_index.get(beat_id)
        if beat is None:
            logger.warning(
                "views.class_move_unresolved id=%s — beat not found in any "
                "confrontation pool; rendering raw id as label",
                beat_id,
            )
            moves.append(ClassMove(id=beat_id, label=beat_id, description=None))
            continue
        description = beat.flavor or beat.narrator_hint or beat.effect
        moves.append(ClassMove(id=beat_id, label=beat.label, description=description))
    return moves


def is_hidden_status_list(statuses: list[Status]) -> bool:
    """Return True iff any status's lowercased text matches a hidden-marker
    token (whole-token membership, not substring)."""
    return any(s.text.lower() in _HIDDEN_STATUS_TOKENS for s in statuses)


def build_game_state_view(handler: WebSocketSessionHandler) -> SessionGameStateView:
    """Read-only view of current session state for the projection filter.

    Zone + visibility state is populated from the live ``GameSnapshot``:
    all player-characters share the party-level ``snapshot.location``,
    and NPCs report their per-entity ``Npc.location``. Creatures whose
    ``statuses`` contain a stealth-like marker go into
    ``hidden_characters`` so ``visible_to()`` masks them even when
    co-located with the viewer. Per-item ownership is not yet tracked
    and stays at the conservative default.

    **GM identity wiring (C1, still partial):**

    - Solo sessions have no separate GM player by design; ``gm_player_id``
      is correctly ``None`` there. ``CoreInvariantStage`` never
      short-circuits on ``is_gm()`` for solo — which is the right
      behavior, because in solo play the single player is the only
      recipient and has no counterpart to be "GM" to.
    - Multiplayer sessions *should* name a GM player (e.g. the session
      creator or a designated seat) so that ``unless: is_gm()`` in
      ``projection.yaml`` can exempt them. That wiring lives downstream
      of MP-02 seating — ``SessionRoom`` does not yet carry a GM seat
      designation, so we still fall through to ``None`` for multiplayer
      with a logged warning. Genre packs that ship ``unless: is_gm()``
      rules today will mask the GM identically to a regular player
      (the safe direction: over-redact rather than leak).

    **Player-character mapping:** ``Character`` does not yet carry a
    ``player_id`` attribute, so the session's active player_id
    (``sd.player_id``) is mapped to the first entry in
    ``snapshot.characters`` — the single-player case this branch is
    authoritative for today. MP seat-assignment (sprint 2) will feed
    the multi-player case via ``SessionRoom``. When no character
    exists yet (pre-chargen) the mapping stays empty and predicates
    that depend on ``character_of()`` evaluate to ``False`` (the
    masked direction).
    """
    from sidequest.game.persistence import GameMode  # noqa: PLC0415 — break import cycle
    from sidequest.game.projection.view import SessionGameStateView

    sd = handler._session_data
    if sd is None:
        return SessionGameStateView(gm_player_id=None, player_id_to_character={})

    # Solo: no human GM. None is correct; CoreInvariantStage's
    # gm-sees-all branch never fires for the single player.
    gm_player_id: str | None = None
    if (
        sd.mode is not None
        and sd.mode != GameMode.SOLO
        and not getattr(handler, "_gm_wiring_warned", False)
    ):
        # Multiplayer: GM seat assignment not yet plumbed through
        # SessionRoom. Log one warning per build so GM-panel users
        # can see that ``unless: is_gm()`` rules are currently
        # over-masking the GM in multiplayer sessions.
        logger.warning(
            "projection.gm_identity_unwired slug=%s mode=%s — "
            "multiplayer sessions do not yet carry a GM-seat "
            "designation; `unless: is_gm()` rules will mask the "
            "GM like any other player until MP-02 GM seating "
            "lands.",
            sd.game_slug,
            sd.mode,
        )
        handler._gm_wiring_warned = True

    snapshot = sd.snapshot

    # Player -> Character.name mapping. Solo / single-player sessions
    # today have exactly one character; that character belongs to the
    # session's active player_id. Without this mapping, the predicate
    # path (e.g. ``visible_to(target)``) receives
    # ``view.character_of(player_id) is None`` and short-circuits to
    # False before ever consulting zone data. Populated from the
    # existing session state — no new fields introduced.
    mapping: dict[str, str] = {}
    if snapshot.characters:
        # Solo default: the active player_id owns the first (and usually
        # only) character. Multiplayer overrides this from the room's
        # seat assignments below.
        mapping[sd.player_id] = snapshot.characters[0].core.name

    # Story 49-8: multiplayer mapping. The room tracks
    # ``slot_to_player_id`` for seated peers (PARTY_STATUS already
    # consumes this map in MP). Merging it into ``mapping`` here is the
    # load-bearing wiring that lets the projection filter recognise
    # peer player_ids — without it, ``visible_to(target)`` and the new
    # POV-swap path collapse to "unknown player" for everyone but the
    # emitter.
    room = getattr(handler, "_room", None)
    if room is not None:
        slot_lookup = getattr(room, "slot_to_player_id", None)
        if callable(slot_lookup):
            try:
                slot_map = slot_lookup()
            except Exception:  # noqa: BLE001 — view build must never crash a turn
                slot_map = {}
            # ``slot_to_player_id`` keys by character_slot (the PC name
            # at seat-time). Match against the live snapshot's PC
            # roster so renamed characters (post-chargen edit) don't
            # leak a stale name into the mapping.
            roster_names = {c.core.name for c in snapshot.characters}
            for slot_name, pid in slot_map.items():
                if slot_name in roster_names:
                    mapping[pid] = slot_name

    # Zone + hidden-character tracking from the live snapshot. Wave 2B
    # (story 45-48): per-character zones come from
    # ``snapshot.character_locations[name]``; party-frame fallback uses
    # the consensus accessor (returns None when seated PCs disagree).
    # NPCs carry their own ``location``. Keys are creature names — the
    # same identity the rest of the projection system uses when it
    # refers to characters by ID.
    character_zones: dict[str, str] = {}
    hidden_characters: set[str] = set()
    party_zone = snapshot.party_location()

    # One-shot OTEL breadcrumb: if we have player-characters but no
    # consensus zone, every co-located visible_to() collapses to False.
    # The direction is conservative-correct but invisible to the GM
    # panel — surface it once per session so rule authors can see why
    # their ``visible_to`` rules are masking everything.
    if (
        party_zone is None
        and snapshot.characters
        and not getattr(handler, "_party_zone_absent_warned", False)
    ):
        logger.warning(
            "projection.party_zone_absent_with_characters slug=%s "
            "characters=%d — party_location() returned None (no consensus) "
            "while snapshot.characters is non-empty; visible_to() / "
            "in_same_zone() will mask every co-located target until "
            "the seated PCs agree on a location (typically the first "
            "encounter).",
            sd.game_slug,
            len(snapshot.characters),
        )
        handler._party_zone_absent_warned = True

    for ch in snapshot.characters:
        name = ch.core.name
        # Prefer per-character location; fall back to party consensus
        # only when this PC has no per-character entry. Both can be
        # None — leave the entry absent in that case (callers handle).
        per_char = snapshot.character_locations.get(name)
        zone = per_char if per_char else party_zone
        if zone is not None:
            character_zones[name] = zone
        if is_hidden_status_list(ch.core.statuses):
            hidden_characters.add(name)
    for npc in snapshot.npcs:
        name = npc.core.name
        if npc.location:
            character_zones[name] = npc.location
        if is_hidden_status_list(npc.core.statuses):
            hidden_characters.add(name)

    return SessionGameStateView(
        gm_player_id=gm_player_id,
        player_id_to_character=mapping,
        character_zones=character_zones,
        hidden_characters=hidden_characters,
    )


def status_effects_by_player(handler: WebSocketSessionHandler) -> dict[str, list[str]]:
    """Per-player status-effect tokens, for PerceptionRewriter.

    Reads the *existing* character-status map on the active
    ``GameSnapshot`` — no new state is introduced. Mirrors the
    player->character mapping used by :func:`build_game_state_view`:
    the session's active ``player_id`` is mapped to the first entry
    in ``snapshot.characters`` (single-player authoritative today;
    MP seat-assignment will feed the multi-player case via
    ``SessionRoom`` in a later sprint, at which point this accessor
    should fan out the same way).

    Returns ``dict[player_id, list[status_token]]``. An empty dict
    (no session, no snapshot, no characters) is safe: the rewriter
    treats missing entries as "no status effects".
    """
    sd = handler._session_data
    if sd is None:
        return {}
    snapshot = sd.snapshot
    if not snapshot.characters:
        return {}
    # Mirror build_game_state_view's mapping: active player_id ->
    # first character. Any connected non-active player_id gets []
    # until MP seat-assignment plumbs a real mapping.
    return {sd.player_id: [s.text for s in snapshot.characters[0].core.statuses]}


DEFAULT_TAIL_BACKFILL_LIMIT = 5


def backfill_last_narration_block(
    handler: WebSocketSessionHandler,
    *,
    player_id: str,
    limit: int = DEFAULT_TAIL_BACKFILL_LIMIT,
) -> list[object]:
    """Fetch the last ``limit`` NARRATIONs (plus interleaved CHAPTER_MARKERs
    and the marker that immediately precedes the oldest narration in the
    window) from the event log and re-emit them as cached-projection
    messages — regardless of ``last_seen_seq``.

    Used to paint the narrative pane on a fresh-browser slug-resume
    where the normal replay would otherwise be empty because the
    client's persisted ``last_seen_seq`` already covers the tail.

    Returns the messages in seq-ascending order (chapter markers before
    their narration). Silently returns an empty list when no narration
    has been logged or when the event log/projection cache is
    unavailable. Cache rows that are missing or include=False are skipped
    individually; the rest of the window is still returned. The caller
    is responsible for updating replay telemetry.

    Pingpong 2026-04-30 "Resume narration replay emits only 1 of N":
    raised the cap from 1 narration → ``limit`` so a player who refreshes
    after several turns lands with a coherent scrollback, not just the
    most recent line.
    """
    from sidequest.server.session_handler import _build_message_for_kind

    if handler._event_log is None or handler._projection_cache is None:
        return []
    if limit <= 0:
        return []
    store = handler._event_log.store

    # Find the seq of the oldest narration we want in the window — the
    # Nth-most-recent. Fewer than ``limit`` narrations is fine; we just
    # take what's there.
    with store._conn:
        narration_seq_rows = store._conn.execute(
            "SELECT seq FROM events WHERE kind = 'NARRATION' ORDER BY seq DESC LIMIT ?",
            (limit,),
        ).fetchall()
    if not narration_seq_rows:
        return []
    oldest_narration_seq = int(narration_seq_rows[-1][0])

    # Capture the chapter marker that precedes the oldest narration in
    # our window (without crossing an even-earlier narration), so the
    # first emitted block has its header attached. Subsequent chapters
    # interleaved between narrations are picked up by the range read
    # below.
    with store._conn:
        chapter_row = store._conn.execute(
            "SELECT seq FROM events "
            "WHERE kind = 'CHAPTER_MARKER' AND seq < ? "
            "  AND seq > COALESCE("
            "    (SELECT MAX(seq) FROM events "
            "     WHERE kind = 'NARRATION' AND seq < ?),"
            "    0"
            "  ) "
            "ORDER BY seq DESC LIMIT 1",
            (oldest_narration_seq, oldest_narration_seq),
        ).fetchone()

    lower_bound = oldest_narration_seq
    if chapter_row is not None:
        lower_bound = int(chapter_row[0])

    with store._conn:
        rows = store._conn.execute(
            "SELECT seq, kind FROM events "
            "WHERE kind IN ('NARRATION', 'CHAPTER_MARKER') AND seq >= ? "
            "ORDER BY seq ASC",
            (lower_bound,),
        ).fetchall()

    def _cached_payload(seq: int) -> str | None:
        with store._conn:
            row = store._conn.execute(
                "SELECT include, payload_json FROM projection_cache "
                "WHERE player_id = ? AND event_seq = ?",
                (player_id, seq),
            ).fetchone()
        if row is None or not bool(row[0]) or row[1] is None:
            return None
        return str(row[1])

    messages: list[object] = []
    for seq_raw, kind in rows:
        seq_i = int(seq_raw)
        cached = _cached_payload(seq_i)
        if cached is None:
            continue
        built = _build_message_for_kind(
            kind=str(kind),
            payload_json=cached,
            seq=seq_i,
        )
        if built is None:
            continue
        messages.append(built)
    return messages


def party_member_from_character(
    handler: WebSocketSessionHandler,
    sd: _SessionData,
    character: Character,
    player_id: str,
    player_name: str,
) -> PartyMember:
    """Build a single PartyMember from a Character object.

    Factored out of :func:`build_session_start_party_status` so the
    same construction can run for the requesting socket's PC and for
    peer PCs that landed in the snapshot via multiplayer chargen.
    """
    from sidequest.protocol.models import (
        CharacterSheetDetails,
        InventoryItem,
        InventoryPayload,
        PartyMember,
    )
    from sidequest.protocol.types import NonBlankString
    from sidequest.server.session_helpers import _resolve_location_display

    # Inventory is stored as list[dict] in Phase 1 (creature_core.py:158).
    # Filter to Carried items — identical to Rust's inventory.carried()
    # iterator, which skips Stored/Dropped.
    carried = [
        item
        for item in character.core.inventory.items
        if str(item.get("state", "Carried")) == "Carried"
    ]

    stats = dict(character.stats)
    abilities = list(character.abilities)
    equipment = [
        f"{item['name']} [equipped]" if item.get("equipped") else item["name"] for item in carried
    ]

    # Compute class_moves from genre pack class definition, resolving each
    # beat id to its label + player-readable description (playtest 2026-05-21:
    # the Abilities panel was showing raw snake_case ids).
    class_moves: list[ClassMove] = []
    class_def = next(
        (c for c in sd.genre_pack.classes if c.display_name == character.char_class),
        None,
    )
    if class_def is not None:
        filtered_ids = _filter_class_moves(class_def.encounter_beat_choices)
        class_moves = _resolve_class_moves(
            filtered_ids, _index_beats(sd.genre_pack.rules.confrontations)
        )

    sheet = CharacterSheetDetails(
        race=NonBlankString(character.race),
        stats=stats,
        abilities=abilities,
        class_moves=class_moves,
        backstory=NonBlankString(character.backstory or "(no backstory)"),
        personality=NonBlankString(character.core.personality),
        pronouns=NonBlankString(character.pronouns) if character.pronouns else None,
        equipment=equipment,
    )

    # Currency noun from inventory.yaml::currency.name (pingpong
    # 2026-04-24 fantasy-leak bug). None → UI neutral fallback;
    # no silent default to "gold".
    currency_name: str | None = None
    if sd.genre_pack.inventory is not None and sd.genre_pack.inventory.currency is not None:
        currency_name = sd.genre_pack.inventory.currency.name

    inventory_payload = InventoryPayload(
        items=[
            InventoryItem(
                name=NonBlankString(str(item["name"])),
                # Protocol alias: "type". Dicts carry "category" from
                # the loadout encoder; map and keep a non-blank string.
                **{"type": str(item.get("category", "equipment") or "equipment")},  # type: ignore[arg-type]
                equipped=bool(item.get("equipped", False)),
                quantity=int(item.get("quantity", 1)),
                description=NonBlankString(str(item.get("description") or item["name"])),
            )
            for item in carried
        ],
        gold=character.core.inventory.gold,
        currency_name=currency_name,
    )

    location_nbs: NonBlankString | None = None
    # Wave 2B (story 45-48): per-character location is the only source
    # of truth — the party-level ``snapshot.location`` is gone. When
    # this PC has no per-character entry yet (pre-first-narration), the
    # PARTY_STATUS frame omits the location field; the UI renders
    # "(unknown)" rather than inheriting whichever player narrated most
    # recently.
    raw_location = sd.snapshot.party_location(perspective=character.core.name)
    loc_display = _resolve_location_display(sd.genre_pack, sd.world_slug, raw_location)
    if loc_display:
        try:
            location_nbs = NonBlankString(loc_display)
        except Exception:
            location_nbs = None

    class_nbs = NonBlankString(character.char_class or "Adventurer")
    char_name_nbs = NonBlankString(character.core.name)

    return PartyMember(
        player_id=NonBlankString(player_id or "anon"),
        name=NonBlankString(player_name or "Player"),
        character_name=char_name_nbs,
        current_hp=character.core.edge.current,
        max_hp=character.core.edge.max,
        statuses=[s.text for s in character.core.statuses],
        **{"class": class_nbs},  # type: ignore[arg-type]
        level=character.core.level,
        portrait_url=None,
        current_location=location_nbs,
        sheet=sheet,
        inventory=inventory_payload,
    )


def resolve_self_character(
    handler: WebSocketSessionHandler,
    sd: _SessionData,
) -> Character | None:
    """Find the Character belonging to ``sd.player_id`` in the snapshot.

    Used to disambiguate "which PC is *me*" when the snapshot carries
    multiple PCs (multiplayer). Returning ``snapshot.characters[0]`` is
    wrong for any player whose seat isn't first — that's the playtest
    2026-04-25 "Tab 2 sees Laverne (YOU)" bug. The seat map (written at
    chargen-commit, lines 2440-2475) is the source of truth; the room
    seat is the live runtime mirror used as a fallback.

    Returns ``None`` for legacy saves with no ``player_seats`` binding
    AND no live room seat (very old solo saves). Callers should fall
    back to ``snapshot.characters[0]`` in that case to keep solo
    single-PC sessions working.
    """
    snapshot = sd.snapshot
    if not snapshot.characters:
        return None
    if sd.player_id and snapshot.player_seats:
        char_name = snapshot.player_seats.get(sd.player_id)
        if char_name:
            for c in snapshot.characters:
                if c.core.name == char_name:
                    return c
    if sd.player_id and handler._room is not None:
        seat_lookup = getattr(handler._room, "slot_to_player_id", None)
        if callable(seat_lookup):
            for slot, pid in seat_lookup().items():
                if pid == sd.player_id:
                    for c in snapshot.characters:
                        if c.core.name == slot:
                            return c
    return None


def build_session_start_party_status(
    handler: WebSocketSessionHandler,
    sd: _SessionData,
    character: Character,
    player_id: str,
) -> PartyStatusMessage:
    """PARTY_STATUS frame at chargen end (Rust connect.rs:2533).

    MP: enumerates every PC; maps each slot back to its seating
    player_id via the room. Falls back to ``peer:<name>`` when
    no seat record is available.
    """
    from sidequest.protocol.messages import PartyStatusMessage, PartyStatusPayload

    seat_map: dict[str, str] = {}
    if handler._room is not None:
        seat_lookup = getattr(handler._room, "slot_to_player_id", None)
        if callable(seat_lookup):
            seat_map = seat_lookup()

    members: list[PartyMember] = []
    all_chars = list(sd.snapshot.characters or [])
    if not all_chars:
        all_chars = [character]
    # Stable ordering: self first, then peers in snapshot order.
    self_chars = [c for c in all_chars if c.core.name == character.core.name]
    peer_chars = [c for c in all_chars if c.core.name != character.core.name]
    for char in self_chars + peer_chars:
        is_self = char.core.name == character.core.name
        if is_self:
            pid = player_id or "anon"
            pname = sd.player_name or "Player"
        else:
            pid = seat_map.get(char.core.name) or f"peer:{char.core.name}"
            pname = char.core.name
        members.append(party_member_from_character(handler, sd, char, pid, pname))

    from sidequest.protocol.models import CompanionMember
    from sidequest.protocol.types import NonBlankString

    companions: list[CompanionMember] = []
    for c in sd.snapshot.companions or []:
        try:
            companions.append(
                CompanionMember(
                    name=NonBlankString(c.name),
                    role=c.role or "",
                    description=c.description or "",
                    notes=c.notes or "",
                    recruited_turn=c.recruited_turn,
                    recruited_by=c.recruited_by or "",
                )
            )
        except Exception as exc:  # noqa: BLE001 — never fail PARTY_STATUS on a bad companion
            import logging

            logging.getLogger(__name__).warning(
                "party_status.companion_skipped name=%r error=%s",
                getattr(c, "name", None),
                exc,
            )

    return PartyStatusMessage(
        type="PARTY_STATUS",  # type: ignore[arg-type]
        payload=PartyStatusPayload(members=members, companions=companions),
        player_id=player_id,
    )
