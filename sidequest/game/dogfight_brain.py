"""Deterministic opponent-maneuver policy for the dogfight (ADR-153 §4).

Pure positioning decision: pick a maneuver id from the legal+affordable set,
weighted by the opponent ace's disposition attitude. NO geometry, NO damage
(the firewall — ADR-153 §2): the entire output is a maneuver id string; the
cross-product interaction table composes geometry and SWN resolves the shot.

Resume-safe (ADR-128): deterministic given (attitude, maneuvers, energy,
turn_seed) — the tie-break is seeded from the encounter turn, never
Math.random / wall-clock, so a replay is identical.

This is the DETERMINISTIC FALLBACK + the legality reference. The narrator is
the primary chooser (ADR-153 §4); this fires when the narrator omits or picks
an illegal opponent maneuver, and as the floor under a skipped narrator pass.
"""

from __future__ import annotations

from sidequest.genre.models.rules import ManeuverDef

# Attitude → ordered class preference. An ace out for blood presses offense; a
# pilot trying to live breaks and recovers; neutral balances.
_PREFERENCE: dict[str, tuple[str, ...]] = {
    "hostile": ("offensive", "offensive_space_only", "evasive", "passive"),
    "neutral": ("evasive", "offensive", "offensive_space_only", "passive"),
    "friendly": ("evasive", "passive", "offensive", "offensive_space_only"),
}


def _affordable(m: ManeuverDef, energy: int) -> bool:
    # A recovery maneuver (negative cost) is always affordable; a spend needs the budget.
    return m.energy_cost <= 0 or energy >= m.energy_cost


def select_opponent_maneuver(
    *, attitude: str, maneuvers: list[ManeuverDef], energy: int, turn_seed: int
) -> str:
    """Pick a legal, affordable maneuver id weighted by attitude.

    Always returns an id present in ``maneuvers`` (falls back to the cheapest
    legal maneuver if no preferred class is affordable). Raises ``ValueError``
    when there are no maneuvers to choose from — a genuine engine/content defect,
    never a silently fabricated id (CLAUDE.md No Silent Fallbacks).
    """
    if not maneuvers:
        raise ValueError("opponent brain: no legal maneuvers to choose from")

    affordable = [m for m in maneuvers if _affordable(m, energy)]
    if not affordable:
        # Nothing in budget: minimize the energy deficit with the cheapest legal
        # maneuver — attitude preference is moot when no move can be afforded (the
        # floor, ADR-006). Cheapest by cost, then id for a deterministic tie-break.
        return min(maneuvers, key=lambda m: (m.energy_cost, m.id)).id

    order = _PREFERENCE.get(attitude, _PREFERENCE["neutral"])
    for cls in order:
        candidates = sorted((m for m in affordable if m.maneuver_class == cls), key=lambda m: m.id)
        if candidates:
            return candidates[turn_seed % len(candidates)].id
    # Affordable, but none match the attitude's preferred classes (unknown
    # classes): cheapest affordable, deterministic by (cost, id).
    return min(affordable, key=lambda m: (m.energy_cost, m.id)).id
