"""Wire tests for scrapbook coverage detection on save resume (Story 45-10).

The unit tests in ``test_scrapbook_coverage.py`` pin the helper's behavior
in isolation. This file pins the *wiring* — that the helper is actually
invoked from production resume paths. AC4 is explicit: "Both resume
paths (slug + legacy) wire the detector. A half-wired fix that only
patches the slug path leaves the legacy path silent — the test catches
it."

Resume path inventory (post-decomposition, 2026-04-28):

1. **Slug-keyed resume** — ``handlers/connect.py:226`` (``store.load()``),
   line 259 (``if saved is not None:``). Triggered when client provides
   a ``slug`` field (MP rooms or ADR-037 slug-keyed solo).

2. **Legacy non-slug resume** — ``handlers/connect.py:856`` (``store.load()``),
   line 888 (``if saved is not None:``). Triggered by genre/world/player
   triple saves without a slug.

The context document (sprint/context/context-story-45-10.md) referenced
session_handler.py:1610 / 2138 — those line numbers were pre-decomposition.
The two paths are still distinct, just relocated into ``handlers/connect.py``.
TEA logs this as a no-impact location update — both paths still need wiring.

Test strategy: lightweight static-source checks + import-graph checks.
A full-stack drive-the-WS-handler test would re-invent
``test_chargen_persist_and_play.py``'s setup at 5x the cost; the static
checks are sharp enough to catch the half-fix regression AC4 names.
"""

from __future__ import annotations

import json as _json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Static-source location of the two resume seams
# ---------------------------------------------------------------------------

_CONNECT_PY = Path(__file__).resolve().parents[2] / "sidequest" / "handlers" / "connect.py"


@pytest.fixture(scope="module")
def connect_source() -> str:
    return _CONNECT_PY.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def resume_branches(connect_source: str) -> dict[str, tuple[int, int]]:
    """Locate both ``if saved is not None:`` branches in ``connect.py``.

    Returns a dict {label: (start_line, end_line)} where end_line is the
    line of the matching ``else:`` (or end of branch) so wiring tests can
    look inside the right window.
    """
    branches: list[int] = []
    for i, line in enumerate(connect_source.splitlines(), start=1):
        if line.strip() == "if saved is not None:":
            branches.append(i)

    assert len(branches) >= 1, (
        f"Expected at least one ``if saved is not None:`` branch in "
        f"connect.py (slug-keyed). Found {len(branches)} "
        f"at lines {branches}. If the count drops, the resume topology "
        f"changed — re-locate the seams before fixing this test."
    )

    # Slug branch is the only branch (Story 45-26 removed legacy non-slug path).
    return {
        "slug": (branches[0], branches[0] + 200),
    }


# ---------------------------------------------------------------------------
# Import surface: connect.py must import the helper
# ---------------------------------------------------------------------------


class TestImportSurface:
    """The helper symbol must be importable from connect.py — without the
    import, the wire is dead. This test fails fast if the import line
    is missing / typo'd / commented out."""

    def test_connect_imports_detector(self, connect_source: str) -> None:
        # Accept either form — `from sidequest.game.scrapbook_coverage import
        # detect_scrapbook_coverage_gaps` or a module-import `from
        # sidequest.game import scrapbook_coverage`. Both wire correctly;
        # the call-site test below verifies one of them is reached.
        forms = [
            "from sidequest.game.scrapbook_coverage import",
            "from sidequest.game import scrapbook_coverage",
            "import sidequest.game.scrapbook_coverage",
        ]
        matches = [f for f in forms if f in connect_source]
        assert matches, (
            "connect.py must import the scrapbook_coverage helper. None of "
            "the accepted import forms found:\n  "
            + "\n  ".join(f"- {f!r}" for f in forms)
            + "\nWithout this import, the helper is referenced but never "
            "loaded — the wire is dead."
        )


# ---------------------------------------------------------------------------
# AC4 — both branches invoke the detector
# ---------------------------------------------------------------------------


class TestSlugResumeWiring:
    """AC4 (slug branch): the slug-keyed resume in ``connect.py`` must
    invoke ``detect_scrapbook_coverage_gaps`` after the snapshot is loaded
    and before the connect-ready cascade runs."""

    def test_slug_branch_calls_detector(self, connect_source: str, resume_branches: dict) -> None:
        start, end = resume_branches["slug"]
        snippet = "\n".join(connect_source.splitlines()[start - 1 : end])
        invocation_patterns = [
            "detect_scrapbook_coverage_gaps(",
            "scrapbook_coverage.detect_scrapbook_coverage_gaps(",
        ]
        matches = [p for p in invocation_patterns if p in snippet]
        assert matches, (
            "Slug-keyed resume branch (connect.py around line "
            f"{start}) must invoke the scrapbook coverage detector. "
            "Acceptable call patterns:\n  "
            + "\n  ".join(f"- {p!r}" for p in invocation_patterns)
            + "\nNone found in the 200-line window after `if saved is not "
            "None:`. Per AC4, a half-wired fix that only patches the legacy "
            "path leaves slug resumes silent — this is the assertion that "
            "blocks that regression."
        )

    def test_slug_branch_invokes_after_snapshot_load(
        self, connect_source: str, resume_branches: dict
    ) -> None:
        """Ordering invariant: detector call must come AFTER ``saved =
        store.load()`` succeeds and AFTER ``snapshot = saved.snapshot``.
        Calling it before snapshot is in scope means the helper sees stale
        state and the genre/world span attributes are wrong."""
        start, end = resume_branches["slug"]
        lines = connect_source.splitlines()[start - 1 : end]

        snapshot_line = next(
            (i for i, ln in enumerate(lines) if "snapshot = saved.snapshot" in ln),
            None,
        )
        detector_line = next(
            (i for i, ln in enumerate(lines) if "detect_scrapbook_coverage_gaps" in ln),
            None,
        )
        assert snapshot_line is not None, (
            "Couldn't locate `snapshot = saved.snapshot` in slug branch — "
            "either the assignment was renamed or the branch shape changed."
        )
        assert detector_line is not None, (
            "Detector call missing from slug branch — covered by sibling "
            "test_slug_branch_calls_detector but flagged here for ordering."
        )
        assert detector_line > snapshot_line, (
            f"Detector must be invoked AFTER snapshot is loaded (line "
            f"{snapshot_line} < {detector_line} required). Calling it on "
            f"a stale snapshot reads wrong genre/world for span attrs."
        )


class TestSlugResumeEndToEnd:
    """Drives a populated SqliteStore through the actual slug-resume code
    path and asserts the OTEL spans + watcher event fire. This is the
    proof-of-life test that catches regressions where the static-source
    asserts pass but production behavior breaks (e.g., import shadowed,
    helper monkeypatched away by a peer subsystem)."""

    @pytest.fixture
    def populated_slug_save(self, tmp_path):
        """Build a slug-keyed save with the Orin regression fixture: 29
        narrative rounds, 10 scrapbook rounds. Returns the slug + save dir
        so the test can drive a real connect through it."""
        from sidequest.game.persistence import (
            GameMode,
            SqliteStore,
            db_path_for_slug,
            upsert_game,
        )
        from sidequest.game.session import GameSnapshot, NarrativeEntry

        slug = "scrapbook-coverage-orin-fixture"
        genre = "test_genre"
        world = "flickering_reach"

        db = db_path_for_slug(tmp_path, slug)
        db.parent.mkdir(parents=True, exist_ok=True)
        store = SqliteStore(db)
        store.initialize()
        upsert_game(
            store,
            slug=slug,
            mode=GameMode.SOLO,
            genre_slug=genre,
            world_slug=world,
        )
        store.init_session(genre, world)

        # Snapshot with a character so chargen gate doesn't intercept.
        from sidequest.game.character import Character
        from sidequest.game.creature_core import CreatureCore, Inventory

        snap = GameSnapshot(genre_slug=genre, world_slug=world, location="Crypt Threshold")
        snap.characters = [
            Character(
                core=CreatureCore(
                    name="Orin",
                    description="Cleric",
                    personality="Steady",
                    inventory=Inventory(),
                ),
                char_class="Cleric",
                race="Human",
                backstory="A wandering cleric.",
            )
        ]
        # 29 narrative rounds — the bug-evidence shape.
        for r in range(1, 30):
            store.append_narrative(
                NarrativeEntry(
                    round=r,
                    author="narrator",
                    content=f"Round {r}.",
                    tags=[],
                )
            )
        store.save(snap)
        # 10 scrapbook rows for rounds 1-10.
        with store._conn:
            for r in range(1, 11):
                store._conn.execute(
                    "INSERT INTO scrapbook_entries "
                    "(turn_id, scene_title, scene_type, location, image_url, "
                    " narrative_excerpt, world_facts, npcs_present) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        r,
                        f"Scene {r}",
                        "exploration",
                        f"Location {r}",
                        None,
                        f"Round {r} excerpt.",
                        _json.dumps([]),
                        _json.dumps([]),
                    ),
                )
        store.close()
        return {"slug": slug, "save_dir": tmp_path, "genre": genre, "world": world}

    @pytest.mark.asyncio
    async def test_slug_resume_emits_coverage_evaluated_and_gap_spans(
        self, populated_slug_save, otel_capture
    ) -> None:
        """Drive an end-to-end slug-keyed resume against the Orin fixture
        and assert the two spans land. This is the wire test that catches
        the half-fix where the helper exists, the import is present, but
        the call site reads from the wrong store reference.

        Mirrors ``test_scrapbook_entry_wiring.py`` setup conventions —
        ``WebSocketSessionHandler(save_dir, genre_pack_search_paths)`` plus
        ``attach_room_context``, then ``handle_message`` against a wire
        ``SESSION_EVENT`` connect message.
        """
        try:
            from sidequest.game.scrapbook_coverage import (  # noqa: F401
                detect_scrapbook_coverage_gaps,
            )
        except ImportError:
            pytest.skip(
                "Helper module not yet implemented — covered by red-phase "
                "unit tests in test_scrapbook_coverage.py."
            )

        import asyncio
        from pathlib import Path

        from sidequest.protocol import GameMessage
        from sidequest.server.session_handler import WebSocketSessionHandler
        from sidequest.server.session_room import RoomRegistry

        save_dir: Path = populated_slug_save["save_dir"]
        slug: str = populated_slug_save["slug"]
        fixture_packs = Path(__file__).resolve().parents[1] / "fixtures" / "packs"

        handler = WebSocketSessionHandler(
            save_dir=save_dir,
            genre_pack_search_paths=[fixture_packs],
        )
        queue: asyncio.Queue[object] = asyncio.Queue()
        handler.attach_room_context(
            registry=RoomRegistry(),
            socket_id="sock-orin",
            out_queue=queue,
        )

        connect = GameMessage.model_validate(
            {
                "type": "SESSION_EVENT",
                "player_id": "orin-player-1",
                "payload": {
                    "event": "connect",
                    "game_slug": slug,
                    "last_seen_seq": 0,
                },
            }
        )

        await handler.handle_message(connect)

        names = [s.name for s in otel_capture.get_finished_spans()]
        assert "scrapbook.coverage_evaluated" in names, (
            f"slug-resume MUST emit scrapbook.coverage_evaluated. "
            f"Spans seen: {names!r}. If empty, the helper isn't being "
            f"invoked from the slug branch — wire is broken."
        )
        assert "scrapbook.coverage_gap_detected" in names, (
            f"slug-resume against the 29/10 Orin fixture MUST emit "
            f"scrapbook.coverage_gap_detected. Spans seen: {names!r}."
        )
