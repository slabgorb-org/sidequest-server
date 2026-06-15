"""Quest-lore coherence projection — Story 117-5 (ADR-053 + ADR-100 + ADR-146).

RED-phase contract (TEA). Keith's symptom: "knowledge has multiple references
but nothing pulls them into a coherent picture." Persisted lore the party has
learned that *relates to* an active quest must surface UNDER that quest in the
QUESTS projection, so the player sees "what I've learned about this job" rather
than scattered, un-attributed references.

THE JOIN MECHANISM (the design crux, settled against the real data structures):

    QuestEntry.anchor_id          (the body/location/NPC id the objective hangs on)
      ──matched against──>  ClueNode.locations[] / ClueNode.implicates[]
                            (genre/models/scenario.py — clue ids that touch that body)
      ──which yields the clue id──>  KnownFact.fact_id == clue_id
                            (character.py — for ScenarioClue facts, 50-14 sets
                             fact_id = clue id; source == "ScenarioClue")

This is a fully STRUCTURAL chain — anchor id → clue → known-fact — over fields
that already carry these ids. It is deterministic and refactor-stable (no fuzzy
text/embedding match). The accusation walk
(server/dispatch/scenario_accusation.py) already recovers clue → fact; this is
the inverse projection, surfaced on the player-facing QUESTS payload.

These tests import a symbol that does not exist yet — they FAIL for
feature-absence until Dev (117-5 GREEN) enriches the quest projection so each
``QuestLogEntry`` carries the related lore the party has learned about its
anchor. The exact carrier field name is Dev's call; these tests assert the
*behaviour* (related lore surfaces under its quest, unrelated lore does not),
and access it through a single helper ``_lore_under(payload, quest_id)`` so the
field name lives in one place.

Run serially:  uv run pytest -n0 tests/server/test_quest_lore_coherence.py
"""

from __future__ import annotations

from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import sidequest.telemetry.spans as _spans_module
from sidequest.game.character import Character, KnownFact
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.projection.quests import build_quests_payload
from sidequest.game.scenario_state import ScenarioState
from sidequest.game.session import GameSnapshot, QuestEntry
from sidequest.genre.models.scenario import ClueGraph, ClueNode
from sidequest.protocol.messages import QuestsMessage
from sidequest.server.websocket_handlers.quests_emit import _maybe_emit_quests

# ---------------------------------------------------------------------------
# Fixture builders (synthetic only — no content-pack dependency)
# ---------------------------------------------------------------------------


def _clue_node(
    node_id: str,
    *,
    locations: list[str] | None = None,
    implicates: list[str] | None = None,
) -> ClueNode:
    """A clue node touching one or more body/NPC ids via locations/implicates.

    ``locations``/``implicates`` are the structural link a quest's ``anchor_id``
    matches against to decide a clue (and its discovered KnownFact) "relates to"
    that quest.
    """
    return ClueNode(
        id=node_id,
        type="testimony",
        description=f"clue {node_id}",
        discovery_method="conversation",
        visibility="public",
        locations=locations or [],
        implicates=implicates or [],
    )


def _scenario_clue_fact(*, content: str, clue_id: str, turn: int = 4) -> KnownFact:
    """A KnownFact minted by scenario clue intake (ADR-100 seam B / 50-14):
    ``source='ScenarioClue'`` and ``fact_id`` carrying the originating clue id."""
    return KnownFact(
        content=content,
        confidence="Discovered",
        source="ScenarioClue",
        learned_turn=turn,
        fact_id=clue_id,
    )


def _loose_fact(*, content: str) -> KnownFact:
    """A generic narrator-emitted fact with no structural tie to any anchor
    (default uuid4 fact_id, source != ScenarioClue). Must NOT be mis-attributed
    to any quest."""
    return KnownFact(content=content, confidence="Certain", source="GameEvent")


def _character(name: str, *, known_facts: list[KnownFact] | None = None) -> Character:
    return Character(
        core=CreatureCore(
            name=name,
            description="placeholder",
            personality="stoic",
            inventory=Inventory(),
        ),
        char_class="Fighter",
        race="Human",
        backstory="placeholder",
        known_facts=known_facts or [],
    )


def _snapshot(
    *,
    quests: dict[str, QuestEntry],
    anchors: list[str],
    clue_nodes: list[ClueNode] | None = None,
    known_facts: list[KnownFact] | None = None,
    stakes: str = "",
) -> GameSnapshot:
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        quest_log=quests,
        quest_anchors=anchors,
        active_stakes=stakes,
    )
    snap.characters.append(_character("Rux", known_facts=known_facts))
    if clue_nodes is not None:
        snap.scenario_state = ScenarioState(clue_graph=ClueGraph(nodes=clue_nodes))
    return snap


# ---------------------------------------------------------------------------
# Accessor for the not-yet-existing lore-under-quest carrier.
# Centralised so the carrier field name (Dev's call) lives in exactly one place.
# ---------------------------------------------------------------------------


def _lore_under(payload: Any, quest_id: str) -> list[Any]:
    """Return the related-lore entries the projection surfaced under ``quest_id``.

    Reads the enrichment Dev adds to ``QuestLogEntry`` in 117-5. Tries the
    expected field name first (``related_lore``) and falls back to a couple of
    plausible synonyms so the test asserts *behaviour*, not a bikeshed name —
    but it does require the field to exist (the whole feature), so it raises
    loudly when no carrier is present (RED until GREEN)."""
    entry = next(e for e in payload.quest_log if e.quest_id == quest_id)
    for field in ("related_lore", "learned_lore", "lore", "known_facts"):
        if hasattr(entry, field):
            return list(getattr(entry, field))
    raise AttributeError(
        "QuestLogEntry carries no related-lore field — 117-5 enrichment absent. "
        f"Inspected fields: {list(type(entry).model_fields)}"
    )


def _lore_text(entries: list[Any]) -> set[str]:
    """Pull the human-readable content out of whatever lore-entry shape Dev
    chose (a model with .content, or a bare string)."""
    out: set[str] = set()
    for e in entries:
        out.add(e.content if hasattr(e, "content") else str(e))
    return out


# ---------------------------------------------------------------------------
# (a) related lore surfaces under its quest
# ---------------------------------------------------------------------------


def test_related_lore_surfaces_under_its_quest() -> None:
    """AC-a: an active quest anchored on body X, plus a discovered ScenarioClue
    fact about X (via a clue whose locations include X), surfaces that fact
    UNDER the quest entry — the coherent 'what I've learned about this job'."""
    snap = _snapshot(
        quests={
            "missing_person": QuestEntry(
                title="The Floor-Boss's Missing Person",
                objective="Find who the floor-boss lost in the under-levels.",
                status="active",
                anchor_id="new_kowloon_underlevels",
            )
        },
        anchors=["new_kowloon_underlevels"],
        clue_nodes=[
            _clue_node("scratched_keycard", locations=["new_kowloon_underlevels"]),
        ],
        known_facts=[
            _scenario_clue_fact(
                content="A scratched keycard was dropped in the under-levels.",
                clue_id="scratched_keycard",
            )
        ],
    )

    payload = build_quests_payload(snap)
    lore = _lore_under(payload, "missing_person")
    assert "A scratched keycard was dropped in the under-levels." in _lore_text(lore), (
        "a ScenarioClue fact whose clue touches the quest's anchor must surface under that quest"
    )


def test_related_lore_via_implicated_npc_anchor() -> None:
    """AC-a (variant): the anchor link also fires through ClueNode.implicates,
    not only locations — a quest anchored on an NPC coheres clues that implicate
    that NPC."""
    snap = _snapshot(
        quests={
            "find_informant": QuestEntry(
                title="Track the Informant",
                objective="Find out what Vex knows.",
                status="active",
                anchor_id="vex",
            )
        },
        anchors=["vex"],
        clue_nodes=[_clue_node("vex_alibi", implicates=["vex"])],
        known_facts=[
            _scenario_clue_fact(
                content="Vex's alibi for the night does not hold.",
                clue_id="vex_alibi",
            )
        ],
    )

    payload = build_quests_payload(snap)
    assert "Vex's alibi for the night does not hold." in _lore_text(
        _lore_under(payload, "find_informant")
    )


# ---------------------------------------------------------------------------
# (b) unrelated lore is NOT mis-attributed (no false coherence)
# ---------------------------------------------------------------------------


def test_unrelated_lore_is_not_misattributed() -> None:
    """AC-b: a fact with no structural tie to the quest's anchor (a loose
    narrator fact, and a ScenarioClue fact about a DIFFERENT body) must NOT be
    pulled under the quest. No false coherence."""
    snap = _snapshot(
        quests={
            "missing_person": QuestEntry(
                title="The Floor-Boss's Missing Person",
                objective="Find who the floor-boss lost.",
                status="active",
                anchor_id="new_kowloon_underlevels",
            )
        },
        anchors=["new_kowloon_underlevels"],
        clue_nodes=[
            # clue about a totally different body
            _clue_node("dockside_ledger", locations=["orbital_docks"]),
        ],
        known_facts=[
            _scenario_clue_fact(
                content="A ledger in the orbital docks lists smuggled goods.",
                clue_id="dockside_ledger",
            ),
            _loose_fact(content="The cantina serves terrible coffee."),
        ],
    )

    payload = build_quests_payload(snap)
    lore = _lore_text(_lore_under(payload, "missing_person"))
    assert "A ledger in the orbital docks lists smuggled goods." not in lore, (
        "a clue about a different body must not cohere under this quest"
    )
    assert "The cantina serves terrible coffee." not in lore, (
        "a loose narrator fact with no anchor tie must not be mis-attributed"
    )


# ---------------------------------------------------------------------------
# (c) multiple active quests each get only their own related lore
# ---------------------------------------------------------------------------


def test_multiple_quests_partition_their_lore() -> None:
    """AC-c: two active quests on two different anchors each surface ONLY their
    own related lore — a correct partition, not a merged pile."""
    snap = _snapshot(
        quests={
            "missing_person": QuestEntry(
                title="Missing Person",
                objective="Find them.",
                status="active",
                anchor_id="under_levels",
            ),
            "the_debt": QuestEntry(
                title="The Debt",
                objective="Settle it.",
                status="active",
                anchor_id="loan_office",
            ),
        },
        anchors=["under_levels", "loan_office"],
        clue_nodes=[
            _clue_node("keycard", locations=["under_levels"]),
            _clue_node("promissory_note", locations=["loan_office"]),
        ],
        known_facts=[
            _scenario_clue_fact(content="A keycard, scratched.", clue_id="keycard"),
            _scenario_clue_fact(content="A promissory note, forged.", clue_id="promissory_note"),
        ],
    )

    payload = build_quests_payload(snap)
    missing_lore = _lore_text(_lore_under(payload, "missing_person"))
    debt_lore = _lore_text(_lore_under(payload, "the_debt"))

    assert missing_lore == {"A keycard, scratched."}, (
        f"missing_person should carry only its keycard clue, got {missing_lore}"
    )
    assert debt_lore == {"A promissory note, forged."}, (
        f"the_debt should carry only its note clue, got {debt_lore}"
    )


# ---------------------------------------------------------------------------
# (d) a quest with no related lore yet → empty/clean, no crash
# ---------------------------------------------------------------------------


def test_quest_with_no_related_lore_is_clean_empty() -> None:
    """AC-d / No Silent Fallbacks: an active quest with no learned lore about its
    anchor projects a clean empty lore list — no crash, no None, no spillover."""
    snap = _snapshot(
        quests={
            "fresh": QuestEntry(
                title="A Fresh Job",
                objective="Just took it.",
                status="active",
                anchor_id="some_anchor",
            )
        },
        anchors=["some_anchor"],
        clue_nodes=[],  # no clues at all
        known_facts=[],  # nothing learned
    )

    payload = build_quests_payload(snap)
    assert _lore_under(payload, "fresh") == [], (
        "a quest with nothing learned about it must project an empty lore list"
    )


def test_no_scenario_bound_is_clean_empty() -> None:
    """AC-d (variant): a session with no scenario bound at all (the common
    non-mystery case) projects empty lore for its quests and never throws."""
    snap = _snapshot(
        quests={
            "spine": QuestEntry(
                title="Go Home",
                objective="Return to Kansas",
                status="active",
                anchor_id="emerald_city",
            )
        },
        anchors=["emerald_city"],
        clue_nodes=None,  # snapshot.scenario_state stays None
        known_facts=[_loose_fact(content="The road is yellow.")],
    )

    payload = build_quests_payload(snap)
    assert _lore_under(payload, "spine") == [], (
        "no scenario bound → no structural lore link → empty, not a crash"
    )


# ---------------------------------------------------------------------------
# (e) WIRING — the enrichment is in the production emit path the UI consumes
# ---------------------------------------------------------------------------


def test_quests_emitter_broadcasts_lore_end_to_end() -> None:
    """AC-e / WIRING: drive the REAL production emitter (``_maybe_emit_quests``,
    the function ``websocket_session_handler`` calls each turn) and prove the
    QUESTS message that reaches the UI carries the related lore under its quest —
    not a helper tested in isolation. This is the integration test the suite
    requires (CLAUDE.md: every test suite needs a wiring test)."""

    class _Handler:
        pass

    snap = _snapshot(
        quests={
            "missing_person": QuestEntry(
                title="The Floor-Boss's Missing Person",
                objective="Find who the floor-boss lost.",
                status="active",
                anchor_id="under_levels",
            )
        },
        anchors=["under_levels"],
        clue_nodes=[_clue_node("keycard", locations=["under_levels"])],
        known_facts=[
            _scenario_clue_fact(
                content="A scratched keycard, dropped in the under-levels.",
                clue_id="keycard",
            )
        ],
        stakes="A corporate favour owed.",
    )

    sent: list[QuestsMessage] = []
    _maybe_emit_quests(
        _Handler(),
        snapshot=snap,
        emit_fn=lambda m, k: sent.append(m),
    )

    assert len(sent) == 1, "the populated spine must broadcast exactly one QUESTS message"
    msg = sent[0]
    assert isinstance(msg, QuestsMessage)
    lore = _lore_under(msg.payload, "missing_person")
    assert "A scratched keycard, dropped in the under-levels." in _lore_text(lore), (
        "the QUESTS payload on the wire must carry the related lore under its "
        "quest — proving the enrichment lives in build_quests_payload, the real "
        "projection the UI consumes, not in an unwired helper"
    )


# ---------------------------------------------------------------------------
# Regression guards (Hermes REJECT round) — empty-anchor wildcard + source
# filter + OTEL lore_count parity.
# ---------------------------------------------------------------------------


def test_empty_anchor_coheres_nothing_not_a_wildcard() -> None:
    """REGRESSION (blocking fix): a quest whose ``anchor_id`` is the empty string
    must cohere NOTHING — an empty anchor is reachable (``quest_offer.mint``
    stores ``anchor_id=seed.anchor`` even when the seed anchor is "", and a
    narrator ``record_quest`` can emit one). It must NEVER act as a wildcard that
    pulls clues keyed under "" (``locations=[""]``) under the wrong quest."""
    snap = _snapshot(
        quests={
            "anchorless": QuestEntry(
                title="An Anchorless Job",
                objective="No body pinned yet.",
                status="active",
                anchor_id="",  # empty — must match nothing
            )
        },
        anchors=[""],
        clue_nodes=[_clue_node("orphan_clue", locations=[""])],
        known_facts=[
            _scenario_clue_fact(
                content="A clue with an empty location key.",
                clue_id="orphan_clue",
            )
        ],
    )

    payload = build_quests_payload(snap)
    assert _lore_under(payload, "anchorless") == [], (
        "an empty-string anchor must cohere nothing — it must not act as a "
        "wildcard pulling clues keyed under the empty string"
    )


def test_non_scenario_fact_with_colliding_id_does_not_surface() -> None:
    """REGRESSION (source-filter discriminator): a generic ``GameEvent`` fact
    whose ``fact_id`` happens to collide with a real clue id must NOT surface
    under the quest. Only ``source=='ScenarioClue'`` facts cohere; the
    ``fact_id == clue_id`` recovery is gated on source, not on id alone."""
    snap = _snapshot(
        quests={
            "missing_person": QuestEntry(
                title="The Floor-Boss's Missing Person",
                objective="Find who the floor-boss lost.",
                status="active",
                anchor_id="under_levels",
            )
        },
        anchors=["under_levels"],
        clue_nodes=[_clue_node("keycard", locations=["under_levels"])],
        known_facts=[
            # source=='GameEvent', but fact_id collides with the clue id.
            KnownFact(
                content="A narrator aside that merely shares the clue's id.",
                confidence="Certain",
                source="GameEvent",
                fact_id="keycard",
            )
        ],
    )

    payload = build_quests_payload(snap)
    lore = _lore_text(_lore_under(payload, "missing_person"))
    assert "A narrator aside that merely shares the clue's id." not in lore, (
        "a non-ScenarioClue fact must not cohere even when its fact_id collides "
        "with a clue id — the join is source-gated, not id-only"
    )


class _SpanCapture:
    def __init__(self, exporter: InMemorySpanExporter) -> None:
        self._exporter = exporter

    @property
    def spans(self) -> list[Any]:
        return list(self._exporter.get_finished_spans())


@pytest.fixture
def capture_spans(monkeypatch: Any) -> _SpanCapture:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    local = provider.get_tracer("test-quest-lore")
    monkeypatch.setattr(_spans_module, "tracer", lambda: local)
    return _SpanCapture(exporter)


def test_quests_emitted_span_carries_lore_count(capture_spans: _SpanCapture) -> None:
    """REGRESSION (OTEL parity): the ``quests.emitted`` span must carry a
    ``lore_count`` attribute so the GM-panel lie-detector can verify the
    coherence projection engaged. Without this, a lore_count regression would
    pass silently (CLAUDE.md OTEL discipline)."""

    class _Handler:
        pass

    snap = _snapshot(
        quests={
            "missing_person": QuestEntry(
                title="The Floor-Boss's Missing Person",
                objective="Find who the floor-boss lost.",
                status="active",
                anchor_id="under_levels",
            )
        },
        anchors=["under_levels"],
        clue_nodes=[_clue_node("keycard", locations=["under_levels"])],
        known_facts=[
            _scenario_clue_fact(
                content="A scratched keycard, dropped in the under-levels.",
                clue_id="keycard",
            )
        ],
        stakes="A corporate favour owed.",
    )

    _maybe_emit_quests(_Handler(), snapshot=snap, emit_fn=lambda m, k: None)

    emitted = [s for s in capture_spans.spans if s.name == "quests.emitted"]
    assert emitted, "expected a 'quests.emitted' span to fire on projection emit"
    attrs = dict(emitted[0].attributes or {})
    assert "lore_count" in attrs, (
        "the quests.emitted span must carry a 'lore_count' attribute for the "
        "GM panel to verify the coherence projection engaged"
    )
    assert attrs["lore_count"] == 1, (
        f"lore_count should count the one cohered fragment, got {attrs['lore_count']}"
    )
