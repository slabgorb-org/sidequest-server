"""Shared CWN/WWN 0-HP downed seam (spec 2026-05-28 Task 11; WWN content Plan 3 Task 7).

Both the strike path (``dispatch.dice.dispatch_dice_throw``) and the WWN cast
path (``server.narration_apply._resolve_wwn_cast_for_beat``) must run the SAME
Mortal/Major Injury resolution after they ablate a defender to 0 HP. This module
holds the single implementation so the two paths cannot drift (CLAUDE.md "Don't
reinvent — wire up what exists").

``physical_save_target_for`` resolves the downed actor's Physical-save target
number the SAME way for both paths: PC stats come from the seated
``Character.stats`` block; opponent stats come from the confrontation's
``opponent_default_stats`` (reserved combat keys removed). Fails loud — never
silently defaults the target number (No Silent Fallbacks).

``run_cwn_wwn_downed_seam`` runs the gated seam: it is a no-op unless the bound
ruleset config IS a ``CwnConfig``/``WwnConfig`` (covers ``cwn``, ``wwn``, and
``awn`` — whose ``AwnConfig`` subclasses ``CwnConfig``) AND the named defender is
at <= 0 HP, and otherwise computes the save target and calls
``ruleset.resolve_downed`` (which emits ``wwn.mortal_injury.declared`` /
``cwn.mortal_injury.declared`` and, on a failed Traumatic-Hit save, the
``*.major_injury.roll`` span).
"""

from __future__ import annotations

import random

from sidequest.game.beat_kinds import _opposite_side_first_actor
from sidequest.game.encounter import StructuredEncounter
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import ConfrontationDef


class DiceDispatchError(Exception):
    """A DICE_THROW (or shared dispatch seam) could not be resolved.

    Wrapper for validation / resolution failures so the session handler can
    surface them as ERROR messages without string-matching the exception
    chain. Defined here (not in ``dice``) so the shared downed seam can raise
    it without importing the dice module; ``dice`` re-exports it.
    """


def physical_save_target_for(
    *,
    ruleset,
    snapshot: GameSnapshot,
    cdef: ConfrontationDef,
    name: str,
    core,
    cfg,
) -> int:
    """Physical-save target number for the downed actor (CWN/WWN Major Injury gate).

    Computes ``ruleset.save_params(...).difficulty`` for the downed actor's
    Physical save. The downed actor's stats + level are resolved the SAME way
    the rest of dispatch does (CreatureCore/Npc carry no ability scores):

    - PC (a ``snapshot.characters`` entry by name) → that ``Character.stats``
      block + ``core.level``.
    - Opponent (no matching Character) → the confrontation's
      ``opponent_ability_scores()`` (reserved hp/armor_class/dexterity keys
      removed) + ``core.level``.

    Only reached inside the CWN/WWN 0-HP branch, so ``cfg`` is a
    Cwn/WwnConfig. Fails loud (No Silent Fallbacks) if ``cfg`` is None, the
    opponent has no authored ability scores, or ``save_params`` rejects the
    stat block — never silently defaults the target number.
    """
    from sidequest.genre.models.rules import CwnConfig, WwnConfig

    if not isinstance(cfg, (CwnConfig, WwnConfig)):
        raise DiceDispatchError(
            "CWN/WWN downed seam reached with a non-CwnConfig/WwnConfig ruleset config "
            f"({type(cfg).__name__}); cannot compute the Physical save target "
            "(CLAUDE.md No Silent Fallbacks — refusing to default to a fixed number)"
        )

    pc = next((c for c in snapshot.characters if c.core.name == name), None)
    if pc is not None:
        stats = pc.stats
    else:
        stats = cdef.opponent_ability_scores()
        if not stats:
            raise DiceDispatchError(
                f"CWN/WWN downed seam: opponent {name!r} has no ability scores to "
                "resolve a Physical save — author them under "
                "opponent_default_stats (No Silent Fallbacks)"
            )

    level = int(getattr(core, "level", 1) or 1)
    return ruleset.save_params(
        stats=stats,
        save=cfg.trauma.major_injury_save,
        level=level,
        label="major-injury",
        cfg=cfg,
    ).difficulty


def run_cwn_wwn_downed_seam(
    *,
    ruleset,
    snapshot: GameSnapshot,
    encounter: StructuredEncounter,
    cdef: ConfrontationDef,
    pack: GenrePack | None,
    actor_side: str,
    rng: random.Random = random,  # type: ignore[assignment]
) -> None:
    """Run the CWN/WWN Mortal/Major Injury seam for a defender just dropped to 0 HP.

    Gated on the bound ruleset config being a ``CwnConfig``/``WwnConfig`` — which
    covers cwn, wwn, AND awn (``AwnConfig`` subclasses ``CwnConfig``). Base
    ``resolve_downed`` is a no-op for native/swn, but ``physical_save_target_for``
    calls ``save_params`` (which native/swn DO have) and reads ``cfg.trauma``
    (present on CwnConfig AND WwnConfig, absent on SwnConfig), so we gate the
    WHOLE seam on the config capability rather than relying on the no-op return.

    The defender is the first live actor on the side OPPOSITE ``actor_side``
    (the same ``_opposite_side_first_actor`` idiom the strike path uses). No-op
    when there is no opposing actor, the defender can't be resolved, or the
    defender still has HP > 0.

    ``resolve_downed`` always declares a Mortal Injury (emits
    ``{ruleset}.mortal_injury.declared``); if a Traumatic Hit landed this scene
    it additionally rolls a Physical save and, on failure, the Major Injury
    table (emits ``{ruleset}.major_injury.roll``).
    """
    from sidequest.genre.models.rules import CwnConfig, WwnConfig

    # Capability gate (not a slug string): the seam runs for any ruleset whose
    # config IS a Cwn/WwnConfig — covers cwn, wwn, AND awn (AwnConfig subclasses
    # CwnConfig) plus future sister modules, instead of silently falling through
    # on a `ruleset in ("cwn","wwn")` string check.
    if not (
        pack and pack.rules and isinstance(pack.rules.ruleset_config(), (CwnConfig, WwnConfig))
    ):
        return
    down_name = _opposite_side_first_actor(encounter, actor_side)
    if down_name is None:
        return
    down_core = snapshot.find_creature_core(down_name)
    if down_core is None or down_core.hp.current > 0:
        return
    cfg = pack.rules.ruleset_config()
    scene_traumatic = any(t.text == "Traumatic Hit Landed" for t in encounter.tags)
    save_target = physical_save_target_for(
        ruleset=ruleset,
        snapshot=snapshot,
        cdef=cdef,
        name=down_name,
        core=down_core,
        cfg=cfg,
    )
    ruleset.resolve_downed(
        core=down_core,
        save_target=save_target,
        scene_traumatic=scene_traumatic,
        cfg=cfg,
        rng=rng,
    )
