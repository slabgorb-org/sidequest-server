"""Shared damage-roll helpers (ADR-114 / Task 11).

Extracted from ``sidequest.server.dispatch.dice`` so that both the
simple-DC path (``dispatch_dice_throw``) and the opposed-check path
(``narration_apply._resolve_opposed_check_branch``) can reuse the same
weapon-damage resolution logic without copy-paste.

The public helpers defined here are:

- ``damage_request_from_spec`` — ``DamageSpec`` → ``DiceRequestPayload``
- ``generate_server_faces``    — server-side random face roll for a dice pool

``resolve_damage_spec_from_beat_and_actor`` is **pure combat-rules logic** and
moved down to ``sidequest.game.ruleset.combat_rules`` (ADR-147 / story 122-2); it
is re-exported from here so existing callers keep importing it from this path.

``dice.py`` re-imports and re-exports these names so existing callers
(tests included) keep working unchanged.
"""

from __future__ import annotations

import random
import re

# ADR-147 / story 122-2: resolve_damage_spec_from_beat_and_actor is pure combat-rules
# logic that moved down to the game tier. Re-exported here so existing server-tier
# callers keep importing it from this path (server -> game is the legal direction).
from sidequest.game.ruleset.combat_rules import (  # noqa: F401
    resolve_damage_spec_from_beat_and_actor,
)
from sidequest.genre.models.inventory import (
    PARITY_BACKING_SIDES,
    DamageSpec,
    parity_value,
)
from sidequest.protocol.dice import (
    DiceRequestPayload,
    DieSides,
    DieSpec,
    ThrowParams,
)
from sidequest.protocol.types import Stat

_DICE_RE = re.compile(r"^(?P<count>\d+)d(?P<faces>\d+)$")

_DAMAGE_STAT = Stat("DAMAGE")
_DAMAGE_THROW_PARAMS = ThrowParams(
    velocity=(0.0, 4.0, -1.0),
    angular=(0.5, 0.5, 0.5),
    position=(0.5, 0.5),
)


def damage_request_from_spec(
    spec: DamageSpec,
    *,
    request_id: str,
    rolling_player_id: str = "server",
    character_name: str = "server",
) -> DiceRequestPayload:
    """Build a ``DiceRequestPayload`` for a weapon damage roll.

    Parses ``spec.dice`` (NdM notation) into N individual ``DieSpec`` entries
    (one per die, each with count=1) so the overlay can animate each die
    independently. ``spec.bonus`` becomes ``modifier``.

    ``stat``, ``difficulty``, and ``context`` are set to damage-roll sentinels
    — the overlay renders them but the engine doesn't interpret them for
    outcome resolution (damage rolls have no DC; the check roll already
    determined hit/miss).

    ``rolling_player_id`` defaults to ``"server"`` for server-originated rolls;
    override when a specific player should animate the throw.

    A parity die (Nd2) has no overlay mesh, so the request throws N backing d6
    instead (``PARITY_BACKING_SIDES``); the caller maps the settled faces to d2
    values via ``parity_damage_total`` for the actual damage total. The context
    string names the mapping so the readout can explain "the d6 shows 4 → 1".

    Raises ``ValueError`` if the dice string is malformed or uses an
    unsupported face count (validated at ``DamageSpec`` construction, so this
    is a belt-and-suspenders guard).
    """
    m = _DICE_RE.match(spec.dice.strip())
    if not m:
        raise ValueError(
            f"damage_request_from_spec: malformed dice string {spec.dice!r} "
            f"(should have been caught at DamageSpec validation)"
        )
    count = int(m["count"])
    faces = int(m["faces"])
    if spec.is_parity_die:
        # Throw a renderable backing d6 per die; the parity map (even→1/odd→2)
        # is applied to the total by parity_damage_total at the call site.
        sides = PARITY_BACKING_SIDES
        context = "unarmed damage (d2: backing d6, even→1 / odd→2)"
    else:
        sides = DieSides.from_wire(faces)
        if sides is DieSides.Unknown:
            raise ValueError(
                f"damage_request_from_spec: unsupported die face count d{faces} in {spec.dice!r}"
            )
        context = "weapon damage"
    # One DieSpec per die so the overlay renders individual dice.
    dice_pool = [DieSpec(sides=sides, count=1) for _ in range(count)]
    return DiceRequestPayload(
        request_id=request_id,
        rolling_player_id=rolling_player_id,
        character_name=character_name,
        dice=dice_pool,
        modifier=spec.bonus,
        stat=_DAMAGE_STAT,
        difficulty=1,  # damage rolls have no DC
        context=context,
        roll_role="damage",  # follow-on weapon roll — never the primary overlay
    )


def parity_damage_total(faces: list[int], bonus: int) -> int:
    """Total a parity (d2) damage roll from its backing-die faces.

    Each backing face maps even→1 / odd→2 (``parity_value``); the mapped
    values are summed and ``bonus`` added. Used in place of the plain
    face-sum total for an ``is_parity_die`` DamageSpec so the d6 shown in the
    overlay never leaks its raw face into the HP math."""
    return sum(parity_value(f) for f in faces) + bonus


def generate_server_faces(dice: list[DieSpec]) -> list[int]:
    """Roll server-side faces for a damage dice pool.

    Each die in the pool gets a random face in ``1..=sides``. The faces are
    used to both: (a) build the authoritative ``DiceResultPayload`` for the
    broadcast, and (b) feed ``resolve_dice_with_faces`` to compute the total.

    Uses ``random.randint`` — no physics, no seed. The seed in the result
    payload drives spectator replay animation; here it's derived from the
    session and round like the check-roll path.
    """
    faces: list[int] = []
    for spec in dice:
        sides = spec.sides.faces()
        assert sides is not None, f"Unknown die in damage pool: {spec.sides!r}"
        for _ in range(spec.count):
            faces.append(random.randint(1, sides))
    return faces
