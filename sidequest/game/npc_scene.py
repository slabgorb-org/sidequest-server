"""Unified scene-membership predicate for NPCs (story 61-7).

Pre-61-7 had two divergent scene-resolution sites:

* ``sidequest/server/session_helpers.py:_npc_in_scene`` — the snapshot
  projection (story 61-2) consulted ``last_seen_location`` only, plus
  an unresolved-encounter actor-membership branch.
* ``sidequest/agents/tools/list_npcs_in_scene.py:102`` — the read-side
  tool the narrator calls in-loop matched ``current_room`` or
  ``location``, with no encounter awareness.

The disjoint field-subset resolutions produced divergent verdicts on
legitimate fixtures (the 61-2 verify-phase adversarial probe measured
the divergence). 61-7 collapses both sites into ``is_npc_in_scene``
below.

Semantic (write-path trust hierarchy):

* The two STRUCTURED-state coordinate fields, ``current_room`` (chassis
  interior, ADR-055 territory) and ``location`` (general-world,
  written by structured world patches), are ORTHOGONAL per their model
  comments. An NPC aboard a docked ship has BOTH set to non-conflicting
  values (location="tavern_pier", current_room="ship_bridge"); the
  narrator may query either coordinate. The predicate matches if EITHER
  structured field equals the scene id (union semantics across the
  structured fields, preserving the existing tool's orthogonal-coordinate
  behavior).
* ``last_seen_location`` is a narrator-prose continuity hint written
  by ``narration_apply.py:1228`` — lower-trust than the structured
  writes. It is consulted ONLY when BOTH structured fields are unset
  (the NPC has not been positioned by any structured-state writer
  yet). This preserves the gaslighting-doctrine anchor: when
  ``current_room`` / ``location`` carry authoritative state, a stale
  ``last_seen_location`` does NOT override them and the narrator is
  not handed a ghost in the present scene.
* An unresolved encounter's actor list overrides location resolution
  entirely — an NPC named in ``encounter.actors`` of an unresolved
  encounter is in-scene regardless of where their location fields
  point. This carries over the 61-2 ``_npc_in_scene`` encounter
  branch and propagates it to the tool path.

Caller responsibility:

* The caller resolves the scene id (``current_room`` parameter)
  before calling. A ``None`` OR empty-string scene id is treated as
  "no scene context" and the predicate returns ``False`` unless the
  encounter branch fires. Callers that want "show all NPCs when scene
  is unresolvable" semantics (the tool's omniscient/debug fallback)
  must bypass this predicate — it is the per-NPC scene-membership
  question, not the per-call scope question.

Attribution helpers:

* ``is_npc_anchored_by_encounter`` exposes the encounter-branch check
  in isolation, so callers (projection + tool OTEL) can count
  encounter-anchored matches separately from location-anchored matches
  without duplicating the membership logic. ``is_npc_in_scene`` itself
  is the union of the two branches.

Design Deviation (logged per story 61-7 AC-2):

* TEA's RED-phase tests encoded the architect's recommended STRICT
  PRECEDENCE order (``current_room > location > last_seen_location``,
  first non-None wins). Implementation surfaced that ``current_room``
  and ``location`` are ORTHOGONAL coordinate axes per the Npc model
  comments — strict precedence would silently break the tool's
  existing chassis-interior-vs-world-location matching. The
  implemented semantic (UNION across structured, FALLBACK to
  ``last_seen_location`` only when both structured fields are None)
  passes every TEA test fixture identically. The behavior difference
  surfaces only on a non-test fixture (current_room and location both
  non-None and disagreeing on a same-axis question), which is
  ill-defined under the strict-precedence framing too.

Empty-string guards (review-fix round 2):

* ``current_room`` parameter is checked via ``if not current_room``
  (falsy-on-None-OR-empty) rather than ``if current_room is None``.
  An empty-string scene id reaching the predicate is treated as
  no-scene-context, matching the omniscient/debug fallback semantic
  the tool uses at the call-scope layer.
* ``npc.core.name`` is NOT guarded at the encounter branch — the
  ``CreatureCore.name_non_blank`` field validator
  (``sidequest/game/creature_core.py:239``) enforces non-empty names
  at construction. A runtime truthy guard here would shadow the
  upstream invariant and silently filter out malformed data the model
  contract already rejects. The regression-guard test
  ``test_upstream_creaturecore_validator_blocks_empty_npc_names`` pins
  the invariant; if the validator is ever relaxed, the test fails and
  the defensive guard can be re-added at that time.
"""

from __future__ import annotations

from sidequest.game.encounter import StructuredEncounter
from sidequest.game.session import Npc


def is_npc_anchored_by_encounter(
    npc: Npc,
    encounter: StructuredEncounter | None,
) -> bool:
    """Return ``True`` iff ``npc`` is named in ``encounter.actors`` of
    an UNRESOLVED encounter.

    Exposed as a separate predicate so callers can count encounter-
    anchored matches separately from location-anchored matches for
    OTEL attribution (per the OTEL Observability Principle, the GM
    panel needs to see which branch fired). ``is_npc_in_scene`` is
    the union of this predicate and the location-resolution branch.

    Guards:

    * ``encounter is None`` or ``encounter.resolved`` — return False.

    Note: ``npc.core.name`` is guaranteed non-empty by the
    ``CreatureCore.name_non_blank`` field validator at
    ``sidequest/game/creature_core.py:239`` — no runtime truthy guard
    is needed here. A regression-guard test pins that invariant
    (``test_upstream_creaturecore_validator_blocks_empty_npc_names``).
    If the upstream validator is ever relaxed, the encounter branch
    becomes vulnerable to empty-name false-positive matches against
    empty-named EncounterActor entries (EncounterActor.name has no
    validator); the test will surface the broken invariant before
    any defense here becomes load-bearing.
    """
    if encounter is None or encounter.resolved:
        return False
    return encounter.find_actor(npc.core.name) is not None


def _is_npc_in_scene_by_location(
    npc: Npc,
    current_room: str | None,
) -> bool:
    """Return ``True`` iff ``npc`` matches ``current_room`` via the
    structured-field union or the prose fallback.

    Excludes the encounter-actor branch (use ``is_npc_anchored_by_encounter``
    for that, or ``is_npc_in_scene`` for the combined predicate).
    """
    if not current_room:
        return False
    cr = npc.current_room
    loc = npc.location
    if cr == current_room or loc == current_room:
        return True
    if cr is None and loc is None:
        return npc.last_seen_location == current_room
    return False


def is_npc_in_scene(
    npc: Npc,
    *,
    current_room: str | None,
    encounter: StructuredEncounter | None = None,
) -> bool:
    """Return ``True`` iff ``npc`` is in the scene identified by
    ``current_room``.

    Union of two branches (see module docstring for the full semantic):

    1. **Encounter-actor override** (``is_npc_anchored_by_encounter``)
       — an NPC named in ``encounter.actors`` of an UNRESOLVED
       encounter is in-scene regardless of location fields.
    2. **Location resolution** (``_is_npc_in_scene_by_location``) —
       structured-field union (``current_room`` OR ``location``) plus
       prose fallback (``last_seen_location`` only when both
       structured fields are None).

    Callers needing per-branch attribution should also call
    ``is_npc_anchored_by_encounter`` to separate the two matching
    paths for OTEL counting.
    """
    if is_npc_anchored_by_encounter(npc, encounter):
        return True
    return _is_npc_in_scene_by_location(npc, current_room)
