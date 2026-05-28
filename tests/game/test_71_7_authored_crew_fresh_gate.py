"""Story 71-7 (RED): authored chassis crew must hydrate on a *real* fresh
session, and the skip path must never be silent.

Root cause (corrected during red phase — see TEA Delivery Findings): a real
freshly-materialized snapshot has ``turn_manager.interaction == 1`` (the
``TurnManager`` baseline), NOT 0. ``preload_authored_npcs`` gates on
``not state.characters AND turn_manager.interaction == 0`` — so the
``interaction == 0`` clause is *never* satisfiable in production and the crew
silently never loads. Every pre-existing test fabricated ``interaction=0``
(``MagicMock(interaction=0)`` / ``_StubTurnManager.interaction = 0``), a value
that does not occur in a real fresh session, which is exactly why they were
green while the live game (session_id=76) loaded zero authored NPCs.

These tests use the REAL baseline (``interaction == 1``) and assert the
observable contract from the story ACs:

- AC1/AC2: a fresh session (no real player character; default interaction) loads
  the authored crew with their seeded dispositions.
- AC3: chapter-authored NPCs already in ``npcs`` coexist with the loaded crew;
  no drops, no duplicates.
- AC4: ``npc.authored_loaded`` fires once per authored NPC on load; the skip
  path emits ``npc.authored_load_skipped`` carrying a ``reason`` (No Silent
  Fallbacks — CLAUDE.md).

Contract-level, not predicate-level: a fresh session is discriminated by the
absence of a real player character (the chargen seam appends the PC *after*
preload), not by a magic interaction number.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sidequest.game.session import GameSnapshot
from sidequest.game.world_materialization import preload_authored_npcs
from sidequest.genre.models.authored_npc import AuthoredNpc
from sidequest.telemetry.spans import SPAN_NPC_AUTHORED_LOADED, Span

# The skip span the fix must add. Asserted as a string literal (not an imported
# constant) so the test is RED today — the constant does not exist yet.
SKIP_SPAN_NAME = "npc.authored_load_skipped"


def _crew() -> list[AuthoredNpc]:
    """Stand-in for the Kestrel crew + matriarch, with the authored dispositions
    from ``coyote_star/npcs.yaml`` (captain 60, engineer 55, doc 50, cook 60,
    matriarch 0)."""
    return [
        AuthoredNpc(
            id="kestrel_captain",
            name="Wainu Moana-Teru",
            pronouns="she/her",
            role="captain",
            appearance="long-boned voidborn",
            initial_disposition=60,
        ),
        AuthoredNpc(
            id="kestrel_engineer",
            name="Hubo Dicia",
            pronouns="he/him",
            role="engineer",
            appearance="wiry free-miner stock",
            initial_disposition=55,
        ),
        AuthoredNpc(
            id="kestrel_doc",
            name="Kuna-Mikkaan",
            pronouns="they/them",
            role="ship's doctor",
            appearance="tall Tsveri, silicon integument",
            initial_disposition=50,
        ),
        AuthoredNpc(
            id="kestrel_cook",
            name="Kanga Moana-Teru",
            pronouns="she/her",
            role="cook",
            appearance="broad-shouldered voidborn",
            initial_disposition=60,
        ),
        AuthoredNpc(
            id="dura_mendes",
            name="Dura Mendes",
            pronouns="she/her",
            role="matriarch",
            appearance="weathered free-miner old-stock",
            initial_disposition=0,
        ),
    ]


def test_fresh_snapshot_default_interaction_is_one_not_zero() -> None:
    """Anchor the corrected root cause: a real fresh snapshot baselines at
    interaction 1. This is the value the gate must treat as fresh."""
    snap = GameSnapshot()
    assert snap.turn_manager.interaction == 1, (
        "Regression anchor: TurnManager baselines interaction at 1; a gate that "
        "requires interaction == 0 can never recognize a real fresh session."
    )
    assert snap.characters == []


def test_preload_loads_crew_on_real_fresh_snapshot() -> None:
    """AC1/AC2: real fresh snapshot (no PC yet, default interaction == 1) loads
    the authored crew with their seeded dispositions."""
    snap = GameSnapshot()
    assert snap.turn_manager.interaction == 1  # real baseline, not fabricated 0
    assert snap.characters == []

    crew = _crew()
    preload_authored_npcs(snap, crew)

    loaded = {n.core.name: n.disposition.value for n in snap.npcs}
    assert loaded == {
        "Wainu Moana-Teru": 60,
        "Hubo Dicia": 55,
        "Kuna-Mikkaan": 50,
        "Kanga Moana-Teru": 60,
        "Dura Mendes": 0,
    }, f"authored crew did not hydrate on a real fresh snapshot: got {loaded!r}"


def test_preload_emits_authored_loaded_span_per_crew_on_fresh() -> None:
    """AC4: one ``npc.authored_loaded`` span per authored NPC on a real fresh
    load (interaction == 1)."""
    snap = GameSnapshot()
    crew = _crew()

    with patch.object(Span, "open", wraps=Span.open) as span_open:
        preload_authored_npcs(snap, crew)

    loaded_spans = [c for c in span_open.call_args_list if c.args and c.args[0] == SPAN_NPC_AUTHORED_LOADED]
    assert len(loaded_spans) == len(crew), (
        f"expected {len(crew)} '{SPAN_NPC_AUTHORED_LOADED}' spans on fresh load, "
        f"got {len(loaded_spans)}"
    )


def test_chapter_npcs_and_crew_coexist_without_duplicates() -> None:
    """AC3: chapter-authored NPCs already present coexist with the loaded crew —
    no drops, no duplicate names."""
    snap = GameSnapshot()
    # Two pre-existing chapter NPCs (as a fresh materialization may carry).
    chapter_npc_names = ["Demiloslava Chop", "Commander Yarya Connorreter"]
    for nm in chapter_npc_names:
        stub = MagicMock()
        stub.core = MagicMock(name=nm)
        stub.core.name = nm
        snap.npcs.append(stub)

    crew = _crew()
    preload_authored_npcs(snap, crew)

    names = [n.core.name for n in snap.npcs]
    # Chapter NPCs retained.
    for nm in chapter_npc_names:
        assert nm in names, f"chapter NPC {nm!r} was dropped: {names!r}"
    # All crew added.
    for c in crew:
        assert c.name in names, f"crew member {c.name!r} not loaded: {names!r}"
    # No duplicates.
    assert len(names) == len(set(names)), f"duplicate NPC registration: {names!r}"
    assert len(names) == len(chapter_npc_names) + len(crew)


def test_resumed_session_still_skips_but_emits_reason_span() -> None:
    """AC4 + AC2 regression guard: a genuinely resumed session (real player
    character present, interaction past the baseline) must STILL skip the
    preload — but emit ``npc.authored_load_skipped`` with a ``reason`` so the
    skip is never silent (No Silent Fallbacks — CLAUDE.md)."""
    snap = GameSnapshot()
    snap.characters.append(MagicMock())  # a real PC — this is a resumed session
    snap.turn_manager.interaction = 7

    with patch.object(Span, "open", wraps=Span.open) as span_open:
        preload_authored_npcs(snap, _crew())

    # Still skips: no crew loaded.
    assert snap.npcs == [], "resumed session must not re-preload authored crew"

    # But the skip is observable: a skip span with a reason fired.
    skip_calls = [c for c in span_open.call_args_list if c.args and c.args[0] == SKIP_SPAN_NAME]
    assert skip_calls, (
        f"resumed-session skip emitted no '{SKIP_SPAN_NAME}' span — silent fallback "
        f"(CLAUDE.md forbids). Spans seen: {[c.args[0] for c in span_open.call_args_list if c.args]!r}"
    )
    attrs = skip_calls[0].args[1] if len(skip_calls[0].args) > 1 else (skip_calls[0].kwargs.get("attrs") or {})
    assert attrs.get("reason"), f"skip span must carry a non-empty 'reason' attribute; got {attrs!r}"
