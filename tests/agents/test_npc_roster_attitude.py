"""RED tests for Story 50-12: narrator NPC roster emits the attitude band.

Story title: "Disposition: narrator NPC serialization emits attitude string
(close the agents-see-only-attitude gap)".

The actual gap (verified against code, not the speculative session ACs):

``register_npc_roster_section`` in ``agents/prompt_framework/core.py`` is
THE proactive, always-on narrator-facing NPC serialization — the
"gaslight discipline" roster the narrator sees every turn
(``world_materialization._apply_npc`` → ``snap.npcs`` →
``register_npc_roster_section``). Today it emits each stateful NPC's
name / pronouns / appearance / last-seen location but NOT the
qualitative disposition band. The narrator therefore cannot tell at a
glance whether the bartender is friendly or hostile without a separate
per-NPC ``query_npc`` round-trip — that is the "agents-see-only-[names]"
gap this story closes.

Spec corrections locked by these tests (see TEA deviations in the
session file — the session ACs were authored by sm-setup without
reading the code):

  * The session AC-1/AC-5 specify a fictional FIVE-tier capitalised
    ``Literal["Hostile","Guarded","Neutral","Trusting","Allied"]``.
    The real ``Attitude`` enum shipped by the 50-10 dependency is
    THREE-tier lowercase ``friendly`` / ``neutral`` / ``hostile`` and
    its module docstring declares those the *stable wire contract*.
    Tests assert the real enum.
  * The session root cause ("the narrator-facing serialization path
    never wired the attitude string output") is wrong: ``query_npc``
    already emits ``attitude`` (query_npc.py:109). The untouched seam
    is the *proactive roster*, tested here.
  * ``snap.npcs`` is ``list[Npc]`` (pydantic models) rendered into a
    text section, not a dict — assertions target the rendered
    ``npc_roster`` section content.

Perception-firewall paranoia (ADR-104/105 + disposition.py doctrine
"the world-state agent reasons in numbers; the narrator reasons in
attitudes"): the roster is the broadcast-layer, always-on prompt. It
must carry ONLY the coarsened band, never the raw ``disposition.value``
integer. A 3-point "add a string" story must not regress the firewall.

``register_npc_roster_section`` emits the band as a
``[attitude: <band>]`` tag on each stateful-NPC roster line
(`test_pool_only_members_get_no_attitude_token` is an intentional
invariant guard — pool-only members never acquire a band).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from sidequest.agents.claude_client import ClaudeClient
from sidequest.agents.orchestrator import Orchestrator, TurnContext
from sidequest.agents.prompt_framework.core import PromptRegistry
from sidequest.agents.prompt_framework.types import (
    AttentionZone,
    SectionCategory,
)
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.disposition import Attitude, Disposition
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.session import Npc

_AGENT = "narrator"
_BANDS = {m.value for m in Attitude}  # {"friendly", "neutral", "hostile"}


# ---------------------------------------------------------------------------
# Fixtures (mirror tests/agents/tools/test_query_npc.py::_npc)
# ---------------------------------------------------------------------------


def _npc(
    name: str,
    *,
    disposition: int = 0,
    pronouns: str | None = "they/them",
    appearance: str | None = "weathered cloak",
    last_seen_location: str | None = "Tavern",
) -> Npc:
    core = CreatureCore(
        name=name,
        description="A quiet figure.",
        personality="watchful",
        inventory=Inventory(items=[], gold=0),
        statuses=[],
        hp=HpPool(current=4, max=4, base_max=4),
    )
    return Npc(
        core=core,
        disposition=Disposition(disposition),
        location="Tavern",
        last_seen_location=last_seen_location,
        last_seen_turn=2,
        pronouns=pronouns,
        appearance=appearance,
    )


def _roster_content(*, npcs: list[Npc], pool: list[NpcPoolMember] | None = None) -> str:
    """Register the roster section and return its rendered text."""
    registry = PromptRegistry()
    registry.register_npc_roster_section(
        _AGENT,
        npc_pool=pool or [],
        npcs=npcs,
    )
    sections = [s for s in registry.registry(_AGENT) if s.name == "npc_roster"]
    assert len(sections) == 1, f"expected exactly one npc_roster section, got {len(sections)}"
    return sections[0].content


def _band_in(content: str) -> str:
    """Extract the single attitude band token present in the roster text.

    Asserts exactly one band literal appears (one NPC fixtures), so a Dev
    that emits the raw int or a fictional label fails loudly rather than
    silently matching nothing.
    """
    present = sorted(b for b in _BANDS if b in content)
    assert len(present) == 1, (
        f"expected exactly one Attitude band literal in roster, found {present!r}\n"
        f"--- roster content ---\n{content}"
    )
    return present[0]


# ---------------------------------------------------------------------------
# AC-1: roster emits the coarsened attitude band per stateful NPC
# ---------------------------------------------------------------------------


def test_roster_emits_attitude_for_friendly_npc() -> None:
    content = _roster_content(npcs=[_npc("Bram", disposition=25)])
    assert "friendly" in content, (
        "npc_roster does not carry the attitude band — the narrator sees "
        "Bram's name but not that he is friendly (the 50-12 gap)."
    )


def test_roster_emits_attitude_for_neutral_npc() -> None:
    content = _roster_content(npcs=[_npc("Bram", disposition=0)])
    assert "neutral" in content


def test_roster_emits_attitude_for_hostile_npc() -> None:
    content = _roster_content(npcs=[_npc("Bram", disposition=-40)])
    assert "hostile" in content


def test_roster_attitude_tracks_disposition_attitude_derivation() -> None:
    """The band must be derived from ``Disposition.attitude()`` at render
    time, not a hardcoded duplicate mapping. Couple the assertion to the
    single source of truth so a divergent reimplementation fails."""
    npc = _npc("Bram", disposition=25)
    content = _roster_content(npcs=[npc])
    assert npc.disposition.attitude().value in content
    assert _band_in(content) == npc.disposition.attitude().value


# ---------------------------------------------------------------------------
# AC-1 spot-check / ADR-020 strict boundaries (>10 friendly, <-10 hostile)
# A 2-point delta across the boundary must flip the rendered band.
# ---------------------------------------------------------------------------


def test_roster_boundary_value_10_is_neutral_not_friendly() -> None:
    content = _roster_content(npcs=[_npc("Edge", disposition=10)])
    assert _band_in(content) == "neutral"


def test_roster_boundary_value_11_is_friendly() -> None:
    content = _roster_content(npcs=[_npc("Edge", disposition=11)])
    assert _band_in(content) == "friendly"


def test_roster_boundary_value_neg10_is_neutral_not_hostile() -> None:
    content = _roster_content(npcs=[_npc("Edge", disposition=-10)])
    assert _band_in(content) == "neutral"


def test_roster_boundary_value_neg11_is_hostile() -> None:
    content = _roster_content(npcs=[_npc("Edge", disposition=-11)])
    assert _band_in(content) == "hostile"


# ---------------------------------------------------------------------------
# AC-4 + perception firewall (ADR-104/105): coarsened band ONLY.
# The always-on roster must never leak the raw disposition integer.
# ---------------------------------------------------------------------------


def test_roster_does_not_leak_raw_disposition_integer() -> None:
    """Disposition 37 → 'friendly'. The literal '37' must NOT appear: the
    broadcast-layer roster is narrator-facing; raw scores are world-state-
    agent-only (disposition.py doctrine). Leaking it here is the exact
    firewall breach ADR-105 guards."""
    content = _roster_content(npcs=[_npc("Sly", disposition=37)])
    assert "friendly" in content
    assert "37" not in content, (
        "raw disposition integer leaked into the always-on narrator roster "
        "— perception firewall breach (ADR-104/105)."
    )


def test_roster_rejects_fictional_five_tier_labels() -> None:
    """Locks the spec correction: the session ACs' five-tier capitalised
    set is fictional. Real enum is three-tier lowercase. RED-coupled — the
    correct lowercase band must be present (fails now) AND none of the
    fictional/capitalised labels may appear (stays locked post-impl)."""
    content = _roster_content(npcs=[_npc("Bram", disposition=5)])
    assert "neutral" in content, "correct lowercase band 'neutral' absent — 50-12 not yet wired."
    for fictional in ("Guarded", "Trusting", "Allied", "Hostile", "Neutral", "Friendly"):
        assert fictional not in content, (
            f"roster emitted fictional/ capitalised label {fictional!r}; the "
            f"50-10 wire contract is lowercase {sorted(_BANDS)!r}."
        )


def test_roster_band_is_a_real_attitude_enum_member() -> None:
    """AC-5 (corrected): type-safety via the real ``Attitude`` enum, not a
    free string and not the fictional Literal."""
    content = _roster_content(npcs=[_npc("Bram", disposition=-3)])
    band = _band_in(content)
    assert band in _BANDS
    assert Attitude(band) is Attitude.NEUTRAL


# ---------------------------------------------------------------------------
# Scope guard: pool members are identity-only (no Disposition). They must
# NOT acquire a fabricated attitude — attitude is a stateful-Npc property.
# ---------------------------------------------------------------------------


def test_pool_only_members_get_no_attitude_token() -> None:
    pool = [
        NpcPoolMember(
            name="Vey",
            role="engineer",
            pronouns="he/him",
            appearance="grease-stained",
            drawn_from="legacy_registry",
        )
    ]
    content = _roster_content(npcs=[], pool=pool)
    assert "Vey" in content
    leaked = sorted(b for b in _BANDS if b in content)
    assert leaked == [], (
        f"identity-only pool member acquired a fabricated attitude {leaked!r}; "
        f"attitude derives from stateful Npc.disposition only."
    )


# ---------------------------------------------------------------------------
# AC-2 / AC-3: mandatory wiring test — the band reaches the actual built
# narrator prompt through the production orchestrator path, and lands in
# the dashboard-visible npc_roster section (Early zone, State category).
# This is the AC-3 observability surface for a serialization enrichment:
# the prompt-zones dashboard already renders prompt sections to the GM
# panel (see test_prompt_zones_dashboard.py). See TEA deviation re: AC-3.
# ---------------------------------------------------------------------------


async def test_attitude_lands_in_built_narrator_prompt() -> None:
    """End-to-end through the production orchestrator path. Scoped to the
    npc_roster section (NOT a bare ``"hostile" in prompt`` — the word
    "hostile" occurs in genre/lethality boilerplate independent of NPC
    attitude; a whole-prompt substring check is vacuous)."""
    client = MagicMock(spec=ClaudeClient)
    orch = Orchestrator(client=client)
    context = TurnContext(
        character_name="Felix",
        genre="space_opera",
        npcs=[_npc("Murchison", disposition=-50)],
    )
    _, registry = await orch.build_narrator_prompt("look around", context)
    agent_name = orch._narrator.name()
    roster = [s for s in registry.registry(agent_name) if s.name == "npc_roster"]
    assert len(roster) == 1, f"expected one npc_roster section, got {len(roster)}"
    section = roster[0].content
    assert "Murchison" in section, "roster NPC missing from npc_roster section"
    assert "hostile" in section, (
        "attitude band did not reach the npc_roster section via the "
        "production Orchestrator.build_narrator_prompt path — seam unwired."
    )
    assert "-50" not in section, "raw disposition leaked into the roster section"


async def test_wiring_attitude_is_carried_by_npc_roster_state_early_section() -> None:
    """The attitude must travel in the canonical ``npc_roster`` section so
    the prompt-zones dashboard (GM panel) surfaces it. Wrong zone/category
    would mean the narrator de-prioritises it over long sessions — the
    exact drift the roster section exists to prevent."""
    client = MagicMock(spec=ClaudeClient)
    orch = Orchestrator(client=client)
    context = TurnContext(
        character_name="Felix",
        genre="space_opera",
        npcs=[_npc("Murchison", disposition=60)],
    )
    _, registry = await orch.build_narrator_prompt("look around", context)
    agent_name = orch._narrator.name()
    roster = [s for s in registry.registry(agent_name) if s.name == "npc_roster"]
    assert len(roster) == 1, f"expected one npc_roster section, got {len(roster)}"
    section = roster[0]
    assert "friendly" in section.content, (
        "attitude band is not inside the npc_roster section — it must ride "
        "the canonical identity anchor, not a side section."
    )
    assert section.zone is AttentionZone.Early
    assert section.category is SectionCategory.State
