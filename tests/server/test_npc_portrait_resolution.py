"""Story 65-6 — world-level NPC portrait resolution on scene invocation.

Portraits are world-level FLAVOR (parity with POIs). When an NPC is invoked in
a turn, the scrapbook emitter attaches a world-scoped portrait_url IFF the NPC
matches a portrait_manifest entry for the current world. These tests pin the
behavior with a real fixture pack (test_genre/flickering_reach):

  - matched NPC  -> portrait_url == world-scoped asset URL (URL == on-disk slug)
  - unmatched NPC -> portrait_url is None

The slug contract is load-bearing: the server must derive the SAME slug the
render script writes (slugify_player_name == daemon CharacterCatalog._slugify_name
== generate_portrait_images._slugify_name). A mismatch means the resolved URL
404s. The cross-repo slug test pins that equality.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.genre.loader import load_genre_pack

_FIXTURE_PACKS = Path(__file__).resolve().parents[1] / "fixtures" / "packs"
_GENRE = "test_genre"
_WORLD = "flickering_reach"

# A real NPC from the fixture portrait_manifest.yaml.
_MANIFEST_NPC = "Odige Fuseborn"
_MANIFEST_NPC_SLUG = "odige_fuseborn"
# An NPC that is NOT in the manifest (ad-hoc, the common case).
_ADHOC_NPC = "Some Random Bystander"


@pytest.fixture(scope="module")
def pack():
    return load_genre_pack(_FIXTURE_PACKS / _GENRE)


def test_fixture_world_has_portrait_manifest(pack) -> None:
    """Sanity: the loader populated world.portrait_manifest from the fixture
    YAML — otherwise the resolver has nothing to match against."""
    world = pack.worlds.get(_WORLD)
    assert world is not None, f"fixture world {_WORLD!r} missing from {_GENRE}"
    names = {e.name for e in world.portrait_manifest}
    assert _MANIFEST_NPC in names, (
        f"expected {_MANIFEST_NPC!r} in portrait_manifest; got {sorted(names)}"
    )


def test_world_portrait_slugs_matches_render_filename_slug(pack) -> None:
    """The manifest slug set the resolver builds must contain the SAME slug the
    render script writes as <slug>.png."""
    from sidequest.server.emitters import _world_portrait_slugs

    slugs = _world_portrait_slugs(pack, _WORLD)
    assert _MANIFEST_NPC_SLUG in slugs


def test_matched_npc_resolves_world_scoped_portrait_url(pack) -> None:
    """An invoked NPC in the manifest gets a world-scoped portrait_url whose
    path is exactly genre_packs/<g>/worlds/<w>/assets/portraits/<slug>.png."""
    from sidequest.server.emitters import _resolve_npc_portrait_url

    url = _resolve_npc_portrait_url(
        pack=pack,
        genre_slug=_GENRE,
        world_slug=_WORLD,
        npc_name=_MANIFEST_NPC,
    )
    assert url is not None, "matched NPC must resolve a portrait_url"
    expected_rel = f"genre_packs/{_GENRE}/worlds/{_WORLD}/assets/portraits/{_MANIFEST_NPC_SLUG}.png"
    assert url.endswith(expected_rel), (
        f"portrait_url {url!r} does not end with the world-scoped asset path "
        f"{expected_rel!r} — the render script writes exactly this path, so a "
        f"mismatch is a 404."
    )


def test_unmatched_npc_resolves_none(pack) -> None:
    """An ad-hoc NPC (not in the manifest) gets no portrait_url."""
    from sidequest.server.emitters import _resolve_npc_portrait_url

    url = _resolve_npc_portrait_url(
        pack=pack,
        genre_slug=_GENRE,
        world_slug=_WORLD,
        npc_name=_ADHOC_NPC,
    )
    assert url is None, "ad-hoc NPC must not resolve a portrait_url"


def test_unbound_pack_resolves_none() -> None:
    """No pack / no world -> no portrait (observable not-found, not a crash)."""
    from sidequest.server.emitters import _resolve_npc_portrait_url, _world_portrait_slugs

    assert _world_portrait_slugs(None, _WORLD) == frozenset()
    assert _world_portrait_slugs(None, None) == frozenset()
    url = _resolve_npc_portrait_url(
        pack=None, genre_slug=_GENRE, world_slug=None, npc_name=_MANIFEST_NPC
    )
    assert url is None


def test_server_slug_equals_render_script_slug() -> None:
    """Cross-component slug contract (the 404 guard).

    The server's slugify_player_name MUST produce the same slug as the render
    script's _slugify_name for every NPC name — otherwise the resolved URL
    points at a filename that does not exist on R2.
    """
    import re
    import unicodedata

    from sidequest.server.utils import slugify_player_name

    def _render_script_slugify(name: str) -> str:
        # Mirror of scripts/render_common._slugify_name / daemon
        # CharacterCatalog._slugify_name. Pinned here so a drift on either side
        # fails this test rather than silently 404ing. Story 101-8: both sides
        # now NFKD-fold non-ASCII before the lower/whitespace/drop steps, so
        # "Big Léon" → "big_leon" (not "big_lon").
        folded = "".join(
            ch for ch in unicodedata.normalize("NFKD", name) if not unicodedata.combining(ch)
        )
        lowered = folded.strip().lower()
        collapsed = re.sub(r"\s+", "_", lowered)
        return re.sub(r"[^a-z0-9_-]", "", collapsed)

    cases = [
        "Odige Fuseborn",
        'Josephine "Josie" Delacroix',
        "Commissaire Renard",
        "Big Léon Marchetti",
        "  Trailing Spaces  ",
        # Story 101-8: diacritic-heavy case — folds to ASCII base letters on both
        # sides (the render-key == consumer-key contract for evropi/coyote_star).
        "Srárný Fyzioloniązka",
    ]
    for name in cases:
        assert slugify_player_name(name) == _render_script_slugify(name), (
            f"slug skew on {name!r}: server={slugify_player_name(name)!r} "
            f"render={_render_script_slugify(name)!r}"
        )


def test_resolved_portrait_emits_otel_span(pack) -> None:
    """OTEL principle: a resolved portrait emits the resolved span so the GM
    panel can confirm the lookup ran (not Claude improvising)."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from sidequest.server.emitters import _resolve_npc_portrait_url

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer(__name__)

    import sidequest.telemetry.spans as spans_mod

    original = spans_mod.tracer
    spans_mod.tracer = lambda: tracer
    try:
        _resolve_npc_portrait_url(
            pack=pack, genre_slug=_GENRE, world_slug=_WORLD, npc_name=_MANIFEST_NPC
        )
        _resolve_npc_portrait_url(
            pack=pack, genre_slug=_GENRE, world_slug=_WORLD, npc_name=_ADHOC_NPC
        )
    finally:
        spans_mod.tracer = original

    names = {s.name for s in exporter.get_finished_spans()}
    assert "scrapbook.npc_portrait_resolved" in names, (
        f"resolved portrait must emit the resolved span; got {names}"
    )
    assert "scrapbook.npc_portrait_not_found" in names, (
        f"unmatched NPC must emit the not_found span; got {names}"
    )
    # Ignore other incidental spans (asset_url_resolved fires inside resolve_asset_url).
    _ = trace  # keep import used


def test_npc_ref_model_carries_portrait_url() -> None:
    """The protocol model gained an optional portrait_url field (default None)
    and serializes it round-trip."""
    from sidequest.protocol.messages import ScrapbookEntryNpcRef

    # Default is None.
    bare = ScrapbookEntryNpcRef(name="Nobody")
    assert bare.portrait_url is None

    ref = ScrapbookEntryNpcRef(
        name=_MANIFEST_NPC,
        role="neutral",
        disposition="wary",
        portrait_url="https://cdn.slabgorb.com/genre_packs/x/worlds/y/assets/portraits/z.png",
    )
    dumped = ref.model_dump()
    assert dumped["portrait_url"].endswith("/assets/portraits/z.png")
    rebuilt = ScrapbookEntryNpcRef.model_validate(dumped)
    assert rebuilt.portrait_url == ref.portrait_url
