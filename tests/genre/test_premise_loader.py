"""Loader wiring + Oz reference + authoring-boundary tests (Plan 1, Task 5)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from sidequest.genre.error import GenreLoadError, GenreValidationError
from sidequest.genre.loader import load_genre_pack

# Resolve the live content repo relative to this test file.
# tests/genre/test_*.py -> tests/genre -> tests -> sidequest-server -> oq-3
_CONTENT = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs" / "wry_whimsy"


@pytest.mark.skipif(not _CONTENT.exists(), reason="sidequest-content not checked out beside server")
def test_wry_whimsy_loads_witnessed_act_vocabulary():
    pack = load_genre_pack(_CONTENT)
    act_ids = {a.id for a in pack.witnessed_acts}
    assert "expose_the_humbug" in act_ids
    assert "refuse_the_premise" in act_ids


@pytest.mark.skipif(not _CONTENT.exists(), reason="sidequest-content not checked out beside server")
def test_oz_loads_the_wizards_humbug_premise_and_blocs():
    pack = load_genre_pack(_CONTENT)
    oz = pack.worlds["oz"]
    premise_ids = {p.premise_id for p in oz.premises}
    bloc_ids = {b.bloc_id for b in oz.blocs}
    assert "the_wizards_humbug" in premise_ids
    assert {"munchkins", "winkies"} <= bloc_ids
    humbug = next(p for p in oz.premises if p.premise_id == "the_wizards_humbug")
    assert humbug.authority == "the_wizard"  # resolves to oz/npcs.yaml id


def _write_minimal_world_with_premises(tmp_path: Path, premises_yaml: str) -> Path:
    """Copy the real wry_whimsy pack, overwrite only oz/premises.yaml.

    Proves a NEW premise loads with ZERO engine changes (the content boundary).

    Mirrors the real content layout: wry_whimsy binds the Fate ruleset, whose
    SRD reference tier lives at ``<content_root>/rulesets`` (sibling of
    genre_packs) and is resolved by the loader via ``pack.parent.parent /
    "rulesets"``. The copied pack is nested under ``genre_packs/`` and the
    shared rulesets tier is copied alongside so that resolution still finds the
    bound ruleset's SRD content (else load fails loud — No Silent Fallbacks).
    """
    import shutil

    content_root = tmp_path / "content"
    dst = content_root / "genre_packs" / "wry_whimsy"
    shutil.copytree(_CONTENT, dst)
    shutil.copytree(_CONTENT.parent.parent / "rulesets", content_root / "rulesets")
    (dst / "worlds" / "oz" / "premises.yaml").write_text(
        textwrap.dedent(premises_yaml), encoding="utf-8"
    )
    return dst


@pytest.mark.skipif(not _CONTENT.exists(), reason="sidequest-content not checked out beside server")
def test_authoring_a_new_premise_needs_no_engine_change(tmp_path):
    pack = load_genre_pack(
        _write_minimal_world_with_premises(
            tmp_path,
            """
            premises:
              - premise_id: the_painted_court
                authority: the_wizard
                claim:
                  subject: the_wizard
                  proposition: "The court's rule is real and absolute."
                belief_reserve: 80
                propped_by: [munchkins]
                drained_by:
                  - act: refuse_the_premise
                    belief_delta: 30
                    cost: "public defiance"
                collapse:
                  threshold: 15
                  outcome: "The painted court scatters like cards."
            blocs:
              - bloc_id: munchkins
                grants_belief_to: [the_painted_court]
                awakening_acts:
                  - act: show_defiance_survives
                    defiance_delta: 20
                tipping_threshold: 60
                tipped_outcome: "The little folk stop bowing."
            """,
        )
    )
    oz = pack.worlds["oz"]
    assert any(p.premise_id == "the_painted_court" for p in oz.premises)


@pytest.mark.skipif(not _CONTENT.exists(), reason="sidequest-content not checked out beside server")
def test_dangling_authority_fails_loud_at_load(tmp_path):
    with pytest.raises((GenreValidationError, GenreLoadError), match="authority"):
        load_genre_pack(
            _write_minimal_world_with_premises(
                tmp_path,
                """
                premises:
                  - premise_id: bad
                    authority: not_a_real_npc
                    claim:
                      subject: x
                      proposition: y
                    collapse:
                      threshold: 0
                      outcome: o
                blocs: []
                """,
            )
        )
