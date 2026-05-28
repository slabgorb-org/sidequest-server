"""Non-beat dice resolution: SWN skill checks (2d6) and saving throws (d20).

Reuses the live DiceRequest/DiceResult round-trip and the five-tier RollOutcome
ladder. No encounter, no beat — the bound module produces the roll params; this
function rolls, resolves, broadcasts, and emits an OTEL span. Fails loud (the
module raises NotImplementedError) if the bound ruleset has no check/save support.

``DiceResultPayload`` carries ``seed=0`` and zero ``ThrowParams`` as sentinels for
server-generated rolls (no client physics session). Downstream consumers (UI dice
overlay, narrator) ignore these fields when the roll was server-side.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sidequest.game.dice import resolve_dice_with_faces
from sidequest.game.ruleset import get_ruleset_module
from sidequest.protocol.dice import (
    DiceRequestPayload,
    DiceResultPayload,
    DieSides,
    DieSpec,
    RollOutcome,
    ThrowParams,
)
from sidequest.protocol.types import Stat
from sidequest.telemetry.spans.encounter import check_resolved_span

# Zero ThrowParams sentinel for server-generated rolls (no physics session).
_ZERO_THROW_PARAMS = ThrowParams(
    velocity=(0.0, 0.0, 0.0),
    angular=(0.0, 0.0, 0.0),
    position=(0.0, 0.0),
)


@dataclass(frozen=True)
class CheckThrowOutcome:
    outcome: RollOutcome
    result: DiceResultPayload


def dispatch_check(
    *,
    kind: str,  # "skill_check" | "save"
    attribute: str | None,  # stat name for skill_check
    save: str | None,  # save CATEGORY for save ("physical"|"evasion"|"mental")
    skill_level: int,
    difficulty_key: str | None,  # difficulty ladder key for skill_check (e.g. "tricky")
    level: int,  # character level, used by save_params
    label: str,
    character_stats: dict[str, int],
    faces: list[int],  # client-reported face values
    pack,  # genre pack with .rules.ruleset and a .rules.ruleset_config() block
    rolling_player_id: str,
    character_name: str,
    session_id: str,
    room_broadcast: Callable[[object], None] | None,
) -> CheckThrowOutcome:
    """Resolve a non-beat SWN check or save, broadcast it, emit an OTEL span.

    ``kind="skill_check"`` rolls 2d6 via ``ruleset.check_params``; ``kind="save"``
    rolls 1d20 via ``ruleset.save_params``. Both paths reuse ``resolve_dice_with_faces``
    and the five-tier ``RollOutcome`` ladder. Fails loud on unknown kind.
    """
    ruleset = get_ruleset_module(pack.rules.ruleset)
    cfg = pack.rules.ruleset_config()

    if kind == "skill_check":
        params = ruleset.check_params(
            stats=character_stats,
            attribute=attribute,
            skill_level=skill_level,
            difficulty_key=difficulty_key,
            label=label,
            cfg=cfg,
        )
    elif kind == "save":
        params = ruleset.save_params(
            stats=character_stats,
            save=save,
            level=level,
            label=label,
            cfg=cfg,
        )
    else:
        raise ValueError(
            f"dispatch_check: unknown kind {kind!r} (expected 'skill_check' or 'save')"
        )

    spec = DieSpec(sides=DieSides.from_wire(params.sides), count=params.count)
    resolved = resolve_dice_with_faces(
        dice=[spec],
        faces=faces,
        modifier=params.modifier,
        difficulty=params.difficulty,
    )

    request_id = f"{session_id}:{kind}:{character_name}"
    result = DiceResultPayload(
        request_id=request_id,
        rolling_player_id=rolling_player_id,
        character_name=character_name,
        rolls=resolved.rolls,
        modifier=params.modifier,
        total=resolved.total,
        difficulty=params.difficulty,
        outcome=resolved.outcome,
        seed=0,
        throw_params=_ZERO_THROW_PARAMS,
    )

    if room_broadcast is not None:
        stat_val = Stat(attribute) if attribute else Stat(save if save else kind)
        request = DiceRequestPayload(
            request_id=request_id,
            rolling_player_id=rolling_player_id,
            character_name=character_name,
            dice=[spec],
            modifier=params.modifier,
            stat=stat_val,
            difficulty=params.difficulty,
            context=params.label,
        )
        room_broadcast(request)
        room_broadcast(result)

    check_resolved_span(
        kind=kind,
        actor=character_name,
        label=params.label,
        total=resolved.total,
        difficulty=params.difficulty,
        outcome=resolved.outcome.value,
    )

    return CheckThrowOutcome(outcome=resolved.outcome, result=result)
