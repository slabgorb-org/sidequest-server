"""RED-phase WIRING tests for Story 76-7 — factions and locations reach the
universal-retrieval index through the PRODUCTION per-turn sync seam.

Sibling of ``test_entity_sync_stateful_wiring.py`` (76-6). Story 76-7 closes the
last two source gaps in the ADR-118 universal index: **factions** (world-tier
lore) and **locations** (diffuse). Today ``sync_for_turn`` projects only the NPC
cast, so ``entity_sync.faction_count`` / ``entity_sync.location_count`` read 0
every turn and the index is "universal" in name only.

These tests drive the real ``sidequest.server.dispatch.entity_sync.sync_for_turn``
(the function ``_execute_narration_turn`` runs every turn) and assert on
behavior + the emitted watcher event — never on source text (project rule "No
Source-Text Wiring Tests").

Crunch in the Genre, Flavor in the WORLD (SOUL / ADR-120). Factions are
campaign-setting flavor: they live at ``World.lore.factions`` (verified — the
loader populates ``pack.worlds[<slug>].lore.factions``), NOT at any genre- or
global tier. The faction source MUST be the *bound world*. The
``test_faction_source_is_world_tier`` case pins exactly this: with no world
bound (``world_slug=""``), zero factions index even though the pack is loaded.

INTENTIONALLY RED until 76-7 lands:
- ``sync_for_turn`` never reads ``sd.genre_pack.worlds[sd.world_slug].lore``, so
  no ``FACTION`` card is ever stored and ``faction_count`` stays 0.
- The published watcher payload carries ``npc_count`` only — it has no
  ``faction_count`` / ``location_count`` key, so the GM panel cannot see the new
  sources (the lie-detector is blind to them).
"""

from __future__ import annotations

import pytest

from sidequest.game.entity_card import EntityType
from sidequest.game.entity_store import EntityStore
from sidequest.genre.models.lore import Faction
from sidequest.server.dispatch import entity_sync as dispatch_entity_sync

# ---------------------------------------------------------------------------
# Duck-typed session state — mirrors the REAL attribute paths sync_for_turn
# reads (after 76-7): snapshot + entity_store (existing) and the world-tier
# faction source genre_pack.worlds[world_slug].lore.factions (verified model
# path: pack.py World.lore -> lore.py WorldLore.factions).
# ---------------------------------------------------------------------------


class _TurnManager:
    interaction = 1


class _Snapshot:
    def __init__(self) -> None:
        self.npcs: list = []
        self.npc_pool: list = []
        self.turn_manager = _TurnManager()


class _WorldLoreStub:
    def __init__(self, factions: list[Faction]) -> None:
        self.factions = factions


class _WorldStub:
    def __init__(self, factions: list[Faction]) -> None:
        self.lore = _WorldLoreStub(factions)


class _GenrePackStub:
    def __init__(self, worlds: dict[str, _WorldStub]) -> None:
        self.worlds = worlds


class _SessionData:
    """Duck-typed ``_SessionData`` exposing exactly what ``sync_for_turn`` reads:
    ``.snapshot``, ``.entity_store`` (existing) plus the world-tier faction
    source ``.genre_pack`` + ``.world_slug`` (76-7)."""

    def __init__(self, *, world_slug: str, factions: list[Faction]) -> None:
        self.snapshot = _Snapshot()
        self.entity_store = EntityStore()
        self.world_slug = world_slug
        worlds = {world_slug: _WorldStub(factions)} if world_slug else {}
        self.genre_pack = _GenrePackStub(worlds)


def _faction(name: str, summary: str = "A power in the land.") -> Faction:
    return Faction(name=name, summary=summary, description=summary)


def _patched_watcher(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Patch the dispatch watcher (patch where USED — lang-review #6) and return
    the captured payload list."""
    captured: list[dict] = []

    def _record(event_type: str, payload: dict, **kwargs: object) -> None:
        captured.append(payload)

    monkeypatch.setattr(dispatch_entity_sync, "_watcher_publish", _record)
    return captured


# ---------------------------------------------------------------------------
# AC1 / AC3 — factions index from the bound world's lore
# ---------------------------------------------------------------------------


class TestFactionSync:
    def test_sync_for_turn_indexes_a_world_faction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC1/AC3: a faction declared on the bound world projects into the live
        ``entity_store`` as a namespaced ``FACTION`` card the narrator's
        retrieval (75-5) can recall. Today nothing reads the world lore → RED."""
        _patched_watcher(monkeypatch)
        sd = _SessionData(world_slug="tideholt", factions=[_faction("The Tide Syndicate")])

        dispatch_entity_sync.sync_for_turn(handler=None, sd=sd)  # type: ignore[arg-type]

        faction_ids = {c.id for c in sd.entity_store.query_by_type(EntityType.FACTION)}
        assert "faction:the_tide_syndicate" in faction_ids

    def test_faction_card_keys_on_name_and_summary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The projected card embeds the faction's name and summary so retrieval
        keys on who they are, not just that they exist."""
        _patched_watcher(monkeypatch)
        sd = _SessionData(
            world_slug="tideholt",
            factions=[_faction("The Tide Syndicate", "Smugglers who run the estuary.")],
        )

        dispatch_entity_sync.sync_for_turn(handler=None, sd=sd)  # type: ignore[arg-type]

        card = sd.entity_store.cards["faction:the_tide_syndicate"]
        assert "The Tide Syndicate" in card.content
        assert "Smugglers who run the estuary." in card.content

    def test_all_world_factions_index(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AC3: the sync iterates the WHOLE faction roster, not just the first."""
        _patched_watcher(monkeypatch)
        sd = _SessionData(
            world_slug="tideholt",
            factions=[_faction("The Tide Syndicate"), _faction("The Lamplighters")],
        )

        dispatch_entity_sync.sync_for_turn(handler=None, sd=sd)  # type: ignore[arg-type]

        faction_ids = {c.id for c in sd.entity_store.query_by_type(EntityType.FACTION)}
        assert faction_ids == {"faction:the_tide_syndicate", "faction:the_lamplighters"}


# ---------------------------------------------------------------------------
# Flavor-in-the-WORLD — the faction source is the bound world, not the genre
# ---------------------------------------------------------------------------


class TestFactionSourceIsWorldTier:
    def test_faction_source_is_world_tier(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SOUL 'Crunch in the Genre, Flavor in the World' (ADR-120): factions
        are world flavor. With NO world bound (``world_slug=""``) the pack is
        still loaded, but there is no world lore to read — so zero factions
        index. Binding the world is what makes them appear. This guards against
        a regression that sources factions from a genre/global tier."""
        _patched_watcher(monkeypatch)
        # A loaded pack, but no bound world → no world-tier lore in scope.
        sd = _SessionData(world_slug="", factions=[])

        dispatch_entity_sync.sync_for_turn(handler=None, sd=sd)  # type: ignore[arg-type]

        assert sd.entity_store.query_by_type(EntityType.FACTION) == []

    def test_binding_the_world_surfaces_its_factions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The positive half of the world-tier contract: the SAME machinery that
        indexes zero factions with no world bound indexes the world's roster once
        a world IS bound — proving the source is the world, keyed by world_slug."""
        _patched_watcher(monkeypatch)
        sd = _SessionData(world_slug="tideholt", factions=[_faction("The Tide Syndicate")])

        dispatch_entity_sync.sync_for_turn(handler=None, sd=sd)  # type: ignore[arg-type]

        assert {c.id for c in sd.entity_store.query_by_type(EntityType.FACTION)} == {
            "faction:the_tide_syndicate"
        }


# ---------------------------------------------------------------------------
# AC4 — the GM-panel lie-detector must SEE the new source counts
# ---------------------------------------------------------------------------


class TestSourceCountTelemetry:
    def test_watcher_event_reports_faction_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC4: the published ``entity_sync`` event must carry ``faction_count``
        so the GM panel can confirm factions actually flowed into the index.
        Today the payload omits the key entirely → KeyError-by-assertion (RED)."""
        captured = _patched_watcher(monkeypatch)
        sd = _SessionData(
            world_slug="tideholt",
            factions=[_faction("The Tide Syndicate"), _faction("The Lamplighters")],
        )

        dispatch_entity_sync.sync_for_turn(handler=None, sd=sd)  # type: ignore[arg-type]

        synced = [p for p in captured if p.get("op") == "synced"]
        assert len(synced) == 1, f"expected one 'synced' event, got {captured!r}"
        assert synced[0]["faction_count"] == 2

    def test_watcher_event_reports_location_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC4: the published event must ALSO carry ``location_count`` — even at
        zero — so the GM panel can tell 'no locations this turn' from 'locations
        not wired'. Today the key is absent entirely → RED. (The non-zero
        location flow is proven once Dev selects the location source adapter —
        see the TEA Delivery Finding; this pins the telemetry contract now.)"""
        captured = _patched_watcher(monkeypatch)
        sd = _SessionData(world_slug="tideholt", factions=[_faction("The Tide Syndicate")])

        dispatch_entity_sync.sync_for_turn(handler=None, sd=sd)  # type: ignore[arg-type]

        synced = [p for p in captured if p.get("op") == "synced"]
        assert len(synced) == 1
        assert "location_count" in synced[0]


# ---------------------------------------------------------------------------
# Idempotency — a faction roster that did not change must not churn the index
# ---------------------------------------------------------------------------


class TestFactionResyncIsIdempotent:
    def test_unchanged_faction_roster_reprojects_nothing_on_resync(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADR-118 §D2 zero-byte-leak: a second sync over an unchanged roster
        re-arms no faction card (the embedding the worker computed survives)."""
        _patched_watcher(monkeypatch)
        sd = _SessionData(world_slug="tideholt", factions=[_faction("The Tide Syndicate")])
        dispatch_entity_sync.sync_for_turn(handler=None, sd=sd)  # type: ignore[arg-type]
        # Pretend the embed worker ran so the card is settled.
        card = sd.entity_store.cards["faction:the_tide_syndicate"]
        card.embedding = [1.0, 0.0]
        card.embedding_pending = False

        dispatch_entity_sync.sync_for_turn(handler=None, sd=sd)  # type: ignore[arg-type]

        assert sd.entity_store.cards["faction:the_tide_syndicate"].embedding_pending is False


# ---------------------------------------------------------------------------
# AC5 — production-path proof against REAL authored world-tier content
# ---------------------------------------------------------------------------


class TestRealWorldFactionsFlowEndToEnd:
    def test_real_bound_world_factions_index_through_production_sync(
        self, session_handler_factory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The strongest faction wiring guard: load a REAL genre pack, bind a
        REAL world that ships authored factions (``elemental_harmony`` /
        ``burning_peace`` — 5 authored factions), drive the production
        ``sync_for_turn``, and assert the world's factions reached the index and
        the GM-panel count reflects them. No source text, real content, real
        seam — fails if the sync exists but never reads the bound world's lore."""
        captured: list[dict] = []

        def _record(event_type: str, payload: dict, **kwargs: object) -> None:
            captured.append(payload)

        monkeypatch.setattr(dispatch_entity_sync, "_watcher_publish", _record)

        sd, handler = session_handler_factory(genre="elemental_harmony")
        sd.world_slug = "burning_peace"  # bind the world whose lore has factions

        dispatch_entity_sync.sync_for_turn(handler, sd)

        faction_cards = sd.entity_store.query_by_type(EntityType.FACTION)
        assert len(faction_cards) >= 1, "real authored world factions must index"
        synced = [p for p in captured if p.get("op") == "synced"]
        assert synced and synced[0]["faction_count"] >= 1
