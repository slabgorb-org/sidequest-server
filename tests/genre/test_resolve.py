"""Trope inheritance resolution tests.

Port of sidequest-genre/tests/resolve_tests.rs (relevant behavior tests).

Covers:
- Multi-level inheritance chains
- Self-cycles and three-node cycles
- Depth limit (CWE-674)
- Merge field semantics (empty child inherits, non-empty child overrides)
- Slugification in extends matching
- Pass-through (no extends)
- Abstract flag and extends cleared after resolution
- Genre abstract tropes not in output
- ID inheritance
- Empty inputs
"""

from __future__ import annotations

import pytest
import yaml

from sidequest.genre.error import GenreCycleError, GenreMissingParentError, GenreValidationError
from sidequest.genre.models.tropes import TropeDefinition
from sidequest.genre.resolve import MAX_INHERITANCE_DEPTH, resolve_trope_inheritance


def tropes_from_yaml(raw: str) -> list[TropeDefinition]:
    """Parse a YAML string into a list of TropeDefinition."""
    data = yaml.safe_load(raw)
    return [TropeDefinition.model_validate(item) for item in data]


# ═══════════════════════════════════════════════════════════
# Multi-level inheritance (A extends B extends C)
# ═══════════════════════════════════════════════════════════


def test_multi_level_inheritance_resolves_grandparent_fields() -> None:
    genre = tropes_from_yaml("""
- name: Archetype Root
  abstract: true
  category: recurring
  triggers:
    - ancient trigger
  narrative_hints:
    - root guidance
  tension_level: 0.3
  resolution_patterns:
    - the cycle repeats
""")
    world = tropes_from_yaml("""
- name: Mid Trope
  extends: archetype-root
  triggers:
    - mid trigger
- name: Leaf Trope
  extends: mid-trope
  description: The final inheritor
""")

    resolved = resolve_trope_inheritance(genre, world)
    assert len(resolved) == 2, "both world tropes should appear"

    mid = next(t for t in resolved if t.name == "Mid Trope")
    assert mid.category == "recurring", "mid should inherit category from root"
    assert any("mid trigger" in t for t in mid.triggers), "mid should have its own triggers"
    assert not any("ancient" in t for t in mid.triggers), "mid should NOT have root triggers"

    leaf = next(t for t in resolved if t.name == "Leaf Trope")
    assert leaf.description == "The final inheritor", "leaf keeps its own description"
    assert any("mid trigger" in t for t in leaf.triggers), (
        "leaf should inherit triggers from mid parent"
    )


# ═══════════════════════════════════════════════════════════
# Self-cycle (A extends A)
# ═══════════════════════════════════════════════════════════


def test_self_cycle_detected() -> None:
    world = tropes_from_yaml("""
- name: Ouroboros
  extends: ouroboros
  category: conflict
""")
    with pytest.raises(GenreCycleError):
        resolve_trope_inheritance([], world)


# ═══════════════════════════════════════════════════════════
# Three-node cycle (A → B → C → A)
# ═══════════════════════════════════════════════════════════


def test_three_node_cycle_detected() -> None:
    world = tropes_from_yaml("""
- name: Alpha
  extends: charlie
  category: conflict
- name: Bravo
  extends: alpha
  category: conflict
- name: Charlie
  extends: bravo
  category: conflict
""")
    with pytest.raises(GenreCycleError):
        resolve_trope_inheritance([], world)


# ═══════════════════════════════════════════════════════════
# Depth limit (CWE-674: unbounded recursion)
# ═══════════════════════════════════════════════════════════


def test_depth_limit_rejects_excessively_deep_chain() -> None:
    # Build a chain of MAX+6 tropes to exceed the limit
    n = MAX_INHERITANCE_DEPTH + 6
    items = []
    for i in range(n):
        if i < n - 1:
            items.append(f"- name: Trope {i}\n  extends: trope-{i + 1}\n  category: conflict")
        else:
            items.append(f"- name: Trope {i}\n  category: conflict")
    yaml_str = "\n".join(items)
    world = tropes_from_yaml(yaml_str)
    with pytest.raises(GenreValidationError, match="maximum depth"):
        resolve_trope_inheritance([], world)


# ═══════════════════════════════════════════════════════════
# Merge field semantics — child overrides vs inherits
# ═══════════════════════════════════════════════════════════


def test_merge_child_description_overrides_parent() -> None:
    genre = tropes_from_yaml("""
- name: Base
  abstract: true
  category: revelation
  description: Parent description
  tension_level: 0.4
""")
    world = tropes_from_yaml("""
- name: Derived
  extends: base
  description: Child description
""")
    resolved = resolve_trope_inheritance(genre, world)
    assert resolved[0].description == "Child description"


def test_merge_child_inherits_description_when_absent() -> None:
    genre = tropes_from_yaml("""
- name: Base
  abstract: true
  category: revelation
  description: Inherited description
""")
    world = tropes_from_yaml("""
- name: Derived
  extends: base
""")
    resolved = resolve_trope_inheritance(genre, world)
    assert resolved[0].description == "Inherited description"


def test_merge_child_inherits_tension_level_when_absent() -> None:
    genre = tropes_from_yaml("""
- name: Base
  abstract: true
  category: conflict
  tension_level: 0.7
""")
    world = tropes_from_yaml("""
- name: Derived
  extends: base
""")
    resolved = resolve_trope_inheritance(genre, world)
    assert resolved[0].tension_level == pytest.approx(0.7)


def test_merge_child_overrides_tension_level() -> None:
    genre = tropes_from_yaml("""
- name: Base
  abstract: true
  category: conflict
  tension_level: 0.3
""")
    world = tropes_from_yaml("""
- name: Derived
  extends: base
  tension_level: 0.9
""")
    resolved = resolve_trope_inheritance(genre, world)
    assert resolved[0].tension_level == pytest.approx(0.9)


def test_merge_empty_child_triggers_inherits_from_parent() -> None:
    genre = tropes_from_yaml("""
- name: Base
  abstract: true
  category: conflict
  triggers:
    - parent trigger one
    - parent trigger two
""")
    world = tropes_from_yaml("""
- name: Derived
  extends: base
""")
    resolved = resolve_trope_inheritance(genre, world)
    assert len(resolved[0].triggers) == 2
    assert resolved[0].triggers[0] == "parent trigger one"


def test_merge_non_empty_child_triggers_overrides_parent() -> None:
    genre = tropes_from_yaml("""
- name: Base
  abstract: true
  category: conflict
  triggers:
    - parent trigger
""")
    world = tropes_from_yaml("""
- name: Derived
  extends: base
  triggers:
    - child trigger
""")
    resolved = resolve_trope_inheritance(genre, world)
    assert len(resolved[0].triggers) == 1
    assert resolved[0].triggers[0] == "child trigger"


def test_merge_empty_child_tags_inherits_from_parent() -> None:
    genre = tropes_from_yaml("""
- name: Base
  abstract: true
  category: conflict
  tags: [dark, foreboding]
""")
    world = tropes_from_yaml("""
- name: Derived
  extends: base
""")
    resolved = resolve_trope_inheritance(genre, world)
    assert resolved[0].tags == ["dark", "foreboding"]


def test_merge_child_factions_overrides_parent() -> None:
    # epic-157 (ADR-059 amendment): the `factions` zone-eligibility tag must
    # survive trope inheritance. A world trope that `extends` a genre parent and
    # declares its own factions — e.g. gulliver's the_petty_holy_war extends
    # "Impossible Authority" and is scoped [the_lilliput_court] — must keep them.
    # Without propagation the tag is silently dropped to [], the Seam-3 trope
    # gate sees empty (permissive), and the Lilliput egg-war leaks into every
    # voyage. This is the regression this story fixes.
    genre = tropes_from_yaml("""
- name: Base
  abstract: true
  category: conflict
""")
    world = tropes_from_yaml("""
- name: Derived
  extends: base
  factions: [the_lilliput_court]
""")
    resolved = resolve_trope_inheritance(genre, world)
    assert resolved[0].factions == ["the_lilliput_court"]


def test_merge_empty_child_factions_inherits_from_parent() -> None:
    # Consistent with the tags/triggers merge semantics: an untagged child
    # inherits the parent's factions rather than silently resetting to empty.
    genre = tropes_from_yaml("""
- name: Base
  abstract: true
  category: conflict
  factions: ["*"]
""")
    world = tropes_from_yaml("""
- name: Derived
  extends: base
""")
    resolved = resolve_trope_inheritance(genre, world)
    assert resolved[0].factions == ["*"]


def test_merge_resolution_hints_inherited_when_child_absent() -> None:
    genre = tropes_from_yaml("""
- name: Base
  abstract: true
  category: conflict
  resolution_hints:
    - hero prevails
    - sacrifice required
""")
    world = tropes_from_yaml("""
- name: Derived
  extends: base
""")
    resolved = resolve_trope_inheritance(genre, world)
    assert resolved[0].resolution_hints is not None
    assert len(resolved[0].resolution_hints) == 2


def test_merge_resolution_patterns_inherited_when_child_absent() -> None:
    genre = tropes_from_yaml("""
- name: Base
  abstract: true
  category: recurring
  resolution_patterns:
    - the mentor reveals the truth
""")
    world = tropes_from_yaml("""
- name: Derived
  extends: base
""")
    resolved = resolve_trope_inheritance(genre, world)
    patterns = resolved[0].resolution_patterns
    assert patterns is not None
    assert patterns[0] == "the mentor reveals the truth"


# ═══════════════════════════════════════════════════════════
# Escalation and PassiveProgression inheritance
# ═══════════════════════════════════════════════════════════


def test_merge_inherits_escalation_from_parent() -> None:
    genre = tropes_from_yaml("""
- name: Base
  abstract: true
  category: climax
  escalation:
    - at: 0.25
      event: warning signs
      stakes: low
    - at: 0.75
      event: climax approaches
      stakes: high
""")
    world = tropes_from_yaml("""
- name: Derived
  extends: base
""")
    resolved = resolve_trope_inheritance(genre, world)
    assert len(resolved[0].escalation) == 2
    assert resolved[0].escalation[0].at == pytest.approx(0.25)
    assert resolved[0].escalation[1].event == "climax approaches"


def test_merge_child_escalation_overrides_parent() -> None:
    genre = tropes_from_yaml("""
- name: Base
  abstract: true
  category: climax
  escalation:
    - at: 0.5
      event: parent beat
      stakes: medium
""")
    world = tropes_from_yaml("""
- name: Derived
  extends: base
  escalation:
    - at: 0.1
      event: child beat
      stakes: low
""")
    resolved = resolve_trope_inheritance(genre, world)
    assert len(resolved[0].escalation) == 1
    assert resolved[0].escalation[0].event == "child beat"


def test_merge_inherits_passive_progression_from_parent() -> None:
    genre = tropes_from_yaml("""
- name: Base
  abstract: true
  category: recurring
  passive_progression:
    rate_per_turn: 0.02
    rate_per_day: 0.05
    accelerators: [combat, dialogue]
    decelerators: [rest]
    accelerator_bonus: 0.01
    decelerator_penalty: 0.005
""")
    world = tropes_from_yaml("""
- name: Derived
  extends: base
""")
    resolved = resolve_trope_inheritance(genre, world)
    prog = resolved[0].passive_progression
    assert prog is not None
    assert prog.rate_per_turn == pytest.approx(0.02)
    assert prog.rate_per_day == pytest.approx(0.05)
    assert prog.accelerators == ["combat", "dialogue"]


# ═══════════════════════════════════════════════════════════
# Slugification matching
# ═══════════════════════════════════════════════════════════


def test_extends_matches_by_slug_with_spaces_and_case() -> None:
    genre = tropes_from_yaml("""
- name: The Dark Mentor
  abstract: true
  category: recurring
  triggers:
    - mentor appears
""")
    world = tropes_from_yaml("""
- name: Local Variant
  extends: the-dark-mentor
  description: A local version
""")
    resolved = resolve_trope_inheritance(genre, world)
    assert len(resolved) == 1
    assert resolved[0].category == "recurring"


def test_extends_matches_case_insensitively() -> None:
    genre = tropes_from_yaml("""
- name: UPPER CASE TROPE
  abstract: true
  category: conflict
  tension_level: 0.8
""")
    world = tropes_from_yaml("""
- name: Child
  extends: upper-case-trope
""")
    resolved = resolve_trope_inheritance(genre, world)
    assert resolved[0].tension_level == pytest.approx(0.8)


# ═══════════════════════════════════════════════════════════
# Pass-through: world tropes without extends
# ═══════════════════════════════════════════════════════════


def test_world_trope_without_extends_passes_through_unchanged() -> None:
    genre = tropes_from_yaml("""
- name: Abstract Parent
  abstract: true
  category: conflict
""")
    world = tropes_from_yaml("""
- name: Standalone Trope
  category: revelation
  triggers:
    - something happens
  tension_level: 0.5
""")
    resolved = resolve_trope_inheritance(genre, world)
    assert len(resolved) == 1
    assert resolved[0].name == "Standalone Trope"
    assert resolved[0].category == "revelation"
    assert resolved[0].tension_level == pytest.approx(0.5)


# ═══════════════════════════════════════════════════════════
# Abstract flag and extends cleared after resolution
# ═══════════════════════════════════════════════════════════


def test_resolved_trope_is_not_abstract() -> None:
    genre = tropes_from_yaml("""
- name: Abstract Parent
  abstract: true
  category: conflict
""")
    world = tropes_from_yaml("""
- name: Concrete Child
  extends: abstract-parent
""")
    resolved = resolve_trope_inheritance(genre, world)
    assert not resolved[0].is_abstract


def test_resolved_trope_has_no_extends() -> None:
    genre = tropes_from_yaml("""
- name: Parent
  abstract: true
  category: conflict
""")
    world = tropes_from_yaml("""
- name: Child
  extends: parent
""")
    resolved = resolve_trope_inheritance(genre, world)
    assert resolved[0].extends is None


# ═══════════════════════════════════════════════════════════
# Only world tropes in output (abstract parents not emitted)
# ═══════════════════════════════════════════════════════════


def test_genre_abstract_tropes_not_in_output() -> None:
    genre = tropes_from_yaml("""
- name: Abstract One
  abstract: true
  category: recurring
- name: Abstract Two
  abstract: true
  category: conflict
""")
    world = tropes_from_yaml("""
- name: Concrete
  extends: abstract-one
""")
    resolved = resolve_trope_inheritance(genre, world)
    assert len(resolved) == 1
    assert resolved[0].name == "Concrete"


# ═══════════════════════════════════════════════════════════
# ID inheritance
# ═══════════════════════════════════════════════════════════


def test_merge_inherits_id_from_parent_when_child_has_none() -> None:
    genre = tropes_from_yaml("""
- name: Parent
  id: parent_id
  abstract: true
  category: conflict
""")
    world = tropes_from_yaml("""
- name: Child
  extends: parent
""")
    resolved = resolve_trope_inheritance(genre, world)
    assert resolved[0].id == "parent_id"


def test_merge_child_id_overrides_parent_id() -> None:
    genre = tropes_from_yaml("""
- name: Parent
  id: parent_id
  abstract: true
  category: conflict
""")
    world = tropes_from_yaml("""
- name: Child
  id: child_id
  extends: parent
""")
    resolved = resolve_trope_inheritance(genre, world)
    assert resolved[0].id == "child_id"


# ═══════════════════════════════════════════════════════════
# Empty inputs
# ═══════════════════════════════════════════════════════════


def test_empty_genre_and_world_tropes_returns_empty() -> None:
    assert resolve_trope_inheritance([], []) == []


def test_empty_world_tropes_returns_empty() -> None:
    genre = tropes_from_yaml("""
- name: Abstract Only
  abstract: true
  category: conflict
""")
    assert resolve_trope_inheritance(genre, []) == []


# ═══════════════════════════════════════════════════════════
# Missing parent error
# ═══════════════════════════════════════════════════════════


def test_missing_parent_raises_error() -> None:
    world = tropes_from_yaml("""
- name: Orphan
  extends: nonexistent-parent
  category: conflict
""")
    with pytest.raises(GenreMissingParentError) as exc_info:
        resolve_trope_inheritance([], world)
    assert "Orphan" in str(exc_info.value)
    assert "nonexistent-parent" in str(exc_info.value)


# ═══════════════════════════════════════════════════════════
# Wiring test: resolve_trope_inheritance is exported from __init__
# ═══════════════════════════════════════════════════════════


def test_resolve_trope_inheritance_wired_into_package() -> None:
    """resolve_trope_inheritance must be importable from sidequest.genre."""
    from sidequest.genre import resolve_trope_inheritance as rtr  # noqa: F401

    assert callable(rtr)
