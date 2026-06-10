"""Story 103-1 RED — SaintDef / SaintRegistry structural validation.

The Saint layer (build plan 2026-06-10 §D-A, AWN rebase addendum 2026-06-09):
a Saint is a curated preset over the AWN mutation catalog — a bundle of
positive mutation IDs plus exactly one negative as the canonical drawback.
World-tier content (ADR-140); the genre catalog stays the only mutation
authority.

These tests pin the structural contract of the models in
``sidequest.mutation.saints`` (new module — bespoke mutation subsystem per
AWN D5, NOT the MagicPlugin seam).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.mutation.saints import SaintDef, SaintRegistry

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _saint(**overrides: object) -> SaintDef:
    """A minimal valid Saint; overrides let each test break one invariant."""
    data: dict[str, object] = {
        "id": "herman_of_the_acushnet",
        "name": "Saint Herman of the Acushnet",
        "tradition": "literary",
        "patron_regions": ["whalecoast"],
        "bundle": ["structure/test_bone_density", "sense/test_deep_sight"],
        "drawback": "negative/test_obsessive",
    }
    data.update(overrides)
    return SaintDef.model_validate(data)


# ---------------------------------------------------------------------------
# SaintDef shape
# ---------------------------------------------------------------------------


class TestSaintDefShape:
    def test_minimal_valid_saint_constructs(self) -> None:
        saint = _saint()
        assert saint.id == "herman_of_the_acushnet"
        assert saint.tradition == "literary"
        assert saint.bundle == ["structure/test_bone_density", "sense/test_deep_sight"]
        assert saint.drawback == "negative/test_obsessive"

    def test_optional_fields_default(self) -> None:
        saint = _saint()
        assert saint.affinity == []
        assert saint.iconography == ""
        assert saint.veneration == ""

    @pytest.mark.parametrize(
        "tradition",
        ["literary", "catholic_immigrant", "folk_place", "wilderness_sleeper"],
    )
    def test_all_four_traditions_accepted(self, tradition: str) -> None:
        assert _saint(tradition=tradition).tradition == tradition

    def test_unknown_tradition_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _saint(tradition="lovecraftian")

    def test_empty_bundle_rejected(self) -> None:
        """A Saint with no marks is not a Saint — bundle must be non-empty."""
        with pytest.raises(ValidationError):
            _saint(bundle=[])

    def test_negative_id_in_bundle_rejected(self) -> None:
        """The bundle is the gift; the drawback is the single sanctioned burden.

        A ``negative/`` id smuggled into the bundle must fail validation —
        otherwise a Saint could stack burdens outside the MP economy.
        """
        with pytest.raises(ValidationError):
            _saint(bundle=["structure/test_bone_density", "negative/test_frail"])

    def test_non_negative_drawback_rejected(self) -> None:
        """The drawback must be a ``negative/`` catalog id, never a positive."""
        with pytest.raises(ValidationError):
            _saint(drawback="structure/test_bone_density")

    def test_negative_id_in_affinity_rejected(self) -> None:
        """Affinity entries are purchasable positives — negatives don't belong."""
        with pytest.raises(ValidationError):
            _saint(affinity=["negative/test_frail"])

    def test_empty_saint_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _saint(id="")

    def test_category_prefixed_saint_id_rejected(self) -> None:
        """Saint ids are world-tier names (``herman_of_the_acushnet``), not
        catalog ids — a ``category/`` shaped id indicates an authoring mixup."""
        with pytest.raises(ValidationError):
            _saint(id="structure/herman")

    def test_extra_fields_forbidden(self) -> None:
        """extra='forbid' — a typoed field name must fail, not vanish silently."""
        with pytest.raises(ValidationError):
            _saint(drawbacks="negative/test_obsessive")  # plural typo


# ---------------------------------------------------------------------------
# SaintRegistry
# ---------------------------------------------------------------------------


class TestSaintRegistry:
    def test_registry_holds_saints(self) -> None:
        registry = SaintRegistry(saints=[_saint()])
        assert len(registry.saints) == 1

    def test_duplicate_saint_ids_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SaintRegistry(saints=[_saint(), _saint(name="Impostor Herman")])

    def test_by_id_returns_saint(self) -> None:
        registry = SaintRegistry(saints=[_saint()])
        assert registry.by_id("herman_of_the_acushnet").name == "Saint Herman of the Acushnet"

    def test_by_id_unknown_raises_keyerror_naming_known_ids(self) -> None:
        """Loud failure contract: the error must carry both the requested id
        and the known roster so a content author can see the typo instantly."""
        registry = SaintRegistry(saints=[_saint()])
        with pytest.raises(KeyError) as exc_info:
            registry.by_id("saint_nobody")
        message = str(exc_info.value)
        assert "saint_nobody" in message
        assert "herman_of_the_acushnet" in message
