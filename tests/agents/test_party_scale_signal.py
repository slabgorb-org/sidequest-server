"""Story 126-36 — the narrator must be TOLD the seat count.

Playtest bug (dust_and_lead, solo session): the narrator wrote quest briefs
scaled to a party — *"fifty each for two riders"* — in a one-player game,
because nothing in the narrator prompt ever states how many players are at
the table. ``register_party_peer_section`` is MP-only: it returns early on an
empty peer list (zero-byte-leak discipline), so a SOLO session registers
**no** party section at all and the narrator has no ground truth on seat
count. It then improvises a plural.

The fix threads the seat count into every narrator prompt as an always-on
``party_scale`` section (seat count = 1 + ``len(context.party_peers)``; peers
exclude self), plus an always-emit ``narrator.party_scale`` OTEL span so the
GM panel (the dev lie-detector) can prove the signal engaged on every turn —
mirroring the always-fire ``narrator.seed_context`` precedent.

Scope note (the OTHER half of 126-36 is content, not tested here): the
verbatim ``first_turn_invitation`` cold-open prose ("pours four small glasses
… waits for the party") is emitted to the player UNCHANGED by the cold-open
path (``websocket_session_handler`` consume step) — the narrator never
regenerates it, so a prompt signal cannot fix that authored line. Neutralising
that wording is a sidequest-content edit verified via ``load_genre_pack`` + GM
review, NOT a server unit test (content is validated, not unit-tested; tests
must not point at live packs). This file covers ONLY the server seat-count
signal that scales the narrator's *generated* quest briefs.

Contract pinned by these RED tests (see TEA Assessment for rationale):
  * section name:  ``party_scale``  — registered for the narrator on EVERY
    turn and EVERY party size (solo included).
  * solo content communicates a single player (one of: "solo", "1 player",
    "one player", "single player").
  * MP content communicates the exact seat count as ``"<n> player"``.
  * span name:  ``narrator.party_scale`` with int attribute ``player_count``.
"""

from __future__ import annotations

import pytest

from sidequest.agents.orchestrator import Orchestrator, TurnContext
from sidequest.game.session import PartyPeer

PARTY_SCALE_SECTION = "party_scale"
PARTY_SCALE_SPAN = "narrator.party_scale"


def _make_peers(n: int) -> list[PartyPeer]:
    """``n`` canonical peer packets (peers exclude self → seat count = n + 1)."""
    return [PartyPeer(name=f"Peer{i}", race="human", char_class="drifter") for i in range(n)]


def _solo_context(turn_number: int = 0) -> TurnContext:
    return TurnContext(
        character_name="Kael",
        genre="caverns_and_claudes",
        turn_number=turn_number,
        party_peers=[],
    )


def _party_context(seat_count: int, turn_number: int = 0) -> TurnContext:
    assert seat_count >= 1
    return TurnContext(
        character_name="Kael",
        genre="caverns_and_claudes",
        turn_number=turn_number,
        party_peers=_make_peers(seat_count - 1),
    )


async def _party_scale_section(orch: Orchestrator, context: TurnContext):
    """Build the narrator prompt and return the ``party_scale`` section (or None)."""
    _, registry = await orch.build_narrator_prompt("I look around.", context)
    sections = registry.registry(orch._narrator.name())
    matches = [s for s in sections if s.name == PARTY_SCALE_SECTION]
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# Section presence — the core gap is SOLO, which currently registers nothing.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_party_scale_section_registers_for_solo():
    """A solo session MUST register a party_scale section.

    This is the bug: ``register_party_peer_section`` returns early on an
    empty peer list, so before the fix a solo prompt has no seat-count
    signal at all. The section must fire even with zero peers.
    """
    orch = Orchestrator()
    section = await _party_scale_section(orch, _solo_context())
    assert section is not None, (
        "solo session registered no party_scale section — the narrator has no "
        "seat-count ground truth and will improvise a party-scaled quest brief"
    )


@pytest.mark.asyncio
async def test_party_scale_solo_states_single_player():
    """Solo content communicates exactly one player (singular framing)."""
    orch = Orchestrator()
    section = await _party_scale_section(orch, _solo_context())
    assert section is not None
    content = section.content.lower()
    assert any(token in content for token in ("solo", "1 player", "one player", "single player")), (
        "solo party_scale section must signal a single player so the narrator "
        f"scales quest briefs to one rider; got: {section.content!r}"
    )


@pytest.mark.asyncio
async def test_party_scale_section_registers_for_multiplayer():
    """A two-player session also registers the party_scale section."""
    orch = Orchestrator()
    section = await _party_scale_section(orch, _party_context(2))
    assert section is not None, "multiplayer session registered no party_scale section"


@pytest.mark.asyncio
@pytest.mark.parametrize("seat_count", [2, 3, 4])
async def test_party_scale_states_exact_seat_count(seat_count: int):
    """MP content states the exact seat count as ``"<n> player"`` so quest
    briefs scale ("a rider" vs "two riders" vs "three riders")."""
    orch = Orchestrator()
    section = await _party_scale_section(orch, _party_context(seat_count))
    assert section is not None
    content = section.content.lower()
    assert f"{seat_count} player" in content, (
        f"party_scale section for {seat_count} players must state "
        f"'{seat_count} player(s)'; got: {section.content!r}"
    )


# ---------------------------------------------------------------------------
# Every-turn — the "two riders" quest brief happens DURING play, not just the
# opening. The signal must persist past turn 0 (mirrors narrator_identity).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_party_scale_fires_on_a_mid_session_turn():
    """The seat-count signal is not opening-only; it must still be present on
    a mid-session turn where the narrator writes a quest brief."""
    orch = Orchestrator()
    section = await _party_scale_section(orch, _solo_context(turn_number=3))
    assert section is not None, (
        "party_scale section vanished after turn 0 — the 'fifty each for two "
        "riders' brief is a mid-session symptom, so the signal must persist"
    )


# ---------------------------------------------------------------------------
# OTEL — always-emit seat-count span (the dev/GM lie-detector). Fires even
# solo (player_count=1) so silence-by-absence is distinguishable from
# signal-not-injected, exactly like narrator.seed_context.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_party_scale_emits_otel_span_solo(otel_capture):
    """Building a solo narrator prompt emits narrator.party_scale with
    player_count == 1."""
    orch = Orchestrator()
    await orch.build_narrator_prompt("I look around.", _solo_context())

    spans = [s for s in otel_capture.get_finished_spans() if s.name == PARTY_SCALE_SPAN]
    assert spans, (
        f"no {PARTY_SCALE_SPAN} span emitted on a solo prompt build; emitted "
        f"spans: {[s.name for s in otel_capture.get_finished_spans()]}"
    )
    attrs = dict(spans[-1].attributes)
    assert attrs.get("player_count") == 1, (
        f"solo player_count must be 1; got {attrs.get('player_count')!r}"
    )


@pytest.mark.asyncio
async def test_party_scale_otel_span_carries_seat_count_mp(otel_capture):
    """A three-seat session emits narrator.party_scale with player_count == 3."""
    orch = Orchestrator()
    await orch.build_narrator_prompt("I look around.", _party_context(3))

    spans = [s for s in otel_capture.get_finished_spans() if s.name == PARTY_SCALE_SPAN]
    assert spans, f"no {PARTY_SCALE_SPAN} span emitted on a 3-seat prompt build"
    attrs = dict(spans[-1].attributes)
    assert attrs.get("player_count") == 3, (
        f"3-seat player_count must be 3; got {attrs.get('player_count')!r}"
    )
