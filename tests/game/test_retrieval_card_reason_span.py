"""Story 84-4 (WI-6) — retrieval.card.reason span emission (RED phase).

ADR-118 §A5 extends the §D5 ``retrieval.universal`` span with the per-card score
decomposition + the tier-lifecycle SHAPE. 84-1 already put ``retrieval.embed_skipped``
on the span and populates ``card_scores``; WI-6 adds:

  * ``retrieval.card.reason`` — a JSON-encoded list, one entry per SELECTED fill
    card (OTEL attributes cannot hold a list of dicts, so it is JSON-encoded into
    a single string attribute).
  * ``retrieval.tier_demotions`` / ``retrieval.tier_rehydrations`` /
    ``retrieval.vectors_shed`` — the A3 forgetting lifecycle counters, all ``0``
    this turn (the LOGIC is WI-3/84-6; WI-6 emits the honest zero-state shape).
  * ``retrieval.embed_skipped`` + the above added to the
    ``UNIVERSAL_RETRIEVAL_SPAN_ATTRS`` contract set in ``entity_card.py`` (84-1
    emitted embed_skipped but did NOT add it to the contract — WI-6 closes that).

OTEL deadlock hazard (project memory): these are span-count / span-attr tests —
run the FILE with ``-n0``. Each is driven through the real ``retrieve_turn_context``
with an offline fake daemon so the fill is deterministic and no socket is touched.

Test discipline (server CLAUDE.md): assertions are on the EMITTED span, never on
source patterns. Net-new symbols imported inside each test so collection survives.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.creature_core import CreatureCore
from sidequest.game.entity_card import EntityCard, EntityType
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager

_RETRIEVAL_SPAN_NAME = "retrieval.universal"
_VEC: list[float] = [0.11, 0.23, 0.37, 0.41, 0.53, 0.67, 0.71, 0.83]


# ---------------------------------------------------------------------------
# Offline fixtures
# ---------------------------------------------------------------------------


class _FakeDaemon:
    def __init__(self, vector: list[float], *, available: bool = True) -> None:
        self._vector = list(vector)
        self._available = available

    def is_available(self) -> bool:
        return self._available

    async def embed(self, text: str) -> dict[str, Any]:
        return {"embedding": list(self._vector)}


def _npc(name: str, last_seen_turn: int) -> Npc:
    return Npc(
        core=CreatureCore(name=name, description=f"{name} is a test NPC.", personality="stoic"),
        last_seen_turn=last_seen_turn,
    )


def _snap(*, current_turn: int, npcs: list[Npc] | None = None) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=current_turn),
        npcs=npcs or [],
        npc_pool=[],
    )


def _seed_card(store: Any, *, entity_type: str, entity_id: str, content: str) -> EntityCard:
    card = EntityCard.new(entity_type, entity_id, content=content)
    store.add(card)
    store.update_embedding(card.id, list(_VEC))
    return card


def _install_span_exporter(monkeypatch) -> InMemorySpanExporter:
    """Redirect the orchestrator's tracer to an in-memory exporter (mirrors the
    75-8 e2e harness)."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(
        "sidequest.game.retrieval_orchestration.tracer",
        provider.get_tracer("test-84-4-span"),
    )
    return exporter


def _the_span(exporter: InMemorySpanExporter) -> Any:
    spans = [s for s in exporter.get_finished_spans() if s.name == _RETRIEVAL_SPAN_NAME]
    assert len(spans) == 1, f"exactly one {_RETRIEVAL_SPAN_NAME} span per turn; got {len(spans)}"
    assert spans[0].attributes is not None
    return spans[0]


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ===========================================================================
# AC-2 — per-card retrieval.card.reason on the span
# ===========================================================================


class TestCardReasonSpanAttribute:
    def test_span_emits_card_reason_for_each_selected_card(self, monkeypatch) -> None:
        """A successful fill of one card emits a ``retrieval.card.reason``
        attribute: a JSON-encoded list whose single entry decomposes that card."""
        from sidequest.game.entity_store import EntityStore
        from sidequest.game.retrieval_orchestration import retrieve_turn_context

        exporter = _install_span_exporter(monkeypatch)
        store = EntityStore()
        _seed_card(
            store,
            entity_type=EntityType.LOCATION,
            entity_id="black_hart",
            content="The Black Hart — a dockside tavern.",
        )
        snap = _snap(current_turn=10, npcs=[])  # thin action → cosine fill runs
        fake = _FakeDaemon(_VEC)

        result = _run(
            retrieve_turn_context(
                store,
                snap,
                "I wander toward a tavern somewhere",
                current_turn=10,
                player_referenced_npcs=set(),
                client=fake,
            )
        )
        assert result.outcome == "success"

        span = _the_span(exporter)
        raw = span.attributes.get("retrieval.card.reason")
        assert raw is not None, "the span must carry a retrieval.card.reason attribute (§A5)"
        decoded = json.loads(raw)  # JSON-encoded list of per-card dicts
        assert isinstance(decoded, list) and len(decoded) == 1
        entry = decoded[0]
        assert entry["card_id"] == "loc:black_hart"
        # All four signal contributions + score + dominant present.
        for key in ("mention", "here", "recency", "sim", "score", "dominant"):
            assert key in entry, f"card reason entry missing {key!r}"

    def test_span_card_reason_empty_list_on_gate_skip(self, monkeypatch) -> None:
        """On a named/present drama-gate skip there is NO fill — the attribute is
        an empty JSON list, never absent and never malformed (the GM panel must
        always be able to parse it)."""
        from sidequest.game.entity_store import EntityStore
        from sidequest.game.retrieval_orchestration import retrieve_turn_context

        exporter = _install_span_exporter(monkeypatch)
        store = EntityStore()
        snap = _snap(current_turn=10, npcs=[_npc("Borin", 10)])  # scene-present
        fake = _FakeDaemon(_VEC)

        result = _run(
            retrieve_turn_context(
                store,
                snap,
                "I attack Borin",
                current_turn=10,
                player_referenced_npcs={"Borin"},
                client=fake,
            )
        )
        assert result.embed_skipped is True

        span = _the_span(exporter)
        raw = span.attributes.get("retrieval.card.reason")
        assert raw is not None, "card.reason must be present (empty) even on a skip"
        assert json.loads(raw) == [], "no fill → empty JSON list, not absent/malformed"


# ===========================================================================
# AC-5 — tier-lifecycle zero-state counters
# ===========================================================================


class TestTierLifecycleShape:
    def test_span_emits_zero_state_tier_lifecycle_counters(self, monkeypatch) -> None:
        """§A5 names tier_demotions / tier_rehydrations / vectors_shed. The LOGIC
        is WI-3 (84-6); WI-6 emits the SHAPE now — all three present and 0 this
        turn (never absent — an absent counter would be a silent gap)."""
        from sidequest.game.entity_store import EntityStore
        from sidequest.game.retrieval_orchestration import retrieve_turn_context

        exporter = _install_span_exporter(monkeypatch)
        store = EntityStore()
        _seed_card(
            store,
            entity_type=EntityType.LOCATION,
            entity_id="black_hart",
            content="The Black Hart — a dockside tavern.",
        )
        snap = _snap(current_turn=10, npcs=[])
        fake = _FakeDaemon(_VEC)

        _run(
            retrieve_turn_context(
                store,
                snap,
                "wander toward a tavern",
                current_turn=10,
                player_referenced_npcs=set(),
                client=fake,
            )
        )

        span = _the_span(exporter)
        for attr in (
            "retrieval.tier_demotions",
            "retrieval.tier_rehydrations",
            "retrieval.vectors_shed",
        ):
            assert attr in span.attributes, f"lifecycle counter {attr!r} must be emitted"
            assert span.attributes[attr] == 0, (
                f"{attr} must be 0 this turn — WI-6 emits shape, not WI-3 logic"
            )


# ===========================================================================
# AC-6 — span-attribute contract includes the WI-6 attributes
# ===========================================================================


class TestSpanAttrContract:
    def test_span_attr_contract_includes_wi6_attributes(self) -> None:
        """The §D5 contract set must enumerate the WI-6 additions (84-1 emitted
        embed_skipped but never added it to the set)."""
        from sidequest.game.entity_card import UNIVERSAL_RETRIEVAL_SPAN_ATTRS

        for name in (
            "retrieval.embed_skipped",
            "retrieval.card.reason",
            "retrieval.tier_demotions",
            "retrieval.tier_rehydrations",
            "retrieval.vectors_shed",
        ):
            assert name in UNIVERSAL_RETRIEVAL_SPAN_ATTRS, (
                f"{name!r} must be in the UNIVERSAL_RETRIEVAL_SPAN_ATTRS contract"
            )

    def test_live_span_carries_full_attr_contract(self, monkeypatch) -> None:
        """Every name in the (extended) contract set is present on the live span
        — the contract is honest, not aspirational. Explicitly includes the WI-6
        additions so this fails RED until they are emitted (a plain iterate-the-set
        loop would pass vacuously against the un-extended 84-1 set)."""
        from sidequest.game.entity_card import UNIVERSAL_RETRIEVAL_SPAN_ATTRS
        from sidequest.game.entity_store import EntityStore
        from sidequest.game.retrieval_orchestration import retrieve_turn_context

        exporter = _install_span_exporter(monkeypatch)
        store = EntityStore()
        _seed_card(
            store,
            entity_type=EntityType.LOCATION,
            entity_id="black_hart",
            content="The Black Hart — a dockside tavern.",
        )
        snap = _snap(current_turn=10, npcs=[])
        fake = _FakeDaemon(_VEC)

        _run(
            retrieve_turn_context(
                store,
                snap,
                "wander toward a tavern",
                current_turn=10,
                player_referenced_npcs=set(),
                client=fake,
            )
        )

        span = _the_span(exporter)
        # The full contract set must be present...
        missing = {name for name in UNIVERSAL_RETRIEVAL_SPAN_ATTRS if name not in span.attributes}
        assert not missing, f"live span missing contract attributes: {missing}"
        # ...AND the WI-6 additions specifically (pins the new behavior so the test
        # cannot pass against the un-extended set).
        for wi6_attr in (
            "retrieval.embed_skipped",
            "retrieval.card.reason",
            "retrieval.tier_demotions",
            "retrieval.tier_rehydrations",
            "retrieval.vectors_shed",
        ):
            assert wi6_attr in span.attributes, (
                f"live span must carry the WI-6 attribute {wi6_attr!r}"
            )
        # ...AND the REVERSE direction (84-4 review): every retrieval.* attribute the
        # span actually SETS must be DECLARED in the contract set. The forward check
        # above only proves declared ⊆ emitted; without this an emitted-but-undeclared
        # attr (the exact 84-1 bug — embed_skipped was set on the span but never added
        # to UNIVERSAL_RETRIEVAL_SPAN_ATTRS) would pass green. Scoped to retrieval.*
        # so unrelated span attributes don't false-fail the contract.
        emitted_retrieval_attrs = {
            name for name in span.attributes if name.startswith("retrieval.")
        }
        undeclared = emitted_retrieval_attrs - set(UNIVERSAL_RETRIEVAL_SPAN_ATTRS)
        assert not undeclared, (
            "span emits retrieval.* attributes not declared in "
            f"UNIVERSAL_RETRIEVAL_SPAN_ATTRS: {undeclared} — add them to the contract "
            "set (the 84-1 embed_skipped gap class)"
        )
