"""Failing tests for Story 37-36: Party-peer identity packet in game_state.

Port-drift reopen (ADR-082 / ADR-085 rule 2). The Rust implementation did
not survive the port back to Python — ``sidequest-server`` has no
``PartyPeer`` type, ``TurnContext`` carries no peer packet, and
``build_narrator_prompt`` never injects canonical peer identity.

The bug the fix exists to catch: in a multiplayer sealed-letter session,
when Player A's turn fires, the narrator has zero canonical identity data
about Player B (name, pronouns, race, class, level). Prose about Player B
in Player A's save drifts — playtest 3 (2026-04-19): Blutka registered
he/him in his own save became she/her in Orin's save because Orin's
``game_state`` carried no ground truth about Blutka.

This is the party-peer parallel of story 37-44 (NPC identity drift).
37-44 injected a ``KNOWN NPCS`` dossier. 37-36 must inject the equivalent
``PARTY MEMBERS`` dossier for other PCs. Perception stays POV; physical
identity is canonical.

Acceptance criteria (from session file):
  AC-1  ``PartyPeer`` type with name, pronouns, race, char_class, level
  AC-2  ``inject_party_peers()`` (or equivalent) reads ``snapshot.characters``
        and produces one ``PartyPeer`` per non-self party member
  AC-3  Narrator system prompt receives the peer dossier every turn
  AC-4  OTEL span ``orchestrator.party_peer_injection`` fires on injection
  AC-5  Canonical = physical (name/pronouns/race/class/level).
        Perception (mood, tactics, feelings) stays POV — must NOT land
        in the dossier.
  AC-6  Tests cover type, injection, prompt integration, OTEL, and the
        wiring seam.

CLAUDE.md — wiring discipline: a unit test that constructs a peer and
inspects a section is not enough. An integration test must prove the
peer dossier lands in the prompt for a real ``_build_turn_context`` →
``build_narrator_prompt`` flow built from ``_SessionData``.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from sidequest.agents.claude_client import ClaudeClient
from sidequest.agents.orchestrator import (
    Orchestrator,
    TurnContext,
)
from sidequest.agents.prompt_framework.types import (
    AttentionZone,
    SectionCategory,
)
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.session import GameSnapshot
from sidequest.genre.loader import DEFAULT_GENRE_PACK_SEARCH_PATHS, GenreLoader

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_character(
    name: str,
    *,
    pronouns: str,
    race: str,
    char_class: str,
    level: int = 1,
    backstory: str = "A party member.",
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


def _blutka() -> Character:
    """Playtest-3 reference peer: Blutka (he/him)."""
    return _make_character("Blutka", pronouns="he/him", race="Orc", char_class="Rogue", level=3)


def _orin() -> Character:
    """Playtest-3 reference peer: Orin (they/them)."""
    return _make_character(
        "Orin", pronouns="they/them", race="Human", char_class="Fighter", level=3
    )


def _make_orchestrator() -> Orchestrator:
    client = MagicMock(spec=ClaudeClient)
    return Orchestrator(client=client)


def _make_mp_room(*, playing_count: int = 2):
    """Story 45-8: ``_build_turn_context`` now gates ``party_peers`` on
    ``room.playing_player_count() > 1``. The 37-36 multiplayer tests must
    therefore hand it a room mock that reports an MP count, otherwise the
    notorious-party gate (correctly) zeroes the peer list.
    """
    room = MagicMock()
    room.playing_player_count = MagicMock(return_value=playing_count)
    room.non_abandoned_player_count = MagicMock(return_value=playing_count)
    room.seated_player_count = MagicMock(return_value=playing_count)
    room.slot_to_player_id = MagicMock(return_value={})
    return room


@pytest.fixture
def sd_factory():
    """Build a ``_SessionData`` with a loaded caverns_and_claudes pack and
    a caller-supplied characters list. The first character in the list is
    taken as the acting player's PC (matching the
    ``snapshot.characters[0]`` convention documented at
    ``session_handler.py:3743-3745``); the rest are peers.
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
            player_id=f"p-{acting_player.lower()}",
            snapshot=snap,
            store=MagicMock(),
            genre_pack=pack,
            orchestrator=MagicMock(),
        )

    return _make


# ---------------------------------------------------------------------------
# AC-1: PartyPeer type exists
# ---------------------------------------------------------------------------


def test_party_peer_type_is_importable():
    """A canonical peer-identity type must exist. The exact import location
    is Dev's choice, but it must be reachable from somewhere stable in the
    ``sidequest`` package. Probe the likely homes: ``game.session`` (next
    to ``NpcPoolMember``) or ``game.party``.
    """
    candidates = (
        "sidequest.game.session",
        "sidequest.game.party",
        "sidequest.game.character",
        "sidequest.agents.orchestrator",
    )
    found = []
    for mod_path in candidates:
        try:
            mod = __import__(mod_path, fromlist=["PartyPeer"])
        except ImportError:
            continue
        if hasattr(mod, "PartyPeer"):
            found.append(mod_path)
    assert found, (
        "`PartyPeer` type not found in any of the expected modules: "
        f"{candidates}. Playtest 3 identity drift (Blutka he/him → she/her "
        "across saves) will remain until a canonical peer-identity type "
        "exists."
    )


def test_party_peer_has_required_identity_fields():
    """The type must carry the five canonical identity fields called out
    in the session AC: ``name``, ``pronouns``, ``race``, ``char_class``,
    ``level``. Perception-layer fields (mood, tactics, stance) MUST NOT be
    required on the type — perception stays POV.
    """
    from tests.server.test_party_peer_identity import _find_party_peer_cls

    cls = _find_party_peer_cls()
    # pydantic BaseModel or dataclass — both expose field names; fall back
    # to constructor probing if neither is detectable.
    required = {"name", "pronouns", "race", "char_class", "level"}
    field_names: set[str] = set()
    if hasattr(cls, "model_fields"):  # pydantic v2
        field_names = set(cls.model_fields.keys())
    elif hasattr(cls, "__dataclass_fields__"):
        field_names = set(cls.__dataclass_fields__.keys())
    else:
        # Best-effort: try constructing from kwargs and inspect __dict__.
        try:
            inst = cls(name="x", pronouns="x/x", race="x", char_class="x", level=1)
            field_names = set(vars(inst).keys())
        except TypeError as exc:  # pragma: no cover - diagnostic path
            pytest.fail(f"PartyPeer constructor does not accept the required fields: {exc}")
    missing = required - field_names
    assert not missing, (
        f"PartyPeer missing required canonical fields: {sorted(missing)}. "
        "These are the fields the narrator needs to keep Blutka a he/him "
        "Orc Rogue across sealed-letter turns."
    )


def _find_party_peer_cls():
    """Locate the PartyPeer class wherever Dev put it."""
    for mod_path in (
        "sidequest.game.session",
        "sidequest.game.party",
        "sidequest.game.character",
        "sidequest.agents.orchestrator",
    ):
        try:
            mod = __import__(mod_path, fromlist=["PartyPeer"])
        except ImportError:
            continue
        if hasattr(mod, "PartyPeer"):
            return mod.PartyPeer
    raise AssertionError("PartyPeer not found — see test_party_peer_type_is_importable")


def test_party_peer_from_character_round_trips_identity():
    """There must be a way to derive a ``PartyPeer`` from a ``Character``.
    Either a ``from_character`` classmethod or a ``to_party_peer`` method,
    or a module-level helper — any is fine; the important invariant is that
    the canonical fields round-trip without loss.
    """
    cls = _find_party_peer_cls()
    blutka = _blutka()

    # Try classmethod first, then module-level helper fallbacks.
    peer = None
    if hasattr(cls, "from_character"):
        peer = cls.from_character(blutka)
    else:
        for mod_path in (
            "sidequest.game.session",
            "sidequest.game.party",
            "sidequest.game.character",
        ):
            try:
                mod = __import__(mod_path, fromlist=["party_peer_from_character"])
            except ImportError:
                continue
            if hasattr(mod, "party_peer_from_character"):
                peer = mod.party_peer_from_character(blutka)
                break
    assert peer is not None, (
        "No PartyPeer.from_character classmethod or party_peer_from_character "
        "helper found. Dev must provide a lossless Character→PartyPeer "
        "conversion so the injector has a single source of truth."
    )

    assert peer.name == "Blutka"
    assert peer.pronouns == "he/him"
    assert peer.race == "Orc"
    assert peer.char_class == "Rogue"
    assert peer.level == 3


# ---------------------------------------------------------------------------
# AC-2: TurnContext field + _build_turn_context population
# ---------------------------------------------------------------------------


def test_turn_context_has_party_peers_field():
    """``TurnContext`` must expose a ``party_peers`` collection field with
    an empty default (so single-player turns pay zero cost).
    """
    from dataclasses import fields

    tc_fields = {f.name: f for f in fields(TurnContext)}
    assert "party_peers" in tc_fields, (
        "TurnContext missing `party_peers` field. The orchestrator has "
        "nowhere to receive peer identity from _build_turn_context."
    )


def test_build_turn_context_populates_party_peers_in_multiplayer(sd_factory) -> None:
    """With Blutka as the acting player and Orin as the party-mate,
    ``_build_turn_context`` must hand the orchestrator an Orin peer packet.
    """
    from sidequest.server.session_handler import _build_turn_context

    sd = sd_factory([_blutka(), _orin()], acting_player="Blutka")
    ctx = _build_turn_context(sd, room=_make_mp_room(playing_count=2))

    peers = getattr(ctx, "party_peers", None)
    assert peers is not None, "TurnContext.party_peers was not populated"
    assert len(peers) == 1, (
        f"Expected 1 peer (Orin), got {len(peers)}. The acting player's "
        "own character must be excluded from party_peers — otherwise the "
        "dossier tells the narrator 'you are Blutka' twice."
    )
    orin_peer = peers[0]
    assert orin_peer.name == "Orin"
    assert orin_peer.pronouns == "they/them"
    assert orin_peer.race == "Human"
    assert orin_peer.char_class == "Fighter"
    assert orin_peer.level == 3


def test_build_turn_context_excludes_acting_player_from_peers(sd_factory) -> None:
    """The acting player's own PC must never appear as a peer. If it did,
    the narrator prompt would describe the acting player in third person
    alongside themselves — the exact behaviour we are eliminating.
    """
    from sidequest.server.session_handler import _build_turn_context

    sd = sd_factory([_blutka(), _orin()], acting_player="Blutka")
    ctx = _build_turn_context(sd, room=_make_mp_room(playing_count=2))

    peer_names = {p.name for p in ctx.party_peers}
    assert "Blutka" not in peer_names, (
        "Acting player's own character landed in party_peers — "
        "violates 'self is not a peer' invariant."
    )


def test_build_turn_context_solo_session_has_no_peers(sd_factory) -> None:
    """Solo session (1 character) must produce an empty party_peers list.
    This is the zero-byte-leak precondition: the injector will skip rendering
    the dossier section when the list is empty."""
    from sidequest.server.session_handler import _build_turn_context

    sd = sd_factory([_blutka()], acting_player="Blutka")
    ctx = _build_turn_context(sd)
    assert ctx.party_peers == [], (
        f"Solo session produced non-empty party_peers: {ctx.party_peers!r}"
    )


# ---------------------------------------------------------------------------
# AC-3: Narrator prompt injection (mirrors npc_roster pattern from 37-44)
# ---------------------------------------------------------------------------


async def _build_prompt_with_peers(peers):
    orch = _make_orchestrator()
    context = TurnContext(
        character_name="Blutka",
        genre="caverns_and_claudes",
        party_peers=list(peers),
    )
    return await orch.build_narrator_prompt("look around", context)


async def test_party_peer_dossier_renders_as_prompt_section():
    """When ``TurnContext.party_peers`` is non-empty, the built prompt must
    include a dossier listing each peer. This is the root-cause fix for
    peer identity drift: without this section, the narrator re-guesses
    Blutka's pronouns every turn in Orin's save.
    """
    cls = _find_party_peer_cls()
    orin_peer = cls.from_character(_orin())
    prompt, _ = await _build_prompt_with_peers([orin_peer])
    assert "Orin" in prompt, (
        "Peer dossier not injected: 'Orin' missing from prompt even though "
        "party_peers=[Orin]. This is the playtest-3 identity drift."
    )


async def test_party_peer_dossier_contains_canonical_pronouns():
    """Canonical pronouns must reach the narrator — the whole point of the
    subsystem. Blutka went from he/him to she/her because these never
    landed in Orin's prompt."""
    cls = _find_party_peer_cls()
    blutka_peer = cls.from_character(_blutka())
    # Build prompt from Orin's POV with Blutka as the peer.
    orch = _make_orchestrator()
    context = TurnContext(
        character_name="Orin",
        genre="caverns_and_claudes",
        party_peers=[blutka_peer],
    )
    prompt, _ = await orch.build_narrator_prompt("I look at my friend", context)
    assert "he/him" in prompt, (
        "Canonical pronouns missing from prompt — exact playtest-3 failure "
        "(Blutka he/him drifted to she/her in Orin's save)."
    )


async def test_party_peer_dossier_contains_canonical_race_and_class():
    """Race and class must round-trip into the prompt — without them the
    narrator will re-describe a dwarf as a halfling under any drift pressure.
    """
    cls = _find_party_peer_cls()
    orin_peer = cls.from_character(_orin())
    prompt, _ = await _build_prompt_with_peers([orin_peer])
    lowered = prompt.lower()
    assert "human" in lowered, "Canonical race (Human) missing from prompt"
    assert "fighter" in lowered, "Canonical class (Fighter) missing from prompt"


async def test_empty_party_peers_produces_no_dossier_section():
    """Zero-byte leak: an empty peers list must produce NO party dossier
    section. Solo sessions are the common case and must pay nothing for
    this subsystem. Parallels the 37-44 npc_roster discipline.
    """
    orch = _make_orchestrator()
    context = TurnContext(
        character_name="Blutka",
        genre="caverns_and_claudes",
        party_peers=[],
    )
    _, registry = await orch.build_narrator_prompt("look around", context)

    agent_name = orch._narrator.name()
    section_names = {s.name for s in registry.registry(agent_name)}
    # The exact section name is Dev's choice; rule out the three likely
    # candidates so the zero-byte discipline is enforced regardless of
    # naming.
    for candidate in ("party_peer_roster", "party_peers", "party_roster"):
        assert candidate not in section_names, (
            f"Empty party_peers still produced `{candidate}` section — "
            "violates zero-byte-leak discipline."
        )


async def test_party_peer_section_uses_early_or_valley_zone():
    """Peer identity is reference data — Early or Valley (mirrors NPC
    roster at 37-44). Primacy is reserved for acute genre/identity rules.
    """
    cls = _find_party_peer_cls()
    orin_peer = cls.from_character(_orin())
    orch = _make_orchestrator()
    context = TurnContext(
        character_name="Blutka",
        genre="caverns_and_claudes",
        party_peers=[orin_peer],
    )
    _, registry = await orch.build_narrator_prompt("look around", context)

    agent_name = orch._narrator.name()
    peer_sections = [
        s
        for s in registry.registry(agent_name)
        if "party" in s.name.lower()
        and "peer" in s.name.lower()
        or s.name.lower() in ("party_peer_roster", "party_peers", "party_roster")
    ]
    assert len(peer_sections) >= 1, (
        "No section with a `party`/`peer` name was registered. The dossier "
        "did not land in the prompt registry."
    )
    zone = peer_sections[0].zone
    assert zone in (AttentionZone.Early, AttentionZone.Valley), (
        f"Party peer section zone={zone!r} — should be Early or Valley "
        "(reference data), not Primacy."
    )


async def test_party_peer_section_is_state_category():
    """Peer dossier is current-world-state, not genre/identity/format."""
    cls = _find_party_peer_cls()
    orin_peer = cls.from_character(_orin())
    orch = _make_orchestrator()
    context = TurnContext(
        character_name="Blutka",
        genre="caverns_and_claudes",
        party_peers=[orin_peer],
    )
    _, registry = await orch.build_narrator_prompt("look around", context)

    agent_name = orch._narrator.name()
    peer_sections = [
        s
        for s in registry.registry(agent_name)
        if s.name.lower() in ("party_peer_roster", "party_peers", "party_roster")
        or ("party" in s.name.lower() and "peer" in s.name.lower())
    ]
    assert peer_sections, "No party peer section registered"
    assert peer_sections[0].category == SectionCategory.State, (
        f"Party peer section category={peer_sections[0].category!r} — should "
        "be State (current party composition), not Identity / Genre / Format."
    )


async def test_multiple_peers_all_rendered_with_their_pronouns():
    """A real party has 3–4 PCs. Every non-self peer must make it into the
    dossier with the correct pronouns. Dropping a peer silently is the
    Frandrew failure mode ported to players.
    """
    cls = _find_party_peer_cls()
    peers = [
        cls.from_character(
            _make_character("Blutka", pronouns="he/him", race="Orc", char_class="Rogue")
        ),
        cls.from_character(
            _make_character("Orin", pronouns="they/them", race="Human", char_class="Fighter")
        ),
        cls.from_character(
            _make_character("Vessa", pronouns="she/her", race="Elf", char_class="Ranger")
        ),
    ]
    orch = _make_orchestrator()
    context = TurnContext(
        character_name="Felix",
        genre="caverns_and_claudes",
        party_peers=peers,
    )
    prompt, _ = await orch.build_narrator_prompt("look around", context)

    for name in ("Blutka", "Orin", "Vessa"):
        assert name in prompt, f"{name} missing from multi-peer dossier"
    assert "he/him" in prompt
    assert "they/them" in prompt
    assert "she/her" in prompt


async def test_party_peer_dossier_omits_perception_layer_fields():
    """AC-5: physical identity is canonical; perception (mood, tactics,
    feelings) is POV and MUST NOT appear in the dossier. If Dev ever adds
    a ``mood`` or ``stance`` field to PartyPeer, this test will surface
    the violation before it ships.

    We check for two specific perception-layer words that would signal
    this class of leakage: ``mood``, ``disposition``. The dossier may
    legitimately contain ``level`` (mechanical) and identity fields only.
    """
    cls = _find_party_peer_cls()
    orin_peer = cls.from_character(_orin())
    prompt, _ = await _build_prompt_with_peers([orin_peer])

    # Locate the peer dossier block specifically to avoid false positives
    # from other prompt sections (e.g. a genre block mentioning "mood").
    # Look for a line containing Orin and scan the surrounding 10 lines.
    lines = prompt.splitlines()
    orin_line = next((i for i, ln in enumerate(lines) if "Orin" in ln), None)
    assert orin_line is not None, "Orin not found in prompt — prerequisite failed"
    window = "\n".join(lines[max(0, orin_line - 2) : orin_line + 6]).lower()
    for forbidden in ("mood:", "disposition:", "stance:", "feelings:"):
        assert forbidden not in window, (
            f"Peer dossier leaked perception-layer field `{forbidden}` — "
            "physical identity must stay canonical; perception stays POV."
        )


# ---------------------------------------------------------------------------
# AC-4: OTEL observability (parallel to npc.auto_registered / npc.reinvented)
# ---------------------------------------------------------------------------


def test_party_peer_injection_span_is_defined_in_catalog():
    """Per the OTEL Observability Principle in CLAUDE.md, every subsystem
    fix must emit OTEL so the GM panel can tell the subsystem engaged
    vs. Claude improvising. Party peer injection needs a dedicated span
    so Sebastien's mechanic-first GM view can verify it fired.
    """
    from sidequest.telemetry import spans as spans_module

    assert hasattr(spans_module, "SPAN_ORCHESTRATOR_PARTY_PEER_INJECTION"), (
        "SPAN_ORCHESTRATOR_PARTY_PEER_INJECTION missing from telemetry "
        "catalog — without it the GM panel can't tell whether peer identity "
        "injection ran this turn, or whether Claude is faking Blutka's "
        "pronouns from guesswork."
    )
    assert (
        spans_module.SPAN_ORCHESTRATOR_PARTY_PEER_INJECTION == "orchestrator.party_peer_injection"
    ), (
        "Span name must be exactly 'orchestrator.party_peer_injection' for "
        "the GM panel filter to match sibling orchestrator spans."
    )


async def test_party_peer_injection_logs_span_event(caplog, monkeypatch):
    """When the dossier is injected, a structured log event with the span
    name must fire at INFO level. Mirrors the pattern at
    ``orchestrator.py:791`` (``orchestrator.genre_identity_injection``).
    """
    # app.py disables propagation on the sidequest logger at import time;
    # re-enable so pytest's caplog captures.
    monkeypatch.setattr(logging.getLogger("sidequest"), "propagate", True)

    cls = _find_party_peer_cls()
    orin_peer = cls.from_character(_orin())

    with caplog.at_level(logging.INFO):
        await _build_prompt_with_peers([orin_peer])

    assert "orchestrator.party_peer_injection" in caplog.text, (
        "Peer injection produced no `orchestrator.party_peer_injection` "
        "event. GM panel filter won't see it fire. CLAUDE.md lie-detector "
        "rule: narration without OTEL is Claude improvising."
    )
    # party_size must be in the event so the panel can distinguish a
    # 2-player session from a 5-player session.
    assert "party_size=1" in caplog.text or "peer_count=1" in caplog.text, (
        "Span event fired without party size attribute — GM panel cannot "
        "tell how many peers were injected this turn."
    )


async def test_party_peer_injection_span_does_not_fire_on_empty_peers(caplog, monkeypatch):
    """Zero-byte discipline also applies to OTEL: no peers → no span. A
    span per turn on every solo session would pollute the GM panel with
    vacuous events.
    """
    monkeypatch.setattr(logging.getLogger("sidequest"), "propagate", True)

    orch = _make_orchestrator()
    context = TurnContext(
        character_name="Blutka",
        genre="caverns_and_claudes",
        party_peers=[],
    )
    with caplog.at_level(logging.INFO):
        await orch.build_narrator_prompt("look around", context)
    assert "orchestrator.party_peer_injection" not in caplog.text, (
        "Empty party_peers still fired the injection span — vacuous "
        "telemetry will swamp solo-session GM panels."
    )


# ---------------------------------------------------------------------------
# AC-6: Wiring seams (CLAUDE.md — every test suite needs a wiring test)
# ---------------------------------------------------------------------------


async def test_wiring_sd_to_prompt_delivers_peer_identity(sd_factory):
    """End-to-end wire (CLAUDE.md: every test suite needs a wiring test):
    ``_SessionData`` with two characters → ``_build_turn_context`` →
    ``Orchestrator.build_narrator_prompt`` → a dedicated peer dossier
    section is registered with Blutka's canonical he/him identity.
    Exercises the complete path that failed in playtest 3.

    Note: we assert against the PromptRegistry structure, not bare
    substrings of the rendered prompt. ``state_summary`` already
    serializes the whole snapshot to JSON, which means peer field values
    will happen to appear in the rendered prompt regardless of whether
    the dossier subsystem is live. The registry check is what proves the
    subsystem wired end-to-end.
    """
    from sidequest.server.session_handler import _build_turn_context

    # Orin is the acting player; Blutka is the peer whose pronouns drifted.
    sd = sd_factory([_orin(), _blutka()], acting_player="Orin")
    ctx = _build_turn_context(sd, room=_make_mp_room(playing_count=2))

    # Precondition: _build_turn_context actually produced a peer entry.
    peers = getattr(ctx, "party_peers", None)
    assert peers and len(peers) == 1 and peers[0].name == "Blutka", (
        "End-to-end wire broken at _build_turn_context: Blutka did not "
        "survive into TurnContext.party_peers."
    )

    orch = _make_orchestrator()
    _, registry = await orch.build_narrator_prompt("I glance at Blutka", ctx)

    agent_name = orch._narrator.name()
    sections = registry.registry(agent_name)
    peer_sections = [
        s
        for s in sections
        if s.name.lower() in ("party_peer_roster", "party_peers", "party_roster")
        or ("party" in s.name.lower() and "peer" in s.name.lower())
    ]
    assert peer_sections, (
        "End-to-end wire broken: TurnContext carried Blutka as a peer but "
        "no dedicated peer dossier section was registered on the prompt. "
        "(The snapshot JSON state_summary will contain Blutka's data "
        "regardless — a registry check is the only way to verify the "
        "dossier subsystem is live.)"
    )
    section_body = peer_sections[0].content
    assert "Blutka" in section_body, (
        "End-to-end wire broken: peer dossier section registered but missing Blutka."
    )
    assert "he/him" in section_body, (
        "End-to-end wire broken: Blutka's canonical pronouns (he/him) did "
        "not reach the peer dossier — exact playtest-3 failure (he/him "
        "drifted to she/her in Orin's save)."
    )
    assert "orc" in section_body.lower(), (
        "End-to-end wire broken: Blutka's canonical race (Orc) missing "
        "from peer dossier in Orin's POV."
    )
