"""Read-old-write-new migration hook for ``GameSnapshot`` JSON.

Runs in ``SqliteStore.load`` BEFORE pydantic validation. Each migration
sub-function takes a snapshot dict, mutates a copy, and returns the
canonical shape. ``migrate_legacy_snapshot`` is the orchestrator — it
records which sub-functions actually rewrote anything and emits a single
``snapshot.canonicalize`` OTEL span with per-field attributes.

The architect's promise (per design 2026-05-04-snapshot-split-brain-cleanup):
this module is the ONLY place backward-compat shims live. When a save
predates a schema change, the shim lives here, not buried in pydantic
validators across the snapshot models. The lie-detector signal is one
span per load; the GM panel can audit which legacy shapes are still in
the wild.
"""

from __future__ import annotations

import copy
from typing import Any

from sidequest.telemetry.spans import SPAN_SNAPSHOT_CANONICALIZE, Span


def _migrate_s1_world_confrontations(out: dict[str, Any]) -> dict[str, Any] | None:
    """S1 — merge ``world_confrontations`` into ``magic_state.confrontations``.

    Dedupe by ``id``; existing ``magic_state.confrontations`` entries win
    on collision (magic_state is the canonical home — see design spec).
    Drops the legacy ``world_confrontations`` field after merge.

    Returns a dict of OTEL attributes when migration occurred, else None.
    """
    if "world_confrontations" not in out:
        return None

    legacy = out.pop("world_confrontations") or []

    if not legacy:
        return {
            "s1_world_confrontations_merged": 0,
            "s1_world_confrontations_dropped_no_target": 0,
        }

    magic_state = out.get("magic_state")
    if not isinstance(magic_state, dict):
        # No magic_state to migrate INTO — drop the entries rather than
        # synthesize a magic config. CLAUDE.md "No Silent Fallbacks": we
        # do not invent canonical state from absent inputs.
        return {
            "s1_world_confrontations_merged": 0,
            "s1_world_confrontations_dropped_no_target": len(legacy),
        }

    existing = magic_state.setdefault("confrontations", [])
    existing_ids = {c.get("id") for c in existing if isinstance(c, dict)}
    merged_count = 0
    for entry in legacy:
        if not isinstance(entry, dict):
            continue
        if entry.get("id") in existing_ids:
            continue  # collision — magic_state's entry wins
        existing.append(entry)
        existing_ids.add(entry.get("id"))
        merged_count += 1

    return {
        "s1_world_confrontations_merged": merged_count,
        "s1_world_confrontations_dropped_no_target": 0,
    }


def _migrate_s2_npc_registry_split(out: dict[str, Any]) -> dict[str, Any] | None:
    """S2 (Wave 2A) — split legacy ``npc_registry`` into ``npc_pool`` +
    ``Npc.last_seen_*``.

    For each entry in legacy ``out["npc_registry"]``:
    - If a matching ``Npc`` (case-folded name) exists in ``out["npcs"]``,
      merge ``last_seen_location`` and ``last_seen_turn`` onto the ``Npc``
      dict and drop the entry. Legacy ``hp/max_hp`` are NOT migrated to
      ``Npc.core.edge`` — the canonical edge pool is already authoritative
      and legacy hp on a matched-Npc entry is redundant.
    - Otherwise, if ``hp`` or ``max_hp`` is set, drop as orphan stat block
      (legacy bug state — combat stats published into the registry without
      a matching Npc; we do not synthesize an Npc from this).
    - Otherwise, emit a ``NpcPoolMember`` dict into ``out["npc_pool"]``
      with ``drawn_from="legacy_registry"`` and ``archetype_id=None``.

    Two silent-skip paths are counted into OTEL attributes (story 45-52,
    Reviewer's silent-failure findings on 45-47):

    - ``s2_malformed_npcs_skipped`` — entries that weren't dicts at all
      (corrupt save / hand-edited JSON). Previously dropped on the floor.
    - ``s2_nameless_entries_dropped`` — entries with an empty/missing name.
      Previously dropped on the floor.

    Drops the ``npc_registry`` field on success. Returns OTEL attributes
    when anything was rewritten, else None.
    """
    if "npc_registry" not in out:
        return None

    legacy = out.get("npc_registry") or []

    # If the registry is empty AND npc_pool already exists (canonical snapshot),
    # don't modify anything — return None (no-op). This prevents spurious
    # migration markers on canonical snapshots that happen to have both fields.
    if not legacy and "npc_pool" in out:
        return None

    # Only pop once we know there's actual work to do
    out.pop("npc_registry")

    # Seed the canonical pool field so the migrated snapshot has the
    # post-Wave-2A shape, even if the legacy registry was empty.
    npcs = out.setdefault("npcs", [])
    pool = out.setdefault("npc_pool", [])

    # If the registry is empty but npc_pool doesn't exist yet (legacy snapshot),
    # return None — no OTEL marker, no backup created. The field is dropped and
    # npc_pool is created.
    if not legacy:
        return None

    by_name: dict[str, dict[str, Any]] = {}
    for npc in npcs:
        if not isinstance(npc, dict):
            continue
        core = npc.get("core")
        if not isinstance(core, dict):
            continue
        name = core.get("name", "")
        if name:
            by_name[name.casefold()] = npc

    pool_added = 0
    last_seen_merged = 0
    orphans_dropped = 0
    malformed_npcs_skipped = 0
    nameless_entries_dropped = 0

    for entry in legacy:
        if not isinstance(entry, dict):
            malformed_npcs_skipped += 1
            continue
        name = entry.get("name", "")
        if not name:
            nameless_entries_dropped += 1
            continue
        match = by_name.get(name.casefold())

        if match is not None:
            # Branch 2: merge last_seen_* onto the existing Npc.
            last_seen_location = entry.get("last_seen_location")
            if last_seen_location is not None:
                match["last_seen_location"] = last_seen_location
            match["last_seen_turn"] = entry.get("last_seen_turn", 0)
            match.setdefault("pool_origin", None)
            last_seen_merged += 1
            continue

        if entry.get("hp") is not None or entry.get("max_hp") is not None:
            # Branch 3: orphan stat block — drop.
            orphans_dropped += 1
            continue

        # Branch 1: emit as pool member.
        pool.append(
            {
                "name": name,
                "role": entry.get("role"),
                "pronouns": entry.get("pronouns"),
                "appearance": entry.get("appearance"),
                "archetype_id": None,
                "drawn_from": "legacy_registry",
            }
        )
        pool_added += 1

    return {
        "s2_pool_added": pool_added,
        "s2_last_seen_merged": last_seen_merged,
        "s2_orphans_dropped": orphans_dropped,
        "s2_malformed_npcs_skipped": malformed_npcs_skipped,
        "s2_nameless_entries_dropped": nameless_entries_dropped,
    }


def _migrate_s3_party_location(out: dict[str, Any]) -> dict[str, Any] | None:
    """S3 (Wave 2B) — promote legacy ``location`` into ``character_locations``.

    Pre-Wave-2B, the party-level ``snapshot.location`` was the fallback for
    any character without a ``character_locations`` entry. Wave 2B removes
    the field; the migration must seed seated PCs from the legacy value
    BEFORE pydantic drops the unknown field (model config is
    ``extra: ignore``).

    Behaviour:
    - If the legacy ``location`` key is absent: no-op (canonical save).
    - If ``location`` is empty / falsy: drop the key, no seed (no silent
      fallback into empty strings).
    - Otherwise, for each seated PC (``player_seats.values()``) WITHOUT
      an existing ``character_locations`` entry, seed their entry with the
      legacy value. Existing entries are newer truth and untouched.

    Drops the legacy ``location`` field on success. Returns OTEL attributes
    when any seat was seeded, else None (silent on canonical input).
    """
    if "location" not in out:
        return None

    legacy_location = out.pop("location")

    if not legacy_location:
        return None

    seats = out.get("player_seats") or {}
    if not isinstance(seats, dict) or not seats:
        return None

    char_locations = out.setdefault("character_locations", {})
    if not isinstance(char_locations, dict):
        return None

    seeded = 0
    for character_name in seats.values():
        if not character_name:
            continue
        if character_name in char_locations:
            continue
        char_locations[character_name] = legacy_location
        seeded += 1

    if seeded == 0:
        return None

    return {"s3_party_location_seeded": seeded}


def _migrate_s4_pc_regions(out: dict[str, Any]) -> dict[str, Any] | None:
    """S4 (Movement subsystem §Q0) — seed per-PC ``pc_regions`` from the
    legacy party-level ``current_region`` anchor.

    Pre-this-story saves carry only the singular ``current_region`` (the
    party-level region). The per-PC model needs ``pc_regions[name]`` for each
    seated PC; ``region_for`` NEVER falls back to ``current_region`` (No Silent
    Fallbacks), so an unmigrated save would have no per-PC region at all. This
    migration seeds it.

    Behaviour (mirrors ``_migrate_s3_party_location``):
    - If ``pc_regions`` is already present and truthy: no-op (canonical save —
      do not clobber live per-PC truth).
    - If ``current_region`` is absent/falsy: no-op (nothing to seed from).
    - If ``player_seats`` is absent/empty: no-op (no seated PCs to seed).
    - Otherwise seed ``pc_regions[name] = current_region`` for each seated PC.

    ``current_region`` is RETAINED (it stays the spawn/teleport anchor) — NOT
    dropped. Returns OTEL attributes when any seat was seeded, else None.
    """
    if out.get("pc_regions"):
        return None

    current_region = out.get("current_region")
    if not current_region:
        return None

    seats = out.get("player_seats") or {}
    if not isinstance(seats, dict) or not seats:
        return None

    pc_regions = out.setdefault("pc_regions", {})
    if not isinstance(pc_regions, dict):
        return None

    seeded = 0
    for character_name in seats.values():
        if not character_name:
            continue
        if character_name in pc_regions:
            continue
        pc_regions[character_name] = current_region
        seeded += 1

    if seeded == 0:
        return None

    return {"s4_pc_regions_seeded": seeded}


def migrate_legacy_snapshot(data: dict[str, Any]) -> dict[str, Any]:
    """Rewrite a legacy snapshot dict into the canonical shape.

    Pure-ish: returns a new dict; does not mutate the input. Emits a
    ``snapshot.canonicalize`` OTEL span only when at least one
    sub-function rewrote a field — silent on canonical input.
    """
    out = copy.deepcopy(data)
    attributes: dict[str, Any] = {}

    # Migration sub-functions. Each returns either None (no-op) or a dict
    # of OTEL attributes to merge into the canonicalize span.
    for sub in (
        _migrate_s1_world_confrontations,
        _migrate_s2_npc_registry_split,
        _migrate_s3_party_location,
        _migrate_s4_pc_regions,
    ):
        attrs = sub(out)
        if attrs is not None:
            attributes.update(attrs)

    if attributes:
        with Span.open(SPAN_SNAPSHOT_CANONICALIZE, attributes):
            pass

    return out
