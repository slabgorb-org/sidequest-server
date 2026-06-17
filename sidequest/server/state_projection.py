"""Pure projection of a :class:`GameSnapshot` onto the ``SessionStateView`` shape.

Extracted from the inline body of ``rest.py::debug_state`` (story 124-4) so the
GM-panel State-tab projection is unit-testable against synthetic snapshots — the
fixture-driven pattern this repo prefers over DB-coupled endpoint tests.

The wire shape is defined in ``sidequest-ui/src/types/watcher.ts``
(``SessionStateView`` / ``NpcRegistryEntry`` / ``PlayerStateView`` / ``ItemView`` /
``TropeStateView``). Read-only: this never mutates the snapshot.

Honesty note (story 124-4): the previous inline projection read several fields
by names the models don't have — ``getattr(trope, "trope_id")`` (field is
``id``), ``getattr(trope, "progression")`` (field is ``progress``), and
``getattr(core, "edge").maximum`` (the field is ``core.hp`` / ``HpPool.max``) —
and hard-coded player inventory to empty. With ``extra="ignore"`` those reads
returned defaults *silently*, so the GM panel showed blank trope ids, ``0``
progression, ``0/0`` NPC HP, and empty inventories for every live save. This
module reads the real attributes (No Silent Fallbacks).
"""

from __future__ import annotations

from typing import Any


def _project_inventory_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map raw ``CreatureCore.inventory.items`` dicts onto the ``ItemView`` shape."""
    out: list[dict[str, Any]] = []
    for it in items:
        nw = it.get("narrative_weight")
        out.append(
            {
                "id": it.get("id") or it.get("name") or "",
                "name": it.get("name") or "",
                "description": it.get("description") or "",
                "narrative_weight": float(nw) if nw is not None else 0.0,
                "state": it.get("state") or "",
                "source_turn": int(it.get("source_turn") or 0),
                "tags": list(it.get("tags") or []),
            }
        )
    return out


def _project_npc_registry(snap: Any) -> list[dict[str, Any]]:
    """Project both canonical NPC stores (``snap.npcs`` + ``snap.npc_pool``).

    The panel's JSON field name stays ``npc_registry`` to preserve the wire
    contract (the in-engine ``npc_registry`` field was dropped in story 45-52).
    """
    npc_registry: list[dict[str, Any]] = []
    for npc in snap.npcs:
        core = npc.core
        # HP lives on the ablative HpPool at ``core.hp`` (``current`` / ``max``),
        # not the long-removed ``core.edge``.
        hp_pool = getattr(core, "hp", None)
        hp_current = (
            int(hp_pool.current) if hp_pool is not None and hp_pool.current is not None else 0
        )
        hp_max = int(hp_pool.max) if hp_pool is not None and hp_pool.max is not None else 0
        npc_registry.append(
            {
                "name": core.name or "",
                "pronouns": npc.pronouns or "",
                "role": npc.npc_role_id or "",
                "location": npc.last_seen_location or npc.location or "",
                "last_seen_turn": npc.last_seen_turn or 0,
                "age": npc.age or "",
                "appearance": npc.appearance or "",
                # ocean_summary is intentionally unused on the wire — the UI
                # renders the OCEAN sparkline from the `ocean` dict directly.
                "ocean_summary": None,
                "ocean": npc.ocean,
                "hp": hp_current,
                "max_hp": hp_max,
            }
        )
    for member in snap.npc_pool:
        # Pool members are identity-only — no HP, last_seen, or personality.
        npc_registry.append(
            {
                "name": member.name or "",
                "pronouns": member.pronouns or "",
                "role": member.role or "",
                "location": "",
                "last_seen_turn": 0,
                "age": "",
                "appearance": member.appearance or "",
                "ocean_summary": None,
                "ocean": None,
                "hp": 0,
                "max_hp": 0,
            }
        )
    return npc_registry


def _project_trope_states(snap: Any) -> list[dict[str, Any]]:
    """Project ``snap.active_tropes`` (``TropeState`` → ``TropeStateView``).

    ``trope_definition_id`` comes from ``TropeState.id`` and ``progression`` from
    ``TropeState.progress`` (a float conventionally in ``[0, 1]`` — clamped by the
    tick engine, not by the Pydantic field) — NOT the absent ``trope_id`` /
    ``progression`` names, and with no ``int()`` cast that would truncate a
    fractional progression to ``0``.
    """
    trope_states: list[dict[str, Any]] = []
    for trope in snap.active_tropes:
        trope_states.append(
            {
                "trope_definition_id": getattr(trope, "id", "") or "",
                "status": str(getattr(trope, "status", "")),
                "progression": float(getattr(trope, "progress", 0.0)),
            }
        )
    return trope_states


def _project_players(snap: Any) -> list[dict[str, Any]]:
    players: list[dict[str, Any]] = []
    for char in snap.characters:
        # Character.name / level / hp / max_hp are Combatant-equivalent methods
        # (Rust port); getattr returns the bound method — call it.
        name_attr = getattr(char, "name", None)
        level_attr = getattr(char, "level", 1)
        hp_attr = getattr(char, "hp", None)
        max_hp_attr = getattr(char, "max_hp", None)
        resolved_name = name_attr() if callable(name_attr) else name_attr
        resolved_level = level_attr() if callable(level_attr) else level_attr
        resolved_hp = hp_attr() if callable(hp_attr) else hp_attr
        resolved_max_hp = max_hp_attr() if callable(max_hp_attr) else max_hp_attr

        core = getattr(char, "core", None)
        inv = getattr(core, "inventory", None) if core is not None else None
        items = _project_inventory_items(list(inv.items)) if inv is not None else []
        gold = int(getattr(inv, "gold", 0) or 0) if inv is not None else 0

        players.append(
            {
                "player_name": getattr(char, "player_name", "") or "",
                "character_name": resolved_name,
                "character_class": getattr(char, "archetype", "") or "",
                "character_hp": int(resolved_hp) if resolved_hp is not None else 0,
                "character_max_hp": int(resolved_max_hp) if resolved_max_hp is not None else 0,
                "character_level": int(resolved_level or 1),
                "character_xp": int(getattr(char, "xp", 0) or 0),
                "region_id": snap.current_region or "",
                "display_location": (snap.character_locations.get(resolved_name) or ""),
                "inventory": {"items": items, "gold": gold},
            }
        )
    return players


def project_session_state_view(
    snap: Any,
    *,
    session_key: str,
    last_activity_ts: int = 0,
) -> dict[str, Any]:
    """Project one loaded :class:`GameSnapshot` onto a ``SessionStateView`` dict.

    Pure and read-only — all DB/IO (save enumeration, snapshot load) stays in the
    caller (``rest.py::debug_state``).
    """
    return {
        "session_key": session_key,
        "genre_slug": snap.genre_slug or "",
        "world_slug": snap.world_slug or "",
        "current_location": snap.party_location() or "",
        "discovered_regions": list(snap.discovered_regions),
        "narration_history_len": len(snap.narrative_log),
        "turn_mode": str(snap.turn_manager.phase),
        "npc_registry": _project_npc_registry(snap),
        "trope_states": _project_trope_states(snap),
        "players": _project_players(snap),
        "player_count": len(snap.characters),
        "has_music_director": False,
        "has_audio_mixer": False,
        "region_names": [],
        "last_activity_ts": int(last_activity_ts or 0),
    }
