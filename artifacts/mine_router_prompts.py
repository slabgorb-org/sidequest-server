"""Operator scratch — mine router A/B prompt rows from real Postgres saves.

Story 92-1 operator evidence step (consumed by the 92-2 gate). Produces the
``--prompts-jsonl`` input for ``scripts/router_ab_eval_cli.py --capture``:
one row per real player action, paired with the state summary built by the
PRODUCTION ``_build_state_summary`` over the save's snapshot + genre pack.

Methodology caveat (recorded in the go/no-go artifact): only the FINAL
snapshot per save is persisted (ADR-115 ``game_state`` upsert), so every
action in a save is paired with that save's final-state summary rather than
the round-accurate one. Actions are real, summaries are real-shaped and
genre-true; both eval backends see byte-identical inputs, so the agreement
metric is unaffected by the time shift.

Usage (from sidequest-server, M3 Ultra):
    SIDEQUEST_GENRE_PACKS=... SIDEQUEST_DATABASE_URL=... \
        uv run python artifacts/mine_router_prompts.py \
        --out artifacts/router_prompts.jsonl [--max-per-save 20]
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from pathlib import Path

from psycopg_pool import ConnectionPool

from sidequest.game.migrations import migrate_legacy_snapshot
from sidequest.game.session import GameSnapshot
from sidequest.genre.loader import load_genre_pack_cached
from sidequest.server.intent_router_pass import _build_state_summary

# Non-play sessions: test fixtures, render probes, cleanup probes. Excluding
# them is doctrine (tests must not point at live content — and vice versa:
# evidence must not point at test rows).
_EXCLUDE_SLUG_SUBSTRINGS = ("test-slug", "slug-cleanup", "render-", "fixture")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--max-per-save", type=int, default=20)
    args = parser.parse_args()

    db_url = os.environ["SIDEQUEST_DATABASE_URL"]  # KeyError = fail loud
    pool = ConnectionPool(db_url, min_size=1, max_size=2, open=True)

    rows_out: list[dict] = []
    skipped: list[str] = []
    with pool.connection() as conn:
        sessions = conn.execute(
            "SELECT s.session_id, s.session_slug FROM sessions s "
            "JOIN game_state g ON g.session_id = s.session_id "
            "WHERE EXISTS (SELECT 1 FROM narrative_log n "
            "  WHERE n.session_id = s.session_id AND n.author = 'player') "
            "ORDER BY s.session_id"
        ).fetchall()

        for session_id, slug in sessions:
            if any(sub in slug for sub in _EXCLUDE_SLUG_SUBSTRINGS):
                continue
            raw = conn.execute(
                "SELECT snapshot_json FROM game_state WHERE session_id = %s",
                (session_id,),
            ).fetchone()
            if raw is None or raw[0] is None:
                skipped.append(f"{slug}: no snapshot")
                continue
            try:
                data = json.loads(raw[0]) if isinstance(raw[0], str) else raw[0]
                snap = GameSnapshot.model_validate(migrate_legacy_snapshot(data))
            except Exception as exc:  # legacy saves are throwaway — log + skip LOUDLY
                skipped.append(f"{slug}: snapshot unparseable: {exc}")
                continue
            if not snap.genre_slug or not snap.world_slug:
                skipped.append(f"{slug}: missing genre/world slugs")
                continue
            try:
                pack = load_genre_pack_cached(snap.genre_slug)
            except Exception as exc:
                skipped.append(f"{slug}: pack load failed: {exc}")
                continue

            summary = _build_state_summary(snap, pack=pack)

            actions = conn.execute(
                "SELECT content, round_number, id FROM narrative_log "
                "WHERE session_id = %s AND author = 'player' ORDER BY id",
                (session_id,),
            ).fetchall()
            for content, round_number, row_id in actions[: args.max_per_save]:
                content = (content or "").strip()
                if not content:
                    continue
                rows_out.append(
                    {
                        "action": content,
                        "state_summary": summary,
                        "genre": snap.genre_slug,
                        "world": snap.world_slug,
                        "round_number": max(int(round_number or 0), 0),
                        "source_save": slug,
                        "event_seq": row_id,
                    }
                )

    # Deterministic shuffle so eval_corpus's prefix sampling
    # (captures[:sample_size]) draws a save/genre-diverse subset instead of
    # the oldest sessions. Seeded → re-mining is reproducible.
    random.Random(922).shuffle(rows_out)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for row in rows_out:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"mined {len(rows_out)} prompt rows from {len({r['source_save'] for r in rows_out})} saves -> {out}")
    if skipped:
        print(f"skipped {len(skipped)} sessions:", file=sys.stderr)
        for line in skipped:
            print(f"  - {line}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
