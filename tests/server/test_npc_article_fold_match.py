"""Playtest 2026-06-20 (wry_whimsy/wonderland, WW-CANONICAL-NPC-NAMES-SHUFFLED):
the invented-name router renamed Carroll's fixed canonical characters.

Server log: ``npc.invented_name_routed original='Queen of Hearts'
minted='The Chorister' strategy='shuffle_fallback' turn=4`` — three names for one
antagonist in a single scene (narration "Queen of Hearts", router mint "The
Chorister", seated Other "The Queen of Diamonds").

Root cause: literary worlds (wonderland/oz/gulliver) author their canonical cast
in ``npcs.yaml`` with a leading definite article ("The Queen of Hearts", "The
White Rabbit", "The Five of Spades"), and ``preload_authored_npcs`` loads them
into ``snapshot.npcs`` from session start. But the narrator routinely cites them
bare ("Queen of Hearts"). Step 1 of ``_apply_npc_mentions`` matched only on
exact-casefold + comma-inversion — NOT the leading article — so the bare mention
missed the preloaded authored NPC, fell through to Step 3, and was minted a fresh
culture-shuffled identity. The procedural namer renamed a world-authored fixed
character.

The fix is definite-article-insensitive reconciliation in ``_npc_name_match_keys``
(the same matcher that already does comma-inversion folding), so a bare narrator
mention reconciles to the preloaded "The …" authored NPC instead of minting. Only
the *definite* article is folded — indefinite "a"/"an" precede generic
descriptors, not proper names, and folding them would collapse "a man" onto
"The Man".

These tests pin article-insensitive matching on both the roster (Step 1) and the
pool (Step 2), the honest ``article_normalized`` telemetry label, the
false-positive guard, and the end-to-end proof that a canonical match never
reaches the Step-3 generator (no ``shuffle_fallback``).
"""

from __future__ import annotations

from sidequest.agents.orchestrator import NpcMention
from sidequest.game.creature_core import CreatureCore
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.session import GameSnapshot, Npc
from sidequest.genre.names.generator import NameGenerator
from sidequest.server.narration_apply import _apply_npc_mentions

REFERENCED_SPAN = "npc.referenced"
ROUTED_SPAN = "npc.invented_name_routed"


def _core(name: str) -> CreatureCore:
    return CreatureCore(name=name, description="X.", personality="Y.")


def _mention(name: str, *, role: str = "", pronouns: str = "") -> NpcMention:
    return NpcMention(name=name, role=role, pronouns=pronouns, appearance="")


def _attrs_for(otel_capture, span_name: str) -> list[dict]:
    return [
        dict(s.attributes or {}) for s in otel_capture.get_finished_spans() if s.name == span_name
    ]


class _SeqNameGenerator(NameGenerator):
    """Deterministic stand-in that records whether ``generate_person`` was hit.

    The whole point of the canonical-match fix is that a preloaded authored NPC
    is reconciled at Step 1 and the Step-3 generator is *never reached* — so the
    strongest proof is ``person_calls == 0``.
    """

    def __init__(self, names: list[str]) -> None:
        super().__init__()
        self._iter = iter(names)
        self.person_calls = 0

    def generate_person(self, pattern: str | None = None) -> str:  # noqa: ARG002
        self.person_calls += 1
        return next(self._iter)


def test_the_prefixed_roster_matches_bare_mention() -> None:
    """``The Queen of Hearts`` on the roster ⇄ bare ``Queen of Hearts`` mention →
    npcs_hit, no phantom pool mint (the WW-CANONICAL repro)."""
    snap = GameSnapshot()
    snap.npcs = [Npc(core=_core("The Queen of Hearts"))]

    _apply_npc_mentions(
        snapshot=snap,
        mentions=[_mention("Queen of Hearts")],
        turn_num=4,
    )

    assert snap.npcs[0].last_seen_turn == 4, "the preloaded authored NPC must be matched"
    assert snap.npc_pool == [], (
        "a bare mention of a 'The …' authored NPC must reconcile to the roster, "
        f"not mint a phantom culture-shuffled identity; got {snap.npc_pool}"
    )


def test_bare_roster_matches_the_prefixed_mention() -> None:
    """Symmetry: a bare roster name ⇄ a ``The …`` mention also reconciles."""
    snap = GameSnapshot()
    snap.npcs = [Npc(core=_core("Queen of Hearts"))]

    _apply_npc_mentions(
        snapshot=snap,
        mentions=[_mention("The Queen of Hearts")],
        turn_num=5,
    )

    assert snap.npcs[0].last_seen_turn == 5
    assert snap.npc_pool == []


def test_the_prefixed_pool_member_matches_bare_mention() -> None:
    """``The Five of Spades`` in the pool ⇄ bare ``Five of Spades`` mention →
    pool upsert, no second member."""
    snap = GameSnapshot()
    snap.npc_pool = [NpcPoolMember(name="The Five of Spades", drawn_from="narrator_invented")]

    _apply_npc_mentions(
        snapshot=snap,
        mentions=[_mention("Five of Spades", role="gardener")],
        turn_num=6,
    )

    assert len(snap.npc_pool) == 1, (
        f"a 'The …' pool member must be matched, not duplicated; got {snap.npc_pool}"
    )
    assert snap.npc_pool[0].role == "gardener", "the upsert must land on the existing member"


def test_distinct_the_names_do_not_falsely_match() -> None:
    """No false positive: two distinct 'The …' NPCs differing past the article
    must not collapse. ``The Red Queen`` must not absorb ``Queen of Hearts``."""
    snap = GameSnapshot()
    snap.npcs = [Npc(core=_core("The Red Queen"))]

    _apply_npc_mentions(
        snapshot=snap,
        mentions=[_mention("Queen of Hearts")],
        turn_num=2,
    )

    assert any(m.name == "Queen of Hearts" for m in snap.npc_pool), (
        "an unrelated name must still mint — article-folding must not over-match"
    )
    assert snap.npcs[0].last_seen_turn != 2, "the distinct rostered NPC must not be touched"


def test_article_match_records_article_normalized_form(otel_capture) -> None:
    """OTEL lie-detector: an article-folded reconciliation is observable and
    honestly labelled — the ``npc.referenced`` span carries
    ``match_form="article_normalized"`` (not the comma label) so the GM panel can
    see the canonical-NPC rescue fired."""
    snap = GameSnapshot()
    snap.npcs = [Npc(core=_core("The White Rabbit"))]

    _apply_npc_mentions(
        snapshot=snap,
        mentions=[_mention("White Rabbit")],
        turn_num=9,
    )

    refs = _attrs_for(otel_capture, REFERENCED_SPAN)
    npcs_hits = [a for a in refs if a.get("match_strategy") == "npcs_hit"]
    assert npcs_hits, "an article-folded roster match must still fire npc.referenced/npcs_hit"
    assert any(a.get("match_form") == "article_normalized" for a in npcs_hits), (
        f"the reconciliation must be span-visible as article_normalized; got {npcs_hits}"
    )


def test_canonical_match_skips_generator_no_invented_route(otel_capture) -> None:
    """End-to-end anti-``shuffle_fallback`` proof: with a culture generator in
    context (Step 3 *would* mint), a bare mention of a preloaded 'The …' authored
    NPC reconciles at Step 1 — the generator is never called and no
    ``npc.invented_name_routed`` span fires. This is the literal bug: the namer
    must not rename a world-authored fixed character."""
    gen = _SeqNameGenerator(["The Chorister"])
    snap = GameSnapshot()
    snap.npcs = [Npc(core=_core("The Queen of Hearts"))]

    _apply_npc_mentions(
        snapshot=snap,
        mentions=[_mention("Queen of Hearts")],
        turn_num=4,
        name_generator=gen,
        culture_name="Victorian English",
        culture_source="world",
    )

    assert gen.person_calls == 0, "a canonical-NPC match must never reach the Step-3 generator"
    assert snap.npc_pool == [], "no phantom pool member may be minted for a canonical NPC"
    assert _attrs_for(otel_capture, ROUTED_SPAN) == [], (
        "no npc.invented_name_routed/shuffle_fallback span may fire when the "
        "narrator cites a preloaded authored NPC by its bare name"
    )
    assert snap.npcs[0].last_seen_turn == 4


def test_exact_match_still_exact_form(otel_capture) -> None:
    """Regression guard: an exact match still works and is tagged
    ``match_form="exact"`` (article-folding didn't perturb the happy path)."""
    snap = GameSnapshot()
    snap.npcs = [Npc(core=_core("The Queen of Hearts"))]

    _apply_npc_mentions(
        snapshot=snap,
        mentions=[_mention("The Queen of Hearts")],
        turn_num=1,
    )

    refs = _attrs_for(otel_capture, REFERENCED_SPAN)
    npcs_hits = [a for a in refs if a.get("match_strategy") == "npcs_hit"]
    assert npcs_hits
    assert any(a.get("match_form") == "exact" for a in npcs_hits)
