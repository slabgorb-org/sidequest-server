"""Phase 2 forensics: pure projections of canonical mechanical state.

Every accessor here was confirmed from source 2026-05-18 (see the plan's
Spec Reconciliation table R3-R9). These functions are PURE (no I/O, never
raise) EXCEPT emit_mechanical_census, which calls publish_event and is
fully wrapped so telemetry never crashes a turn (Phase 1 contract).
"""

from __future__ import annotations

import hashlib
import json
import logging

logger = logging.getLogger(__name__)


def inventory_digest(items: list) -> list[dict]:
    """Fold raw inventory entries into [{item, qty}], aggregated by name.

    R7: items is list[dict]; narrator dups arrive as quantity:1 singletons,
    so we MUST sum by name, not trust per-entry quantity. Nameless entries
    are loud-skipped (never silently dropped). Output is name-sorted for a
    stable diff key."""
    agg: dict[str, int] = {}
    for entry in items or []:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not name:
            logger.warning("mechanical_census.inventory_unnamed_entry entry=%r", entry)
            continue
        qty = entry.get("quantity", 1)
        if not isinstance(qty, int):
            qty = 1
        agg[name] = agg.get(name, 0) + qty
    return [{"item": n, "qty": agg[n]} for n in sorted(agg)]


def inv_hash(items: list) -> str:
    """Stable 16-hex digest of the aggregated inventory (cheap diff key)."""
    digest = inventory_digest(items)
    blob = json.dumps(digest, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def seat_index(room, player_id: str) -> int:
    """Best-effort positional seat (R9): index in playing_player_ids().
    Not durable across reconnect; player_id is the real key. Never raises
    — absent / no room -> -1 (honest sentinel, not a silent 0)."""
    try:
        return list(room.playing_player_ids()).index(player_id)
    except (AttributeError, ValueError, TypeError):
        return -1


def build_pc_census(
    *,
    character,
    player_id: str,
    character_name: str,
    seat: int,
    round_number: int,
    location,
) -> dict:
    """Project ONE seated PC's canonical mechanical state to a plain dict.

    Reads (confirmed from source): hp = single HpPool (R3, ADR-114); xp/level/
    acquired_advancements from core (R4 — no tier/pending); location string
    + chassis current_room (R6); inventory aggregated digest (R7). Never
    raises on a partial model — missing attrs degrade to honest None/[]."""
    core = character.core
    edge = core.hp
    statuses = [
        {"text": getattr(s, "text", ""), "severity": getattr(s, "severity", "")}
        for s in (getattr(core, "statuses", None) or [])
    ]
    raw_items = getattr(getattr(core, "inventory", None), "items", None) or []
    return {
        "player_id": player_id,
        "character_name": character_name,
        "seat": seat,
        "round": round_number,
        "interaction": round_number,
        "location": location,
        "chassis_room": getattr(character, "current_room", None),
        "edge": {
            "current": getattr(edge, "current", None),
            "max": getattr(edge, "max", None),
            "base_max": getattr(edge, "base_max", None),
        },
        "down": bool(character.is_broken()),
        "statuses": statuses,
        "inventory": inventory_digest(raw_items),
        "inv_hash": inv_hash(raw_items),
        "gold": getattr(getattr(core, "inventory", None), "gold", 0),
        "xp": getattr(core, "xp", 0),
        "level": getattr(core, "level", 1),
        "acquired_advancements": list(getattr(core, "acquired_advancements", None) or []),
        "ability_count": len(getattr(character, "abilities", None) or []),
    }


def build_trope_census(snapshot, round_number: int) -> dict:
    """Project SESSION-level trope state once per round (R5: tropes have
    no PC key; tension is not in the save and is excluded)."""
    tropes = []
    for t in getattr(snapshot, "active_tropes", None) or []:
        tropes.append(
            {
                "id": getattr(t, "id", ""),
                "status": getattr(t, "status", "dormant"),
                "progress": getattr(t, "progress", 0.0),
                "beats_fired": getattr(t, "beats_fired", 0),
                "last_fired_turn": getattr(t, "last_fired_turn", None),
            }
        )
    return {
        "round": round_number,
        "interaction": round_number,
        "active_tropes": tropes,
        "turns_since_meaningful": getattr(snapshot, "turns_since_meaningful", None),
        "total_beats_fired": getattr(snapshot, "total_beats_fired", None),
    }


def emit_mechanical_census(room, snapshot) -> None:
    """Emit one component='mechanical' census per SEATED PC + one session
    trope_census, via Phase 1's publish_event sink. MUST be called from
    inside emit_event's open C2 `with conn:` block (R1) so each row rides
    the turn txn (event_seq attributed, atomic with events).

    Sealed rounds (ADR-036): every seated PC every round, keyed by
    player_id, no acting-player concept. Fully wrapped: ANY failure
    loud-logs and returns — telemetry never crashes a turn. Per-PC build
    failure is isolated (one bad PC never drops the others or the trope
    row). The census fields NEVER set field='encounter' (so the adjacent
    _maybe_persist_encounter_row hazard cannot fire on a census)."""
    # Imported here (not module top) to avoid a telemetry<->game import
    # cycle; publish_event is the Phase-1 sink entrypoint.
    from sidequest.telemetry.watcher_hub import publish_event

    try:
        round_number = int(getattr(getattr(snapshot, "turn_manager", None), "interaction", 0))
    except (TypeError, ValueError):
        round_number = 0
    try:
        player_seats = dict(getattr(snapshot, "player_seats", None) or {})
        by_name = {
            getattr(getattr(c, "core", None), "name", None): c
            for c in (getattr(snapshot, "characters", None) or [])
            if getattr(c, "core", None) is not None
        }
        locations = dict(getattr(snapshot, "character_locations", None) or {})
        seated = list(room.playing_player_ids()) if room is not None else []
    except Exception:  # noqa: BLE001 — telemetry must never crash a turn
        logger.warning("mechanical_census.roster_resolution_failed", exc_info=True)
        return

    if not seated:
        # No seated players → nothing to photograph; trope census is also
        # skipped (no active turn, no recipients to correlate it with).
        return

    for pid in seated:
        name = player_seats.get(pid)
        character = by_name.get(name)
        if character is None:
            # seated but no committed PC yet (CHARGEN) -> honest skip,
            # not a zeroed/fabricated body (No-Silent-Fallback).
            continue
        try:
            census = build_pc_census(
                character=character,
                player_id=pid,
                character_name=name,
                seat=seat_index(room, pid),
                round_number=round_number,
                location=locations.get(name),
            )
            publish_event("census", census, component="mechanical")
        except Exception:  # noqa: BLE001 — isolate one PC's failure
            logger.warning("mechanical_census.build_failed pc=%s", pid, exc_info=True)
            continue

    try:
        trope = build_trope_census(snapshot, round_number)
        publish_event("trope_census", trope, component="mechanical")
    except Exception:  # noqa: BLE001
        logger.warning("mechanical_census.trope_build_failed", exc_info=True)
