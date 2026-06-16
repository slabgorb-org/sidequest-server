"""Fate-gear content-validator rules (story 114-10; design §Validator).

Three content checks keep a Fate pack paradigm-sound and balanced (so an author
cannot ship a free stunt-item — the forbidden re-balance the binding exists to
delete). The design's fourth check — "permission is never an engine gate" — is a
structural property of the engine, not a per-pack content check, so it is proven
behaviorally in the test suite, not here:

  1. ``check_no_inventory_under_fate`` — a ``ruleset: fate`` pack shipping
     ``inventory.yaml`` is a hard error (Fate has no economy; No Silent Fallbacks).
  2. ``check_refresh_invariant`` — every archetype's authored ``refresh`` must equal
     ``base_refresh − max(0, total_stunts − free_stunts)`` (SRD floor 1).
  3. ``check_dangling_gear_ids`` — no archetype references a gear id absent from the
     genre-tier GearDef set.

(The fourth design check — "permission is never an engine gate" — is a structural
property of the engine, not a per-pack content check; it is proven behaviorally in
the test suite, not here.)

These are pure functions (refactor-stable) wired into the genre loader's fate-gear
validation pass (``loader._validate_fate_gear``) so a misconfigured pack fails loud
at load."""

from __future__ import annotations

from collections.abc import Iterable

#: SRD floor — refresh can never be validated below this (mirrors fate_gear.REFRESH_FLOOR).
REFRESH_FLOOR = 1


def check_no_inventory_under_fate(*, ruleset: str, has_inventory: bool) -> str | None:
    """A ``ruleset: fate`` pack must not ship an ``inventory.yaml`` (Fate has no
    equipment economy). Returns an error string, or None if clean. Non-fate packs
    legitimately ship inventory and are never flagged."""
    if ruleset == "fate" and has_inventory:
        return (
            "a ruleset: fate pack must not ship inventory.yaml — Fate has no "
            "equipment economy (author gear.yaml instead; No Silent Fallbacks)"
        )
    return None


def check_refresh_invariant(
    *,
    archetype: str,
    authored_refresh: int,
    base_refresh: int,
    free_stunts: int,
    total_stunts: int,
) -> str | None:
    """The refresh invariant: an archetype's authored ``refresh`` must equal
    ``base_refresh − max(0, total_stunts − free_stunts)`` (SRD floor). Returns an
    error string naming the offending archetype, or None if balanced."""
    expected = max(REFRESH_FLOOR, base_refresh - max(0, total_stunts - free_stunts))
    if authored_refresh != expected:
        return (
            f"archetype {archetype!r} authored refresh {authored_refresh} but the "
            f"invariant requires {expected} (base_refresh={base_refresh} − "
            f"max(0, total_stunts={total_stunts} − free_stunts={free_stunts}); "
            "stunt-gear must debit refresh — no free stunts)"
        )
    return None


def check_dangling_gear_ids(
    *,
    archetype: str,
    referenced_ids: Iterable[str],
    available_ids: Iterable[str],
) -> list[str]:
    """Every gear id an archetype references must exist in the genre-tier GearDef set.
    Returns one error string per dangling id (empty list if clean)."""
    available = set(available_ids)
    return [
        f"archetype {archetype!r} references unknown gear id {gear_id!r} "
        f"(not in the genre-tier gear.yaml set)"
        for gear_id in referenced_ids
        if gear_id not in available
    ]
