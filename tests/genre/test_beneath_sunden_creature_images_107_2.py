"""Story 107-2 invariant, retuned by 158-52 — bestiary is the render source.

History: the 2026-06-13 combat playtest surfaced the early opponent as "the
creature of animal musk" with a bare "T" letter chip because the low-band
shaft creatures had no ``creatures.yaml`` image spec. Story 107-2 authored
the 6 shaft specs and pinned "every low-tagged bestiary entry must appear in
``creatures.yaml``" — the per-world-manifest model.

Story 158-52 (Keith, 2026-07-01) replaces that model: ``bestiary.yaml`` is
the single source of truth for creature-image production; the render
pipeline DERIVES the prompt from it, and ``creatures.yaml`` is demoted to an
OPTIONAL per-world override (naming conceits, bespoke marquee plates).
"Renderable" therefore means "resolvable to a render prompt":

  1. the entry has a non-empty bestiary ``description`` (the render subject),
  2. its NAMING is handled for this world's "nothing is named" conceit —
     either the world declares top-level ``name_is_secret: true`` in
     ``creatures.yaml`` (the pipeline de-proper-nouns every derived name) or
     the entry ships a per-id override whose ``name`` is not the bestiary
     proper noun. Z-Image paints proper nouns from subject AND clip.

The no-text/no-caption cleanup clause is NOT a per-description obligation
any more: it lives in the world's ``visual_style.yaml positive_suffix``
(which REPLACES the genre suffix for this world) and auto-layers at render
time. A guard below pins that clause where it now lives.

GATED: skipped when ``sidequest-content`` is not on disk (shipped-content
assertion, mirrors ``tests/genre/test_world_bestiary_content.py``).
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

pytestmark = pytest.mark.skipif(
    not GENRE_PACKS_DIR.is_dir(), reason="sidequest-content not on disk"
)

# The six low-band (level 1-2) shaft creatures 107-2 authored bespoke specs
# for — the actual early-combat opponents. Their non-proper-noun guard is
# kept under the derived-source model (158-52).
LOW_BAND_IDS = (
    "gnaw_swarm",
    "rope_spider",
    "hold_skeleton",
    "shaft_goblin",
    "grave_ghoul",
    "harrier_pack_leader",
)

# Medium/style tokens that MUST NOT appear in an override `description` — they
# auto-layer from visual_style.yaml positive_suffix; duplicating them flattens
# the render (context-story-107-2 Technical Guardrails, explicit list).
STYLE_TOKENS = ("pen-and-ink", "engraving", "crosshatch", "b&w", "black-and-white")


def _world_dir():
    try:
        pack_dir = find_pack_path("caverns_and_claudes")
    except PackNotFound:
        pytest.skip("caverns_and_claudes not on disk")
    return pack_dir / "worlds" / "beneath_sunden"


def _creatures_manifest() -> tuple[dict[str, dict], bool]:
    """Return (specs-by-id, name_is_secret flag) from creatures.yaml.

    Under the derived-source model the file is an OPTIONAL override manifest;
    for this world it must exist because it is where the naming conceit is
    declared (flag or per-id naming overrides).
    """
    path = _world_dir() / "creatures.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "creatures.yaml must be a mapping"
    creatures = data.get("creatures") or []
    assert isinstance(creatures, list), "`creatures:` must be a list when present"
    specs = {c["id"]: c for c in creatures if isinstance(c, dict) and c.get("id")}
    return specs, data.get("name_is_secret") is True


def _bestiary_entries_by_id() -> dict[str, dict]:
    path = _world_dir() / "bestiary.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = data.get("entries") if isinstance(data, dict) else None
    assert isinstance(entries, list) and entries
    return {e["id"]: e for e in entries if isinstance(e, dict) and e.get("id")}


def _naming_handled(spec: dict | None, secret_flag: bool, proper: str) -> bool:
    """The 158-52 naming contract for a 'nothing is named' world.

    A per-id override name that differs from the bestiary proper noun handles
    it; otherwise the world-level ``name_is_secret: true`` flag must be set so
    the pipeline de-proper-nouns the derived name.
    """
    if spec is not None:
        name = spec.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip().lower() != proper.strip().lower()
    return secret_flag


def test_every_low_tagged_bestiary_entry_is_renderable() -> None:
    """RETUNED (158-52): every low-tagged bestiary entry resolves to a render
    prompt — non-empty bestiary description + naming handled. Presence in
    creatures.yaml is no longer the bar; the bestiary is the source of truth.
    Keyed to the bestiary's own `tags` so a future low-band addition can't
    silently regress to a 'T' chip."""
    specs, secret_flag = _creatures_manifest()
    bestiary = _bestiary_entries_by_id()
    low_tagged = {
        eid: e for eid, e in bestiary.items() if "low" in (e.get("tags") or [])
    }
    assert low_tagged, "precondition: bestiary tags its low band"

    no_subject = [
        eid
        for eid, e in low_tagged.items()
        if not (e.get("description") or "").strip()
    ]
    assert not no_subject, (
        f"low-band bestiary entries with no description: {no_subject} — a "
        "derived render prompt needs the bestiary description as its subject"
    )

    unhandled = sorted(
        eid
        for eid, e in low_tagged.items()
        if not _naming_handled(specs.get(eid), secret_flag, e["name"])
    )
    assert not unhandled, (
        f"{len(unhandled)} low-band entries would derive their bestiary "
        f"proper noun into the CLIP prompt (first 10: {unhandled[:10]}) — "
        "this world's conceit is that nothing is named. Declare top-level "
        "`name_is_secret: true` in creatures.yaml or ship per-id naming "
        "overrides."
    )


def test_low_band_shaft_ids_keep_non_proper_noun_guard() -> None:
    """The historical 107-2 guard for the 6 bespoke shaft ids, kept under the
    derived model: where a bespoke spec names one of them, that name is a
    descriptive phrase (it slugifies to the PNG filename) — never the bestiary
    proper noun, no digits, no quotes. An id whose spec was dropped in the
    demotion must instead be covered by the world naming flag."""
    specs, secret_flag = _creatures_manifest()
    bestiary = _bestiary_entries_by_id()
    for cid in LOW_BAND_IDS:
        assert cid in bestiary, f"{cid}: shaft id vanished from bestiary.yaml"
        spec = specs.get(cid)
        proper = bestiary[cid]["name"]
        if spec is None or not (spec.get("name") or "").strip():
            assert secret_flag, (
                f"{cid}: no bespoke naming override and no world "
                "`name_is_secret: true` flag — the derived CLIP would paint "
                f"the proper noun {proper!r}"
            )
            continue
        name = spec["name"]
        assert name.strip().lower() != proper.strip().lower(), (
            f"{cid}: override `name` is the bestiary proper noun {proper!r}"
        )
        assert not any(ch.isdigit() for ch in name), (
            f"{cid}: spec name has digits: {name!r}"
        )
        assert '"' not in name and "'" not in name, (
            f"{cid}: spec name has quotes: {name!r}"
        )


def test_override_specs_are_well_formed_and_style_free() -> None:
    """Every override entry that remains in the demoted creatures.yaml must be
    a valid per-field override: a non-empty id, and any field it does declare
    is non-empty. A declared `description` stays SUBJECT-only camera prose —
    medium/style tokens auto-layer from visual_style.yaml and must not appear
    (context-story-107-2 Technical Guardrails, Keith emphatic)."""
    specs, _ = _creatures_manifest()
    assert specs, (
        "creatures.yaml carries no override entries at all — this world needs "
        "at least its bespoke capstone plates or naming overrides"
    )
    for cid, spec in specs.items():
        for field in ("name", "description"):
            if field in spec:
                assert isinstance(spec[field], str) and spec[field].strip(), (
                    f"{cid}: declared override field `{field}` is empty — "
                    "omit the field to inherit from the bestiary instead"
                )
        desc = (spec.get("description") or "").lower()
        leaked = [tok for tok in STYLE_TOKENS if tok in desc]
        assert not leaked, (
            f"{cid}: style/medium token(s) {leaked} in override `description` "
            "— these belong in visual_style.yaml positive_suffix"
        )


def test_world_suffix_carries_no_text_clause() -> None:
    """The no-text/no-caption cleanup clause moved out of per-creature
    descriptions and lives in the world visual_style positive_suffix (which
    REPLACES the genre suffix for this world; guidance_scale=0 means the
    negative prompt is ignored, so the positive suffix is the only place the
    clause can act). If this guard trips, every derived creature plate loses
    its caption protection at once."""
    path = _world_dir() / "visual_style.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    suffix = (data.get("positive_suffix") or "").lower()
    assert suffix.strip(), "beneath_sunden must ship a world positive_suffix"
    assert "no text" in suffix and "no caption" in suffix, (
        "world positive_suffix lost the no-text/no-caption cleanup clause — "
        "under the derived-source model this is the ONLY place it layers in"
    )


# ── GATE META-TESTS (story 158-61) ──────────────────────────────────────
#
# A gate that is too narrow does not fail. It PASSES input it should have
# rejected — which is precisely how the two 158-60 review findings survived
# review in the first place. Re-running the gates above against the shipped
# world therefore proves nothing about their REACH.
#
# So the tests below do not check the content. They check THE GATE: each one
# hands the real gate functions a copy of the shipped world with exactly one
# thing wrong and asserts that some gate rejects it. The control test pins the
# unmodified copy as clean, so a rejection can only have come from the poison.
#
# This is a fixture-driven behavior test, not a source-text wiring test
# (sidequest-server/CLAUDE.md "No Source-Text Wiring Tests"): the production
# gate functions run for real, against a real-shaped world on disk.

_WORLD_FILES = ("bestiary.yaml", "creatures.yaml", "visual_style.yaml")


def _meta(fn: Callable[..., None]) -> Callable[..., None]:
    """Mark a test as a gate meta-test so ``_gate_functions`` excludes it.

    Attribute-marked rather than name-matched so renaming a test cannot
    silently fold a meta-test into the set of gates it is measuring.
    """
    fn._is_gate_meta = True  # type: ignore[attr-defined]
    return fn


def _gate_functions() -> list[Callable[[], object]]:
    """Every shipped-content gate defined in this module.

    Enumerated from the module namespace rather than hand-listed so a gate
    added or renamed later is measured automatically.
    """
    gates = [
        obj
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj) and not getattr(obj, "_is_gate_meta", False)
    ]
    assert gates, "found no shipped-content gates in this module to measure"
    return gates


def _world_copy(tmp_path: Path) -> Path:
    """A byte-for-byte copy of the shipped beneath_sunden world files."""
    src = _world_dir()
    dest = tmp_path / "beneath_sunden"
    dest.mkdir()
    for name in _WORLD_FILES:
        shutil.copyfile(src / name, dest / name)
    return dest


def _rewrite(world: Path, filename: str, mutate: Callable[[dict], None]) -> None:
    path = world / filename
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"fixture precondition: {filename} is a mapping"
    mutate(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _spec(data: dict, cid: str) -> dict:
    for entry in data.get("creatures") or []:
        if isinstance(entry, dict) and entry.get("id") == cid:
            return entry
    raise AssertionError(f"fixture precondition: {cid!r} is a shipped override spec")


def _gates_rejecting(world: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Run every gate against ``world``; return the gates that REJECTED it.

    An empty list means the whole gate let this world through.
    """
    monkeypatch.setattr(sys.modules[__name__], "_world_dir", lambda: world)
    rejected: list[str] = []
    for fn in _gate_functions():
        try:
            fn()
        except AssertionError:
            rejected.append(fn.__name__)
    return rejected


@_meta
def test_gate_accepts_the_shipped_world_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CONTROL for every poison test below.

    Each poison test starts from this same copy and changes exactly one thing,
    so this baseline must be clean — otherwise a 'rejected' result would prove
    nothing about the poison. Also pins that the harness actually reaches the
    gates (a typo'd fixture path would fail here, not silently pass).
    """
    rejected = _gates_rejecting(_world_copy(tmp_path), monkeypatch)
    assert not rejected, (
        f"the unmodified shipped world is rejected by {rejected} — the poison "
        "fixtures below are measuring the copy, not the poison"
    )


@_meta
def test_gate_rejects_digits_in_a_low_band_spec_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gap 1. The spec ``name`` slugifies to the PNG filename and is sent as the
    CLIP prompt, so digits in it corrupt both. That guard exists — in
    ``test_low_band_shaft_ids_keep_non_proper_noun_guard``, which walks the
    hardcoded ``LOW_BAND_IDS`` six. ``stirge`` is one of the five low-band specs
    158-60 added, so it is low-tagged in the bestiary but outside that tuple and
    receives no name gating at all.
    """
    world = _world_copy(tmp_path)

    def poison(data: dict) -> None:
        _spec(data, "stirge")["name"] = "The Small Thirst On 2 Fast Wings"

    _rewrite(world, "creatures.yaml", poison)
    assert _gates_rejecting(world, monkeypatch), (
        "a low-band override name carrying a digit passed every gate — the name "
        "guard reaches only the hardcoded LOW_BAND_IDS six, not the eleven "
        "entries the bestiary actually tags `low`. Derive the id list from the "
        "bestiary tags, as test_every_low_tagged_bestiary_entry_is_renderable "
        "already does."
    )


@_meta
def test_gate_rejects_quotes_in_a_low_band_spec_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gap 1, second limb of the same guard. A quote in the name breaks the
    slugified filename and reaches the CLIP prompt as a quoted phrase — the very
    thing this world's 'nothing is named' conceit exists to prevent. ``grimlock``
    is likewise low-tagged but outside ``LOW_BAND_IDS``.
    """
    world = _world_copy(tmp_path)

    def poison(data: dict) -> None:
        _spec(data, "grimlock")["name"] = 'The Blind Thing That Hunts By "Sound"'

    _rewrite(world, "creatures.yaml", poison)
    assert _gates_rejecting(world, monkeypatch), (
        "a low-band override name carrying quotes passed every gate — same "
        "hardcoded-tuple blind spot as the digits case"
    )


@_meta
def test_gate_rejects_a_bad_spec_for_a_newly_low_tagged_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gap 1, the mechanism rather than the symptom.

    The fix is 'derive the id list from the bestiary `low` tags', NOT 'widen the
    hand-kept tuple from six to eleven'. This fixture adds a TWELFTH low-tagged
    bestiary entry plus a non-compliant override for it — exactly what a future
    content story does. A tag-derived gate catches it. A gate keyed to any
    hand-kept list, of six ids or of eleven, does not, and the low band regresses
    to the 'T' letter-chip the moment it grows again.
    """
    world = _world_copy(tmp_path)

    def add_bestiary_entry(data: dict) -> None:
        data["entries"].append(
            {
                "id": "sump_leech",
                "name": "Sump Leech",
                "description": (
                    "A blind swollen leech the length of a forearm, ringed and "
                    "glistening, humped across the wet stone of a standing pool."
                ),
                "tags": ["beast", "vermin", "low"],
            }
        )

    def add_override_spec(data: dict) -> None:
        data["creatures"].append(
            {
                "id": "sump_leech",
                "name": "The 9 Ringed Thing In The Standing Water",
                "description": (
                    "A blind ringed leech humped across wet cut stone, one low "
                    "grudging light along its back. No text, no caption, no "
                    "title, no lettering, no labels, no watermark, no border."
                ),
                "threat_level": 1,
                "tags": ["beast", "vermin", "low"],
            }
        )

    _rewrite(world, "bestiary.yaml", add_bestiary_entry)
    _rewrite(world, "creatures.yaml", add_override_spec)
    assert _gates_rejecting(world, monkeypatch), (
        "a brand-new low-tagged entry shipped an override name with a digit in "
        "it and passed every gate — the name guard is not closed under low-band "
        "growth. Widening LOW_BAND_IDS by hand does not fix this; deriving the "
        "id list from the bestiary `low` tags does."
    )


@_meta
def test_gate_rejects_a_spec_with_no_bestiary_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gap 2. Referential integrity runs in one direction only today:
    ``test_all_room_bindings_reference_real_bestiary_ids`` checks room→bestiary,
    and ``test_every_low_tagged_bestiary_entry_is_renderable`` walks bestiary→
    spec. Nothing walks spec→bestiary.

    Under ADR-155 ``bestiary.yaml`` is the single source of truth for creature-
    image production and ``creatures.yaml`` is an optional per-field override.
    An override for an id the source of truth has never heard of overrides
    nothing: it renders no plate and raises no complaint. Per No Silent
    Fallbacks that must fail at author time.
    """
    world = _world_copy(tmp_path)

    def poison(data: dict) -> None:
        data["creatures"].append(
            {
                "id": "the_spec_that_binds_to_nothing",
                "name": "The Thing That Was Never In The Roster",
                "description": (
                    "A shape in the dark that no bestiary entry describes. No "
                    "text, no caption, no title, no lettering, no labels, no "
                    "watermark, no border."
                ),
                "threat_level": 1,
                "tags": ["undead", "low"],
            }
        )

    _rewrite(world, "creatures.yaml", poison)
    assert _gates_rejecting(world, monkeypatch), (
        "a phantom override spec resolving to no bestiary entry passed every "
        "gate — add the converse spec→bestiary integrity check, mirroring "
        "test_all_room_bindings_reference_real_bestiary_ids"
    )


@_meta
def test_gate_rejects_a_typod_spec_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Gap 2, the way it actually reaches a repo. Nobody authors a phantom on
    purpose; they mistype an id. One dropped letter costs twice — the bespoke
    plate is silently detached from the creature it was written for (which then
    falls back to its derived prose), and an orphan override is left behind. The
    world still parses, every gate still passes, and the only symptom is a
    portrait that quietly stopped being the one someone wrote.
    """
    world = _world_copy(tmp_path)

    def poison(data: dict) -> None:
        _spec(data, "grimlock")["id"] = "grimlok"

    _rewrite(world, "creatures.yaml", poison)
    assert _gates_rejecting(world, monkeypatch), (
        "a one-letter typo in an override id detached a bespoke plate from its "
        "creature and left an orphan spec, and every gate passed — this is the "
        "silent fallback the converse integrity check exists to catch"
    )
