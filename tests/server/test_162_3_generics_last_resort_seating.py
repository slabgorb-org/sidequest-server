"""Story 162-3 — bestiary generics replace ephemeral stub minting at the seater.

The last strategy of the opponent-seating stack (survey §4 conflict #2) is the
ephemeral stub mint (``encounter_lifecycle._seed_combat_hp_depletion_to_npcs``,
the 108-2 fabrication branch). 162-3 ends it:

  * When a router-named opponent resolves to NEITHER a roster entry NOR a
    scene-active pool antagonist, the seater draws the Other from the world's
    authored bestiary ``generics:`` section — the sanctioned last-resort source
    (origin precedence: authored > room-bound > region-population > MM pool >
    GENERICS > error). The seat is stamped ``Origin(kind=GENERIC,
    creature_id=<row id>)`` and carries the ROW's authored stats (SOUL "Bind
    the Ruleset, Don't Balance It" — same rule as the 108-2 bound-creature HP
    preserve: authored stat blocks are the balanced math; the confrontation's
    generic ``opponent_default_stats`` must not shadow them).

  * When no generics are available either, stub fabrication becomes a LOUD
    failure on the default (non-degenerate) path: raise, seat nothing, append
    nothing, and emit the refusal span (No Silent Fallbacks).

  * Degenerate callers (test fixtures, one-off scenario generation — the story
    context's explicit carve-out) may OPT IN via an explicit keyword to the old
    warn-and-mint behavior; the mint stays stamped EPHEMERAL_STUB and keeps
    firing the ``encounter.opponent_minted_stub`` lie-detector span. The
    opt-in is never the default.

Existing-suite note: the OLD contract's direct pins
(``test_opponent_roster_resolution.py`` §3 and
``test_162_2_identity_fork_seating.py`` ``TestNovelStubStampsOrigin``) are
retired in this RED commit — this file is their replacement at the public
seam. The wider WN harness sweep (``seat_wn_combat`` et al. reach the stub
branch by design) is enumerated in the session Delivery Findings.

RED today: no ``generics`` schema, no ``OriginKind.GENERIC``, the seater mints
stubs silently, and there is no degenerate opt-in keyword.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.agents.orchestrator import NpcMention
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager
from sidequest.genre.models.bestiary import Bestiary, BestiaryEntry
from sidequest.genre.models.inventory import DamageSpec
from sidequest.genre.models.rules import (
    BeatDef,
    ConfrontationDef,
    ResolutionMode,
    RulesConfig,
)
from sidequest.server.dispatch.encounter_lifecycle import (
    instantiate_encounter_from_trigger,
)

_LOC = "the_dropmouth"
_ROUTER_NAME = "Gruk the Smasher"
_GENERIC_SEAT_SPAN = "encounter.opponent_seated_from_generics"
_REFUSAL_SPAN = "encounter.stub_fabrication_refused"
_MINTED_STUB_SPAN = "encounter.opponent_minted_stub"


@pytest.fixture
def otel_capture() -> Iterator[InMemorySpanExporter]:
    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


def _combat_cdef(*, opponent_source: str = "bestiary") -> ConfrontationDef:
    """Minimal hp_depletion combat cdef (the 162-2 fork-suite fixture shape).

    ``opponent_default_stats`` hp=8 / armor_class=12 deliberately DIFFER from
    the generic row (hp=6 / armor_class=11) so the stat-source assertions
    discriminate: a seat carrying 8/12 took the frame default (the old stub
    path), a seat carrying 6/11 took the authored generic row.

    ``opponent_source="frame"`` (story 162-3 Dev deviation) marks a def whose
    Other seats from the frame's own ``opponent_default_stats`` and NEVER from
    bestiary generics — vehicle-scale hp_depletion (e.g. ship_combat's hull).
    """
    strike = BeatDef.model_validate(
        {
            "id": "strike",
            "label": "Strike",
            "kind": "strike",
            "base": 2,
            "stat_check": "Strength",
            "damage_channel": "strike",
            "effect": "A blow.",
            "narrator_hint": "Hit.",
        }
    )
    return ConfrontationDef(
        type="combat",
        label="Skirmish",
        category="combat",
        resolution_mode=ResolutionMode.beat_selection,
        win_condition="hp_depletion",
        player_metric=None,
        opponent_metric=None,
        opponent_default_stats={
            "Strength": 10,
            "hp": 8,
            "armor_class": 12,
            "dexterity": 11,
        },
        opponent_damage=DamageSpec(dice="1d6"),
        beats=[strike],
        opponent_source=opponent_source,
    )


def _generic_entry() -> BestiaryEntry:
    return BestiaryEntry(
        id="hold_dead",
        name="Hold-Dead",
        level=1,
        hp=6,
        armor_class=11,
        attack_bonus=1,
        damage="1d6",
        role="risen dwarfhold laborer, generic Other",
    )


def _roster_entry() -> BestiaryEntry:
    return BestiaryEntry(
        id="ghast",
        name="Vellum Ghast",
        level=3,
        hp=24,
        armor_class=15,
        attack_bonus=3,
        damage="1d8",
    )


class _FakeGenrePack:
    """Duck-typed pack for the seater: real ``RulesConfig`` + a recording
    ``effective_bestiary``. A fake (not ``MagicMock(spec=GenrePack)``) so any
    OTHER pack accessor the implementation reaches for fails LOUD with
    AttributeError at the fixture — never a silent auto-mock (the fixture is
    then extended deliberately)."""

    def __init__(
        self,
        bestiary: object | None,
        source: str = "world",
        *,
        cdef: ConfrontationDef | None = None,
    ) -> None:
        self.rules = RulesConfig(confrontations=[cdef or _combat_cdef()])
        self._bestiary = bestiary
        self._source = source
        self.bestiary_requests: list[str | None] = []

    def effective_bestiary(self, world: str | None) -> tuple[object | None, str]:
        self.bestiary_requests.append(world)
        return self._bestiary, self._source


def _generics_pack() -> _FakeGenrePack:
    """A pack whose effective (world) bestiary authors ONE generic row.

    The bestiary is a namespace (not a ``Bestiary`` model) because today's
    ``Bestiary`` cannot carry ``generics`` at all — the schema half of this
    story (tests/game/test_162_3_bestiary_generics_schema.py) turns it into a
    real field; the rows here are REAL ``BestiaryEntry`` objects either way.
    """
    return _FakeGenrePack(SimpleNamespace(entries=[_roster_entry()], generics=[_generic_entry()]))


def _frame_generics_pack() -> _FakeGenrePack:
    """A pack that authors generics BUT whose combat def declares
    ``opponent_source: frame`` — the Other must seat from the frame's own
    ``opponent_default_stats``, never the generics rows (vehicle-scale
    carve-out, Dev deviation)."""
    return _FakeGenrePack(
        SimpleNamespace(entries=[_roster_entry()], generics=[_generic_entry()]),
        cdef=_combat_cdef(opponent_source="frame"),
    )


def _no_generics_packs() -> list[tuple[str, _FakeGenrePack]]:
    return [
        ("bestiary-without-generics", _FakeGenrePack(Bestiary(entries=[_roster_entry()]))),
        ("no-bestiary-at-all", _FakeGenrePack(None, source="genre")),
    ]


def _snapshot(*, npc: Npc | None = None, player: str = "Kirk") -> GameSnapshot:
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=5),
    )
    snap.character_locations[player] = _LOC
    if npc is not None:
        snap.npcs.append(npc)
    return snap


def _statted_creature(name: str, *, creature_id: str = "ghast", hp: int = 24) -> Npc:
    return Npc(
        core=CreatureCore(
            name=name,
            description="A dead delver still at the shift.",
            personality="Relentless.",
            inventory=Inventory(),
            hp=HpPool(current=hp, max=hp, base_max=hp),
            armor_class=15,
        ),
        creature_id=creature_id,
        threat_level=1,
        disposition=-20,
        last_seen_location=_LOC,
        last_seen_turn=4,
    )


def _drive(snap: GameSnapshot, pack: _FakeGenrePack, *, opponent: str = _ROUTER_NAME, **kw):
    return instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,  # type: ignore[arg-type]  # duck-typed; see _FakeGenrePack
        encounter_type="combat",
        player_name="Kirk",
        npcs_present=[NpcMention(name=opponent, side="opponent", role="hostile")],
        genre_slug=snap.genre_slug,
        **kw,
    )


# ---------------------------------------------------------------------------
# 1. Generics are the sanctioned last-resort Other (AC2 / AC4)
# ---------------------------------------------------------------------------


class TestGenericsSeatTheLastResortOther:
    def test_unbacked_opponent_seats_from_generics_not_stub(
        self, otel_capture: InMemorySpanExporter
    ) -> None:
        """No roster match, no pool antagonist, generics authored → the Other
        is drawn from the generics row: stamped ``Origin(kind=GENERIC,
        creature_id='hold_dead')``, and the old fabrication tell (the
        ``opponent_minted_stub`` span) stays silent. RED today: the seat is an
        EPHEMERAL_STUB with no creature_id."""
        from sidequest.game.origin import OriginKind

        snap = _snapshot()
        pack = _generics_pack()

        enc = _drive(snap, pack)

        assert enc is not None
        assert len(snap.npcs) == 1, (
            f"expected exactly the generic-backed Other; roster: "
            f"{[n.core.name for n in snap.npcs]!r}"
        )
        seated = snap.npcs[0]
        assert seated.origin is not None, "generic seat left origin unstamped"
        assert seated.origin.kind == OriginKind.GENERIC, (
            f"expected the sanctioned GENERIC origin, got {seated.origin.kind!r}"
        )
        assert seated.origin.creature_id == "hold_dead"
        names = {s.name for s in otel_capture.get_finished_spans()}
        assert _MINTED_STUB_SPAN not in names, (
            "a generics-backed seat is NOT a fabrication — the stub lie-detector must stay silent"
        )

    def test_generics_resolution_is_world_scoped(self) -> None:
        """The generics lookup rides the session world's effective bestiary —
        the same genre/world layering every other bestiary consumer uses
        (guardrail: world overrides, genre default inherited)."""
        snap = _snapshot()
        pack = _generics_pack()

        _drive(snap, pack)

        assert snap.world_slug in pack.bestiary_requests, (
            f"expected the seater to resolve generics for world "
            f"{snap.world_slug!r}; effective_bestiary saw {pack.bestiary_requests!r}"
        )

    def test_generic_seated_other_carries_authored_row_stats(self) -> None:
        """The generic row IS an authored stat block — its hp/AC seat the
        Other (6/11), not the confrontation frame's generic defaults (8/12).
        Same doctrine as the 108-2 bound-creature HP preserve: authored
        bestiary math wins over ``opponent_default_stats``."""
        snap = _snapshot()
        pack = _generics_pack()

        enc = _drive(snap, pack)

        assert enc is not None
        seated = snap.npcs[0]
        assert seated.core.hp.max == 6, (
            f"generic Other must carry the authored row hp (6), not the cdef "
            f"default (8); got {seated.core.hp.max}"
        )
        assert seated.core.hp.current == 6
        assert seated.core.armor_class == 11, (
            f"generic Other must carry the authored row AC (11); got {seated.core.armor_class}"
        )

    def test_generic_seated_other_is_reachable_by_actor_name(self) -> None:
        """Whatever naming policy the seat takes (canonicalize to the row name
        with the router name aliased, or keep the router name over the row's
        chassis), the encounter's opponent actor MUST resolve to the seated
        core — ``find_creature_core(actor.name)`` is how the hp_depletion
        pipeline, the WN attack tools and the payload builder reach the Other."""
        snap = _snapshot()
        pack = _generics_pack()

        enc = _drive(snap, pack)

        assert enc is not None
        opponent_actors = [a for a in enc.actors if a.side == "opponent"]
        assert len(opponent_actors) == 1
        core = snap.find_creature_core(opponent_actors[0].name)
        assert core is not None, (
            f"opponent actor {opponent_actors[0].name!r} does not resolve to a "
            f"seated core; roster: {[n.core.name for n in snap.npcs]!r}"
        )
        assert core.hp.max == 6, "actor resolved to a core that is not the generic seat"

    def test_generic_seat_is_observable_on_gm_panel(
        self, otel_capture: InMemorySpanExporter
    ) -> None:
        """OTEL principle: drawing the Other from generics is a subsystem
        decision — the GM panel must see WHICH row seated WHOM, or the panel
        cannot distinguish an authored generic from narrator improvisation."""
        snap = _snapshot()
        pack = _generics_pack()

        _drive(snap, pack)

        spans = {s.name: s for s in otel_capture.get_finished_spans()}
        assert _GENERIC_SEAT_SPAN in spans, (
            f"generic-seat decision span not emitted; saw {sorted(spans)!r}"
        )
        attrs = dict(spans[_GENERIC_SEAT_SPAN].attributes or {})
        assert attrs.get("opponent") == _ROUTER_NAME, (
            f"span must carry the router-named opponent; attrs={attrs!r}"
        )
        assert attrs.get("creature_id") == "hold_dead", (
            f"span must name the generic row that seated; attrs={attrs!r}"
        )


# ---------------------------------------------------------------------------
# 2. No generics → LOUD failure, nothing seated, nothing appended (AC3)
# ---------------------------------------------------------------------------


class TestStubFabricationFailsLoud:
    @pytest.mark.parametrize(
        "pack",
        [p for _, p in _no_generics_packs()],
        ids=[label for label, _ in _no_generics_packs()],
    )
    def test_no_source_and_no_generics_raises(self, pack: _FakeGenrePack) -> None:
        """The default path is NON-degenerate: with every legitimate source
        exhausted (roster, pool, generics) the seater refuses — it never
        invents an Other (No Silent Fallbacks). The message must point the
        author at the fix (the generics section). RED today: a stub is minted
        and the call returns an encounter."""
        snap = _snapshot()

        with pytest.raises(ValueError, match=r"(?i)generic"):
            _drive(snap, pack)

        assert snap.encounter is None, (
            f"refusal must not half-seat an encounter; got {snap.encounter!r}"
        )
        assert snap.npcs == [], (
            f"refusal must not append a fabricated Npc; roster grew to "
            f"{[n.core.name for n in snap.npcs]!r}"
        )

    def test_refusal_is_observable_and_never_the_old_stub_span(
        self, otel_capture: InMemorySpanExporter
    ) -> None:
        """Every NPC-gen attempt spans its origin + fallback reason (AC6). The
        refusal names the opponent the table asked for; the old fabrication
        span must NOT fire on the refusal path."""
        snap = _snapshot()
        pack = _FakeGenrePack(Bestiary(entries=[_roster_entry()]))

        with pytest.raises(ValueError, match=r"(?i)generic"):
            _drive(snap, pack)

        spans = {s.name: s for s in otel_capture.get_finished_spans()}
        assert _REFUSAL_SPAN in spans, (
            f"stub-fabrication refusal span not emitted; saw {sorted(spans)!r}"
        )
        attrs = dict(spans[_REFUSAL_SPAN].attributes or {})
        assert attrs.get("opponent") == _ROUTER_NAME, (
            f"refusal span must name the unseatable opponent; attrs={attrs!r}"
        )
        assert _MINTED_STUB_SPAN not in spans, (
            "the refusal path must never ALSO fabricate (mint span fired)"
        )


# ---------------------------------------------------------------------------
# 3. Precedence: generics are LAST — roster and pool still win (AC5)
# ---------------------------------------------------------------------------


class TestGenericsAreLastResortOnly:
    def test_roster_creature_wins_over_generics(self, otel_capture: InMemorySpanExporter) -> None:
        """Green guard (holds today, must keep holding): a bound roster
        creature matching the actor seats itself — its authored HP survives
        (108-2) and the generics path is never consulted for the seat."""
        creature = _statted_creature("Molgrath the Eyeless", hp=24)
        snap = _snapshot(npc=creature)
        pack = _generics_pack()

        enc = _drive(snap, pack, opponent="Molgrath the Eyeless")

        assert enc is not None
        assert len(snap.npcs) == 1, "roster hit must not grow the roster"
        assert creature.core.hp.max == 24, "108-2 regressed: bound HP clobbered"
        from sidequest.game.origin import derive_origin

        assert derive_origin(creature).kind.value != "generic", (
            "a roster seat must never be re-stamped as a generics seat"
        )
        names = {s.name for s in otel_capture.get_finished_spans()}
        assert _GENERIC_SEAT_SPAN not in names, (
            "generics span fired on a roster-backed seat — precedence inverted"
        )

    def test_pool_antagonist_wins_over_generics(self, otel_capture: InMemorySpanExporter) -> None:
        """Green guard: the 153-10 scene-active pool antagonist still outranks
        generics — the narrator's developed Other beats an anonymous authored
        generic (precedence: ... > MM pool > generics)."""
        snap = _snapshot()
        snap.npc_pool.append(NpcPoolMember(name=_ROUTER_NAME, drawn_from="narrator_invented"))
        pack = _generics_pack()

        enc = _drive(snap, pack)

        assert enc is not None
        assert len(snap.npcs) == 1, "pool promotion must seat exactly one Npc"
        promoted = snap.npcs[0]
        assert promoted.ephemeral is False, "pool promotion is not a fabrication"
        assert promoted.core.hp.max == 8, (
            "pool promotion seeds the confrontation frame stats (existing contract)"
        )
        if promoted.origin is not None:
            assert promoted.origin.kind.value != "generic", (
                "a pool promotion must never be stamped as a generics seat"
            )
        names = {s.name for s in otel_capture.get_finished_spans()}
        assert _GENERIC_SEAT_SPAN not in names, (
            "generics span fired on a pool-promoted seat — precedence inverted"
        )


# ---------------------------------------------------------------------------
# 4. The degenerate opt-in (test fixtures / one-off scenario generation)
# ---------------------------------------------------------------------------


class TestDegenerateOptIn:
    def test_degenerate_optin_mints_synthetic_loudly(
        self,
        otel_capture: InMemorySpanExporter,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The story context's carve-out: degenerate callers may opt in — the
        mint then happens the OLD way, stamped EPHEMERAL_STUB (the 162-2
        provenance contract survives on this path), with the
        ``opponent_minted_stub`` lie-detector span AND a WARNING log. The
        opt-in is an explicit keyword; the default (every test above) refuses.
        RED today: the keyword does not exist (TypeError)."""
        from sidequest.game.origin import OriginKind

        snap = _snapshot()
        pack = _FakeGenrePack(Bestiary(entries=[_roster_entry()]))

        with caplog.at_level(logging.WARNING):
            enc = _drive(snap, pack, allow_synthetic_opponent=True)

        assert enc is not None, "the degenerate opt-in must still seat the encounter"
        assert len(snap.npcs) == 1
        stub = snap.npcs[0]
        assert stub.ephemeral is True
        assert stub.origin is not None
        assert stub.origin.kind == OriginKind.EPHEMERAL_STUB
        names = {s.name for s in otel_capture.get_finished_spans()}
        assert _MINTED_STUB_SPAN in names, (
            "the degenerate mint must keep firing the fabrication lie-detector span"
        )
        warned = [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING
            and ("stub" in r.getMessage().lower() or "synthetic" in r.getMessage().lower())
        ]
        assert warned, (
            "the degenerate mint must WARN (loud even when tolerated); "
            f"warning-level records: {[r.getMessage() for r in caplog.records]!r}"
        )


# ---------------------------------------------------------------------------
# 5. GM-panel wiring: the new spans are routed, not just emitted
# ---------------------------------------------------------------------------


class TestSpansRoutedForGmPanel:
    def test_generics_spans_are_registered_span_routes(self) -> None:
        """A span the GM panel cannot route is a lie-detector with no readout
        (ADR-090/103 registry contract — mirror of the minted-stub route).
        RED today: the constants do not exist."""
        from sidequest.telemetry.spans import SPAN_ROUTES
        from sidequest.telemetry.spans.encounter import (
            SPAN_ENCOUNTER_OPPONENT_SEATED_FROM_GENERICS,
            SPAN_ENCOUNTER_STUB_FABRICATION_REFUSED,
        )

        assert SPAN_ENCOUNTER_OPPONENT_SEATED_FROM_GENERICS == _GENERIC_SEAT_SPAN
        assert SPAN_ENCOUNTER_STUB_FABRICATION_REFUSED == _REFUSAL_SPAN
        assert SPAN_ENCOUNTER_OPPONENT_SEATED_FROM_GENERICS in SPAN_ROUTES
        assert SPAN_ENCOUNTER_STUB_FABRICATION_REFUSED in SPAN_ROUTES


# ---------------------------------------------------------------------------
# 6. opponent_source="frame" seats from the frame, NEVER generics (Dev deviation)
# ---------------------------------------------------------------------------


class TestFrameSourcedSkipsGenerics:
    def test_frame_def_mints_from_frame_and_never_consults_generics(
        self,
        otel_capture: InMemorySpanExporter,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A def declaring ``opponent_source: frame`` (vehicle-scale hp_depletion,
        e.g. ship_combat's hull) seats the unbacked Other from its OWN
        ``opponent_default_stats`` (8/12) — bypassing the world's authored
        generics ENTIRELY, with no ``allow_synthetic_opponent`` and NO warning.
        A humanoid bestiary generic must never wear a hull. This is the sanctioned
        frame carve-out, NOT the degenerate opt-in."""
        from sidequest.game.origin import OriginKind

        snap = _snapshot()
        pack = _frame_generics_pack()

        with caplog.at_level(logging.WARNING):
            enc = _drive(snap, pack)

        assert enc is not None
        assert len(snap.npcs) == 1
        seated = snap.npcs[0]
        # Frame-sourced: the old ephemeral stub, NOT a GENERIC seat.
        assert seated.origin is not None
        assert seated.origin.kind == OriginKind.EPHEMERAL_STUB, (
            f"frame-sourced Other must mint EPHEMERAL_STUB from the frame, not a "
            f"GENERIC seat; got {seated.origin.kind!r}"
        )
        # Frame stats (8/12), never the generic row (6/11).
        assert seated.core.hp.max == 8 and seated.core.armor_class == 12, (
            f"frame-sourced Other must carry the frame's opponent_default_stats "
            f"(8/12), not the generic row (6/11); got "
            f"{seated.core.hp.max}/{seated.core.armor_class}"
        )
        # Generics were NEVER consulted — the frame carve-out short-circuits the
        # lookup BEFORE effective_bestiary is called.
        assert pack.bestiary_requests == [], (
            f"frame-sourced seat must skip generics entirely; effective_bestiary "
            f"was consulted: {pack.bestiary_requests!r}"
        )
        # The frame mint fires the SAME lie-detector span as any mint...
        names = {s.name for s in otel_capture.get_finished_spans()}
        assert _MINTED_STUB_SPAN in names, (
            "the frame-sourced mint must still fire the fabrication lie-detector span"
        )
        assert _GENERIC_SEAT_SPAN not in names, (
            "a frame-sourced seat is not a generics seat — that span must stay silent"
        )
        # ...but WITHOUT the degenerate-opt-in warning (the frame is authored content).
        warned = [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING and "synthetic_stub_minted" in r.getMessage()
        ]
        assert not warned, (
            f"the frame-sourced path is authored content, not a degenerate opt-in — "
            f"it must NOT warn; got {[r.getMessage() for r in caplog.records]!r}"
        )


# ---------------------------------------------------------------------------
# 7. Multi-opponent refusal rolls back ANY opponent seated earlier in the pass
# ---------------------------------------------------------------------------


class TestRefusalRollsBackHalfSeat:
    def test_refusal_after_earlier_pool_promotion_leaves_nothing(self) -> None:
        """The "nothing half-seated" invariant must hold for MULTI-opponent
        seating, not just the single-opponent case. Opponent A resolves to a
        scene-active pool antagonist (promoted + appended to the roster);
        opponent B is a no-source router name in a world with no generics
        (raises). The refusal must roll BACK A's promotion too — a discarded
        encounter must leave the roster exactly as it found it."""
        _A = "Rax the Enforcer"  # pool-promoted, appended before B is reached
        _B = "Some Nameless Goon"  # no roster / pool / generics source -> raise
        snap = _snapshot()
        snap.npc_pool.append(NpcPoolMember(name=_A, drawn_from="narrator_invented"))
        pack = _FakeGenrePack(Bestiary(entries=[_roster_entry()]))  # no generics

        with pytest.raises(ValueError, match=r"(?i)generic"):
            instantiate_encounter_from_trigger(
                snapshot=snap,
                pack=pack,  # type: ignore[arg-type]  # duck-typed; see _FakeGenrePack
                encounter_type="combat",
                player_name="Kirk",
                npcs_present=[
                    NpcMention(name=_A, side="opponent", role="hostile"),
                    NpcMention(name=_B, side="opponent", role="hostile"),
                ],
                genre_slug=snap.genre_slug,
            )

        assert snap.encounter is None, (
            f"refusal must restore the encounter slot; got {snap.encounter!r}"
        )
        assert snap.npcs == [], (
            f"refusal must roll back the EARLIER pool-promoted opponent too — the "
            f"roster must be empty, not half-seated; got "
            f"{[n.core.name for n in snap.npcs]!r}"
        )
