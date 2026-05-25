"""NarrativeSheet — game-layer character health summary for player-facing surfaces.

ADR-040 (No Raw Stats) governs this module: the narrative sheet carries NO raw
ability scores, dice modifiers, or hidden GM numbers. The single deliberate
exception (ADR-040 amendment, ADR-114 §8) is the **lethality number** — current
HP and max HP — which IS exposed here by direct playgroup mandate. Sebastien and
Jade want legible lethality, and the dice overlay already shows damage rolling.

Six-band health description
---------------------------
The ``health_band`` string summarises HP status for prose consumption:

    unwounded  > 0.75 fraction
    wounded    > 0.50 fraction
    bloodied   > 0.25 fraction
    staggering > 0.00 fraction
    down       == 0   (or negative — over-broken)

Bands match the spec used by:
  - ``sidequest.agents.narrator_perception_filter._hp_band``
  - ``sidequest.agents.tools.query_encounter`` encounter entries

hp_current / hp_max (ADR-040 amendment, ADR-114 §8)
----------------------------------------------------
The raw numbers are ADDITIVE to the band — not a replacement. Both fields are
always present alongside ``health_band``. No other raw stat is exposed.
"""

from __future__ import annotations

from pydantic import BaseModel

from sidequest.game.creature_core import CreatureCore


class NarrativeSheet(BaseModel):
    """Player-facing character health summary.

    ADR-040: no raw stats except the lethality number (ADR-114 §8 amendment).
    """

    model_config = {"extra": "forbid"}

    # ------------------------------------------------------------------ #
    # Health band — six-tier severity label (ADR-040 prose surface)       #
    # ------------------------------------------------------------------ #

    health_band: str
    """Qualitative HP severity: unwounded / wounded / bloodied /
    staggering / down. Derived from hp_current/hp_max fraction at
    sheet-build time."""

    # ------------------------------------------------------------------ #
    # Lethality numbers — ADR-040 amendment, ADR-114 §8 ONLY             #
    # ------------------------------------------------------------------ #

    hp_current: int
    """Current HP. Exposed raw per ADR-114 §8 (playgroup mandate:
    Sebastien + Jade want legible lethality). This is the ONLY raw
    stat exception to ADR-040."""

    hp_max: int
    """Maximum HP. Exposed raw per ADR-114 §8. See hp_current."""


def _hp_band(fraction: float) -> str:
    """Map an HP fraction to a severity band.

    Boundaries mirror ``sidequest.agents.narrator_perception_filter._hp_band``
    and the per-tool spec for Task 6:
    ``unwounded`` >0.75 · ``wounded`` >0.50 · ``bloodied`` >0.25 ·
    ``staggering`` >0 · ``down`` ==0. Negative HP (over-broken) collapses
    to ``down``.
    """
    if fraction <= 0.0:
        return "down"
    if fraction > 0.75:
        return "unwounded"
    if fraction > 0.50:
        return "wounded"
    if fraction > 0.25:
        return "bloodied"
    return "staggering"


def build_narrative_sheet(core: CreatureCore) -> NarrativeSheet:
    """Build a NarrativeSheet from a CreatureCore.

    Derives the six-band health_band from core.hp.current / core.hp.max.
    Populates hp_current and hp_max directly (ADR-040 amendment, ADR-114 §8).

    Args:
        core: The creature core carrying the HpPool. Must have hp.current
              and hp.max set (HpPool is always present post-ADR-114).

    Returns:
        NarrativeSheet with health_band, hp_current, and hp_max populated.
    """
    fraction = core.hp.current / core.hp.max if core.hp.max > 0 else 0.0
    return NarrativeSheet(
        health_band=_hp_band(fraction),
        hp_current=core.hp.current,
        hp_max=core.hp.max,
    )
