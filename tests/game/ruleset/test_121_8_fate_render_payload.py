"""RED (ADR-144 F4a3 / story 121-8): the Fate chargen RENDER payload (server→UI).

121-7 (done) shipped the bare ``fate_*`` ``input_type`` *surface* so the
paired-negative gate held, but DEFERRED the rich per-step payload — the design §7
wire contract — to its UI consumer (this story). See
``builder.py::_render_fate_step_message`` ("The rich per-step payload fields ...
land with their UI consumer in story 121-8"). Today ``CharacterCreationPayload``
carries the roll-the-bones rich fields but NO Fate fields, so the three UI
renderers have nothing to draw.

This module pins the server half of the §7 wire contract: when the production
``CharacterBuilder`` renders a fate-step scene, the emitted
``CharacterCreationPayload`` must carry the populated, snake_case fields the UI
mirrors. **Fixtures only — synthetic ``FateConfig`` / scenes through the real
builder; no genre pack is loaded** (mirrors ``test_121_7_fate_interactive_chargen``).

PINNED PUBLIC CONTRACT (Dev implements to these field names — see TEA Assessment).
Additive ``CharacterCreationPayload`` fields, snake_case, ``| None = None``:

  RENDER (server→UI), per ``input_type``:
    fate_aspects        fate_aspect_slots: list[{kind,label,value,required,suggestion}]
    fate_skill_pyramid  fate_available_skills: list[str]
                        fate_pyramid: list[int]            # rung counts (apex-narrowest)
                        fate_apex_rating: int
                        fate_current_allocation: dict[str,int]
                        fate_ladder_labels: dict[int,str]  # 4->Great .. 1->Average
                        fate_legal: bool
                        fate_violations: list[str]
    fate_stunts         fate_available_stunts: list[{name,description}]
                        fate_selected_stunts: list[str]
                        fate_free_stunts: int
                        fate_base_refresh: int
                        fate_current_refresh: int
                        fate_legal: bool
                        fate_violations: list[str]

  SUBMISSION (client→server) fields are pinned in
  ``tests/server/test_121_8_fate_chargen_handler.py``.

The ladder labels are the canonical ``fate_resolution.LADDER`` adjectives
(0 Mediocre / 1 Average / 2 Fair / 3 Good / 4 Great).
"""

from __future__ import annotations

from typing import Any

from sidequest.game.builder import CharacterBuilder
from sidequest.genre.models.character import CharCreationScene, MechanicalEffects
from sidequest.genre.models.rules import RulesConfig
from sidequest.protocol.messages import CharacterCreationPayload

# ---------------------------------------------------------------------------
# Fixtures (synthetic; copied from the 121-7 RED so this module stands alone)
# ---------------------------------------------------------------------------

NOIR_SKILLS = {
    "Investigate": 3,
    "Contacts": 2,
    "Notice": 2,
    "Deceive": 1,
    "Shoot": 1,
    "Rapport": 1,
    "Will": 1,
    "Stealth": 0,
    "Athletics": 0,
    "Fight": 0,
    "Burglary": 0,
    "Drive": 0,
    "Empathy": 0,
    "Provoke": 0,
}
HIGH_CONCEPT = "Hard-Boiled Private Eye"
TROUBLE = "Can't Walk Away From a Dame in Trouble"
STUNT_NAMES = [
    "The Right Word in the Right Ear",
    "Always a Way Out",
    "Gun Nut",
    "Quick on the Draw",
    "Streetwise",
    "Nerves of Steel",
]


def fate_rules() -> RulesConfig:
    """A minimal ``ruleset: fate`` RulesConfig matching the synthetic noir pack.
    No d20 stats — a fate pack authors no ability scores."""
    fate_block: dict[str, object] = {
        "skills": dict(NOIR_SKILLS),
        "refresh": 3,
        "default_high_concept": HIGH_CONCEPT,
        "default_trouble": TROUBLE,
        "stunts": [{"name": n} for n in STUNT_NAMES],
    }
    return RulesConfig.model_validate({"ruleset": "fate", "fate": fate_block})


def fate_step_scene(step: str) -> CharCreationScene:
    """A scene that declares itself a Fate chargen step (aspects/pyramid/stunts)."""
    return CharCreationScene(
        id=f"fate_{step}",
        title="T",
        narration="N",
        choices=[],
        mechanical_effects=MechanicalEffects(fate_chargen_step=step),  # type: ignore[call-arg]
    )


def render_payload(step: str) -> CharacterCreationPayload:
    """Drive the REAL builder's fate-step render and return the emitted payload."""
    builder = CharacterBuilder(scenes=[fate_step_scene(step)], rules=fate_rules())
    return builder.to_scene_message(player_id="p1").payload


def _attr(obj: Any, key: str) -> Any:
    """Read a field whether Dev models the item as a ProtocolBase or a dict."""
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _ladder(labels: Any, rating: int) -> Any:
    """Ladder labels may key by int (server model) or str (post-JSON)."""
    if labels is None:
        return None
    return labels.get(rating, labels.get(str(rating)))


# ---------------------------------------------------------------------------
# fate_aspects render payload (HC + Trouble + N free, pre-filled, editable)
# ---------------------------------------------------------------------------


class TestAspectsRenderPayload:
    def test_input_type_is_fate_aspects(self) -> None:
        assert render_payload("aspects").input_type == "fate_aspects"

    def test_emits_one_slot_per_mandatory_and_free_aspect(self) -> None:
        slots = render_payload("aspects").fate_aspect_slots
        assert slots is not None, "fate_aspects must carry fate_aspect_slots"
        # HC + Trouble + free_aspect_count(3) == 5 editable slots.
        assert len(slots) == 5

    def test_high_concept_slot_is_required_and_seeded(self) -> None:
        slots = render_payload("aspects").fate_aspect_slots or []
        hc = next((s for s in slots if _attr(s, "kind") == "high_concept"), None)
        assert hc is not None, "an editable high_concept slot must be present"
        assert _attr(hc, "required") is True
        # Pre-filled from FateConfig.default_high_concept (seed-then-edit, §5).
        assert HIGH_CONCEPT in (_attr(hc, "value"), _attr(hc, "suggestion"))

    def test_trouble_slot_is_required_and_seeded(self) -> None:
        slots = render_payload("aspects").fate_aspect_slots or []
        trouble = next((s for s in slots if _attr(s, "kind") == "trouble"), None)
        assert trouble is not None
        assert _attr(trouble, "required") is True
        assert TROUBLE in (_attr(trouble, "value"), _attr(trouble, "suggestion"))

    def test_free_aspect_slots_are_optional(self) -> None:
        slots = render_payload("aspects").fate_aspect_slots or []
        free = [s for s in slots if _attr(s, "kind") not in ("high_concept", "trouble")]
        assert len(free) == 3
        assert all(_attr(s, "required") is False for s in free)


# ---------------------------------------------------------------------------
# fate_skill_pyramid render payload (allocation widget + live legality mirror)
# ---------------------------------------------------------------------------


class TestPyramidRenderPayload:
    def test_input_type_is_fate_skill_pyramid(self) -> None:
        assert render_payload("pyramid").input_type == "fate_skill_pyramid"

    def test_available_skills_are_the_pack_skills(self) -> None:
        p = render_payload("pyramid")
        assert p.fate_available_skills is not None
        assert set(p.fate_available_skills) == set(NOIR_SKILLS)

    def test_pyramid_shape_and_apex_come_from_config(self) -> None:
        p = render_payload("pyramid")
        assert p.fate_pyramid == [1, 2, 3, 4]  # SRD default chargen_pyramid
        assert p.fate_apex_rating == 4

    def test_ladder_labels_map_ratings_to_adjectives(self) -> None:
        labels = render_payload("pyramid").fate_ladder_labels
        assert labels is not None, "the pyramid widget needs ladder rung labels"
        assert _ladder(labels, 4) == "Great"
        assert _ladder(labels, 3) == "Good"
        assert _ladder(labels, 2) == "Fair"
        assert _ladder(labels, 1) == "Average"

    def test_initial_allocation_is_empty(self) -> None:
        assert render_payload("pyramid").fate_current_allocation == {}

    def test_empty_allocation_mirrors_as_illegal(self) -> None:
        # The live mirror runs the server validator on the CURRENT allocation;
        # an empty pyramid does not satisfy [1,2,3,4], so legal is False and the
        # violations are surfaced for the UI to echo (not adjudicate).
        p = render_payload("pyramid")
        assert p.fate_legal is False
        assert p.fate_violations  # non-empty


# ---------------------------------------------------------------------------
# fate_stunts render payload (catalog picker + refresh readout)
# ---------------------------------------------------------------------------


class TestStuntsRenderPayload:
    def test_input_type_is_fate_stunts(self) -> None:
        assert render_payload("stunts").input_type == "fate_stunts"

    def test_available_stunts_are_the_catalog(self) -> None:
        p = render_payload("stunts")
        assert p.fate_available_stunts is not None
        assert {_attr(s, "name") for s in p.fate_available_stunts} == set(STUNT_NAMES)

    def test_nothing_selected_initially(self) -> None:
        assert render_payload("stunts").fate_selected_stunts == []

    def test_refresh_readout_starts_at_base(self) -> None:
        p = render_payload("stunts")
        assert p.fate_free_stunts == 3
        assert p.fate_base_refresh == 3
        # No stunts selected yet, so current refresh is undebited.
        assert p.fate_current_refresh == 3

    def test_empty_selection_is_legal(self) -> None:
        # Zero stunts is always within the free budget — a legal starting state.
        assert render_payload("stunts").fate_legal is True


# ---------------------------------------------------------------------------
# CharacterCreationPayload model contract (extra="forbid" → fields must exist)
# ---------------------------------------------------------------------------


class TestPayloadModelFields:
    def test_render_fields_are_declared(self) -> None:
        # ProtocolBase is extra="forbid": these only construct once Dev declares
        # them. In RED this raises ValidationError (the first signal).
        p = CharacterCreationPayload(
            phase="scene",
            input_type="fate_skill_pyramid",
            fate_aspect_slots=[],
            fate_available_skills=["Shoot"],
            fate_pyramid=[1, 2, 3, 4],
            fate_apex_rating=4,
            fate_current_allocation={"Shoot": 4},
            fate_ladder_labels={4: "Great"},
            fate_available_stunts=[],
            fate_selected_stunts=[],
            fate_free_stunts=3,
            fate_base_refresh=3,
            fate_current_refresh=3,
            fate_legal=True,
            fate_violations=[],
        )
        assert p.fate_apex_rating == 4
        assert p.fate_current_allocation == {"Shoot": 4}
        assert p.fate_legal is True

    def test_submission_fields_are_declared(self) -> None:
        # Client→server per-step submission fields (the UI sends these; the
        # handler reads them). Also gated by extra="forbid".
        p = CharacterCreationPayload(
            phase="fate_aspects_confirm",
            fate_high_concept="Ace Reporter",
            fate_trouble="Deadline's a Killer",
            fate_free_aspects=["A Nose for Trouble"],
            fate_allocation={"Shoot": 4},
            fate_selected_stunts=["Gun Nut"],
        )
        assert p.fate_high_concept == "Ace Reporter"
        assert p.fate_trouble == "Deadline's a Killer"
        assert p.fate_free_aspects == ["A Nose for Trouble"]
        assert p.fate_allocation == {"Shoot": 4}


# ---------------------------------------------------------------------------
# Paired-negative (the structural ruleset gate, §7): a non-fate scene never
# emits a fate payload; a d20 input_type never co-renders with fate fields.
# ---------------------------------------------------------------------------


class TestPairedNegativeRender:
    def test_non_fate_scene_carries_no_fate_payload(self) -> None:
        from sidequest.genre.models.character import CharCreationChoice

        scene = CharCreationScene(
            id="plain",
            title="T",
            narration="N",
            choices=[
                CharCreationChoice(
                    label="Go", description="d", mechanical_effects=MechanicalEffects()
                )
            ],
            mechanical_effects=None,
        )
        builder = CharacterBuilder(scenes=[scene], rules=fate_rules())
        payload = builder.to_scene_message(player_id="p1").payload
        assert not (payload.input_type or "").startswith("fate_")
        # The fate render fields stay None on a non-fate surface — the surfaces
        # never co-render (design §7 structural gate).
        assert payload.fate_aspect_slots is None
        assert payload.fate_available_skills is None
        assert payload.fate_available_stunts is None
