"""Story 82-8 (ADR-021 track 3): item narrative_weight / WealthTier consumer.

RED phase — these tests fail on current ``develop`` and define the contract
for wiring the ``WealthTier`` gold→label seam, which today is data-only with
**no production consumer** (epic 82, "Surface the Dark Subsystems").

Seam chosen for track 3: ``WealthTier`` gold→label (the cleaner single
consumer of the two alternatives — item ``narrative_weight`` is the other).
The wiring doctrine requires a *production consumer* AND an *OTEL emit* before
the track counts as live, plus a *player-facing* surface for the result
(mechanics-first legibility — Sebastien/Jade want wealth to read as a tier,
not a bare number).

Four ACs, mapped to the test classes below:

- AC-1  pure resolver + boundary values  → ``TestResolveWealthTier``
- AC-3  player-facing protocol field      → ``TestInventoryPayloadWealthTierField``
- AC-2  OTEL emission on application       → ``TestWealthTierWiringThroughViews`` (span)
- AC-4  behavioral wiring test (real path) → ``TestWealthTierWiringThroughViews``

Symbols that do not exist yet (``resolve_wealth_tier``, the
``wealth_tier_label`` field, the ``inventory.wealth_tier`` span) are imported
*inside* each test so a missing symbol fails only that AC, not collection of
the whole file.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.persistence import GameMode
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.progression import WealthTier
from sidequest.protocol.models import InventoryPayload
from sidequest.server import views
from sidequest.server.session_handler import WebSocketSessionHandler, _SessionData

CONTENT_GENRE_PACKS = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


# The mutant_wasteland / road_warrior tier ladder shape: ascending caps, the
# final tier uncapped (``max_gold=None``). A gold value resolves to the first
# tier whose cap still contains it; the boundary value ``gold == max_gold``
# lands in *that* tier (the AC's "gold exactly on a tier boundary" edge).
def _ladder() -> list[WealthTier]:
    return [
        WealthTier(max_gold=0, label="starving"),
        WealthTier(max_gold=10, label="scraping"),
        WealthTier(max_gold=50, label="provisioned"),
        WealthTier(max_gold=200, label="stocked"),
        WealthTier(max_gold=1000, label="connected"),
        WealthTier(max_gold=5000, label="warlord"),
        WealthTier(max_gold=None, label="pre-war rich"),
    ]


# ---------------------------------------------------------------------------
# AC-1: pure gold→tier resolver with boundary semantics
# ---------------------------------------------------------------------------


class TestResolveWealthTier:
    """``resolve_wealth_tier(gold, tiers)`` returns the WealthTier whose cap
    first contains ``gold`` (or the uncapped tier), or None when no tiers are
    authored. This is the mechanical heart and where the boundary edge lives.
    """

    def test_zero_gold_resolves_to_floor_tier(self) -> None:
        from sidequest.genre.models.progression import resolve_wealth_tier

        tier = resolve_wealth_tier(0, _ladder())
        assert tier is not None
        assert tier.label == "starving"

    @pytest.mark.parametrize(
        ("gold", "label"),
        [
            (10, "scraping"),  # exactly on the 10-cap boundary
            (50, "provisioned"),  # exactly on the 50-cap boundary
            (200, "stocked"),  # exactly on the 200-cap boundary
            (5000, "warlord"),  # exactly on the last finite cap
        ],
    )
    def test_gold_exactly_on_boundary_stays_in_that_tier(self, gold: int, label: str) -> None:
        """Boundary edge (AC-1): ``gold == max_gold`` belongs to that tier,
        not the next one up."""
        from sidequest.genre.models.progression import resolve_wealth_tier

        tier = resolve_wealth_tier(gold, _ladder())
        assert tier is not None
        assert tier.label == label

    @pytest.mark.parametrize(
        ("gold", "label"),
        [
            (1, "scraping"),  # one above the 0 floor
            (11, "provisioned"),  # one above the 10 cap
            (51, "stocked"),  # one above the 50 cap
        ],
    )
    def test_gold_one_above_boundary_promotes(self, gold: int, label: str) -> None:
        from sidequest.genre.models.progression import resolve_wealth_tier

        tier = resolve_wealth_tier(gold, _ladder())
        assert tier is not None
        assert tier.label == label

    def test_gold_above_highest_cap_resolves_to_uncapped_tier(self) -> None:
        """Beyond the last finite cap, the ``max_gold=None`` tier catches all."""
        from sidequest.genre.models.progression import resolve_wealth_tier

        tier = resolve_wealth_tier(10_000, _ladder())
        assert tier is not None
        assert tier.label == "pre-war rich"

    def test_negative_gold_resolves_to_floor_tier(self) -> None:
        """Defensive floor (min-value edge): debt/negative gold can't fall off
        the bottom of the ladder — it clamps to the lowest tier, not None."""
        from sidequest.genre.models.progression import resolve_wealth_tier

        tier = resolve_wealth_tier(-5, _ladder())
        assert tier is not None
        assert tier.label == "starving"

    def test_empty_tiers_returns_none(self) -> None:
        """No authored tiers → None (No Silent Fallbacks: never fabricate a
        tier the content author didn't declare)."""
        from sidequest.genre.models.progression import resolve_wealth_tier

        assert resolve_wealth_tier(500, []) is None

    def test_resolver_returns_wealthtier_object_not_label(self) -> None:
        """Return type is the WealthTier (carrying label + description), not a
        bare string — so a consumer can surface description too if it wants."""
        from sidequest.genre.models.progression import resolve_wealth_tier

        tier = resolve_wealth_tier(75, _ladder())
        assert isinstance(tier, WealthTier)
        assert tier.label == "stocked"


# ---------------------------------------------------------------------------
# AC-3: player-facing protocol field on InventoryPayload
# ---------------------------------------------------------------------------


class TestInventoryPayloadWealthTierField:
    """``InventoryPayload`` must carry a nullable ``wealth_tier_label`` so the
    resolved tier reaches the player UI alongside the raw ``gold`` number."""

    def test_inventory_payload_accepts_wealth_tier_label(self) -> None:
        payload = InventoryPayload(items=[], gold=75, wealth_tier_label="stocked")
        assert payload.wealth_tier_label == "stocked"

    def test_inventory_payload_wealth_tier_label_defaults_none(self) -> None:
        """A pack with no wealth tiers leaves the label None — the UI shows
        the bare gold number rather than a fabricated tier."""
        payload = InventoryPayload(items=[], gold=75)
        assert payload.wealth_tier_label is None

    def test_inventory_payload_serializes_wealth_tier_label(self) -> None:
        payload = InventoryPayload(items=[], gold=75, wealth_tier_label="stocked")
        dumped = payload.model_dump(by_alias=True)
        assert dumped["wealth_tier_label"] == "stocked"


# ---------------------------------------------------------------------------
# AC-2 + AC-4: wiring + OTEL through party_member_from_character (real path)
# ---------------------------------------------------------------------------


def _char(name: str, gold: int) -> Character:
    return Character(
        core=CreatureCore(
            name=name,
            description="d",
            personality="p",
            inventory=Inventory(gold=gold),
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        backstory=f"{name}'s tale.",
        char_class="Delver",
        race="Human",
    )


def _sd_with_tiers(
    player_id: str, characters: list[Character], tiers: list[WealthTier]
) -> _SessionData:
    """Session-data fixture with a real pack whose wealth_tiers we control.

    caverns_and_claudes authors no wealth_tiers (defaults to []), so we inject
    a known ladder — the test owns the boundary data instead of coupling to
    whatever a pack happens to author today.
    """
    pack = load_genre_pack(CONTENT_GENRE_PACKS / "caverns_and_claudes")
    pack.progression.wealth_tiers = tiers
    sd = _SessionData(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        player_name=player_id,
        player_id=player_id,
        snapshot=GameSnapshot(
            genre_slug="caverns_and_claudes",
            world_slug="mawdeep",
            turn_manager=TurnManager(interaction=1),
            characters=list(characters),
        ),
        repository=MagicMock(),
        dungeon_repository=MagicMock(),
        telemetry_sink=MagicMock(),
        genre_pack=pack,
        orchestrator=MagicMock(),
        mode=GameMode.MULTIPLAYER,
    )
    # Avoid the world-location resolution path (story 45-48): no per-char
    # location means current_location is omitted, keeping this test focused
    # on the wealth-tier seam.
    sd.snapshot.character_locations = {}
    return sd


class TestWealthTierWiringThroughViews:
    """The production consumer: ``party_member_from_character`` resolves the
    character's gold against the pack's wealth_tiers, surfaces the label on the
    player-facing InventoryPayload, and emits an OTEL span the GM panel reads.
    """

    def test_party_member_inventory_carries_wealth_tier_label(self) -> None:
        """AC-4 wiring: gold=75 against the ladder surfaces "stocked"
        (50 < 75 <= 200) on the player-facing inventory payload."""
        pc = _char("Solo", gold=75)
        sd = _sd_with_tiers("p:solo", [pc], _ladder())
        handler = WebSocketSessionHandler(save_dir=Path("/tmp/sq-test-saves"))

        member = views.party_member_from_character(handler, sd, pc, "p:solo", "Solo")

        assert member.inventory is not None
        assert member.inventory.wealth_tier_label == "stocked"

    def test_zero_gold_member_gets_floor_tier_label(self) -> None:
        """Boundary through the real path: gold=0 surfaces the floor tier."""
        pc = _char("Solo", gold=0)
        sd = _sd_with_tiers("p:solo", [pc], _ladder())
        handler = WebSocketSessionHandler(save_dir=Path("/tmp/sq-test-saves"))

        member = views.party_member_from_character(handler, sd, pc, "p:solo", "Solo")

        assert member.inventory is not None
        assert member.inventory.wealth_tier_label == "starving"

    def test_no_authored_tiers_leaves_label_none(self) -> None:
        """No Silent Fallbacks: a pack with empty wealth_tiers surfaces a None
        label (bare gold number in the UI), never a fabricated default tier."""
        pc = _char("Solo", gold=75)
        sd = _sd_with_tiers("p:solo", [pc], [])
        handler = WebSocketSessionHandler(save_dir=Path("/tmp/sq-test-saves"))

        member = views.party_member_from_character(handler, sd, pc, "p:solo", "Solo")

        assert member.inventory is not None
        assert member.inventory.wealth_tier_label is None

    def test_wealth_tier_resolution_emits_otel_span(self, otel_capture) -> None:  # noqa: ANN001
        """AC-2 (lie-detector): resolving the tier emits an
        ``inventory.wealth_tier`` span carrying the gold and resolved label,
        so the GM panel can confirm the engine engaged rather than the
        narrator improvising a wealth descriptor."""
        pc = _char("Solo", gold=75)
        sd = _sd_with_tiers("p:solo", [pc], _ladder())
        handler = WebSocketSessionHandler(save_dir=Path("/tmp/sq-test-saves"))

        views.party_member_from_character(handler, sd, pc, "p:solo", "Solo")

        spans = [s for s in otel_capture.get_finished_spans() if s.name == "inventory.wealth_tier"]
        assert spans, (
            "expected an 'inventory.wealth_tier' span; saw "
            f"{[s.name for s in otel_capture.get_finished_spans()]}"
        )
        attrs = spans[0].attributes or {}
        assert attrs.get("label") == "stocked"
        assert attrs.get("gold") == 75

    def test_no_authored_tiers_emits_no_wealth_tier_span(self, otel_capture) -> None:  # noqa: ANN001
        """When no tiers are authored there is nothing to resolve — the span
        must NOT fire (a fired span would be the GM-panel lie of a resolution
        that never happened)."""
        pc = _char("Solo", gold=75)
        sd = _sd_with_tiers("p:solo", [pc], [])
        handler = WebSocketSessionHandler(save_dir=Path("/tmp/sq-test-saves"))

        views.party_member_from_character(handler, sd, pc, "p:solo", "Solo")

        spans = [s for s in otel_capture.get_finished_spans() if s.name == "inventory.wealth_tier"]
        assert not spans, f"unexpected wealth-tier span with no authored tiers: {spans}"
