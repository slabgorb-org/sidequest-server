"""RED (story 90-1): pregen.seed_manual populates the encounters pool for
ruleset-module packs — the e2e wiring proof.

Today ``_generate_encounter`` swallows encountergen's ``sys.exit(1)`` ("has no
allowed_classes") into ``None`` and **silently skips** encounter seeding, so a
``ruleset: wwn`` world's Monster Manual ships 0 encounters (87-4 finding:
evropi 49 NPCs / 0 enc; long_foundry 39 / 0). Story 90-1 routes encountergen
through the pack's content bestiary (Option B, Keith 2026-06-05) so the pool
is populated — and a ruleset-module pack with no bestiary fails LOUD, never a
silent empty pool.

These are the wiring tests (server CLAUDE.md "Every Test Suite Needs a Wiring
Test"): real pack → real ``seed_manual`` → non-empty pool, asserted on
behavior + the ``pregen.seed_manual`` OTEL span, not source text.
"""

from __future__ import annotations

import random

import pytest

from sidequest.game.monster_manual import MonsterManual
from sidequest.server.dispatch.pregen import seed_manual
from sidequest.telemetry.spans.pregen import SPAN_PREGEN_SEED_MANUAL
from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path


def _require_pack(slug: str) -> None:
    try:
        find_pack_path(slug)
    except PackNotFound:
        pytest.skip(f"sidequest-content pack {slug!r} not on disk")


# ---------------------------------------------------------------------------
# AC3 — the encounters pool is populated for both heavy_metal worlds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("world", ["evropi", "long_foundry"])
def test_seed_manual_populates_encounters_for_wwn_world(world: str) -> None:
    """AC3: seeding a ``ruleset: wwn`` world yields a NON-EMPTY encounters
    pool with well-formed entries (named, tiered). Was 0/0 in 87-4."""
    _require_pack("heavy_metal")
    manual = MonsterManual(genre="heavy_metal", world=world)
    seed_manual(
        genre_packs_path=GENRE_PACKS_DIR,
        genre="heavy_metal",
        world=world,
        manual=manual,
        rng=random.Random(901),
    )
    assert len(manual.encounters) > 0, (
        f"heavy_metal/{world} encounters pool is EMPTY — encountergen must emit "
        "from the pack bestiary for ruleset-module packs (90-1 AC3); a silent "
        "empty pool is the bug this story retires"
    )


# ---------------------------------------------------------------------------
# AC4 — the seeding decision is OTEL-visible (GM-panel lie detector)
# ---------------------------------------------------------------------------


def test_seed_manual_span_reports_nonzero_encounters(otel_capture) -> None:
    """AC4: the ``pregen.seed_manual`` span must report ``encounters_after > 0``
    for a wwn world, so the GM panel can verify the pool was populated rather
    than silently skipped."""
    _require_pack("heavy_metal")
    manual = MonsterManual(genre="heavy_metal", world="evropi")
    seed_manual(
        genre_packs_path=GENRE_PACKS_DIR,
        genre="heavy_metal",
        world="evropi",
        manual=manual,
        rng=random.Random(901),
    )
    spans = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_PREGEN_SEED_MANUAL]
    assert spans, "pregen.seed_manual span must fire (it already does — wiring precondition)"
    encounters_after = spans[-1].attributes.get("encounters_after")
    assert isinstance(encounters_after, int) and encounters_after > 0, (
        f"span reports encounters_after={encounters_after!r} for heavy_metal/evropi — "
        "the seeding decision must be OTEL-visible AND non-empty (90-1 AC4)"
    )
