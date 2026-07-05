"""Story 162-2 (RED) — one roster resolver; aliases are display, never identity.

Survey §4 conflict #7: every seam matches names its own way — the seater's
``by_name`` dict is EXACT match (``encounter_lifecycle.py``), the MM uses
``.lower()``, narration_apply adds casefold / comma-inverted / ``invented_from``
legs. A prose rename that one seam resolves, another forks. AC2/AC3 pin ONE
shared lookup and the alias-ledger invariant (aliases never move identity).

THE CONTRACT THIS SUITE PINS (the test IS the spec):

    # sidequest.game.origin  (same NET-NEW module as test_162_2_origin_model)

    def resolve_roster_npc(npcs: Sequence[Npc], name: str) -> Npc | None
        # THE single roster lookup every identity seam shares. Resolution
        # order (first hit wins):
        #   1. canonical name   (normalize_name on both sides)
        #   2. alias ledger     (npc.aliases, normalized)
        #   3. invented_from    (the perseus original→mint binding)
        # Blank/unknown -> None. Canonical outranks another entity's alias.
        #
        # OTEL (AC5, the lie-detector): a hit through leg 2 or 3 emits
        # SPAN_IDENTITY_RESOLVED — the engine ASSERTING "prose name X is
        # entity Y" — with attrs query / canonical / via. An exact canonical
        # hit is not a derivation and emits nothing (no span spam).

    # sidequest.telemetry.spans
    SPAN_IDENTITY_RESOLVED = "identity.resolved"

Alias-ledger invariant (AC3): recording a prose name via the EXISTING
``alias_accretion.accrete_npc_aliases`` ledger changes how an entity is FOUND,
never WHO it is — ``identity_key`` is bit-identical before and after.

RED today: ``sidequest.game.origin`` does not exist. Net-new symbols are
imported INSIDE each test so collection survives. Span assertions filter the
in-memory exporter by span name (the established otel_capture pattern).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.session import Npc

_IDENTITY_RESOLVED_SPAN = "identity.resolved"


@pytest.fixture
def otel_capture() -> Iterator[InMemorySpanExporter]:
    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


def _npc(
    name: str,
    *,
    aliases: list[str] | None = None,
    invented_from: str | None = None,
    creature_id: str | None = None,
) -> Npc:
    return Npc(
        core=CreatureCore(
            name=name,
            description="x",
            personality="x",
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        aliases=aliases or [],
        invented_from=invented_from,
        creature_id=creature_id,
    )


# ---------------------------------------------------------------------------
# AC2 — the one shared lookup
# ---------------------------------------------------------------------------


class TestResolveRosterNpc:
    def test_resolves_exact_canonical_name(self) -> None:
        from sidequest.game.origin import resolve_roster_npc

        thief = _npc("Thief", creature_id="thief")
        assert resolve_roster_npc([thief], "Thief") is thief

    def test_resolves_casefold_and_whitespace_variant(self) -> None:
        """One normalization (normalize_name) — the seam-divergent
        exact-vs-casefold split dies here."""
        from sidequest.game.origin import resolve_roster_npc

        thief = _npc("Molgrath the Eyeless")
        assert resolve_roster_npc([thief], "  molgrath  THE eyeless ") is thief

    def test_resolves_through_alias_ledger(self) -> None:
        """The fork-killer leg: the narrator's prose name, once recorded as an
        alias, resolves to the canonical entity instead of minting a twin."""
        from sidequest.game.origin import resolve_roster_npc

        thief = _npc("Thief", creature_id="thief", aliases=["Molgrath the Eyeless"])
        assert resolve_roster_npc([thief], "Molgrath the Eyeless") is thief

    def test_resolves_through_invented_from(self) -> None:
        """The perseus double-mint binding: re-narrating the ORIGINAL invented
        name ("Varra") re-cites the minted entity ("Rifenna Muse")."""
        from sidequest.game.origin import resolve_roster_npc

        muse = _npc("Rifenna Muse", invented_from="Varra")
        assert resolve_roster_npc([muse], "Varra") is muse

    def test_unknown_name_returns_none(self) -> None:
        from sidequest.game.origin import resolve_roster_npc

        assert resolve_roster_npc([_npc("Thief")], "Hold-Dead") is None

    def test_blank_name_returns_none(self) -> None:
        """A blank query is unanswerable, not an accidental match against a
        blank-normalized alias (paranoia: '' must not casefold-match '')."""
        from sidequest.game.origin import resolve_roster_npc

        assert resolve_roster_npc([_npc("Thief")], "   ") is None

    def test_empty_roster_returns_none(self) -> None:
        from sidequest.game.origin import resolve_roster_npc

        assert resolve_roster_npc([], "Thief") is None

    def test_canonical_name_outranks_another_entities_alias(self) -> None:
        """Two entities: B's alias collides with A's canonical name. The
        canonical owner wins — an alias may never SHADOW a real entity."""
        from sidequest.game.origin import resolve_roster_npc

        real_king = _npc("The Pale King", creature_id="pale_king")
        pretender = _npc("Vellum Ghast", creature_id="ghast", aliases=["The Pale King"])
        # Order-independence paranoia: the pretender listed FIRST must still lose.
        assert resolve_roster_npc([pretender, real_king], "The Pale King") is real_king

    def test_resolves_diacritic_name_from_ascii_query(self) -> None:
        """RED (rework, review [MEDIUM]/[RULE]): the culture namer mints
        "Veyra Solnë"; the player types "veyra solne". casefold alone leaves
        the ë — the resolver misses and the seater mints a stub twin, the
        exact double-mint this module exists to kill. normalize_name must
        fold diacritics (compose foundation.slug_fold.fold_to_ascii)."""
        from sidequest.game.origin import resolve_roster_npc

        veyra = _npc("Veyra Solnë")
        assert resolve_roster_npc([veyra], "veyra solne") is veyra

    def test_shared_alias_resolves_first_in_roster_order(self) -> None:
        """CONTRACT GUARD (rework, review [MEDIUM]) — green on arrival; pins
        deterministic behavior. Two DIFFERENT npcs each carry the alias "the
        Butcher": resolution is first-in-roster-order, both directions, so
        the tiebreak is a defined contract rather than accidental — and a
        future ambiguity-signal design change must consciously break THIS
        test, not silently change bindings."""
        from sidequest.game.origin import resolve_roster_npc

        cultist_a = _npc("Marrow Djen", aliases=["the Butcher"])
        cultist_b = _npc("Ilse Varn", aliases=["the Butcher"])
        assert resolve_roster_npc([cultist_a, cultist_b], "the Butcher") is cultist_a
        assert resolve_roster_npc([cultist_b, cultist_a], "the Butcher") is cultist_b


# ---------------------------------------------------------------------------
# AC5 — identity derivation is observable (the lie-detector)
# ---------------------------------------------------------------------------


class TestIdentityResolvedSpan:
    def test_span_name_pinned(self) -> None:
        from sidequest.telemetry.spans import SPAN_IDENTITY_RESOLVED

        assert SPAN_IDENTITY_RESOLVED == _IDENTITY_RESOLVED_SPAN

    def test_alias_hit_emits_identity_resolved_span(
        self, otel_capture: InMemorySpanExporter
    ) -> None:
        """The engine claiming "Molgrath the Eyeless IS the Thief" is a
        subsystem decision — the GM panel must see it or it's improv."""
        from sidequest.game.origin import resolve_roster_npc

        thief = _npc("Thief", creature_id="thief", aliases=["Molgrath the Eyeless"])
        resolve_roster_npc([thief], "Molgrath the Eyeless")

        spans = [s for s in otel_capture.get_finished_spans() if s.name == _IDENTITY_RESOLVED_SPAN]
        assert len(spans) == 1
        attrs = dict(spans[0].attributes or {})
        assert attrs["query"] == "Molgrath the Eyeless"
        assert attrs["canonical"] == "Thief"
        assert attrs["via"] == "alias"

    def test_invented_from_hit_emits_span_with_via(
        self, otel_capture: InMemorySpanExporter
    ) -> None:
        from sidequest.game.origin import resolve_roster_npc

        muse = _npc("Rifenna Muse", invented_from="Varra")
        resolve_roster_npc([muse], "Varra")

        spans = [s for s in otel_capture.get_finished_spans() if s.name == _IDENTITY_RESOLVED_SPAN]
        assert len(spans) == 1
        assert dict(spans[0].attributes or {})["via"] == "invented_from"

    def test_exact_canonical_hit_emits_no_span(self, otel_capture: InMemorySpanExporter) -> None:
        """An exact-name hit derives nothing — spamming the panel on every
        roster lookup would drown the real assertions."""
        from sidequest.game.origin import resolve_roster_npc

        thief = _npc("Thief")
        resolve_roster_npc([thief], "Thief")

        assert [
            s for s in otel_capture.get_finished_spans() if s.name == _IDENTITY_RESOLVED_SPAN
        ] == []

    def test_miss_emits_no_span(self, otel_capture: InMemorySpanExporter) -> None:
        from sidequest.game.origin import resolve_roster_npc

        resolve_roster_npc([_npc("Thief")], "Hold-Dead")

        assert [
            s for s in otel_capture.get_finished_spans() if s.name == _IDENTITY_RESOLVED_SPAN
        ] == []


# ---------------------------------------------------------------------------
# AC3 — aliases are display-layer: the ledger never moves identity
# ---------------------------------------------------------------------------


class TestAliasesNeverMoveIdentity:
    def test_alias_accretion_leaves_identity_key_unchanged(self) -> None:
        """Record the prose rename in the EXISTING ledger
        (``accrete_npc_aliases``); ``identity_key`` must be bit-identical
        before and after — the alias changed how the entity is FOUND, not
        WHO it is."""
        from sidequest.game.alias_accretion import accrete_npc_aliases
        from sidequest.game.origin import derive_origin, identity_key

        thief = _npc("Thief", creature_id="thief")
        thief.manual_origin = True
        key_before = identity_key(derive_origin(thief), thief.core.name)

        accrete_npc_aliases(thief, ["Molgrath the Eyeless"], turn=7)

        key_after = identity_key(derive_origin(thief), thief.core.name)
        assert key_before == key_after
        assert "Molgrath the Eyeless" in thief.aliases

    def test_accreted_alias_makes_entity_resolvable_without_new_identity(self) -> None:
        """End-to-end AC3: accrete → resolve. One entity answers to both
        names; the roster never grows."""
        from sidequest.game.alias_accretion import accrete_npc_aliases
        from sidequest.game.origin import resolve_roster_npc

        thief = _npc("Thief", creature_id="thief")
        roster = [thief]
        accrete_npc_aliases(thief, ["Molgrath the Eyeless"], turn=7)

        assert resolve_roster_npc(roster, "Molgrath the Eyeless") is thief
        assert resolve_roster_npc(roster, "Thief") is thief
        assert len(roster) == 1
