"""World-tier items catalog.

A world's ``items.yaml`` is the canonical inventory of named, mechanical,
and narrative items the narrator can draw on. Five sections are recognized,
each shaped for a different gameplay lane:

* ``named_items`` — NPC-shaped items (item_legacy_v1 plugin shape):
  OCEAN, disposition, nature, history, demands, prohibitions.
* ``modifier_items`` — confrontation-modifier items
  (research-doc shape; ``confrontation_modifiers[]``).
* ``reliquaries`` — Three Rites relics with ``divine_favor_effect`` text
  that the Cleric narration consumes at ``divine_favor >= 0.7``.
* ``crimson_remnants`` — Crimson God items with ``cost_signature`` and
  notoriety acceleration.
* ``consumable_items`` — single-use scrolls/potions, often
  ``replaces_baseline`` shimming over a default item table.

The per-item shape varies by section — they were authored against
different design docs and there is no single normalised schema yet.
Loader policy: require ``id`` and ``name`` so consumers can index and
display, accept arbitrary other fields so the narrator can read them
without code change. Loud-fail on duplicate ids across the whole file.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class WorldItem(BaseModel):
    """A single item entry from any section of a world's items.yaml.

    Tolerant shape: only ``id`` and ``name`` are required so the loader
    can build a global index and the UI can render a label. Section-
    specific fields (``ocean``, ``divine_favor_effect``, ``cost_signature``,
    ``confrontation_modifiers``, etc.) are accepted as-is and exposed via
    pydantic's model dump.
    """

    model_config = {"extra": "allow"}

    id: str
    name: str


class WorldItemsCatalog(BaseModel):
    """A world's full items.yaml — all five sections plus header keys.

    Empty sections default to ``[]``. Worlds without an ``items.yaml``
    are represented as ``World.items = None`` rather than an empty
    catalog, so callers can distinguish "world has no items file" from
    "world authored an empty items list".
    """

    model_config = {"extra": "forbid"}

    world: str | None = None
    genre: str | None = None

    named_items: list[WorldItem] = Field(default_factory=list)
    modifier_items: list[WorldItem] = Field(default_factory=list)
    reliquaries: list[WorldItem] = Field(default_factory=list)
    crimson_remnants: list[WorldItem] = Field(default_factory=list)
    consumable_items: list[WorldItem] = Field(default_factory=list)

    def all_items(self) -> list[WorldItem]:
        """Return every item across every section in declaration order."""
        return [
            *self.named_items,
            *self.modifier_items,
            *self.reliquaries,
            *self.crimson_remnants,
            *self.consumable_items,
        ]

    def section_counts(self) -> dict[str, int]:
        """Per-section item counts. Used in the loader's OTEL payload."""
        return {
            "named_items": len(self.named_items),
            "modifier_items": len(self.modifier_items),
            "reliquaries": len(self.reliquaries),
            "crimson_remnants": len(self.crimson_remnants),
            "consumable_items": len(self.consumable_items),
        }
