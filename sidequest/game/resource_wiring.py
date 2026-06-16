"""Single chokepoint that materializes a genre pack's declared resource pools
into a snapshot. ADR-033 pools were dead in production before this (init_resource_pools
had no caller); this is the wiring. Idempotent — safe to call on create AND load."""

from __future__ import annotations

from typing import Any

from sidequest.game.session import GameSnapshot


def wire_genre_resources(snapshot: GameSnapshot, pack: Any | None) -> None:
    """Upsert the pack's RulesConfig.resources into snapshot.resources.

    No-op when pack/rules/resources are absent. Preserves existing ``current``
    on upsert (see GameSnapshot.init_resource_pools upsert semantics) so a
    reloaded mid-delve light value is not reset to ``starting``.
    """
    rules = getattr(pack, "rules", None)
    declarations = getattr(rules, "resources", None)
    if not declarations:
        return
    snapshot.init_resource_pools(declarations)
