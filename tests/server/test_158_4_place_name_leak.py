"""Story 158-4: narrator place-names must not leak into the NPC roster.

sq-playtest 2026-06-22 (caverns_and_claudes/beneath_sunden, sub-bug (b) of the
NPC-roster finding): the narrator invented dwarfhold PLACE names ("Torchdeep",
"Torchhold") in prose and they reconciled into ``snapshot.npcs`` as phantom NPCs
(disp=0, creature_id=None) — entries with no creature backing standing in the
roster beside the real camp cast.

Root cause (mapped in the RED phase): the Step-3 "novel name" branch of
``_apply_npc_mentions`` (`sidequest/server/narration_apply.py`) already declines
the culture namer for two kinds of non-person mention — ``is_creature`` mentions
(``npc.creature_preserved``) and descriptive epithets (``npc.epithet_preserved``)
— but a bare proper-noun PLACE is neither. It falls through to the ``else``
person branch and is minted as an ``NpcPoolMember(drawn_from="narrator_invented")``
(narration_apply.py:3151-3170), which a later engagement/seeding promotes into
``snapshot.npcs``.

A bare "Torchdeep" is structurally indistinguishable from a bare person name
"Brecca": same empty role, neutral side, no pronouns. The DRIVER confirmed these
names are NOT in any beneath_sunden content file, so a known-location skip-set
cannot catch them. The only reliable discriminator is the same one the engine
already uses for creatures — a flag on the mention, set by the narrator's
structured emission / post-narration extractor. This RED pins:

* a new ``NpcMention.is_place`` flag (parallel to ``is_creature``), parsed by
  ``NpcMention.from_value`` so the narrator/extractor dict carries it through;
* a Step-3 PLACE guard that DECLINES the mint entirely — no ``NpcPoolMember``,
  no ``Npc`` — for a place mention (AC1/AC2);
* an OTEL ``npc.place_skipped`` decline span so the GM panel can verify the guard
  fired (AC3, the lie-detector — twin of ``npc.creature_preserved``);
* the guard is SURGICAL — a real person mention (``is_place=False``, the default)
  still mints (No Silent Fallbacks: the guard must never swallow real NPCs).

Test doctrine mirrors test_126_32_narrated_npc_binding.py: assert BEHAVIOR
(no phantom NPC) plus the OTEL CONTRACT, driving the REAL production seam
``_apply_npc_mentions``. Spans are referenced by string literal so a failure is
behavioral, never a collection-time ImportError. AC4 (live beneath_sunden
re-verify) is an out-of-band DRIVER check — it is intentionally not automated;
AC1 precludes a deterministic live-play repro (the leak is generation-variance).
"""

from __future__ import annotations

import pytest

from sidequest.agents.orchestrator import NpcMention
from sidequest.game.session import GameSnapshot
from sidequest.server.narration_apply import _apply_npc_mentions

# The two narrator-invented dwarfhold place-names the DRIVER caught in the roster.
PLACE_NAMES = ["Torchdeep", "Torchhold"]

# OTEL decline span — twin of npc.creature_preserved / npc.epithet_preserved.
# Referenced by string literal (test doctrine): a failure is behavioral, not a
# collection-time ImportError if the span constant is renamed.
PLACE_SKIPPED_SPAN = "npc.place_skipped"


def _mention(
    name: str,
    *,
    role: str = "",
    pronouns: str = "",
    appearance: str = "",
    side: str = "neutral",
    is_creature: bool = False,
    is_new: bool = False,
    is_place: bool = False,
) -> NpcMention:
    return NpcMention(
        name=name,
        role=role,
        pronouns=pronouns,
        appearance=appearance,
        side=side,
        is_creature=is_creature,
        is_new=is_new,
        is_place=is_place,
    )


def _attrs_for(otel_capture, span_name: str) -> list[dict]:
    return [
        dict(s.attributes or {}) for s in otel_capture.get_finished_spans() if s.name == span_name
    ]


# ===========================================================================
# AC1 + AC2 — a place mention is DECLINED at the reconcile, never minted
# ===========================================================================


@pytest.mark.parametrize("place_name", PLACE_NAMES)
def test_place_mention_is_not_minted_as_npc(place_name: str) -> None:
    """A narrator mention flagged as a place must NOT create an NPC entity —
    neither a pool member nor a promoted ``Npc``.

    Drives the real Step-3 novel-mint path with an empty snapshot (the name
    matches nothing in ``snapshot.npcs`` or ``snapshot.npc_pool``, so today it
    falls to the person mint and lands in the pool). With the guard, the place
    is declined and both stores stay empty.

    Deterministic — constructs the mention directly, no LLM (AC1).
    """
    snapshot = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")
    snapshot.character_locations["Groucho"] = "Ropefoot"

    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=[_mention(place_name, is_place=True)],
        turn_num=5,
        acting_character_name="Groucho",
    )

    minted = [m for m in snapshot.npc_pool if m.name.casefold() == place_name.casefold()]
    assert minted == [], (
        f"a place mention ({place_name!r}) must not mint an NpcPoolMember; "
        f"the phantom leaked into the pool: {[(m.name, m.drawn_from) for m in minted]!r}"
    )
    assert snapshot.npc_pool == [], (
        f"a place mention ({place_name!r}) must mint nothing at all; "
        f"pool: {[m.name for m in snapshot.npc_pool]!r}"
    )
    leaked_npcs = [n for n in snapshot.npcs if n.core.name.casefold() == place_name.casefold()]
    assert leaked_npcs == [], (
        f"a place mention ({place_name!r}) must never reach snapshot.npcs as a "
        f"phantom NPC (disp=0, creature_id=None); roster: "
        f"{[n.core.name for n in snapshot.npcs]!r}"
    )


# ===========================================================================
# AC3 — the decline emits an OTEL lie-detector span
# ===========================================================================


@pytest.mark.parametrize("place_name", PLACE_NAMES)
def test_place_mention_emits_skip_span(otel_capture, place_name: str) -> None:
    """The PLACE guard must emit ``npc.place_skipped`` so the GM panel can verify
    the engine declined the mint (per OTEL Observability Principle). A silent
    guard is a half-fix: you can't tell a working guard from a name that simply
    happened to draw clean.
    """
    snapshot = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")
    snapshot.character_locations["Groucho"] = "Ropefoot"

    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=[_mention(place_name, is_place=True)],
        turn_num=5,
        acting_character_name="Groucho",
    )

    spans = _attrs_for(otel_capture, PLACE_SKIPPED_SPAN)
    assert spans, (
        f"a {PLACE_SKIPPED_SPAN} span must fire when a place mention is declined "
        "(GM-panel lie detector)"
    )
    assert any(s.get("npc_name") == place_name for s in spans), (
        f"the {PLACE_SKIPPED_SPAN} span must record the declined place name "
        f"({place_name!r}); got {[s.get('npc_name') for s in spans]!r}"
    )


# ===========================================================================
# Negative guard — the place guard must be SURGICAL, not swallow real NPCs
# ===========================================================================


def test_person_mention_still_mints_when_not_a_place(otel_capture) -> None:
    """A normal person mention (``is_place`` defaults False) must STILL mint a
    pool member, and must NOT trip the place-skip span.

    This is the No-Silent-Fallbacks paranoia check: the new guard must fire ONLY
    on flagged places. If it ever swallowed a default (person) mention, the real
    camp cast would vanish from the roster — the opposite failure.
    """
    snapshot = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")
    snapshot.character_locations["Groucho"] = "Ropefoot"

    # No pack threaded → the raw narrator name is preserved verbatim (the
    # namegen reroute is not exercised here), so the pool member is named exactly.
    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=[_mention("Hargrave", pronouns="he/him", is_new=True)],
        turn_num=5,
        acting_character_name="Groucho",
    )

    minted = [m for m in snapshot.npc_pool if m.name == "Hargrave"]
    assert len(minted) == 1, (
        "a default (person) mention must still mint a pool member — the place "
        f"guard must not over-fire; pool: {[m.name for m in snapshot.npc_pool]!r}"
    )
    assert minted[0].drawn_from == "narrator_invented"
    assert _attrs_for(otel_capture, PLACE_SKIPPED_SPAN) == [], (
        "the place-skip span must NOT fire for a person mention (is_place=False)"
    )


# ===========================================================================
# Contract wiring — the place signal survives the dict→object boundary
# ===========================================================================


def test_npc_mention_from_value_parses_is_place() -> None:
    """``NpcMention.from_value`` must parse ``is_place`` from the structured dict
    the narrator / post-narration extractor emits.

    Without this, the producer could mark a place but the flag would be dropped
    at the parse boundary and the Step-3 guard would never see it — a silent
    wiring gap. Defaults to False so every existing mention stays a person
    (backward-compatible, twin of ``is_creature``).
    """
    place = NpcMention.from_value({"name": "Torchhold", "is_place": True})
    assert place.is_place is True, (
        "from_value must carry is_place=True through so the reconcile guard can fire"
    )

    person = NpcMention.from_value({"name": "Brecca Half-Hand"})
    assert person.is_place is False, "is_place must default False for an unflagged mention"

    bare = NpcMention.from_value("Brecca")
    assert bare.is_place is False, "the bare-string fallback must default is_place to False"
