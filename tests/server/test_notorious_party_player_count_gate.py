"""RED-phase tests for Story 45-8: Notorious-party gating on session.player_count.

Playtest 3 (2026-04-19, evropi) regression: Rux — a named party member
seeded by the world configuration (the conceptual ``notorious_party``
fixture) — leaked into pumblestone_sweedlewit's solo-session narration.
The narrator referenced Rux by name despite pumblestone being the only
PLAYING player in the room.

Root cause traced to ``sidequest.server.session_helpers._build_turn_context``::

    party_peers: list[PartyPeer] = [
        PartyPeer.from_character(pc)
        for pc in snapshot.characters
        if pc.core.name != char_name
    ]

The list comprehension gates on **snapshot identity**, not on **session
player_count**. When a save (or world fixture) contains canonical PCs from
a previous full-party session — Rux, Hant, Ludzo, Prot'Thokk, Th'Rook —
they all survive the filter and ride into pumblestone's solo prompt.
Story 37-36 closed identity drift in multiplayer; 45-8 closes the inverse
leak in solo.

Acceptance criteria — see ``.session/45-8-session.md``:

  AC1  Solo session (player_count == 1) → notorious_party context fully
       gated out of narrator prompt context.
  AC2  Multiplayer (player_count > 1) → notorious_party seeds normally.
  AC3  OTEL span fires every turn carrying:
         - ``session.player_count`` (int)
         - ``notorious_party_gated`` (bool)
         - ``party_context_available`` (bool)
  AC4  No silent fallback — if the gating mechanism is broken/missing the
       narrator path must fail loud (OTEL event + structured log), never
       proceed with wrong context.
  AC5  Wiring test: spin a solo room with multiple known characters in
       the snapshot, build the turn context end-to-end, assert the prompt
       contains none of the named party members.

CLAUDE.md "Every Test Suite Needs a Wiring Test" — the suite below
covers (a) gating logic, (b) prompt-render integration, (c) OTEL
observability, (d) fail-loud behaviour, (e) end-to-end wiring from
``_SessionData`` → ``_build_turn_context`` → ``Orchestrator.build_narrator_prompt``.

Test paranoia (TEA discipline):
  - Solo with a single self character → already empty (covered by 37-36)
  - Solo with N>1 characters in snapshot → MUST be empty (45-8 RED)
  - Multiplayer with N>1 characters → MUST be populated (regression guard)
  - Boundary: player_count=2 (just over the gate) → seeds
  - room=None (gate machinery missing) → fail loud, no silent passthrough
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from sidequest.agents.claude_client import ClaudeClient
from sidequest.agents.orchestrator import Orchestrator
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.session import GameSnapshot
from sidequest.genre.loader import DEFAULT_GENRE_PACK_SEARCH_PATHS, GenreLoader

# ---------------------------------------------------------------------------
# Helpers — recreate the playtest-3 evropi cast that leaked into pumblestone's
# solo prose. Names are load-bearing: the assertions below grep the rendered
# prompt for these exact strings.
# ---------------------------------------------------------------------------


def _make_character(
    name: str,
    *,
    pronouns: str = "they/them",
    race: str = "Human",
    char_class: str = "Fighter",
    level: int = 1,
    backstory: str = "A canonical party member from the evropi save.",
) -> Character:
    core = CreatureCore(
        name=name,
        description=f"A {race} {char_class}.",
        personality="stoic",
        level=level,
        inventory=Inventory(),
    )
    return Character(
        core=core,
        char_class=char_class,
        race=race,
        pronouns=pronouns,
        backstory=backstory,
    )


def _pumblestone() -> Character:
    """Solo-session player from playtest 3 (heavy_metal/evropi)."""
    return _make_character(
        "Pumblestone Sweedlewit",
        pronouns="he/him",
        race="Halfling",
        char_class="Burglar",
        level=2,
    )


def _rux() -> Character:
    """Notorious-party member: kobold dragonkin, the leaker."""
    return _make_character("Rux", pronouns="he/him", race="Kobold", char_class="Cleric", level=3)


def _hant() -> Character:
    return _make_character("Hant", pronouns="she/her", race="Human", char_class="Ranger", level=3)


def _ludzo() -> Character:
    return _make_character("Ludzo", pronouns="he/him", race="Dwarf", char_class="Fighter", level=3)


def _make_orchestrator() -> Orchestrator:
    client = MagicMock(spec=ClaudeClient)
    return Orchestrator(client=client)


def _make_room(*, playing_count: int, seat_map: dict[str, str] | None = None):
    """Mock a ``SessionRoom`` exposing exactly the surface ``_build_turn_context``
    reads. ``playing_player_count()`` is the source-of-truth for AC1/AC2.
    """
    room = MagicMock()
    room.playing_player_count = MagicMock(return_value=playing_count)
    room.non_abandoned_player_count = MagicMock(return_value=playing_count)
    room.seated_player_count = MagicMock(return_value=playing_count)
    room.slot_to_player_id = MagicMock(return_value=dict(seat_map or {}))
    return room


@pytest.fixture
def sd_factory():
    """Build a ``_SessionData`` carrying the caverns_and_claudes pack and a
    caller-supplied character roster on the snapshot. The first listed
    character is the acting PC (matches the convention in
    ``test_party_peer_identity.py``).
    """
    from sidequest.server.session_handler import _SessionData

    pack = GenreLoader(DEFAULT_GENRE_PACK_SEARCH_PATHS).load("caverns_and_claudes")

    def _make(characters: list[Character], *, acting_player: str) -> _SessionData:
        snap = GameSnapshot(
            genre_slug="caverns_and_claudes",
            world_slug="crypt_of_the_seven",
            location="Entrance",
        )
        snap.characters = list(characters)
        return _SessionData(
            genre_slug="caverns_and_claudes",
            world_slug="crypt_of_the_seven",
            player_name=acting_player,
            player_id=f"p-{acting_player.lower().replace(' ', '_')}",
            snapshot=snap,
            store=MagicMock(),
            genre_pack=pack,
            orchestrator=MagicMock(),
        )

    return _make


# ---------------------------------------------------------------------------
# AC1 — Solo Session Isolation Gate
#
# When a solo player's save (or a world fixture) carries multiple PCs in
# ``snapshot.characters``, the gate must produce an empty ``party_peers``
# list. The current code does NOT check player_count and so leaks every
# non-self PC.
# ---------------------------------------------------------------------------


def test_solo_session_with_multi_pc_snapshot_produces_empty_party_peers(
    sd_factory,
) -> None:
    """RED: in a solo session whose snapshot carries multiple canonical
    characters (Rux, Hant, Ludzo) alongside the acting player, the gate
    must drop every peer. The pre-fix implementation pulls them all in.
    """
    from sidequest.server.session_helpers import _build_turn_context

    sd = sd_factory(
        [_pumblestone(), _rux(), _hant(), _ludzo()],
        acting_player="Pumblestone Sweedlewit",
    )
    room = _make_room(
        playing_count=1,
        seat_map={"Pumblestone Sweedlewit": sd.player_id},
    )

    ctx = _build_turn_context(sd, room=room)

    assert ctx.party_peers == [], (
        "Solo session (player_count=1) leaked party peers from "
        "snapshot.characters — exact playtest-3 regression: "
        f"got {[p.name for p in ctx.party_peers]!r}, expected []."
    )


def test_solo_session_npc_registry_unchanged_by_gate(sd_factory) -> None:
    """The gate scope is party-peer / known-PC context only. NPC registry,
    encounter context, and world_context must NOT be collateral damage.
    """
    from sidequest.server.session_helpers import _build_turn_context

    sd = sd_factory([_pumblestone(), _rux()], acting_player="Pumblestone Sweedlewit")
    room = _make_room(playing_count=1)

    ctx = _build_turn_context(sd, room=room)
    # Snapshot has no NPCs and no pool — those should round-trip empty,
    # not None. Asserting structure preserved (gate didn't crash adjacent
    # state). Story 45-52 cleanup: ``npc_registry`` was dropped; canonical
    # cast-pool channel is ``npc_pool``.
    assert ctx.npc_pool == [], "Gate damaged unrelated state: npc_pool not the snapshot value."
    assert ctx.character_name == "Pumblestone Sweedlewit", (
        "Gate damaged acting-character resolution."
    )


async def test_solo_session_prompt_contains_no_named_party_members(
    sd_factory,
) -> None:
    """End-to-end: from ``_SessionData`` with multi-PC snapshot through
    ``_build_turn_context`` and ``build_narrator_prompt``, NO part of the
    notorious_party fixture (Rux/Hant/Ludzo names) reaches the prompt.

    AC1 — verifiable by grep, per the session file.
    """
    from sidequest.server.session_helpers import _build_turn_context

    sd = sd_factory(
        [_pumblestone(), _rux(), _hant(), _ludzo()],
        acting_player="Pumblestone Sweedlewit",
    )
    room = _make_room(
        playing_count=1,
        seat_map={"Pumblestone Sweedlewit": sd.player_id},
    )
    ctx = _build_turn_context(sd, room=room)

    orch = _make_orchestrator()
    prompt, registry = await orch.build_narrator_prompt("look around", ctx)

    # No party-peer roster section should be registered.
    agent_name = orch._narrator.name()
    section_names = {s.name for s in registry.registry(agent_name)}
    for forbidden in ("party_peer_roster", "party_peers", "party_roster"):
        assert forbidden not in section_names, (
            f"Solo session still registered party section `{forbidden}` — AC1 gate broken."
        )

    # Guard against the snapshot JSON ``state_summary`` carrying canonical
    # peer data into the prompt (the bug surface that motivated the
    # registry-only check in 37-36 — we want it absent here too because
    # AC1 says "no part of the party-member fixture reaches the prompt").
    for forbidden_name in ("Rux", "Hant", "Ludzo"):
        assert forbidden_name not in prompt, (
            f"Notorious-party member `{forbidden_name}` leaked into the "
            "rendered prompt for a solo session — AC1 grep precondition "
            "fails. Story 45-8 explicitly requires `grep` cleanliness."
        )


# ---------------------------------------------------------------------------
# AC2 — Multiplayer Passthrough
#
# When player_count > 1, party_peers must continue to populate. This is
# the regression guard for 37-36 — the 45-8 fix must not strangle MP.
# ---------------------------------------------------------------------------


def test_multiplayer_session_seeds_party_peers(sd_factory) -> None:
    """player_count == 3 → all non-self PCs land in ctx.party_peers."""
    from sidequest.server.session_helpers import _build_turn_context

    sd = sd_factory(
        [_pumblestone(), _rux(), _hant()],
        acting_player="Pumblestone Sweedlewit",
    )
    room = _make_room(
        playing_count=3,
        seat_map={
            "Pumblestone Sweedlewit": sd.player_id,
            "Rux": "p-rux",
            "Hant": "p-hant",
        },
    )

    ctx = _build_turn_context(sd, room=room)

    peer_names = {p.name for p in ctx.party_peers}
    assert peer_names == {"Rux", "Hant"}, (
        "Multiplayer passthrough broken: expected {Rux, Hant} as peers, "
        f"got {peer_names!r}. The 45-8 gate over-fired."
    )


def test_two_player_session_just_over_gate_seeds_peers(sd_factory) -> None:
    """Boundary: player_count == 2 must pass (gate is on ``== 1``)."""
    from sidequest.server.session_helpers import _build_turn_context

    sd = sd_factory([_pumblestone(), _rux()], acting_player="Pumblestone Sweedlewit")
    room = _make_room(
        playing_count=2,
        seat_map={
            "Pumblestone Sweedlewit": sd.player_id,
            "Rux": "p-rux",
        },
    )

    ctx = _build_turn_context(sd, room=room)
    peer_names = {p.name for p in ctx.party_peers}
    assert peer_names == {"Rux"}, (
        "Boundary failure: 2-player session lost Rux. The gate must apply "
        "only when player_count == 1."
    )


async def test_multiplayer_prompt_contains_peer_names(sd_factory) -> None:
    """Wiring: in MP, the rendered prompt MUST contain peer names."""
    from sidequest.server.session_helpers import _build_turn_context

    sd = sd_factory([_pumblestone(), _rux()], acting_player="Pumblestone Sweedlewit")
    room = _make_room(
        playing_count=2,
        seat_map={
            "Pumblestone Sweedlewit": sd.player_id,
            "Rux": "p-rux",
        },
    )
    ctx = _build_turn_context(sd, room=room)

    orch = _make_orchestrator()
    prompt, registry = await orch.build_narrator_prompt("look around", ctx)

    assert "Rux" in prompt, (
        "Multiplayer regression: peer name `Rux` missing from rendered "
        "prompt — 45-8 gate over-fired or 37-36 dossier got dropped."
    )
    agent_name = orch._narrator.name()
    section_names = {s.name for s in registry.registry(agent_name)}
    has_party_section = any(
        n in section_names for n in ("party_peer_roster", "party_peers", "party_roster")
    )
    assert has_party_section, "Multiplayer regression: no party-peer roster section registered."


# ---------------------------------------------------------------------------
# AC3 — OTEL Observability
#
# A span must fire on EVERY turn (gate decision telemetry — even when the
# gate is *passing* or *not engaged*). The GM panel filters on this so
# Sebastien can see the gate is alive.
# ---------------------------------------------------------------------------


GATE_SPAN_CANDIDATES: tuple[str, ...] = (
    "orchestrator.notorious_party_gate",
    "session.notorious_party_gate",
    "narrator.notorious_party_gate",
    # Allow co-emission on the existing party_peer_injection span if Dev
    # picks that home — AC3 only requires the attributes, not a new span
    # name. The gate span MUST still fire on solo (zero peers), so
    # piggybacking on injection alone is invalid for solo; the injection
    # span is silent in that case (37-36 zero-byte rule).
)


def _gate_span_in_log(log_text: str) -> bool:
    return any(name in log_text for name in GATE_SPAN_CANDIDATES)


def test_notorious_party_gate_span_is_defined_in_catalog():
    """A dedicated span constant must exist in
    ``sidequest.telemetry.spans`` so GM-panel routing and the
    test_routing_completeness gate both see it. Probe the standard names —
    Dev picks one, this test pins it down so it can't quietly drop later.
    """
    from sidequest.telemetry import spans as spans_module

    expected_attr = (
        "SPAN_ORCHESTRATOR_NOTORIOUS_PARTY_GATE",
        "SPAN_SESSION_NOTORIOUS_PARTY_GATE",
        "SPAN_NARRATOR_NOTORIOUS_PARTY_GATE",
    )
    found = [name for name in expected_attr if hasattr(spans_module, name)]
    assert found, (
        "No `*_NOTORIOUS_PARTY_GATE` constant in the telemetry spans "
        f"catalog. Expected one of: {expected_attr}. Without a catalog "
        "entry the GM panel filter cannot route gate events and "
        "test_routing_completeness will fail when the value lands."
    )


async def test_solo_session_emits_gate_span_with_gated_true(
    sd_factory,
    caplog,
    monkeypatch,
) -> None:
    """AC3: every solo turn must fire a gate span carrying the three
    required attributes — ``session.player_count``, ``notorious_party_gated``,
    ``party_context_available``.
    """
    from sidequest.server.session_helpers import _build_turn_context

    monkeypatch.setattr(logging.getLogger("sidequest"), "propagate", True)

    sd = sd_factory(
        [_pumblestone(), _rux(), _hant()],
        acting_player="Pumblestone Sweedlewit",
    )
    room = _make_room(
        playing_count=1,
        seat_map={"Pumblestone Sweedlewit": sd.player_id},
    )

    with caplog.at_level(logging.INFO):
        _build_turn_context(sd, room=room)

    text = caplog.text
    assert _gate_span_in_log(text), (
        "Gate span did not fire on solo turn. Expected one of "
        f"{GATE_SPAN_CANDIDATES} in caplog. CLAUDE.md OTEL Observability "
        "Principle: a gate without a span is Claude-improvising-gate."
    )
    # All three attributes must appear in the same log block.
    assert "player_count=1" in text or "session.player_count=1" in text, (
        "Gate span missing required attribute `session.player_count=1`."
    )
    assert "notorious_party_gated=true" in text.lower() or "notorious_party_gated=True" in text, (
        "Gate span missing required attribute `notorious_party_gated=true`."
    )
    assert (
        "party_context_available=false" in text.lower() or "party_context_available=False" in text
    ), "Gate span missing required attribute `party_context_available=false` on solo turn."


async def test_multiplayer_session_emits_gate_span_with_gated_false(
    sd_factory,
    caplog,
    monkeypatch,
) -> None:
    """AC3 mirror: in MP the same span fires with
    ``notorious_party_gated=false`` and ``party_context_available=true``.
    """
    from sidequest.server.session_helpers import _build_turn_context

    monkeypatch.setattr(logging.getLogger("sidequest"), "propagate", True)

    sd = sd_factory([_pumblestone(), _rux()], acting_player="Pumblestone Sweedlewit")
    room = _make_room(
        playing_count=2,
        seat_map={
            "Pumblestone Sweedlewit": sd.player_id,
            "Rux": "p-rux",
        },
    )

    with caplog.at_level(logging.INFO):
        _build_turn_context(sd, room=room)

    text = caplog.text
    assert _gate_span_in_log(text), (
        "Gate span did not fire on multiplayer turn. AC3 requires the "
        "span to fire on EVERY turn, not just solo."
    )
    assert "player_count=2" in text or "session.player_count=2" in text
    assert "notorious_party_gated=false" in text.lower() or "notorious_party_gated=False" in text, (
        "Gate span attribute `notorious_party_gated` should be false in "
        "multiplayer (gate did not engage)."
    )
    assert (
        "party_context_available=true" in text.lower() or "party_context_available=True" in text
    ), (
        "Gate span attribute `party_context_available` should be true in "
        "multiplayer (peer context is in the prompt)."
    )


# ---------------------------------------------------------------------------
# AC4 — No Silent Fallbacks
#
# If the gate machinery is missing — ``room`` is None or
# ``playing_player_count`` is unreadable — the narrator path must not
# silently degrade to "all snapshot characters". CLAUDE.md "No Silent
# Fallbacks": fail loud.
# ---------------------------------------------------------------------------


def test_missing_room_fails_loud_when_snapshot_has_multi_pcs(
    sd_factory,
    caplog,
    monkeypatch,
) -> None:
    """If ``room`` is None and snapshot.characters contains the
    notorious_party fixture, the gate cannot determine player_count.
    AC4 forbids silently seeding the full party — either raise, or emit
    a structured error event AND drop the peers (defensive default).

    Either contract is acceptable to TEA; the assertion below accepts
    EITHER (a) a raised exception OR (b) empty party_peers + structured
    error log. What is NOT acceptable: silent passthrough that puts Rux
    back into the prompt.
    """
    from sidequest.server.session_helpers import _build_turn_context

    monkeypatch.setattr(logging.getLogger("sidequest"), "propagate", True)

    sd = sd_factory(
        [_pumblestone(), _rux(), _hant()],
        acting_player="Pumblestone Sweedlewit",
    )

    raised: Exception | None = None
    ctx = None
    with caplog.at_level(logging.WARNING):
        try:
            ctx = _build_turn_context(sd, room=None)
        except Exception as exc:  # noqa: BLE001 — broad to honour either contract
            raised = exc

    if raised is not None:
        # Contract (a): explicit fail-loud raise. Acceptable.
        return

    # Contract (b): defensive default — must NOT seed peers, AND must log.
    assert ctx is not None
    assert ctx.party_peers == [], (
        "AC4 violated: room=None silently seeded "
        f"{[p.name for p in ctx.party_peers]!r} as party peers. The gate "
        "machinery was unreachable and the narrator path silently "
        "fell back to 'every snapshot character is a peer'."
    )
    text = caplog.text.lower()
    assert "notorious_party" in text or "gate" in text or "player_count" in text, (
        "AC4 violated: room=None defaulted to safe-empty without an "
        "OTEL/structured log event. Silent fallback is forbidden — the "
        "operator needs to see this state in the GM panel."
    )


# ---------------------------------------------------------------------------
# AC5 — Wiring (CLAUDE.md: every test suite needs an integration test)
#
# Spin a real ``_SessionData`` with the playtest-3 cast, run the actual
# session_helpers + orchestrator, and check the prompt registry. This is
# the lock that closes the bug at the system level — not just the unit
# level.
# ---------------------------------------------------------------------------


async def test_wiring_solo_evropi_save_does_not_leak_canonical_party(
    sd_factory,
) -> None:
    """End-to-end RED: the playtest-3 reproducer.

    Build a solo session whose snapshot looks like pumblestone's loaded
    save (the player + the canonical heavy_metal/evropi cast). Drive the
    real ``_build_turn_context`` and ``build_narrator_prompt``. Assert
    the registered prompt sections contain no canonical-party names.
    """
    from sidequest.server.session_helpers import _build_turn_context

    sd = sd_factory(
        [_pumblestone(), _rux(), _hant(), _ludzo()],
        acting_player="Pumblestone Sweedlewit",
    )
    room = _make_room(
        playing_count=1,
        seat_map={"Pumblestone Sweedlewit": sd.player_id},
    )
    ctx = _build_turn_context(sd, room=room)

    orch = _make_orchestrator()
    _, registry = await orch.build_narrator_prompt(
        "I take a careful look at the empty road.",
        ctx,
    )

    agent_name = orch._narrator.name()
    sections = registry.registry(agent_name)

    # No party section at all.
    for s in sections:
        lname = s.name.lower()
        assert "party" not in lname or "peer" not in lname, (
            f"Wiring leak: solo session registered a party-peer section "
            f"`{s.name}`. The 45-8 gate did not run on the prompt path."
        )

    # No section body mentions the leakers, individually. (state_summary
    # is JSON-rendered; if the gate also redacts there, all three names
    # vanish. If the gate is registry-scope only, this assertion will
    # surface the residual JSON leak — Dev must extend the gate.)
    rendered = "\n".join(s.content for s in sections)
    for forbidden_name in ("Rux", "Hant", "Ludzo"):
        assert forbidden_name not in rendered, (
            f"Wiring leak: `{forbidden_name}` survived in the rendered "
            "registry content of a solo session. Per AC1 — verifiable "
            "via grep — the prompt must be free of every notorious_party "
            "fixture name."
        )


async def test_wiring_multiplayer_evropi_save_keeps_party_visible(
    sd_factory,
) -> None:
    """Mirror wiring test for AC2: with 3 PLAYING players, the canonical
    party MUST reach the prompt. Closes the regression door on the gate
    over-firing in legitimate multiplayer.
    """
    from sidequest.server.session_helpers import _build_turn_context

    sd = sd_factory(
        [_pumblestone(), _rux(), _hant()],
        acting_player="Pumblestone Sweedlewit",
    )
    room = _make_room(
        playing_count=3,
        seat_map={
            "Pumblestone Sweedlewit": sd.player_id,
            "Rux": "p-rux",
            "Hant": "p-hant",
        },
    )
    ctx = _build_turn_context(sd, room=room)

    orch = _make_orchestrator()
    prompt, registry = await orch.build_narrator_prompt(
        "I greet my companions.",
        ctx,
    )

    agent_name = orch._narrator.name()
    section_names = {s.name for s in registry.registry(agent_name)}
    has_peer_section = any(
        n in section_names for n in ("party_peer_roster", "party_peers", "party_roster")
    )
    assert has_peer_section, (
        "Wiring regression: 3-player session produced no peer dossier "
        "section. The 45-8 gate over-fired."
    )
    assert "Rux" in prompt and "Hant" in prompt, (
        "Wiring regression: 3-player session lost canonical peer names from the rendered prompt."
    )
