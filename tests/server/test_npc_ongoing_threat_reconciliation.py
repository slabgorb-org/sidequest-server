"""RED-phase contract for story 83-3 — ongoing-threat identity stability.

Deferred from ping-pong #74 (origin session: wry_whimsy/oz 2026-06-03). A single
ongoing threat was re-minted under a NEW identity every turn: the narrator
re-describes it each turn ("Clemence Coralfast" -> "The Unseen Watcher" ->
"Keeper Goldbraid") and each novel descriptor misses the *name-only* match in
Steps 1/2 of ``_apply_npc_mentions`` and falls through to the Step-3 novel-mint
branch. None of the descriptors reconciled to the authored Cowardly Lion already
on the roster (disposition 5). Result: the GM panel and the player see three
monsters where there is one, and a hand-authored NPC is shadowed by phantom
duplicates.

== The gap these tests pin ==

``_apply_npc_mentions`` (narration_apply.py:1771) matches a mention against the
roster (Step 1) and the pool (Step 2) by NAME ONLY (exact casefold + the
comma-inversion key set). A creature the narrator re-describes under a fresh
descriptor each turn therefore never matches an existing entity and mints a new
``NpcPoolMember`` at Step 3 every turn. There is no reconciliation guard that
resolves a recurring *threat* to its existing pool member or authored ``Npc``.

== Levers (story names three; the impl may use any combination) ==

1. continuity flag  — the mention points at an existing entity (NpcMention
                      already carries ``is_new``; the narrator sets it False for
                      recurring NPCs, and it is consumed by render_trigger but
                      NOT by the matching pipeline).
2. similarity       — appearance/role reconciliation.
3. scene-guard      — "one active unnamed threat" per scene.

== Test doctrine (mechanism-agnostic) ==

These tests assert BEHAVIOR (the snapshot collapses re-descriptions to a single
identity; an authored creature Npc is not shadowed) plus the OTEL CONTRACT (a
reconciliation span fires recording the match + the signal used). They do NOT
assert *which* lever Dev chooses. To stay agnostic:

  * "should reconcile" inputs align ALL THREE signals — ``is_new=False``,
    role/appearance echoing the existing entity, and a single active creature in
    scene — so any conforming implementation reconciles.
  * "must NOT merge" inputs align the DISTINCT signals — ``is_new=True`` and a
    dissimilar appearance/role — so the guard stays conservative (AC4: two
    genuinely distinct threats in the same scene stay distinct, mirroring the
    comma-inversion false-positive precedent).

All tests drive the REAL production seam ``_apply_npc_mentions`` (called from
``_apply_narration_result_to_snapshot``, narration_apply.py:3842). The span is
referenced by string literal (not import) so RED-phase failures are behavioral,
never an ImportError at collection — the exact span string is the contract Dev
must satisfy (telemetry/spans/npc.py constant + Span.open + SPAN_ROUTES entry
for GM-panel visibility).
"""

from __future__ import annotations

from sidequest.agents.orchestrator import NpcMention
from sidequest.game.creature_core import CreatureCore
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.session import GameSnapshot, Npc
from sidequest.server.narration_apply import (
    _apply_npc_mentions,  # noqa: PLC2701 — driving the real production seam
)

# Span this story must introduce — the lie-detector proving the engine collapsed
# a re-described threat instead of minting a phantom. Referenced by string so
# the RED suite fails on behavior, not on a missing import.
SPAN_CREATURE_RECONCILED = "npc.creature_reconciled"
REFERENCED_SPAN = "npc.referenced"


# ---------------------------------------------------------------------------
# Builders (mirror tests/server/test_npc_comma_inversion_match.py idiom)
# ---------------------------------------------------------------------------


def _core(name: str) -> CreatureCore:
    return CreatureCore(name=name, description="X.", personality="Y.")


def _authored_lion() -> Npc:
    """The hand-authored Cowardly Lion already on the roster (disposition 5).

    A creature-shaped Npc carries a ``creature_id`` (no ``is_creature`` field
    exists on Npc — creature-ness is inferred from creature_id/threat_level).
    """
    return Npc(
        core=_core("Cowardly Lion"),
        creature_id="cowardly_lion",
        threat_level=2,
        disposition=5,
        appearance="a large, timid lion with a tangled mane",
    )


def _creature_mention(
    name: str,
    *,
    is_new: bool,
    role: str = "",
    appearance: str = "",
) -> NpcMention:
    return NpcMention(
        name=name,
        role=role,
        appearance=appearance,
        is_new=is_new,
        is_creature=True,
        side="opponent",
    )


def _creature_pool_members(snap: GameSnapshot) -> list[NpcPoolMember]:
    return [m for m in snap.npc_pool if m.is_creature]


def _attrs_for(otel_capture, span_name: str) -> list[dict]:
    return [
        dict(s.attributes or {}) for s in otel_capture.get_finished_spans() if s.name == span_name
    ]


# A recurring forest threat the narrator re-describes under three different
# descriptors across three turns — same role, same look, continuity-flagged.
_THREAT_ROLE = "forest predator"
_THREAT_LOOK = "a hulking shadow stalking between the trees"


# ===========================================================================
# AC-1 — a re-described recurring threat collapses to ONE persistent identity.
#        (NEW BEHAVIOR — FAILS today: each novel descriptor mints a new member.)
# ===========================================================================


def test_redescribed_threat_collapses_to_single_pool_member() -> None:
    """AC-1: the narrator re-describes one forest threat under a fresh descriptor
    each turn. With continuity (``is_new=False``), matching role/appearance, and
    a single active creature in scene, all three descriptions must reconcile to a
    SINGLE pool member — not mint three.

    FAILS today: Steps 1/2 match by name only, so each novel descriptor falls to
    the Step-3 novel-mint branch and appends a fresh ``NpcPoolMember``.
    """
    snap = GameSnapshot()

    # Turn 1 — first sighting: genuinely new, mints the canonical pool member.
    _apply_npc_mentions(
        snapshot=snap,
        mentions=[
            _creature_mention(
                "a snarling forest beast", is_new=True, role=_THREAT_ROLE, appearance=_THREAT_LOOK
            )
        ],
        turn_num=1,
    )
    # Turn 2 — re-described; continuity-flagged; same role/look; only threat here.
    _apply_npc_mentions(
        snapshot=snap,
        mentions=[
            _creature_mention(
                "the lurking predator", is_new=False, role=_THREAT_ROLE, appearance=_THREAT_LOOK
            )
        ],
        turn_num=2,
    )
    # Turn 3 — re-described again.
    _apply_npc_mentions(
        snapshot=snap,
        mentions=[
            _creature_mention(
                "the shadow that stalks", is_new=False, role=_THREAT_ROLE, appearance=_THREAT_LOOK
            )
        ],
        turn_num=3,
    )

    creatures = _creature_pool_members(snap)
    assert len(creatures) == 1, (
        "a single recurring threat the narrator re-described across three turns must "
        f"reconcile to ONE persistent pool identity, not mint one per descriptor; "
        f"got {len(creatures)}: {[m.name for m in creatures]}"
    )


# ===========================================================================
# AC-2 — an authored roster Npc is matched, not shadowed by a phantom.
#        (NEW BEHAVIOR — FAILS today: re-described name misses Step 1, mints.)
# ===========================================================================


def test_authored_creature_npc_not_shadowed_by_phantom() -> None:
    """AC-2: the Cowardly Lion is already on the roster (disposition 5). When the
    narrator references the same creature in prose under a different descriptor
    (continuity-flagged, appearance echoing the lion), it must reconcile to the
    authored Npc — no freshly-minted phantom pool member.

    FAILS today: "the trembling forest-beast" misses the name-only Step-1 match
    against "Cowardly Lion" and mints a phantom at Step 3, shadowing the author's
    NPC with a duplicate.
    """
    snap = GameSnapshot()
    snap.npcs = [_authored_lion()]

    _apply_npc_mentions(
        snapshot=snap,
        mentions=[
            _creature_mention(
                "the trembling forest-beast",
                is_new=False,
                role="cowardly lion",
                appearance="a large cowardly lion, mane tangled, cowering",
            )
        ],
        turn_num=4,
    )

    assert snap.npc_pool == [], (
        "referencing the authored Cowardly Lion under a re-described name must reconcile "
        f"to the roster Npc, not mint a phantom pool member; got {[m.name for m in snap.npc_pool]}"
    )
    assert len(snap.npcs) == 1 and snap.npcs[0].core.name == "Cowardly Lion", (
        "the authored Npc must remain the single identity for this creature"
    )
    assert snap.npcs[0].last_seen_turn == 4, (
        "reconciling to the authored Npc must stamp last_seen_turn (the creature was "
        f"referenced this turn); got {snap.npcs[0].last_seen_turn}"
    )


# ===========================================================================
# AC-3 — reconciliation is span-visible (the GM-panel lie-detector).
#        (NEW BEHAVIOR — FAILS today: no npc.creature_reconciled span exists.)
# ===========================================================================


def test_reconciliation_emits_creature_reconciled_span(otel_capture) -> None:
    """AC-3: when the reconciliation guard collapses a re-described threat, an
    OTEL span records the match — the incoming descriptor, the identity it
    reconciled to, and the signal used (continuity / similarity / scene-guard) —
    so the GM panel can confirm the engine collapsed the duplicate.

    FAILS today: no ``npc.creature_reconciled`` span is emitted because no
    reconciliation occurs (the second descriptor mints instead).
    """
    snap = GameSnapshot()
    _apply_npc_mentions(
        snapshot=snap,
        mentions=[
            _creature_mention(
                "a snarling forest beast", is_new=True, role=_THREAT_ROLE, appearance=_THREAT_LOOK
            )
        ],
        turn_num=1,
    )
    _apply_npc_mentions(
        snapshot=snap,
        mentions=[
            _creature_mention(
                "the lurking predator", is_new=False, role=_THREAT_ROLE, appearance=_THREAT_LOOK
            )
        ],
        turn_num=2,
    )

    reconciled = _attrs_for(otel_capture, SPAN_CREATURE_RECONCILED)
    assert reconciled, (
        f"reconciling a re-described threat must emit a {SPAN_CREATURE_RECONCILED!r} span; "
        "the GM panel cannot otherwise confirm the duplicate was collapsed"
    )
    attrs = reconciled[0]
    assert attrs.get("incoming") == "the lurking predator", (
        f"span must record the incoming descriptor that was reconciled away; got {attrs!r}"
    )
    assert attrs.get("reconciled_to") == "a snarling forest beast", (
        "span must record the surviving identity the descriptor reconciled to (the "
        f"turn-1 canonical name); got {attrs!r}"
    )
    signal = attrs.get("signal")
    assert isinstance(signal, str) and signal.strip(), (
        "span must record WHICH signal fired (continuity / similarity / scene-guard) "
        f"so the match is auditable, not a silent collapse; got signal={signal!r}"
    )


# ===========================================================================
# AC-4 — no false merges: two genuinely distinct threats stay distinct.
#        (CONSERVATISM GUARD — passes today; must keep passing after the guard.)
# ===========================================================================


def test_distinct_creatures_in_scene_stay_distinct() -> None:
    """AC-4: two genuinely distinct threats in the same scene — each flagged
    ``is_new=True`` with a dissimilar role/appearance — must remain two separate
    identities. The reconciliation guard must be conservative and never collapse
    distinct creatures into one (mirror the comma-inversion false-positive guard).

    Passes today (different names mint separately); pinned so an over-aggressive
    scene-guard ("one creature per scene") that ignores ``is_new`` and appearance
    cannot regress AC-4.
    """
    snap = GameSnapshot()
    _apply_npc_mentions(
        snapshot=snap,
        mentions=[
            _creature_mention(
                "a towering stone golem",
                is_new=True,
                role="construct",
                appearance="hewn from granite, runes glowing along its arms",
            )
        ],
        turn_num=1,
    )
    _apply_npc_mentions(
        snapshot=snap,
        mentions=[
            _creature_mention(
                "a darting needle-swarm",
                is_new=True,
                role="swarm",
                appearance="a shifting cloud of metallic stinging flies",
            )
        ],
        turn_num=2,
    )

    creatures = _creature_pool_members(snap)
    assert len(creatures) == 2, (
        "two genuinely distinct threats (distinct appearance + is_new=True) must stay "
        f"distinct; the guard must not over-merge. got {len(creatures)}: "
        f"{[m.name for m in creatures]}"
    )


def test_multiple_threats_redescription_reconciles_by_similarity(otel_capture) -> None:
    """AC-1 + AC-4 (similarity lever): when SEVERAL creatures are active in scene,
    a continuity-flagged re-description must reconcile to the matching one by
    role/appearance similarity — not mint a third, and not collapse onto the wrong
    threat. The scene-guard lever alone (one-active-threat) cannot resolve this;
    the similarity lever disambiguates.

    Two distinct creatures are minted (golem + needle-swarm); a third mention
    (``is_new=False``) re-describes the golem ("the granite sentinel", glowing
    granite runes). It must reconcile to the golem via ``signal="similarity"`` —
    no third pool member, and ``reconciled_to`` names the golem, not the swarm.
    """
    snap = GameSnapshot()
    _apply_npc_mentions(
        snapshot=snap,
        mentions=[
            _creature_mention(
                "a towering stone golem",
                is_new=True,
                role="construct",
                appearance="hewn from granite, runes glowing along its arms",
            )
        ],
        turn_num=1,
    )
    _apply_npc_mentions(
        snapshot=snap,
        mentions=[
            _creature_mention(
                "a darting needle-swarm",
                is_new=True,
                role="swarm",
                appearance="a shifting cloud of metallic stinging flies",
            )
        ],
        turn_num=2,
    )

    _apply_npc_mentions(
        snapshot=snap,
        mentions=[
            _creature_mention(
                "the granite sentinel",
                is_new=False,
                role="construct",
                appearance="a towering shape of glowing granite, runes along its arms",
            )
        ],
        turn_num=3,
    )

    creatures = _creature_pool_members(snap)
    assert len(creatures) == 2, (
        "a re-description in a multi-creature scene must reconcile to an existing threat, "
        f"not mint a third; got {len(creatures)}: {[m.name for m in creatures]}"
    )
    reconciled = _attrs_for(otel_capture, SPAN_CREATURE_RECONCILED)
    assert reconciled, "the multi-creature re-description must emit a reconciliation span"
    attrs = reconciled[0]
    assert attrs.get("reconciled_to") == "a towering stone golem", (
        f"the granite re-description must reconcile to the GOLEM, not the swarm; got {attrs!r}"
    )
    assert attrs.get("signal") == "similarity", (
        f"a multi-creature disambiguation must report signal='similarity'; got {attrs!r}"
    )


def test_distinct_creature_does_not_merge_into_authored_npc() -> None:
    """AC-4 (roster side): a genuinely new, dissimilar creature must NOT be
    reconciled onto an unrelated authored creature Npc just because one is
    already in scene. The Cowardly Lion must not absorb a giant spider.

    Passes today; pins that the guard's roster-side reconciliation stays
    appearance/continuity-aware, not "any creature collapses onto any roster
    creature".
    """
    snap = GameSnapshot()
    snap.npcs = [_authored_lion()]

    _apply_npc_mentions(
        snapshot=snap,
        mentions=[
            _creature_mention(
                "a giant cave spider",
                is_new=True,
                role="arachnid",
                appearance="a chitinous bulk with eight glistening legs",
            )
        ],
        turn_num=2,
    )

    assert any(m.name == "a giant cave spider" for m in snap.npc_pool), (
        "a genuinely distinct new creature must still mint — it must not be falsely "
        f"reconciled onto the authored Cowardly Lion. pool={[m.name for m in snap.npc_pool]}"
    )
    assert snap.npcs[0].last_seen_turn == 0, (
        "the unrelated authored Lion must not be touched by a distinct creature mention"
    )


# ===========================================================================
# AC-5 — existing match/ratification behavior is preserved (regression).
#        (Passes today; pins the person path + comma-inversion are untouched.)
# ===========================================================================


def test_distinct_named_persons_not_collapsed() -> None:
    """AC-5: the creature-reconciliation guard must not bleed into the person
    path. Two distinct named PERSON NPCs (``is_creature=False``) stay distinct —
    a new person still mints, the rostered person is untouched.

    Pins that Steps 1-3 person matching (incl. the comma-inversion keys) keeps
    working unchanged.
    """
    snap = GameSnapshot()
    snap.npcs = [Npc(core=_core("Denis Gilligan"))]

    _apply_npc_mentions(
        snapshot=snap,
        mentions=[NpcMention(name="Colonel Phill", is_creature=False, is_new=True)],
        turn_num=2,
    )

    assert any(m.name == "Colonel Phill" for m in snap.npc_pool), (
        "an unrelated new person must still mint — the creature guard must not over-match "
        f"persons. pool={[m.name for m in snap.npc_pool]}"
    )
    assert snap.npcs[0].last_seen_turn == 0, (
        "the rostered person NPC must not be touched by an unrelated person mention"
    )


def test_person_mention_never_fires_creature_reconcile_span(otel_capture) -> None:
    """AC-5 firewall: a PERSON mention (``is_creature=False``) must never fire the
    creature-reconciliation span, even when a creature is already in scene and the
    person is continuity-flagged. The guard is creature-scoped.

    Passes today (no span exists); pins the firewall after the guard lands so the
    creature path cannot leak onto persons.
    """
    snap = GameSnapshot()
    # A creature already in scene...
    _apply_npc_mentions(
        snapshot=snap,
        mentions=[
            _creature_mention(
                "a snarling forest beast", is_new=True, role=_THREAT_ROLE, appearance=_THREAT_LOOK
            )
        ],
        turn_num=1,
    )
    # ...and a continuity-flagged PERSON mention that must NOT be creature-reconciled.
    _apply_npc_mentions(
        snapshot=snap,
        mentions=[
            NpcMention(
                name="the hooded stranger",
                role="merchant",
                appearance="a cloaked figure",
                is_creature=False,
                is_new=False,
            )
        ],
        turn_num=2,
    )

    assert _attrs_for(otel_capture, SPAN_CREATURE_RECONCILED) == [], (
        "a person mention must never fire the creature-reconciliation span — the guard "
        "is creature-scoped (is_creature=True only)"
    )


# ===========================================================================
# Regression — the existing comma-inversion roster match still fires npcs_hit.
#        (Passes today; the new guard must not displace the name-match path.)
# ===========================================================================


def test_exact_name_creature_match_still_uses_existing_npcs_hit(otel_capture) -> None:
    """Regression: when the narrator DOES reuse the authored creature's exact name,
    the existing Step-1 ``npcs_hit`` path must still fire — the reconciliation guard
    is an ADDITION beneath name matching, not a replacement for it.
    """
    snap = GameSnapshot()
    snap.npcs = [_authored_lion()]

    _apply_npc_mentions(
        snapshot=snap,
        mentions=[
            _creature_mention(
                "Cowardly Lion", is_new=False, role="cowardly lion", appearance="a timid lion"
            )
        ],
        turn_num=6,
    )

    refs = _attrs_for(otel_capture, REFERENCED_SPAN)
    assert any(a.get("match_strategy") == "npcs_hit" for a in refs), (
        "an exact-name creature mention must still reconcile via the existing Step-1 "
        f"npcs_hit path, not the new reconciliation guard; got {refs}"
    )
    assert snap.npc_pool == [], "exact-name match must not mint a phantom"
