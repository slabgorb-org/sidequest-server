"""Backward-compatible re-export shim — inventory resolution moved to the game tier.

ADR-147 (Honest Layering, story 122-2): ``resolve_inventory`` and its catalog-merge
helpers are pure (they import only ``sidequest.genre.*`` + ``sidequest.telemetry.*``),
so they were relocated down to :mod:`sidequest.game.inventory_resolve`. The combat-rules
helper :func:`sidequest.game.ruleset.combat_rules.resolve_damage_spec_from_beat_and_actor`
pulls ``resolve_inventory`` in, and a game-tier module must not import upward from
``server/`` — moving the function down deletes that edge.

This module stays as a re-export so the existing server-tier callers
(``views``, ``narration_apply``, ``chargen_summary``, ``chargen_mixin``,
``agents.tools.wn_tools``) keep importing from the old path unchanged. Importing
down from ``server/`` into ``game/`` is the legal direction (ADR-147 law).
"""

from __future__ import annotations

from sidequest.game.inventory_resolve import (
    VerbatimFieldLockError,
    merge_inventory_catalog,
    resolve_inventory,
)

__all__ = ["VerbatimFieldLockError", "merge_inventory_catalog", "resolve_inventory"]
