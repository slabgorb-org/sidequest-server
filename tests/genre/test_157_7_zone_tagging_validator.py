"""Story 157-7 — strict zone-tagging load validator (fail-loud, lands last).

The capstone of epic-157 (faction/zone-scoped content eligibility). The runtime
predicate ``is_eligible`` (157-2) is deliberately PERMISSIVE — untagged content
stays eligible so the engine could ship before content tagging. This story locks
the door: a STRICT load-time validator that, for any *zoned* world (a world with
at least one ``Region.controlled_by``), refuses to load if any pooled, home-less
authored item (bestiary entry / trope / seed-trope) is untagged or carries a
faction slug that is not a real ``controlled_by`` value in that world.

Per the design spec ``docs/superpowers/specs/2026-06-20-faction-zone-content-
eligibility-design.md`` §"Strict load validator + error handling" and the project
"No Silent Fallbacks" doctrine: a missing/typo'd tag must be a LOUD load failure,
not a silent never-match at runtime.

═══════════════════════════════════════════════════════════════════════════════
THE CONTRACT THESE TESTS DEFINE (for Dev's green phase)
═══════════════════════════════════════════════════════════════════════════════
A new validator in ``sidequest/genre/loader.py``, matching the existing
``_validate_*(..., world_slug=...)`` convention (see ``_validate_authored_npc_
uniqueness`` etc.):

    _validate_zone_tagged_content(
        cartography: CartographyConfig | None,
        bestiary: Bestiary | None,
        tropes: list[TropeDefinition],     # RESOLVED — post trope-inheritance
        seed_tropes: list[SeedTrope],
        *,
        world_slug: str,
    ) -> None

Behaviour:
  • Not zoned (no region has ``controlled_by``) → return, validate NOTHING.
  • Zoned → every bestiary entry / trope / seed must have non-empty ``factions``,
    and every faction value must be ``"*"`` OR a real ``controlled_by`` slug in
    this world's cartography.
  • Any violation → raise ``GenreLoadError`` whose message NAMES the offending
    item id(s) and the ``world_slug``. ALL offenders are collected (not just the
    first).
  • Per rejected item, emit a persisted ``zone_eligibility.validator_failure``
    span (the GM-panel lie-detector) BEFORE raising, via the same
    ``with Span.open(SPAN, {...}): pass`` idiom Seam 4 uses in ``game/seed_deck.py``.
  • Wired into ``_load_single_world`` AFTER trope inheritance resolves, skipped
    for unzoned worlds.
  • Exempt (NOT validated): authored cartography NPCs (zone is region-derived)
    and runtime walk-ons (don't exist at load) — only the THREE pooled types.

RED until green adds ``_validate_zone_tagged_content`` and the
``SPAN_ZONE_ELIGIBILITY_VALIDATOR_FAILURE`` span constant. The module-level
imports below are the first thing that fails.

SEQUENCING (real-pack proof): the four already-tagged zoned worlds (gulliver,
oz, wonderland, the_circuit — 157-5/6) plus the three tagged by story 157-8
(perseus_cloud, coyote_star, glenross — sidequest-content#485) must ALL load
clean. The space_opera / tea_and_murder real-pack assertions require #485 present
in the content tree; that PR lands before this story's validator merges.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

# RED: these two symbols do not exist yet — green creates them.
from sidequest.genre.loader import (
    GenreLoadError,
    _validate_zone_tagged_content,
    load_genre_pack,
)
from sidequest.genre.models.bestiary import Bestiary, BestiaryEntry
from sidequest.genre.models.tropes import SeedTrope, TropeDefinition
from sidequest.genre.models.world import CartographyConfig, Region
from sidequest.protocol.models import LocationEntity, LocationEntityBinding
from sidequest.telemetry.spans.zone_eligibility import (
    SPAN_ZONE_ELIGIBILITY_VALIDATOR_FAILURE,
)

# Real ``controlled_by`` slugs used as the "valid" zone set in synthetic worlds.
AKKAD = "house_akkad"
THAR = "regency_of_thar"
HOUYHNHNM = "the_houyhnhnm_assembly"
TYPO = "the_houyhnhm_assembly"  # one 'n' dropped — the silent-never-match bug
STAR = "*"

CONTENT = Path(__file__).resolve().parents[3] / "sidequest-content/genre_packs"


# ═══════════════════════════════════════════════════════════════════════════
# otel_capture — tests/genre has no conftest fixture for this, so mirror the
# server conftest one (in-memory exporter on the global TracerProvider).
# ═══════════════════════════════════════════════════════════════════════════
@pytest.fixture
def otel_capture() -> Iterator:
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    provider._active_span_processor._span_processors = ()  # type: ignore[attr-defined]

    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


# ═══════════════════════════════════════════════════════════════════════════
# Builders
# ═══════════════════════════════════════════════════════════════════════════
def _region(controlled_by: str | None = None, *, entities: list | None = None) -> Region:
    return Region(
        name="R",
        summary="s",
        description="d",
        controlled_by=controlled_by,
        entities=entities or [],
    )


def _cartography(*controlled_bys: str | None, entities: list | None = None) -> CartographyConfig:
    """A cartography with one region per arg. ``entities`` (if given) is attached
    to the FIRST region — for the authored-NPC exemption test."""
    regions: dict[str, Region] = {}
    for i, cb in enumerate(controlled_bys):
        regions[f"r{i}"] = _region(cb, entities=entities if i == 0 else None)
    return CartographyConfig(regions=regions)


def _entry(entry_id: str, factions: list[str]) -> BestiaryEntry:
    return BestiaryEntry(
        id=entry_id,
        name=entry_id.replace("_", " ").title(),
        level=1,
        hp=1,
        armor_class=10,
        attack_bonus=0,
        factions=factions,
    )


def _bestiary(*entries: BestiaryEntry) -> Bestiary:
    return Bestiary(entries=list(entries))  # Bestiary requires a non-empty list


def _trope(trope_id: str, factions: list[str]) -> TropeDefinition:
    return TropeDefinition(id=trope_id, name=trope_id, factions=factions)


def _seed(seed_id: str, factions: list[str]) -> SeedTrope:
    return SeedTrope(id=seed_id, name=seed_id, lifespan_turns=5, factions=factions)


def _validator_failure_spans(otel_capture) -> list:
    return [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == SPAN_ZONE_ELIGIBILITY_VALIDATOR_FAILURE
    ]


# ═══════════════════════════════════════════════════════════════════════════
# AC8 / AC1 — unzoned worlds skip the validator entirely
# ═══════════════════════════════════════════════════════════════════════════
def test_unzoned_world_skips_validator_even_when_untagged() -> None:
    """The 11 single-zone worlds (no ``controlled_by`` anywhere) must keep loading
    unchanged: untagged pooled content is NOT a violation when the world is
    unzoned. This is the permissive short-circuit (AC-8)."""
    cart = _cartography(None, None)  # no region owns a faction → unzoned
    # All three pools untagged — would be rejected in a zoned world, allowed here.
    result = _validate_zone_tagged_content(
        cart,
        _bestiary(_entry("ambient_beast", [])),
        [_trope("a_mood", [])],
        [_seed("a_hook", [])],
        world_slug="unzoned_world",
    )
    assert result is None  # validator returns None on success, never raises here


def test_unzoned_with_no_cartography_skips() -> None:
    """A world with no cartography at all is trivially unzoned — no crash, no
    rejection (the validator must tolerate ``None``)."""
    result = _validate_zone_tagged_content(
        None,
        _bestiary(_entry("x", [])),
        [],
        [],
        world_slug="no_carto_world",
    )
    assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# AC2 — non-empty assertion: untagged pooled content in a zoned world fails loud
# ═══════════════════════════════════════════════════════════════════════════
def test_zoned_untagged_bestiary_raises() -> None:
    cart = _cartography(AKKAD, None)
    with pytest.raises(GenreLoadError) as exc:
        _validate_zone_tagged_content(
            cart,
            _bestiary(_entry("untagged_brute", [])),
            [],
            [],
            world_slug="zoned_world",
        )
    msg = str(exc.value)
    assert "untagged_brute" in msg, "error must name the offending item id"
    assert "zoned_world" in msg, "error must name the world"


def test_zoned_star_sentinel_passes() -> None:
    """``["*"]`` is a valid, conscious world-global tag — non-empty, sentinel.
    The validator must NOT over-reject it (this is exactly how story 157-8 tags
    perseus_cloud / coyote_star / glenross)."""
    cart = _cartography(AKKAD, None)
    result = _validate_zone_tagged_content(
        cart,
        _bestiary(_entry("ambient", [STAR])),
        [_trope("global_mood", [STAR])],
        [_seed("global_hook", [STAR])],
        world_slug="zoned_world",
    )
    assert result is None


def test_zoned_real_slug_passes() -> None:
    """A real ``controlled_by`` slug is eligible — the happy path must not raise."""
    cart = _cartography(AKKAD, THAR)
    result = _validate_zone_tagged_content(
        cart,
        _bestiary(_entry("akkadian_hound", [AKKAD]), _entry("thari_drone", [THAR])),
        [],
        [],
        world_slug="zoned_world",
    )
    assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# Story 162-3 — the bestiary ``generics:`` rows are pooled content too: a zoned
# world's untagged generic must fail loud, not silently vanish at seat time.
# ═══════════════════════════════════════════════════════════════════════════
def test_zoned_untagged_generic_row_raises() -> None:
    """162-3 folds ``bestiary.generics`` into the validated pool. A generic row
    with no ``factions`` in a zoned world is exactly the silent-never-match bug
    this validator exists to kill (the seater's last-resort Other would just
    never match its zone) — it must be a LOUD load failure."""
    cart = _cartography(AKKAD, None)
    bestiary = Bestiary(
        entries=[_entry("tagged_roster", [AKKAD])],
        generics=[_entry("untagged_generic", [])],
    )
    with pytest.raises(GenreLoadError) as exc:
        _validate_zone_tagged_content(cart, bestiary, [], [], world_slug="zoned_world")
    msg = str(exc.value)
    assert "untagged_generic" in msg, "error must name the offending generic row id"
    assert "zoned_world" in msg


def test_zoned_tagged_generic_row_passes() -> None:
    """A ``["*"]``-tagged generic row (exactly how coyote_star tags its generics)
    is eligible — the validator must not over-reject the real shipped content."""
    cart = _cartography(AKKAD, None)
    bestiary = Bestiary(
        entries=[_entry("tagged_roster", [AKKAD])],
        generics=[_entry("global_generic", [STAR])],
    )
    result = _validate_zone_tagged_content(cart, bestiary, [], [], world_slug="zoned_world")
    assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# AC3 — referential check: a typo'd faction slug is a LOUD failure, not a
#        silent never-match at runtime
# ═══════════════════════════════════════════════════════════════════════════
def test_zoned_typod_faction_raises() -> None:
    """``the_houyhnhm_assembly`` (one 'n' dropped) is not a real ``controlled_by``
    slug. Left to the runtime predicate it would silently never match and the
    content would just vanish; the validator turns it into a load failure."""
    cart = _cartography(HOUYHNHNM, None)
    with pytest.raises(GenreLoadError) as exc:
        _validate_zone_tagged_content(
            cart,
            _bestiary(_entry("yahoo_brute", [TYPO])),
            [],
            [],
            world_slug="gulliver",
        )
    assert TYPO in str(exc.value), "error must name the offending typo'd slug"


def test_zoned_mixed_valid_and_typo_still_raises() -> None:
    """One good slug does not excuse a second bad one on the same item."""
    cart = _cartography(HOUYHNHNM, None)
    with pytest.raises(GenreLoadError):
        _validate_zone_tagged_content(
            cart,
            _bestiary(_entry("two_tag_beast", [HOUYHNHNM, TYPO])),
            [],
            [],
            world_slug="gulliver",
        )


# ═══════════════════════════════════════════════════════════════════════════
# AC4 — all three pools are validated (bestiary, trope, seed) for BOTH rules
# ═══════════════════════════════════════════════════════════════════════════
def test_zoned_untagged_trope_raises() -> None:
    cart = _cartography(AKKAD, None)
    with pytest.raises(GenreLoadError) as exc:
        _validate_zone_tagged_content(
            cart, None, [_trope("untagged_trope", [])], [], world_slug="zoned_world"
        )
    assert "untagged_trope" in str(exc.value)


def test_zoned_untagged_seed_raises() -> None:
    cart = _cartography(AKKAD, None)
    with pytest.raises(GenreLoadError) as exc:
        _validate_zone_tagged_content(
            cart, None, [], [_seed("untagged_seed", [])], world_slug="zoned_world"
        )
    assert "untagged_seed" in str(exc.value)


def test_zoned_typod_trope_faction_raises() -> None:
    cart = _cartography(HOUYHNHNM, None)
    with pytest.raises(GenreLoadError):
        _validate_zone_tagged_content(
            cart, None, [_trope("bad_trope", [TYPO])], [], world_slug="gulliver"
        )


def test_zoned_typod_seed_faction_raises() -> None:
    cart = _cartography(HOUYHNHNM, None)
    with pytest.raises(GenreLoadError):
        _validate_zone_tagged_content(
            cart, None, [], [_seed("bad_seed", [TYPO])], world_slug="gulliver"
        )


def test_all_offenders_named_across_pools() -> None:
    """All violations are collected and reported, not just the first — an author
    fixing one tag should see every remaining problem on the next load."""
    cart = _cartography(AKKAD, None)
    with pytest.raises(GenreLoadError) as exc:
        _validate_zone_tagged_content(
            cart,
            _bestiary(_entry("bad_beast", [])),
            [_trope("bad_trope", [])],
            [_seed("bad_seed", [])],
            world_slug="zoned_world",
        )
    msg = str(exc.value)
    assert "bad_beast" in msg
    assert "bad_trope" in msg
    assert "bad_seed" in msg


# ═══════════════════════════════════════════════════════════════════════════
# AC5 — OTEL lie-detector span + wiring into the live load path
# ═══════════════════════════════════════════════════════════════════════════
def test_validator_failure_emits_otel_span(otel_capture) -> None:
    """Per the OTEL Observability Principle, a rejection must emit a persisted
    ``zone_eligibility.validator_failure`` span so the GM panel can verify the
    validator engaged. The span fires BEFORE the raise (Span.open closes the
    span, then the error aborts)."""
    cart = _cartography(AKKAD, None)
    with pytest.raises(GenreLoadError):
        _validate_zone_tagged_content(
            cart,
            _bestiary(_entry("untagged_brute", [])),
            [],
            [],
            world_slug="zoned_world",
        )
    spans = _validator_failure_spans(otel_capture)
    assert len(spans) >= 1, "validator must emit a zone_eligibility.validator_failure span"
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("content_id") == "untagged_brute"
    assert attrs.get("world") == "zoned_world"
    # subsystem identifies the offending pool — latitude on creature/bestiary naming.
    assert attrs.get("subsystem") in {"bestiary", "creature"}


def test_no_span_when_zoned_world_is_clean(otel_capture) -> None:
    """A fully-tagged zoned world is not a rejection — no failure span fires."""
    cart = _cartography(AKKAD, None)
    _validate_zone_tagged_content(
        cart, _bestiary(_entry("ok", [STAR])), [], [], world_slug="zoned_world"
    )
    assert _validator_failure_spans(otel_capture) == []


def test_validator_is_wired_into_load_path(monkeypatch) -> None:
    """Existence is not enough (project doctrine: Verify Wiring, Not Just
    Existence). Spy on the validator and load a REAL zoned pack — it must be
    invoked for the zoned world."""
    import sidequest.genre.loader as loader_mod

    seen: list[str] = []
    original = loader_mod._validate_zone_tagged_content

    def _spy(*args, **kwargs):
        seen.append(kwargs.get("world_slug", ""))
        return original(*args, **kwargs)

    monkeypatch.setattr(loader_mod, "_validate_zone_tagged_content", _spy)
    load_genre_pack(CONTENT / "wry_whimsy")
    assert "gulliver" in seen, "validator must run during load of the zoned gulliver world"


def test_load_aborts_when_validator_rejects(monkeypatch) -> None:
    """A validator ``GenreLoadError`` must propagate out of ``load_genre_pack`` —
    proving the validator is on the load path and that a rejection truly stops a
    bad pack from loading."""
    import sidequest.genre.loader as loader_mod

    def _boom(*args, **kwargs):
        if kwargs.get("world_slug") == "gulliver":
            raise GenreLoadError(path="synthetic", detail="SENTINEL_VALIDATOR_REJECTION")

    monkeypatch.setattr(loader_mod, "_validate_zone_tagged_content", _boom)
    with pytest.raises(GenreLoadError, match="SENTINEL_VALIDATOR_REJECTION"):
        load_genre_pack(CONTENT / "wry_whimsy")


# ═══════════════════════════════════════════════════════════════════════════
# AC6 — real-pack proof: every pack with a zoned world loads CLEAN
#        (guards against over-rejection; proves post-inheritance ordering)
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize(
    "pack_name",
    [
        "wry_whimsy",  # gulliver, oz, wonderland (157-5 / 157-6)
        "road_warrior",  # the_circuit (157-6)
        "space_opera",  # perseus_cloud, coyote_star (157-8 / content#485)
        "tea_and_murder",  # glenross (157-8 / content#485)
    ],
)
def test_real_zoned_packs_load_clean(pack_name: str) -> None:
    """All seven zoned worlds are fully tagged, so every pack containing one must
    load without ``GenreLoadError`` once the validator is wired.

    Also the post-inheritance ordering proof: gulliver's ``the_petty_holy_war``
    trope ``extends`` a genre parent and only carries its ``[the_lilliput_court]``
    scope after ``resolve_trope_inheritance`` merges it. A validator that ran
    BEFORE inheritance would see empty factions and wrongly reject gulliver — so
    a clean wry_whimsy load proves the validator runs on RESOLVED tropes.

    NOTE: space_opera / tea_and_murder require content#485 (story 157-8) present
    in the content tree — that PR lands before this validator merges.
    """
    pack_dir = CONTENT / pack_name
    assert pack_dir.is_dir(), f"missing content pack dir: {pack_dir}"
    pack = load_genre_pack(pack_dir)  # must not raise GenreLoadError
    assert pack.worlds, f"{pack_name} loaded no worlds"


# ═══════════════════════════════════════════════════════════════════════════
# AC7 — exemptions: authored cartography NPCs are zone-derived, NOT validated
# ═══════════════════════════════════════════════════════════════════════════
def test_authored_npc_in_zoned_region_is_not_validated() -> None:
    """An authored NPC lives in a region's ``entities`` manifest and carries NO
    ``factions`` field at all (its zone is the region's ``controlled_by``). The
    validator must NOT reach into cartography NPCs — only the three pooled types.
    A zoned world whose only "untagged" thing is an authored NPC loads clean."""
    npc = LocationEntity(
        id="the_emperor",
        label="The Emperor of Lilliput",
        tier="real_object",
        binding=LocationEntityBinding(kind="npc", ref="the_emperor"),
    )
    cart = _cartography(AKKAD, None, entities=[npc])
    # Pools are all correctly tagged; only the NPC is "untagged" (and exempt).
    result = _validate_zone_tagged_content(
        cart,
        _bestiary(_entry("akkadian_hound", [AKKAD])),
        [_trope("global_mood", [STAR])],
        [_seed("global_hook", [STAR])],
        world_slug="zoned_world",
    )
    assert result is None  # NPC ignored → no rejection
