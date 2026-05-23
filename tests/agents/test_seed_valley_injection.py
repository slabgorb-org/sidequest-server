"""RED-phase tests for Story 22-3 — VALLEY-zone narrator injection.

Contract under test (ACs 4, 5, 6, 7):

* **AC4 — Active seeds render in VALLEY.** When the snapshot carries
  ``active_seeds``, ``build_narrator_prompt`` registers exactly one
  Valley-zone ``PromptSection`` whose content surfaces each active
  seed's ``name``, ``description``, ``flavor_tags``, ``delivery_hints``,
  and ``narrative_hint`` so the narrator can retroactively weave the
  seed into the macro arc (ADR-009 VALLEY zone, ADR-014 baited-hook).
* **AC5 — Ghosts render as Faded.** When the snapshot carries
  ``seed_ghosts``, the same section (or an adjacent Faded subsection)
  surfaces each ghost's ``name`` and is clearly tagged so the narrator
  treats them as cross-session callbacks, NOT as live hooks. The
  ghost's ``narrative_hint`` is NOT surfaced (the bait is gone — the
  memory remains, but the resolution guidance does not).
* **AC6 — Stable (cached) block is byte-identical across seed-state
  drift.** Two builds that differ only in ``active_seeds`` must produce
  byte-identical Primacy+Early text. Seeds live in Valley by design
  (ADR-101 Phase D Task 6 cache layout); a regression that leaks seed
  prose into the cached block invalidates the 1h breakpoint (stories
  60-3 / 60-4 cache-rebate work).
* **AC7 — OTEL spans fire for the subsystem.** Per the project CLAUDE.md
  OTEL Observability Principle, every backend fix MUST add OTEL watcher
  events. The seed-context renderer fires a span on every prompt build
  carrying ``active_count`` and ``ghost_count`` so the GM panel
  (Sebastien's mechanic-first lie-detector view) can prove injection
  engaged. This is the prereq for 22-4 (panel surfacing).

Test strategy: drive ``Orchestrator.build_narrator_prompt`` end-to-end
with a real ``Orchestrator`` and a synthetic ``TurnContext`` that
carries the relevant ``snapshot`` and ``pack``. Assert on the live
``PromptRegistry`` contents — *not* by regex-matching production source
(server CLAUDE.md "No Source-Text Wiring Tests" rule). Where the seed
content depends on a pack lookup, we pass a duck-typed pack with
``seed_tropes`` populated so the renderer can resolve description /
narrative_hint by id without depending on disk YAML.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from sidequest.agents.orchestrator import Orchestrator, TurnContext
from sidequest.agents.prompt_framework.core import PromptRegistry
from sidequest.agents.prompt_framework.types import AttentionZone, SectionCategory
from sidequest.game.session import GameSnapshot, SeedGhost, SeedState
from sidequest.genre.models.tropes import SeedTrope

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class _DuckPack:
    """Minimal pack stand-in: only ``seed_tropes`` is read by the renderer.

    Matches the duck-typing the existing ``tick_tropes`` engine uses
    (it reads only ``pack.tropes``). When the renderer needs a seed's
    full prose (description + narrative_hint) it looks the id up in
    ``seed_tropes``. Tests that only need ghost rendering can pass an
    empty list.
    """

    seed_tropes: list[SeedTrope]


def _seedtrope(seed_id: str, **overrides) -> SeedTrope:
    defaults = dict(
        id=seed_id,
        name=f"Seed {seed_id}",
        description=f"Authored prose for {seed_id} — distinctive marker.",
        flavor_tags=["mystery", "household"],
        lifespan_turns=8,
        delivery_hints=[
            f"hint-alpha-{seed_id}",
            f"hint-beta-{seed_id}",
        ],
        narrative_hint=f"retroactive-connection-{seed_id}",
    )
    defaults.update(overrides)
    return SeedTrope(**defaults)


def _active_state(seed_id: str, activated_at: int = 1) -> SeedState:
    return SeedState(
        id=seed_id,
        name=f"Seed {seed_id}",
        activated_at_turn=activated_at,
        flavor_tags=["mystery", "household"],
        lifespan_turns=8,
        delivery_hints=[
            f"hint-alpha-{seed_id}",
            f"hint-beta-{seed_id}",
        ],
    )


def _ghost(seed_id: str, expired_at: int = 10) -> SeedGhost:
    return SeedGhost(
        id=seed_id,
        name=f"Ghost {seed_id}",
        expired_at_turn=expired_at,
        delivery_hints=[f"faded-hint-{seed_id}"],
    )


def _turn_context(
    *,
    snapshot: GameSnapshot,
    pack: _DuckPack,
    turn_number: int = 3,
) -> TurnContext:
    """A TurnContext carrying the snapshot + pack the renderer needs.

    Fields tracked: ``snapshot`` (snapshot reference, used by the existing
    time-skip path at orchestrator.py:1886 — we ride the same seam),
    ``pack`` (Any-typed Pack reference per orchestrator.py:606 — the
    seed renderer looks up SeedTrope-by-id here), and ``turn_number``
    (mid-session, post-opening so the turn-0 opening-scene path is
    skipped).
    """
    return TurnContext(
        character_name="Kael",
        genre="tea_and_murder",
        turn_number=turn_number,
        snapshot=snapshot,
        pack=pack,
    )


def _make_orchestrator() -> Orchestrator:
    """Default Orchestrator — legacy ClaudeClient backend. Sufficient
    for prompt-zone assertions (we are not driving the SDK turn loop;
    only ``build_narrator_prompt``, which is backend-agnostic for the
    Valley registrations under test)."""
    return Orchestrator()


def _seed_sections(registry: PromptRegistry, agent_name: str) -> list:
    """All Valley-zone sections whose name contains 'seed' (case-
    insensitive). The renderer may emit one combined section or a
    pair (actives + ghosts); the contract is the *zone* and the
    *content*, not the exact section count or name."""
    out = []
    for section in registry.registry(agent_name):
        if section.zone != AttentionZone.Valley:
            continue
        if "seed" in section.name.lower():
            out.append(section)
    return out


def _combined_seed_content(registry: PromptRegistry, agent_name: str) -> str:
    return "\n\n".join(s.content for s in _seed_sections(registry, agent_name))


# ---------------------------------------------------------------------------
# AC4 — Active seeds render in VALLEY
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_active_seed_registers_valley_section():
    """Baseline wiring assertion: a snapshot with one active seed
    produces at least one Valley-zone section that names the seed.
    If this fails the renderer is not wired at all."""
    snap = GameSnapshot(genre_slug="tea_and_murder", world_slug="glenross")
    snap.active_seeds = [_active_state("alpha")]
    pack = _DuckPack(seed_tropes=[_seedtrope("alpha")])

    orch = _make_orchestrator()
    _, registry = await orch.build_narrator_prompt(
        "I look around the parlour.",
        _turn_context(snapshot=snap, pack=pack),
    )

    sections = _seed_sections(registry, orch._narrator.name())
    assert sections, (
        "Expected at least one Valley-zone section whose name contains "
        "'seed' when snapshot.active_seeds is non-empty. None registered — "
        "the renderer is not wired into build_narrator_prompt. Look at "
        "the magic_context registration site (orchestrator.py ~1872) for "
        "the pattern: a context-builder helper + conditional "
        "registry.register_section call."
    )


@pytest.mark.asyncio
async def test_active_seed_section_lives_in_valley_zone():
    """Seeds belong in Valley — NOT Primacy/Early (would invalidate the
    1h cache breakpoint) and NOT Recency (recency is reserved for
    per-turn guardrails per ADR-009 and ADR-111).
    """
    snap = GameSnapshot(genre_slug="x", world_slug="y")
    snap.active_seeds = [_active_state("alpha")]
    pack = _DuckPack(seed_tropes=[_seedtrope("alpha")])

    orch = _make_orchestrator()
    _, registry = await orch.build_narrator_prompt(
        "act", _turn_context(snapshot=snap, pack=pack)
    )

    sections = _seed_sections(registry, orch._narrator.name())
    assert sections
    for s in sections:
        assert s.zone == AttentionZone.Valley, (
            f"Seed section {s.name!r} is in zone {s.zone} — must be "
            "Valley. Primacy/Early would invalidate the cached stable "
            "block; Late/Recency would compete with per-turn guardrails."
        )


@pytest.mark.asyncio
async def test_active_seed_section_categorized_as_state():
    """Seeds are world-state context, not Guardrail prose. Categorizing
    as State keeps them out of the guardrail-prose migration paths
    (ADR-111/112)."""
    snap = GameSnapshot(genre_slug="x", world_slug="y")
    snap.active_seeds = [_active_state("alpha")]
    pack = _DuckPack(seed_tropes=[_seedtrope("alpha")])

    orch = _make_orchestrator()
    _, registry = await orch.build_narrator_prompt(
        "act", _turn_context(snapshot=snap, pack=pack)
    )

    sections = _seed_sections(registry, orch._narrator.name())
    for s in sections:
        assert s.category == SectionCategory.State, (
            f"Seed section {s.name!r} category is {s.category} — must "
            "be State. (Mirrors world_context, game_state, magic_context, "
            "active_tropes — all State-categorized Valley contributors.)"
        )


@pytest.mark.asyncio
async def test_active_seed_surfaces_authored_prose_fields():
    """The renderer must surface name, description, flavor_tags,
    delivery_hints, and narrative_hint so the narrator has both the
    *what* (description + tags) and the *how* (delivery_hints — where
    in the world the seed shows up — and narrative_hint — the
    retroactive-connection guidance authored in 22-2).

    These are the five fields the SOUL "baited hook" doctrine relies on.
    Dropping any one of them strips information the narrator needs to
    make the seed land.
    """
    snap = GameSnapshot(genre_slug="x", world_slug="y")
    snap.active_seeds = [_active_state("alpha", activated_at=2)]
    pack = _DuckPack(seed_tropes=[_seedtrope("alpha")])

    orch = _make_orchestrator()
    _, registry = await orch.build_narrator_prompt(
        "act", _turn_context(snapshot=snap, pack=pack)
    )

    content = _combined_seed_content(registry, orch._narrator.name())
    assert content, "No Valley seed content rendered."

    # name + description from SeedTrope
    assert "Seed alpha" in content, "Seed name must render"
    assert "Authored prose for alpha" in content, "Seed description must render"
    # flavor_tags
    assert "mystery" in content and "household" in content, (
        "Flavor tags must render — they signal tone."
    )
    # delivery_hints
    assert "hint-alpha-alpha" in content, "Delivery hints must render"
    # narrative_hint
    assert "retroactive-connection-alpha" in content, (
        "narrative_hint must render — this is the 22-2 retroactive-"
        "connection guidance that lets the narrator wire the seed to "
        "the macro arc. Dropping it neuters the seed."
    )


@pytest.mark.asyncio
async def test_multiple_active_seeds_all_render():
    """Two actives → both surface, with each seed's distinctive marker
    string present. Catches an implementation that renders only the
    first entry in the list."""
    snap = GameSnapshot(genre_slug="x", world_slug="y")
    snap.active_seeds = [
        _active_state("alpha"),
        _active_state("bravo"),
    ]
    pack = _DuckPack(
        seed_tropes=[_seedtrope("alpha"), _seedtrope("bravo")]
    )

    orch = _make_orchestrator()
    _, registry = await orch.build_narrator_prompt(
        "act", _turn_context(snapshot=snap, pack=pack)
    )

    content = _combined_seed_content(registry, orch._narrator.name())
    assert "Authored prose for alpha" in content
    assert "Authored prose for bravo" in content
    assert "retroactive-connection-alpha" in content
    assert "retroactive-connection-bravo" in content


@pytest.mark.asyncio
async def test_no_active_seeds_no_ghosts_no_section_registered():
    """Zero-byte-leak: when both seed lists are empty (a fresh session
    pre-draw, or a pack with no seeds), no Valley seed section
    registers. Mirrors the discipline at
    ``orchestrator.py:1813`` (game_state only registers when
    state_summary is set) and the time_skip / npc_pool sections —
    every conditional Valley contributor opts out cleanly when its
    input is empty."""
    snap = GameSnapshot(genre_slug="x", world_slug="y")
    # both lists default to []
    pack = _DuckPack(seed_tropes=[])

    orch = _make_orchestrator()
    _, registry = await orch.build_narrator_prompt(
        "act", _turn_context(snapshot=snap, pack=pack)
    )

    sections = _seed_sections(registry, orch._narrator.name())
    assert not sections, (
        f"Expected zero seed sections when both active_seeds and "
        f"seed_ghosts are empty; got {[s.name for s in sections]}. "
        "Empty inputs must register nothing — the prompt zone stays "
        "quiet (zero-byte-leak)."
    )


# ---------------------------------------------------------------------------
# AC5 — Ghosts render as Faded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ghost_renders_with_faded_marker():
    """Ghosts must surface in the Valley block, clearly tagged so the
    narrator does not treat them as live hooks. We accept any of the
    obvious "Faded" / "Dormant" / "Expired" markers — the contract is
    that the narrator sees a tag, not a specific word."""
    snap = GameSnapshot(genre_slug="x", world_slug="y")
    snap.seed_ghosts = [_ghost("ancestor", expired_at=14)]
    pack = _DuckPack(seed_tropes=[])  # ghosts don't need pack lookup

    orch = _make_orchestrator()
    _, registry = await orch.build_narrator_prompt(
        "act", _turn_context(snapshot=snap, pack=pack)
    )

    content = _combined_seed_content(registry, orch._narrator.name())
    assert content, "Ghost-only snapshot still must produce a Valley section"
    assert "Ghost ancestor" in content, "Ghost name must render"

    lowered = content.lower()
    assert any(marker in lowered for marker in ("faded", "dormant", "expired")), (
        "Ghost section must carry an unmistakable Faded/Dormant/Expired "
        "marker so the narrator treats it as cross-session callback, "
        "not as a live hook. Without the tag the LLM will read ghosts "
        "as another live thread and double-bait the prompt."
    )


@pytest.mark.asyncio
async def test_ghost_does_not_surface_narrative_hint():
    """The ghost record (SeedGhost) carries no ``narrative_hint`` — the
    retroactive-connection guidance is for live seeds only. The
    renderer MUST NOT look up the original SeedTrope to inject the
    narrative_hint for an expired seed; the bait is gone. The ghost
    section should carry name + (optionally) the original delivery_hints
    so callbacks have sensory anchors, and nothing else.

    Catches an over-eager implementation that uniformly renders both
    actives and ghosts via the same lookup helper and accidentally
    surfaces hint guidance for an expired seed.
    """
    snap = GameSnapshot(genre_slug="x", world_slug="y")
    snap.seed_ghosts = [_ghost("ancestor")]
    # The renderer might still be given the original SeedTrope (its
    # author kept it in seed_tropes.yaml). Verify the renderer is
    # disciplined about which fields it consumes per-state-type.
    pack = _DuckPack(seed_tropes=[_seedtrope("ancestor")])

    orch = _make_orchestrator()
    _, registry = await orch.build_narrator_prompt(
        "act", _turn_context(snapshot=snap, pack=pack)
    )

    content = _combined_seed_content(registry, orch._narrator.name())
    assert "Ghost ancestor" in content, "Ghost name should render"
    assert "retroactive-connection-ancestor" not in content, (
        "Ghost section leaked the original seed's narrative_hint — the "
        "bait is gone; the hint must NOT surface for expired seeds. "
        "The renderer is sharing a lookup helper across active and "
        "ghost rendering paths without distinguishing them. Split the "
        "renderers (or branch on state type) so SeedGhost carries only "
        "name + delivery_hints into prose."
    )


@pytest.mark.asyncio
async def test_seed_context_wrapper_tag_is_balanced_for_ghost_only_render():
    """Regression: the seed-context wrapper tag must open and close
    exactly once on every produced block, regardless of which state
    is non-empty. A spec-check spotted a malformed render path where
    ``</seed-context>`` was appended without a matching opening tag
    on ghost-only state — XML-unbalanced prose confuses the narrator
    when it tries to bracket the section in its own output.
    """
    snap = GameSnapshot(genre_slug="x", world_slug="y")
    snap.seed_ghosts = [_ghost("ancestor")]
    pack = _DuckPack(seed_tropes=[])

    orch = _make_orchestrator()
    _, registry = await orch.build_narrator_prompt(
        "act", _turn_context(snapshot=snap, pack=pack)
    )

    content = _combined_seed_content(registry, orch._narrator.name())
    assert content.count("<seed-context>") == 1, (
        f"Expected exactly one <seed-context> opener; got "
        f"{content.count('<seed-context>')}. Block:\n{content}"
    )
    assert content.count("</seed-context>") == 1, (
        f"Expected exactly one </seed-context> closer; got "
        f"{content.count('</seed-context>')}. Block:\n{content}"
    )


@pytest.mark.asyncio
async def test_actives_and_ghosts_coexist_in_same_zone():
    """Mixed state — actives + ghosts in one snapshot. Both surface,
    each with their own discipline (actives full, ghosts Faded). Catches
    a renderer that handles one list and drops the other when both are
    non-empty."""
    snap = GameSnapshot(genre_slug="x", world_slug="y")
    snap.active_seeds = [_active_state("alpha")]
    snap.seed_ghosts = [_ghost("ancestor")]
    pack = _DuckPack(seed_tropes=[_seedtrope("alpha"), _seedtrope("ancestor")])

    orch = _make_orchestrator()
    _, registry = await orch.build_narrator_prompt(
        "act", _turn_context(snapshot=snap, pack=pack)
    )

    content = _combined_seed_content(registry, orch._narrator.name())
    # Active surfaces with full prose
    assert "Authored prose for alpha" in content
    assert "retroactive-connection-alpha" in content
    # Ghost surfaces with name + Faded marker, no narrative_hint
    assert "Ghost ancestor" in content
    lowered = content.lower()
    assert any(marker in lowered for marker in ("faded", "dormant", "expired"))
    assert "retroactive-connection-ancestor" not in content


# ---------------------------------------------------------------------------
# AC6 — Cached block byte-stability across seed-state drift
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_primacy_and_early_text_is_byte_identical_across_seed_drift():
    """Two builds that differ ONLY in active_seeds must produce
    byte-identical Primacy+Early prose. That is the cached prefix
    (orchestrator.py:3378-3385) and ADR-101 Phase D Task 6's cache
    layout. A regression that leaks seed state into the stable block
    breaks the 1h cache breakpoint (stories 60-2 / 60-3 / 60-4 rebate
    work)."""

    def _stable_text(registry: PromptRegistry, agent_name: str) -> str:
        sections = [
            s
            for s in registry.registry(agent_name)
            if s.zone in (AttentionZone.Primacy, AttentionZone.Early)
        ]
        # Order by registration insertion (registry preserves insertion
        # order via its internal list); join is what the SDK turn does.
        return "\n\n".join(s.content for s in sections)

    pack = _DuckPack(seed_tropes=[_seedtrope("alpha"), _seedtrope("bravo")])

    snap_one = GameSnapshot(genre_slug="x", world_slug="y")
    snap_one.active_seeds = [_active_state("alpha")]

    snap_two = GameSnapshot(genre_slug="x", world_slug="y")
    snap_two.active_seeds = [_active_state("bravo")]
    snap_two.seed_ghosts = [_ghost("gamma")]

    orch = _make_orchestrator()
    _, reg_one = await orch.build_narrator_prompt(
        "act", _turn_context(snapshot=snap_one, pack=pack)
    )
    _, reg_two = await orch.build_narrator_prompt(
        "act", _turn_context(snapshot=snap_two, pack=pack)
    )

    agent_name = orch._narrator.name()
    stable_one = _stable_text(reg_one, agent_name)
    stable_two = _stable_text(reg_two, agent_name)

    assert stable_one == stable_two, (
        "Primacy+Early text drifted between two builds that differ "
        "only in active_seeds / seed_ghosts. Seeds must register in "
        "Valley, never in the cached stable block. The 1h cache "
        "breakpoint (60-4) requires byte-identical Primacy+Early "
        "across per-turn drift."
    )


# ---------------------------------------------------------------------------
# AC7 — OTEL span fires for seed-context injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_injection_fires_otel_span(otel_capture):
    """The renderer must emit an OTEL span on every prompt build,
    carrying the active_count and ghost_count it just rendered. This
    is the prereq for 22-4 (GM-panel surfacing) and the OTEL
    Observability Principle's lie-detector contract: a renderer that
    does not emit a span cannot be distinguished from one that wings
    it.

    We accept any span whose name contains 'seed' (case-insensitive)
    so Dev can pick a name like ``narrator.seed_context``,
    ``seed_injection``, etc. The attributes are pinned: active_count
    and ghost_count as ints matching the snapshot's lists."""
    snap = GameSnapshot(genre_slug="x", world_slug="y")
    snap.active_seeds = [_active_state("alpha"), _active_state("bravo")]
    snap.seed_ghosts = [_ghost("ancestor")]
    pack = _DuckPack(
        seed_tropes=[_seedtrope("alpha"), _seedtrope("bravo"), _seedtrope("ancestor")]
    )

    orch = _make_orchestrator()
    await orch.build_narrator_prompt(
        "act", _turn_context(snapshot=snap, pack=pack)
    )

    spans = otel_capture.get_finished_spans()
    seed_spans = [s for s in spans if "seed" in s.name.lower()]
    assert seed_spans, (
        "No OTEL span containing 'seed' fired during prompt build. The "
        "GM panel cannot prove seed injection engaged without a span. "
        "Per CLAUDE.md OTEL Observability Principle: every subsystem "
        "decision must emit a span. Names referenced in plan: "
        "``narrator.seed_context_rendered`` (preferred), "
        "``seed_injection``, or sibling-of `narrator.trope_engine`."
    )

    # Pin the attributes Sebastien's GM panel will surface in 22-4.
    span = seed_spans[0]
    attrs = dict(span.attributes or {})
    assert attrs.get("active_count") == 2, (
        f"Expected active_count=2 on seed span; got attrs={attrs}. "
        "Sebastien's panel filters on these counts — wrong values "
        "either silence the panel or mis-attribute injection state."
    )
    assert attrs.get("ghost_count") == 1, (
        f"Expected ghost_count=1 on seed span; got attrs={attrs}"
    )


@pytest.mark.asyncio
async def test_seed_span_fires_even_with_empty_lists(otel_capture):
    """Counter-case: the span fires every prompt build, not only when
    seeds are present. An always-emit span is what lets the GM panel
    distinguish "no seeds this turn" from "renderer not invoked" —
    both produce zero content, only the always-emit span tells them
    apart. (Same discipline as the trope-tick span which fires every
    turn even on zero-progressing-trope ticks.)"""
    snap = GameSnapshot(genre_slug="x", world_slug="y")
    pack = _DuckPack(seed_tropes=[])

    orch = _make_orchestrator()
    await orch.build_narrator_prompt(
        "act", _turn_context(snapshot=snap, pack=pack)
    )

    spans = otel_capture.get_finished_spans()
    seed_spans = [s for s in spans if "seed" in s.name.lower()]
    assert seed_spans, (
        "No seed span fired on an empty-state prompt build. Span must "
        "fire every turn — silence-by-absence is indistinguishable "
        "from renderer-not-invoked. Sebastien needs the always-emit "
        "signal."
    )
    attrs = dict(seed_spans[0].attributes or {})
    assert attrs.get("active_count") == 0
    assert attrs.get("ghost_count") == 0
