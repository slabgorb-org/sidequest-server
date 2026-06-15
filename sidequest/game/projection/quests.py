"""Quest-spine payload builder (ADR-137 / Story 77-8).

Converts the stored quest spine — ``snapshot.quest_log`` (dict[str, QuestEntry]),
``snapshot.quest_anchors`` (list[str] of body ids), and ``snapshot.active_stakes``
(str) — into a protocol ``QuestsPayload`` for the client quest/objective panel
(Story 77-5). Pure projection: no game logic, no writes. The RELATIONSHIPS-snapshot
analog for quests.

Anchors are associated to their owning quest via ``QuestEntry.anchor_id``. An
anchor no quest claims is still projected, with ``quest_id=None`` — surfaced
explicitly, never silently dropped (No Silent Fallbacks).

Coherence (Story 117-5, ADR-053 + ADR-100 + ADR-146): each quest also carries
the discovered lore the party has learned *about its anchor*, recovered through
a fully structural three-hop join — no fuzzy text/embedding match:

    QuestEntry.anchor_id
      ──matched against──> ClueNode.locations[] / ClueNode.implicates[]
      ──yields the clue id──> KnownFact.fact_id (source=="ScenarioClue", where
                              50-14 sets fact_id == clue id)

Only ScenarioClue-sourced facts cohere; generic narrator facts (no anchor tie)
stay out — no false coherence (the precedent for fact↔clue recovery is the
accusation walk, ``server/dispatch/scenario_accusation.py``).
"""

from __future__ import annotations

from typing import Any

from sidequest.protocol.models import (
    QuestAnchorEntry,
    QuestLogEntry,
    QuestLoreEntry,
    QuestsPayload,
)


def _related_lore_for_anchor(
    anchor_id: str | None,
    *,
    clue_ids_by_anchor: dict[str, set[str]],
    facts_by_clue_id: dict[str, QuestLoreEntry],
) -> list[QuestLoreEntry]:
    """Cohere the discovered ScenarioClue facts under one quest's anchor.

    Walks the structural join: anchor → clue ids touching it → the discovered
    facts whose ``fact_id`` is that clue id. Returns a clean empty list when the
    quest has no anchor, no clue touches the anchor, or nothing is learned yet
    (No Silent Fallbacks — empty, never None, never a crash).

    A falsy anchor (``None`` *or* ``""``) coheres NOTHING. An empty-string
    anchor is reachable — ``quest_offer.mint`` stores ``anchor_id=seed.anchor``
    even when the seed anchor is "", and a narrator ``record_quest`` can emit an
    empty anchor — and must never act as a wildcard that pulls every clue keyed
    under "" under the wrong quest.
    """
    if not anchor_id:
        return []
    out: list[QuestLoreEntry] = []
    for clue_id in clue_ids_by_anchor.get(anchor_id, ()):  # type: ignore[arg-type]
        lore = facts_by_clue_id.get(clue_id)
        if lore is not None:
            out.append(lore)
    return out


def build_quests_payload(snapshot: Any) -> QuestsPayload:
    """Project the stored quest spine into a ``QuestsPayload``.

    An empty spine yields a clean empty-but-valid payload (empty lists + ""),
    never None and never a throw.
    """
    quest_log = getattr(snapshot, "quest_log", {}) or {}
    quest_anchors = getattr(snapshot, "quest_anchors", []) or []
    active_stakes = getattr(snapshot, "active_stakes", "") or ""

    # Build the structural anchor→clue→fact join inputs once.
    #
    # clue_ids_by_anchor: body/NPC id -> the clue ids whose locations/implicates
    #   touch that body. facts_by_clue_id: clue id -> the discovered ScenarioClue
    #   fact minted for it (fact_id == clue id per 50-14). A quest's related lore
    #   is the intersection: clues touching its anchor that the party discovered.
    clue_ids_by_anchor: dict[str, set[str]] = {}
    scenario_state = getattr(snapshot, "scenario_state", None)
    if scenario_state is not None:
        clue_graph = getattr(scenario_state, "clue_graph", None)
        nodes = getattr(clue_graph, "nodes", []) if clue_graph is not None else []
        for node in nodes:
            for body_id in (*node.locations, *node.implicates):
                if not body_id:
                    # An empty body id is not a real anchor — never index it,
                    # or a "" anchor would cohere this clue as a wildcard.
                    continue
                clue_ids_by_anchor.setdefault(body_id, set()).add(node.id)

    facts_by_clue_id: dict[str, QuestLoreEntry] = {}
    for character in getattr(snapshot, "characters", []) or []:
        for fact in getattr(character, "known_facts", []) or []:
            if fact.source != "ScenarioClue":
                continue
            # fact_id == originating clue id for ScenarioClue facts (50-14).
            # Dedup across characters keyed on that clue id.
            facts_by_clue_id.setdefault(
                fact.fact_id, QuestLoreEntry(fact_id=fact.fact_id, content=fact.content)
            )

    log_entries = [
        QuestLogEntry(
            quest_id=quest_id,
            title=entry.title,
            objective=entry.objective,
            status=entry.status,
            anchor_id=entry.anchor_id,
            related_lore=_related_lore_for_anchor(
                entry.anchor_id,
                clue_ids_by_anchor=clue_ids_by_anchor,
                facts_by_clue_id=facts_by_clue_id,
            ),
        )
        for quest_id, entry in quest_log.items()
    ]

    # Reverse map: anchor body id -> owning quest id (first quest that claims it).
    # A falsy anchor ("" or None) is not a real anchor — never let it claim an
    # anchor entry, mirroring the empty-anchor-coheres-nothing rule above.
    anchor_owner: dict[str, str] = {}
    for quest_id, entry in quest_log.items():
        if entry.anchor_id and entry.anchor_id not in anchor_owner:
            anchor_owner[entry.anchor_id] = quest_id

    anchor_entries = [
        QuestAnchorEntry(anchor_id=anchor_id, quest_id=anchor_owner.get(anchor_id))
        for anchor_id in quest_anchors
    ]

    return QuestsPayload(
        quest_log=log_entries,
        quest_anchors=anchor_entries,
        active_stakes=active_stakes,
    )
