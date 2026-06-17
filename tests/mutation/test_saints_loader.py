"""Story 103-1 RED — load_saint_registry: fail-loud loading + catalog cross-validation.

AC2 (story context): "a Saint referencing ``structure/does_not_exist`` fails
world load with the offending saint id + mutation id in the error" — never a
silent skip (No Silent Fallbacks).

Also pins the MP-affordability invariant: a Saint bundle is funded by the AWN
economy itself — base MP plus the single drawback's MP, spent at the
random-pull rate (the Saint's spring "rolls" for you; curation replaces dice,
not pricing). A bundle that cannot be paid for under
``(base_mp + per_negative_mp) // spend_random_positive`` is a content error
the loader must refuse, or Saints would mint MP the Wild path doesn't get.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.mutation.models import (
    MpEconomy,
    MutationCatalog,
    NegativeMutationDef,
    PositiveMutationDef,
    StigmaTables,
)
from sidequest.mutation.saints import load_saint_registry

# ---------------------------------------------------------------------------
# Synthetic catalog (pattern: tests/integration/test_mutation_wiring.py)
# ---------------------------------------------------------------------------


def _catalog() -> MutationCatalog:
    return MutationCatalog(
        mp_economy=MpEconomy(mutant_classes=["Mutant"]),
        stigma=StigmaTables(body_part=["a"] * 6, nature=["b"] * 6, flavor=["c"] * 12),
        negatives=[
            NegativeMutationDef(
                id="negative/test_obsessive",
                name="Obsessive Monologue",
                roll_range=(1, 50),
                effect="cannot stop telling a story once begun",
            ),
            NegativeMutationDef(
                id="negative/test_frail",
                name="Frail",
                roll_range=(51, 100),
                effect="frail",
            ),
        ],
        positives=[
            PositiveMutationDef(
                id="structure/test_bone_density",
                name="Whale-Bone Density",
                category="structure",
                effect="dense bones",
            ),
            PositiveMutationDef(
                id="sense/test_deep_sight",
                name="Deep-Pressure Sight",
                category="sense",
                effect="see in the deep",
            ),
            PositiveMutationDef(
                id="hybrid/test_salt_blood",
                name="Salt Blood",
                category="hybrid",
                effect="salt tolerance",
            ),
            PositiveMutationDef(
                id="cognition/test_story_recall",
                name="Story Recall",
                category="cognition",
                effect="perfect recall of tales",
            ),
            PositiveMutationDef(
                id="exotic/test_fifth_mark",
                name="Fifth Mark",
                category="exotic",
                effect="one mark too many",
            ),
        ],
    )


_VALID_SAINTS_YAML = """\
saints:
  - id: herman_of_the_acushnet
    name: Saint Herman of the Acushnet
    tradition: literary
    patron_regions: [whalecoast]
    bundle:
      - structure/test_bone_density
      - sense/test_deep_sight
    drawback: negative/test_obsessive
    affinity:
      - hybrid/test_salt_blood
    iconography: a whale-boned pilgrim
    veneration: feast of the Acushnet
"""


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "saints.yaml"
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


class TestLoadSaintRegistry:
    def test_missing_file_raises_filenotfounderror_naming_path(self, tmp_path: Path) -> None:
        """Absence is the CALLER's decision (mirrors load_mutation_catalog);
        the loader itself fails loud and names the path it looked for."""
        missing = tmp_path / "saints.yaml"
        with pytest.raises(FileNotFoundError) as exc_info:
            load_saint_registry(missing, _catalog())
        assert "saints.yaml" in str(exc_info.value)

    def test_valid_file_loads_registry(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _VALID_SAINTS_YAML)
        registry = load_saint_registry(path, _catalog())
        saint = registry.by_id("herman_of_the_acushnet")
        assert saint.bundle == ["structure/test_bone_density", "sense/test_deep_sight"]
        assert saint.drawback == "negative/test_obsessive"
        assert saint.affinity == ["hybrid/test_salt_blood"]

    def test_unknown_bundle_id_fails_loud_with_saint_and_mutation_id(self, tmp_path: Path) -> None:
        """AC2 verbatim: the error surface carries the offending saint id AND
        the unresolvable mutation id — not a warning, not a skip."""
        bad = _VALID_SAINTS_YAML.replace("structure/test_bone_density", "structure/does_not_exist")
        path = _write(tmp_path, bad)
        with pytest.raises(ValueError) as exc_info:
            load_saint_registry(path, _catalog())
        message = str(exc_info.value)
        assert "herman_of_the_acushnet" in message
        assert "structure/does_not_exist" in message

    def test_unknown_drawback_id_fails_loud(self, tmp_path: Path) -> None:
        bad = _VALID_SAINTS_YAML.replace("negative/test_obsessive", "negative/does_not_exist")
        path = _write(tmp_path, bad)
        with pytest.raises(ValueError) as exc_info:
            load_saint_registry(path, _catalog())
        message = str(exc_info.value)
        assert "herman_of_the_acushnet" in message
        assert "negative/does_not_exist" in message

    def test_unknown_affinity_id_fails_loud(self, tmp_path: Path) -> None:
        bad = _VALID_SAINTS_YAML.replace("hybrid/test_salt_blood", "hybrid/does_not_exist")
        path = _write(tmp_path, bad)
        with pytest.raises(ValueError) as exc_info:
            load_saint_registry(path, _catalog())
        message = str(exc_info.value)
        assert "herman_of_the_acushnet" in message
        assert "hybrid/does_not_exist" in message

    def test_malformed_top_level_fails_loud(self, tmp_path: Path) -> None:
        """``saints:`` must be a list of Saint mappings — scalar garbage is a
        validation error, never an empty registry."""
        path = _write(tmp_path, "saints: not-a-list\n")
        with pytest.raises(Exception) as exc_info:
            load_saint_registry(path, _catalog())
        # pydantic ValidationError or an explicit ValueError both satisfy the
        # loud contract; what must NOT happen is a silently-empty registry.
        assert exc_info.value is not None

    def test_bundle_over_mp_budget_fails_loud(self, tmp_path: Path) -> None:
        """Affordability invariant: with the default economy
        (base 2 + 2 per negative, random pull = 1 MP) the budget funds at most
        4 bundle marks. A 5-mark bundle is a pricing violation — the Saint
        would grant more than the economy pays for. Loud refusal at load."""
        five_bundle = _VALID_SAINTS_YAML.replace(
            "    bundle:\n      - structure/test_bone_density\n      - sense/test_deep_sight\n",
            "    bundle:\n"
            "      - structure/test_bone_density\n"
            "      - sense/test_deep_sight\n"
            "      - hybrid/test_salt_blood\n"
            "      - cognition/test_story_recall\n"
            "      - exotic/test_fifth_mark\n",
        )
        path = _write(tmp_path, five_bundle)
        with pytest.raises(ValueError) as exc_info:
            load_saint_registry(path, _catalog())
        assert "herman_of_the_acushnet" in str(exc_info.value)

    def test_four_mark_bundle_at_budget_loads(self, tmp_path: Path) -> None:
        """Boundary: exactly 4 marks == (2 + 2) // 1 — affordable, must load."""
        four_bundle = _VALID_SAINTS_YAML.replace(
            "    bundle:\n      - structure/test_bone_density\n      - sense/test_deep_sight\n",
            "    bundle:\n"
            "      - structure/test_bone_density\n"
            "      - sense/test_deep_sight\n"
            "      - hybrid/test_salt_blood\n"
            "      - cognition/test_story_recall\n",
        ).replace(
            "    affinity:\n      - hybrid/test_salt_blood\n",
            "",
        )
        path = _write(tmp_path, four_bundle)
        registry = load_saint_registry(path, _catalog())
        assert len(registry.by_id("herman_of_the_acushnet").bundle) == 4

    def test_duplicate_bundle_entries_fail_loud(self, tmp_path: Path) -> None:
        """The same mark twice in one bundle is an authoring error — the state
        model appends ids and a duplicate would double-count the grant."""
        dup = _VALID_SAINTS_YAML.replace(
            "      - sense/test_deep_sight\n",
            "      - structure/test_bone_density\n",
        )
        path = _write(tmp_path, dup)
        with pytest.raises(ValueError) as exc_info:
            load_saint_registry(path, _catalog())
        assert "herman_of_the_acushnet" in str(exc_info.value)
