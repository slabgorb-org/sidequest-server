"""GenrePack.effective_bestiary + encountergen routing — world-over-genre
REPLACE resolution (story 90-1, genre/world repoint).

**These are SEAM tests: they touch ZERO shipped content.** The seam under test
— ``GenrePack.effective_bestiary(world)`` and encountergen's ruleset-module
routing — is pure logic that needs no real pack. Asserting it against live
slugs (``find_pack_path("heavy_metal")``, ``worlds/evropi/bestiary.yaml``) was
the prod-rows-in-tests anti-pattern that made a *content* repoint redden
*server* tests; fixing the seam must mean updating fixtures, never chasing live
content.

Two layers, mirroring the canonical pattern in
``tests/genre/test_pack_effective_cultures.py``:

1. **In-memory unit tests** — ``model_construct`` a pack + world and assert the
   resolution (world REPLACES genre when present; genre serves otherwise; None
   only when neither tier supplies one).
2. **Synthetic-fixture routing tests** — copy ``tests/fixtures/packs/test_genre``
   to a tmp dir, bind a ruleset module, and assert ``main()`` actually samples
   the resolved bestiary (world-over-genre) and fails loud when neither tier
   ships one.

Shipped content (does heavy_metal/evropi author a well-formed roster, do all
ruleset-module worlds resolve a non-empty bestiary) is a *content* assertion and
lives gated in ``tests/genre/test_world_bestiary_content.py`` — out of these
seam tests, so a content move can never break the seam suite again.

Decision (Keith, 2026-06-05 — Option B; genre/world repoint update): for a
ruleset-module pack (ADR-117 ``ruleset: wwn|cwn|swn|awn``) encountergen reads a
content-authored bestiary instead of generating from ``allowed_classes``. After
the repoint, creature rosters live at the WORLD tier
(``worlds/<slug>/bestiary.yaml``); ``effective_bestiary`` resolves them
world-over-genre. The bestiary is REQUIRED for ruleset-module packs — fail loud
if neither tier supplies one (No Silent Fallbacks). Native packs are untouched.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from sidequest.cli.encountergen.encountergen import main
from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.bestiary import Bestiary, BestiaryEntry
from sidequest.genre.models.pack import GenrePack, World

# The combat-layer fields a bestiary entry must supply (90-1 schema decision).
BESTIARY_ENTRY_REQUIRED_FIELDS = {"id", "name", "level", "hp", "armor_class", "attack_bonus"}

# The complete native fixture pack ships everything load_genre_pack + the native
# generation path need (rules.allowed_classes, archetypes, cultures, ...).
_FIXTURE_PACKS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "packs"
_NATIVE_FIXTURE_PACK = _FIXTURE_PACKS_DIR / "test_genre"


# ---------------------------------------------------------------------------
# In-memory builders (no content) — mirror test_pack_effective_cultures.py
# ---------------------------------------------------------------------------


def _entry(id_: str) -> BestiaryEntry:
    # model_construct bypasses the field validators — the seam only reads the
    # bestiary object back out, so a minimal stub is enough.
    return BestiaryEntry.model_construct(
        id=id_, name=id_.title(), level=1, hp=4, armor_class=12, attack_bonus=1
    )


def _bestiary(*ids: str) -> Bestiary:
    return Bestiary.model_construct(entries=[_entry(i) for i in ids])


def _pack(
    *, genre_bestiary: Bestiary | None = None, worlds: dict[str, World] | None = None
) -> GenrePack:
    return GenrePack.model_construct(bestiary=genre_bestiary, worlds=worlds or {})


def _world(*, bestiary: Bestiary | None = None) -> World:
    return World.model_construct(bestiary=bestiary)


# ---------------------------------------------------------------------------
# Seam unit tests — world-over-genre REPLACE resolution
# ---------------------------------------------------------------------------


def test_world_bestiary_replaces_genre_when_present() -> None:
    pack = _pack(
        genre_bestiary=_bestiary("genre_goblin"),
        worlds={"w": _world(bestiary=_bestiary("world_wraith", "world_hound"))},
    )
    bestiary, source = pack.effective_bestiary("w")
    assert source == "world"
    assert bestiary is not None
    assert [e.id for e in bestiary.entries] == ["world_wraith", "world_hound"]


def test_genre_bestiary_when_world_declares_none() -> None:
    # A world present but shipping no bestiary inherits the genre tier.
    pack = _pack(genre_bestiary=_bestiary("genre_goblin"), worlds={"w": _world(bestiary=None)})
    bestiary, source = pack.effective_bestiary("w")
    assert source == "genre"
    assert bestiary is not None
    assert [e.id for e in bestiary.entries] == ["genre_goblin"]


def test_genre_bestiary_when_world_arg_none() -> None:
    pack = _pack(genre_bestiary=_bestiary("genre_goblin"))
    bestiary, source = pack.effective_bestiary(None)
    assert source == "genre"
    assert bestiary is not None
    assert [e.id for e in bestiary.entries] == ["genre_goblin"]


def test_genre_bestiary_when_world_unknown() -> None:
    pack = _pack(
        genre_bestiary=_bestiary("genre_goblin"),
        worlds={"w": _world(bestiary=_bestiary("world_wraith"))},
    )
    bestiary, source = pack.effective_bestiary("no_such_world")
    assert source == "genre"
    assert bestiary is not None
    assert [e.id for e in bestiary.entries] == ["genre_goblin"]


def test_no_bestiary_either_tier_returns_none() -> None:
    # The only None case: neither tier supplies a bestiary. main() turns this
    # into a fail-loud for ruleset-module packs (asserted below).
    pack = _pack(genre_bestiary=None, worlds={"w": _world(bestiary=None)})
    bestiary, source = pack.effective_bestiary("w")
    assert bestiary is None
    assert source == "genre"


# ---------------------------------------------------------------------------
# Synthetic-fixture routing tests — main() samples the resolved bestiary
# ---------------------------------------------------------------------------


def _write_bestiary(path: Path, ids: tuple[str, ...]) -> None:
    entries = [
        {
            "id": i,
            "name": i.replace("_", " ").title(),
            "level": 1,
            "hp": 6,
            "armor_class": 13,
            "attack_bonus": 2,
            "damage": "1d6",
            "role": "skirmisher",
            "tags": ["beast"],
            "description": f"A {i.replace('_', ' ')}.",
        }
        for i in ids
    ]
    path.write_text(yaml.safe_dump({"entries": entries}), encoding="utf-8")


# SWN attribute_map keying the six canonical SWN attributes to the test_genre
# fixture's flavor stats — the minimal block a ``ruleset: swn`` pack must author.
_SWN_ATTRIBUTE_MAP = {
    "STRENGTH": "Brawn",
    "DEXTERITY": "Reflexes",
    "CONSTITUTION": "Toughness",
    "INTELLIGENCE": "Wits",
    "WISDOM": "Instinct",
    "CHARISMA": "Presence",
}


def _make_ruleset_module_pack(
    tmp_path: Path,
    *,
    world: str = "flickering_reach",
    world_bestiary_ids: tuple[str, ...] | None = ("worldbeast",),
    genre_bestiary_ids: tuple[str, ...] | None = ("genrebeast",),
) -> Path:
    """Copy the native fixture pack and rebind it to a ruleset module (swn — the
    lightest binding, attribute_map only) so main() takes the bestiary branch.
    No shipped content involved."""
    dst = tmp_path / "test_genre"
    shutil.copytree(_NATIVE_FIXTURE_PACK, dst)

    # Bind swn — drops the native allowed_classes routing in main(). The swn
    # block needs a complete attribute_map (RulesConfig fail-loud, no default).
    rules_path = dst / "rules.yaml"
    rules = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
    rules["ruleset"] = "swn"
    rules["swn"] = {"attribute_map": _SWN_ATTRIBUTE_MAP}
    rules_path.write_text(yaml.safe_dump(rules), encoding="utf-8")

    # main() checks worlds/<world>/creatures.yaml BEFORE the bestiary branch, so
    # remove it to exercise the bestiary path the seam owns.
    (dst / "worlds" / world / "creatures.yaml").unlink(missing_ok=True)

    if genre_bestiary_ids is not None:
        _write_bestiary(dst / "bestiary.yaml", genre_bestiary_ids)
    if world_bestiary_ids is not None:
        _write_bestiary(dst / "worlds" / world / "bestiary.yaml", world_bestiary_ids)
    return dst


# Corpus files the test_genre fixture's cultures reference. namegen resolves a
# pack-local corpus/ dir first, so stubbing these makes the native generation
# path self-contained — no dependency on shipped sidequest-content corpus.
_FIXTURE_CORPUS_FILES = (
    "english.txt",
    "finnish.txt",
    "french.txt",
    "german.txt",
    "hungarian.txt",
    "polish.txt",
    "swedish.txt",
)
# namegen enforces a minimum corpus size (>=200 words); generate enough stubs.
_STUB_CORPUS_WORDS = "\n".join(f"Stubname{n:03d}" for n in range(250)) + "\n"


def _make_native_pack(tmp_path: Path) -> Path:
    """Copy the native fixture pack with a self-contained corpus so the native
    namegen path resolves without shipped content."""
    dst = tmp_path / "test_genre"
    shutil.copytree(_NATIVE_FIXTURE_PACK, dst)
    corpus_dir = dst / "corpus"
    corpus_dir.mkdir(exist_ok=True)
    for fname in _FIXTURE_CORPUS_FILES:
        (corpus_dir / fname).write_text(_STUB_CORPUS_WORDS, encoding="utf-8")
    return dst


def test_main_samples_world_bestiary_over_genre(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Routing: main() on a ruleset-module pack samples the WORLD bestiary, not
    the genre tier — disjoint name sets prove which tier was sampled."""
    _make_ruleset_module_pack(
        tmp_path,
        world_bestiary_ids=("voidwretch", "glasswraith"),
        genre_bestiary_ids=("genre_filler",),
    )
    rc = main(
        [
            "--genre-packs-path",
            str(tmp_path),
            "--genre",
            "test_genre",
            "--world",
            "flickering_reach",
            "--tier",
            "1",
            "--count",
            "5",
        ]
    )
    assert rc == 0, "encountergen must succeed on a ruleset-module pack via its bestiary"
    output = json.loads(capsys.readouterr().out)
    assert output["enemies"], "bestiary path must emit at least one enemy"

    names = {e["name"] for e in output["enemies"]}
    assert names <= {"Voidwretch", "Glasswraith"}, (
        f"main() must sample the WORLD bestiary (world-over-genre), got {names}"
    )
    assert "Genre Filler" not in names, "genre tier must not be sampled when the world ships one"

    # The bestiary supplies the combat layer; encountergen still composes prose.
    for enemy in output["enemies"]:
        assert "armor_class" in enemy and isinstance(enemy["armor_class"], int)
        assert "attack_bonus" in enemy and isinstance(enemy["attack_bonus"], int)
        assert isinstance(enemy["hp"], int) and enemy["hp"] > 0
        assert enemy["visual_prompt"], "narrative layer (visual_prompt) must still be composed"


def test_main_falls_back_to_genre_bestiary_when_world_ships_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Routing: a ruleset-module world with no bestiary inherits the genre tier."""
    _make_ruleset_module_pack(
        tmp_path,
        world_bestiary_ids=None,
        genre_bestiary_ids=("genre_only_beast",),
    )
    rc = main(
        [
            "--genre-packs-path",
            str(tmp_path),
            "--genre",
            "test_genre",
            "--world",
            "flickering_reach",
            "--tier",
            "1",
            "--count",
            "3",
        ]
    )
    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    assert output["enemies"]
    assert {e["name"] for e in output["enemies"]} == {"Genre Only Beast"}


def test_main_fails_loud_when_ruleset_module_resolves_no_bestiary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Routing: a ruleset-module pack with no bestiary at EITHER tier must fail
    loud (No Silent Fallbacks) — never a silently-empty Monster Manual pool."""
    _make_ruleset_module_pack(tmp_path, world_bestiary_ids=None, genre_bestiary_ids=None)
    rc = main(
        [
            "--genre-packs-path",
            str(tmp_path),
            "--genre",
            "test_genre",
            "--world",
            "flickering_reach",
            "--tier",
            "1",
            "--count",
            "2",
        ]
    )
    assert rc == 1, "ruleset-module pack with no bestiary must fail loud"
    err = capsys.readouterr().err
    assert "bestiary" in err.lower() and "REQUIRE" in err, (
        f"fail-loud message must name the missing bestiary contract; got {err.strip()!r}"
    )


# ---------------------------------------------------------------------------
# Native-dial regression lock (synthetic native fixture) — must stay GREEN
# ---------------------------------------------------------------------------


def test_native_fixture_pack_routes_through_allowed_classes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A native pack keeps the allowed_classes generation path — the bestiary
    branch must not steal native routing. Uses the synthetic native fixture, not
    a shipped pack."""
    pack_dir = _make_native_pack(tmp_path)
    pack = load_genre_pack(pack_dir)
    assert pack.rules.ruleset == "native", "precondition: test_genre fixture is native"
    assert pack.rules.allowed_classes, "precondition: native fixture declares allowed_classes"

    rc = main(
        [
            "--genre-packs-path",
            str(tmp_path),
            "--genre",
            "test_genre",
            "--tier",
            "1",
            "--count",
            "2",
        ]
    )
    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    assert len(output["enemies"]) == 2
    for enemy in output["enemies"]:
        assert enemy["class"] in pack.rules.allowed_classes, (
            "native path must keep drawing classes from rules.allowed_classes"
        )
        assert enemy["hp"] > 0
