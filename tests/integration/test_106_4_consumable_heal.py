"""Story 106-4: a consumed healing item applies its heal to the HpPool.

Bug (2026-06-13 beneath_sunden playtest): drinking a Potion of Mending
consumed the item and narrated a heal but restored ZERO HP — the consume
lane removed the item but never applied its effect (the consume half was
wired; the effect half was not). This drives the real
``_apply_narration_result_to_snapshot`` consume seam and asserts
(a) HP rises by the item's ``heal_amount`` and (b) a ``state_patch.hp``
span fires (GM-panel lie detector, ADR-114 §6) with a consumable source —
so the GM panel can prove the heal was mechanically applied, not just
narrated.

Heal magnitude per the WWN-SRD escalation ruling (gm-decisions 2026-06-13):
Potion of Mending = ``1d6+2`` (WWN-family Lazarus-Patch idiom).
"""

from __future__ import annotations

import asyncio

import pytest
from opentelemetry.sdk.trace import TracerProvider

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.session import GameSnapshot, TurnManager
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from sidequest.server.watcher import WatcherSpanProcessor
from sidequest.telemetry import spans as spans_module
from sidequest.telemetry.watcher_hub import watcher_hub
from tests._helpers.session_room import room_for


def _wounded_hero(name: str, *, hp_current: int, hp_max: int, items: list[dict]) -> Character:
    inv = Inventory()
    inv.items = list(items)
    return Character(
        core=CreatureCore(
            name=name,
            description=f"{name}, test hero",
            personality="stoic",
            inventory=inv,
            statuses=[],
            hp=HpPool(current=hp_current, max=hp_max, base_max=hp_max),
        ),
        char_class="Warrior",
        race="Human",
        backstory=f"{name} wanders the integration suite.",
    )


async def _setup(monkeypatch: pytest.MonkeyPatch, label: str) -> list[dict]:
    watcher_hub.bind_loop(asyncio.get_running_loop())
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001

    captured: list[dict] = []

    class _Sock:
        async def send_json(self, data: dict) -> None:
            captured.append(data)

    await watcher_hub.subscribe(_Sock())  # type: ignore[arg-type]

    provider = TracerProvider()
    provider.add_span_processor(WatcherSpanProcessor(watcher_hub))
    local_tracer = provider.get_tracer(label)
    monkeypatch.setattr(spans_module, "tracer", lambda: local_tracer)

    return captured


def _heal_potion() -> dict:
    return {
        "id": "potion_healing",
        "name": "Potion of Mending",
        "description": "A cloudy red liquid in a glass vial.",
        "category": "magic",
        "value": 75,
        "weight": 0.2,
        "rarity": "rare",
        "narrative_weight": 0.4,
        "tags": ["magic", "consumable", "healing", "potion"],
        "heal_amount": "1d6+2",
        "equipped": False,
        "quantity": 1,
        "uses_remaining": None,
        "state": "Carried",
    }


@pytest.mark.asyncio
async def test_consuming_heal_potion_restores_hp_and_emits_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = await _setup(monkeypatch, "test-106-4-heal")
    hero = _wounded_hero("Harpo", hp_current=1, hp_max=10, items=[_heal_potion()])
    snapshot = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        location="The Eastern Passage",
        discovered_regions=["The Eastern Passage"],
        quest_log={},
        lore_established=[],
        characters=[hero],
        turn_manager=TurnManager(),
    )
    snapshot.turn_manager.record_interaction()

    result = NarrationTurnResult(
        narration="Harpo drinks the potion down; the warmth spreads.",
        items_consumed=[{"name": "Potion of Mending"}],
    )
    _apply_narration_result_to_snapshot(
        snapshot, result, player_name="Harpo", room=room_for(snapshot)
    )
    await asyncio.sleep(0.05)

    # The item is consumed (existing behavior — the consume half was wired).
    assert snapshot.characters[0].core.inventory.items == []

    # The bug: HP restored ZERO. After the fix, HP rises by 1d6+2 (3..8),
    # clamped to max 10 → final 4..9 from a starting 1.
    new_hp = snapshot.characters[0].core.hp.current
    assert new_hp > 1, "potion restored zero HP — the 106-4 bug"
    assert 4 <= new_hp <= 10, f"expected HP healed from 1 by 1d6+2, got {new_hp}"

    # GM-panel lie detector: a state_patch.hp span must fire so the heal is
    # mechanically verifiable, not just narrated.
    hp_events = [
        e
        for e in captured
        if e["event_type"] == "state_transition"
        and e["component"] == "combat"
        and e["fields"].get("field") == "hp"
    ]
    assert len(hp_events) >= 1, (
        "no state_patch.hp span fired for the consumable heal "
        f"(captured: {[e['fields'] for e in captured if e['event_type'] == 'state_transition']})"
    )
    fields = hp_events[0]["fields"]
    assert fields["delta"] >= 3, f"heal delta should be >=3 (1d6+2), got {fields['delta']}"
    assert fields["actor"] == "Harpo"
    assert "consum" in str(fields["source"]).lower() or "heal" in str(fields["source"]).lower()


def test_chargen_materializer_carries_heal_amount_onto_inventory_dict() -> None:
    """Wiring: the chargen kit materializer (``_item_dict_from_catalog``) must
    copy ``heal_amount`` from the catalog onto the inventory dict — otherwise
    the kit-rolled potion ships with no effect and the consume seam heals 0
    even though the catalog declares a magnitude (the real-play half of the bug,
    distinct from the synthetic-dict path above)."""
    from sidequest.genre.models.inventory import CatalogItem
    from sidequest.server.dispatch.chargen_loadout import _item_dict_from_catalog

    potion = CatalogItem(
        id="potion_healing",
        name="Potion of Mending",
        description="rust and vinegar",
        category="magic",
        tags=["magic", "consumable", "healing", "potion"],
        heal_amount="1d6+2",
    )
    item_dict = _item_dict_from_catalog(potion)
    assert item_dict["heal_amount"] == "1d6+2"

    # A non-healing item carries no heal_amount key (no empty-string noise).
    rope = CatalogItem(id="rope_hemp", name="Hemp Rope", description="50ft", category="utility")
    assert "heal_amount" not in _item_dict_from_catalog(rope)


def test_gained_item_materializer_carries_heal_amount() -> None:
    """Wiring parity: a narrator-granted potion (``item_dict_from_catalog``)
    also carries ``heal_amount`` so a picked-up heal works immediately."""
    from sidequest.game.item_catalog_resolution import item_dict_from_catalog
    from sidequest.genre.models.inventory import CatalogItem

    potion = CatalogItem(
        id="potion_healing_greater",
        name="Potion of Mending (Greater)",
        description="the good stuff",
        category="magic",
        tags=["magic", "consumable", "healing", "potion"],
        heal_amount="2d6+2",
    )
    assert item_dict_from_catalog(potion)["heal_amount"] == "2d6+2"


# --- Part B: guaranteed-heal kit (every kit gets one; 30% chance it's Greater) ---


class _FixedRng:
    """Deterministic stand-in: ``random()`` returns a fixed value so the
    upgrade branch is exercised without flakiness."""

    def __init__(self, value: float) -> None:
        self._v = value

    def random(self) -> float:
        return self._v

    def randrange(self, n: int) -> int:
        return 0


def test_guaranteed_grant_upgrades_below_chance() -> None:
    """A roll strictly below ``upgrade_chance`` yields the better item."""
    from sidequest.game.builder import roll_guaranteed_grant
    from sidequest.genre.models.character import GuaranteedGrant

    grant = GuaranteedGrant(
        item="potion_healing", upgrade="potion_healing_greater", upgrade_chance=0.30
    )
    assert roll_guaranteed_grant(grant, _FixedRng(0.1)) == "potion_healing_greater"


def test_guaranteed_grant_base_at_or_above_chance() -> None:
    """A roll at/above ``upgrade_chance`` yields the base item (never worse)."""
    from sidequest.game.builder import roll_guaranteed_grant
    from sidequest.genre.models.character import GuaranteedGrant

    grant = GuaranteedGrant(
        item="potion_healing", upgrade="potion_healing_greater", upgrade_chance=0.30
    )
    assert roll_guaranteed_grant(grant, _FixedRng(0.5)) == "potion_healing"


def test_guaranteed_grant_no_upgrade_always_base() -> None:
    """A grant with no upgrade always yields the base item regardless of roll."""
    from sidequest.game.builder import roll_guaranteed_grant
    from sidequest.genre.models.character import GuaranteedGrant

    grant = GuaranteedGrant(item="potion_healing")
    assert roll_guaranteed_grant(grant, _FixedRng(0.0)) == "potion_healing"
