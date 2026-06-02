"""Fail-loud cross-reference validation for a world's Premise/Bloc content.

Per the project rule **No Silent Fallbacks**: a dangling reference (authority
that is not an authored NPC, a bloc/premise id that does not exist, an act
outside the genre vocabulary) raises on the first violation rather than loading
a half-wired political layer.
"""

from __future__ import annotations

from collections.abc import Collection

from sidequest.genre.error import GenreValidationError
from sidequest.genre.models.premises import BlocDef, PremiseDef


def validate_premises(
    *,
    premises: list[PremiseDef],
    blocs: list[BlocDef],
    authored_npc_ids: Collection[str],
    valid_act_ids: Collection[str],
    world_slug: str,
) -> None:
    """Validate Premise/Bloc cross-references for one world.

    Args:
        premises: The world's authored premises.
        blocs: The world's authored blocs.
        authored_npc_ids: Ids of NPCs authored in this world (premise authorities
            must resolve to one of these).
        valid_act_ids: Ids in the genre-tier witnessed-act vocabulary
            (every ``drained_by`` / ``awakening_acts`` act must be one of these).
        world_slug: World identifier, for error messages.

    Raises:
        GenreValidationError: On the first dangling reference found.
    """
    npc_ids = set(authored_npc_ids)
    act_ids = set(valid_act_ids)
    premise_ids = {p.premise_id for p in premises}
    bloc_ids = {b.bloc_id for b in blocs}

    if len(premise_ids) != len(premises):
        raise GenreValidationError(f"[{world_slug}] duplicate premise_id among premises")
    if len(bloc_ids) != len(blocs):
        raise GenreValidationError(f"[{world_slug}] duplicate bloc_id among blocs")

    for p in premises:
        if p.authority not in npc_ids:
            raise GenreValidationError(
                f"[{world_slug}] premise '{p.premise_id}' authority "
                f"'{p.authority}' is not an authored NPC id in this world"
            )
        for prop in p.propped_by:
            if prop not in bloc_ids:
                raise GenreValidationError(
                    f"[{world_slug}] premise '{p.premise_id}' propped_by "
                    f"'{prop}' is not a declared bloc_id"
                )
        for d in p.drained_by:
            if d.act not in act_ids:
                raise GenreValidationError(
                    f"[{world_slug}] premise '{p.premise_id}' drained_by act "
                    f"'{d.act}' is not in the genre witnessed-act vocabulary"
                )

    for b in blocs:
        for pid in b.grants_belief_to:
            if pid not in premise_ids:
                raise GenreValidationError(
                    f"[{world_slug}] bloc '{b.bloc_id}' grants_belief_to "
                    f"'{pid}' is not a declared premise_id"
                )
        for a in b.awakening_acts:
            if a.act not in act_ids:
                raise GenreValidationError(
                    f"[{world_slug}] bloc '{b.bloc_id}' awakening act "
                    f"'{a.act}' is not in the genre witnessed-act vocabulary"
                )
