"""Tests for Stories 50-18, 50-19, 50-20, 50-21 — scene-harness fixture hydrator.

Unit tests for ``sidequest.game.scene_harness.hydrate_fixture``: the YAML →
``GameSnapshot`` converter that backs ``POST /dev/scene/{name}`` per ADR-092.

Layout (sectioned by the comment banners below):
* Story 50-18 — hydrator contract, error mapping, yaml.safe_load discipline,
  path-traversal guard.
* Story 50-19 — extend ``_hydrate_character()`` to hydrate
  ``Character.known_facts`` from a ``known_facts:`` YAML block.
* Story 50-23 — multi-PC ``characters:`` list hydration.
* Story 50-20 — top-level ``scenario_state:`` block + DAG-order independence.
* Story 50-21 — top-level ``encounter:`` block → ``GameSnapshot.encounter``
  (StructuredEncounter, ADR-033).

The 50-18 contract was the original spec for this file; each later story's
cases extend it for an additional hydration path.

Hydrator contract (extracted from ADR-092 §Hydration rules and the four
canonical fixtures in ``scenarios/fixtures/``):

    from sidequest.game.scene_harness import (
        FixtureNotFoundError,
        FixtureValidationError,
        hydrate_fixture,
    )

    snapshot = hydrate_fixture(name="combat_brawl_wasteland", fixtures_dir=Path(...))

* Returns a ``GameSnapshot`` with ``model_config = {"extra": "ignore"}``.
* Raises ``FixtureNotFoundError`` when ``{fixtures_dir}/{name}.yaml`` is missing.
* Raises ``FixtureValidationError`` on schema violations (missing ``genre``
  / ``world``, malformed ``character``, etc.) — never a bare ``yaml.YAMLError``
  or ``pydantic.ValidationError`` leaking past the module boundary.
* MUST use ``yaml.safe_load`` (lang-review rule #8 — never ``yaml.load`` on
  untrusted-input parsers; the harness is dev-gated, but the rule still binds).
* MUST NOT silently default missing ``genre`` / ``world`` to empty strings
  (CLAUDE.md "No Silent Fallbacks").
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Canonical fixtures live at ``orc-quest/scenarios/fixtures/`` — one level up
# from the server tree. The tests resolve relative to this file so they keep
# working when the suite is invoked from any cwd.
REPO_ROOT = Path(__file__).resolve().parents[3]
CANONICAL_FIXTURES_DIR = REPO_ROOT / "scenarios" / "fixtures"


# ── Module surface ──────────────────────────────────────────────────────────


def test_module_exports_hydrate_fixture() -> None:
    """The hydrator is importable from a stable public path.

    ADR-092 implementation table assigns the hydrator to ``sidequest-server``.
    Tests, the dev-gated route, and any future companion tools must import
    from one canonical location — drift between callers is a wiring bug.
    """
    import sidequest.game.scene_harness as scene_harness

    assert hasattr(scene_harness, "hydrate_fixture"), (
        "scene_harness module must export hydrate_fixture()"
    )
    assert callable(scene_harness.hydrate_fixture)


def test_module_exports_error_types() -> None:
    """Distinct exception types let callers (the HTTP route) map errors to
    404 vs 422 without inspecting message strings."""
    from sidequest.game.scene_harness import (
        FixtureNotFoundError,
        FixtureValidationError,
    )

    # Both are real Exception subclasses with distinct identities.
    assert issubclass(FixtureNotFoundError, Exception)
    assert issubclass(FixtureValidationError, Exception)
    assert FixtureNotFoundError is not FixtureValidationError


# ── Happy path: canonical fixtures hydrate ──────────────────────────────────


@pytest.mark.parametrize(
    "fixture_name",
    [
        "combat_brawl_wasteland",
        "combat_dogfight_space",
        "social_negotiation_tea",
        "social_poker_wasteland",
    ],
)
def test_canonical_fixture_hydrates_without_error(fixture_name: str) -> None:
    """Every canonical fixture in scenarios/fixtures/ must hydrate cleanly.

    These four were authored against ADR-069 and are the regression set
    ADR-092 §Implementation must keep green.
    """
    from sidequest.game.scene_harness import hydrate_fixture
    from sidequest.game.session import GameSnapshot

    snapshot = hydrate_fixture(name=fixture_name, fixtures_dir=CANONICAL_FIXTURES_DIR)

    assert isinstance(snapshot, GameSnapshot), (
        f"hydrate_fixture({fixture_name!r}) must return GameSnapshot, got {type(snapshot).__name__}"
    )


def test_combat_brawl_wasteland_fixture_populates_genre_and_world() -> None:
    """combat_brawl_wasteland.yaml carries genre=mutant_wasteland, world=flickering_reach.

    The hydrator must preserve top-level identity fields verbatim — the
    slug-keyed connect flow keys on ``genre_slug`` + ``world_slug``, so any
    silent rename breaks the entire downstream wire-up.
    """
    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="combat_brawl_wasteland", fixtures_dir=CANONICAL_FIXTURES_DIR)

    assert snapshot.genre_slug == "mutant_wasteland"
    assert snapshot.world_slug == "flickering_reach"


def test_combat_brawl_wasteland_fixture_populates_first_character() -> None:
    """``character:`` block hydrates into ``snapshot.characters[0]`` per
    ADR-069 §Hydration rule 2 (inherited unchanged by ADR-092)."""
    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="combat_brawl_wasteland", fixtures_dir=CANONICAL_FIXTURES_DIR)

    assert len(snapshot.characters) >= 1, "fixture defined a character block"
    pc = snapshot.characters[0]
    # ``Character.name`` is a method (Combatant Protocol); the data lives at
    # ``.core.name``. The fixture YAML's flat ``name:`` field un-flattens
    # into ``Character.core.name`` per the post-port shape.
    assert pc.core.name == "Skar", (
        f"expected Skar from combat_brawl_wasteland.yaml, got {pc.core.name!r}"
    )


def test_combat_brawl_wasteland_fixture_populates_npc_list() -> None:
    """``npcs:`` block hydrates into ``snapshot.npcs`` per ADR-069 rule 4 —
    name, role, disposition each preserved."""
    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="combat_brawl_wasteland", fixtures_dir=CANONICAL_FIXTURES_DIR)

    # combat_brawl_wasteland.yaml defines exactly one NPC: Rust Jaw (hostile, disposition -15).
    # Like Character, Npc nests CreatureCore under ``.core``.
    rust_jaw = next((n for n in snapshot.npcs if n.core.name == "Rust Jaw"), None)
    assert rust_jaw is not None, "Rust Jaw must hydrate from combat_brawl_wasteland.yaml"


def test_minimal_fixture_with_only_genre_and_world(tmp_path: Path) -> None:
    """Unspecified fields use GameSnapshot defaults (ADR-069 rule 8).

    A fixture with only ``genre:`` and ``world:`` should hydrate; everything
    else is field-defaulted by pydantic. This is the cheapest possible
    fixture and the baseline for "minimal repro" debugging.
    """
    fixture = tmp_path / "minimal.yaml"
    fixture.write_text(
        "genre: caverns_and_claudes\nworld: default\n",
        encoding="utf-8",
    )

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="minimal", fixtures_dir=tmp_path)

    assert snapshot.genre_slug == "caverns_and_claudes"
    assert snapshot.world_slug == "default"
    # Defaults: empty list, empty list, empty string.
    assert snapshot.characters == []
    assert snapshot.npcs == []


def test_unknown_top_level_fields_are_ignored(tmp_path: Path) -> None:
    """GameSnapshot uses ``extra="ignore"`` — unknown YAML keys must NOT raise.

    Forward-compat: fixtures may carry comments or experimental keys that
    don't map to current GameSnapshot fields. Per ADR-092, this is the
    pydantic ``extra="ignore"`` discipline.
    """
    fixture = tmp_path / "extra_fields.yaml"
    fixture.write_text(
        "genre: caverns_and_claudes\n"
        "world: default\n"
        "experimental_unknown_field: 42\n"
        "_internal_note: this is a comment-like key\n",
        encoding="utf-8",
    )

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="extra_fields", fixtures_dir=tmp_path)
    # The unknown fields are not present on GameSnapshot — pydantic dropped them.
    assert not hasattr(snapshot, "experimental_unknown_field")
    assert snapshot.genre_slug == "caverns_and_claudes"


# ── Error paths: distinguish 404 (missing) from 422 (invalid) ───────────────


def test_missing_fixture_raises_FixtureNotFoundError(tmp_path: Path) -> None:
    """A fixture filename that doesn't exist on disk must raise the
    not-found exception — not a generic ``FileNotFoundError`` or, worse,
    a silent ``GameSnapshot()`` with default fields (ADR-092 §Decision
    point 5: "Failure is loud")."""
    from sidequest.game.scene_harness import FixtureNotFoundError, hydrate_fixture

    with pytest.raises(FixtureNotFoundError) as exc_info:
        hydrate_fixture(name="does_not_exist", fixtures_dir=tmp_path)

    # The error message must surface the path so the dev knows where to look.
    msg = str(exc_info.value)
    assert "does_not_exist" in msg, (
        f"FixtureNotFoundError must name the missing fixture; got: {msg!r}"
    )


def test_missing_genre_raises_FixtureValidationError(tmp_path: Path) -> None:
    """ADR-092 §Hydration rule 1: top-level ``genre:`` is REQUIRED.

    A silent default to ``""`` would break slug minting downstream — the
    request must fail loudly at validation time, not silently produce a
    save file with an empty genre that then 500s on connect.
    """
    fixture = tmp_path / "no_genre.yaml"
    fixture.write_text("world: default\n", encoding="utf-8")

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError) as exc_info:
        hydrate_fixture(name="no_genre", fixtures_dir=tmp_path)

    # Field-level detail per ADR-092 §Decision point 5.
    assert "genre" in str(exc_info.value).lower()


def test_missing_world_raises_FixtureValidationError(tmp_path: Path) -> None:
    """ADR-092 §Hydration rule 1: top-level ``world:`` is REQUIRED."""
    fixture = tmp_path / "no_world.yaml"
    fixture.write_text("genre: caverns_and_claudes\n", encoding="utf-8")

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError) as exc_info:
        hydrate_fixture(name="no_world", fixtures_dir=tmp_path)

    assert "world" in str(exc_info.value).lower()


def test_empty_genre_string_raises_FixtureValidationError(tmp_path: Path) -> None:
    """``genre: ""`` is the silent-fallback trap: pydantic accepts the empty
    string for ``genre_slug: str = ""``, but the slug generator downstream
    will produce a malformed slug. Hydrator must reject before the snapshot
    ever leaves the boundary.
    """
    fixture = tmp_path / "empty_genre.yaml"
    fixture.write_text(
        'genre: ""\nworld: default\n',
        encoding="utf-8",
    )

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError):
        hydrate_fixture(name="empty_genre", fixtures_dir=tmp_path)


def test_malformed_yaml_raises_FixtureValidationError(tmp_path: Path) -> None:
    """Tab-indented YAML / unbalanced brackets / invalid syntax must surface
    as a structured ``FixtureValidationError`` — never a raw ``yaml.YAMLError``
    that leaks the parser stack trace to the HTTP layer."""
    fixture = tmp_path / "garbage.yaml"
    # Unterminated single-quote — a yaml.scanner.ScannerError producer.
    fixture.write_text("genre: 'unterminated\nworld: default\n", encoding="utf-8")

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError):
        hydrate_fixture(name="garbage", fixtures_dir=tmp_path)


# ── Security & rule-driven tests (lang-review rules #6, #8, #11) ────────────


def test_hydrator_uses_yaml_safe_load_not_yaml_load(tmp_path: Path) -> None:
    """lang-review rule #8: unsafe deserialization.

    ``yaml.load`` without ``Loader=SafeLoader`` allows arbitrary Python
    object instantiation via ``!!python/object/apply:`` tags. The hydrator
    MUST use ``yaml.safe_load`` (or an explicit ``SafeLoader``).

    Test strategy: write a fixture that exploits ``yaml.load`` semantics
    (a ``!!python/object/apply:os.system [['echo "PWN"']]`` payload). If
    the hydrator uses ``yaml.safe_load``, parsing raises and we surface
    ``FixtureValidationError``. If it uses ``yaml.load``, the payload
    constructs (and may even execute) — either way the snapshot type is
    wrong and the test fails.
    """
    fixture = tmp_path / "exploit.yaml"
    # ``!!python/name:os.system`` is the simplest payload that yaml.safe_load
    # rejects and yaml.load happily instantiates as a callable.
    fixture.write_text(
        "genre: !!python/name:os.system\nworld: default\n",
        encoding="utf-8",
    )

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError):
        hydrate_fixture(name="exploit", fixtures_dir=tmp_path)


def test_fixture_name_is_validated_against_path_traversal(tmp_path: Path) -> None:
    """lang-review rule #11: input validation at boundaries.

    ``name`` is reflected into a filesystem path. ``../../etc/passwd`` or
    an absolute path component MUST be rejected before any I/O, mapping
    to ``FixtureNotFoundError`` (or ``FixtureValidationError``) rather
    than silently traversing out of ``fixtures_dir``.
    """
    from sidequest.game.scene_harness import (
        FixtureNotFoundError,
        FixtureValidationError,
        hydrate_fixture,
    )

    # Either error type is acceptable — both are loud failures, neither
    # results in a traversed read.
    with pytest.raises((FixtureNotFoundError, FixtureValidationError)):
        hydrate_fixture(name="../etc/passwd", fixtures_dir=tmp_path)


# ── Story 50-19: known_facts hydration (ADR-092 follow-on) ──────────────────
#
# RED tests for the known_facts extension of _hydrate_character(). Each entry
# under character.known_facts must construct a KnownFact with confidence in
# Literal["Certain", "Suspected", "Rumored", "Discovered"] (post-50-17 enum
# promotion). The fixture authoring contract:
#
#     character:
#       name: Wren
#       ...
#       known_facts:
#         - content: "..."
#           confidence: "Certain"
#         - content: "..."
#           confidence: "Suspected"
#
# Defaults from KnownFact carry through when fields are omitted; the model
# itself uses ``extra="forbid"`` so a typo in the YAML key surfaces loudly.


def _write_character_fixture(tmp_path: Path, name: str, known_facts_yaml: str) -> None:
    """Helper: write a minimal fixture with a character.known_facts block.

    Keeps test bodies focused on the assertion, not the YAML scaffolding.
    """
    fixture = tmp_path / f"{name}.yaml"
    fixture.write_text(
        "genre: caverns_and_claudes\n"
        "world: default\n"
        "character:\n"
        "  name: Wren\n"
        "  description: A scout\n"
        "  personality: cautious\n"
        "  backstory: scouted these tunnels before\n"
        "  char_class: thief\n"
        "  race: human\n"
        f"  known_facts:\n{known_facts_yaml}",
        encoding="utf-8",
    )


def test_character_known_facts_block_hydrates(tmp_path: Path) -> None:
    """AC#1, AC#4: a single known_facts entry projects to character.known_facts.

    The base hydrator already handles every other character field; this is
    the new wiring story 50-19 must add.
    """
    _write_character_fixture(
        tmp_path,
        "single_fact",
        '    - content: "The goblin speaks broken common"\n      confidence: "Certain"\n',
    )

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="single_fact", fixtures_dir=tmp_path)

    pc = snapshot.characters[0]
    assert len(pc.known_facts) == 1, (
        f"expected one KnownFact hydrated from known_facts: block, got {len(pc.known_facts)}"
    )
    fact = pc.known_facts[0]
    assert fact.content == "The goblin speaks broken common"
    assert fact.confidence == "Certain"


@pytest.mark.parametrize(
    "confidence",
    ["Certain", "Suspected", "Rumored", "Discovered"],
)
def test_known_facts_all_four_confidence_tiers(tmp_path: Path, confidence: str) -> None:
    """AC#5, AC#6: every confidence tier in the Literal hydrates verbatim.

    Parametrized so a regression on one tier doesn't masquerade as a
    "test passed" because the suite only happened to hit "Certain".
    """
    _write_character_fixture(
        tmp_path,
        f"tier_{confidence.lower()}",
        f'    - content: "fact about {confidence.lower()}"\n      confidence: "{confidence}"\n',
    )

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name=f"tier_{confidence.lower()}", fixtures_dir=tmp_path)

    pc = snapshot.characters[0]
    assert pc.known_facts[0].confidence == confidence, (
        f"confidence mismatch — fixture wrote {confidence!r}, "
        f"hydrator produced {pc.known_facts[0].confidence!r}"
    )


def test_known_facts_mixed_confidence_fixture(tmp_path: Path) -> None:
    """AC#5: a fixture with four facts spanning all tiers hydrates in order.

    The session-file canonical example — verifies list ordering survives
    YAML → KnownFact projection (no dict-key reordering or set coercion).
    """
    _write_character_fixture(
        tmp_path,
        "mixed_confidence",
        '    - content: "The goblin speaks broken common"\n'
        '      confidence: "Certain"\n'
        '    - content: "A larger creature lurks deeper"\n'
        '      confidence: "Suspected"\n'
        '    - content: "Spiked weapons are common"\n'
        '      confidence: "Rumored"\n'
        '    - content: "There is a hidden exit"\n'
        '      confidence: "Discovered"\n',
    )

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="mixed_confidence", fixtures_dir=tmp_path)

    pc = snapshot.characters[0]
    confidences = [f.confidence for f in pc.known_facts]
    assert confidences == ["Certain", "Suspected", "Rumored", "Discovered"], (
        f"YAML list order must survive hydration; got {confidences!r}"
    )
    # Spot-check content survives too — paranoia against a swap bug where
    # confidences land correctly but content is paired off-by-one.
    assert pc.known_facts[3].content == "There is a hidden exit"


def test_invalid_confidence_raises_FixtureValidationError(tmp_path: Path) -> None:
    """AC#3: a confidence string outside the Literal must raise 422, not 500.

    Post-50-17 pydantic owns this validation; the hydrator's job is just
    to wrap pydantic's ``ValidationError`` as ``FixtureValidationError``
    (the existing pattern for character.* and npcs.*).
    """
    _write_character_fixture(
        tmp_path,
        "bad_confidence",
        '    - content: "this fact has a typo"\n      confidence: "Bogus"\n',
    )

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError) as exc_info:
        hydrate_fixture(name="bad_confidence", fixtures_dir=tmp_path)

    # The error message should point at the offending field so the dev
    # knows what to fix without re-running the hydrator in a debugger.
    msg = str(exc_info.value).lower()
    assert "confidence" in msg or "bogus" in msg or "known_facts" in msg, (
        f"FixtureValidationError must name confidence/known_facts in its message; got: {msg!r}"
    )


def test_legacy_confirmed_confidence_is_rejected(tmp_path: Path) -> None:
    """50-17 regression seam: the pre-promotion value 'confirmed' must NOT
    silently coerce to 'Certain'.

    The KnownFact docstring spells this out:
        "The pre-50-17 legacy value 'confirmed' is rejected."

    If hydrator (or pydantic) ever started accepting it again, every
    save-file written against the new enum would drift toward the old
    string and the journal UI confidence-prop chain would corrupt.
    """
    _write_character_fixture(
        tmp_path,
        "legacy_confirmed",
        '    - content: "ancient fact written under old schema"\n      confidence: "confirmed"\n',
    )

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError):
        hydrate_fixture(name="legacy_confirmed", fixtures_dir=tmp_path)


def test_hydrated_known_facts_have_accusation_weight(tmp_path: Path) -> None:
    """AC#7 (adapted): every hydrated confidence value is a valid key in
    the accusation weight lookup table.

    Deviation logged: SM's session referenced ``AccusationEvaluator._confidence_weight()``,
    which doesn't exist as a method. The actual weight lookup is the
    module-level dict ``sidequest.game.accusation._CONFIDENCE_WEIGHTS``
    indexed in :meth:`AccusationEvaluator.evaluate` (line 184). The
    integration probe is mechanically equivalent: a KeyError here would
    mean the hydrated confidence string fell outside the supported set.
    """
    _write_character_fixture(
        tmp_path,
        "weights",
        '    - content: "fact A"\n'
        '      confidence: "Certain"\n'
        '    - content: "fact B"\n'
        '      confidence: "Suspected"\n'
        '    - content: "fact C"\n'
        '      confidence: "Rumored"\n'
        '    - content: "fact D"\n'
        '      confidence: "Discovered"\n',
    )

    from sidequest.game.accusation import _CONFIDENCE_WEIGHTS
    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="weights", fixtures_dir=tmp_path)
    pc = snapshot.characters[0]

    # Every hydrated fact must produce a weight without KeyError.
    weights = [_CONFIDENCE_WEIGHTS[f.confidence] for f in pc.known_facts]
    assert weights == [2.0, 1.0, 0.5, 1.5], (
        f"confidence weights drifted from accusation.py contract; got {weights!r}"
    )


def test_missing_known_facts_defaults_to_empty_list(tmp_path: Path) -> None:
    """AC#8 (backward compat): a character with no ``known_facts:`` key
    still hydrates, and ``Character.known_facts`` is the empty list.

    This is the regression guard against the 50-19 implementation
    accidentally requiring the new key. Existing canonical fixtures
    (combat_test, dogfight, negotiation, poker) do not declare
    known_facts and must continue to load.
    """
    fixture = tmp_path / "no_facts.yaml"
    fixture.write_text(
        "genre: caverns_and_claudes\n"
        "world: default\n"
        "character:\n"
        "  name: Wren\n"
        "  description: A scout\n"
        "  personality: cautious\n"
        "  backstory: scouted these tunnels before\n"
        "  char_class: thief\n"
        "  race: human\n",
        encoding="utf-8",
    )

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="no_facts", fixtures_dir=tmp_path)
    pc = snapshot.characters[0]
    assert pc.known_facts == [], (
        f"omitting known_facts: should yield empty list, got {pc.known_facts!r}"
    )


def test_canonical_fixtures_still_hydrate_with_known_facts_implementation() -> None:
    """AC#8: the canonical fixtures shipped pre-50-19 must continue to
    hydrate after the known_facts code path is added.

    Wiring-test (CLAUDE.md "Every Test Suite Needs a Wiring Test"):
    proves 50-19's hydrator change didn't break the regression set.

    NB: the 50-18 tests at the top of this file reference legacy names
    (combat_test, dogfight, negotiation, poker) that do not exist in
    ``scenarios/fixtures/`` — see TEA Delivery Findings. This test uses
    the real filenames so its red/green status reflects ONLY the
    known_facts change.
    """
    from sidequest.game.scene_harness import hydrate_fixture

    real_fixtures = (
        "combat_brawl_wasteland",
        "combat_dogfight_space",
        "social_negotiation_tea",
        "social_poker_wasteland",
    )
    for fixture_name in real_fixtures:
        snapshot = hydrate_fixture(name=fixture_name, fixtures_dir=CANONICAL_FIXTURES_DIR)
        # Each canonical PC either has no known_facts block or it's a
        # well-formed empty list. Either way: not None, no exceptions.
        for pc in snapshot.characters:
            assert isinstance(pc.known_facts, list), (
                f"{fixture_name}: pc.known_facts must be a list, "
                f"got {type(pc.known_facts).__name__}"
            )


def test_known_facts_entry_uses_KnownFact_defaults_when_fields_omitted(
    tmp_path: Path,
) -> None:
    """AC#2: a known_facts entry with only ``content`` + ``confidence``
    inherits KnownFact defaults (source="GameEvent", learned_turn=0,
    auto-minted fact_id, category=FactCategory.Lore).

    Verifies the hydrator forwards the entry to the pydantic constructor
    rather than re-implementing defaults locally (which would drift over
    time).
    """
    _write_character_fixture(
        tmp_path,
        "minimal_fact",
        '    - content: "minimal entry"\n      confidence: "Suspected"\n',
    )

    from sidequest.game.character import KnownFact
    from sidequest.game.scene_harness import hydrate_fixture
    from sidequest.protocol.models import FactCategory

    snapshot = hydrate_fixture(name="minimal_fact", fixtures_dir=tmp_path)
    fact = snapshot.characters[0].known_facts[0]

    assert isinstance(fact, KnownFact), (
        f"hydrated entry must be a KnownFact instance, got {type(fact).__name__}"
    )
    assert fact.source == "GameEvent"
    assert fact.learned_turn == 0
    assert fact.category == FactCategory.Lore
    # fact_id is auto-minted (uuid4 hex) — non-empty and not the literal default.
    assert fact.fact_id and len(fact.fact_id) >= 8


def test_known_facts_not_a_list_raises_FixtureValidationError(tmp_path: Path) -> None:
    """ADR-092 §"Failure is loud" + lang-review rule #1 (silent exception
    swallowing): if a fixture sets ``known_facts:`` to a mapping or scalar
    instead of a list, the hydrator MUST surface a structured error rather
    than silently skip the block.

    The base hydrator's sibling pattern (``inventory`` and ``statuses``)
    silently ignores wrong shapes, which would mask a fixture typo. For
    known_facts — a stateful, save-bearing field — silent skip is a
    correctness hazard. Loud is correct.
    """
    fixture = tmp_path / "bad_facts_shape.yaml"
    fixture.write_text(
        "genre: caverns_and_claudes\n"
        "world: default\n"
        "character:\n"
        "  name: Wren\n"
        "  description: A scout\n"
        "  personality: cautious\n"
        "  backstory: scouted these tunnels before\n"
        "  char_class: thief\n"
        "  race: human\n"
        "  known_facts:\n"
        "    not_a_list: true\n",
        encoding="utf-8",
    )

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError):
        hydrate_fixture(name="bad_facts_shape", fixtures_dir=tmp_path)


def test_fixture_supplied_fact_id_is_stripped_and_re_minted(tmp_path: Path) -> None:
    """Security: a fixture-supplied ``fact_id`` must NOT override the auto-mint.

    Threat: ``fact_id`` is the UI dedup key in JournalResponsePayload — a
    fixture that pre-loads a real ``ScenarioClue.id`` would silently
    suppress the legitimate journal entry when the scenario discovers
    that clue in play. The hydrator strips ``fact_id`` from each entry
    before constructing ``KnownFact`` so this footgun is unreachable
    from fixture YAML.
    """
    forged_id = "deadbeef" * 4  # 32 hex chars — a plausible-looking uuid4().hex
    _write_character_fixture(
        tmp_path,
        "forged_fact_id",
        f'    - content: "fact with forged id"\n'
        f'      confidence: "Certain"\n'
        f'      fact_id: "{forged_id}"\n',
    )

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="forged_fact_id", fixtures_dir=tmp_path)
    fact = snapshot.characters[0].known_facts[0]
    assert fact.content == "fact with forged id"
    assert fact.fact_id != forged_id, (
        "fixture-supplied fact_id must be stripped; hydrator must mint fresh"
    )
    assert fact.fact_id and len(fact.fact_id) >= 8


def test_known_facts_extra_field_rejected_by_pydantic(tmp_path: Path) -> None:
    """KnownFact has ``model_config = {"extra": "forbid"}`` — a typo'd key
    in the fixture (e.g., ``confidance: Certain``) must surface as a
    FixtureValidationError, not silently drop the value.

    This guards the model's extra=forbid contract through the hydrator.
    """
    _write_character_fixture(
        tmp_path,
        "extra_field",
        '    - content: "fact with typo"\n'
        '      confidance: "Certain"\n',  # typo: confidance vs confidence
    )

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError):
        hydrate_fixture(name="extra_field", fixtures_dir=tmp_path)


# ── Story 50-23: multi-PC ``characters:`` list hydration ────────────────────
#
# RED tests for the multi-PC extension of ``hydrate_fixture()``. The hydrator
# must accept a top-level ``characters:`` list (each entry the same shape as
# the legacy singular ``character:`` block) and project to
# ``GameSnapshot.characters`` in order. The legacy singular form continues
# to work as ``characters[0]``; declaring both raises FixtureValidationError.
#
# Unblocks Wave 2 party fixtures (party_combat_caverns 4-PC, party_social_tea
# 3-PC). Multiplayer smoke tests are bottlenecked on this hydrator path.
#
# Hot spot: ``_hydrate_character()`` already exists for the singular path —
# the list path can reuse it per-entry. The conflict-validation case belongs
# in the fixture-level validator (top of ``hydrate_fixture``), not the
# per-entry helper.


def _write_multi_pc_fixture(
    tmp_path: Path,
    name: str,
    characters_yaml: str,
    *,
    extra: str = "",
) -> None:
    """Helper: write a minimal multi-PC fixture with a ``characters:`` block.

    ``characters_yaml`` is the list body indented appropriately under
    ``characters:``. ``extra`` is appended at top-level for cases that also
    need an NPC roster, location, or the legacy singular block.
    """
    fixture = tmp_path / f"{name}.yaml"
    fixture.write_text(
        f"genre: caverns_and_claudes\nworld: default\ncharacters:\n{characters_yaml}{extra}",
        encoding="utf-8",
    )


# Every PC entry below carries ``backstory`` + ``char_class`` because
# ``Character`` enforces non-blank validators on both (character.py:128-139).
# Omitting them would surface as a FixtureValidationError during fixture
# setup, masking whether the test is actually exercising the
# multi-PC-list code path or just hitting per-field validation.


def test_characters_list_with_single_entry_hydrates_into_position_zero(
    tmp_path: Path,
) -> None:
    """AC#1, AC#2, AC#8 (single-entry case): a one-entry ``characters:`` list
    populates ``snapshot.characters[0]`` exactly like the legacy singular
    ``character:`` block. This is the bridge case — a fixture author can
    write the new shape without owning a full multi-PC party yet.
    """
    _write_multi_pc_fixture(
        tmp_path,
        "single_in_list",
        "  - name: Wren\n"
        "    description: scout\n"
        "    personality: cautious\n"
        "    backstory: scouted these tunnels before\n"
        "    char_class: thief\n"
        "    race: human\n",
    )

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="single_in_list", fixtures_dir=tmp_path)
    assert len(snapshot.characters) == 1, (
        f"single-entry list must produce one character, got {len(snapshot.characters)}"
    )
    assert snapshot.characters[0].core.name == "Wren"


def test_characters_list_multi_pc_preserves_declared_order(tmp_path: Path) -> None:
    """AC#2, AC#7, AC#8 (multi-entry): a 4-PC fixture lands in
    ``snapshot.characters`` in fixture-declared order.

    List order is load-bearing because the multiplayer slug-connect handler
    binds player N → ``snapshot.characters[N]`` by position; a set/dict
    coercion would silently swap which player controls which PC.
    """
    _write_multi_pc_fixture(
        tmp_path,
        "party_of_four",
        "  - name: Wren\n    description: scout\n    personality: cautious\n"
        "    backstory: scouted these tunnels before\n    char_class: thief\n    race: human\n"
        "  - name: Borin\n    description: warrior\n    personality: hot-tempered\n"
        "    backstory: clan war veteran\n    char_class: fighter\n    race: dwarf\n"
        "  - name: Caia\n    description: cleric\n    personality: stoic\n"
        "    backstory: temple novitiate\n    char_class: cleric\n    race: human\n"
        "  - name: Dax\n    description: rogue\n    personality: sly\n"
        "    backstory: street thief\n    char_class: thief\n    race: halfling\n",
    )

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="party_of_four", fixtures_dir=tmp_path)
    names = [pc.core.name for pc in snapshot.characters]
    assert names == ["Wren", "Borin", "Caia", "Dax"], (
        f"YAML list order must survive hydration; got {names!r}"
    )


def test_characters_list_each_pc_has_distinct_stats(tmp_path: Path) -> None:
    """AC#7: each PC in the list carries its own ``level`` / ``stats`` —
    fields don't bleed across siblings.

    Bug class guarded: looped construction that accidentally mutates a
    shared dict default (lang-review rule #2). A correct hydrator builds a
    fresh ``Character`` per entry; a wrong one shares a dict and you see
    Borin's stats appear on Wren.
    """
    _write_multi_pc_fixture(
        tmp_path,
        "distinct_stats",
        "  - name: Wren\n    description: scout\n    personality: cautious\n"
        "    backstory: scouted these tunnels before\n    char_class: thief\n    race: human\n"
        "    level: 3\n    stats: {DEX: 16, STR: 10}\n"
        "  - name: Borin\n    description: warrior\n    personality: hot-tempered\n"
        "    backstory: clan war veteran\n    char_class: fighter\n    race: dwarf\n"
        "    level: 5\n    stats: {DEX: 10, STR: 18}\n",
    )

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="distinct_stats", fixtures_dir=tmp_path)
    wren, borin = snapshot.characters[0], snapshot.characters[1]
    assert wren.core.name == "Wren"
    assert borin.core.name == "Borin"
    assert wren.core.level == 3
    assert borin.core.level == 5
    assert wren.stats == {"DEX": 16, "STR": 10}
    assert borin.stats == {"DEX": 10, "STR": 18}


def test_characters_list_each_pc_has_distinct_known_facts(tmp_path: Path) -> None:
    """AC#7: known_facts per-PC do not bleed across siblings.

    The mutable-default-arg trap (lang-review rule #2) would manifest here —
    a hydrator that appends to a shared default list across iterations
    would smear facts across every PC. The assertion checks each PC's
    fact list independently rather than the sum, so a shared-list bug
    cannot pass by accident.
    """
    _write_multi_pc_fixture(
        tmp_path,
        "distinct_facts",
        "  - name: Wren\n    description: scout\n    personality: cautious\n"
        "    backstory: scouted these tunnels before\n    char_class: thief\n    race: human\n"
        "    known_facts:\n"
        '      - content: "Wren saw the goblin king"\n'
        '        confidence: "Certain"\n'
        "  - name: Borin\n    description: warrior\n    personality: hot-tempered\n"
        "    backstory: clan war veteran\n    char_class: fighter\n    race: dwarf\n"
        "    known_facts:\n"
        '      - content: "Borin smelled smoke"\n'
        '        confidence: "Suspected"\n',
    )

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="distinct_facts", fixtures_dir=tmp_path)
    wren_facts = snapshot.characters[0].known_facts
    borin_facts = snapshot.characters[1].known_facts
    assert len(wren_facts) == 1, (
        f"Wren must have exactly 1 fact (not shared), got {len(wren_facts)}"
    )
    assert len(borin_facts) == 1, (
        f"Borin must have exactly 1 fact (not shared), got {len(borin_facts)}"
    )
    assert wren_facts[0].content == "Wren saw the goblin king"
    assert wren_facts[0].confidence == "Certain"
    assert borin_facts[0].content == "Borin smelled smoke"
    assert borin_facts[0].confidence == "Suspected"


def test_characters_list_shares_one_npc_roster_with_party(tmp_path: Path) -> None:
    """AC#7: the ``npcs:`` roster is a single shared list — every party
    member sees the same NPCs (one encounter, multiple PCs). The hydrator
    must not duplicate or per-PC-scope the npc list.
    """
    fixture = tmp_path / "shared_npcs.yaml"
    fixture.write_text(
        "genre: caverns_and_claudes\n"
        "world: default\n"
        "characters:\n"
        "  - name: Wren\n    description: scout\n    personality: cautious\n"
        "    backstory: scouted these tunnels before\n    char_class: thief\n    race: human\n"
        "  - name: Borin\n    description: warrior\n    personality: hot-tempered\n"
        "    backstory: clan war veteran\n    char_class: fighter\n    race: dwarf\n"
        "npcs:\n"
        "  - name: Rust Jaw\n    role: bandit\n    disposition: -15\n"
        "  - name: Iron Eye\n    role: bandit-lieutenant\n    disposition: -20\n",
        encoding="utf-8",
    )

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="shared_npcs", fixtures_dir=tmp_path)
    assert len(snapshot.characters) == 2
    npc_names = {n.core.name for n in snapshot.npcs}
    assert npc_names == {"Rust Jaw", "Iron Eye"}, (
        f"shared npc roster must hydrate intact; got {npc_names!r}"
    )


def test_singular_character_block_still_maps_to_position_zero(tmp_path: Path) -> None:
    """AC#3, AC#9: the legacy singular ``character:`` block remains supported
    and lands at ``snapshot.characters[0]``.

    Existing canonical fixtures (combat_brawl_wasteland, social_negotiation_tea,
    etc.) use the singular form; breaking backwards-compat here breaks every
    Wave 1 regression test in this file and the router test file.
    """
    fixture = tmp_path / "legacy_singular.yaml"
    fixture.write_text(
        "genre: caverns_and_claudes\n"
        "world: default\n"
        "character:\n"
        "  name: Wren\n"
        "  description: scout\n"
        "  personality: cautious\n"
        "  backstory: scouted these tunnels before\n"
        "  char_class: thief\n"
        "  race: human\n",
        encoding="utf-8",
    )

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="legacy_singular", fixtures_dir=tmp_path)
    assert len(snapshot.characters) == 1
    assert snapshot.characters[0].core.name == "Wren"


def test_both_character_and_characters_blocks_raises_FixtureValidationError(
    tmp_path: Path,
) -> None:
    """AC#4, AC#10: declaring BOTH ``character:`` and ``characters:`` is a
    fixture authoring bug — the hydrator MUST reject it loudly rather than
    silently pick one.

    "No Silent Fallbacks" (CLAUDE.md). If the author meant the legacy form,
    they wrote ``character:``; if they meant the new form, they wrote
    ``characters:``. Both present means the fixture is in an undefined
    state and the right answer is 422, not "pick one and hope."
    """
    # Both blocks are INDIVIDUALLY valid (each entry carries all non-blank
    # required fields). The only thing that can fail validation is the
    # conflict check itself — otherwise this test would pass on a hydrator
    # that has no conflict check at all but happens to raise during
    # per-entry pydantic validation.
    fixture = tmp_path / "both_blocks.yaml"
    fixture.write_text(
        "genre: caverns_and_claudes\n"
        "world: default\n"
        "character:\n"
        "  name: Solo\n"
        "  description: solo PC\n"
        "  personality: stoic\n"
        "  backstory: lone wanderer\n"
        "  char_class: ranger\n"
        "  race: elf\n"
        "characters:\n"
        "  - name: Party\n"
        "    description: a party member\n"
        "    personality: gregarious\n"
        "    backstory: tavern regular\n"
        "    char_class: bard\n"
        "    race: half-elf\n",
        encoding="utf-8",
    )

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError) as exc_info:
        hydrate_fixture(name="both_blocks", fixtures_dir=tmp_path)

    msg = str(exc_info.value).lower()
    assert "character" in msg, (
        f"FixtureValidationError must name the conflicting field "
        f"so the fixture author knows what to remove; got: {msg!r}"
    )


def test_missing_both_character_blocks_yields_empty_characters_list(
    tmp_path: Path,
) -> None:
    """AC#5: when NEITHER ``character:`` nor ``characters:`` is present,
    the hydrator continues — ``snapshot.characters`` is ``[]``.

    Behaviour preservation: legacy fixtures with NPCs only (seed worlds,
    cutscene-style fixtures) must continue to load. Do not regress to
    "characters block required."
    """
    fixture = tmp_path / "no_pcs.yaml"
    fixture.write_text(
        "genre: caverns_and_claudes\n"
        "world: default\n"
        "npcs:\n"
        "  - name: Solo NPC\n    role: bystander\n",
        encoding="utf-8",
    )

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="no_pcs", fixtures_dir=tmp_path)
    assert snapshot.characters == [], (
        f"missing character blocks must yield empty list, got {snapshot.characters!r}"
    )
    # NPC roster still loads — proves the hydrator didn't bail early.
    assert any(n.core.name == "Solo NPC" for n in snapshot.npcs)


def test_explicit_empty_characters_list_yields_empty_list(tmp_path: Path) -> None:
    """AC#8 (empty-list case): a fixture with ``characters: []`` is
    semantically different from a missing block — the author explicitly
    declared "no PCs in this scene." Hydrator treats both identically:
    empty list, no error.
    """
    fixture = tmp_path / "empty_list.yaml"
    fixture.write_text(
        "genre: caverns_and_claudes\nworld: default\ncharacters: []\n",
        encoding="utf-8",
    )

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="empty_list", fixtures_dir=tmp_path)
    assert snapshot.characters == []


def test_malformed_character_entry_in_list_raises_FixtureValidationError(
    tmp_path: Path,
) -> None:
    """AC#6: a malformed entry in the ``characters:`` list (e.g. missing
    required ``name``) MUST raise FixtureValidationError — never silently
    skip.

    Sibling pattern from 50-19 (known_facts wrong shape) made the same
    choice: save-bearing data fails loud. The HTTP layer maps this to 422.
    """
    # First entry is FULLY VALID. Only the second entry is malformed
    # (missing required ``name``). This separates "the hydrator rejects
    # the bad entry" from "the hydrator chokes on the first entry too" —
    # so the test cannot pass by accident on a hydrator that simply
    # blows up on the first valid entry.
    _write_multi_pc_fixture(
        tmp_path,
        "bad_entry",
        "  - name: Wren\n    description: scout\n    personality: cautious\n"
        "    backstory: scouted these tunnels before\n    char_class: thief\n    race: human\n"
        "  - description: malformed entry has no name\n"
        "    personality: also broken\n"
        "    backstory: nobody\n    char_class: missing\n    race: orc\n",
    )

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError):
        hydrate_fixture(name="bad_entry", fixtures_dir=tmp_path)


def test_malformed_character_entry_does_not_silently_skip(tmp_path: Path) -> None:
    """AC#6 + lang-review rule #1 (silent exception swallowing): a future
    refactor must NOT replace strict validation with
    ``[hydrate(e) for e in entries if valid(e)]`` — that would silently
    drop bad entries and produce ``len(snapshot.characters) == 1`` instead
    of raising.

    Assert the loud failure mode by raising, not by counting survivors:
    a test that asserted ``len == 1`` would PASS on a silent-skip
    implementation, which is exactly the bug class this test guards.
    """
    _write_multi_pc_fixture(
        tmp_path,
        "silent_skip_guard",
        "  - name: Wren\n    description: scout\n    personality: cautious\n"
        "    backstory: scouted these tunnels before\n    char_class: thief\n    race: human\n"
        "  - name: ''\n    description: blank name\n    personality: blank\n"
        "    backstory: nobody\n    char_class: missing\n    race: orc\n",
    )

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError):
        hydrate_fixture(name="silent_skip_guard", fixtures_dir=tmp_path)


def test_characters_list_not_a_list_raises_FixtureValidationError(
    tmp_path: Path,
) -> None:
    """ADR-092 §"Failure is loud" + lang-review rule #1: if a fixture sets
    ``characters:`` to a mapping or scalar instead of a list, the hydrator
    MUST surface a structured error.

    Same discipline 50-19 added for ``known_facts``. The base hydrator's
    sibling pattern (``inventory`` / ``statuses``) silently ignores wrong
    shapes, which would mask a fixture typo — but ``characters`` is
    save-bearing and the silent-skip cost is higher than the loud-fail cost.
    """
    fixture = tmp_path / "bad_characters_shape.yaml"
    fixture.write_text(
        "genre: caverns_and_claudes\nworld: default\ncharacters:\n  not_a_list: true\n",
        encoding="utf-8",
    )

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError):
        hydrate_fixture(name="bad_characters_shape", fixtures_dir=tmp_path)


# ── Story 50-20: scenario_state hydration (ADR-092 follow-on) ───────────────
#
# RED tests for the top-level ``scenario_state:`` block. Hydration projects
# the block into ``GameSnapshot.scenario_state`` (a ``ScenarioState``), which
# holds:
#
#     * clue_graph (ClueGraph — list of ClueNode)
#     * discovered_clues (set[str] of clue ids)
#     * npc_roles (dict[NPC name -> "guilty" | "witness" | "innocent"])
#     * guilty_npc (string — accepted as name or id; persists as the canonical id)
#     * tension (float, clamped to [0.0, 1.0])
#
# Validation rules (ACs 3-9):
#   * ClueGraph nodes must satisfy ``ClueNode`` pydantic shape — missing
#     required fields raise FixtureValidationError (HTTP 422).
#   * ``discovered_clues`` must obey the DAG: a discovered clue with a
#     ``requires`` entry not also in the final ``discovered_clues`` set is
#     invalid. The hydrator validates set-membership against the final
#     declared set — YAML order is irrelevant per the AC2 ``set[str]``
#     typing (Reviewer [HIGH-1] rework, 50-20).
#   * ``npc_roles`` values must be one of ("guilty", "witness", "innocent").
#   * ``guilty_npc`` must resolve to an entry in the fixture's ``npcs``
#     roster (name match OR id match — fixture-author convenience).
#   * ``tension`` is clamped silently to [0.0, 1.0]; out-of-range is NOT a
#     422 (matches ``ScenarioState.set_tension()`` semantics).
#   * Missing ``scenario_state:`` block: ``snapshot.scenario_state`` is
#     None (backwards-compat with the four canonical pre-50-20 fixtures).
#   * Malformed ``scenario_state:`` (e.g. a list at the top, or a typo'd
#     child key) raises FixtureValidationError — no silent skip.


def _write_scenario_state_fixture(
    tmp_path: Path,
    name: str,
    *,
    npcs_yaml: str = "",
    scenario_state_yaml: str | None = None,
) -> None:
    """Helper: write a minimal mystery fixture with an optional
    scenario_state block.

    Keeps test bodies focused on the scenario_state assertion under test
    rather than YAML scaffolding.
    """
    body = (
        "genre: tea_and_murder\n"
        "world: victoria\n"
        "character:\n"
        "  name: Investigator\n"
        "  description: A keen-eyed sleuth\n"
        "  personality: observant\n"
        "  backstory: tea, biscuits, and bodies\n"
        "  char_class: detective\n"
        "  race: human\n"
    )
    if npcs_yaml:
        body += "npcs:\n" + npcs_yaml
    if scenario_state_yaml is not None:
        body += scenario_state_yaml
    (tmp_path / f"{name}.yaml").write_text(body, encoding="utf-8")


# A canonical clue graph used across multiple tests — three nodes in a chain:
# ``clue_a`` → ``clue_b`` → ``clue_c``. ``clue_b`` requires ``clue_a``;
# ``clue_c`` requires ``clue_b``.
_CLUE_GRAPH_CHAIN_YAML = (
    "scenario_state:\n"
    "  clue_graph:\n"
    "    nodes:\n"
    "      - id: clue_a\n"
    "        type: physical_evidence\n"
    "        description: The teacup is cracked along the rim\n"
    "        discovery_method: observation\n"
    "        visibility: public\n"
    "        requires: []\n"
    "      - id: clue_b\n"
    "        type: testimony\n"
    "        description: The butler heard raised voices\n"
    "        discovery_method: interrogation\n"
    "        visibility: public\n"
    "        requires: [clue_a]\n"
    "      - id: clue_c\n"
    "        type: motive\n"
    "        description: Insurance policy named the victim\n"
    "        discovery_method: research\n"
    "        visibility: secret\n"
    "        requires: [clue_b]\n"
)


_NPCS_THREE_SUSPECTS = (
    "  - name: Lady Ashworth\n    role: hostess\n    disposition: 0\n"
    "  - name: Mr. Pike\n    role: butler\n    disposition: 0\n"
    "  - name: Dr. Hartmoor\n    role: physician\n    disposition: 0\n"
)


def test_scenario_state_block_hydrates_all_five_fields(tmp_path: Path) -> None:
    """AC#1, #2, #11: a complete scenario_state block projects to all 5
    ScenarioState fields with the values declared in YAML.

    This is the happy path — if the snapshot is missing even one field
    after hydration, Wave 2 mystery fixtures cannot stage the scenario
    state they need.
    """
    _write_scenario_state_fixture(
        tmp_path,
        "complete_block",
        npcs_yaml=_NPCS_THREE_SUSPECTS,
        scenario_state_yaml=(
            _CLUE_GRAPH_CHAIN_YAML
            + "  discovered_clues: [clue_a, clue_b]\n"
            + "  npc_roles:\n"
            + "    Lady Ashworth: guilty\n"
            + "    Mr. Pike: witness\n"
            + "    Dr. Hartmoor: innocent\n"
            + "  guilty_npc: Lady Ashworth\n"
            + "  tension: 0.65\n"
        ),
    )

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="complete_block", fixtures_dir=tmp_path)

    assert snapshot.scenario_state is not None, (
        "scenario_state block was provided — snapshot.scenario_state must not be None"
    )
    state = snapshot.scenario_state

    # clue_graph: three nodes, in YAML order
    assert [n.id for n in state.clue_graph.nodes] == ["clue_a", "clue_b", "clue_c"], (
        f"clue_graph node ids drifted from YAML order; got "
        f"{[n.id for n in state.clue_graph.nodes]!r}"
    )

    # discovered_clues: set semantics (membership, not order)
    assert state.discovered_clues == {"clue_a", "clue_b"}, (
        f"discovered_clues mismatch; got {state.discovered_clues!r}"
    )

    # npc_roles: each NPC mapped to its role string
    assert state.npc_roles == {
        "Lady Ashworth": "guilty",
        "Mr. Pike": "witness",
        "Dr. Hartmoor": "innocent",
    }, f"npc_roles mismatch; got {state.npc_roles!r}"

    # guilty_npc: name resolved against roster (acceptable persistence shape)
    assert state.guilty_npc == "Lady Ashworth", (
        f"guilty_npc must persist as the resolved roster entry; got {state.guilty_npc!r}"
    )

    # tension: exact YAML float (no clamping needed at 0.65)
    assert state.tension == pytest.approx(0.65), f"tension mismatch; got {state.tension!r}"


def test_partial_scenario_state_block_uses_defaults(tmp_path: Path) -> None:
    """AC#2, #12: a partial block (only clue_graph) hydrates with empty
    defaults for every omitted field — no silent fabrication.

    Guards against an implementation that requires the full block and
    rejects partials, or one that invents npc_roles from the npcs list.
    """
    _write_scenario_state_fixture(
        tmp_path,
        "partial_block",
        npcs_yaml=_NPCS_THREE_SUSPECTS,
        scenario_state_yaml=_CLUE_GRAPH_CHAIN_YAML,
    )

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="partial_block", fixtures_dir=tmp_path)

    state = snapshot.scenario_state
    assert state is not None, "partial block must still hydrate scenario_state"
    assert len(state.clue_graph.nodes) == 3, (
        "clue_graph from YAML must survive — only omitted fields default"
    )
    # All omitted fields take their pydantic defaults — no invented values.
    assert state.discovered_clues == set()
    assert state.npc_roles == {}
    assert state.guilty_npc == ""
    assert state.tension == 0.0


def test_missing_scenario_state_block_leaves_snapshot_none(tmp_path: Path) -> None:
    """AC#8, #10: a fixture WITHOUT a scenario_state block hydrates normally
    and ``snapshot.scenario_state`` remains None.

    Without this guard, 50-20 would break the four pre-existing canonical
    fixtures (none of which carry scenario_state) by requiring the new
    block.
    """
    _write_scenario_state_fixture(
        tmp_path,
        "no_scenario_block",
        npcs_yaml=_NPCS_THREE_SUSPECTS,
        scenario_state_yaml=None,
    )

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="no_scenario_block", fixtures_dir=tmp_path)

    assert snapshot.scenario_state is None, (
        f"omitting scenario_state: must leave snapshot.scenario_state at the "
        f"GameSnapshot pydantic default (None); got {snapshot.scenario_state!r}"
    )


def test_clue_node_type_alias_populates_clue_type(tmp_path: Path) -> None:
    """AC#3: ClueNode uses ``type:`` as the YAML key (alias for ``clue_type``).

    ClueNode declares ``clue_type: str = Field(alias="type",
    populate_by_name=True)``. The fixture author writes ``type:``; the
    hydrated model exposes the value as ``clue_type``. A regression that
    drops the alias would force fixture authors to write ``clue_type:``
    or fail mysteriously — neither is acceptable.
    """
    _write_scenario_state_fixture(
        tmp_path,
        "type_alias",
        npcs_yaml=_NPCS_THREE_SUSPECTS,
        scenario_state_yaml=_CLUE_GRAPH_CHAIN_YAML,
    )

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="type_alias", fixtures_dir=tmp_path)
    state = snapshot.scenario_state
    assert state is not None

    types = [node.clue_type for node in state.clue_graph.nodes]
    assert types == ["physical_evidence", "testimony", "motive"], (
        f"ClueNode.type → clue_type alias broken; got {types!r}"
    )


def test_discovered_clue_with_unmet_prerequisite_raises(tmp_path: Path) -> None:
    """AC#4, #13: pre-discovering ``clue_b`` without ``clue_a`` violates
    the DAG and must raise FixtureValidationError.

    The hydrator validates each declared clue's ``requires`` against the
    final declared set (no replay through ``ScenarioState.discover_clue``,
    post-rework). A FixtureValidationError must surface at the HTTP
    boundary as 422 with field-level detail — never a leaked 500 from
    an internal exception type.
    """
    _write_scenario_state_fixture(
        tmp_path,
        "bad_dag",
        npcs_yaml=_NPCS_THREE_SUSPECTS,
        scenario_state_yaml=(
            _CLUE_GRAPH_CHAIN_YAML + "  discovered_clues: [clue_b]\n"  # missing clue_a prerequisite
        ),
    )

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError) as exc_info:
        hydrate_fixture(name="bad_dag", fixtures_dir=tmp_path)

    msg = str(exc_info.value).lower()
    # The error must name the offending clue id and its missing prereq so
    # the fixture author knows what to fix without re-running a debugger.
    assert "clue_b" in msg, f"DAG violation error must name the offending clue id; got {msg!r}"
    assert "clue_a" in msg, f"DAG violation error must name the missing prerequisite; got {msg!r}"


def test_invalid_npc_role_value_raises(tmp_path: Path) -> None:
    """AC#5, #15: an npc_roles value outside ("guilty", "witness", "innocent")
    must raise FixtureValidationError.

    Without this guard, a fixture typo like ``role: kiler`` would silently
    populate ``npc_roles["X"] = "kiler"`` and any downstream
    "if role == 'witness'" branch would silently false-out.
    """
    _write_scenario_state_fixture(
        tmp_path,
        "bad_role",
        npcs_yaml=_NPCS_THREE_SUSPECTS,
        scenario_state_yaml=(
            _CLUE_GRAPH_CHAIN_YAML
            + "  npc_roles:\n"
            + "    Lady Ashworth: murderer\n"  # not in allowed set
        ),
    )

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError) as exc_info:
        hydrate_fixture(name="bad_role", fixtures_dir=tmp_path)

    msg = str(exc_info.value).lower()
    assert "murderer" in msg or "role" in msg, (
        f"role validation error must name the bad value or 'role'; got {msg!r}"
    )


def test_guilty_npc_missing_from_roster_raises(tmp_path: Path) -> None:
    """AC#6, #14: a guilty_npc not present in the npcs roster must raise
    FixtureValidationError.

    The error should expose the available roster so the fixture author
    can spot the typo without grepping the genre pack.
    """
    _write_scenario_state_fixture(
        tmp_path,
        "missing_guilty",
        npcs_yaml=_NPCS_THREE_SUSPECTS,
        scenario_state_yaml=(
            _CLUE_GRAPH_CHAIN_YAML + "  guilty_npc: Professor Plum\n"  # not in roster
        ),
    )

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError) as exc_info:
        hydrate_fixture(name="missing_guilty", fixtures_dir=tmp_path)

    msg = str(exc_info.value)
    # The error must name the missing NPC. The roster hint is desirable
    # but not strictly mandated by the AC text — assert the minimum.
    assert "Professor Plum" in msg or "guilty" in msg.lower(), (
        f"guilty_npc validation error must name the missing NPC; got {msg!r}"
    )


@pytest.mark.parametrize(
    "raw_tension,expected",
    [
        (1.5, 1.0),
        (2.0, 1.0),
        (-0.2, 0.0),
        (-1.0, 0.0),
        (0.5, 0.5),
        (0.0, 0.0),
        (1.0, 1.0),
    ],
)
def test_tension_clamps_silently_to_unit_interval(
    tmp_path: Path, raw_tension: float, expected: float
) -> None:
    """AC#7: tension outside [0.0, 1.0] is clamped, NOT rejected with 422.

    This matches the ``ScenarioState.set_tension()`` semantic for runtime
    mutation — fixtures get the same forgiving contract. Boundary values
    (0.0, 1.0) and an in-range value (0.5) are included to guard against
    an implementation that clamps to (0,1) exclusive or off-by-one.
    """
    _write_scenario_state_fixture(
        tmp_path,
        f"tension_{str(raw_tension).replace('.', '_').replace('-', 'neg')}",
        npcs_yaml=_NPCS_THREE_SUSPECTS,
        scenario_state_yaml=(_CLUE_GRAPH_CHAIN_YAML + f"  tension: {raw_tension}\n"),
    )

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(
        name=f"tension_{str(raw_tension).replace('.', '_').replace('-', 'neg')}",
        fixtures_dir=tmp_path,
    )
    state = snapshot.scenario_state
    assert state is not None
    assert state.tension == pytest.approx(expected), (
        f"tension={raw_tension} must clamp to {expected}; got {state.tension!r}"
    )


def test_malformed_scenario_state_block_raises(tmp_path: Path) -> None:
    """AC#9 + ADR-092 "Failure is loud": a scenario_state declared as a
    list (or scalar) instead of a mapping must raise FixtureValidationError.

    Lang-review rule #1 (no silent exception swallowing) — a future
    refactor must NOT replace the explicit shape check with
    ``data.get("scenario_state", {})`` and silently coerce the wrong
    shape away. Assert by raising, not by counting an empty
    scenario_state survivor.
    """
    fixture = tmp_path / "bad_scenario_shape.yaml"
    fixture.write_text(
        "genre: tea_and_murder\n"
        "world: victoria\n"
        "character:\n"
        "  name: Investigator\n"
        "  description: A keen-eyed sleuth\n"
        "  personality: observant\n"
        "  backstory: tea, biscuits, and bodies\n"
        "  char_class: detective\n"
        "  race: human\n"
        "scenario_state:\n"
        "  - just_a_list_item\n",
        encoding="utf-8",
    )

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError):
        hydrate_fixture(name="bad_scenario_shape", fixtures_dir=tmp_path)


def test_clue_node_missing_required_field_raises(tmp_path: Path) -> None:
    """AC#3 + ClueNode ``extra='forbid'``: a clue node missing
    ``discovery_method`` (required field) must raise FixtureValidationError.

    Pydantic catches the missing field; the hydrator's job is to wrap
    ``ValidationError`` as ``FixtureValidationError`` so the HTTP layer
    returns 422 rather than leaking ``pydantic.ValidationError`` as 500.
    """
    _write_scenario_state_fixture(
        tmp_path,
        "bad_clue_shape",
        npcs_yaml=_NPCS_THREE_SUSPECTS,
        scenario_state_yaml=(
            "scenario_state:\n"
            "  clue_graph:\n"
            "    nodes:\n"
            "      - id: orphan_clue\n"
            "        type: physical_evidence\n"
            "        description: missing discovery_method below\n"
            # discovery_method intentionally omitted
            "        visibility: public\n"
        ),
    )

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError):
        hydrate_fixture(name="bad_clue_shape", fixtures_dir=tmp_path)


def test_guilty_npc_resolves_by_name_or_id(tmp_path: Path) -> None:
    """AC#6 + SM Assessment guidance: the fixture may identify the guilty
    NPC by ``name`` (matches ``npc_roles`` keys, fixture-author ergonomic)
    OR by ``id`` if the NPC has one.

    The npcs block in canonical fixtures doesn't carry ``id`` — NPCs are
    identified by name. So name-match must work; id-match is a forward
    compatibility hedge. Both forms hydrate without error.
    """
    _write_scenario_state_fixture(
        tmp_path,
        "guilty_by_name",
        npcs_yaml=_NPCS_THREE_SUSPECTS,
        scenario_state_yaml=(
            _CLUE_GRAPH_CHAIN_YAML + "  guilty_npc: Mr. Pike\n"  # match by name
        ),
    )

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="guilty_by_name", fixtures_dir=tmp_path)
    state = snapshot.scenario_state
    assert state is not None
    # Whether the implementation stores name or resolved id, it must be
    # non-empty and refer to the named suspect — NOT the empty default.
    assert state.guilty_npc, "guilty_npc resolved by name must not be empty after hydration"
    assert "Pike" in state.guilty_npc, (
        f"guilty_npc must refer to the named suspect; got {state.guilty_npc!r}"
    )


def test_canonical_fixtures_still_hydrate_with_scenario_state_implementation() -> None:
    """AC#10 (backwards-compat): the four canonical pre-50-20 fixtures must
    continue to hydrate cleanly after the scenario_state branch is added.

    Wiring-test (CLAUDE.md "Every Test Suite Needs a Wiring Test"): proves
    the 50-20 hydrator change doesn't accidentally require the new block.
    """
    from sidequest.game.scene_harness import hydrate_fixture

    real_fixtures = (
        "combat_brawl_wasteland",
        "combat_dogfight_space",
        "social_negotiation_tea",
        "social_poker_wasteland",
    )
    for fixture_name in real_fixtures:
        snapshot = hydrate_fixture(name=fixture_name, fixtures_dir=CANONICAL_FIXTURES_DIR)
        # No scenario_state block means snapshot.scenario_state stays None.
        # A future canonical fixture that opts INTO scenario_state would
        # have a non-None value; until then, this assertion is the
        # regression guard.
        assert snapshot.scenario_state is None, (
            f"{fixture_name}: pre-50-20 fixture must keep "
            f"snapshot.scenario_state=None; got {snapshot.scenario_state!r}"
        )


# ── Story 50-20 (rework, Reviewer [HIGH-1]): DAG-order independence ─────────
#
# `discovered_clues` is documented in AC#2 as ``set[str]`` — unordered by
# definition. AC#4's "unmet requires" must be checked against the FINAL
# declared set, not the per-clue replay state. The initial 50-20 impl built
# a ScenarioState with empty discovered_clues and replayed each declared id
# through ``ScenarioState.discover_clue()`` in YAML order, which raises if
# any clue's requires aren't yet in the (intermediate) discovered set.
#
# This rejects valid fixtures where the final set IS DAG-valid but the YAML
# listing isn't topologically sorted. The Reviewer found that
# ``discovered_clues: [clue_b, clue_a]`` (both clues present, clue_b
# requires clue_a) raised with the misleading message
# "missing prerequisites ['clue_a']" — even though clue_a is in the YAML
# immediately below clue_b.
#
# Fix discipline: validate against the final declared set, not the replay
# state. The hydrator should accept any YAML ordering whose final set
# satisfies the DAG, and reject only when a clue's requires are genuinely
# absent from the declared set.


def test_discovered_clues_in_reverse_yaml_order_still_hydrate(tmp_path: Path) -> None:
    """Reviewer [HIGH-1] regression: a DAG-valid fixture in reverse YAML
    order must hydrate cleanly.

    ``discovered_clues: [clue_b, clue_a]`` — both clues are in the YAML;
    the final set ``{clue_a, clue_b}`` satisfies ``clue_b.requires == [clue_a]``.
    AC#2 documents ``discovered_clues`` as a set, so YAML order must be
    irrelevant to the validation verdict.

    Pre-fix behavior: raises ``FixtureValidationError`` claiming clue_a
    is missing — which is misleading since clue_a is literally on the
    next line of YAML.
    Post-fix behavior: hydrates successfully with the final set populated.
    """
    _write_scenario_state_fixture(
        tmp_path,
        "reverse_order",
        npcs_yaml=_NPCS_THREE_SUSPECTS,
        scenario_state_yaml=(_CLUE_GRAPH_CHAIN_YAML + "  discovered_clues: [clue_b, clue_a]\n"),
    )

    from sidequest.game.scene_harness import hydrate_fixture

    # Must not raise — the final set is DAG-valid.
    snapshot = hydrate_fixture(name="reverse_order", fixtures_dir=tmp_path)
    state = snapshot.scenario_state
    assert state is not None, "reverse-order DAG-valid fixture must hydrate"
    assert state.discovered_clues == {"clue_a", "clue_b"}, (
        f"final discovered_clues set must contain both ids regardless of YAML "
        f"order; got {state.discovered_clues!r}"
    )


def test_discovered_clues_full_chain_in_reverse_order_still_hydrates(tmp_path: Path) -> None:
    """Stress the reverse-order case with the full three-clue chain.

    ``discovered_clues: [clue_c, clue_b, clue_a]`` — every clue present,
    but listed in fully reverse topological order. Final set
    ``{clue_a, clue_b, clue_c}`` satisfies all `requires` edges.

    This guards against an implementation that accepts simple-pair reverse
    order (the previous test) but breaks on longer chains by, say, only
    sorting one level deep.
    """
    _write_scenario_state_fixture(
        tmp_path,
        "full_chain_reverse",
        npcs_yaml=_NPCS_THREE_SUSPECTS,
        scenario_state_yaml=(
            _CLUE_GRAPH_CHAIN_YAML + "  discovered_clues: [clue_c, clue_b, clue_a]\n"
        ),
    )

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="full_chain_reverse", fixtures_dir=tmp_path)
    state = snapshot.scenario_state
    assert state is not None
    assert state.discovered_clues == {"clue_a", "clue_b", "clue_c"}, (
        f"full reverse-chain fixture must produce the complete set; got {state.discovered_clues!r}"
    )


def test_discovered_clue_skipping_middle_of_chain_still_raises(tmp_path: Path) -> None:
    """Negative regression: after the [HIGH-1] fix, a fixture that skips a
    chain step must still raise — the rejection logic must check the final
    set, not just "any subset of the YAML list works."

    ``discovered_clues: [clue_a, clue_c]`` — clue_b is GENUINELY absent
    from the declared set. clue_c requires clue_b which is missing. The
    final-set check must catch this.

    Pre-fix behavior: raises (replay sees clue_c with missing clue_b).
    Post-fix behavior: must still raise — the final set is NOT DAG-valid.
    """
    _write_scenario_state_fixture(
        tmp_path,
        "skip_middle",
        npcs_yaml=_NPCS_THREE_SUSPECTS,
        scenario_state_yaml=(_CLUE_GRAPH_CHAIN_YAML + "  discovered_clues: [clue_a, clue_c]\n"),
    )

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError) as exc_info:
        hydrate_fixture(name="skip_middle", fixtures_dir=tmp_path)

    msg = str(exc_info.value).lower()
    # The error must name clue_c (the unsatisfied clue) and clue_b (the
    # missing prerequisite) — same field-detail discipline as the
    # original test_discovered_clue_with_unmet_prerequisite_raises.
    assert "clue_c" in msg, (
        f"DAG violation error must name the offending clue id (clue_c); got {msg!r}"
    )
    assert "clue_b" in msg, (
        f"DAG violation error must name the missing prerequisite (clue_b); got {msg!r}"
    )


# ════════════════════════════════════════════════════════════════════════════
# Story 50-22 (RED): hydrate ``character.abilities`` + top-level ``magic_state``
# ════════════════════════════════════════════════════════════════════════════
#
# ADR-092 follow-on to 50-21. Two new hydration paths:
#   1. ``abilities:`` under a ``character:`` block → ``Character.abilities``
#      (``list[AbilityDefinition]`` — sidequest/protocol/models.py:42).
#   2. top-level ``magic_state:`` → ``GameSnapshot.magic_state``
#      (``MagicState`` — sidequest/magic/state.py:123).
#
# Discipline mirrors the scenario_state branch (50-20):
#   * Absent block → snapshot field stays at the pydantic default
#     (``abilities=[]``, ``magic_state=None``) so the four canonical
#     pre-50-22 fixtures keep working unchanged.
#   * Malformed block → ``FixtureValidationError`` (HTTP 422). NEVER a
#     silent skip and NEVER a silent empty default (ADR-014 "magic state
#     is Diamond"; ADR-092 "Failure is loud"; CLAUDE.md "No Silent
#     Fallbacks"; lang-review #1 / #11).
#
# IMPORTANT (TEA deviation — see session ## Design Deviations): the session
# Technical Approach's example ``config: {world_slug, ledger_bars: [],
# confrontations_by_name: {}}`` is INVALID. ``WorldMagicConfig``
# (sidequest/magic/models.py:287 — NOT genre/models/magic.py as the session
# Schema References claim) is ``extra="forbid"`` with 11 required fields and
# no ``confrontations_by_name`` field. These tests carry the corrected
# minimal-valid config shape; Dev must follow the tests, not the session
# example.

# A minimal but fully-valid ``WorldMagicConfig`` rendered as the ``config:``
# sub-block of a ``magic_state:`` fixture block. Every required field of
# WorldMagicConfig (sidequest/magic/models.py:287) is present; values mirror
# the canonical ``tests/magic/conftest.py::world_config`` so they are
# known-good against the pydantic validators (WorldKnowledge awareness
# ordering, intensity bounds, LedgerBarSpec threshold/scope rules).
_MINIMAL_WORLD_MAGIC_CONFIG_YAML = (
    "  config:\n"
    "    world_slug: coyote_star\n"
    "    genre_slug: space_opera\n"
    "    allowed_sources: [innate]\n"
    "    active_plugins: [innate_v1]\n"
    "    intensity: 0.25\n"
    "    world_knowledge:\n"
    "      primary: classified\n"
    "      local_register: folkloric\n"
    "    visibility:\n"
    "      primary: feared\n"
    "      local_register: dismissed\n"
    "    hard_limits:\n"
    "      - id: psionics_never_decisive\n"
    "        description: psionics can never be the decisive factor\n"
    "    cost_types: [sanity]\n"
    "    ledger_bars: []\n"
    "    narrator_register: clinical\n"
)

# The full minimal ``magic_state:`` block (config only — every other
# MagicState field defaults: ledger={}, working_log=[], confrontations=[],
# control_tier={}, known/prepared/spent_spells={}, reliquary=[]).
_MAGIC_STATE_MINIMAL_YAML = "magic_state:\n" + _MINIMAL_WORLD_MAGIC_CONFIG_YAML

# A two-ability block, written at the indentation a ``character:`` mapping
# expects (key at 2 spaces, list dashes at 4, entry keys at 6). Covers an
# explicit ``involuntary: true`` and a defaulted (omitted) one.
_ABILITIES_TWO_YAML = (
    "  abilities:\n"
    "    - name: Voidstep\n"
    "      genre_description: Slip a half-second sideways out of causality\n"
    "      mechanical_effect: Once per scene, negate one incoming consequence\n"
    "      source: Class\n"
    "      involuntary: false\n"
    "    - name: The Bleeding-Through\n"
    "      genre_description: The other side notices you noticing it\n"
    "      mechanical_effect: On sanity threshold cross, narrator may act\n"
    "      source: Race\n"
)


def _write_magic_fixture(
    tmp_path: Path,
    name: str,
    *,
    abilities_yaml: str | None = None,
    magic_state_yaml: str | None = None,
    extra_character_yaml: str = "",
) -> None:
    """Write a minimal magic-flavored fixture.

    ``abilities_yaml`` is appended INSIDE the ``character:`` block (it must
    be indented as a character-level key). ``magic_state_yaml`` is appended
    at the top level. Either may be ``None`` to omit that block entirely
    (the backward-compat / regression-lock path).
    """
    body = (
        "genre: space_opera\n"
        "world: coyote_star\n"
        "character:\n"
        "  name: Practitioner\n"
        "  description: A focused adept of the deep arts\n"
        "  personality: Focused and wary\n"
        "  backstory: Apprenticed in the classified registers\n"
        "  char_class: Mage\n"
        "  race: Human\n"
    )
    if extra_character_yaml:
        body += extra_character_yaml
    if abilities_yaml is not None:
        body += abilities_yaml
    if magic_state_yaml is not None:
        body += magic_state_yaml
    (tmp_path / f"{name}.yaml").write_text(body, encoding="utf-8")


# ── AC-1: Character.abilities hydration (happy paths) ───────────────────────


def test_character_abilities_block_hydrates_all_fields(tmp_path: Path) -> None:
    """AC-1: a ``abilities:`` list under ``character:`` projects to
    ``Character.abilities`` with every AbilityDefinition field preserved.

    RED driver: today ``_hydrate_character`` never reads ``abilities`` and
    never passes it to the ``Character`` constructor, so the field stays at
    its pydantic default ``[]`` — this assertion fails until 50-22 lands.
    """
    _write_magic_fixture(tmp_path, "abil_full", abilities_yaml=_ABILITIES_TWO_YAML)

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="abil_full", fixtures_dir=tmp_path)
    abilities = snapshot.characters[0].abilities

    assert len(abilities) == 2, (
        f"both declared abilities must hydrate; got {len(abilities)}: "
        f"{[a.name for a in abilities]!r}"
    )
    first, second = abilities
    assert first.name == "Voidstep"
    assert first.genre_description == "Slip a half-second sideways out of causality"
    assert first.mechanical_effect == "Once per scene, negate one incoming consequence"
    assert str(first.source) == "Class", f"source enum must round-trip; got {first.source!r}"
    assert first.involuntary is False
    assert second.name == "The Bleeding-Through"
    assert str(second.source) == "Race"


def test_ability_involuntary_defaults_false_when_omitted(tmp_path: Path) -> None:
    """AC-1: an ability entry omitting ``involuntary`` defaults to ``False``
    (AbilityDefinition.involuntary default), NOT raised, NOT True.

    The second entry of ``_ABILITIES_TWO_YAML`` omits ``involuntary``.
    """
    _write_magic_fixture(tmp_path, "abil_default", abilities_yaml=_ABILITIES_TWO_YAML)

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="abil_default", fixtures_dir=tmp_path)
    bleeding = snapshot.characters[0].abilities[1]
    assert bleeding.involuntary is False, (
        f"omitted involuntary must default to False; got {bleeding.involuntary!r}"
    )


def test_missing_abilities_block_defaults_to_empty_list(tmp_path: Path) -> None:
    """AC-1 (backward compat / regression lock): a character with NO
    ``abilities:`` key hydrates ``Character.abilities == []``.

    This passes both before and after 50-22 — it guards against an
    implementation that *requires* the block or crashes on its absence.
    """
    _write_magic_fixture(tmp_path, "abil_absent", abilities_yaml=None)

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="abil_absent", fixtures_dir=tmp_path)
    assert snapshot.characters[0].abilities == [], (
        f"omitting abilities: must yield []; got {snapshot.characters[0].abilities!r}"
    )


def test_empty_abilities_list_hydrates_to_empty(tmp_path: Path) -> None:
    """AC-1: an explicit empty ``abilities: []`` hydrates to ``[]`` —
    distinct from a malformed shape, this is a valid no-op declaration.
    """
    _write_magic_fixture(tmp_path, "abil_empty", abilities_yaml="  abilities: []\n")

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="abil_empty", fixtures_dir=tmp_path)
    assert snapshot.characters[0].abilities == []


@pytest.mark.parametrize("source", ["Race", "Class", "Item", "Play"])
def test_ability_all_four_sources_hydrate(tmp_path: Path, source: str) -> None:
    """AC-1: every ``AbilitySource`` enum member round-trips through the
    fixture. A regression that hardcodes one source or drops the enum
    coercion would fail at least one parametrization.
    """
    abilities_yaml = (
        "  abilities:\n"
        "    - name: Test Power\n"
        "      genre_description: does a thing\n"
        "      mechanical_effect: mechanically does the thing\n"
        f"      source: {source}\n"
    )
    _write_magic_fixture(tmp_path, f"abil_src_{source.lower()}", abilities_yaml=abilities_yaml)

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name=f"abil_src_{source.lower()}", fixtures_dir=tmp_path)
    ability = snapshot.characters[0].abilities[0]
    assert str(ability.source) == source, (
        f"source {source!r} must survive hydration; got {ability.source!r}"
    )


def test_abilities_hydrate_under_multi_pc_characters_list(tmp_path: Path) -> None:
    """AC-1 + AC-4: ``abilities:`` works under a ``characters:`` LIST entry,
    not only the legacy singular ``character:`` block.

    Both shapes funnel through ``_hydrate_character``; this proves the new
    abilities branch is reached from the multi-PC path too (the path James's
    "Rux" save and every MP fixture take).
    """
    body = (
        "genre: space_opera\n"
        "world: coyote_star\n"
        "characters:\n"
        "  - name: Adept One\n"
        "    description: first practitioner\n"
        "    personality: calm\n"
        "    backstory: trained early\n"
        "    char_class: Mage\n"
        "    race: Human\n"
        "    abilities:\n"
        "      - name: Spark\n"
        "        genre_description: a small ignition\n"
        "        mechanical_effect: lights tinder\n"
        "        source: Class\n"
        "  - name: Adept Two\n"
        "    description: second practitioner\n"
        "    personality: brash\n"
        "    backstory: self-taught\n"
        "    char_class: Mage\n"
        "    race: Human\n"
    )
    (tmp_path / "multi_abil.yaml").write_text(body, encoding="utf-8")

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="multi_abil", fixtures_dir=tmp_path)
    assert len(snapshot.characters) == 2
    assert [a.name for a in snapshot.characters[0].abilities] == ["Spark"], (
        f"characters[0] abilities must hydrate from the list path; "
        f"got {snapshot.characters[0].abilities!r}"
    )
    assert snapshot.characters[1].abilities == [], (
        "characters[1] declared no abilities — must stay [] (no cross-PC bleed)"
    )


# ── AC-1 / AC-3: malformed abilities fail LOUDLY ────────────────────────────


def test_ability_missing_required_field_raises(tmp_path: Path) -> None:
    """AC-1, AC-3, lang-review #1/#11: an ability entry missing the required
    ``source`` field must raise FixtureValidationError (pydantic
    ValidationError wrapped), NOT silently drop the ability.
    """
    abilities_yaml = (
        "  abilities:\n"
        "    - name: Halfbaked\n"
        "      genre_description: incomplete\n"
        "      mechanical_effect: missing source below\n"
    )
    _write_magic_fixture(tmp_path, "abil_nosrc", abilities_yaml=abilities_yaml)

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError) as exc_info:
        hydrate_fixture(name="abil_nosrc", fixtures_dir=tmp_path)
    assert "source" in str(exc_info.value).lower(), (
        f"error must name the missing field 'source'; got {exc_info.value!r}"
    )


def test_ability_invalid_source_value_raises(tmp_path: Path) -> None:
    """AC-1, AC-3: a ``source`` outside the AbilitySource enum
    (Race/Class/Item/Play) must raise FixtureValidationError.

    Without this, a typo like ``source: Clas`` would either crash later or
    (worse) be silently coerced — Sebastien's lie-detector wants the loud
    422 at the fixture boundary.
    """
    abilities_yaml = (
        "  abilities:\n"
        "    - name: Bogus\n"
        "      genre_description: x\n"
        "      mechanical_effect: y\n"
        "      source: Sorcery\n"  # not a valid AbilitySource
    )
    _write_magic_fixture(tmp_path, "abil_badsrc", abilities_yaml=abilities_yaml)

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError):
        hydrate_fixture(name="abil_badsrc", fixtures_dir=tmp_path)


def test_ability_extra_field_rejected_by_pydantic(tmp_path: Path) -> None:
    """AC-1, AC-3: ``AbilityDefinition`` is ``extra="forbid"``. An unknown
    key in an ability entry must raise FixtureValidationError, not be
    silently ignored.

    Mirrors ``test_known_facts_extra_field_rejected_by_pydantic`` — fixture
    authors get told about typos rather than having them swallowed.
    """
    abilities_yaml = (
        "  abilities:\n"
        "    - name: Typo'd\n"
        "      genre_description: x\n"
        "      mechanical_effect: y\n"
        "      source: Class\n"
        "      involutary: true\n"  # misspelled 'involuntary' — extra=forbid rejects
    )
    _write_magic_fixture(tmp_path, "abil_extra", abilities_yaml=abilities_yaml)

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError):
        hydrate_fixture(name="abil_extra", fixtures_dir=tmp_path)


def test_abilities_not_a_list_raises(tmp_path: Path) -> None:
    """AC-1, AC-3, lang-review #1: ``abilities:`` declared as a mapping
    (not a list) must raise FixtureValidationError — the same shape-guard
    discipline ``known_facts`` enforces (``_hydrate_character`` raises a
    FixtureValidationError for the non-list shape BEFORE pydantic).

    Guards against a future ``data.get("abilities", [])`` that would
    silently coerce a wrong shape into an empty list.
    """
    abilities_yaml = "  abilities:\n    not_a_list: true\n"
    _write_magic_fixture(tmp_path, "abil_notlist", abilities_yaml=abilities_yaml)

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError):
        hydrate_fixture(name="abil_notlist", fixtures_dir=tmp_path)


# ── AC-2: MagicState hydration (happy paths) ────────────────────────────────


def test_magic_state_minimal_config_hydrates(tmp_path: Path) -> None:
    """AC-2: a top-level ``magic_state:`` block with a valid minimal
    ``config:`` projects to ``GameSnapshot.magic_state`` as a real
    ``MagicState`` whose config round-trips.

    RED driver: today ``magic_state:`` is an unknown top-level key (see
    ``test_unknown_top_level_fields_are_ignored``) so it is silently
    dropped and ``snapshot.magic_state is None`` — fails until 50-22.
    """
    _write_magic_fixture(tmp_path, "magic_min", magic_state_yaml=_MAGIC_STATE_MINIMAL_YAML)

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="magic_min", fixtures_dir=tmp_path)
    ms = snapshot.magic_state
    assert ms is not None, "declared magic_state: must hydrate, not stay None"
    # Field-level identity check — not a bare truthy (lang-review #6).
    assert ms.config.world_slug == "coyote_star"
    assert ms.config.genre_slug == "space_opera"
    assert ms.config.narrator_register == "clinical"
    # Unspecified collections default empty.
    assert ms.ledger == {}
    assert ms.confrontations == []
    assert ms.control_tier == {}


def test_magic_state_optional_collections_hydrate(tmp_path: Path) -> None:
    """AC-2: optional ``ledger:`` / ``control_tier:`` / ``known_spells:``
    sub-blocks hydrate onto MagicState with the declared values.

    The ledger key is the ``scope|owner_id|bar_id`` serialized form
    (sidequest/magic/state.py::_serialize_bar_key); LedgerBar carries a
    full LedgerBarSpec + value.
    """
    magic_yaml = (
        "magic_state:\n"
        + _MINIMAL_WORLD_MAGIC_CONFIG_YAML
        + "  ledger:\n"
        + '    "character|practitioner|sanity":\n'
        + "      spec:\n"
        + "        id: sanity\n"
        + "        scope: character\n"
        + "        direction: down\n"
        + "        range: [0.0, 1.0]\n"
        + "        threshold_low: 0.4\n"
        + "        starts_at_chargen: 1.0\n"
        + "      value: 0.55\n"
        + "  control_tier:\n"
        + "    practitioner: 2\n"
        + "  known_spells:\n"
        + "    practitioner: [void_lance]\n"
    )
    _write_magic_fixture(tmp_path, "magic_full", magic_state_yaml=magic_yaml)

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="magic_full", fixtures_dir=tmp_path)
    ms = snapshot.magic_state
    assert ms is not None
    bar = ms.ledger["character|practitioner|sanity"]
    assert bar.value == pytest.approx(0.55), f"ledger bar value must hydrate; got {bar.value!r}"
    assert bar.spec.id == "sanity"
    assert bar.spec.direction == "down"
    assert ms.control_tier == {"practitioner": 2}
    assert ms.known_spells == {"practitioner": ["void_lance"]}


def test_missing_magic_state_block_leaves_field_none(tmp_path: Path) -> None:
    """AC-2 (backward compat / regression lock): omitting ``magic_state:``
    leaves ``snapshot.magic_state`` at the GameSnapshot pydantic default
    (``None``). Passes before and after 50-22.
    """
    _write_magic_fixture(tmp_path, "magic_absent", magic_state_yaml=None)

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="magic_absent", fixtures_dir=tmp_path)
    assert snapshot.magic_state is None, (
        f"omitting magic_state: must leave it None; got {snapshot.magic_state!r}"
    )


# ── AC-2 / AC-3: malformed magic_state fails LOUDLY (no silent default) ──────


def test_magic_state_missing_config_raises(tmp_path: Path) -> None:
    """AC-2, AC-3: ``magic_state:`` present but with NO ``config:`` must
    raise FixtureValidationError — ``MagicState.config`` is required and
    there is no synthetic-config fallback (ADR-014 Diamond, AC-3 explicit).
    """
    magic_yaml = "magic_state:\n  control_tier:\n    practitioner: 1\n"
    _write_magic_fixture(tmp_path, "magic_nocfg", magic_state_yaml=magic_yaml)

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError) as exc_info:
        hydrate_fixture(name="magic_nocfg", fixtures_dir=tmp_path)
    assert "config" in str(exc_info.value).lower(), (
        f"error must name the missing required 'config'; got {exc_info.value!r}"
    )


def test_magic_state_malformed_config_raises(tmp_path: Path) -> None:
    """AC-2, AC-3, lang-review #11: a ``config:`` missing a required
    WorldMagicConfig field (here ``narrator_register``) must raise
    FixtureValidationError (pydantic wrapped) — never a partial/empty
    MagicState.
    """
    broken_config = _MINIMAL_WORLD_MAGIC_CONFIG_YAML.replace(
        "    narrator_register: clinical\n", ""
    )
    magic_yaml = "magic_state:\n" + broken_config
    _write_magic_fixture(tmp_path, "magic_badcfg", magic_state_yaml=magic_yaml)

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError):
        hydrate_fixture(name="magic_badcfg", fixtures_dir=tmp_path)


def test_magic_state_config_extra_field_rejected(tmp_path: Path) -> None:
    """AC-2, AC-3: ``WorldMagicConfig`` is ``extra="forbid"``. The exact
    invalid shape from the session's Technical Approach example
    (``confrontations_by_name: {}``) must raise — proving the corrected
    test shape is enforced and the stale example cannot sneak back in.
    """
    magic_yaml = (
        "magic_state:\n"
        + _MINIMAL_WORLD_MAGIC_CONFIG_YAML
        + "  config_typo_marker: ignored\n"  # top-level magic_state extra (forbid)
    )
    # Also exercise the in-config extra field that the session example used.
    magic_yaml_inconfig = "magic_state:\n" + _MINIMAL_WORLD_MAGIC_CONFIG_YAML.replace(
        "    ledger_bars: []\n",
        "    ledger_bars: []\n    confrontations_by_name: {}\n",
    )
    _write_magic_fixture(tmp_path, "magic_extra_top", magic_state_yaml=magic_yaml)
    _write_magic_fixture(tmp_path, "magic_extra_cfg", magic_state_yaml=magic_yaml_inconfig)

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError):
        hydrate_fixture(name="magic_extra_top", fixtures_dir=tmp_path)
    with pytest.raises(FixtureValidationError):
        hydrate_fixture(name="magic_extra_cfg", fixtures_dir=tmp_path)


def test_magic_state_block_not_a_mapping_raises(tmp_path: Path) -> None:
    """AC-2, AC-3, lang-review #1: ``magic_state:`` declared as a list (or
    scalar) instead of a mapping must raise FixtureValidationError.

    Direct analog of ``test_malformed_scenario_state_block_raises`` — a
    future refactor must NOT replace the shape check with
    ``data.get("magic_state", {})`` and silently coerce the wrong shape.
    """
    body = (
        "genre: space_opera\n"
        "world: coyote_star\n"
        "character:\n"
        "  name: Practitioner\n"
        "  description: a focused adept\n"
        "  personality: focused\n"
        "  backstory: studied the arts\n"
        "  char_class: Mage\n"
        "  race: Human\n"
        "magic_state:\n"
        "  - just_a_list_item\n"
    )
    (tmp_path / "magic_notmap.yaml").write_text(body, encoding="utf-8")

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError):
        hydrate_fixture(name="magic_notmap", fixtures_dir=tmp_path)


def test_present_but_empty_magic_state_does_not_silently_default(tmp_path: Path) -> None:
    """AC-3 (the headline guarantee): ``magic_state: {}`` (present but
    empty — no ``config:``) must RAISE, never silently produce an empty
    ``MagicState`` or leave the field None.

    This is the ADR-014 "magic state is Diamond" / "No Silent Fallbacks"
    lie-detector test. An implementation that does
    ``MagicState(**(data.get("magic_state") or {}))`` and swallows the
    resulting ValidationError, or that treats empty-dict as "absent",
    would pass every other test but fail this one.
    """
    body = (
        "genre: space_opera\n"
        "world: coyote_star\n"
        "character:\n"
        "  name: Practitioner\n"
        "  description: a focused adept\n"
        "  personality: focused\n"
        "  backstory: studied the arts\n"
        "  char_class: Mage\n"
        "  race: Human\n"
        "magic_state: {}\n"
    )
    (tmp_path / "magic_emptymap.yaml").write_text(body, encoding="utf-8")

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError):
        hydrate_fixture(name="magic_emptymap", fixtures_dir=tmp_path)


# ── AC-4: integration / backward-compat (wiring + regression lock) ──────────


def test_canonical_fixtures_still_hydrate_with_magic_state_implementation() -> None:
    """AC-4 (backwards-compat WIRING test, CLAUDE.md "Every Test Suite
    Needs a Wiring Test"): the four canonical pre-50-22 fixtures must keep
    hydrating cleanly, with ``magic_state is None`` and every character's
    ``abilities == []`` — proving the 50-22 branch does not require either
    new block.
    """
    from sidequest.game.scene_harness import hydrate_fixture

    real_fixtures = (
        "combat_brawl_wasteland",
        "combat_dogfight_space",
        "social_negotiation_tea",
        "social_poker_wasteland",
    )
    for fixture_name in real_fixtures:
        snapshot = hydrate_fixture(name=fixture_name, fixtures_dir=CANONICAL_FIXTURES_DIR)
        assert snapshot.magic_state is None, (
            f"{fixture_name}: pre-50-22 fixture must keep magic_state=None; "
            f"got {snapshot.magic_state!r}"
        )
        for idx, ch in enumerate(snapshot.characters):
            assert ch.abilities == [], (
                f"{fixture_name}: characters[{idx}] declared no abilities — "
                f"must stay []; got {ch.abilities!r}"
            )


def test_abilities_magic_state_and_scenario_state_coexist(tmp_path: Path) -> None:
    """AC-4: all three optional blocks (character.abilities, top-level
    magic_state, top-level scenario_state) in ONE fixture hydrate without
    interfering with each other — the integration path Wave 2 fixtures take.
    """
    body = (
        "genre: space_opera\n"
        "world: coyote_star\n"
        "character:\n"
        "  name: Practitioner\n"
        "  description: a focused adept\n"
        "  personality: focused\n"
        "  backstory: studied the arts\n"
        "  char_class: Mage\n"
        "  race: Human\n" + _ABILITIES_TWO_YAML + _MAGIC_STATE_MINIMAL_YAML + "scenario_state:\n"
        "  clue_graph:\n"
        "    nodes:\n"
        "      - id: clue_a\n"
        "        type: physical_evidence\n"
        "        description: a residue\n"
        "        discovery_method: observation\n"
        "        visibility: public\n"
        "        requires: []\n"
    )
    (tmp_path / "all_three.yaml").write_text(body, encoding="utf-8")

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="all_three", fixtures_dir=tmp_path)
    assert len(snapshot.characters[0].abilities) == 2, "abilities must survive coexistence"
    assert snapshot.magic_state is not None, "magic_state must survive coexistence"
    assert snapshot.scenario_state is not None, "scenario_state must survive coexistence"
    assert [n.id for n in snapshot.scenario_state.clue_graph.nodes] == ["clue_a"], (
        "scenario_state must not be clobbered by the magic_state branch"
    )


# ── Story 50-21 — hydrate StructuredEncounter (ADR-092 follow-on) ───────────
#
# `hydrate_fixture` must read a top-level `encounter:` block and project it
# to `GameSnapshot.encounter` as a fully-initialized `StructuredEncounter`
# (ADR-033 confrontation engine). The canonical combat_brawl_wasteland
# fixture already carries:
#
#     encounter:
#       type: combat
#
# which is currently SILENTLY DROPPED — snapshot.encounter stays None.
# Wiring it through unblocks Wave 2 pre-armed combat fixtures for ADR-093
# difficulty calibration.
#
# Spec interpretation (logged as a TEA test-design deviation):
#   * Fixture YAML key is `type:` (short form). This is the AC-4 ground
#     truth: the existing canonical fixture uses `type: combat` (lines
#     66-68) and AC-8 forbids editing canonical fixtures, so the hydrator
#     MUST map `type` → StructuredEncounter.encounter_type.
#   * Per-metric override uses the model field names `player_metric:` /
#     `opponent_metric:` with a nested `threshold:`, mirroring how 50-20's
#     scenario_state hydrator keyed on model field names. Architect to
#     confirm the override key shape during SPEC-CHECK.
#   * AC-3 does not specify the EncounterMetric `name`. EncounterMetric
#     requires a non-empty `name: str`; tests assert it is a non-empty
#     string rather than a specific value, leaving the label to Dev.


def _write_encounter_fixture(
    tmp_path: Path,
    name: str,
    *,
    encounter_yaml: str | None = None,
) -> None:
    """Helper: write a minimal combat fixture with an optional encounter block.

    Mirrors ``_write_scenario_state_fixture`` — keeps test bodies focused on
    the encounter assertion rather than YAML scaffolding. The genre/world/
    character scaffold is the minimum the hydrator's required-field guards
    accept.
    """
    body = (
        "genre: mutant_wasteland\n"
        "world: flickering_reach\n"
        "character:\n"
        "  name: Skar\n"
        "  description: A scarred vault dweller with mismatched eyes\n"
        "  personality: cautious but curious\n"
        "  backstory: emerged from a sealed vault\n"
        "  char_class: Beastkin\n"
        "  race: Uplifted Animal\n"
    )
    if encounter_yaml is not None:
        body += encounter_yaml
    (tmp_path / f"{name}.yaml").write_text(body, encoding="utf-8")


def test_encounter_block_missing_leaves_snapshot_none(tmp_path: Path) -> None:
    """AC-1, AC-8: a fixture WITHOUT an encounter block hydrates normally and
    ``snapshot.encounter`` stays at the GameSnapshot pydantic default (None).

    Backward-compat guard: the three canonical fixtures that predate 50-21
    (combat_dogfight_space, social_negotiation_tea, social_poker_wasteland)
    carry no encounter block and must keep working unchanged.
    """
    _write_encounter_fixture(tmp_path, "no_encounter_block", encounter_yaml=None)

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="no_encounter_block", fixtures_dir=tmp_path)

    assert snapshot.encounter is None, (
        f"omitting encounter: must leave snapshot.encounter at the GameSnapshot "
        f"pydantic default (None); got {snapshot.encounter!r}"
    )


def test_combat_brawl_wasteland_fixture_encounter_hydrated() -> None:
    """AC-4, AC-5 (wiring test): the REAL canonical combat_brawl_wasteland
    fixture hydrates its ``encounter: type: combat`` block end-to-end.

    CLAUDE.md "Every Test Suite Needs a Wiring Test" — this exercises the
    production fixture path (CANONICAL_FIXTURES_DIR), not a tmp_path
    synthetic, proving the encounter branch is reachable from the same
    fixtures the dev-gated POST /dev/scene route loads.
    """
    from sidequest.game.encounter import StructuredEncounter
    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="combat_brawl_wasteland", fixtures_dir=CANONICAL_FIXTURES_DIR)

    assert snapshot.encounter is not None, (
        "combat_brawl_wasteland.yaml declares 'encounter: type: combat' — "
        "snapshot.encounter must not be None"
    )
    assert isinstance(snapshot.encounter, StructuredEncounter), (
        f"snapshot.encounter must be a StructuredEncounter, got {type(snapshot.encounter).__name__}"
    )
    assert snapshot.encounter.encounter_type == "combat", (
        f"fixture 'type: combat' must map to encounter_type=='combat'; got "
        f"{snapshot.encounter.encounter_type!r}"
    )


def test_encounter_default_metrics_initialized(tmp_path: Path) -> None:
    """AC-3: an encounter block with only ``type:`` initializes both metrics
    with defaults — current=0, starting=0, threshold=10, non-empty name.

    EncounterMetric.threshold has NO pydantic default; the hydrator must
    supply one or StructuredEncounter construction fails. The metric name
    is left to Dev (AC-3 is silent) — assert only that it is non-empty so
    the EncounterMetric non-blank constraint is satisfied.
    """
    _write_encounter_fixture(
        tmp_path, "default_metrics", encounter_yaml="encounter:\n  type: combat\n"
    )

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="default_metrics", fixtures_dir=tmp_path)

    enc = snapshot.encounter
    assert enc is not None, "encounter block was provided — must hydrate"
    for label, metric in (
        ("player_metric", enc.player_metric),
        ("opponent_metric", enc.opponent_metric),
    ):
        assert metric.current == 0, f"{label}.current default must be 0; got {metric.current}"
        assert metric.starting == 0, f"{label}.starting default must be 0; got {metric.starting}"
        assert metric.threshold == 10, (
            f"{label}.threshold default must be 10 (AC-3); got {metric.threshold}"
        )
        assert isinstance(metric.name, str) and metric.name.strip(), (
            f"{label}.name must be a non-empty string; got {metric.name!r}"
        )


def test_encounter_missing_type_raises_FixtureValidationError(
    tmp_path: Path,
) -> None:
    """AC-2: an encounter block present but WITHOUT ``type:`` raises
    FixtureValidationError — no silent default to "combat".

    Enforces CLAUDE.md "No Silent Fallbacks" and lang-review #11 (input
    validation at the fixture-parser boundary). The error message must
    name the offending field so a fixture author can fix it.
    """
    _write_encounter_fixture(
        tmp_path,
        "encounter_no_type",
        encounter_yaml="encounter:\n  player_metric:\n    threshold: 5\n",
    )

    from sidequest.game.scene_harness import (
        FixtureValidationError,
        hydrate_fixture,
    )

    with pytest.raises(FixtureValidationError) as exc_info:
        hydrate_fixture(name="encounter_no_type", fixtures_dir=tmp_path)

    msg = str(exc_info.value).lower()
    assert "type" in msg, f"missing-type error must name the encounter type field; got {msg!r}"


def test_encounter_empty_type_raises_FixtureValidationError(tmp_path: Path) -> None:
    """AC-2 (edge): ``type:`` present but empty/blank raises
    FixtureValidationError — same discipline as the empty-genre guard
    (test_empty_genre_string_raises_FixtureValidationError). An empty
    string must not silently become a valid encounter_type.
    """
    _write_encounter_fixture(
        tmp_path,
        "encounter_empty_type",
        encounter_yaml='encounter:\n  type: ""\n',
    )

    from sidequest.game.scene_harness import (
        FixtureValidationError,
        hydrate_fixture,
    )

    with pytest.raises(FixtureValidationError):
        hydrate_fixture(name="encounter_empty_type", fixtures_dir=tmp_path)


def test_encounter_custom_metric_threshold(tmp_path: Path) -> None:
    """AC-3: a per-metric override sets that metric's threshold while the
    un-overridden metric keeps the default (10).

    Guards against an implementation that ignores overrides, or one that
    applies a single override to both metrics.
    """
    _write_encounter_fixture(
        tmp_path,
        "custom_threshold",
        encounter_yaml=("encounter:\n  type: combat\n  player_metric:\n    threshold: 25\n"),
    )

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="custom_threshold", fixtures_dir=tmp_path)

    enc = snapshot.encounter
    assert enc is not None, "encounter block was provided — must hydrate"
    assert enc.player_metric.threshold == 25, (
        f"player_metric override must be honored; got {enc.player_metric.threshold} (expected 25)"
    )
    assert enc.opponent_metric.threshold == 10, (
        f"un-overridden opponent_metric must keep the default 10; got "
        f"{enc.opponent_metric.threshold}"
    )


def test_encounter_metric_override_any_yaml_key_order(tmp_path: Path) -> None:
    """AC-7: metric overrides project correctly regardless of YAML key
    order. ``opponent_metric`` declared before ``type`` must still produce
    a valid encounter with both overrides applied.
    """
    _write_encounter_fixture(
        tmp_path,
        "order_stable",
        encounter_yaml=(
            "encounter:\n"
            "  opponent_metric:\n"
            "    threshold: 7\n"
            "  type: combat\n"
            "  player_metric:\n"
            "    threshold: 3\n"
        ),
    )

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="order_stable", fixtures_dir=tmp_path)

    enc = snapshot.encounter
    assert enc is not None, "encounter block was provided — must hydrate"
    assert enc.encounter_type == "combat", (
        f"type declared after opponent_metric must still bind; got {enc.encounter_type!r}"
    )
    assert enc.player_metric.threshold == 3, (
        f"player_metric override lost under reordering; got {enc.player_metric.threshold}"
    )
    assert enc.opponent_metric.threshold == 7, (
        f"opponent_metric override lost under reordering; got {enc.opponent_metric.threshold}"
    )


def test_encounter_block_not_a_mapping_raises(tmp_path: Path) -> None:
    """AC-2 (paranoid): ``encounter:`` as a scalar string instead of a
    mapping raises FixtureValidationError — mirrors
    test_malformed_scenario_state_block_raises. Must NOT silently coerce
    or skip.
    """
    _write_encounter_fixture(
        tmp_path,
        "encounter_scalar",
        encounter_yaml="encounter: combat\n",
    )

    from sidequest.game.scene_harness import (
        FixtureValidationError,
        hydrate_fixture,
    )

    with pytest.raises(FixtureValidationError):
        hydrate_fixture(name="encounter_scalar", fixtures_dir=tmp_path)


def test_encounter_legacy_metric_key_raises_FixtureValidationError(
    tmp_path: Path,
) -> None:
    """Paranoid (coverage beyond enumerated ACs — logged as TEA deviation):
    StructuredEncounter has an explicit ``_reject_legacy_metric``
    model_validator that raises on the single-dial ``metric`` key. A
    fixture carrying ``metric:`` inside the encounter block must surface as
    FixtureValidationError at the module boundary — never a raw
    pydantic ValidationError / ValueError leak.

    This enforces the SAME boundary contract every other block obeys
    (character, npcs, scenario_state all re-wrap pydantic errors). Rationale
    for adding it: the model already guards legacy metric; the hydrator
    must not let that guard leak an un-wrapped exception type that the
    HTTP 404/422 mapping can't classify.
    """
    _write_encounter_fixture(
        tmp_path,
        "encounter_legacy_metric",
        encounter_yaml=(
            "encounter:\n  type: combat\n  metric:\n    name: legacy\n    threshold: 10\n"
        ),
    )

    from sidequest.game.scene_harness import (
        FixtureValidationError,
        hydrate_fixture,
    )

    with pytest.raises(FixtureValidationError):
        hydrate_fixture(name="encounter_legacy_metric", fixtures_dir=tmp_path)


def test_canonical_fixtures_still_hydrate_with_encounter_implementation() -> None:
    """AC-8 (backwards-compat wiring test): all four canonical fixtures
    hydrate cleanly after the encounter branch is added.

    Only combat_brawl_wasteland carries an encounter block — it must now
    produce a non-None StructuredEncounter (encounter_type=='combat'). The
    other three carry NO encounter block and must keep
    snapshot.encounter==None. Guards against an implementation that
    requires the new block or mis-detects it.
    """
    from sidequest.game.encounter import StructuredEncounter
    from sidequest.game.scene_harness import hydrate_fixture

    expectations = {
        "combat_brawl_wasteland": "combat",
        "combat_dogfight_space": None,
        "social_negotiation_tea": None,
        "social_poker_wasteland": None,
    }
    for fixture_name, expected_type in expectations.items():
        snapshot = hydrate_fixture(name=fixture_name, fixtures_dir=CANONICAL_FIXTURES_DIR)
        if expected_type is None:
            assert snapshot.encounter is None, (
                f"{fixture_name}: no encounter block — snapshot.encounter must "
                f"stay None; got {snapshot.encounter!r}"
            )
        else:
            assert isinstance(snapshot.encounter, StructuredEncounter), (
                f"{fixture_name}: declares an encounter block — must hydrate a "
                f"StructuredEncounter; got {type(snapshot.encounter).__name__}"
            )
            assert snapshot.encounter.encounter_type == expected_type, (
                f"{fixture_name}: encounter_type mismatch; got "
                f"{snapshot.encounter.encounter_type!r}, expected {expected_type!r}"
            )


# ════════════════════════════════════════════════════════════════════════════
# Story 90-4 — seed a DETERMINISTIC WWN fixture (epic 90, ADR-092 follow-on)
# ════════════════════════════════════════════════════════════════════════════
#
# Epic-90 finding (3): "The scene-harness hydrator can't seed per-character WWN
# spellcasting (core.spellcasting) and its encounter block builds a dial
# StructuredEncounter, not WWN hp_depletion — so a content-only fixture can't
# prove wwn.spell.cast or deterministic WWN combat."
#
# This story closes BOTH gaps so a fixture playtest can deterministically fire
# the WWN crunch (the counterpart to 90-3's live free-play proof). Two hydrator
# functions change:
#
#   * ``_hydrate_character`` — read a ``spellcasting:`` block and project it to
#     ``Character.core.spellcasting`` (a wwn_magic.SpellcastingState: prepared
#     spell ids / casts_remaining / casts_per_day / max_spell_level).
#   * ``_hydrate_encounter`` — read ``win_condition:`` / ``category:`` /
#     ``actors:`` so a fixture can stand up a WWN ``hp_depletion`` combat that
#     seats a player-side caster and an opponent-side defender (the dial-only
#     StructuredEncounter the hydrator builds today cannot drive an
#     hp_depletion cast/strike).
#
# Acceptance criteria (defined by TEA in RED — no ACs were recorded in the
# sprint YAML; see the TEA test-design deviation log in the session file):
#
#   AC-1  ``character.spellcasting:`` → ``Character.core.spellcasting`` as a
#         SpellcastingState with every field (prepared/casts_remaining/
#         casts_per_day/max_spell_level) preserved.
#   AC-2  Omitting ``spellcasting:`` leaves ``core.spellcasting`` at the
#         CreatureCore default (None) — backward-compat regression lock. The
#         four canonical fixtures must keep ``core.spellcasting is None``.
#   AC-3  ``spellcasting:`` is save-bearing — a malformed shape (non-mapping)
#         or an extra/typo'd key (SpellcastingState ``extra="forbid"``) raises
#         FixtureValidationError at the module boundary (No Silent Fallbacks;
#         lang-review #1/#11). Never a silent skip, never a leaked
#         pydantic.ValidationError.
#   AC-4  Spellcasting hydrates under the multi-PC ``characters:`` LIST path,
#         not only the singular ``character:`` block (both funnel through
#         ``_hydrate_character``).
#   AC-5  ``encounter.win_condition: hp_depletion`` → encounter.win_condition
#         == "hp_depletion". Omitting it keeps the StructuredEncounter default
#         ("dial_threshold") so every pre-90-4 fixture is unchanged.
#   AC-6  An invalid ``win_condition`` value raises FixtureValidationError (the
#         StructuredEncounter Literal rejects the typo; the hydrator re-wraps it
#         rather than leaking a raw ValidationError).
#   AC-7  ``encounter.category: combat`` → encounter.category == "combat".
#   AC-8  ``encounter.actors:`` seats EncounterActors (name/role/side); a
#         malformed actors block (non-list, or an invalid ``side``) raises
#         FixtureValidationError.
#
# The end-to-end DETERMINISTIC proof (a hydrated fixture actually firing
# wwn.spell.cast + ablative HP on the real elemental_harmony pack) is the
# wiring test mandated by CLAUDE.md "Every Test Suite Needs a Wiring Test" —
# it lives in tests/integration/test_wwn_scene_harness_fixture_proof.py because
# it needs a real content pack on disk (skip-guarded, mirroring the sibling
# tests/integration/test_wwn_*_dispatch.py proofs).


# A full WWN spellcasting block, indented for a ``character:`` mapping (key at
# 2 spaces, nested keys at 4, list dashes at 6) — matches the elemental_harmony
# Channeler's authored economy (2 casts/day, cinder_lance + river_step prepared).
_SPELLCASTING_YAML = (
    "  spellcasting:\n"
    "    prepared:\n"
    "      - cinder_lance\n"
    "      - river_step\n"
    "    casts_remaining: 2\n"
    "    casts_per_day: 2\n"
    "    max_spell_level: 1\n"
)


# ── AC-1 / AC-3 / AC-4: _hydrate_character spellcasting ─────────────────────


def test_character_spellcasting_block_hydrates_all_fields(tmp_path: Path) -> None:
    """AC-1: a ``spellcasting:`` block under ``character:`` projects to
    ``Character.core.spellcasting`` with every SpellcastingState field
    preserved.

    RED driver: today ``_hydrate_character`` never reads ``spellcasting`` and
    never passes it to the ``CreatureCore`` constructor, so the field stays at
    its CreatureCore default (None) — this assertion fails until 90-4 lands.
    """
    _write_magic_fixture(tmp_path, "sc_full", extra_character_yaml=_SPELLCASTING_YAML)

    from sidequest.game.scene_harness import hydrate_fixture
    from sidequest.game.wwn_magic import SpellcastingState

    snapshot = hydrate_fixture(name="sc_full", fixtures_dir=tmp_path)
    sc = snapshot.characters[0].core.spellcasting

    assert sc is not None, (
        "declared character.spellcasting must hydrate onto core.spellcasting, not stay None"
    )
    assert isinstance(sc, SpellcastingState), (
        f"core.spellcasting must be a SpellcastingState; got {type(sc).__name__}"
    )
    assert sc.prepared == ["cinder_lance", "river_step"], (
        f"prepared spell ids must round-trip in order; got {sc.prepared!r}"
    )
    assert sc.casts_remaining == 2, f"casts_remaining must hydrate; got {sc.casts_remaining}"
    assert sc.casts_per_day == 2, f"casts_per_day must hydrate; got {sc.casts_per_day}"
    assert sc.max_spell_level == 1, f"max_spell_level must hydrate; got {sc.max_spell_level}"


def test_missing_spellcasting_block_leaves_core_spellcasting_none(tmp_path: Path) -> None:
    """AC-2 (backward compat / regression lock): a character with NO
    ``spellcasting:`` key hydrates ``core.spellcasting == None`` (the
    CreatureCore pydantic default).

    Guards against an implementation that *requires* the block or fabricates
    an empty SpellcastingState for non-casters.
    """
    _write_magic_fixture(tmp_path, "sc_absent", extra_character_yaml="")

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="sc_absent", fixtures_dir=tmp_path)
    assert snapshot.characters[0].core.spellcasting is None, (
        "omitting spellcasting: must leave core.spellcasting at the CreatureCore "
        f"default (None); got {snapshot.characters[0].core.spellcasting!r}"
    )


def test_spellcasting_hydrates_under_multi_pc_characters_list(tmp_path: Path) -> None:
    """AC-4: ``spellcasting:`` works under a ``characters:`` LIST entry, not
    only the legacy singular ``character:`` block.

    Both shapes funnel through ``_hydrate_character``; this proves the new
    spellcasting branch is reachable from the multi-PC path a real MP fixture
    takes.
    """
    body = (
        "genre: elemental_harmony\n"
        "world: test_world\n"
        "characters:\n"
        "  - name: Mei Lin\n"
        "    description: A Channeler of the Ember Isles\n"
        "    personality: serene\n"
        "    backstory: trained in the ember registers\n"
        "    char_class: Channeler\n"
        "    race: Human\n"
        "    spellcasting:\n"
        "      prepared:\n"
        "        - cinder_lance\n"
        "      casts_remaining: 3\n"
        "      casts_per_day: 3\n"
        "      max_spell_level: 1\n"
        "  - name: Bo\n"
        "    description: A non-caster bruiser\n"
        "    personality: blunt\n"
        "    backstory: raised on the docks\n"
        "    char_class: Warrior\n"
        "    race: Human\n"
    )
    (tmp_path / "sc_multi.yaml").write_text(body, encoding="utf-8")

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="sc_multi", fixtures_dir=tmp_path)
    caster, bruiser = snapshot.characters
    assert caster.core.spellcasting is not None, "caster in characters: list must seed spellcasting"
    assert caster.core.spellcasting.prepared == ["cinder_lance"], (
        f"multi-PC caster prepared lost; got {caster.core.spellcasting.prepared!r}"
    )
    assert caster.core.spellcasting.casts_remaining == 3
    assert bruiser.core.spellcasting is None, (
        "a sibling PC without a spellcasting: block must keep core.spellcasting None "
        f"(no cross-contamination); got {bruiser.core.spellcasting!r}"
    )


def test_spellcasting_block_not_a_mapping_raises(tmp_path: Path) -> None:
    """AC-3: ``spellcasting:`` declared as a scalar (or list) instead of a
    mapping raises FixtureValidationError — mirrors the magic_state /
    scenario_state non-mapping guards. Must NOT silently skip the block.

    RED driver: today ``_hydrate_character`` ignores the key entirely, so
    hydrate_fixture returns normally and pytest.raises fails — exactly the
    No-Silent-Fallbacks gap this story closes (lang-review #1/#11).
    """
    _write_magic_fixture(
        tmp_path,
        "sc_scalar",
        extra_character_yaml="  spellcasting: cinder_lance\n",
    )

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError) as exc_info:
        hydrate_fixture(name="sc_scalar", fixtures_dir=tmp_path)

    assert "spellcasting" in str(exc_info.value).lower(), (
        f"the error must name the offending block; got {str(exc_info.value)!r}"
    )


def test_spellcasting_extra_field_rejected(tmp_path: Path) -> None:
    """AC-3: an extra/typo'd key inside ``spellcasting:`` raises
    FixtureValidationError. SpellcastingState is ``extra="forbid"``; the
    hydrator must re-wrap the pydantic ValidationError at the module boundary
    rather than leak it (the same boundary contract abilities/known_facts obey).
    """
    _write_magic_fixture(
        tmp_path,
        "sc_extra",
        extra_character_yaml=(
            "  spellcasting:\n"
            "    prepared: []\n"
            "    casts_remaining: 1\n"
            "    casts_per_day: 1\n"
            "    max_spell_level: 1\n"
            "    casts_per_week: 9\n"  # not a SpellcastingState field → extra=forbid
        ),
    )

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError):
        hydrate_fixture(name="sc_extra", fixtures_dir=tmp_path)


def test_canonical_fixtures_still_hydrate_with_spellcasting_none() -> None:
    """AC-2 (backward-compat wiring test): all four canonical fixtures hydrate
    cleanly after the spellcasting branch is added, with every character's
    ``core.spellcasting is None`` (none of them declares a spellcasting block).

    Guards against an implementation that fabricates a SpellcastingState for
    every fixture character.
    """
    from sidequest.game.scene_harness import hydrate_fixture

    for fixture_name in (
        "combat_brawl_wasteland",
        "combat_dogfight_space",
        "social_negotiation_tea",
        "social_poker_wasteland",
    ):
        snapshot = hydrate_fixture(name=fixture_name, fixtures_dir=CANONICAL_FIXTURES_DIR)
        for ch in snapshot.characters:
            assert ch.core.spellcasting is None, (
                f"{fixture_name}: {ch.core.name} declares no spellcasting block — "
                f"core.spellcasting must stay None; got {ch.core.spellcasting!r}"
            )


# ── AC-5..AC-8: _hydrate_encounter win_condition / category / actors ────────


def test_encounter_win_condition_hp_depletion_hydrates(tmp_path: Path) -> None:
    """AC-5: ``encounter.win_condition: hp_depletion`` projects to
    ``StructuredEncounter.win_condition == "hp_depletion"``.

    RED driver: today ``_hydrate_encounter`` never reads ``win_condition`` and
    the StructuredEncounter defaults to "dial_threshold" — so a fixture cannot
    stand up a WWN hp_depletion combat. This is the headline encounter gap.
    """
    _write_encounter_fixture(
        tmp_path,
        "enc_hp",
        encounter_yaml="encounter:\n  type: combat\n  win_condition: hp_depletion\n",
    )

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="enc_hp", fixtures_dir=tmp_path)
    enc = snapshot.encounter
    assert enc is not None, "encounter block was provided — must hydrate"
    assert enc.win_condition == "hp_depletion", (
        f"declared win_condition must hydrate; got {enc.win_condition!r}"
    )


def test_encounter_win_condition_defaults_dial_threshold(tmp_path: Path) -> None:
    """AC-5 (backward compat): an encounter block WITHOUT ``win_condition:``
    keeps the StructuredEncounter default ("dial_threshold"), so every
    pre-90-4 fixture (e.g. combat_brawl_wasteland) is unchanged.
    """
    _write_encounter_fixture(
        tmp_path, "enc_default_wc", encounter_yaml="encounter:\n  type: combat\n"
    )

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="enc_default_wc", fixtures_dir=tmp_path)
    enc = snapshot.encounter
    assert enc is not None
    assert enc.win_condition == "dial_threshold", (
        f"omitting win_condition must keep the dial_threshold default; got {enc.win_condition!r}"
    )


def test_encounter_invalid_win_condition_raises(tmp_path: Path) -> None:
    """AC-6: an invalid ``win_condition`` value raises FixtureValidationError.

    StructuredEncounter.win_condition is a closed Literal
    (dial_threshold | hp_depletion | table_showdown); a typo must surface as
    a wrapped FixtureValidationError at the module boundary, not a leaked
    pydantic.ValidationError and not a silent default.
    """
    _write_encounter_fixture(
        tmp_path,
        "enc_bad_wc",
        encounter_yaml="encounter:\n  type: combat\n  win_condition: hp_deletion\n",  # typo
    )

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError):
        hydrate_fixture(name="enc_bad_wc", fixtures_dir=tmp_path)


def test_encounter_category_hydrates(tmp_path: Path) -> None:
    """AC-7: ``encounter.category: combat`` projects to
    ``StructuredEncounter.category == "combat"``.

    category is what ``_encounter_is_mobile`` and the resolution channels read;
    a fixture-built hp_depletion combat must carry it so the engine classifies
    the confrontation without a pack round-trip.
    """
    _write_encounter_fixture(
        tmp_path,
        "enc_cat",
        encounter_yaml="encounter:\n  type: combat\n  category: combat\n",
    )

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="enc_cat", fixtures_dir=tmp_path)
    enc = snapshot.encounter
    assert enc is not None
    assert enc.category == "combat", f"declared category must hydrate; got {enc.category!r}"


def test_encounter_actors_seated(tmp_path: Path) -> None:
    """AC-8: an ``encounter.actors:`` block seats EncounterActors with
    name/role/side preserved.

    The cast/strike spine resolves its defender via
    ``_opposite_side_first_actor(encounter, actor.side)`` — without seated
    actors a fixture-built combat has no player/opponent seating and the WWN
    cast has no defender. RED driver: today ``_hydrate_encounter`` builds an
    actors-less StructuredEncounter (actors == []).
    """
    _write_encounter_fixture(
        tmp_path,
        "enc_actors",
        encounter_yaml=(
            "encounter:\n"
            "  type: combat\n"
            "  win_condition: hp_depletion\n"
            "  category: combat\n"
            "  actors:\n"
            "    - name: Skar\n"
            "      role: caster\n"
            "      side: player\n"
            "    - name: Jade Duelist\n"
            "      role: defender\n"
            "      side: opponent\n"
        ),
    )

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="enc_actors", fixtures_dir=tmp_path)
    enc = snapshot.encounter
    assert enc is not None
    assert len(enc.actors) == 2, (
        f"both declared actors must seat; got {len(enc.actors)}: {[a.name for a in enc.actors]!r}"
    )
    player = enc.find_actor_for_player("Skar")
    assert player is not None, "the player-side caster must be seated and findable by name"
    assert player.side == "player", f"caster side must be 'player'; got {player.side!r}"
    assert player.role == "caster", f"caster role must round-trip; got {player.role!r}"
    opp = enc.find_actor("Jade Duelist")
    assert opp is not None, "the opponent must be seated"
    assert opp.side == "opponent", f"opponent side must be 'opponent'; got {opp.side!r}"


def test_encounter_actors_not_a_list_raises(tmp_path: Path) -> None:
    """AC-8: ``actors:`` declared as a mapping/scalar instead of a list raises
    FixtureValidationError — same non-list discipline the abilities/known_facts
    blocks enforce. Must NOT silently skip seating.
    """
    _write_encounter_fixture(
        tmp_path,
        "enc_actors_scalar",
        encounter_yaml="encounter:\n  type: combat\n  actors: Skar\n",
    )

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError):
        hydrate_fixture(name="enc_actors_scalar", fixtures_dir=tmp_path)


def test_encounter_actor_invalid_side_raises(tmp_path: Path) -> None:
    """AC-8: an actor with an invalid ``side`` raises FixtureValidationError.

    EncounterActor.side is a closed Literal (player|opponent|neutral); a bogus
    side must surface as a wrapped FixtureValidationError, not a leaked
    pydantic.ValidationError.
    """
    _write_encounter_fixture(
        tmp_path,
        "enc_bad_side",
        encounter_yaml=(
            "encounter:\n"
            "  type: combat\n"
            "  actors:\n"
            "    - name: Skar\n"
            "      role: caster\n"
            "      side: protagonist\n"  # not a valid ActorSide
        ),
    )

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError):
        hydrate_fixture(name="enc_bad_side", fixtures_dir=tmp_path)


def test_combat_brawl_wasteland_encounter_stays_dial_threshold_and_actorless() -> None:
    """AC-5 / AC-8 (backward-compat wiring test): the real canonical
    combat_brawl_wasteland fixture (``encounter: type: combat`` with no
    win_condition / actors block) keeps win_condition=="dial_threshold" and
    actors==[] after the 90-4 branch is added.

    Guards against an implementation that fabricates hp_depletion / seats
    phantom actors for every encounter.
    """
    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="combat_brawl_wasteland", fixtures_dir=CANONICAL_FIXTURES_DIR)
    enc = snapshot.encounter
    assert enc is not None, "combat_brawl_wasteland declares an encounter block"
    assert enc.win_condition == "dial_threshold", (
        f"a fixture with no win_condition must stay dial_threshold; got {enc.win_condition!r}"
    )
    assert enc.actors == [], f"a fixture with no actors block must seat none; got {enc.actors!r}"


# ════════════════════════════════════════════════════════════════════════════
# Story 90-7 — WWN Effort hydration + the wwn.magic_hydrated lie-detector span
# ════════════════════════════════════════════════════════════════════════════
#
# #787 (story 90-4) shipped the spellcasting + hp_depletion halves of the WWN
# scene-harness hydrator but DROPPED the two things the story title promised:
#
#   AC-1  ``_hydrate_character`` must read an ``effort:`` block and project it to
#         ``core.effort`` — a dict keyed by class-source, each value an EffortPool
#         whose ``source`` is DERIVED FROM THE KEY (WWN SRD §1.4.4: Effort from
#         one class-source cannot fuel another, so a caster carries a dict keyed
#         by source). The cast spine reads ``core.effort.get(source)`` at commit:
#
#             effort:
#               high_mage: {max: 3}   # -> core.effort["high_mage"] = EffortPool(source="high_mage", max=3)
#
#   AC-2  Backward-compat: a character with no ``effort:`` key keeps
#         ``core.effort`` at its CreatureCore default (empty dict), not None and
#         not a fabricated pool.
#
#   AC-3  ``source`` is authoritative from the KEY — a stray in-value ``source:``
#         must NOT override it (footgun guard: the key wins; #788 strips it).
#
#   AC-4  The ``effort:`` block is save-bearing — a malformed shape (non-mapping
#         block, non-mapping pool value, bad field type, or an extra=forbid typo)
#         fails loudly as FixtureValidationError -> HTTP 422, NEVER a silent skip
#         (CLAUDE.md "No Silent Fallbacks"; ADR-092 "Failure is loud";
#         lang-review #1/#11). The hydrator re-wraps the pydantic ValidationError
#         at the module boundary rather than leaking it.
#
#   AC-5  Effort hydrates under the multi-PC ``characters:`` LIST path (not just
#         the singular ``character:`` block) and sources do not cross-contaminate
#         between PCs or between sources on one PC.
#
#   AC-6  Hydration emits a ``wwn.magic_hydrated`` OTEL watcher event whenever a
#         fixture seeds WWN crunch (spellcasting OR effort) — the GM-panel lie-
#         detector that proves the deterministic fixture seeded real Effort/spells
#         rather than the narrator improvising (CLAUDE.md OTEL Observability
#         Principle; this is the wiring test that survives refactor). The event
#         carries the seeded shape (actor, has_spellcasting, prepared count,
#         casts_per_day, sorted effort_sources) under component "magic". It must
#         NOT fire for a non-caster (no noise on the canonical dial fixtures).
#
# Scope boundary: the spellcasting hydration (``spellcasting:`` -> core.spellcasting)
# and the encounter hp_depletion seeding (win_condition/category/actors) already
# landed in #787 and are covered by the sections above — 90-7 adds ONLY the
# effort branch and the span.
#
# Reference: oq-1 closed PR #788 (the worked effort+span implementation + tests).


# An ``effort:`` block indented for a ``character:`` mapping (key at 2 spaces,
# source at 4, pool fields at 6) — one High Mage source with a 3-point pool,
# matching the WWN SRD seed (effort_base + relevant skill + governing attribute
# modifier).
_EFFORT_YAML = "  effort:\n    high_mage:\n      max: 3\n"


def _capture_wwn_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict, dict]]:
    """Patch ``watcher_hub.publish_event`` (module-qualified — the same handle
    ``scene_harness`` calls through, per its line-46 ``import ... as _hub``) so
    every emission is captured. Mirrors the canonical ``_capture_events`` harness
    in tests/server/test_render_mounts.py.

    Post-90-8 this capture is the NEGATIVE half of the lie-detector contract:
    the hydrator must no longer raw-publish ``wwn.magic_hydrated`` — the
    routed span (see ``_capture_spans``) replaces it.
    """
    captured: list[tuple[str, dict, dict]] = []

    def fake_publish(event_type, fields, *, component="", severity="info"):
        captured.append((event_type, dict(fields), {"component": component, "severity": severity}))

    import sidequest.telemetry.watcher_hub as _hub

    monkeypatch.setattr(_hub, "publish_event", fake_publish)
    return captured


def _wwn_hydrated_events(captured: list[tuple[str, dict, dict]]) -> list[tuple[str, dict, dict]]:
    """Filter a capture list down to ``wwn.magic_hydrated`` emissions."""
    return [e for e in captured if e[0] == "wwn.magic_hydrated"]


def _capture_spans(monkeypatch: pytest.MonkeyPatch):
    """Install a local in-memory tracer behind ``spans.tracer`` (story 90-8).

    ``Span.open`` with no ``tracer_override`` lazily resolves
    ``sidequest.telemetry.spans.tracer()`` — monkeypatching the module
    attribute intercepts the default production path, per the Span.open
    docstring and the tests/integration/conftest.py ``watcher_setup``
    precedent. Returns the exporter; filter with ``_wwn_hydrated_spans``.
    """
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    import sidequest.telemetry.spans as spans_module

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    local_tracer = provider.get_tracer("test-scene-harness-hydrator-90-8")
    monkeypatch.setattr(spans_module, "tracer", lambda: local_tracer)
    return exporter


def _wwn_hydrated_spans(exporter) -> list:
    """Filter finished spans down to ``wwn.magic_hydrated``."""
    return [s for s in exporter.get_finished_spans() if s.name == "wwn.magic_hydrated"]


def _typed_payload(span) -> dict:
    """Recover the typed GM-panel payload the WatcherSpanProcessor would emit
    for ``span`` — the SPAN_ROUTES extract is the production translation, so
    asserting through it pins the full span→typed-feed path (story 90-8)."""
    from sidequest.telemetry.spans import SPAN_ROUTES

    assert span.name in SPAN_ROUTES, (
        f"{span.name} must be routed in SPAN_ROUTES or the typed Subsystems "
        f"feed never sees it (the exact gap story 90-8 closes)"
    )
    return SPAN_ROUTES[span.name].extract(span)


# ── AC-1 / AC-3 / AC-5: _hydrate_character effort hydration ─────────────────


def test_character_effort_block_hydrates(tmp_path: Path) -> None:
    """AC-1: an ``effort:`` block under ``character:`` projects to ``core.effort``
    — a dict keyed by class-source, each value an EffortPool whose ``source`` is
    the key and whose ``max`` round-trips, so the cast spine can read it at
    commit time (``core.effort.get(source)``).

    RED driver: today ``_hydrate_character`` never reads ``effort`` and never
    passes it to ``CreatureCore``, so ``core.effort`` stays at its default
    (``{}``) — these assertions fail until 90-7 lands.
    """
    _write_magic_fixture(tmp_path, "effort_pc", extra_character_yaml=_EFFORT_YAML)

    from sidequest.game.scene_harness import hydrate_fixture
    from sidequest.game.wwn_magic import EffortPool

    snapshot = hydrate_fixture(name="effort_pc", fixtures_dir=tmp_path)
    effort = snapshot.characters[0].core.effort

    assert "high_mage" in effort, f"effort dict must be keyed by source; got {list(effort)!r}"
    pool = effort["high_mage"]
    assert isinstance(pool, EffortPool), (
        f"effort value must be an EffortPool; got {type(pool).__name__}"
    )
    assert pool.source == "high_mage", (
        f"EffortPool.source must be set from the fixture key; got {pool.source!r}"
    )
    assert pool.max == 3, f"EffortPool.max must hydrate from the block; got {pool.max}"
    assert pool.available == 3, (
        f"a freshly-seeded pool must be fully available; got {pool.available}"
    )


def test_missing_effort_block_leaves_core_effort_empty_dict(tmp_path: Path) -> None:
    """AC-2 (backward-compat / regression lock): a character with NO ``effort:``
    key keeps ``core.effort`` at the CreatureCore default (empty dict) — not None,
    not a fabricated pool.

    Guards against an implementation that *requires* the block or seeds a phantom
    pool for non-casters.
    """
    _write_magic_fixture(tmp_path, "no_effort", extra_character_yaml="")

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="no_effort", fixtures_dir=tmp_path)
    assert snapshot.characters[0].core.effort == {}, (
        f"omitting effort: must leave core.effort == {{}}; got {snapshot.characters[0].core.effort!r}"
    )


def test_effort_source_derived_from_key_not_in_value(tmp_path: Path) -> None:
    """AC-3: the EffortPool ``source`` is authoritative from the mapping KEY. A
    stray ``source:`` inside the value must be stripped/overridden so the key
    wins — otherwise a fixture could key a pool ``high_mage`` but stamp it
    ``source: vowed`` and the cast spine's ``core.effort.get(source)`` lookup
    would silently miss (a deterministic proof passing for the wrong reason).
    """
    _write_magic_fixture(
        tmp_path,
        "effort_source_footgun",
        extra_character_yaml="  effort:\n    high_mage:\n      max: 3\n      source: vowed\n",
    )

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="effort_source_footgun", fixtures_dir=tmp_path)
    effort = snapshot.characters[0].core.effort
    assert "high_mage" in effort, f"pool must be keyed by the mapping key; got {list(effort)!r}"
    assert effort["high_mage"].source == "high_mage", (
        f"in-value source: must NOT override the key; got {effort['high_mage'].source!r}"
    )
    assert "vowed" not in effort, (
        f"the in-value source: must not spawn a second pool; got {list(effort)!r}"
    )


def test_multiple_effort_sources_hydrate_independently(tmp_path: Path) -> None:
    """AC-1/AC-5: two distinct class-sources each seed their own EffortPool keyed
    by source, with no bleed between them (SRD §1.4.4 — Effort from one source
    cannot fuel another).
    """
    _write_magic_fixture(
        tmp_path,
        "two_sources",
        extra_character_yaml=(
            "  effort:\n    high_mage:\n      max: 3\n    vowed:\n      max: 2\n"
        ),
    )

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="two_sources", fixtures_dir=tmp_path)
    effort = snapshot.characters[0].core.effort
    assert set(effort) == {"high_mage", "vowed"}, f"both sources must seed; got {set(effort)!r}"
    assert effort["high_mage"].source == "high_mage" and effort["high_mage"].max == 3
    assert effort["vowed"].source == "vowed" and effort["vowed"].max == 2


def test_effort_hydrates_under_multi_pc_characters_list(tmp_path: Path) -> None:
    """AC-5: ``effort:`` works under a ``characters:`` LIST entry, not only the
    singular ``character:`` block (both funnel through ``_hydrate_character``).
    A sibling PC without an effort block keeps ``core.effort == {}`` — no
    cross-contamination.
    """
    body = (
        "genre: elemental_harmony\n"
        "world: test_world\n"
        "characters:\n"
        "  - name: Mei Lin\n"
        "    description: A High Mage of the Ember Isles\n"
        "    personality: serene\n"
        "    backstory: trained in the ember registers\n"
        "    char_class: High Mage\n"
        "    race: Human\n"
        "    effort:\n"
        "      high_mage:\n"
        "        max: 3\n"
        "  - name: Bo\n"
        "    description: A non-caster bruiser\n"
        "    personality: blunt\n"
        "    backstory: raised on the docks\n"
        "    char_class: Warrior\n"
        "    race: Human\n"
    )
    (tmp_path / "effort_multi.yaml").write_text(body, encoding="utf-8")

    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="effort_multi", fixtures_dir=tmp_path)
    caster, bruiser = snapshot.characters
    assert caster.core.effort.get("high_mage") is not None, (
        "caster in characters: list must seed effort"
    )
    assert caster.core.effort["high_mage"].max == 3
    assert bruiser.core.effort == {}, (
        f"a sibling PC with no effort: block must keep core.effort == {{}}; got {bruiser.core.effort!r}"
    )


# ── AC-4: effort block is save-bearing (loud-fail) ──────────────────────────


def test_effort_block_not_a_mapping_raises(tmp_path: Path) -> None:
    """AC-4: ``effort:`` declared as a list (or scalar) instead of a
    source-keyed mapping raises FixtureValidationError naming the block — never
    a silent skip (No Silent Fallbacks; lang-review #1/#11).

    RED driver: today the key is ignored entirely, so hydrate_fixture returns
    normally and pytest.raises fails — exactly the gap this story closes.
    """
    _write_magic_fixture(
        tmp_path,
        "list_effort",
        extra_character_yaml="  effort:\n    - high_mage\n",
    )

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError) as exc_info:
        hydrate_fixture(name="list_effort", fixtures_dir=tmp_path)

    assert "effort" in str(exc_info.value).lower(), (
        f"the error must name the offending block; got {str(exc_info.value)!r}"
    )


def test_effort_pool_value_not_a_mapping_raises(tmp_path: Path) -> None:
    """AC-4: an effort SOURCE whose value is a list/scalar instead of a pool
    mapping raises FixtureValidationError — the per-source shape is validated,
    not blindly splatted into EffortPool(**value).
    """
    _write_magic_fixture(
        tmp_path,
        "bad_pool_shape",
        extra_character_yaml="  effort:\n    high_mage:\n      - 3\n",
    )

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError):
        hydrate_fixture(name="bad_pool_shape", fixtures_dir=tmp_path)


def test_effort_pool_bad_field_type_raises(tmp_path: Path) -> None:
    """AC-4: an effort pool with a non-int ``max`` surfaces as
    FixtureValidationError — the pydantic ValidationError from EffortPool(...)
    must be re-wrapped at the hydrate_fixture boundary, not leaked.
    """
    _write_magic_fixture(
        tmp_path,
        "bad_effort_value",
        extra_character_yaml="  effort:\n    high_mage:\n      max: not_an_int\n",
    )

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError):
        hydrate_fixture(name="bad_effort_value", fixtures_dir=tmp_path)


def test_effort_pool_extra_field_rejected(tmp_path: Path) -> None:
    """AC-4: an extra/typo'd key inside a pool raises FixtureValidationError.
    EffortPool is ``extra="forbid"``; the hydrator must re-wrap the pydantic
    ValidationError at the module boundary (the same contract spellcasting /
    known_facts obey) rather than silently dropping the field.
    """
    _write_magic_fixture(
        tmp_path,
        "effort_extra",
        extra_character_yaml="  effort:\n    high_mage:\n      max: 3\n      bonus: 9\n",
    )

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError):
        hydrate_fixture(name="effort_extra", fixtures_dir=tmp_path)


# ── 90-7 fast-follow: non-string YAML key must fail loud, not leak TypeError ─


def test_effort_pool_non_string_key_raises(tmp_path: Path) -> None:
    """A non-string YAML key under a pool (YAML permits int/bool keys:
    ``high_mage:\\n  1: foo`` -> ``{1: 'foo'}``) must fail loud as
    FixtureValidationError, NOT leak a raw ``TypeError: keywords must be
    strings`` from the ``EffortPool(**pool_kwargs)`` splat. ADR-092 "Failure is
    loud" wants HTTP 422 at the module boundary, not an opaque 500.

    RED driver (pre-fix): the ``except ValidationError`` around the splat does
    not catch TypeError, so the raw TypeError escapes hydrate_fixture's wrap.
    """
    _write_magic_fixture(
        tmp_path,
        "effort_nonstr_key",
        extra_character_yaml="  effort:\n    high_mage:\n      max: 3\n      1: foo\n",
    )

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError):
        hydrate_fixture(name="effort_nonstr_key", fixtures_dir=tmp_path)


def test_spellcasting_non_string_key_raises(tmp_path: Path) -> None:
    """The same loud-fail contract for the sibling spellcasting branch — a
    non-string key under ``spellcasting:`` must raise FixtureValidationError,
    not a raw TypeError. (Pre-existing gap from #787's 90-4; closed here
    alongside the effort branch's identical splat.)
    """
    _write_magic_fixture(
        tmp_path,
        "sc_nonstr_key",
        extra_character_yaml=(
            "  spellcasting:\n"
            "    prepared: []\n"
            "    casts_remaining: 1\n"
            "    casts_per_day: 1\n"
            "    max_spell_level: 1\n"
            "    1: foo\n"
        ),
    )

    from sidequest.game.scene_harness import FixtureValidationError, hydrate_fixture

    with pytest.raises(FixtureValidationError):
        hydrate_fixture(name="sc_nonstr_key", fixtures_dir=tmp_path)


# ── AC-6: wwn.magic_hydrated OTEL span (GM-panel lie-detector / wiring test) ─


def test_effort_hydration_emits_wwn_magic_hydrated_span(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-6 (wiring test, 90-8 routed-span contract): seeding an ``effort:``
    block opens a single ``wwn.magic_hydrated`` OTEL span whose ROUTED typed
    payload carries the sorted effort_sources and has_spellcasting=False.
    This is the GM-panel lie-detector mandated by CLAUDE.md — and post-90-8
    it must arrive via SPAN_ROUTES (typed Subsystems feed), not a raw
    publish_event that only the RAW console can see.

    RED driver (90-8): the hydrator still calls ``_hub.publish_event``, so no
    span is opened and the raw-emit negative assertion fails too.
    """
    _write_magic_fixture(tmp_path, "effort_span", extra_character_yaml=_EFFORT_YAML)

    raw_events = _capture_wwn_events(monkeypatch)
    exporter = _capture_spans(monkeypatch)
    from sidequest.game.scene_harness import hydrate_fixture

    hydrate_fixture(name="effort_span", fixtures_dir=tmp_path)

    spans = _wwn_hydrated_spans(exporter)
    assert len(spans) == 1, (
        f"effort hydration must open exactly one wwn.magic_hydrated span; got {len(spans)}"
    )
    payload = _typed_payload(spans[0])
    sources = payload["effort_sources"]
    assert not isinstance(sources, str), (
        f"effort_sources must reach the typed feed as a JSON array; got {sources!r}"
    )
    assert list(sources) == ["high_mage"], (
        f"span must report the seeded sources (sorted); got {sources!r}"
    )
    assert payload["has_spellcasting"] is False, (
        f"effort-only fixture must report has_spellcasting=False; got "
        f"{payload.get('has_spellcasting')!r}"
    )
    assert payload["actor"] == "Practitioner", (
        f"span must name the actor; got {payload.get('actor')!r}"
    )
    assert _wwn_hydrated_events(raw_events) == [], (
        "the raw publish_event emit must be RETIRED by the routed span — a "
        "double emit puts one raw + one typed event on the dashboard per "
        f"hydration; got {_wwn_hydrated_events(raw_events)!r}"
    )


def test_spellcasting_hydration_emits_wwn_magic_hydrated_span(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-6 (90-8 routed-span contract): the lie-detector covers ALL WWN
    crunch, not just effort — a spellcasting-only fixture (the #787 path)
    must also open ``wwn.magic_hydrated``, whose typed payload reports
    has_spellcasting=True with the prepared count and casts_per_day, and an
    EMPTY effort_sources array (the boundary shape most likely to be
    silently dropped by OTEL attribute encoding).
    """
    _write_magic_fixture(tmp_path, "sc_span", extra_character_yaml=_SPELLCASTING_YAML)

    exporter = _capture_spans(monkeypatch)
    from sidequest.game.scene_harness import hydrate_fixture

    hydrate_fixture(name="sc_span", fixtures_dir=tmp_path)

    spans = _wwn_hydrated_spans(exporter)
    assert len(spans) == 1, (
        f"spellcasting hydration must open one wwn.magic_hydrated span; got {len(spans)}"
    )
    payload = _typed_payload(spans[0])
    assert payload["has_spellcasting"] is True, (
        f"a spellcasting fixture must report has_spellcasting=True; got "
        f"{payload.get('has_spellcasting')!r}"
    )
    assert payload["prepared"] == 2, (
        f"span must report prepared count; got {payload.get('prepared')!r}"
    )
    assert payload["casts_per_day"] == 2, (
        f"span must report casts_per_day; got {payload.get('casts_per_day')!r}"
    )
    sources = payload["effort_sources"]
    assert not isinstance(sources, str), (
        f"effort_sources must be a JSON array even when empty; got {sources!r}"
    )
    assert list(sources) == [], f"a spellcasting-only fixture seeds no effort; got {sources!r}"


def test_non_caster_emits_no_wwn_magic_hydrated_span(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-6 (no-noise guard, 90-8 routed-span contract): a plain non-caster
    fixture (no effort, no spellcasting) must open NO ``wwn.magic_hydrated``
    span AND raw-publish nothing — the GM panel must not light up for the
    canonical dial fixtures on either channel.
    """
    _write_magic_fixture(tmp_path, "plain_pc", extra_character_yaml="")

    raw_events = _capture_wwn_events(monkeypatch)
    exporter = _capture_spans(monkeypatch)
    from sidequest.game.scene_harness import hydrate_fixture

    hydrate_fixture(name="plain_pc", fixtures_dir=tmp_path)

    assert _wwn_hydrated_spans(exporter) == [], (
        f"a non-caster fixture must not open wwn.magic_hydrated; got "
        f"{[s.name for s in _wwn_hydrated_spans(exporter)]!r}"
    )
    assert _wwn_hydrated_events(raw_events) == [], (
        f"a non-caster fixture must not raw-publish wwn.magic_hydrated; got "
        f"{_wwn_hydrated_events(raw_events)!r}"
    )


# ── 90-7 fast-follow: committed on-disk WWN fixture exercises the effort block ─


def test_canonical_wwn_fixture_hydrates_effort_spellcasting_and_hp_depletion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wiring test: the committed ``combat_wwn_emberfront.yaml`` fixture in
    CANONICAL_FIXTURES_DIR hydrates all three WWN halves end-to-end —
    source-keyed Effort, prepared spellcasting, and an hp_depletion combat with
    seated actors — and emits the ``wwn.magic_hydrated`` lie-detector span.

    Closes the 90-7 delivery-finding gap: before this, the effort: block was
    covered only by tmp_path unit fixtures, with no on-disk artifact a real
    POST /dev/scene/{name} run (or 90-3's live proof) could load. This test
    drives the same CANONICAL_FIXTURES_DIR path the harness endpoint uses, so a
    schema drift in the committed fixture fails here, not silently in a playtest.
    """
    exporter = _capture_spans(monkeypatch)
    from sidequest.game.scene_harness import hydrate_fixture

    snapshot = hydrate_fixture(name="combat_wwn_emberfront", fixtures_dir=CANONICAL_FIXTURES_DIR)

    core = snapshot.characters[0].core
    # Effort: source-keyed by "channeler" (the real elemental_harmony Channeler source).
    assert core.effort.get("channeler") is not None, (
        f"fixture must seed core.effort['channeler']; got {sorted(core.effort)!r}"
    )
    assert core.effort["channeler"].source == "channeler"
    assert core.effort["channeler"].max == 2, (
        f"effort pool max must hydrate from the fixture; got {core.effort['channeler'].max}"
    )
    # Spellcasting: prepared verbatim so the cast can fire with no rest beat.
    assert core.spellcasting is not None, "fixture must seed core.spellcasting"
    assert core.spellcasting.prepared == ["cinder_lance"], (
        f"prepared spell must round-trip; got {core.spellcasting.prepared!r}"
    )
    assert core.spellcasting.casts_remaining == 2
    # Encounter: hp_depletion with both actors seated for the cast/strike spine.
    enc = snapshot.encounter
    assert enc is not None and enc.win_condition == "hp_depletion", (
        f"fixture must stand up an hp_depletion combat; got {enc.win_condition if enc else None!r}"
    )
    assert {a.side for a in enc.actors} == {"player", "opponent"}, (
        f"both sides must seat for the cast defender lookup; got {[a.side for a in enc.actors]!r}"
    )
    # Lie-detector span fired for the seeded crunch (90-8: routed span, so
    # the typed Subsystems feed — not just the RAW console — sees it).
    spans = _wwn_hydrated_spans(exporter)
    assert len(spans) == 1, (
        f"the WWN fixture must open one wwn.magic_hydrated span; got {len(spans)}"
    )
    payload = _typed_payload(spans[0])
    assert list(payload["effort_sources"]) == ["channeler"], (
        f"span must report the seeded effort source; got {payload.get('effort_sources')!r}"
    )
    assert payload["has_spellcasting"] is True
