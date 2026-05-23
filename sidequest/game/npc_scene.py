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
  before calling. A ``None`` scene id is treated as "no scene
  context" and the predicate returns ``False`` unless the encounter
  branch fires. Callers that want "show all NPCs when scene is
  unresolvable" semantics (the tool's pre-perspective fallback) must
  bypass this predicate — it is the per-NPC scene-membership question,
  not the per-call scope question.

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
"""

from __future__ import annotations

from sidequest.game.encounter import StructuredEncounter
from sidequest.game.session import Npc


def is_npc_in_scene(
    npc: Npc,
    *,
    current_room: str | None,
    encounter: StructuredEncounter | None = None,
) -> bool:
    """Return ``True`` iff ``npc`` is in the scene identified by
    ``current_room``.

    See module docstring for the full semantic. Summary:

    1. **Encounter-actor override:** an NPC named in
       ``encounter.actors`` of an UNRESOLVED encounter is in-scene
       regardless of location fields.
    2. **No scene context:** when ``current_room is None`` and the
       encounter branch did not fire, return ``False``.
    3. **Structured-field union:** match if EITHER ``npc.current_room``
       or ``npc.location`` equals ``current_room``. These are
       orthogonal coordinates (chassis-interior vs general-world);
       a non-conflicting match on either is sufficient.
    4. **Prose fallback:** when BOTH structured fields are ``None``,
       consult ``npc.last_seen_location`` as a narrator-prose
       continuity hint. Otherwise (any structured field set),
       ``last_seen_location`` is ignored — structured-state writes
       overrule stale prose observations.
    5. **Default:** return ``False``.
    """
    if encounter is not None and not encounter.resolved:
        name = npc.core.name
        if any(actor.name == name for actor in encounter.actors):
            return True

    if current_room is None:
        return False

    cr = npc.current_room
    loc = npc.location
    if cr == current_room or loc == current_room:
        return True

    if cr is None and loc is None:
        return npc.last_seen_location == current_room

    return False
