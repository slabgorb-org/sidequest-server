"""Tests for misc model types: axes, theme, narrative, culture, inventory, etc."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.genre.models import (
    AxesConfig,
    AxisDefinition,
    CarryMode,
    Culture,
    GenreTheme,
    InventoryConfig,
    Legend,
    NpcTrait,
    NpcTraitsDatabase,
    Prompts,
)


class TestAxesConfig:
    def test_valid(self) -> None:
        ac = AxesConfig(
            definitions=[
                AxisDefinition(
                    id="comedy",
                    name="Comedy",
                    description="Tone",
                    poles=["serious", "gonzo"],
                    default=0.3,
                ),
            ]
        )
        assert len(ac.definitions) == 1

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            AxesConfig.model_validate({"definitions": [], "bogus": True})


class TestGenreTheme:
    def _valid_data(self) -> dict:
        return {
            "primary": "#FFF",
            "secondary": "#000",
            "accent": "#F00",
            "background": "#111",
            "surface": "#222",
            "text": "#CCC",
            "border_style": "solid",
            "web_font_family": "Serif",
            "display_font_family": "Display",
            "archetype": "parchment",
            "dinkus": {"enabled": True, "cooldown": 2, "default_weight": "medium", "glyph": {}},
            "session_opener": {"enabled": True},
        }

    def test_valid(self) -> None:
        t = GenreTheme.model_validate(self._valid_data())
        assert t.primary == "#FFF"

    def test_extra_forbidden(self) -> None:
        data = self._valid_data()
        data["extra_field"] = True
        with pytest.raises(ValidationError):
            GenreTheme.model_validate(data)


class TestPrompts:
    def test_valid_minimal(self) -> None:
        p = Prompts(narrator="N", combat="C", npc="NPC", world_state="WS")
        assert p.narrator == "N"
        assert p.chase is None

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            Prompts.model_validate(
                {
                    "narrator": "N",
                    "combat": "C",
                    "npc": "NPC",
                    "world_state": "WS",
                    "bogus": True,
                }
            )


class TestCulture:
    def test_valid(self) -> None:
        c = Culture(
            name="Nordic",
            summary="Northern",
            description="Cold folk",
            slots={},
            person_patterns=[],
            place_patterns=[],
        )
        assert c.name == "Nordic"

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            Culture.model_validate(
                {
                    "name": "X",
                    "summary": "Y",
                    "description": "Z",
                    "slots": {},
                    "person_patterns": [],
                    "place_patterns": [],
                    "bogus": True,
                }
            )


class TestInventoryConfig:
    def test_defaults(self) -> None:
        inv = InventoryConfig()
        assert inv.currency is None
        assert inv.item_catalog == []

    def test_carry_mode_enum(self) -> None:
        from sidequest.genre.models.inventory import InventoryPhilosophy

        ip = InventoryPhilosophy(carry_mode=CarryMode.item_count, weight_limit=50.0)
        assert ip.carry_mode == CarryMode.item_count


class TestNpcTraits:
    def test_trait_alias(self) -> None:
        """'trait' YAML key maps to trait_name field."""
        t = NpcTrait.model_validate({"trait": "cautious"})
        assert t.trait_name == "cautious"

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            NpcTrait.model_validate({"trait": "x", "bogus": True})

    def test_database_valid(self) -> None:
        db = NpcTraitsDatabase(
            personality=[NpcTrait.model_validate({"trait": "brave"})],
            physical=[],
            behavioral=[],
        )
        assert len(db.personality) == 1


class TestLegend:
    def test_description_alias_for_summary(self) -> None:
        """'description' key should be accepted as alias for 'summary'."""
        leg = Legend.model_validate({"name": "The Fall", "description": "A great war."})
        assert leg.summary == "A great war."

    def test_roundtrip(self) -> None:
        leg = Legend(name="X", summary="Y", era="Ancient")
        data = leg.model_dump()
        leg2 = Legend.model_validate(data)
        assert leg2.era == "Ancient"

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            Legend.model_validate({"name": "X", "summary": "Y", "extra": True})
