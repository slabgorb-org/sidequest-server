"""RED — Story 158-32: world authored-NPC rosters must LOAD, or the loader fails loud.

Playtest pingpong 2026-06-25 (heavy_metal/barsoom, session 2026-06-25-barsoom-84c17bdf,
turns 3-4):

    npc.invented_name_routed original='Salensus Oll' minted='Dentos Foun'
    culture='Yellow Martian' source='world' strategy='shuffle_fallback'
    -> npc.auto_registered name='Dentos Foun'

One canonical Burroughs antagonist fractured into THREE identities in a single
scene — narration "Salensus Oll", this turn's mint "Dentos Foun", and the
Relationships surface "Neon Sill" — with the canonical "Salensus Oll" NOWHERE in
the registry. "Kantos Vah" (also authored) landed in the *pool* as
``narrator_invented``.

ROOT CAUSE (found during TEA red-phase investigation; the story's "add a server
name-lock" framing was a mis-diagnosis): barsoom's authored roster never loads.

* The loader (``loader.py``) reads ``npcs_raw.get("npcs", [])`` — top-level key
  ``npcs:``. But ``heavy_metal/worlds/barsoom/npcs.yaml`` and
  ``heavy_metal/worlds/evropi/npcs.yaml`` use the key ``authored_npcs:`` with
  entries (``culture``, ``location``, ``disposition: <string>``, ``goals``) that
  do not conform to the ``AuthoredNpc`` schema (``extra="forbid"``,
  ``initial_disposition: int``). Only these 2 of 14 worlds are malformed — every
  other world uses ``npcs:`` and conforms.
* Result: ``pack.worlds["barsoom"].authored_npcs == []``. Nothing pre-loads into
  the live roster, so every canon mention falls through ``_apply_npc_mentions``
  Step 1/2 to the Step-3 culture generator and is shuffled into a phantom. The
  ``.get("npcs", [])`` swallow is itself a No-Silent-Fallbacks violation: a
  present-but-malformed npcs.yaml loaded empty instead of failing loud, so the
  defect slipped to a playtest.

THE FIX (operator decision 2026-06-26 — "Content + loader fail-loud"):
  1. CONTENT (heavy_metal): rewrite barsoom + evropi npcs.yaml to the AuthoredNpc
     schema (top-level key ``npcs:``, conforming fields) so the rosters load.
  2. SERVER (loader): a present npcs.yaml that yields zero authored NPCs (e.g. the
     list sits under an unrecognized top-level key) must raise a LOUD, world-named
     error — never silently produce an empty roster.

  Once the roster loads, the EXISTING ``preload_authored_npcs`` + Step-1
  article-fold reconciliation (the WW-CANONICAL machinery already protecting
  wonderland/oz/gulliver) prevents the fracture. No new mint-seam lock is built.

SEQUENCING NOTE for Dev: the content rewrite and the loader fail-loud are one
atomic change — landing the loud loader before the content fix would make
``load_genre_pack(heavy_metal)`` raise on barsoom/evropi and break every existing
heavy_metal-loading test.

Span names are asserted as STRINGS, not imported constants.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sidequest.agents.orchestrator import NpcMention
from sidequest.game.session import GameSnapshot
from sidequest.game.world_materialization import preload_authored_npcs
from sidequest.genre.loader import load_genre_pack
from sidequest.server.narration_apply import _apply_npc_mentions
from tests._helpers.genre_paths import GENRE_PACKS_DIR, GENRE_WORKSHOPPING_DIR

ROUTED_SPAN = "npc.invented_name_routed"


def _heavy_metal_dir() -> Path | None:
    for root in (GENRE_PACKS_DIR, GENRE_WORKSHOPPING_DIR):
        candidate = root / "heavy_metal"
        if (candidate / "pack.yaml").is_file():
            return candidate
    return None


_HM_DIR = _heavy_metal_dir()
requires_heavy_metal = pytest.mark.skipif(
    _HM_DIR is None, reason="heavy_metal pack not checked out"
)


def _mention(name: str, *, role: str = "", pronouns: str = "") -> NpcMention:
    return NpcMention(name=name, role=role, pronouns=pronouns, appearance="")


def _attrs_for(otel_capture, span_name: str) -> list[dict]:
    return [
        dict(s.attributes or {}) for s in otel_capture.get_finished_spans() if s.name == span_name
    ]


# ===========================================================================
# Group A — content conformance: the authored rosters must LOAD non-empty
# ===========================================================================


@requires_heavy_metal
def test_barsoom_authored_roster_loads_with_canon_names() -> None:
    """barsoom/npcs.yaml must load a non-empty AuthoredNpc roster including the
    canon antagonist + host that the playtest fractured.

    RED today: the file uses key ``authored_npcs:`` with non-conforming entries,
    so ``world.authored_npcs`` is silently ``[]``. GREEN once the content is
    rewritten to the ``npcs:`` / AuthoredNpc schema.
    """
    assert _HM_DIR is not None
    pack = load_genre_pack(_HM_DIR)
    names = {n.name for n in pack.worlds["barsoom"].authored_npcs}

    assert names, (
        "barsoom authored roster loaded EMPTY — npcs.yaml uses the wrong top-level "
        "key (authored_npcs: instead of npcs:) with non-conforming entries, so the "
        "loader yields []. The canon roster never reaches game state."
    )
    missing = {"Salensus Oll", "Kantos Vah"} - names
    assert not missing, f"barsoom must author its canon figures; missing {sorted(missing)}"


@requires_heavy_metal
def test_evropi_authored_roster_loads_non_empty() -> None:
    """evropi/npcs.yaml has the same key/schema defect as barsoom and must also
    load a non-empty roster after the content rewrite."""
    assert _HM_DIR is not None
    pack = load_genre_pack(_HM_DIR)
    names = {n.name for n in pack.worlds["evropi"].authored_npcs}

    assert names, (
        "evropi authored roster loaded EMPTY — same authored_npcs:/schema defect as "
        "barsoom; the rewrite to npcs:/AuthoredNpc must populate it."
    )
    assert "Ebbe" in names, f"a known evropi canon figure must load; got {sorted(names)}"


# ===========================================================================
# Group B — loader fail-loud (No Silent Fallbacks)
# ===========================================================================


def test_loader_fails_loud_on_npcs_yaml_unrecognized_top_level_key(
    minimal_pack_factory: Any, tmp_path: Path
) -> None:
    """A present npcs.yaml whose authored list sits under an unrecognized key
    (``authored_npcs:`` — the barsoom/evropi mistake) must raise a LOUD,
    world-named error at load — never silently yield an empty roster.

    RED today: ``npcs_raw.get("npcs", [])`` returns ``[]`` and load succeeds with
    a silently-empty roster (no raise). GREEN once the loader rejects the
    unrecognized key.
    """
    pack = minimal_pack_factory(tmp_path)
    world_dir = pack.path / "worlds" / "flickering_reach"
    (world_dir / "npcs.yaml").write_text(
        "world_name: Flickering Reach\n"
        "authored_npcs:\n"
        "  - id: canon_boss\n"
        '    name: "Canon Boss"\n'
        '    role: "the warlord"\n',
        encoding="utf-8",
    )

    with pytest.raises(Exception) as exc_info:  # noqa: PT011 - Dev picks the exact error type
        load_genre_pack(pack.path)

    message = str(exc_info.value)
    assert "flickering_reach" in message or "npcs.yaml" in message, (
        "the loud error must name the offending world / file so the config "
        f"mistake is locatable; got: {message!r}"
    )


def test_loader_accepts_well_formed_npcs_yaml(minimal_pack_factory: Any, tmp_path: Path) -> None:
    """Positive control: a well-formed ``npcs:`` roster still loads cleanly — the
    fail-loud guard must be targeted at the malformed shape, not a blanket
    rejection of npcs.yaml."""
    pack = minimal_pack_factory(tmp_path)
    world_dir = pack.path / "worlds" / "flickering_reach"
    (world_dir / "npcs.yaml").write_text(
        'npcs:\n  - id: canon_boss\n    name: "Canon Boss"\n    role: "the warlord"\n',
        encoding="utf-8",
    )

    loaded = load_genre_pack(pack.path)
    names = {n.name for n in loaded.worlds["flickering_reach"].authored_npcs}
    assert names == {"Canon Boss"}, (
        f"a conforming npcs: roster must load its entries verbatim; got {sorted(names)}"
    )


def test_loader_fails_loud_on_null_npcs_value(minimal_pack_factory: Any, tmp_path: Path) -> None:
    """A present `npcs:` key with a NULL value (bare `npcs:` / wrong-indent typo)
    must raise a LOUD, world-named error — not silently load an empty roster.

    `yaml.safe_load("npcs:")` → `{"npcs": None}`: the key IS present (so the
    missing-key guard passes), and `None or []` collapses to `[]` — the exact
    silent-empty-roster the story exists to eliminate (No Silent Fallbacks),
    wearing a different hat. Only an EXPLICIT `npcs: []` is the sanctioned empty
    roster; a null value is a malformed file. RED today (no raise); GREEN once the
    loader validates the `npcs:` value.
    """
    pack = minimal_pack_factory(tmp_path)
    world_dir = pack.path / "worlds" / "flickering_reach"
    # `npcs:` with nothing under it → parses to {"npcs": None}.
    (world_dir / "npcs.yaml").write_text("npcs:\n", encoding="utf-8")

    with pytest.raises(Exception) as exc_info:  # noqa: PT011 - Dev picks the exact error type
        load_genre_pack(pack.path)

    message = str(exc_info.value)
    assert "flickering_reach" in message or "npcs.yaml" in message, (
        "a null `npcs:` value must fail loud naming the world/file, never silently "
        f"yield an empty roster; got: {message!r}"
    )


def test_loader_fails_loud_on_non_list_npcs_value(
    minimal_pack_factory: Any, tmp_path: Path
) -> None:
    """A present `npcs:` mapped to a NON-LIST (a mapping/scalar — e.g. entries
    indented as a dict by mistake) must raise a LOUD, WORLD-NAMED error.

    Today such a value is truthy, so `… or []` passes it through and
    `AuthoredNpc.model_validate(n) for n in <mapping>` blows up with a bare
    pydantic/TypeError that names neither the file nor the world — defeating the
    story's "world-named error" requirement. RED today (raises, but anonymous);
    GREEN once the loader asserts the value is a list and raises a world-named
    `GenreLoadError`.
    """
    pack = minimal_pack_factory(tmp_path)
    world_dir = pack.path / "worlds" / "flickering_reach"
    # `npcs:` as a MAPPING instead of a list (a plausible indent mistake).
    (world_dir / "npcs.yaml").write_text(
        'npcs:\n  canon_boss:\n    name: "Canon Boss"\n    role: "the warlord"\n',
        encoding="utf-8",
    )

    with pytest.raises(Exception) as exc_info:  # noqa: PT011 - Dev picks the exact error type
        load_genre_pack(pack.path)

    message = str(exc_info.value)
    assert "flickering_reach" in message or "npcs.yaml" in message, (
        "a non-list `npcs:` value must fail loud naming the world/file so the "
        f"config mistake is locatable; got: {message!r}"
    )


# ===========================================================================
# Group C — end-to-end regression: a loaded + preloaded canon name never fractures
# ===========================================================================


@requires_heavy_metal
def test_barsoom_canon_name_does_not_fracture_after_preload(otel_capture) -> None:
    """The headline regression, end-to-end through the production machinery.

    Load barsoom, run the real ``preload_authored_npcs`` into a fresh snapshot,
    then have the narrator cite canon "Salensus Oll". With the roster loaded and
    preloaded, the mention reconciles to the live roster (Step-1 ``npcs_hit``) —
    it must NOT mint a phantom culture-shuffled identity, and no
    ``npc.invented_name_routed`` / shuffle_fallback span may fire.

    RED today: ``world.authored_npcs`` is empty, so preload is a no-op, Salensus
    Oll is absent from ``snapshot.npcs``, the cite falls through to the Step-3
    generator, and the pool grows a phantom. GREEN once the content loads and
    preload seats the canon antagonist.
    """
    assert _HM_DIR is not None
    pack = load_genre_pack(_HM_DIR)
    world = pack.worlds["barsoom"]
    assert world.authored_npcs, "precondition (Group A): barsoom roster must load"
    assert "Salensus Oll" in {n.name for n in world.authored_npcs}

    snapshot = GameSnapshot(genre_slug="heavy_metal", world_slug="barsoom")
    preload_authored_npcs(snapshot, list(world.authored_npcs))

    assert any(n.core.name == "Salensus Oll" for n in snapshot.npcs), (
        "preload must seat the canon antagonist in the live roster from session start"
    )
    pool_before = [m.name for m in snapshot.npc_pool]

    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=[_mention("Salensus Oll", role="jeddak", pronouns="he/him")],
        turn_num=4,
        pack=pack,
        world="barsoom",
    )

    assert [m.name for m in snapshot.npc_pool] == pool_before, (
        "citing a preloaded canon NPC must reconcile to the roster, not mint a "
        f"phantom pool identity; pool became {[m.name for m in snapshot.npc_pool]}"
    )
    assert _attrs_for(otel_capture, ROUTED_SPAN) == [], (
        "no shuffle_fallback route may fire for a preloaded canon proper noun"
    )
    matches = [n for n in snapshot.npcs if n.core.name == "Salensus Oll"]
    assert len(matches) == 1, "the canon antagonist must remain a single identity"
    assert matches[0].last_seen_turn == 4, "the live-roster cite must be recorded (npcs_hit)"
