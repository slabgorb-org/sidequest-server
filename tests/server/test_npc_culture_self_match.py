"""RED — Story 83-2: culture self-match — named people-group resolves to its own culture.

The bug: ``_resolve_invented_naming_context`` (narration_apply.py) shuffles the
world's bound cultures randomly and returns the first one whose corpus builds.
A narrator-named people-group (e.g. "The Munchkins") therefore gets assigned a
RANDOM culture — "Roric the Crimson" minted with culture=Quadling is the
canonical symptom from the Oz playtest.

The fix: when the narrator names a specific people/group/clan, match the mention
name against the bound cultures (by culture name + any authored aliases/demonyms)
and resolve to the SELF-MATCHED culture deterministically. Fall back to the
existing shuffle ONLY when there is no match (a genuinely unaffiliated stranger).

Test-design contract (pinned by TEA, session 83-2):

* ``_resolve_invented_naming_context`` gains a ``mention_name: str | None``
  parameter that it uses as the probe for self-matching BEFORE the shuffle loop.
* Matching must reuse ``alias_resolution._phrase_matches`` word-boundary discipline
  (not a forked matcher) — phrase appears word-bounded and case-insensitively.
  Singular/plural tolerance ("Munchkin" ~ "Munchkins") is required.
* ``Culture`` must grow an ``aliases: list[str]`` field (default []) so content
  authors can declare authored demonyms/plural forms rather than relying on the
  engine to guess them.
* The ``npc.invented_name_routed`` span must record two new attributes:
  - ``resolution_strategy``: "self_match" | "shuffle_fallback"
  - ``matched_token``: the culture name token that matched (empty string on shuffle)
* No-Silent-Fallbacks (AC5): a self-matched culture whose corpus fails (raises
  FileNotFoundError/ValueError in ``build_from_culture``) must fire the loud namegen
  guard and/or degrade via ``npc.invented_name_unrouted`` — never silently continue
  to a different culture.

RED failure modes in this suite:
  1. TypeError — tests call ``_resolve_invented_naming_context(pack, world,
     mention_name=...)``; the current signature has no ``mention_name`` kwarg.
  2. AssertionError — ``npc.invented_name_routed`` span lacks ``resolution_strategy``
     / ``matched_token`` attributes (new OTEL requirement).
  3. AssertionError — with the shuffle pinned so Quadling is always first, a
     "Munchkin" mention still resolves to "Quadling" (self-match not implemented).
  4. ValidationError — ``Culture(name="Munchkin", ..., aliases=[...])`` raises because
     the ``aliases`` field does not yet exist on the model (``extra="forbid"``).
  5. AssertionError — when the self-matched culture's corpus fails, the engine
     silently falls through to another culture instead of degrading loud (AC5).

Alias-resolution reuse recommendation (for Dev): the ``alias_resolution.py``
module (ADR-118) already exposes ``_phrase_matches(phrase, text)`` — word-bounded
(``\\b``), case-insensitive. The self-match logic should reuse this function directly
rather than forking its own ``re.search(rf"\\b{re.escape(name)}\\b", text, re.I)``
pattern. This avoids two matchers drifting apart. The one wrinkle: word-boundary
will NOT match "Munchkin" inside "Munchkins" (because ``s`` is a word character).
Handle this via the ``aliases`` list (content authors add plural forms) OR via a
small engine-side plural heuristic in ``_resolve_invented_naming_context`` — TEA
recommends the aliases approach (content controls, not engine guessing) but leaves
the decision to Dev.

Span names and attribute keys are asserted as **string literals** so this file
collects cleanly while those strings do not yet exist in production spans.
"""

from __future__ import annotations

import random as stdlib_random
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sidequest.agents.orchestrator import NarrationTurnResult, NpcMention
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.culture import Culture, CultureSlot
from sidequest.genre.models.pack import GenrePack
from sidequest.server.narration_apply import _apply_npc_mentions, _resolve_invented_naming_context
from tests._helpers.session_room import room_for

# ---------------------------------------------------------------------------
# Span name literals — asserted as strings so tests collect RED
# ---------------------------------------------------------------------------

ROUTED_SPAN = "npc.invented_name_routed"
UNROUTED_SPAN = "npc.invented_name_unrouted"

# Paths for the end-to-end wiring test
CONTENT_GENRE_PACKS = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"
OZ_WORLD_DIR = CONTENT_GENRE_PACKS / "wry_whimsy"


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_munchkin_culture() -> Culture:
    """Minimal Munchkin culture with word_list-only slots (no corpus I/O)."""
    return Culture(
        name="Munchkin",
        summary="The small, blue-clad farming folk of the East.",
        description="Munchkins of the East Country.",
        person_patterns=["{given_name}", "{given_name} {family_name}"],
        slots={
            "given_name": CultureSlot(word_list=["Boq", "Bini", "Nan", "Pell", "Tobble"]),
            "family_name": CultureSlot(word_list=["Bluefield", "Roundroof", "Cobblepot"]),
        },
    )


def _make_quadling_culture() -> Culture:
    """Minimal Quadling culture with word_list-only slots (no corpus I/O)."""
    return Culture(
        name="Quadling",
        summary="The red-clad southern folk of Glinda.",
        description="Quadlings of the South Country.",
        person_patterns=["{given_name}", "{given_name} {family_name}"],
        slots={
            "given_name": CultureSlot(word_list=["Glinda", "Rosamund", "Ruby", "Roric", "Coral"]),
            "family_name": CultureSlot(word_list=["Redgate", "Poppyfield", "Crimsonby"]),
        },
    )


def _mock_pack_with_cultures(cultures: list[Culture]) -> MagicMock:
    """MagicMock(spec=GenrePack) with effective_cultures returning the given list.

    ``source_dir`` is set to a non-None but non-existent path — safe for
    word_list-only cultures (no corpus file I/O in build_from_culture).
    """
    pack = MagicMock(spec=GenrePack)
    pack.source_dir = Path("/tmp/sidequest-test-nonexistent")
    pack.effective_cultures.return_value = (cultures, "world")
    return pack


def _mention(name: str, *, is_creature: bool = False) -> NpcMention:
    return NpcMention(name=name, is_creature=is_creature)


def _result(narration: str, npcs_present: list[NpcMention]) -> NarrationTurnResult:
    return NarrationTurnResult(
        narration=narration,
        npcs_present=list(npcs_present),
        is_degraded=False,
    )


def _attrs_for(otel_capture, span_name: str) -> list[dict]:
    return [
        dict(s.attributes or {}) for s in otel_capture.get_finished_spans() if s.name == span_name
    ]


def _pin_shuffle_quadling_first(lst: list) -> None:
    """Reverse-alphabetic sort: Quadling before Munchkin before Emerald etc.

    Used to guarantee that the CURRENT shuffle-first code resolves to Quadling,
    making assertions on culture=="Munchkin" fail deterministically RED.
    """
    lst.sort(key=lambda c: c.name, reverse=True)


# ===========================================================================
# AC1 — Culture model: aliases field
# ===========================================================================


def test_culture_model_has_aliases_field() -> None:
    """Culture must declare an ``aliases`` field (list[str], default []) for
    content authors to supply plural forms and demonyms.

    RED: ``Culture`` has no ``aliases`` field — this raises AttributeError.
    The fix: add ``aliases: list[str] = Field(default_factory=list)`` to Culture.
    """
    munchkin = _make_munchkin_culture()
    # aliases must exist and default to empty — ``extra="forbid"`` means
    # AttributeError if the field is not declared on the model.
    aliases = munchkin.aliases  # noqa: B018 — AttributeError if field absent  # type: ignore[attr-defined]
    assert isinstance(aliases, list), (
        f"Culture.aliases must be a list; got {type(aliases).__name__}"
    )


def test_culture_can_be_constructed_with_aliases() -> None:
    """Culture construction must accept ``aliases`` without Pydantic ValidationError.

    RED: ``Culture(name=..., aliases=[...])`` raises
    ``pydantic.ValidationError: extra fields not permitted`` because the field
    does not yet exist on the model.
    """
    # This will raise ValidationError (extra="forbid") until aliases is added.
    culture = Culture(  # type: ignore[call-arg]
        name="Munchkin",
        summary="The blue-clad folk.",
        description="Munchkins of the East.",
        person_patterns=["{given_name}"],
        slots={"given_name": CultureSlot(word_list=["Boq", "Bini"])},
        aliases=["Munchkins", "Little Folk"],  # ← the new field being tested
    )
    assert "Munchkins" in culture.aliases  # type: ignore[attr-defined]
    assert "Little Folk" in culture.aliases  # type: ignore[attr-defined]


# ===========================================================================
# AC1 — _resolve_invented_naming_context: mention_name parameter
# ===========================================================================


def test_resolve_naming_context_accepts_mention_name_kwarg() -> None:
    """``_resolve_invented_naming_context`` must accept a ``mention_name`` kwarg.

    RED: TypeError — the function has no such parameter yet.
    """
    pack = _mock_pack_with_cultures([_make_munchkin_culture()])
    # This must not raise TypeError once the parameter is added.
    _gen, _culture, _source, _unresolved = _resolve_invented_naming_context(
        pack,
        "oz",
        mention_name="Munchkin",  # type: ignore[call-arg]
    )


def test_resolve_naming_context_self_match_returns_matching_culture() -> None:
    """When mention_name matches a bound culture by name, that culture is returned.

    RED: TypeError (missing mention_name param) — once param added, will fail
    because the shuffle loop ignores mention_name and may return Quadling.
    """
    munchkin = _make_munchkin_culture()
    quadling = _make_quadling_culture()
    pack = _mock_pack_with_cultures([munchkin, quadling])

    # Pin the shuffle so Quadling is always first — current code would return Quadling.
    with patch.object(stdlib_random, "shuffle", _pin_shuffle_quadling_first):
        _gen, culture_name, _source, unresolved = _resolve_invented_naming_context(
            pack,
            "oz",
            mention_name="Munchkin",  # type: ignore[call-arg]
        )

    assert not unresolved, "self-match must not set naming_unresolved"
    assert culture_name == "Munchkin", (
        f"mention_name='Munchkin' must resolve to culture 'Munchkin', not {culture_name!r}. "
        "The function is ignoring mention_name and returning the shuffle-first culture."
    )


def test_resolve_naming_context_plural_mention_matches_singular_culture() -> None:
    """A plural mention ('The Munchkins') must self-match the singular culture 'Munchkin'.

    RED: TypeError (missing param); once param added, fails because plural
    form 'Munchkins' does not word-boundary-match 'Munchkin' (no \\b after k),
    requiring either authored aliases or engine-side plural handling.
    """
    munchkin = _make_munchkin_culture()
    quadling = _make_quadling_culture()
    pack = _mock_pack_with_cultures([munchkin, quadling])

    with patch.object(stdlib_random, "shuffle", _pin_shuffle_quadling_first):
        _gen, culture_name, _source, _unresolved = _resolve_invented_naming_context(
            pack,
            "oz",
            mention_name="The Munchkins",  # type: ignore[call-arg]
        )

    assert culture_name == "Munchkin", (
        f"plural mention 'The Munchkins' must resolve to culture 'Munchkin', "
        f"not {culture_name!r}. Singular/plural tolerance is required (AC2)."
    )


def test_resolve_naming_context_case_insensitive() -> None:
    """Matching must be case-insensitive: 'the munchkins' → 'Munchkin'.

    RED: TypeError (missing param); once added, may fail on case sensitivity.
    """
    munchkin = _make_munchkin_culture()
    quadling = _make_quadling_culture()
    pack = _mock_pack_with_cultures([munchkin, quadling])

    with patch.object(stdlib_random, "shuffle", _pin_shuffle_quadling_first):
        _gen, culture_name, _source, _unresolved = _resolve_invented_naming_context(
            pack,
            "oz",
            mention_name="the munchkins",  # type: ignore[call-arg]
        )

    assert culture_name == "Munchkin", (
        f"case-insensitive match 'the munchkins' must resolve to 'Munchkin', got {culture_name!r}"
    )


def test_resolve_naming_context_no_match_falls_back_to_shuffle() -> None:
    """An unmatched mention_name leaves the shuffle behavior intact.

    This is the AC3 regression guard — the shuffle fallback must be preserved.
    The function is called with a mention_name that matches NO culture by name;
    the returned culture must be one of the valid bound cultures (not None/unresolved).

    RED: TypeError (missing param). Once param added, this test should PASS
    (existing shuffle behavior is preserved). Included as a regression guard.
    """
    munchkin = _make_munchkin_culture()
    quadling = _make_quadling_culture()
    pack = _mock_pack_with_cultures([munchkin, quadling])

    _gen, culture_name, _source, unresolved = _resolve_invented_naming_context(
        pack,
        "oz",
        mention_name="Roric the Crimson",  # type: ignore[call-arg]
    )

    assert not unresolved, "an unmatched stranger should still get a shuffled culture"
    assert culture_name in {"Munchkin", "Quadling"}, (
        f"unmatched mention must still return a valid culture; got {culture_name!r}"
    )


# ===========================================================================
# AC2 — authored alias match
# ===========================================================================


def test_resolve_naming_context_alias_match() -> None:
    """An authored alias on a culture resolves a mention that doesn't match the name.

    RED: ValidationError (Culture has no 'aliases' field) OR TypeError
    (missing mention_name param).
    """
    # Once Culture.aliases is added, this construction must succeed.
    munchkin = Culture(  # type: ignore[call-arg]
        name="Munchkin",
        summary="The blue-clad folk.",
        description="Munchkins of the East.",
        person_patterns=["{given_name}"],
        slots={"given_name": CultureSlot(word_list=["Boq", "Bini", "Nan"])},
        aliases=["Munchkins", "Little Folk"],  # authored aliases
    )
    quadling = _make_quadling_culture()
    pack = _mock_pack_with_cultures([munchkin, quadling])

    with patch.object(stdlib_random, "shuffle", _pin_shuffle_quadling_first):
        _gen, culture_name, _source, _unresolved = _resolve_invented_naming_context(
            pack,
            "oz",
            mention_name="The Little Folk",  # type: ignore[call-arg]
        )

    assert culture_name == "Munchkin", (
        f"authored alias 'Little Folk' in mention 'The Little Folk' must resolve to "
        f"culture 'Munchkin', not {culture_name!r}"
    )


def test_resolve_naming_context_alias_plural_authored() -> None:
    """A culture with 'Munchkins' in aliases must match a 'Munchkins' mention.

    RED: ValidationError (aliases not a field) OR TypeError (missing mention_name).
    """
    munchkin = Culture(  # type: ignore[call-arg]
        name="Munchkin",
        summary="The blue-clad folk.",
        description="Munchkins of the East.",
        person_patterns=["{given_name}"],
        slots={"given_name": CultureSlot(word_list=["Boq", "Bini", "Nan"])},
        aliases=["Munchkins"],  # plural form as alias
    )
    quadling = _make_quadling_culture()
    pack = _mock_pack_with_cultures([munchkin, quadling])

    with patch.object(stdlib_random, "shuffle", _pin_shuffle_quadling_first):
        _gen, culture_name, _source, _unresolved = _resolve_invented_naming_context(
            pack,
            "oz",
            mention_name="The Munchkins",  # type: ignore[call-arg]
        )

    assert culture_name == "Munchkin", (
        f"alias 'Munchkins' must match mention 'The Munchkins' → culture 'Munchkin'; "
        f"got {culture_name!r}"
    )


# ===========================================================================
# AC4 — OTEL: resolution_strategy + matched_token attributes
# ===========================================================================


def test_otel_routed_span_records_self_match_strategy(otel_capture, monkeypatch) -> None:
    """The npc.invented_name_routed span must include resolution_strategy='self_match'
    and a non-empty matched_token when the culture was found by name/alias match.

    RED: AssertionError — the current span has no resolution_strategy or matched_token
    attributes.
    """
    from sidequest.genre.names.generator import NameGenerator

    munchkin = _make_munchkin_culture()
    quadling = _make_quadling_culture()
    pack = _mock_pack_with_cultures([munchkin, quadling])

    # Pin shuffle: Quadling first — proves the self-match, not the shuffle order,
    # determines the culture.
    monkeypatch.setattr(stdlib_random, "shuffle", _pin_shuffle_quadling_first)
    # Patch generate_person so we get a crisp deterministic name.
    monkeypatch.setattr(
        NameGenerator, "generate_person", lambda self, pattern=None: "Boq Bluefield"
    )

    snapshot = GameSnapshot()
    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=[_mention("Munchkin")],
        turn_num=5,
        pack=pack,
        world="oz",
    )

    routed = _attrs_for(otel_capture, ROUTED_SPAN)
    assert len(routed) == 1, f"exactly one {ROUTED_SPAN} span must fire; got {len(routed)}"
    attrs = routed[0]

    # AC4 — new attributes required by 83-2
    assert "resolution_strategy" in attrs, (
        "npc.invented_name_routed span must record 'resolution_strategy'; "
        "attribute is missing. Dev must add it to npc_invented_name_routed_span()."
    )
    assert attrs["resolution_strategy"] == "self_match", (
        f"culture matched by name must record resolution_strategy='self_match'; "
        f"got {attrs.get('resolution_strategy')!r}"
    )
    assert "matched_token" in attrs, (
        "npc.invented_name_routed span must record 'matched_token'; attribute is missing."
    )
    assert attrs["matched_token"], "matched_token must be non-empty on a self-match resolution"
    # Culture must be the matched one, not the shuffle-first one
    assert attrs.get("culture") == "Munchkin", (
        f"self-match must resolve to 'Munchkin', got culture={attrs.get('culture')!r}"
    )


def test_otel_routed_span_records_shuffle_fallback_strategy(otel_capture, monkeypatch) -> None:
    """When no culture matches the mention name, npc.invented_name_routed must record
    resolution_strategy='shuffle_fallback' and matched_token=''.

    RED: AssertionError — the current span has no resolution_strategy attribute.
    """
    from sidequest.genre.names.generator import NameGenerator

    munchkin = _make_munchkin_culture()
    quadling = _make_quadling_culture()
    pack = _mock_pack_with_cultures([munchkin, quadling])
    monkeypatch.setattr(
        NameGenerator, "generate_person", lambda self, pattern=None: "Roric Redgate"
    )

    snapshot = GameSnapshot()
    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=[_mention("Roric the Crimson")],  # matches no culture name
        turn_num=3,
        pack=pack,
        world="oz",
    )

    routed = _attrs_for(otel_capture, ROUTED_SPAN)
    assert len(routed) == 1, f"exactly one {ROUTED_SPAN} span must fire; got {len(routed)}"
    attrs = routed[0]

    assert "resolution_strategy" in attrs, (
        "npc.invented_name_routed span must always record 'resolution_strategy'"
    )
    assert attrs["resolution_strategy"] == "shuffle_fallback", (
        f"unmatched mention must record resolution_strategy='shuffle_fallback'; "
        f"got {attrs.get('resolution_strategy')!r}"
    )
    assert "matched_token" in attrs, (
        "npc.invented_name_routed span must always record 'matched_token'"
    )
    assert attrs["matched_token"] == "", (
        f"matched_token must be '' on shuffle_fallback; got {attrs.get('matched_token')!r}"
    )


# ===========================================================================
# AC3 — shuffle fallback regression guard
# ===========================================================================


def test_unmatched_mention_still_mints_a_valid_culture_name(otel_capture, monkeypatch) -> None:
    """AC3 regression guard: an unmatched stranger still gets a culture-bound generated name.

    This path should continue to work after the self-match logic is added.
    RED: TypeError (missing mention_name param on _resolve_invented_naming_context,
    called indirectly via _apply_npc_mentions with pack=).
    Once param added, this should PASS — it protects the shuffle fallback.
    """
    from sidequest.genre.names.generator import NameGenerator

    munchkin = _make_munchkin_culture()
    pack = _mock_pack_with_cultures([munchkin])
    monkeypatch.setattr(
        NameGenerator, "generate_person", lambda self, pattern=None: "Nan Cobblepot"
    )

    snapshot = GameSnapshot()
    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=[_mention("A Wandering Stranger")],  # matches no culture
        turn_num=1,
        pack=pack,
        world="oz",
    )

    assert len(snapshot.npc_pool) == 1
    member = snapshot.npc_pool[0]
    assert member.name == "Nan Cobblepot", (
        f"unmatched stranger must still be culture-routed; got {member.name!r}"
    )
    assert member.drawn_from == "narrator_invented"

    routed = _attrs_for(otel_capture, ROUTED_SPAN)
    assert len(routed) == 1, "shuffle-fallback path must still emit the provenance span"


# ===========================================================================
# AC5 — No-Silent-Fallbacks: self-matched culture with broken corpus
# ===========================================================================


def test_self_matched_culture_with_broken_corpus_does_not_silently_shuffle(
    otel_capture, monkeypatch
) -> None:
    """When a culture is self-matched but its corpus fails to build, the engine must
    NOT silently fall through to a different culture.

    Current behavior (BUGGY): the ``except (FileNotFoundError, ValueError): continue``
    loop silently tries the next culture. With self-match, if 'Munchkin' matched but
    its build_from_culture raises, the engine must degrade loud via npc.invented_name_unrouted
    (or an equivalent loud signal) rather than silently minting the name from Quadling.

    RED: AssertionError — the current code silently falls through to Quadling, minting
    a Quadling-culture name without any warning span about the broken Munchkin corpus.
    """
    from sidequest.genre.names import generator as namegen_module

    munchkin = _make_munchkin_culture()
    quadling = _make_quadling_culture()
    pack = _mock_pack_with_cultures([munchkin, quadling])

    # Monkeypatch shuffle so self-match code can be tested deterministically
    monkeypatch.setattr(stdlib_random, "shuffle", _pin_shuffle_quadling_first)

    def _build_raises_for_munchkin(culture: Culture, corpus_dir, **kwargs):  # type: ignore[return]
        if culture.name == "Munchkin":
            raise ValueError("Corpus 'given_name' for Munchkin has 0 words; minimum is 20")
        # Quadling would succeed — but the test asserts we never reach here
        from sidequest.genre.names.generator import build_from_culture as _real

        return _real(culture, corpus_dir, **kwargs)

    monkeypatch.setattr(namegen_module, "build_from_culture", _build_raises_for_munchkin)

    snapshot = GameSnapshot()
    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=[_mention("Munchkin")],  # self-matches Munchkin
        turn_num=7,
        pack=pack,
        world="oz",
    )

    # The engine must degrade loud when the self-matched culture's corpus is broken.
    # It must NOT silently mint a Quadling-culture name.
    unrouted = _attrs_for(otel_capture, UNROUTED_SPAN)
    assert len(unrouted) == 1, (
        "when the self-matched culture's corpus fails, the engine must emit "
        f"{UNROUTED_SPAN!r} (loud degrade), not silently route to another culture; "
        f"got {len(unrouted)} unrouted spans. "
        "Current code silently continues to the next culture — this is the AC5 bug."
    )
    assert unrouted[0].get("severity") == "warning", (
        "npc.invented_name_unrouted must have severity='warning'"
    )

    # The routed span must NOT fire — no culture was successfully resolved.
    routed = _attrs_for(otel_capture, ROUTED_SPAN)
    assert routed == [], (
        "npc.invented_name_routed must NOT fire when the self-matched culture's "
        "corpus is broken — that would falsely claim a successful culture route"
    )


# ===========================================================================
# Wiring test — real oz pack, end-to-end
# ===========================================================================

pytestmark_oz = pytest.mark.skipif(
    not OZ_WORLD_DIR.exists(),
    reason="sidequest-content/genre_packs/wry_whimsy not checked out",
)


@pytestmark_oz
def test_wiring_munchkin_mention_resolves_to_munchkin_culture_end_to_end(
    otel_capture, monkeypatch
) -> None:
    """MANDATORY WIRING TEST (CLAUDE.md "Every Test Suite Needs a Wiring Test").

    Drive the real ``_apply_narration_result_to_snapshot`` →
    ``_apply_npc_mentions`` → ``_resolve_invented_naming_context`` production
    path with the real ``wry_whimsy`` pack and world ``oz``. A mention of
    "Munchkin" must resolve to the Munchkin culture, NOT a random Oz culture.

    RED: The ROUTED span records culture="Quadling" (or Winkie, or Emerald) because
    the current code shuffles randomly. Once the self-match fix lands, this must
    always record culture="Munchkin".

    To guarantee deterministic RED, the shuffle is pinned so Quadling is first —
    the current code therefore always returns Quadling.
    """
    from sidequest.genre import load_genre_pack
    from sidequest.genre.names.generator import NameGenerator
    from sidequest.server.session_handler import _apply_narration_result_to_snapshot

    monkeypatch.setattr(stdlib_random, "shuffle", _pin_shuffle_quadling_first)
    monkeypatch.setattr(
        NameGenerator, "generate_person", lambda self, pattern=None: "Boq Bluefield"
    )

    pack = load_genre_pack(OZ_WORLD_DIR)
    snapshot = GameSnapshot(genre_slug="wry_whimsy", world_slug="oz")
    result = _result(
        narration="A group of short, blue-clad farmers approaches.",
        npcs_present=[_mention("Munchkin")],
    )

    _apply_narration_result_to_snapshot(
        snapshot,
        result,
        "player",
        room=room_for(snapshot, slug="oz"),
        pack=pack,
        world="oz",
        acting_character_name=None,
    )

    routed = _attrs_for(otel_capture, ROUTED_SPAN)
    assert len(routed) == 1, (
        f"exactly one {ROUTED_SPAN} span must fire end-to-end; got {len(routed)}"
    )
    assert routed[0].get("culture") == "Munchkin", (
        "WIRING FAILURE: mention 'Munchkin' in Oz must resolve to culture='Munchkin', "
        f"got culture={routed[0].get('culture')!r}. "
        "The self-match fix is not wired into the production path."
    )
    # AC4 — production path must also emit the new attributes
    assert routed[0].get("resolution_strategy") == "self_match", (
        f"end-to-end self-match must record resolution_strategy='self_match'; "
        f"got {routed[0].get('resolution_strategy')!r}"
    )


@pytestmark_oz
def test_wiring_plural_munchkins_resolves_to_munchkin_culture_end_to_end(
    otel_capture, monkeypatch
) -> None:
    """WIRING: plural form 'The Munchkins' must resolve to culture='Munchkin' in Oz.

    RED: Resolves to Quadling (shuffle pinned) instead of Munchkin.
    """
    from sidequest.genre import load_genre_pack
    from sidequest.genre.names.generator import NameGenerator
    from sidequest.server.session_handler import _apply_narration_result_to_snapshot

    monkeypatch.setattr(stdlib_random, "shuffle", _pin_shuffle_quadling_first)
    monkeypatch.setattr(
        NameGenerator, "generate_person", lambda self, pattern=None: "Nan Roundroof"
    )

    pack = load_genre_pack(OZ_WORLD_DIR)
    snapshot = GameSnapshot(genre_slug="wry_whimsy", world_slug="oz")
    result = _result(
        narration="The Munchkins gather in their blue square.",
        npcs_present=[_mention("The Munchkins")],
    )

    _apply_narration_result_to_snapshot(
        snapshot,
        result,
        "player",
        room=room_for(snapshot, slug="oz"),
        pack=pack,
        world="oz",
        acting_character_name=None,
    )

    routed = _attrs_for(otel_capture, ROUTED_SPAN)
    assert len(routed) == 1, f"exactly one {ROUTED_SPAN} span must fire; got {len(routed)}"
    assert routed[0].get("culture") == "Munchkin", (
        "WIRING FAILURE: plural mention 'The Munchkins' in Oz must resolve to "
        f"culture='Munchkin'; got {routed[0].get('culture')!r}. "
        "Singular/plural tolerance is required by AC2."
    )
