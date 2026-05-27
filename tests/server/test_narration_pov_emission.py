"""Wiring test — 3-PC MP session per-recipient POV emission (Story 49-8).

End-to-end proof that the new narration projection lands at the wire:

  - Carl's outbound queue receives "You plant a boot..." (2nd-person)
    because he is the anchor of his own action card.
  - Donut's queue receives "Carl plants a boot..." (3rd-person)
    unchanged for that same card.
  - Katia's queue receives "Carl plants a boot..." (3rd-person)
    unchanged for that same card.
  - All three players receive all three cards (no perception filtering
    in this story — that is ADR-028 follow-up).

This test exercises ``emit_event`` via ``WebSocketSessionHandler._emit_event``
just like ``test_perception_rewriter_wiring.py`` — pure dict payloads with
a fake message class so we can read the dict that landed on each queue.

RED until:
  (1) ``sidequest.server.visibility_classifier.classify_narration_visibility``
      exists,
  (2) ``sidequest.agents.pov_swap.swap_to_second_person`` exists,
  (3) the emit pipeline routes NARRATION through both — first to stamp
      the sidecar, then to swap text per-recipient at fan-out.

The sentinel here is the wire-level text on each player's queue. If a
recipient whose ``player_id_to_character[pid] == anchor_pc`` does NOT
see 2nd-person prose, the wiring is broken.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.event_log import EventLog
from sidequest.game.persistence import (
    GameMode,
)
from sidequest.game.projection.cache import ProjectionCache
from sidequest.game.projection.composed import ComposedFilter
from sidequest.game.projection.rules import load_rules_from_yaml_str
from sidequest.game.session import GameSnapshot
from sidequest.server.session_handler import (
    WebSocketSessionHandler,
    _SessionData,
)
from sidequest.server.session_room import RoomRegistry

_GENRE = "caverns_and_claudes"
_WORLD = "sunden"
_SLUG = "pov-emission-wiring"
_FIXTURE_PACKS = Path(__file__).resolve().parents[1] / "fixtures" / "packs"

_RULES_YAML = """
rules:
  - kind: NARRATION
    visibility_tag: {}
"""


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _pc(name: str, pronouns: str = "he/him") -> Character:
    core = CreatureCore(
        name=name,
        description="A test subject.",
        personality="Test.",
        inventory=Inventory(),
    )
    return Character(
        core=core,
        backstory="A wanderer.",
        char_class="Fighter",
        race="Human",
        pronouns=pronouns,
    )


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Bind the process pool to a per-worker throwaway PG db, clean per test
    (ADR-115 F1: events/projection persist to Postgres)."""
    import psycopg

    from sidequest.game import db_pool

    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(plain, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename <> 'alembic_version'"
        ).fetchall()
        if rows:
            names = ", ".join(f'"{r[0]}"' for r in rows)
            conn.execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    yield
    db_pool.close_pool()


def _seed_game_row(tmp_path: Path):
    """Register the session in Postgres and return the PgSaveRepository."""
    from sidequest.game import db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug

    repo, _dungeon, _sink = _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug=_SLUG,
        mode=str(GameMode.MULTIPLAYER),
        genre_slug=_GENRE,
        world_slug=_WORLD,
    )
    return repo


def _make_handler_three_pcs(tmp_path: Path) -> WebSocketSessionHandler:
    """Build a handler with three connected PCs (Carl/Donut/Katia)
    seated in a MULTIPLAYER room. Mirrors the 2026-05-12 playtest
    layout."""
    handler = WebSocketSessionHandler(save_dir=tmp_path, genre_pack_search_paths=[_FIXTURE_PACKS])
    snap = GameSnapshot(genre_slug=_GENRE, world_slug=_WORLD)
    snap.characters = [
        _pc("Carl", pronouns="he/him"),
        _pc("Donut", pronouns="he/him"),
        _pc("Katia", pronouns="she/her"),
    ]
    handler._session_data = _SessionData.__new__(_SessionData)
    handler._session_data.snapshot = snap
    handler._session_data.player_id = "p_carl"  # Carl is the emitter
    handler._session_data.genre_slug = _GENRE
    handler._session_data.world_slug = _WORLD

    store = _seed_game_row(tmp_path)
    repo = store
    handler._event_log = EventLog(repo)
    handler._projection_filter = ComposedFilter(
        rules=load_rules_from_yaml_str(_RULES_YAML),
        pack_slug=_GENRE,
    )
    handler._projection_cache = ProjectionCache(repo)

    registry = RoomRegistry()
    room = registry.get_or_create(slug=_SLUG, mode=GameMode.MULTIPLAYER)
    room.connect("p_carl", socket_id="sock-carl")
    room.connect("p_donut", socket_id="sock-donut")
    room.connect("p_katia", socket_id="sock-katia")
    # MP seat assignments — without these, build_game_state_view's
    # player_id_to_character mapping only knows about the emitter.
    # Story 49-8 reads this mapping at swap-time to resolve anchor_pc
    # to a player_id.
    room.seat("p_carl", character_slot="Carl")
    room.seat("p_donut", character_slot="Donut")
    room.seat("p_katia", character_slot="Katia")
    handler._room = room
    return handler


def _attach_queues(room) -> dict[str, asyncio.Queue]:
    q_carl: asyncio.Queue = asyncio.Queue()
    q_donut: asyncio.Queue = asyncio.Queue()
    q_katia: asyncio.Queue = asyncio.Queue()
    room.attach_outbound("sock-carl", q_carl)
    room.attach_outbound("sock-donut", q_donut)
    room.attach_outbound("sock-katia", q_katia)
    return {"p_carl": q_carl, "p_donut": q_donut, "p_katia": q_katia}


# ---------------------------------------------------------------------------
# 1. Carl receives 2nd-person for his own action card
# ---------------------------------------------------------------------------


def test_anchor_recipient_sees_second_person_prose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recipient whose player_id maps to the card's anchor_pc must
    see the prose rewritten to 2nd-person ('You plant a boot...').

    Architecture note: the emitter (Carl in this fixture) receives
    their NARRATION frame via the return value of ``_emit_event``,
    which production then pushes onto the emitter's outbound queue via
    the websocket layer's outbound list. Single-delivery contract —
    the emitter's queue stays clean inside emit_event's fan-out loop
    (which is for peers only) so that production handle_message can
    push the return value once.

    This is the load-bearing wiring assertion for Story 49-8. Without
    this, the 2026-05-12 playtest bug is unfixed: every player sees
    every per-PC card third-person.
    """
    handler = _make_handler_three_pcs(tmp_path)
    queues = _attach_queues(handler._room)

    # Fake message class so we can read the dict each player sees.
    from sidequest.server import session_handler as handler_module

    class _FakeMsg:
        def __init__(self, payload):
            self.payload = payload

    monkeypatch.setitem(handler_module._KIND_TO_MESSAGE_CLS, "NARRATION", _FakeMsg)

    # Build a NARRATION payload anchored on Carl. Production code
    # builds this via classify_narration_visibility; here we hand-build
    # the dict to keep the test focused on emit-pipeline wiring.
    payload = {
        "text": "Carl plants a boot on the moth's thorax.",
        "footnotes": [],
        "_visibility": {
            "visible_to": "all",
            "fidelity": {},
            "anchor_pc": "Carl",
            "pov_strategy": "pc_anchored",
        },
    }

    from sidequest.server import views as views_module

    monkeypatch.setattr(
        views_module,
        "status_effects_by_player",
        lambda _h: {},
    )

    out_to_self = handler._emit_event("NARRATION", payload)

    # Carl is the emitter — his frame returns from _emit_event and is
    # NOT placed on his queue inside emit_event (production handle_message
    # forwards it to his queue via the outbound list).
    assert queues["p_carl"].qsize() == 0, (
        "Carl is the emitter; his frame must NOT be queued by emit_event "
        "(handle_message pushes the return value onto his queue exactly "
        f"once); got {queues['p_carl'].qsize()} duplicate frame(s)"
    )

    assert out_to_self is not None, "emit_event must return the emitter's frame"
    carl_text = out_to_self.payload["text"]

    assert "You plant a boot" in carl_text, (
        f"Carl (anchor) must see 2nd-person prose; got: {carl_text!r}"
    )
    assert "Carl plants" not in carl_text, (
        f"Carl must NOT see his own name in 3rd-person; got: {carl_text!r}"
    )


# ---------------------------------------------------------------------------
# 2. Donut + Katia receive 3rd-person unchanged for Carl's card
# ---------------------------------------------------------------------------


def test_non_anchor_recipients_see_third_person_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Donut and Katia must see Carl's card unchanged — 'Carl plants
    a boot...' — because their player_ids do NOT map to anchor_pc.

    This story does NOT filter peer cards (that's ADR-028); all three
    players still receive the card. Only the prose framing differs.
    """
    handler = _make_handler_three_pcs(tmp_path)
    queues = _attach_queues(handler._room)

    from sidequest.server import session_handler as handler_module

    class _FakeMsg:
        def __init__(self, payload):
            self.payload = payload

    monkeypatch.setitem(handler_module._KIND_TO_MESSAGE_CLS, "NARRATION", _FakeMsg)

    from sidequest.server import views as views_module

    monkeypatch.setattr(views_module, "status_effects_by_player", lambda _h: {})

    payload = {
        "text": "Carl plants a boot on the moth's thorax.",
        "footnotes": [],
        "_visibility": {
            "visible_to": "all",
            "fidelity": {},
            "anchor_pc": "Carl",
            "pov_strategy": "pc_anchored",
        },
    }
    handler._emit_event("NARRATION", payload)

    # Donut and Katia must each receive exactly one frame, in 3rd-person.
    for pid in ("p_donut", "p_katia"):
        assert queues[pid].qsize() == 1, (
            f"player {pid} expected exactly one NARRATION frame, got {queues[pid].qsize()}"
        )
        frame = queues[pid].get_nowait()
        text = frame.payload["text"]
        assert "Carl plants a boot" in text, (
            f"non-anchor recipient {pid} must see 3rd-person; got: {text!r}"
        )
        assert "You plant" not in text, (
            f"non-anchor recipient {pid} must NOT receive the swapped prose; got: {text!r}"
        )


# ---------------------------------------------------------------------------
# 3. Atmospheric card (no anchor) goes to everyone unchanged
# ---------------------------------------------------------------------------


def test_atmospheric_card_broadcast_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When pov_strategy=='atmospheric' (no anchor), every recipient
    receives the original prose with no swap. Regression-sentinel for
    setting-only narration."""
    handler = _make_handler_three_pcs(tmp_path)
    queues = _attach_queues(handler._room)

    from sidequest.server import session_handler as handler_module

    class _FakeMsg:
        def __init__(self, payload):
            self.payload = payload

    monkeypatch.setitem(handler_module._KIND_TO_MESSAGE_CLS, "NARRATION", _FakeMsg)

    from sidequest.server import views as views_module

    monkeypatch.setattr(views_module, "status_effects_by_player", lambda _h: {})

    canonical = "Rain hammers the slate roof. The corridor smells of wet iron."
    payload = {
        "text": canonical,
        "footnotes": [],
        "_visibility": {
            "visible_to": "all",
            "fidelity": {},
            "anchor_pc": None,
            "pov_strategy": "atmospheric",
        },
    }
    handler._emit_event("NARRATION", payload)

    for pid in ("p_donut", "p_katia"):
        assert queues[pid].qsize() == 1
        frame = queues[pid].get_nowait()
        assert frame.payload["text"] == canonical, (
            f"atmospheric prose must be untouched for {pid}; got: {frame.payload['text']!r}"
        )


# ---------------------------------------------------------------------------
# 4. Anchor pronoun-driven swap — she/her case
# ---------------------------------------------------------------------------


def test_anchor_swap_uses_recipient_pc_pronouns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anchored on Katia (she/her), Katia's tab must see 2nd-person.
    Reflexive 'herself' must become 'yourself' on her tab.

    This fixture seats Carl as the emitter (``handler._session_data.player_id
    == "p_carl"``) but anchors the narration on Katia — modelling a
    turn where one player submits an action that the narrator chose to
    frame from Katia's POV. Carl is therefore both the emitter (return
    value path) and a NON-anchor recipient — he should see Katia in
    third-person. Katia, who is a peer recipient AND the anchor, must
    receive the swapped frame on her queue.
    """
    handler = _make_handler_three_pcs(tmp_path)
    queues = _attach_queues(handler._room)

    from sidequest.server import session_handler as handler_module

    class _FakeMsg:
        def __init__(self, payload):
            self.payload = payload

    monkeypatch.setitem(handler_module._KIND_TO_MESSAGE_CLS, "NARRATION", _FakeMsg)

    from sidequest.server import views as views_module

    monkeypatch.setattr(views_module, "status_effects_by_player", lambda _h: {})

    payload = {
        "text": "Katia braces herself and eases the knife back.",
        "footnotes": [],
        "_visibility": {
            "visible_to": "all",
            "fidelity": {},
            "anchor_pc": "Katia",
            "pov_strategy": "pc_anchored",
        },
    }
    out_to_self = handler._emit_event("NARRATION", payload)

    # Katia is a peer recipient AND the anchor — her frame lands on the
    # peer queue with the 2nd-person swap applied.
    assert queues["p_katia"].qsize() == 1, "Katia must receive her own card"
    katia_text = queues["p_katia"].get_nowait().payload["text"]
    assert "You brace yourself" in katia_text, (
        f"Katia (anchor, she/her) must see swapped reflexive; got: {katia_text!r}"
    )
    assert "herself" not in katia_text
    assert "Katia" not in katia_text

    # Donut is a peer non-anchor — sees Katia in 3rd-person.
    assert queues["p_donut"].qsize() == 1
    donut_text = queues["p_donut"].get_nowait().payload["text"]
    assert "Katia braces herself" in donut_text, (
        f"non-anchor Donut must see 3rd-person; got: {donut_text!r}"
    )

    # Carl is the emitter AND a non-anchor — his frame is the return
    # value, unswapped (3rd-person).
    assert out_to_self is not None
    carl_text = out_to_self.payload["text"]
    assert "Katia braces herself" in carl_text, (
        f"emitter (Carl) is not the anchor and must see 3rd-person; got: {carl_text!r}"
    )


# ---------------------------------------------------------------------------
# 5. ADR-105 B3+B4 — NARRATION_SEGMENT firewall + per-segment POV through
#    the REAL emit pipeline (ComposedFilter + queues). This is the test
#    that would have caught the 2026-05-16 caverns_sunden leak.
# ---------------------------------------------------------------------------


def test_narration_segment_firewalled_and_pov_swapped_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A private segment owned by Carl, emitted with author=Carl through
    the production ``_emit_event`` + the owner-socket forward:

      - Donut & Katia (non-owners) receive NOTHING — the visibility-gated
        CoreInvariant (B1) excludes them at fan-out. This is the firewall:
        the withheld perception never reaches the other tabs.
      - Carl (owner) receives the segment rewritten to 2nd-person (B4
        per-segment POV) via the owner-socket forward — emit_event's peer
        fan-out never delivers to the emitter, so the handler must push
        the returned frame to the owner's live socket.
    """
    handler = _make_handler_three_pcs(tmp_path)
    queues = _attach_queues(handler._room)

    from sidequest.server import session_handler as handler_module
    from sidequest.server import views as views_module

    class _FakeMsg:
        def __init__(self, payload):
            self.payload = payload

    monkeypatch.setitem(handler_module._KIND_TO_MESSAGE_CLS, "NARRATION_SEGMENT", _FakeMsg)
    monkeypatch.setattr(views_module, "status_effects_by_player", lambda _h: {})

    # Carl (he/him) privately senses something the others cannot. In a
    # pre-B3 world this prose would have ridden the shared NARRATION blob
    # to every tab — the exact leak ADR-105 closes.
    payload = {
        "text": "Carl feels the cold draft the others miss, and tastes iron on it.",
        "anchor_pc": "Carl",
        "turn_id": "t-seg-1",
        "_visibility": {
            "visible_to": ["p_carl"],
            "fidelity": {},
            "anchor_pc": "Carl",
            "pov_strategy": "pc_anchored",
        },
    }

    # Production sequence (websocket_session_handler.py): emit with
    # author=owner, then forward the returned owner frame to the owner's
    # live socket (emit_event's peer fan-out never delivers to the emitter).
    out = handler._emit_event("NARRATION_SEGMENT", payload, author_player_id="p_carl")
    room = handler._room
    assert room is not None
    _sock = room.socket_for_player("p_carl")
    _q = room.queue_for_socket(_sock) if _sock is not None else None
    assert _q is not None
    _q.put_nowait(out)

    # FIREWALL: non-owners receive NOTHING. This is the load-bearing
    # assertion — Donut/Katia are the 2026-05-16 leak victims.
    assert queues["p_donut"].qsize() == 0, (
        "FIREWALL BREACH: non-owner Donut received a private segment"
    )
    assert queues["p_katia"].qsize() == 0, (
        "FIREWALL BREACH: non-owner Katia received a private segment"
    )

    # B4 PER-SEGMENT POV: the owner reads 2nd-person, never their own
    # name in 3rd-person.
    assert out is not None
    owner_text = out.payload["text"]
    assert "you" in owner_text.lower(), (
        f"owner (Carl) must see 2nd-person private prose; got: {owner_text!r}"
    )
    assert "Carl feels" not in owner_text, (
        f"owner must NOT see their own name 3rd-person; got: {owner_text!r}"
    )

    # OWNER DELIVERY: the forwarded frame is on Carl's live socket queue.
    assert queues["p_carl"].qsize() == 1, (
        "owner must receive their own private segment via the owner-socket "
        f"forward; got {queues['p_carl'].qsize()} frame(s)"
    )
    delivered = queues["p_carl"].get_nowait()
    assert "you" in delivered.payload["text"].lower()
