"""Story 102-6 RED — Psionic discipline AUTHORING surface (AC4).

The load-bearing requirement (ADR-140 "Crunch in the Genre"): Jade must be able
to author a homebrew discipline in pack YAML and have it validate and activate
with ZERO engine edits. Discipline catalogs are content; mechanics are engine.

This mirrors the WWN spell catalog authoring path exactly
(``genre/models/wwn_spell.py`` + ``load_wwn_spell_catalog`` +
``_validate_wwn_starting_prepared_refs``):

  * Model: ``PsionicDiscipline`` + ``PsionicDisciplineCatalog`` (``extra="forbid"``,
    duplicate-id rejection).
  * Loader: ``load_psionic_discipline_catalog(path)`` — ``yaml.safe_load`` →
    ``model_validate`` (Python rule #8: safe load, no arbitrary code).
  * Catalog lookup fails loud on an unknown id (``.get`` raises, parallel to
    ``WwnSpellCatalog.get``).
  * A YAML-authored discipline is activatable through the engine with no Python
    change — proven by activating the parsed discipline and observing the
    discipline span.

Per project memory, this is the CODE-shaped test: a SYNTHETIC discipline authored
inline (tmp_path), NOT a live pack slug. Real catalog completeness for space_opera
/ heavy_metal is the pack VALIDATOR's territory, not a unit test.

Symbols are reached inside the test bodies so the module always collects; the RED
failure is a crisp ImportError / assertion.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

_DISCIPLINE_YAML = textwrap.dedent(
    """
    version: "1.0"
    disciplines:
      - id: telepathic_contact
        name: Telepathic Contact
        level: 1
        effort_cost: 1
        duration: scene
        genre_description: >
          The psychic brushes another mind and hears its surface song.
        mechanical_effect: >
          Reads the target's surface thoughts for the scene.
      - id: psychic_assault
        name: Psychic Assault
        level: 1
        effort_cost: 1
        strain_cost: 1
        duration: scene
        save: mental
        genre_description: >
          A spike of raw will drives into the target's skull.
        mechanical_effect: >
          Mental save or psychic damage; the caster takes 1 System Strain.
    """
).strip()


def _write_catalog(tmp_path: Path) -> Path:
    path = tmp_path / "disciplines.yaml"
    path.write_text(_DISCIPLINE_YAML, encoding="utf-8")
    return path


# ===========================================================================
# YAML → model: a homebrew discipline parses with zero engine edits
# ===========================================================================


def test_discipline_catalog_loads_from_yaml(tmp_path):
    """A discipline authored purely in YAML loads into a catalog."""
    from sidequest.genre.models.psionics import load_psionic_discipline_catalog

    catalog = load_psionic_discipline_catalog(_write_catalog(tmp_path))

    ids = {d.id for d in catalog.disciplines}
    assert ids == {"telepathic_contact", "psychic_assault"}
    contact = catalog.get("telepathic_contact")
    assert contact.effort_cost == 1
    assert contact.duration == "scene"


def test_strain_costing_discipline_carries_strain_cost(tmp_path):
    """The strain overcommit cost is authorable in content (the AC3 hook)."""
    from sidequest.genre.models.psionics import load_psionic_discipline_catalog

    catalog = load_psionic_discipline_catalog(_write_catalog(tmp_path))
    assault = catalog.get("psychic_assault")
    assert assault.strain_cost == 1
    assert assault.save == "mental"


def test_catalog_get_unknown_id_fails_loud(tmp_path):
    """An unknown discipline id must raise — never a silent None (No Silent
    Fallbacks)."""
    from sidequest.genre.models.psionics import load_psionic_discipline_catalog

    catalog = load_psionic_discipline_catalog(_write_catalog(tmp_path))
    with pytest.raises(KeyError):
        catalog.get("astral_projection")


def test_duplicate_discipline_id_rejected(tmp_path):
    """Duplicate ids in one catalog are a content error caught at load
    (mirrors WwnSpellCatalog) — Python rule #11 input validation at the
    boundary."""
    from sidequest.genre.models.psionics import load_psionic_discipline_catalog

    dupe = tmp_path / "dupe.yaml"
    dupe.write_text(
        textwrap.dedent(
            """
            version: "1.0"
            disciplines:
              - id: telepathic_contact
                name: Telepathic Contact
                level: 1
                effort_cost: 1
                duration: scene
                genre_description: x
                mechanical_effect: y
              - id: telepathic_contact
                name: Telepathic Contact (dupe)
                level: 1
                effort_cost: 1
                duration: scene
                genre_description: x
                mechanical_effect: y
            """
        ).strip(),
        encoding="utf-8",
    )
    # The dup-id model_validator raises pydantic ValidationError at load.
    with pytest.raises(ValidationError):
        load_psionic_discipline_catalog(dupe)


def test_unknown_discipline_field_rejected(tmp_path):
    """``extra="forbid"`` — a typo'd key in a homebrew discipline fails loud at
    load instead of silently dropping the author's intent."""
    from sidequest.genre.models.psionics import load_psionic_discipline_catalog

    bad = tmp_path / "bad.yaml"
    bad.write_text(
        textwrap.dedent(
            """
            version: "1.0"
            disciplines:
              - id: telepathic_contact
                name: Telepathic Contact
                level: 1
                efort_cost: 1
                duration: scene
                genre_description: x
                mechanical_effect: y
            """
        ).strip(),
        encoding="utf-8",
    )
    # extra="forbid" raises pydantic ValidationError on the unknown key at load.
    with pytest.raises(ValidationError):
        load_psionic_discipline_catalog(bad)


# ===========================================================================
# Zero-engine-edit activation: a YAML-authored discipline is activatable
# ===========================================================================


def test_yaml_authored_discipline_is_activatable(tmp_path):
    """The whole point of AC4: a discipline that exists ONLY in YAML can be
    activated through the existing engine — a discipline span fires and Effort
    is committed, with no Python change to support this specific discipline."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from sidequest.game.creature_core import CreatureCore, HpPool
    from sidequest.game.ruleset import get_ruleset_module
    from sidequest.game.wwn_magic import EffortPool
    from sidequest.genre.models.psionics import load_psionic_discipline_catalog

    catalog = load_psionic_discipline_catalog(_write_catalog(tmp_path))
    discipline = catalog.get("telepathic_contact")

    core = CreatureCore(
        name="Sael",
        description="A precog",
        personality="watchful",
        hp=HpPool(current=10, max=10, base_max=10),
        effort={"psionic": EffortPool(source="psionic", max=3)},
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    module = get_ruleset_module("swn")
    module.activate_discipline(core=core, discipline=discipline, source="psionic", _tracer=tracer)

    disc_spans = [
        s for s in exporter.get_finished_spans() if s.name.endswith(".discipline.activated")
    ]
    assert disc_spans, "a YAML-authored discipline must be activatable (discipline span fires)"
    assert disc_spans[0].attributes["discipline_id"] == "telepathic_contact"
    assert core.effort["psionic"].available == 2, "activation commits the authored effort_cost"
