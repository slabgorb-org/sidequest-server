"""environment_clock — deterministic per-beat survival-clock burn.

Server-injected (NOT LLM-decomposed) dispatch for the light & darkness
survival clock (Phase 3 of the light-darkness spec). On a time-advancing turn
in an unlit region it burns the ``light`` pool one unit, then reconciles the
darkness penalty against the resulting state: if the pool is at its floor in an
unlit region, exactly one darkness ``Status`` (a ``roll_modifier`` penalty) is
present on the acting PC; otherwise none.

Two design decisions (deviations from spec §6.1/§7 mechanism, same intent):

* **State-reconciled, not crossing-edge.** The penalty is derived from current
  state every tick, not minted on a downward threshold crossing. This handles
  spec §12 ("enter a region already unlit ⇒ −2 immediately") where no crossing
  happens because you *begin* at 0, and makes the reconcile idempotent and
  resume-safe — a second tick at the floor neither stacks a second penalty nor
  drops the existing one.
* **Sentinel-text identification.** The darkness status is found/removed by its
  sentinel ``text`` (:data:`DARKNESS_STATUS_TEXT`) so reconcile touches only its
  own status and never a combat wound.

The burn mechanism deviates from spec §6.1 (no exploration-beat taxonomy
exists yet); the intent — undodgeable, deterministic light decay — is preserved.
"""

from __future__ import annotations

from sidequest.agents.subsystems import SubsystemOutput
from sidequest.game.creature_core import CreatureCore
from sidequest.game.resource_pool import ResourcePatchOp, ResourcePool
from sidequest.game.session import GameSnapshot
from sidequest.game.status import Status, StatusSeverity
from sidequest.protocol.dispatch import SubsystemDispatch
from sidequest.telemetry.spans import light_tick_span

DARKNESS_STATUS_TEXT = "Plunged into darkness — every action is harder."
# Stable MACHINE identity for the darkness status (Status.source), used by
# reconcile to find/remove exactly its own status. Distinct from the
# human-readable ``text`` so a wording/i18n change can never silently break the
# clear-by-identity logic.
DARKNESS_STATUS_SOURCE = "environment_clock"
DARKNESS_PENALTY = -2  # spec §9 default N; tunable later via threshold metadata


def _ensure_darkness_penalty(core: CreatureCore) -> bool:
    """Ensure exactly one darkness penalty status is present. Returns True
    when one was newly added (False when already present — idempotent).

    Identity is by ``source`` (structured marker), not ``text``. The status is
    ``StatusSeverity.Wound``, which a narrator-explicit clear or rest could
    remove — that is fine: this penalty is environment-derived and re-asserted
    against current state on every tick, so a premature clear heals only until
    the next unlit tick re-applies it."""
    if any(s.source == DARKNESS_STATUS_SOURCE for s in core.statuses):
        return False
    core.statuses.append(
        Status(
            text=DARKNESS_STATUS_TEXT,
            source=DARKNESS_STATUS_SOURCE,
            severity=StatusSeverity.Wound,
            roll_modifier=DARKNESS_PENALTY,
        )
    )
    return True


def _clear_darkness_penalty(core: CreatureCore) -> bool:
    """Remove the darkness penalty status if present. Returns True when one
    was removed. Keys on ``source`` so it touches only this subsystem's own
    status — never a combat wound that happens to share wording."""
    before = len(core.statuses)
    core.statuses[:] = [s for s in core.statuses if s.source != DARKNESS_STATUS_SOURCE]
    return len(core.statuses) != before


def _find_torch(core: CreatureCore) -> dict | None:
    """Return the first inventory item carrying the ``light`` tag with a
    positive ``quantity`` (a usable torch), or None. Identity is by the
    structured ``tags`` field (matches the content schema:
    ``tags: [light, consumable, essential]``), not item name."""
    for item in core.inventory.items:
        tags = item.get("tags") or []
        if "light" in tags and int(item.get("quantity", 0) or 0) > 0:
            return item
    return None


def _emit_light_tick(data: dict[str, object], pool_max: float) -> None:
    """Emit the ``light.tick`` lie-detector span from the assembled ``data``
    dict (the single source of truth, so the span never drifts from the
    returned mechanical result). Every tick against a real ``light`` pool —
    lit no-burn and unlit burn alike — emits, so the GM panel sees the clock
    turning. OTEL attributes cannot be None: ``crossed`` (a list or None) is
    joined into a string ("" when none)."""
    crossed = data.get("crossed")
    crossed_threshold = ",".join(crossed) if isinstance(crossed, list) else ""
    with light_tick_span(
        region=str(data.get("region", "")),
        lit=bool(data.get("lit", False)),
        burned=bool(data.get("burned", False)),
        light_current=float(data.get("light_current", 0.0) or 0.0),
        light_max=float(pool_max),
        crossed_threshold=crossed_threshold,
        penalty_applied=bool(data.get("penalty_applied", False)),
    ):
        pass


def _run_relight(
    dispatch: SubsystemDispatch,
    *,
    snapshot: GameSnapshot,
    pool: ResourcePool,
) -> SubsystemOutput:
    """Light a torch: consume one torch charge, set the ``light`` pool to its
    max, and clear the darkness penalty on the acting PC.

    Charge model: a torch is an inventory item dict tagged ``light`` (content
    schema ``tags: [light, consumable, essential]``); its ``quantity`` is the
    charge count — one item = one relight to max. On relight ``quantity`` is
    decremented; the item is removed when it hits zero. ``torch_charges_remaining``
    reports the remaining ``quantity`` of the consumed torch.

    Fail loud (No Silent Fallbacks): no usable torch ⇒ ``data["error"] =
    "no_torch"`` and NOTHING is mutated (light unchanged, penalty unchanged) so
    the relight visibly fails and the player knows.

    Does NOT burn light. The OTEL ``light.relit`` span is Task 4.3 — the data
    dict here carries ``relit`` / ``torch_charges_remaining`` for it to read.
    """
    character_name = dispatch.params.get("character_name")
    core = snapshot.find_creature_core(character_name) if character_name else None

    data: dict[str, object] = {
        "region": dispatch.params.get("region", ""),
        "lit": True,
        "burned": False,
        "light_current": pool.current,
        "crossed": None,
        "relit": False,
    }

    if character_name and core is None:
        # A name was given but no seated PC matched — surface it, mutate nothing.
        data["character_unresolved"] = character_name
        data["error"] = "no_torch"
        return SubsystemOutput(directives=[], data=data)

    torch = _find_torch(core) if core is not None else None
    if torch is None:
        # No usable torch: fail loud, mutate nothing.
        data["error"] = "no_torch"
        return SubsystemOutput(directives=[], data=data)

    # Consume one charge. Remove the item dict when its last charge is spent.
    remaining = int(torch.get("quantity", 0) or 0) - 1
    if remaining <= 0:
        core.inventory.items.remove(torch)
        remaining = 0
    else:
        torch["quantity"] = remaining

    # Set the light pool to its max via the public resource-patch surface (Set
    # bypasses the voluntary guard that rejects Subtract on this non-voluntary
    # pool).
    result = snapshot.apply_resource_patch_by_name("light", ResourcePatchOp.Set, pool.max)
    data["light_current"] = result.new_value

    if _clear_darkness_penalty(core):
        data["penalty_cleared"] = True

    data["relit"] = True
    data["torch_charges_remaining"] = remaining
    return SubsystemOutput(directives=[], data=data)


async def run_environment_clock_dispatch(
    dispatch: SubsystemDispatch,
    *,
    snapshot: GameSnapshot,
) -> SubsystemOutput:
    """Burn the ``light`` pool one unit in an unlit region and reconcile the
    darkness penalty against current state.

    ``params``:
      - ``lit`` (bool): the region is lit (lantern, daylight, etc.). No burn;
        any darkness penalty is cleared.
      - ``region`` (str): region id, for OTEL/data attribution only.
      - ``character_name`` (str): the PC whose darkness penalty is reconciled.

    A missing ``light`` pool is a structured skip (``data["error"] =
    "no_light_pool"``) — this subsystem can be dispatched on a turn for a pack
    that has not declared the survival clock; that is not a configuration fault,
    so it returns an error code rather than raising.
    """
    pool = snapshot.resources.get("light")
    if pool is None:
        return SubsystemOutput(directives=[], data={"error": "no_light_pool"})

    if dispatch.params.get("mode") == "relight":
        return _run_relight(dispatch, snapshot=snapshot, pool=pool)

    lit = bool(dispatch.params.get("lit", False))
    region = dispatch.params.get("region", "")
    character_name = dispatch.params.get("character_name")
    core = snapshot.find_creature_core(character_name) if character_name else None

    data: dict[str, object] = {
        "region": region,
        "lit": lit,
        "burned": False,
        "light_current": pool.current,
        "crossed": None,
    }

    # No silent fallback: a name was given but no seated PC matched. Surface it
    # (still burn light below); penalty reconcile is skipped because there is no
    # core to reconcile against.
    if character_name and core is None:
        data["character_unresolved"] = character_name

    if lit:
        # Lit region: no burn; clear any darkness penalty.
        if core is not None and _clear_darkness_penalty(core):
            data["penalty_cleared"] = True
        _emit_light_tick(data, pool.max)
        return SubsystemOutput(directives=[], data=data)

    # Unlit region: burn one unit (clamped at the pool floor). Mutates through
    # the public GameSnapshot surface (not the private pool method) so the
    # snapshot's resource-patch invariants run.
    result = snapshot.apply_resource_patch_by_name("light", ResourcePatchOp.Subtract, 1.0)
    data["burned"] = True
    data["light_current"] = result.new_value
    crossed = [t.event_id for t in result.crossed_thresholds]
    data["crossed"] = crossed or None

    # Reconcile penalty against state (handles enter-at-0 with no crossing).
    if core is not None:
        if result.new_value <= pool.min:
            if _ensure_darkness_penalty(core):
                data["penalty_applied"] = True
        elif _clear_darkness_penalty(core):
            data["penalty_cleared"] = True

    _emit_light_tick(data, pool.max)
    return SubsystemOutput(directives=[], data=data)
