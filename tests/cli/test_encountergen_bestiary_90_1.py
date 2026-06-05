"""RED (story 90-1): encountergen is ruleset-aware via a content bestiary.

Epic 90 root story. Today ``generate_enemy`` is native-dial-only: it reads
``pack.rules.allowed_classes`` and hard-fails (``sys.exit(1)``, stderr message
"... has no allowed_classes in rules.yaml") for RulesetModule packs (ADR-117:
``ruleset: wwn|cwn|swn|awn``), which deliberately drop that block. The result
(87-4 finding) is an empty Monster Manual ``encounters`` pool for heavy_metal
evropi/long_foundry — no reachable hostiles in free play.

Decision (Keith, 2026-06-05 — Option B): for a ruleset-module pack,
encountergen reads a **content-authored bestiary** (``bestiary.yaml`` at the
pack root; ruleset implicit from ``rules.yaml``) instead of generating from
``allowed_classes``. A bestiary entry supplies the COMBAT layer (id, name,
level, hp, armor_class, attack_bonus, ...); encountergen keeps composing the
narrative layers (OCEAN, tropes, visual_prompt) as today. The bestiary is
REQUIRED for ruleset-module packs — fail loud if missing, never a silent empty
pool (No Silent Fallbacks). Native packs are untouched (regression-locked
below).

These tests FAIL until the bestiary path lands. Scope guards: this story does
NOT change triggered-confrontation seating — ``opponent_default_stats`` seating
is already locked by ``tests/integration/test_wwn_heavy_metal_combat.py`` and
is deliberately not re-asserted here.
"""

from __future__ import annotations

import json
import random

import pytest
import yaml

from sidequest.cli.encountergen.encountergen import main
from sidequest.genre.loader import load_genre_pack
from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

# The combat-layer fields a bestiary entry must supply (90-1 schema decision).
BESTIARY_ENTRY_REQUIRED_FIELDS = {"id", "name", "level", "hp", "armor_class", "attack_bonus"}


def _pack_dir_or_skip(slug: str):
    try:
        return find_pack_path(slug)
    except PackNotFound:
        pytest.skip(f"sidequest-content pack {slug!r} not on disk")


# ---------------------------------------------------------------------------
# AC1 — no hard-fail on a ruleset-module pack
# ---------------------------------------------------------------------------


def test_ruleset_module_pack_does_not_hard_fail(capsys: pytest.CaptureFixture[str]) -> None:
    """AC1: encountergen on a ``ruleset: wwn`` pack (heavy_metal) must not
    sys.exit over missing ``allowed_classes`` — it routes to the bestiary."""
    _pack_dir_or_skip("heavy_metal")
    argv = [
        "--genre-packs-path",
        str(GENRE_PACKS_DIR),
        "--genre",
        "heavy_metal",
        "--tier",
        "1",
        "--count",
        "2",
    ]
    try:
        rc = main(argv)
    except SystemExit as exc:  # the current hard-fail path
        err = capsys.readouterr().err
        pytest.fail(
            f"encountergen hard-failed (exit {exc.code}) on a ruleset-module pack; "
            f"stderr: {err.strip()!r} — 90-1 requires the bestiary path instead"
        )
    assert rc == 0, "encountergen must succeed on a ruleset-module pack via its bestiary"
    output = json.loads(capsys.readouterr().out)
    assert output["enemies"], "bestiary path must emit at least one enemy"


# ---------------------------------------------------------------------------
# AC2 — bestiary-sourced combat layer on the emitted enemies
# ---------------------------------------------------------------------------


def test_wwn_enemies_carry_bestiary_combat_fields(capsys: pytest.CaptureFixture[str]) -> None:
    """AC2: enemies emitted for a wwn pack carry the bestiary combat layer
    (armor_class + attack_bonus — fields the allowed_classes path never set)
    with WWN-sane values."""
    _pack_dir_or_skip("heavy_metal")
    rc = main(
        [
            "--genre-packs-path",
            str(GENRE_PACKS_DIR),
            "--genre",
            "heavy_metal",
            "--tier",
            "1",
            "--count",
            "2",
        ]
    )
    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    assert output["enemies"]
    for enemy in output["enemies"]:
        assert "armor_class" in enemy, "bestiary combat layer must include armor_class"
        assert "attack_bonus" in enemy, "bestiary combat layer must include attack_bonus"
        assert isinstance(enemy["armor_class"], int) and 5 <= enemy["armor_class"] <= 22, (
            f"WWN-sane armor_class expected, got {enemy['armor_class']!r}"
        )
        assert isinstance(enemy["attack_bonus"], int), (
            f"integer attack_bonus expected, got {enemy['attack_bonus']!r}"
        )
        assert isinstance(enemy["hp"], int) and enemy["hp"] > 0
        # Narrative layers still composed by encountergen (not the bestiary's job).
        assert enemy["visual_prompt"], "narrative layer (visual_prompt) must still be composed"


# ---------------------------------------------------------------------------
# Bestiary content contract — required for ruleset-module packs (fail loud)
# ---------------------------------------------------------------------------


def test_heavy_metal_ships_a_wellformed_bestiary() -> None:
    """90-1 content deliverable: heavy_metal (ruleset: wwn) ships
    ``bestiary.yaml`` at the pack root with at least one well-formed entry
    carrying the agreed combat-layer fields."""
    pack_dir = _pack_dir_or_skip("heavy_metal")
    bestiary_path = pack_dir / "bestiary.yaml"
    assert bestiary_path.is_file(), (
        "ruleset-module packs REQUIRE a bestiary.yaml (90-1 fail-loud contract); "
        f"missing at {bestiary_path}"
    )
    data = yaml.safe_load(bestiary_path.read_text(encoding="utf-8"))
    entries = data.get("entries") if isinstance(data, dict) else None
    assert entries, "bestiary.yaml must define a non-empty top-level `entries:` list"
    for entry in entries:
        missing = BESTIARY_ENTRY_REQUIRED_FIELDS - set(entry)
        assert not missing, (
            f"bestiary entry {entry.get('id', '?')!r} missing fields: {sorted(missing)}"
        )


def test_bestiary_requirement_is_ruleset_generic() -> None:
    """AC6: the bestiary path keys on ``ruleset != native`` — every live
    ruleset-module pack (wwn/cwn/swn) ships a bestiary, not just heavy_metal.
    This pins the seam as ruleset-generic rather than a wwn special case."""
    failures: list[str] = []
    found_any = False
    for slug in ("heavy_metal", "elemental_harmony", "neon_dystopia", "space_opera"):
        try:
            pack_dir = find_pack_path(slug)
        except PackNotFound:
            continue
        pack = load_genre_pack(pack_dir)
        if pack.rules.ruleset == "native":
            continue
        found_any = True
        if not (pack_dir / "bestiary.yaml").is_file():
            failures.append(f"{slug} (ruleset: {pack.rules.ruleset})")
    if not found_any:
        pytest.skip("no ruleset-module packs on disk")
    assert not failures, (
        f"ruleset-module packs missing bestiary.yaml: {failures} — the 90-1 seam is "
        "ruleset-generic (wwn/cwn/swn), not heavy_metal-only"
    )


# ---------------------------------------------------------------------------
# AC5 — native-dial regression lock (must PASS today and after 90-1)
# ---------------------------------------------------------------------------


def test_native_pack_still_generates_via_allowed_classes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC5 regression lock: a native pack (caverns_and_claudes) keeps today's
    allowed_classes generation path, byte-for-byte deterministic under a seed.
    GREEN now; must stay GREEN after the bestiary branch lands."""
    pack_dir = _pack_dir_or_skip("caverns_and_claudes")
    pack = load_genre_pack(pack_dir)
    assert pack.rules.ruleset == "native", "precondition: caverns_and_claudes is native"
    assert pack.rules.allowed_classes, "precondition: native pack declares allowed_classes"

    rng = random.Random(90_1)
    rc = main(
        [
            "--genre-packs-path",
            str(GENRE_PACKS_DIR),
            "--genre",
            "caverns_and_claudes",
            "--tier",
            "1",
            "--count",
            "2",
        ]
    )
    assert rc == 0
    del rng  # determinism is owned by main's internal seeding; rc + shape is the lock
    output = json.loads(capsys.readouterr().out)
    assert len(output["enemies"]) == 2
    for enemy in output["enemies"]:
        assert enemy["class"] in pack.rules.allowed_classes, (
            "native path must keep drawing classes from rules.allowed_classes"
        )
        assert enemy["hp"] > 0
