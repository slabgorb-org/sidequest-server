"""Story 103-2 RED — apply_stock: ONE generic application path.

Build plan §D-B / story AC2: a stocks.yaml fixture with ARBITRARY trait
values applies correctly to the created character — attrs, Move, AC,
trauma target, granted mutations on sheet. The engine reads the schema,
never the stock's name: if Synthetic needs ``if stock == "synthetic"``,
the schema is wrong (epic guardrail, verbatim).

Contract pinned here:

  - ``apply_stock(character, state, catalog, registry, *, actor, stock_id,
    session_id, saints=None, saint_id=None) -> CharacterMutationState``
  - attr_mods delta ``character.stats``; ``ac``/``move`` are overrides
    applied when set (None = hook absent, character untouched);
    ``trauma_target_mod`` lands on the creature core. Move and trauma
    target become GENERIC creature fields (story-context assumption:
    "if not [reachable], the hook is added generically").
  - granted_mutations land on the mutation sheet at ZERO MP cost — a
    stock's gifts are birthright, not purchases. Pinned as a property:
    grant count must not move ``mp_remaining``.
  - Saint affinity layering (AC4): ``saint_id`` rides the SAME call and
    prices through 103-1's preset math exactly once — no double-pricing,
    no second economy. Refused loudly when the stock doesn't allow it
    (``saint_affinity_allowed: false``) or no registry is supplied.
  - Idempotent per actor: re-entrant chargen confirm must not re-add
    attr mods or re-grant mutations (the stat double-apply is the
    nastiest failure shape — +1 STR becoming +2 on a reconnect).
  - OTEL (build plan D-D): ``awn.stock.applied`` fires once with stock id
    + applied trait deltas, and is routed for the GM panel.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.mutation.models import (
    MpEconomy,
    MutationCatalog,
    NegativeMutationDef,
    PositiveMutationDef,
    StigmaTables,
)
from sidequest.mutation.saints import SaintDef, SaintRegistry
from sidequest.mutation.state import MutationState
from sidequest.mutation.stocks import StockDef, StockRegistry, apply_stock
from sidequest.telemetry import spans as spans_module

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _catalog() -> MutationCatalog:
    return MutationCatalog(
        mp_economy=MpEconomy(mutant_classes=["Mutant"]),
        stigma=StigmaTables(body_part=["a"] * 6, nature=["b"] * 6, flavor=["c"] * 12),
        negatives=[
            NegativeMutationDef(
                id="negative/test_obsessive",
                name="Obsessive Monologue",
                roll_range=(1, 100),
                effect="cannot stop telling a story once begun",
            ),
        ],
        positives=[
            PositiveMutationDef(
                id="hybrid/test_crushing_jaws",
                name="Crushing Jaws",
                category="hybrid",
                effect="bite",
            ),
            PositiveMutationDef(
                id="structure/test_thick_hide",
                name="Thick Hide",
                category="structure",
                effect="natural armor",
            ),
            PositiveMutationDef(
                id="sense/test_deep_sight",
                name="Deep-Pressure Sight",
                category="sense",
                effect="see in the deep",
            ),
        ],
    )


_ANIMAL_TRAITS = dict(
    attr_mods={"STR": 1, "WIS": -1},
    move=12,
    ac=14,
    trauma_target_mod=1,
    granted_mutations=["hybrid/test_crushing_jaws"],
    saint_affinity_allowed=True,
)


def _registry() -> StockRegistry:
    return StockRegistry(
        stocks=[
            StockDef(id="harbor_seal", name="Harbor Seal Uplift", **_ANIMAL_TRAITS),
            # The SAME trait values under an arbitrary id — the schema-driven
            # twin for the zero-special-cases property test.
            StockDef(id="zz_arbitrary_stock", name="Arbitrary", **_ANIMAL_TRAITS),
            # Sleeper shape: every hook inert (AC3's mutation half).
            StockDef(id="sleeper", name="Sleeper"),
            # Two grants, otherwise inert — for the grants-are-free property.
            StockDef(
                id="two_gift_stock",
                name="Two Gifts",
                granted_mutations=[
                    "hybrid/test_crushing_jaws",
                    "structure/test_thick_hide",
                ],
            ),
        ]
    )


def _saints() -> SaintRegistry:
    return SaintRegistry(
        saints=[
            SaintDef(
                id="herman_of_the_acushnet",
                name="Saint Herman of the Acushnet",
                tradition="literary",
                bundle=["sense/test_deep_sight", "structure/test_thick_hide"],
                drawback="negative/test_obsessive",
            )
        ]
    )


def _character() -> Character:
    return Character(
        core=CreatureCore(name="Pup", description="seal-kin", personality="loyal"),
        backstory="raised by the tide",
        char_class="Mutant",
        race="uplift",
        stats={"STR": 10, "DEX": 9, "WIS": 8},
    )


def _apply(
    character: Character,
    state: MutationState,
    *,
    stock_id: str = "harbor_seal",
    actor: str = "Pup",
    saints: SaintRegistry | None = None,
    saint_id: str | None = None,
):
    return apply_stock(
        character,
        state,
        _catalog(),
        _registry(),
        actor=actor,
        stock_id=stock_id,
        session_id="stock-apply-test",
        saints=saints,
        saint_id=saint_id,
    )


@pytest.fixture
def span_exporter(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """In-memory exporter through spans_module.tracer — the seam every
    production emit site resolves (pattern: test_saint_preset.py)."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test.stock_apply")
    monkeypatch.setattr(spans_module, "tracer", lambda: tracer)
    return exporter


# ---------------------------------------------------------------------------
# AC2 — generic trait application
# ---------------------------------------------------------------------------


class TestGenericApplication:
    def test_attr_mods_delta_stats(self) -> None:
        character = _character()
        _apply(character, MutationState())
        assert character.stats["STR"] == 11
        assert character.stats["WIS"] == 7
        assert character.stats["DEX"] == 9  # unlisted attr untouched

    def test_ac_and_move_overrides_apply(self) -> None:
        character = _character()
        _apply(character, MutationState())
        assert character.core.armor_class == 14
        assert character.core.move == 12

    def test_trauma_target_mod_lands_on_core(self) -> None:
        character = _character()
        _apply(character, MutationState())
        assert character.core.trauma_target_mod == 1

    def test_inert_hooks_touch_nothing(self) -> None:
        """Sleeper: every hook at its default. AC stays the unarmored 10,
        move stays engine-default (None), trauma mod 0, stats unmoved —
        absence of a hook is data, not a code path."""
        character = _character()
        _apply(character, MutationState(), stock_id="sleeper")
        assert character.stats == {"STR": 10, "DEX": 9, "WIS": 8}
        assert character.core.armor_class == 10
        assert character.core.move is None
        assert character.core.trauma_target_mod == 0

    def test_granted_mutations_on_sheet(self) -> None:
        state = MutationState()
        cs = _apply(_character(), state)
        assert cs.positive_ids == ["hybrid/test_crushing_jaws"]
        assert cs.negative_ids == []
        assert "hybrid/test_crushing_jaws" in cs.acquisition_log
        assert "Pup" in state.characters

    def test_sleeper_yields_zero_mutations(self) -> None:
        """AC3 (mutation half): picking Sleeper yields zero mutations —
        the implant economy is items + System Strain, never the catalog."""
        cs = _apply(_character(), MutationState(), stock_id="sleeper")
        assert cs.positive_ids == []
        assert cs.negative_ids == []

    def test_grants_are_free_mp_independent_of_grant_count(self) -> None:
        """Stock gifts are birthright, not purchases: zero grants and two
        grants leave the SAME mp_remaining. This is the property that proves
        grants never route through MP spending."""
        cs_none = _apply(_character(), MutationState(), stock_id="sleeper")
        cs_two = _apply(_character(), MutationState(), stock_id="two_gift_stock")
        assert cs_none.mp_remaining == cs_two.mp_remaining

    def test_schema_driven_not_name_driven(self) -> None:
        """Zero per-stock special cases (AC8): two stocks with identical
        trait values but different ids produce IDENTICAL application results.
        If this fails, somewhere the engine read the name."""
        char_a, char_b = _character(), _character()
        state_a, state_b = MutationState(), MutationState()
        cs_a = _apply(char_a, state_a, stock_id="harbor_seal")
        cs_b = _apply(char_b, state_b, stock_id="zz_arbitrary_stock")
        assert char_a.stats == char_b.stats
        assert char_a.core.armor_class == char_b.core.armor_class
        assert char_a.core.move == char_b.core.move
        assert char_a.core.trauma_target_mod == char_b.core.trauma_target_mod
        assert cs_a.positive_ids == cs_b.positive_ids
        assert cs_a.mp_remaining == cs_b.mp_remaining

    def test_unknown_stock_raises_keyerror(self) -> None:
        with pytest.raises(KeyError) as exc_info:
            _apply(_character(), MutationState(), stock_id="synthetic")
        assert "synthetic" in str(exc_info.value)

    def test_idempotent_no_stat_double_apply(self) -> None:
        """A re-entrant chargen confirm must not re-add attr mods — +1 STR
        becoming +2 on reconnect is the nastiest shape of this bug. Mirrors
        apply_saint_preset's actor-presence guard."""
        character = _character()
        state = MutationState()
        first = _apply(character, state)
        first_positives = list(first.positive_ids)
        second = _apply(character, state)
        assert character.stats["STR"] == 11  # not 12
        assert second.positive_ids == first_positives


# ---------------------------------------------------------------------------
# AC4 — Saint affinity layering (one economy, priced once)
# ---------------------------------------------------------------------------


class TestSaintLayering:
    def test_layered_saint_prices_exactly_once(self) -> None:
        """Stock grants are free; the layered Saint bundle prices through
        103-1's preset math exactly once: 2 (base) + 2 (drawback) - 2 marks
        x 1 (random rate) = 2 MP banked. Positives = stock grant + bundle;
        the drawback is the only negative."""
        state = MutationState()
        cs = _apply(
            _character(),
            state,
            stock_id="harbor_seal",
            saints=_saints(),
            saint_id="herman_of_the_acushnet",
        )
        assert set(cs.positive_ids) == {
            "hybrid/test_crushing_jaws",
            "sense/test_deep_sight",
            "structure/test_thick_hide",
        }
        assert cs.negative_ids == ["negative/test_obsessive"]
        assert cs.mp_remaining == 2

    def test_drawback_lands_before_gifts_in_log(self) -> None:
        """AWN p.16 ordering survives the layering — burdens before gifts."""
        cs = _apply(
            _character(),
            MutationState(),
            stock_id="harbor_seal",
            saints=_saints(),
            saint_id="herman_of_the_acushnet",
        )
        assert cs.acquisition_log[0] == "negative/test_obsessive"

    def test_saint_refused_when_stock_disallows(self) -> None:
        """Sleeper has saint_affinity_allowed: false — a saint_id with it is
        an authoring/UI error, refused loudly naming the stock."""
        with pytest.raises(ValueError) as exc_info:
            _apply(
                _character(),
                MutationState(),
                stock_id="sleeper",
                saints=_saints(),
                saint_id="herman_of_the_acushnet",
            )
        assert "sleeper" in str(exc_info.value)

    def test_saint_id_without_registry_fails_loud(self) -> None:
        with pytest.raises(ValueError):
            _apply(
                _character(),
                MutationState(),
                stock_id="harbor_seal",
                saints=None,
                saint_id="herman_of_the_acushnet",
            )


# ---------------------------------------------------------------------------
# AC6 / build plan D-D — awn.stock.applied, the lie-detector span
# ---------------------------------------------------------------------------


class TestStockAppliedSpan:
    def test_span_fires_with_trait_deltas(self, span_exporter: InMemorySpanExporter) -> None:
        _apply(_character(), MutationState())
        spans = [s for s in span_exporter.get_finished_spans() if s.name == "awn.stock.applied"]
        assert spans, (
            "awn.stock.applied must fire on stock application; captured: "
            f"{[s.name for s in span_exporter.get_finished_spans()]}"
        )
        attrs = spans[0].attributes or {}
        assert attrs.get("actor") == "Pup"
        assert attrs.get("stock_id") == "harbor_seal"
        # The applied trait deltas must be auditable from the GM panel alone:
        assert attrs.get("ac") == 14
        assert attrs.get("move") == 12
        assert attrs.get("trauma_target_mod") == 1
        assert attrs.get("granted_count") == 1
        # attr_mods serialize to a string attribute (OTEL attrs are flat);
        # the GM must be able to read which attrs moved and by how much.
        assert "STR" in str(attrs.get("attr_mods"))

    def test_span_not_refired_on_idempotent_replay(
        self, span_exporter: InMemorySpanExporter
    ) -> None:
        character = _character()
        state = MutationState()
        _apply(character, state)
        _apply(character, state)
        spans = [s for s in span_exporter.get_finished_spans() if s.name == "awn.stock.applied"]
        assert len(spans) == 1

    def test_span_route_registered_for_gm_panel(self) -> None:
        """A span the WatcherHub can't route never reaches the GM panel."""
        from sidequest.telemetry.spans._core import SPAN_ROUTES

        assert "awn.stock.applied" in SPAN_ROUTES


# ---------------------------------------------------------------------------
# Review rework (103-2 review finding [MEDIUM][EDGE]): trait application
# must be atomic — validate every attr key BEFORE mutating any. The
# per-attr-during-mutation check left a partially-mutated character when a
# later key was unknown (a corrupting trap for any non-confirm caller).
# ---------------------------------------------------------------------------


class TestAtomicAttrApplication:
    def test_unknown_attr_leaves_stats_untouched(self) -> None:
        """attr_mods {STR: 1, ZZZ: 1}: the loud ValueError must fire with
        ZERO prior mutation — STR stays 10, not 11."""
        registry = StockRegistry(
            stocks=[
                StockDef(
                    id="bad_attr_stock",
                    name="Bad Attr",
                    attr_mods={"STR": 1, "ZZZ": 1},
                )
            ]
        )
        character = _character()
        state = MutationState()
        with pytest.raises(ValueError) as exc_info:
            apply_stock(
                character,
                state,
                _catalog(),
                registry,
                actor="Pup",
                stock_id="bad_attr_stock",
                session_id="stock-apply-test",
            )
        message = str(exc_info.value)
        assert "bad_attr_stock" in message
        assert "ZZZ" in message
        assert character.stats == {"STR": 10, "DEX": 9, "WIS": 8}, (
            "partial attr application — validation must complete before any mutation"
        )
        assert "Pup" not in state.characters, "no state may register on a failed apply"
