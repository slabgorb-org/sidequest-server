"""Encounter lifecycle — instantiation, resolution.

Port of sidequest-api/crates/sidequest-server/src/dispatch/
{state_mutations,tropes,response}.rs combat-sensitive paths (Story 3.4).
"""

from __future__ import annotations

import logging
import random as _random
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from sidequest.agents.orchestrator import NpcMention
    from sidequest.game.ruleset.base import RulesetModule

from sidequest.game.disposition import Attitude
from sidequest.game.encounter import (
    ActorSide,
    EncounterActor,
    EncounterMetric,
    EncounterPhase,
    StructuredEncounter,
)
from sidequest.game.lore_store import LoreStore
from sidequest.game.origin import Origin, OriginKind, resolve_roster_npc
from sidequest.game.resource_pool import ResourceThreshold
from sidequest.game.ruleset.registry import get_ruleset_module
from sidequest.game.ruleset.without_number import WithoutNumberRulesetModule
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.table.types import TablePot, TableSeat, TableState
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.progression import (
    ProgressionConfig,
    resolve_affinity_tier,
    resolve_level,
)
from sidequest.genre.models.rules import ConfrontationDef, ResolutionMode, WinCondition
from sidequest.protocol.models import AdvancementDelta as LevelUp
from sidequest.protocol.models import AffinityTierUp
from sidequest.server.dispatch.confrontation import find_confrontation_def
from sidequest.server.dispatch.sealed_letter import ROLE_BLUE, ROLE_RED
from sidequest.telemetry.spans import (
    SPAN_CONFRONTATION_COLOCATION,
    Span,
    encounter_confrontation_initiated_span,
    encounter_creature_zone_reconciled_span,
    encounter_no_opponent_available_span,
    encounter_opponent_minted_stub_span,
    encounter_opponent_resolved_from_roster_span,
    encounter_opponent_seated_from_generics_span,
    encounter_opponent_toothless_span,
    encounter_resolved_span,
    encounter_roster_resolution_skipped_span,
    encounter_sealed_letter_arity_rejected_span,
    encounter_stub_fabrication_refused_span,
    npc_edge_published_span,
    participant_joined_span,
    table_dealt_span,
    table_seat_seeded_span,
)
from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

_log = logging.getLogger(__name__)

_VALID_SIDES = ("player", "opponent", "neutral")


def _raise_missing_ruleset(context: str) -> str:
    raise ValueError(
        f"{context}: pack/rules missing — cannot resolve ruleset slug. A missing "
        f"ruleset is a configuration error, not a silent 'dial' default "
        f"(spec 2026-06-17 §1, No Silent Fallbacks)."
    )


class NoOpponentAvailableError(ValueError):
    """Raised when a category=combat encounter resolves to zero opponents
    after both the narrator's ``npcs_present`` and the location-scoped
    registry fallback come up empty (Story 45-33).

    Subclass of ``ValueError`` so existing ``except ValueError`` blocks
    still catch it; the dedicated class lets ``_apply_narration_result_to_snapshot``
    catch THIS path gracefully (CLAUDE.md "strict helper, lenient caller")
    without swallowing the sealed-letter validator's
    ``"exactly one opponent"`` ValueError, which is a config/extraction
    error that should propagate.
    """


class SealedLetterArityError(ValueError):
    """Raised when the narrator triggers a sealed-letter encounter (1v1
    red/blue contract — dogfight, duel, etc.) against zero or multiple
    NPCs (Playtest 2026-05-08).

    Subclass of ``ValueError`` so existing ``except ValueError`` blocks
    still match. The dedicated class lets the narration-apply caller
    catch THIS path gracefully — declining the encounter without
    crashing the turn — while still propagating other ValueErrors
    (config/extraction errors that the existing test suite asserts
    crash the turn).

    Sealed-letter encounters are commit-reveal duels addressed by role
    tag (red/blue); a pack of raiders has no clean mapping into that
    contract. The selector (the narrator) made an inappropriate choice;
    the engine declines, OTEL records the gap, and the turn proceeds
    on prose alone.
    """


class InitiativeUnresolvableError(ValueError):
    """Raised when a seated combat cannot roll initiative because a player
    actor's stat block carries no DEXTERITY score (story 158-28).

    The encounter is ALREADY seated (``snapshot.encounter`` is set before the
    initiative roll), so this is a partial-seat condition, not a failed seat.
    Subclass of ``ValueError`` so existing ``except ValueError`` blocks still
    catch it; the dedicated class lets ``run_confrontation_dispatch`` degrade
    LOUDLY (ADR-006) — keep the seat, surface an error outcome the GM panel
    sees — rather than letting the bare ValueError wedge the turn. Distinct
    from the player-NOT-FOUND initiative guard, which stays a fail-loud
    ValueError (a genuine roster name-skew defect, not a degradable condition).
    """


def _validate_side(actor_name: str, declared: str) -> ActorSide:
    """Validate that side is in {player, opponent, neutral}.

    Raises ValueError on invalid value, emitting encounter_invalid_side_span
    for OTEL observability.
    """
    if declared in _VALID_SIDES:
        return cast(ActorSide, declared)
    from sidequest.telemetry.spans import encounter_invalid_side_span

    with encounter_invalid_side_span(
        actor_name=actor_name,
        declared_side=declared,
        valid_set="|".join(_VALID_SIDES),
    ):
        pass
    raise ValueError(f"actor {actor_name!r} declared_side={declared!r} not in {_VALID_SIDES}")


def reap_resolved_encounter_husk(
    snapshot: GameSnapshot, *, is_dice_replay: bool, turn: int
) -> bool:
    """Clear a resolved encounter left lingering in ``snapshot.encounter``.

    A confrontation that resolved on a PRIOR turn persists on the snapshot as a
    zeroed husk, and the narrator can then layer a fresh fight on the corpse —
    the phantom-wound CRITICAL (sq-playtest 2026-06-14, heavy_metal/barsoom): a
    stale resolved arena bout sat in state for turns while a new "fight" was
    narrated with no live encounter, no dice, no HP delta.

    Reaped ONLY on a genuine new player turn (``is_dice_replay=False``). The
    dice-resolution replay re-entry (``suppress_intent_router=True``) narrates a
    JUST-resolved encounter within the SAME logical turn and must keep it — so
    that path never reaps. A live (unresolved) encounter is never touched: only
    ``encounter.resolved`` husks are cleared, so a fight in progress is safe.

    Returns ``True`` when a husk was cleared. Emits an ``encounter`` state
    transition (``op=husk_reaped``) so the GM panel can confirm the cleanup
    fired (CLAUDE.md OTEL principle).
    """
    enc = snapshot.encounter
    if is_dice_replay or enc is None or not enc.resolved:
        return False

    # 108-2 (MINTING-MAJOR persist): a FABRICATED combat stub (ephemeral, no
    # backing roster/bestiary entry — the "Arena Opponent" / "Hold-Dead" stubs)
    # must not survive its fight as durable canon the narrator can re-reference
    # as a living NPC on later turns (snapshot showed it lingering at hp 0/10,
    # `npc.referenced match=npcs_hit` turns 7-8). Reap any ephemeral opponent of
    # the resolved encounter together with the husk. Bound creatures
    # (``creature_id`` set, ephemeral=False) and narrator-declared NPCs are NEVER
    # touched — only engine-fabricated stubs are quarantined.
    opponent_names = {a.name for a in enc.actors if a.side == "opponent"}
    reaped_stubs = [
        npc.core.name for npc in snapshot.npcs if npc.ephemeral and npc.core.name in opponent_names
    ]
    if reaped_stubs:
        snapshot.npcs[:] = [
            npc for npc in snapshot.npcs if not (npc.ephemeral and npc.core.name in opponent_names)
        ]
        for stub_name in reaped_stubs:
            _watcher_publish(
                "state_transition",
                {
                    "field": "npcs",
                    "op": "ephemeral_stub_reaped",
                    "npc_name": stub_name,
                    "encounter_type": enc.encounter_type,
                    "turn": str(turn),
                    "source": "turn_start",
                },
                component="encounter",
            )
            _log.info(
                "encounter.ephemeral_stub_reaped npc=%s type=%s turn=%s "
                "(fabricated combat stub removed with its resolved encounter so it "
                "cannot persist as canon)",
                stub_name,
                enc.encounter_type,
                turn,
            )

    # ADR-153 §7 (158-30): stamp the reaped (type, turn) BEFORE clearing so the
    # seater can refuse a same-turn re-seat (``instantiate_encounter_from_trigger``
    # guard) — the husk_reaped clear must win over a same-turn re-dispatch / drift
    # keep-alive, else a resolved dogfight resurrects in Setup and soft-locks the
    # player into ship maneuvers on foot. Keyed by turn so a later turn still seats.
    snapshot.husk_reaped_this_turn = (enc.encounter_type, turn)
    snapshot.encounter = None
    _watcher_publish(
        "state_transition",
        {
            "field": "encounter",
            "op": "husk_reaped",
            "encounter_type": enc.encounter_type,
            "outcome": enc.outcome or "",
            "turn": str(turn),
            "source": "turn_start",
        },
        component="encounter",
    )
    _log.info(
        "encounter.husk_reaped type=%s outcome=%s turn=%s "
        "(resolved encounter cleared at turn start so no fight layers on the corpse)",
        enc.encounter_type,
        enc.outcome,
        turn,
    )
    return True


def _stamp_encounter_presence(npc, *, turn: int, location: str | None) -> None:
    """Story 72-8: refresh recency on an NPC that is PRESENT in an encounter.

    ``Npc.last_seen_turn`` / ``last_seen_location`` were stamped only on the
    prose-mention path (``narration_apply._apply_npc_mentions`` → ``npcs_hit``).
    An NPC physically seated as an encounter opponent — HP/dial mutated round
    over round — went un-stamped when the narrator didn't also name it in that
    turn's ``npcs_present`` prose, so the engine treated an actively-fought
    combatant as "not recently seen". Presence is a stronger continuity signal
    than a prose name-drop; this seam closes the gap (precondition for 72-6's
    LRU/last-seen prune, which would otherwise mis-evict an on-board NPC).

    Mirrors the prose path's write discipline exactly: ``last_seen_turn`` always
    advances to the current turn; ``last_seen_location`` is overwritten ONLY when
    a location actually resolved (No Silent Fallbacks — never stamp a bogus
    location when ``party_location`` returned ``None``).
    """
    npc.last_seen_turn = turn
    if location:
        npc.last_seen_location = location


def _has_authored_reprisal_source(cdef, opponent_core) -> bool:
    """Slots 1-3 of the reprisal damage-resolution priority: an AUTHORED damage
    source on the confrontation or the opponent itself (NOT the ruleset's unarmed
    floor). Mirrors the runtime resolver in ``dispatch.dice._resolve_opponent_reprisal``
    so the at-seat detector agrees with it EXACTLY (no drift):

    1. ``cdef.opponent_damage`` — the authored enemy weapon (space_opera's fix).
    2. The opponent's STRIKE beat ``damage_override`` — a natural-attack spec on
       the beat the reprisal reuses (first ``damage_channel == "strike"`` beat).
    3. The opponent core's inventory weapon — an item dict carrying a ``damage``
       field (``resolve_damage_spec_from_beat_and_actor`` priority 2).

    Catalog-id weapon resolution (priority 3 of the runtime resolver) is NOT
    re-checked here: a seeded/materialized mook carries no catalog-id inventory,
    and threading the pack catalog into the seating seam to cover a case the
    seeded opponent never has would over-couple the detector. Absence of an
    authored source is the content gap the seating diagnostic surfaces.
    """
    if getattr(cdef, "opponent_damage", None) is not None:
        return True
    strike_beat = next(
        (
            b
            for b in (getattr(cdef, "beats", None) or [])
            if str(getattr(b, "damage_channel", "none") or "none") == "strike"
        ),
        None,
    )
    if strike_beat is not None and getattr(strike_beat, "damage_override", None) is not None:
        return True
    if opponent_core is not None:
        inv = getattr(opponent_core, "inventory", None)
        for item_dict in getattr(inv, "items", []) or []:
            if isinstance(item_dict, dict) and item_dict.get("damage") is not None:
                return True
    return False


def _opponent_reprisal_damage_resolvable(cdef, opponent_core, ruleset) -> bool:
    """BUG 1 (eh-opp-damage) + Story 153-20: can the seated opponent's reprisal
    resolve ANY damage at all?

    True when the opponent has an authored damage source
    (``_has_authored_reprisal_source`` — slots 1-3) OR — Story 153-20 — when the
    bound ruleset is a Without Number binding (``swn``/``wwn``/``cwn``/``awn``),
    whose SRD gives a weaponless strike a real unarmed floor
    (``WithoutNumberRulesetModule.SRD_UNARMED_DICE``, mirroring
    ``without_number.resolve_damage``'s last resort — story 153-1). A weaponless
    WN opponent is therefore NOT toothless/invulnerable.

    WN-gated (SOUL "Bind the Ruleset, Don't Balance It", ADR-143): off the WN path
    there is no SRD unarmed guarantee, so the conservative false-toothless bias is
    preserved (No Silent Fallbacks — a false "toothless" flag is a visible nudge to
    author ``opponent_damage``, never a silent miss). The unarmed value is taken
    DIRECTLY from the ruleset binding — never authored or invented here.
    """
    if _has_authored_reprisal_source(cdef, opponent_core):
        return True
    return isinstance(ruleset, WithoutNumberRulesetModule)


def _generics_for(pack, world_slug: str | None) -> list:
    """Resolve the world's authored bestiary ``generics:`` rows (story 162-3).

    Rides the same genre/world layering every bestiary consumer uses:
    ``pack.effective_bestiary(world)`` (world tier overrides, genre tier
    inherited). ``pack=None`` or no resolvable bestiary → ``[]`` — the caller
    then refuses loudly (or mints under the explicit degenerate opt-in);
    the empty list is never a silent seat.
    """
    if pack is None:
        return []
    bestiary, _source = pack.effective_bestiary(world_slug or None)
    if bestiary is None:
        return []
    return list(bestiary.generics or [])


def _seed_combat_hp_depletion_to_npcs(
    *,
    snapshot: GameSnapshot,
    actors: list[EncounterActor],
    cdef,
    turn: int,
    source: str,
    acting_character_name: str,
    ruleset: RulesetModule,
    pack=None,
    allow_synthetic_opponent: bool = False,
) -> None:
    """Seed opponent ``Npc.core`` HP + AC from content for hp_depletion combats.

    Task 9 (space_opera → SWN binding): under ``win_condition: hp_depletion``
    there is no dial — combat resolves when the opponent's ``core.hp.current``
    reaches 0 and the SWN attack rolls against ``core.armor_class``. Both must
    be content-authored on the per-confrontation ``opponent_default_stats``
    block via the reserved ``hp`` / ``armor_class`` keys (see
    ``ConfrontationDef.opponent_hp`` / ``opponent_armor_class``).

    For each opponent-side ``EncounterActor`` we seed the backing
    ``Npc.core.hp`` pool (via ``hp_pool_from_hp``) and ``core.armor_class``
    from the content values. CRITICAL (item 3 / CLAUDE.md "no half-wiring"):
    if no backing ``Npc`` exists for an opponent name (a router-named
    opponent that was never materialized into ``snapshot.npcs``), we CREATE
    one here so the opponent core is reachable via
    ``snapshot.find_creature_core(name)`` — without it the hp_depletion
    resolution path (``apply_beat`` reads the opponent via the edge_resolver
    and resolves at ``core.hp.current <= 0``) could never fire and the SWN
    attack would have no AC to roll against.

    Contract enforced at LOAD time (Task 1): the ``ConfrontationDef`` validator
    requires ``hp`` / ``armor_class`` / ``dexterity`` under
    ``opponent_default_stats`` for any combat hp_depletion confrontation that
    loads, so ``opponent_hp`` / ``opponent_armor_class`` are guaranteed present
    here — no runtime fail-loud needed at this seam.

    Story 162-3: the fabrication last-resort is gone from the default path.
    An opponent with no roster/pool backing seats from the world bestiary's
    authored ``generics:`` section (resolved via ``pack.effective_bestiary``
    against ``snapshot.world_slug``), stamped ``Origin(kind=GENERIC)`` and
    carrying the ROW's stats (authored bestiary math outranks the frame
    default — the 108-2 bound-creature rule). With no generics available the
    seeder RAISES ``ValueError`` (No Silent Fallbacks); degenerate callers
    (test fixtures, one-off scenario generation) may pass
    ``allow_synthetic_opponent=True`` to keep the old warn-and-mint behavior.
    ``pack=None`` means generics are unresolvable — direct-driving callers
    that never reach the fabrication branch may omit it.

    Frame-sourced carve-out: a def declaring ``opponent_source: frame``
    (vehicle-scale hp_depletion, e.g. space_opera ship_combat's hull) or a
    sealed-letter Other (ADR-153 §6 commit-reveal duel) skips generics ENTIRELY
    and mints from the def's own authored ``opponent_default_stats``,
    unconditionally — no ``allow_synthetic_opponent`` needed, no warning. The
    load validator requires ``opponent_default_stats`` on every combat
    hp_depletion cdef, so the frame IS an authored source; a humanoid bestiary
    generic must never wear a hull. This is NOT the degenerate opt-in.
    """
    from sidequest.game.creature_core import CreatureCore, Inventory, hp_pool_from_hp
    from sidequest.game.session import Npc

    hp = cdef.opponent_hp
    ac = cdef.opponent_armor_class

    # Story 72-8: resolve the acting character's location ONCE (same accessor the
    # prose path and ``_npc_fallback_at_location`` use) — every opponent seated in
    # this encounter shares the acting frame's location. ``None`` when the seat has
    # no resolved location; we then stamp only the turn, never a bogus location.
    actor_loc = snapshot.party_location(perspective=acting_character_name)

    for actor in actors:
        if actor.side != "opponent":
            continue
        # Story 162-2: the unified roster lookup replaces the exact-match
        # ``by_name`` dict — an actor named by the narrator's prose alias or a
        # case/whitespace variant resolves to the canonical entity instead of
        # minting a twin beside it (the two-names-one-enemy fork). Resolved
        # live per actor so a stub/promotion appended for an earlier opponent
        # in this same seeding pass is visible to later actors.
        npc = resolve_roster_npc(snapshot.npcs, actor.name)
        created = npc is None
        # Rework round 1 (review [HIGH]): an alias / case-variant hit must
        # CANONICALIZE the seat — every downstream consumer resolves the
        # opponent core by exact ``actor.name`` (``find_creature_core``: the
        # HP-bar filter, WN attack tools, query_encounter, payload builder),
        # so a seat left under the prose alias is an unreachable opponent.
        # Mirrors the 108-2 conscription, which already seats canonically;
        # the alias itself stays in the ledger for narrator prose.
        if npc is not None and npc.core.name != actor.name:
            actor.name = npc.core.name
        pool_origin = ""
        if created:
            # 153-10 ([WWN-OTHER-SEATING]): before fabricating a hollow stub, check
            # ``snapshot.npc_pool`` for a scene-active PERSON antagonist the
            # narrator established on a prior turn. The seater declined the ambient
            # MM grab upstream (``_resolve_opponent_from_roster``), so a router-
            # named pool antagonist reaches us un-backed. Promote it carrying its
            # narrated identity + disposition (the native sibling of the Fate
            # seeder's 126-32a promotion) instead of minting a phantom of the same
            # name beside the cast member the player engaged — then seed the
            # content hp/AC over the promotion's placeholder pool so hp_depletion
            # still resolves. This is NOT a fabrication, so it does NOT fire the
            # ``minted_stub`` lie-detector span; the ``pool_origin`` rides the
            # edge-published span below so the GM panel sees the promotion.
            pool_member = next(
                (m for m in snapshot.npc_pool if m.name == actor.name and not m.is_creature),
                None,
            )
            if pool_member is not None:
                from sidequest.server.narration_apply import (
                    _promote_pool_member_to_npc,
                    _seed_invented_npc_identity,
                )

                npc = _promote_pool_member_to_npc(pool_member)
                _seed_invented_npc_identity(
                    npc=npc, member=pool_member, snapshot=snapshot, turn_num=turn
                )
                npc.core.hp = hp_pool_from_hp(hp)
                npc.core.armor_class = ac
                snapshot.npcs.append(npc)
                pool_origin = pool_member.name
            else:
                # 108-2 (MINTING-MAJOR): reaching here means the opponent name
                # resolved to NEITHER a bound roster entry NOR a co-located statted
                # adversary NOR a scene-active pool antagonist (the
                # materialized-threat resolution + pool promotion upstream already
                # tried) — a router-named free string with no backing.
                #
                # Story 162-3: the fabrication last-resort is replaced by the
                # world bestiary's AUTHORED ``generics:`` section — the sanctioned
                # final rung of the origin precedence. The generic row seats
                # under the router's name (narrator continuity) but carries the
                # ROW's identity (creature_id) and the ROW's stats — authored
                # bestiary math outranks the frame default, the same rule as the
                # 108-2 bound-creature HP preserve above.
                #
                # Frame-sourced carve-out: a sealed-letter Other (ADR-153 §6
                # commit-reveal duel / dogfight) and any def declaring
                # ``opponent_source: frame`` (vehicle-scale hp_depletion, e.g.
                # space_opera ship_combat's hull) seat FROM THE DEF FRAME by
                # design — the load validator requires ``opponent_default_stats``
                # on every combat hp_depletion cdef, so the frame IS an authored
                # source, and a humanoid bestiary generic must never wear a
                # hull. The frame-default mint is therefore NOT a 162-3
                # fabrication: it keeps the pre-162-3 behavior (ephemeral +
                # minted-stub span) unconditionally.
                frame_other = (
                    getattr(cdef, "resolution_mode", None) == ResolutionMode.sealed_letter_lookup
                    or getattr(cdef, "opponent_source", "bestiary") == "frame"
                )
                generics = [] if frame_other else _generics_for(pack, snapshot.world_slug)
                if generics:
                    # Selection: first authored row. Deterministic and
                    # resume-safe; the single chokepoint for any future
                    # role/tag-matched selection.
                    row = generics[0]
                    core = CreatureCore(
                        name=actor.name,
                        description=row.description or row.role or "Combat opponent",
                        personality="Adversary",
                        inventory=Inventory(),
                        hp=hp_pool_from_hp(int(row.hp)),
                        armor_class=int(row.armor_class),
                    )
                    npc = Npc(
                        core=core,
                        creature_id=row.id,
                        origin=Origin(kind=OriginKind.GENERIC, creature_id=row.id),
                    )
                    snapshot.npcs.append(npc)
                    with encounter_opponent_seated_from_generics_span(
                        confrontation_type=str(getattr(cdef, "confrontation_type", "") or ""),
                        opponent=actor.name,
                        creature_id=row.id,
                        hp=int(row.hp),
                        armor_class=int(row.armor_class),
                        reason=(
                            "router-named opponent has no roster/pool backing — "
                            "seated from the world's authored bestiary generics"
                        ),
                    ):
                        pass
                elif frame_other or allow_synthetic_opponent:
                    # Two sanctioned mint paths: (a) the frame-sourced seat (see
                    # carve-out above — always allowed, the frame is authored
                    # content, no warning), and (b) the story-context degenerate
                    # carve-out (test fixtures, one-off scenario generation) —
                    # tolerated but WARNED. Both stay observable: the fabrication
                    # lie-detector span fires, the mint is stamped EPHEMERAL_STUB
                    # (162-2 provenance) and reaped with its encounter.
                    if not frame_other:
                        _log.warning(
                            "encounter.synthetic_stub_minted opponent=%r type=%r — "
                            "degenerate opt-in fabricated an ephemeral stub "
                            "(no roster/pool/generics source)",
                            actor.name,
                            str(getattr(cdef, "confrontation_type", "") or ""),
                        )
                    core = CreatureCore(
                        name=actor.name,
                        description="Combat opponent",
                        personality="Adversary",
                        inventory=Inventory(),
                        hp=hp_pool_from_hp(hp),
                        armor_class=ac,
                    )
                    npc = Npc(
                        core=core,
                        ephemeral=True,
                        origin=Origin(kind=OriginKind.EPHEMERAL_STUB),
                    )
                    snapshot.npcs.append(npc)
                    with encounter_opponent_minted_stub_span(
                        confrontation_type=str(getattr(cdef, "confrontation_type", "") or ""),
                        opponent=actor.name,
                        hp=int(hp),
                        armor_class=int(ac),
                        reason=(
                            "frame-sourced Other seated from the def's authored "
                            "opponent_default_stats (ADR-153 §6 / opponent_source=frame)"
                            if frame_other
                            else (
                                "degenerate opt-in (allow_synthetic_opponent) — no "
                                "roster/bestiary backing and no generics; fabricated "
                                "an ephemeral stub (author the encounter's adversary)"
                            )
                        ),
                    ):
                        pass
                else:
                    # No legitimate source left. Refuse, observably (No Silent
                    # Fallbacks) — the caller restores any half-seated encounter.
                    with encounter_stub_fabrication_refused_span(
                        confrontation_type=str(getattr(cdef, "confrontation_type", "") or ""),
                        opponent=actor.name,
                        reason=(
                            "no roster entry, no scene-active pool antagonist, "
                            "and the world's bestiary authors no generics section"
                        ),
                    ):
                        pass
                    raise ValueError(
                        f"no opponent source for {actor.name!r}: not in the roster, no "
                        f"scene-active pool antagonist, and the world's bestiary authors "
                        f"no generics section — author generic rows (bestiary.yaml "
                        f"`generics:`) or pass allow_synthetic_opponent=True on "
                        f"degenerate paths"
                    )
        elif npc.creature_id is not None:
            # 108-2: a BOUND, statted bestiary creature (resolved upstream or
            # named directly). Its authored HP pool IS the WWN-balanced math the
            # ruleset binding exists to inherit (SOUL "Bind the Ruleset, Don't
            # Balance It") — the confrontation's generic ``opponent_default_stats``
            # must NOT clobber it. Reset only ``current`` to the creature's OWN
            # max (combat-start full) and keep its own AC.
            npc.core.hp.current = npc.core.hp.max
        else:
            # Overwrite branch: a narrator-declared NPC with no real stats. Reset
            # its pool to FULL from the content default at combat START. This is a
            # start-of-fight assumption — a re-entry would heal the opponent, but
            # the ADR-116 no-reopen flow (an encounter resolves and is not
            # re-instantiated) prevents that.
            npc.core.hp = hp_pool_from_hp(hp)
            npc.core.armor_class = ac
        # Story 72-8: presence stamp — this opponent is on the board this turn.
        _stamp_encounter_presence(npc, turn=turn, location=actor_loc)
        # OTEL doctrine: distinguish the CREATE branch (a narrator-improv /
        # router-named opponent materialized fresh — the GM panel must see
        # this as an NPC-materialization event) from the OVERWRITE branch.
        # The stamped recency rides on the existing edge-published span so the
        # GM panel can confirm presence-stamping fired without a new span family.
        with npc_edge_published_span(
            npc_name=actor.name,
            current=npc.core.hp.current,
            max=npc.core.hp.max,
            source=source,
            turn_number=turn,
            created=created,
            seed_source="opponent_default_stats",
            last_seen_turn=npc.last_seen_turn,
            last_seen_location=npc.last_seen_location or "",
            # 153-10: non-empty when this opponent was PROMOTED from a pool
            # antagonist (vs fabricated as an ephemeral stub) — lets the GM panel
            # distinguish "seated the cast member" from "minted a phantom".
            pool_origin=pool_origin,
        ):
            pass
        # BUG 1 (eh-opp-damage) + Story 153-20: flag the seating CONTENT GAP at
        # INSTANTIATION. When this opponent has no AUTHORED reprisal damage source
        # (cdef.opponent_damage / strike damage_override / inventory weapon), every
        # enemy reprisal would land for 0 HP absent the ruleset's unarmed floor.
        # Surface it here, at seating, so the GM panel catches it immediately
        # instead of only via the per-turn ``dice.opponent_reprisal_damage_spec_missing``
        # warning six rounds deep.
        #
        # 153-20: a weaponless Without Number opponent is NOT actually toothless —
        # the SRD unarmed floor rescues it. ``reprisal_resolvable`` rides the WN-aware
        # detector verdict onto the span, and ``unarmed_floor`` (taken DIRECTLY from
        # the ruleset binding, never invented) is set only under a WN binding, so the
        # GM panel can tell "floor resolved" apart from a genuine (non-WN) toothless
        # flag. The span still fires on the content gap either way — author
        # ``opponent_damage`` to make the intent explicit (No Silent Fallbacks).
        opponent_core = npc.core
        if not _has_authored_reprisal_source(cdef, opponent_core):
            resolvable = _opponent_reprisal_damage_resolvable(cdef, opponent_core, ruleset)
            unarmed_floor = (
                ruleset.SRD_UNARMED_DICE
                if isinstance(ruleset, WithoutNumberRulesetModule)
                else None
            )
            # ``ruleset`` (always) + WN-only ``unarmed_floor`` discriminate
            # "floor resolved" from a genuine toothless flag; ``reprisal_resolvable``
            # rides the WN-aware verdict. Built as dict[str, Any] so the **spread
            # doesn't trip pyright's _tracer overload check on the span helper.
            span_attrs: dict[str, Any] = {
                "ruleset": str(getattr(ruleset, "slug", "") or ""),
                "reprisal_resolvable": resolvable,
            }
            if unarmed_floor:
                span_attrs["unarmed_floor"] = unarmed_floor
            with encounter_opponent_toothless_span(
                confrontation_type=str(getattr(cdef, "confrontation_type", "") or ""),
                opponent=actor.name,
                rationale=(
                    "hp_depletion combat seated this opponent with no AUTHORED "
                    "reprisal damage source: cdef.opponent_damage is unset, no "
                    "strike beat carries damage_override, and the opponent core "
                    "has no inventory weapon with a damage spec. "
                    + (
                        f"The {getattr(ruleset, 'slug', '')} SRD unarmed floor "
                        f"({unarmed_floor}) gives it a real reprisal — author "
                        "opponent_damage to make the intent explicit."
                        if unarmed_floor
                        else "No ruleset unarmed floor applies — the opponent is "
                        "toothless; author opponent_damage (No Silent Fallbacks)."
                    )
                ),
                **span_attrs,
            ):
                pass


def _seed_fate_opponents(
    *,
    snapshot: GameSnapshot,
    actors: list[EncounterActor],
    pack: GenrePack,
    turn: int,
    acting_character_name: str,
) -> None:
    """Ensure every opponent-side actor in a Fate confrontation has a backing
    CreatureCore carrying a FateSheet (ADR-144 F2d).

    The Fate analogue of ``_seed_combat_hp_depletion_to_npcs``: under a Fate-bound
    pack the conflict/contest engine resolves attacks + defenses against the Other's
    FateSheet (skill ladder + stress + consequences), so a seated opponent with no
    sheet is the impossible state ``decide_opponent_action`` / ``_resolve_attack``
    fail loud on (playtest 150-2 re-brick at THROW RESOLUTION). For every
    opponent-side actor we guarantee a backing ``Npc`` whose ``core.fate_sheet`` is
    populated:

    - a router-named opponent with no backing ``Npc`` is CREATED with a sheet
      (marked ``ephemeral`` so it is reaped with its resolved encounter). NOTE
      (162-3): this still mirrors the PRE-162-3 hp_depletion create branch — the
      Fate seeder does NOT yet consult bestiary ``generics:`` or refuse loudly;
      a Fate-genre generics sibling is scoped as follow-up (see 162-3 Delivery
      Findings / TEA Fate-seeder question);
    - a seated native-stat creature (a bestiary mob with ``fate_sheet=None``) has a
      sheet ATTACHED beside its existing core (the Fate facet rides ALONGSIDE the
      d20 core — fate_sheet.py: a Fate-bound creature simply ALSO has the facet);
    - a creature that already carries a sheet (an authored Fate adversary) is left
      untouched.

    No-op off a Fate pack — the native/WN opponent path owns those. Runs for EVERY
    Fate confrontation category (pre_combat standoff, combat contest, social), not
    just ``combat``: any Fate confrontation can seat an Other the engine must
    resolve against.
    """
    if not (pack and pack.rules and pack.rules.ruleset == "fate"):
        return

    from sidequest.game.creature_core import CreatureCore, Inventory
    from sidequest.game.ruleset.fate import FateRulesetModule
    from sidequest.game.session import Npc
    from sidequest.telemetry.spans.fate import fate_opponent_seeded_span

    module = get_ruleset_module("fate")
    # The registry returns the FateRulesetModule for 'fate'; assert for the type
    # checker (a non-Fate module here would be a registry misconfiguration).
    assert isinstance(module, FateRulesetModule)

    actor_loc = snapshot.party_location(perspective=acting_character_name)
    by_name = {npc.core.name: npc for npc in snapshot.npcs}
    for actor in actors:
        if actor.side != "opponent":
            continue
        npc = by_name.get(actor.name)
        if npc is None:
            # Story 126-32 (manifestation a): the narrated antagonist may have
            # been established on a PRIOR turn and is sitting in
            # ``snapshot.npc_pool``. Seating runs in the pre-narrator dispatch
            # bank (ADR-113), BEFORE this turn's post-narrator
            # ``_apply_npc_mentions`` mint — so the pool, NOT ``snapshot.npcs``,
            # is where a prior-turn narrated opponent lives. Promote it carrying
            # its narrated identity (pronouns / appearance / disposition) instead
            # of fabricating a hollow phantom (``description="Fate conflict
            # opponent"``, no pronouns) beside the cast member the player has been
            # talking to. Creature members are skipped — a bestiary mob is the
            # native/MM seater's job, not the Fate person-binder's.
            pool_member = next(
                (m for m in snapshot.npc_pool if m.name == actor.name and not m.is_creature),
                None,
            )
            if pool_member is not None:
                from sidequest.server.narration_apply import (
                    _promote_pool_member_to_npc,
                    _seed_invented_npc_identity,
                )

                npc = _promote_pool_member_to_npc(pool_member)
                _seed_invented_npc_identity(
                    npc=npc, member=pool_member, snapshot=snapshot, turn_num=turn
                )
                npc.core.fate_sheet = module.seed_opponent_fate_sheet(rules=pack.rules)
                snapshot.npcs.append(npc)
                created = False
            else:
                core = CreatureCore(
                    name=actor.name,
                    description="Fate conflict opponent",
                    personality="Adversary",
                    inventory=Inventory(),
                    fate_sheet=module.seed_opponent_fate_sheet(rules=pack.rules),
                )
                npc = Npc(core=core, ephemeral=True)
                snapshot.npcs.append(npc)
                created = True
        elif npc.core.fate_sheet is None:
            npc.core.fate_sheet = module.seed_opponent_fate_sheet(rules=pack.rules)
            created = False
        else:
            continue  # authored Fate adversary already carries a sheet — untouched
        _stamp_encounter_presence(npc, turn=turn, location=actor_loc)
        assert npc.core.fate_sheet is not None  # set on both seeded branches above
        fate_opponent_seeded_span(
            opponent=actor.name,
            skill_count=len(npc.core.fate_sheet.skills),
            refresh=npc.core.fate_sheet.refresh,
            created=created,
        )


def _roll_and_persist_initiative(
    *,
    snapshot: GameSnapshot,
    enc: StructuredEncounter,
    actors: list[EncounterActor],
    cdef,
    pack: GenrePack,
) -> None:
    """SWN P4: roll 1d8+DEX once for player+opponent actors, persist on the
    encounter, emit the polygraph span. No-op for rulesets with no ordering.

    DEX is resolved at THIS seam because CreatureCore/Npc carry no ability
    scores: PCs from Character.stats[attribute_map['DEXTERITY']], opponents
    from the content `dexterity` reserved key (guaranteed present by the
    ConfrontationDef load-time validator). Fail loud on a missing PC score.
    """
    import random

    from sidequest.game.ruleset import get_ruleset_module
    from sidequest.telemetry.spans.encounter import encounter_initiative_rolled_span

    cfg = pack.rules.ruleset_config()
    if cfg is None:
        return  # non-SWN ruleset: no ordering (native returns None anyway)

    dex_key = cfg.attribute_map.get("DEXTERITY")
    if dex_key is None:
        raise ValueError(
            "ruleset 'swn' but attribute_map has no DEXTERITY entry — "
            "RulesConfig validator should have caught this"
        )

    char_by_name = {c.core.name: c for c in snapshot.characters}
    actor_dex_scores: dict[str, int] = {}
    for actor in actors:
        if actor.side == "opponent":
            dex = cdef.opponent_dexterity
            if dex is None:
                raise ValueError(
                    f"opponent '{actor.name}' has no dexterity in "
                    f"opponent_default_stats for '{cdef.confrontation_type}' — "
                    "the load-time validator should have required it"
                )
            actor_dex_scores[actor.name] = int(dex)
        elif actor.side == "player":
            ch = char_by_name.get(actor.name)
            if ch is None:
                # Story 59-35: a FRIENDLY NPC ally seated side="player" carries
                # no SWN ability scores (CreatureCore has none) — it does not
                # roll its own initiative; it acts on narrator beats like an
                # opponent NPC. Skip it here. A player-side actor that is NEITHER
                # a Character NOR a roster Npc is a real name-skew defect — fail
                # loud (No Silent Fallbacks), preserving the original guard for
                # genuine PCs.
                if any(n.core.name == actor.name for n in snapshot.npcs):
                    continue
                raise ValueError(
                    f"player actor '{actor.name}' not found among snapshot.characters "
                    "— cannot resolve DEX for initiative (no silent fallback)"
                )
            score = ch.stats.get(dex_key)
            if score is None:
                # 158-28: degrade LOUDLY (ADR-006), don't wedge the turn. The
                # encounter is already seated; the typed error lets the
                # confrontation handler keep the seat and surface an error
                # outcome the GM panel sees, instead of a bare ValueError.
                _log.warning(
                    "encounter.initiative_unresolvable actor=%s dex_key=%s stats=%s "
                    "— seated combat cannot roll initiative (degrading loudly)",
                    actor.name,
                    dex_key,
                    sorted(ch.stats),
                )
                raise InitiativeUnresolvableError(
                    f"player '{actor.name}' stat block has no '{dex_key}' "
                    f"(DEXTERITY flavor) — cannot roll initiative (stats={sorted(ch.stats)})"
                )
            actor_dex_scores[actor.name] = int(score)
        # neutral actors do not act -> excluded from initiative.

    ruleset = get_ruleset_module(pack.rules.ruleset)
    entries = ruleset.roll_initiative(actor_dex_scores=actor_dex_scores, rng=random.Random())
    if not entries:
        return
    enc.initiative = entries
    order_str = ", ".join(f"{e.token_id}({e.value})" for e in entries)
    with encounter_initiative_rolled_span(
        encounter_type=enc.encounter_type,
        initiative_order=order_str,
        source="instantiate",
    ):
        pass


def _publish_combat_edge_to_npcs(
    *,
    snapshot: GameSnapshot,
    actors: list[EncounterActor],
    opponent_metric,
    turn: int,
    source: str,
    acting_character_name: str,
) -> None:
    """Story 45-21 / 45-52: publish dial-derived edge onto opponent ``Npc``s.

    DIAL-THRESHOLD path only. For each opponent-side ``EncounterActor``
    whose ``name`` matches an ``Npc`` in ``snapshot.npcs``, overwrite the
    npc's ``core.hp`` pool using the opponent dial as the canonical pool
    size:

        max     = opponent_metric.threshold
        current = max(1, threshold - current)

    The opponent dial is ascending — when ``current`` reaches ``threshold``
    the opponent loses (= defeated). Inverting it into a descending HP
    view gives narrator / GM panel a consistent "current > 0 = alive"
    read while keeping the dial as the single source of truth.

    hp_depletion combats (no dial) are handled by
    ``_seed_combat_hp_depletion_to_npcs`` instead — this function is the
    legacy dial-derived path preserved for ``win_condition: dial_threshold``
    packs (do not regress them).

    Renamed from ``_publish_combat_stats_to_registry`` in story 45-52 —
    the legacy ``npc_registry`` is gone; per ADR-114 (HP restored) and
    ADR-014 (materialization seam) the canonical home for runtime
    creature pools is ``Npc.core.hp``. Emits one
    ``npc.edge_published`` OTEL span per write so the GM panel can verify
    the seam fired.

    No-op for actors with no matching ``Npc`` — the auto-promotion seam in
    ``narration_apply`` promotes pool members on cite; here we only update
    what is already there. CLAUDE.md "no silent fallback": the no-match
    case is expected for the player actor and pool-fallback synthetic
    mentions that haven't been promoted to stateful Npcs yet.
    """
    if opponent_metric is None:
        return
    threshold = int(getattr(opponent_metric, "threshold", 0) or 0)
    current_dial = int(getattr(opponent_metric, "current", 0) or 0)
    if threshold <= 0:
        # Defensive: a zero-threshold dial would publish current=0/max=0,
        # which is exactly the bug shape this story exists to fix.
        return
    hp_max = threshold
    # HpPool requires a positive ceiling — clamp to 1 so an opponent already
    # at the dial cap still publishes a representable pool.
    hp_current = max(1, threshold - current_dial)

    # Story 72-8: resolve the acting character's location ONCE (see the
    # hp_depletion sibling) — shared by every opponent seated this turn.
    actor_loc = snapshot.party_location(perspective=acting_character_name)

    by_name = {npc.core.name: npc for npc in snapshot.npcs}
    for actor in actors:
        if actor.side != "opponent":
            continue
        npc = by_name.get(actor.name)
        if npc is None:
            continue
        npc.core.hp.max = hp_max
        npc.core.hp.base_max = hp_max
        npc.core.hp.current = hp_current
        # Story 72-8: presence stamp — this opponent is on the board this turn.
        _stamp_encounter_presence(npc, turn=turn, location=actor_loc)
        with npc_edge_published_span(
            npc_name=actor.name,
            current=hp_current,
            max=hp_max,
            source=source,
            turn_number=turn,
            last_seen_turn=npc.last_seen_turn,
            last_seen_location=npc.last_seen_location or "",
        ):
            pass


# ADR-116 ("A Confrontation Requires an Other"): a confrontation needs at
# least one opponent-side participant. Adversarial categories source an
# opponent (and fail loud if none is available); a one-sided "chase" is not a
# confrontation — it's narration ("race against time"). Staged rollout:
# ``combat`` and ``movement`` are enforced now; ``social`` / ``pre_combat`` are
# deferred pending validation against the social-first packs (victoria,
# tea_and_murder) so we don't regress a parley shape.
_ADVERSARIAL_CATEGORIES = frozenset({"combat", "movement"})

# Story 59-17: role tokens that mark a same-location NPC as a genuine
# adversary for sealed-letter (1v1) candidate sourcing. ``npc_role_id`` is a
# free-form string, so this is a conservative allowlist — combined with the
# hostile-disposition signal in ``_npc_is_adversary`` it covers (a) explicitly
# hostile-tagged NPCs and (b) bestiary-materialized creatures (disposition
# default -20). Anything else (a deck-crew bystander, a merchant, an ally,
# a None role with neutral disposition) is NOT a duel candidate.
_ADVERSARIAL_ROLE_IDS = frozenset(
    {"hostile", "enemy", "opponent", "adversary", "rival", "antagonist"}
)

# Story 59-23 (#C3): ship-scale confrontations whose Other is a SHIP, never a
# person standing in the room. For these the person-location fallback
# (``_npc_fallback_at_location``) must NOT source opponents — it would conscript
# the player's own crew (who share the bridge) as the enemy hull. The Other must
# arrive as a materialized/named threat (ADR-116); absent one, the No-Opponent
# guard fails loud. Personal combat / brawls / chases are unaffected — they
# legitimately seat people in the room (story 59-13's chase dial depends on it).
_SHIP_SCALE_CONFRONTATION_TYPES = frozenset({"ship_combat"})


def _is_adversarial(category: str) -> bool:
    return category in _ADVERSARIAL_CATEGORIES


def _requires_opponent(cdef) -> bool:
    """True when this confrontation MUST seat an opponent-side Other.

    Two sources of the requirement:

    1. Adversarial category (``combat`` / ``movement``) — ADR-116's staged
       rollout, unchanged.
    2. ``resolution_mode: opposed_check`` — an opposed check resolves by
       rolling BOTH sides' d20+modifier each beat (``narration_apply.
       _resolve_opposed_check_branch``). That branch finds the opposing
       roller via ``actor.side == "opponent"`` and hard-fails if none is
       seated, so the Other MUST be opponent-side regardless of category.
       This is the precise completion of ADR-116's social deferral
       (playtest 59-8, Keith's "dice-driven" call): we don't make *all*
       ``social`` adversarial — only the ones that actually roll an
       opposed check need (and get) a metric-bearing Other. A social
       ``beat_selection`` parley still seats its NPC as ``neutral``.
    3. ``resolution_mode: contest`` — a Fate Contest also resolves by rolling
       BOTH sides each exchange; the contest engine
       (``fate_contest.run_fate_contest_exchange`` → ``_seat_opponent_commits``)
       raises if no opponent is seated, so the Other MUST be opponent-side
       regardless of category (ADR-116). A contest with nobody on the other
       side cannot resolve.
    4. ``resolution_mode: conflict`` (story 153-3) — a Fate Conflict resolves
       attacks/defenses against the Other's ``FateSheet`` stress, so it always
       needs an opponent-side Other regardless of category. The combat-category
       conflicts in play are already covered by (1); folding ``conflict`` in here
       closes the gap for a future mental/social Conflict.
    """
    if _is_adversarial(cdef.category):
        return True
    return cdef.resolution_mode in (
        ResolutionMode.opposed_check,
        ResolutionMode.contest,
        ResolutionMode.conflict,
    )


def _npc_is_adversary(npc: Npc) -> bool:
    """Sealed-letter duel candidacy: does this same-location NPC read as the Other?

    A sealed-letter encounter (commit-reveal duel) seats exactly one
    opponent. When the router supplies no explicit ``npcs_present`` (Story
    59-17), the opponent is sourced from ``snapshot.npcs`` at the player's
    location — but a 1v1 duel must NOT conscript a bystander who merely
    happens to share the room (Story 45-33; ADR-116 "an Other", not "any
    warm body"). An NPC qualifies only if it reads as an adversary:

    - ``disposition.attitude() == HOSTILE`` — the production signal for
      bestiary-materialized creatures (disposition default -20), and for any
      NPC the disposition engine has turned hostile through play; OR
    - ``npc_role_id`` is an explicit adversarial token (``_ADVERSARIAL_ROLE_IDS``).

    Neutral disposition + non-adversarial/None role ⇒ NOT an adversary. The
    caller then sees zero candidates and the sealed-letter arity validator
    raises loudly ("got 0 npcs_present") rather than silently seating the
    bystander (CLAUDE.md No Silent Fallbacks). This is deliberately
    conservative: when hostility is ambiguous, refuse the duel — never
    substitute a wrong Other.
    """
    if npc.disposition.attitude() == Attitude.HOSTILE:
        return True
    role = (npc.npc_role_id or "").strip().lower()
    return role in _ADVERSARIAL_ROLE_IDS


def _co_located(npc: Npc, *, pc_region: str | None, scene_match: bool) -> bool:
    """ADR-116 co-location for seating. Prefer the engine-owned region id when
    the candidate carries a region stamp (procedural-dungeon creatures, Task 1);
    otherwise fall back to the caller's free-text scene match — narrator NPCs and
    all non-procedural worlds carry no ``region``, so their behavior is unchanged.
    This is the fix for region-keyed seating: co-location no longer depends on
    the narrator-owned scene string that drifts across seams/turns (Gap B)."""
    if npc.region and pc_region:
        return npc.region == pc_region
    return scene_match


def _npc_fallback_at_location(
    snapshot: GameSnapshot,
    *,
    adversarial: bool,
    acting_character_name: str | None = None,
    adversary_only: bool = False,
) -> tuple[list, bool]:
    """Synthesise NpcMention entries from snapshot.npcs at the player's location.

    Story 45-18 (Playtest 3 Orin): when the narrator emits
    ``confrontation=combat`` with an empty ``npcs_present`` list (the
    structured-output extraction dropped the adversary), the encounter would
    start with ``actors=[player only]`` and opponent-side beats would either
    raise "unknown actor" or be silently dropped — opponent_metric stuck at 0
    for 6 rounds. ``snapshot.npcs`` records each NPC's ``last_seen_location``
    from prior turns, so we use it as a fallback population source.

    Story 45-52: rewired from the legacy ``snapshot.npc_registry``
    (removed) to ``snapshot.npcs``. Both stores carried ``last_seen_*``
    fields; the post-Wave-2A canonical home is ``Npc.last_seen_location``.

    Filter: same location as the player. NPCs last seen elsewhere are NOT
    pulled into the encounter — that would over-register characters who
    happened to be in the roster at all.

    Side: adversarial encounters (combat + movement/chase, per ADR-116
    ``_is_adversarial``) default to ``opponent`` for the fallback NPCs/mobs —
    the per-side dials require this so the opposing-side dial can advance when
    the pursuer's beat fires (a chase pursuer filed ``neutral`` froze the dial
    at 0, story 59-13). Non-adversarial encounters use ``neutral`` — the
    narrator can re-classify them on a later turn via an explicit
    ``npcs_present`` mention. ``snapshot.npcs`` carries both narrator-declared
    NPCs and bestiary mobs (``creature_id`` set); both are seatable here.

    Story 59-17: ``adversary_only`` (sealed-letter 1v1 sourcing) additionally
    filters candidates through ``_npc_is_adversary`` so a same-location
    bystander is never promoted into a duel. The non-sealed path leaves this
    False — a brawl/chase pulls in every location NPC as an opponent (story
    59-13's chase dial depends on it).

    Story 59-35: when ``adversarial`` is True the opponent fallback ALSO skips
    ``Attitude.FRIENDLY`` NPCs — a co-located companion fights at the player's
    side (``_friendly_fallback_at_location`` seats it ``side="player"``), it is
    never conscripted as the Other. So the ``adversary_only=False`` contract is
    "every HOSTILE/NEUTRAL location NPC" (no longer literally *every* NPC). The
    chase-dial guarantee holds for hostile/neutral pursuers; only friendlies are
    diverted to the friendly-seater.

    Returns ``(mentions, location_available)`` so the caller can decorate
    the empty-result span: ``location_available=False`` means the player
    had no resolved location (silent-failure detector — story 45-52,
    Reviewer's findings) rather than "no NPCs at this location."
    """
    from sidequest.agents.orchestrator import NpcMention

    # Wave 2B (story 45-48): "the player's location" is the acting PC's
    # per-character location; party-frame fallback uses the consensus
    # accessor (returns None when seated PCs disagree, matching the prior
    # "no global ⇒ no fallback" semantics).
    location = snapshot.party_location(perspective=acting_character_name)
    if not location:
        return [], False
    pc_region = snapshot.region_for(perspective=acting_character_name)
    default_side = "opponent" if adversarial else "neutral"
    fallback: list = []
    for npc in snapshot.npcs:
        if not _co_located(
            npc, pc_region=pc_region, scene_match=(npc.last_seen_location == location)
        ):
            continue
        # Story 59-17: sealed-letter sourcing seats only genuine adversaries —
        # a neutral-disposition bystander sharing the room is not the Other.
        if adversary_only and not _npc_is_adversary(npc):
            continue
        # Story 59-35: an adversarial (opponent-sourcing) fallback must NOT
        # conscript a FRIENDLY ally as the enemy — a co-located companion fights
        # at the player's side (``_friendly_fallback_at_location`` seats it
        # side="player"), never as the Other. Hostile/neutral NPCs are still
        # seated as opponents. (``adversary_only`` already excludes friendlies
        # via ``_npc_is_adversary``; this covers the general brawl/chase
        # opponent fallback where every same-location NPC was otherwise pulled
        # in as an opponent.)
        if adversarial and npc.disposition.attitude() == Attitude.FRIENDLY:
            continue
        fallback.append(
            NpcMention(
                name=npc.core.name,
                pronouns=npc.pronouns or "",
                role=npc.npc_role_id or "",
                appearance=npc.appearance or "",
                side=default_side,
            )
        )
    return fallback, True


def _reconcile_surfaced_adversary(
    snapshot: GameSnapshot,
    *,
    location: str,
    turn: int,
) -> Npc | None:
    """158-1 ([WWN-COMBAT-NEVER-SEATS]): when a combat target has NO co-located
    adversary, recover a bound Monster-Manual adversary the narrator surfaced
    on-stage whose stored zone drifted from the PC's current scene.

    The forensic shape (beneath_sunden ``8b54610d``): the authored entrance
    Gnaw-Swarm sits in ``snapshot.npcs`` at its authored room ("Under the Rope")
    while the PC descended to a procedural region; the narrator dragged it forward
    in prose ("pooling at your feet") but the engine kept it at the entrance. The
    co-location projection (``_resolve_opponent_from_roster`` /
    ``_npc_fallback_at_location``, ADR-116) finds no Other → the router never seats
    → the narrator free-narrates the fight and fabricates player HP.

    Recovery is deliberately NARROW — the same over-reach guard
    ``_resolve_opponent_from_roster`` documents (region-wide sourcing is forbidden;
    ADR-116). A candidate must be:

      * ``creature_id``-statted AND ``manual_origin`` — a bound bestiary adversary
        (ADR-059), never a narrator-invented person;
      * adversarial and not ``Attitude.FRIENDLY``;
      * carrying a REAL stored zone (at least one of ``location`` /
        ``last_seen_location`` non-None) that is stale relative to the PC's scene —
        a creature with NO location is unplaced, not zone-drifted;
      * surfaced THIS turn or the immediately-preceding one — ``last_seen_turn > 0``
        (the model's ``0`` means "never mentioned this session" — a never-surfaced
        creature is not "engaged this turn") AND ``0 <= turn - last_seen_turn <= 1``.
        Narration stamps ``last_seen_turn`` AFTER the seater runs, so a creature the
        narrator put on-stage last turn carries ``turn - 1`` at this turn's seat
        time; a creature last seen earlier (or never) is genuinely off-stage and is
        left untouched (the 158-1 no-over-reach AC);
      * NOT already co-located (else the normal candidate scan already had it).

    The most-recently-surfaced match has its ``last_seen_location`` and
    ``location`` reconciled to the PC's scene (SOUL "Yes, And" — trust the
    narrator's on-stage placement), emitting ``encounter.creature_zone_reconciled``
    for the GM panel. Returns the reconciled, now-co-located ``Npc`` or ``None``.
    """
    candidates = [
        n
        for n in snapshot.npcs
        if n.creature_id is not None
        and n.manual_origin
        and _npc_is_adversary(n)
        and n.disposition.attitude() != Attitude.FRIENDLY
        # Must carry a REAL stored zone to have drifted FROM — a creature with no
        # location at all (both fields None) is unplaced, not zone-drifted, and
        # would emit a phantom from_location="" span (review finding).
        and (n.last_seen_location is not None or n.location is not None)
        and n.last_seen_location != location
        and n.location != location
        # Surfaced THIS turn or the immediately-preceding one. ``last_seen_turn > 0``
        # excludes the model's "never mentioned in this session" default (0) — a
        # never-surfaced creature is NOT "engaged this turn" and reconciling it is
        # the region-wide over-reach ADR-116 forbids (review finding: the window
        # ``0 <= 1 - 0 <= 1`` would otherwise admit it at interaction==1).
        and n.last_seen_turn > 0
        and 0 <= turn - n.last_seen_turn <= 1
    ]
    if not candidates:
        return None
    # Most recently surfaced, then highest threat, then name (deterministic).
    candidates.sort(
        key=lambda n: (n.last_seen_turn, n.threat_level or 0, n.core.name),
        reverse=True,
    )
    chosen = candidates[0]
    from_location = chosen.last_seen_location or chosen.location or ""
    with encounter_creature_zone_reconciled_span(
        creature_name=chosen.core.name,
        creature_id=chosen.creature_id or "",
        from_location=from_location,
        to_location=location,
        last_seen_turn=chosen.last_seen_turn,
        current_turn=turn,
    ):
        pass
    chosen.last_seen_location = location
    chosen.location = location
    return chosen


def _resolve_opponent_from_roster(
    snapshot: GameSnapshot,
    *,
    threat_name: str,
    acting_character_name: str | None,
    confrontation_category: str,
    is_fate: bool,
) -> Npc | None:
    """108-2: reconcile a router-named free-string opponent to a bound, statted
    adversary present in the scene BEFORE the seater fabricates a stub.

    The intent router names the adversary as a free string
    (``confrontation params["opponent"]``); when that string matches no roster
    ``Npc`` the seater used to fabricate a generic HP-10 placeholder (the
    "Hold-Dead, Still at the Shift" / "Arena Opponent" stubs) while the
    narrator's own prose referenced a BOUND bestiary creature ("Molgrath the
    Eyeless", HP 24) — a player-visible identity split (prose name ≠ combat-panel
    name) and a discard of the WWN-balanced Monster-Manual stats (107-2 /
    ADR-059). The narrator knows the roster; the seater didn't consult it.

    Returns the co-located bound creature to seat in the router name's place, or
    ``None`` to leave the router name as-is — because it already matches a roster
    entry (seat it directly; the seater dedups), because no co-located bound
    adversary exists (the truly-novel fight: since 162-3 the seater seats a
    bestiary generic, mints a frame-sourced/degenerate-opt-in stub, or RAISES —
    downstream), or because the confrontation is one where conscription is
    never right: a NON-combat confrontation (150-2) or ANY confrontation under a
    FATE binding (153-9 — ``is_fate``; a Fate conflict resolves on FateSheet
    stress, not the bound creature's hp, so there is nothing to preserve).

    A candidate is a ``creature_id``-statted, adversarial (``_npc_is_adversary``),
    non-friendly NPC at the acting PC's resolved location — the same room signal
    (``last_seen_location`` / ``location``) ``_npc_fallback_at_location`` and the
    Monster-Manual injector use. Region-wide sourcing (``_co_located``) is now safe
    when the NPC carries a ``region`` stamp (Task 1 procedural creatures); the
    ``_co_located`` helper gates region-matching on that stamp so narrator NPCs and
    non-procedural worlds keep the exact free-text behaviour.
    """
    # A roster match — canonical name, recorded alias, or invented_from binding
    # (story 162-2: the unified resolver, replacing the exact-name scan) —
    # means the router named a real NPC: seat it directly (the seater resolves
    # the same way and reuses it). Resolution is only for unbacked inventions.
    if resolve_roster_npc(snapshot.npcs, threat_name) is not None:
        return None
    location = snapshot.party_location(perspective=acting_character_name)
    if not location:
        return None
    pc_region = snapshot.region_for(perspective=acting_character_name)
    candidates = [
        n
        for n in snapshot.npcs
        if n.creature_id is not None
        and _co_located(
            n,
            pc_region=pc_region,
            scene_match=(n.last_seen_location == location or n.location == location),
        )
        and _npc_is_adversary(n)
        and n.disposition.attitude() != Attitude.FRIENDLY
    ]
    if not candidates:
        # 158-1: no co-located adversary — but the player may be attacking a bound
        # bestiary creature the narrator surfaced on-stage whose stored zone
        # drifted from the PC's scene (the entrance Gnaw-Swarm the party moved
        # past). Reconcile its zone so the bound creature reaches the fight instead
        # of a fabricated stub. Native COMBAT only: a Fate conflict resolves on
        # FateSheet stress and a non-combat standoff never conscripts a bestiary
        # mob (the same 150-2 / 153-9 declines enforced below) — so there is
        # nothing to recover for those paths.
        if confrontation_category == "combat" and not is_fate:
            reconciled = _reconcile_surfaced_adversary(
                snapshot,
                location=location,
                turn=snapshot.turn_manager.interaction,
            )
            if reconciled is not None:
                candidates = [reconciled]
        if not candidates:
            return None
    # Deterministic pick: most recently in scene, then highest threat, then name.
    candidates.sort(
        key=lambda n: (n.last_seen_turn, n.threat_level or 0, n.core.name),
        reverse=True,
    )
    # 153-10 ([WWN-OTHER-SEATING]): the router named a scene-active antagonist the
    # narrator established on a PRIOR turn that lives in ``snapshot.npc_pool`` — a
    # PERSON, not yet promoted to ``snapshot.npcs``, so the candidate scan above
    # (gated ``creature_id is not None`` over ``snapshot.npcs``) cannot see it. On
    # a vague / non-roster target this is the "MM grab": an ambient bestiary mob
    # wins by default over the cast member the player actually engaged (playtest
    # 150-14 "Mistos Warden over the Daggereyes"). Prefer the pool antagonist —
    # decline the conscription so the caller seats the router-named threat, and the
    # hp seeder (``_seed_combat_hp_depletion_to_npcs``) promotes the pool member
    # downstream carrying its narrated identity. This is the native/WN sibling of
    # the Fate seater's 126-32a pool consultation. Applies in COMBAT too (unlike
    # the 150-2 / 153-9 declines below): a NAMED scene antagonist always outranks
    # an ambient mob, regardless of category. Emit the lie-detector span naming the
    # refused bestiary mob (No Silent Fallbacks).
    if any(m.name == threat_name and not m.is_creature for m in snapshot.npc_pool):
        with encounter_roster_resolution_skipped_span(
            router_name=threat_name,
            declined_name=candidates[0].core.name,
            confrontation_category=confrontation_category,
            reason="pool_antagonist",
        ):
            pass
        return None
    # Every candidate here is a bestiary monster (the filter is
    # ``creature_id is not None``). The 108-2 reconciliation exists to preserve a
    # bound creature's COMBAT hp stats (ADR-059) — so it is only correct for a
    # native (Without-Number / dial) COMBAT confrontation. Decline in two cases:
    #
    #   * 150-2 (Defect A) — NON-combat, any ruleset: a bestiary monster is never
    #     the right Other for a standoff / social duel / chase. dust_and_lead
    #     seated a "Western Diamondback" rattlesnake against a human drifter
    #     because the only co-located adversary was an ambient bestiary hazard.
    #   * 153-9 ([FATE-OTHER-SEATING]) — ANY category under a FATE binding: a Fate
    #     conflict resolves against the Other's FateSheet stress, NOT hp_depletion
    #     (ADR-143/144 "Bind the Ruleset"), so there is no bound-hp value to
    #     preserve. Conscripting an ambient co-located adversary over the
    #     narrator's NAMED scene-active antagonist is the bug: the router names
    #     "Silas Vance" and the seater grabs the same-surname "Marguerite Vance".
    #
    # Either way: decline the conscription and let the seater seat the
    # router-named threat instead; emit a lie-detector span so the GM panel sees
    # the engine refused the ambient adversary (No Silent Fallbacks). Native
    # (non-Fate) combat keeps the 108-2 behavior untouched.
    if confrontation_category != "combat" or is_fate:
        with encounter_roster_resolution_skipped_span(
            router_name=threat_name,
            declined_name=candidates[0].core.name,
            confrontation_category=confrontation_category,
            reason="fate_binding" if is_fate else "non_combat",
        ):
            pass
        return None
    # Story 162-2: conscription BINDS the router/prose name to the bound
    # creature durably — record it in the alias ledger (existing accretion
    # path, emits ``entity.alias_accreted``) so every later reference by
    # either name resolves to this one entity instead of re-running the
    # guessing stack (the two-names-one-enemy fork, closed permanently).
    from sidequest.game.alias_accretion import accrete_npc_aliases

    accrete_npc_aliases(
        candidates[0],
        [threat_name],
        turn=snapshot.turn_manager.interaction,
    )
    return candidates[0]


def _friendly_fallback_at_location(
    snapshot: GameSnapshot,
    *,
    acting_character_name: str | None = None,
) -> list[NpcMention]:
    """Story 59-35: source scene-present FRIENDLY NPCs as side="player" allies.

    The friendly half of ADR-116 seating — symmetric to
    ``_npc_fallback_at_location``, and the engine enactment of the SOUL "Guitar
    Solo" principle: an allied NPC at the player's side FIGHTS, it is never a
    silent spectator. Scans ``snapshot.npcs`` for NPCs at the acting PC's
    location whose ``disposition.attitude()`` is ``Attitude.FRIENDLY`` and
    returns an ``NpcMention`` per ally with ``side="player"``.

    Differs from the opponent fallback in two deliberate ways:

    1. **Additive, not empty-gated.** The opponent fallback fires only when
       ``npcs_present`` is empty; this runs regardless — an ally fights
       alongside the PC whether or not the narrator named the enemy. The caller
       keeps these mentions OUT of the ``npcs_present`` list the no-opponent
       guard inspects, so a friendly ally never satisfies "a confrontation
       requires an Other" (ADR-116 invariant unchanged).
    2. **Disposition is the sole gate.** Only ``Attitude.FRIENDLY`` qualifies;
       hostile/neutral NPCs are seated (if at all) by the opponent fallback /
       explicit ``npcs_present``. Mirrors the location filter (same
       ``last_seen_location`` as the acting PC) so an ally last seen elsewhere
       is not pulled in.

    Returns ``[]`` when the acting PC has no resolved location (No Silent
    Fallbacks — never seat an ally against a bogus location).
    """
    from sidequest.agents.orchestrator import NpcMention

    location = snapshot.party_location(perspective=acting_character_name)
    if not location:
        return []
    pc_region = snapshot.region_for(perspective=acting_character_name)
    allies: list[NpcMention] = []
    for npc in snapshot.npcs:
        if not _co_located(
            npc, pc_region=pc_region, scene_match=(npc.last_seen_location == location)
        ):
            continue
        if npc.disposition.attitude() != Attitude.FRIENDLY:
            continue
        allies.append(
            NpcMention(
                name=npc.core.name,
                pronouns=npc.pronouns or "",
                role=npc.npc_role_id or "",
                appearance=npc.appearance or "",
                side="player",
            )
        )
    return allies


def _build_table_seat_seeds(
    *,
    player_names: list[str],
    npc_names: list[str],
    snapshot: GameSnapshot,
) -> dict[str, dict]:
    """Derive per-seat private_state seeds from real actor sheets.

    Stat mappings (documented):
    - PC ``perception`` ← WIS modifier: (WIS - 10) // 2.
      Perception/notice is WIS-flavored in the native ruleset (see
      native.py stat_modifier pattern). The modifier scale (~-1..+5) is
      correct for the d20 opposed check in engine.py (``rng.randint(1,20)
      + perception``).
    - PC ``concealment`` ← DEX modifier: (DEX - 10) // 2.
      Concealing a cheat is a sleight-of-hand action; DEX is the
      canonical sleight stat. Same modifier scale as perception.
    - NPC ``ocean`` ← npc.ocean (dict with at least ``neuroticism`` key,
      or empty dict when None). Fed directly into the OCEAN policy knobs.
    - NPC ``disposition`` ← mapped from npc.disposition.attitude():
      Disposition.HOSTILE → "larcenous" (hostile NPC at a table is the
      closest real-data approximation to "inclined to cheat"); FRIENDLY
      and NEUTRAL both map to "neutral" (no cheat tendency). The policy
      checks ``disposition == "larcenous"``; this mapping is the
      reconciliation between the engine vocabulary and ADR-020's
      three-tier attitude model.

    Missing actor: empty seed + warning log (no crash). An NPC with no
    backing Npc record (``snapshot.npcs`` lookup miss) gets empty seed;
    the engine's .get(..., default) fallbacks apply and crunch degrades
    gracefully for that seat only.
    """

    def _stat_mod(score: int) -> int:
        return (score - 10) // 2  # native ruleset modifier formula

    def _score(stats: dict[str, int], stat_key_upper: str, stat_key_lower: str) -> int | None:
        # Membership check, NOT `.get() or`: a legitimate score of 0 is a
        # valid stat (modifier -5) and must NOT be treated as absent.
        if stat_key_upper in stats:
            return stats[stat_key_upper]
        if stat_key_lower in stats:
            return stats[stat_key_lower]
        return None

    by_char_name = {c.core.name: c for c in snapshot.characters}
    by_npc_name = {n.core.name: n for n in snapshot.npcs}

    seeds: dict[str, dict] = {}

    for name in player_names:
        char = by_char_name.get(name)
        if char is None:
            _log.warning(
                "table seat seed: PC %r not found in snapshot.characters — "
                "seeding empty (no stat mods applied); this is a character-name "
                "skew and should be investigated",
                name,
            )
            seeds[name] = {}
            continue
        stats = char.stats
        wis = _score(stats, "WIS", "wis")
        dex = _score(stats, "DEX", "dex")
        # An actor genuinely lacking the stat gets a 0 modifier (no bonus) —
        # documented, not silent: the seat still seeds, just with no edge.
        perception = _stat_mod(wis) if wis is not None else 0
        concealment = _stat_mod(dex) if dex is not None else 0
        seeds[name] = {"perception": perception, "concealment": concealment}

    for name in npc_names:
        npc = by_npc_name.get(name)
        if npc is None:
            _log.warning(
                "table seat seed: NPC %r not found in snapshot.npcs — "
                "seeding empty (no OCEAN/disposition applied)",
                name,
            )
            seeds[name] = {}
            continue
        ocean: dict = dict(npc.ocean) if npc.ocean else {}
        attitude = npc.disposition.attitude()
        # Disposition vocabulary reconciliation: the engine's _choose_beat
        # checks disposition == "larcenous". ADR-020 only gives three bands
        # (friendly/neutral/hostile). Map HOSTILE → "larcenous" because a
        # hostile NPC at a table is the real-data signal for cheat-inclined
        # behaviour. FRIENDLY and NEUTRAL map to "neutral" (no cheat branch).
        disposition_str = "larcenous" if attitude == Attitude.HOSTILE else "neutral"
        seeds[name] = {"ocean": ocean, "disposition": disposition_str}

    return seeds


def instantiate_table_encounter(
    *,
    cdef: ConfrontationDef,
    player_names: list[str],
    npc_names: list[str],
    stake_kind: str,
    stake_descriptor: str,
    seed: int,
    ruleset_slug: str,
    seat_seeds: dict[str, dict] | None = None,
) -> StructuredEncounter:
    """Build + deal a table_resolution StructuredEncounter.

    Seats every PC then every NPC (≥2 total or TableNeedsOthersError via
    deal_table), populates each private_state via the kind's deal(), seeds the
    pot from antes, and stamps win_condition=table_showdown. The dual dials are
    inert placeholders (same as the hp_depletion path). Emits table.dealt.

    ``seat_seeds`` is a dict keyed by party_name → seed dict to MERGE into each
    seat's private_state BEFORE deal() runs. Keys set here (perception,
    concealment, ocean, disposition) are NOT overwritten by deal() — deal only
    sets its own keys (cards/strength/cheat_trace for poker; valuation/max_bid
    for auction). This is the integration seam that bridges real actor stats
    (Character.stats, Npc.ocean/disposition) into the crunch layer. The trigger
    branch (``instantiate_encounter_from_trigger``) builds seat_seeds from the
    real snapshot; direct-call tests may pass them explicitly.
    """
    resolved_seeds: dict[str, dict] = seat_seeds or {}
    parties = [(name, True) for name in player_names] + [(name, False) for name in npc_names]
    seats: list[TableSeat] = []
    actors: list[EncounterActor] = []
    for idx, (party_name, is_pc) in enumerate(parties, start=1):
        seat_id = f"seat_{idx}"
        pre_seed = dict(resolved_seeds.get(party_name, {}))
        seats.append(
            TableSeat(
                seat_id=seat_id,
                party_name=party_name,
                is_pc=is_pc,
                status="active",
                private_state=pre_seed,
            )
        )
        if pre_seed:
            with table_seat_seeded_span(
                seat_id=seat_id,
                party_name=party_name,
                is_pc=is_pc,
                keys_seeded=",".join(sorted(pre_seed.keys())),
            ):
                pass
        # every seat is its own party; side is cosmetic for table types
        actors.append(
            EncounterActor(
                name=party_name,
                role=seat_id,
                side="player" if is_pc else "opponent",
            )
        )

    table_state = TableState(
        game_kind=cdef.table_game or "",
        seats=seats,
        pot=TablePot(
            stake_kind=stake_kind,
            stake_descriptor=stake_descriptor,
            contributions={s.seat_id: 0 for s in seats},
        ),
        order=[s.seat_id for s in seats],
        dealer_seat=seats[0].seat_id if seats else "",
        max_decision_points=cdef.max_decision_points,
    )

    module = get_ruleset_module(ruleset_slug)
    # Wrap the deal in the span so it captures the work and still emits even if
    # the deal raises (TableNeedsOthersError on <2). All attrs are known up front.
    with table_dealt_span(
        seat_count=len(seats), game_kind=table_state.game_kind, stake_kind=stake_kind
    ):
        module.deal_table(table_state, rng=_random.Random(seed))

    return StructuredEncounter(
        encounter_type=cdef.confrontation_type,
        win_condition=cdef.win_condition.value,
        category=cdef.category,
        player_metric=EncounterMetric(name="table_player_inert", threshold=1),
        opponent_metric=EncounterMetric(name="table_opponent_inert", threshold=1),
        actors=actors,
        table_state=table_state,
    )


def instantiate_encounter_from_trigger(
    *,
    snapshot: GameSnapshot,
    pack: GenrePack,
    encounter_type: str,
    player_name: str,
    npcs_present: list,
    genre_slug: str | None,
    additional_player_names: list[str] | None = None,
    security_tier: str | None = None,
    materialized_threat: NpcMention | None = None,
    allow_synthetic_opponent: bool = False,
) -> StructuredEncounter | None:
    """Create a StructuredEncounter when the narrator emits ``confrontation=T``.

    Writes the new encounter to ``snapshot.encounter`` and returns it.
    Returns ``None`` when an active (unresolved) encounter already exists —
    caller leaves the current encounter alone.

    Raises ``ValueError`` when ``encounter_type`` doesn't match any
    ConfrontationDef in the pack (CLAUDE.md: no silent fallback).

    Story 162-3: an hp_depletion combat opponent with no roster/pool backing
    seats from the world bestiary's authored ``generics:`` section — the
    sanctioned last resort. With no generics available this RAISES
    ``ValueError``; the refusal restores ``snapshot.encounter`` AND rolls back
    any opponent Npc the seeder appended for an earlier actor in the same pass
    (nothing half-seated, nothing fabricated — No Silent Fallbacks, including the
    multi-opponent case). A def declaring ``opponent_source: frame`` (or a
    sealed-letter Other, ADR-153 §6) is exempt: it seats from the def's own
    authored ``opponent_default_stats``, never generics.
    ``allow_synthetic_opponent=True`` is the explicit degenerate opt-in (test
    fixtures, one-off scenario generation): warn-and-mint the old ephemeral stub
    instead of raising. Production callers must never pass it.

    Raises ``ValueError`` when any NPC's side is not in {player, opponent, neutral}
    (CLAUDE.md: no silent fallback). Emits encounter_invalid_side_span for OTEL.

    The encounter's dual dials are taken from the matched ConfrontationDef.
    Actors are assigned side="player" for the calling player and side read from
    each NpcMention's ``side`` field (validated against {player, opponent, neutral}).

    Multiplayer (playtest 2026-05-03 [BUG] — confrontation widget missing
    in-fiction principal): a bundled MP turn produces ONE narrator call with
    both PCs' actions concatenated, but the trigger only carries one
    ``player_name`` — the action submitter for the barrier-firing frame. The
    other PCs in the bundle never reached the actor roster, so the client
    widget rendered only one PC even though both played the round. Pass
    ``additional_player_names`` (typically every other PC in
    ``snapshot.player_seats.values()`` minus ``player_name``) to seat them as
    side="player" actors. Solo callers and tests can leave it as ``None`` for
    back-compat. Sealed-letter (commit-reveal duel) encounters keep the
    strict 1-PC red / 1-NPC blue pairing — the resolver looks up actors by
    role tag and a third PC there would break role lookup.

    When ``npcs_present`` is empty the constructor falls back to NPCs from
    ``snapshot.npcs`` whose ``last_seen_location`` matches the player's
    current location (Story 45-18, rewired from the legacy ``npc_registry``
    in story 45-52). The fallback is only consulted when the explicit list
    is empty — an explicit ``npcs_present`` is always authoritative.

    Note: ``GenrePack`` has no ``.slug`` attribute; ``genre_slug`` must be
    passed explicitly by the caller (e.g. from ``sd.genre_slug`` or
    ``snapshot.genre_slug``).
    """
    from sidequest.game.encounter import EncounterMetric
    from sidequest.genre.models.rules import MetricDef

    current = snapshot.encounter
    if current is not None and not current.resolved:
        return None

    # Story 73-5: suppress the re-fired ``encounter.confrontation_initiated``
    # span on a confrontation's RESOLUTION turn. When the prior encounter is
    # resolved but is STILL the same ``encounter_type`` sitting on the snapshot
    # (the just-resolved confrontation hasn't been torn down yet), a re-dispatch
    # this turn is the same confrontation resolving — not a new one. The router
    # re-emits ``confrontation`` for the same type on the resolution turn
    # (e.g. social_duel concede), and the unguarded path below would rebuild a
    # fresh encounter and re-fire the cosmetic "initiated" span, showing the GM
    # panel a fresh confrontation on a turn that is actually resolving.
    #
    # Return None (the same no-op contract as the active-encounter branch
    # above) so no new encounter is built and no span fires. A GENUINELY new
    # confrontation only reaches here once the resolved encounter is torn down
    # (``snapshot.encounter is None`` ⇒ the first branch's guard passes), or
    # when it is a DIFFERENT ``encounter_type`` (a resolved fight replaced by a
    # distinct confrontation) — both still fire the span below.
    if current is not None and current.resolved and current.encounter_type == encounter_type:
        return None

    # ADR-153 §7 (158-30): a duel husk-reaped THIS turn stays reaped — refuse to
    # re-seat it the same turn. The husk_reaped clear (``reap_resolved_encounter_husk``,
    # turn start) must win over a same-turn re-dispatch / drift keep-alive, else a
    # resolved dogfight resurrects in Setup and soft-locks the player into ship
    # maneuvers on foot (coyote_star 2026-06-25). Keyed by (type, turn): a
    # genuinely-fresh dogfight on a LATER turn still seats (created_turn exemption),
    # because the stamped turn no longer matches the current interaction. The
    # refusal is observable (CLAUDE.md: OTEL is the lie detector), never silent.
    reaped = snapshot.husk_reaped_this_turn
    if (
        reaped is not None
        and reaped[0] == encounter_type
        and reaped[1] == snapshot.turn_manager.interaction
    ):
        _watcher_publish(
            "state_transition",
            {
                "field": "encounter",
                "op": "reseat_refused_husk_reaped",
                "encounter_type": encounter_type,
                "turn": str(snapshot.turn_manager.interaction),
                "source": "seater",
            },
            component="encounter",
        )
        return None

    defs = pack.rules.confrontations if pack.rules else []
    cdef = find_confrontation_def(defs, encounter_type)
    if cdef is None:
        raise ValueError(f"unknown encounter_type {encounter_type!r} — not in pack confrontations")

    # Free-for-all N-seat table resolution — exclusive of the dial/sealed-letter
    # paths. Seated every PC + every NPC as TableSeats; deals hands; stamps
    # win_condition=table_showdown. The dual dials are inert placeholders.
    if cdef.resolution_mode == ResolutionMode.table_resolution:
        additional = additional_player_names or []
        table_location_available = True
        npc_names_list = [getattr(n, "name", None) or str(n) for n in npcs_present]
        if not npc_names_list:
            # location fallback for table seats — adversary_only=False because
            # gamblers/auction participants need not be hostile.
            fallback, table_location_available = _npc_fallback_at_location(
                snapshot,
                adversarial=False,
                acting_character_name=player_name,
            )
            npc_names_list = [getattr(n, "name", None) or str(n) for n in fallback]
        # No table-mates after sourcing (explicit + fallback both empty) — a
        # one-seat hand is not a confrontation (ADR-116, generalized by
        # TableNeedsOthersError). Mirror the adversarial guard: surface the
        # lie-detector signal via OTEL and DECLINE the encounter (return None,
        # "caller leaves the current encounter alone") rather than letting
        # deal_table raise TableNeedsOthersError and 500 the turn —
        # confrontation.py only catches NoOpponentAvailableError.
        total_parties = 1 + len(additional) + len(npc_names_list)
        if total_parties < 2:
            with encounter_no_opponent_available_span(
                encounter_type=encounter_type,
                genre_slug=genre_slug or "",
                player_name=player_name,
                category=cdef.category,
                location_available=table_location_available,
            ):
                pass
            return None
        # stake_kind defaults to "money" for MVP; Task 16 will add content-declared
        # stake blocks. stake_descriptor is the confrontation label.
        all_player_names = [player_name, *additional]
        seat_seeds = _build_table_seat_seeds(
            player_names=all_player_names,
            npc_names=npc_names_list,
            snapshot=snapshot,
        )
        enc = instantiate_table_encounter(
            cdef=cdef,
            player_names=all_player_names,
            npc_names=npc_names_list,
            stake_kind="money",
            stake_descriptor=cdef.label,
            seed=snapshot.turn_manager.interaction,
            ruleset_slug=(
                pack.rules.ruleset
                if pack and pack.rules
                else _raise_missing_ruleset("table_resolution")
            ),
            seat_seeds=seat_seeds,
        )
        snapshot.encounter = enc
        # Story 150-3: stamp the birth turn so the location-change abandon ladder
        # (narration_apply) doesn't kill this table the same turn it's dealt — a
        # table scene moves the location to the table itself in the same response.
        enc.created_turn = snapshot.turn_manager.interaction
        return enc

    # Story 45-18: NPC fallback when narrator's npcs_present is empty.
    #
    # Story 59-17: sealed-letter encounters (commit-reveal duels) now ALSO
    # consult the location fallback. The production confrontation seam is
    # router-driven (Story 59-4 / ADR-113) and the pre-narrator pass
    # dispatches with a hardcoded ``npcs_present=[]``
    # (``intent_router_pass.py`` line 167) — it has no explicit actor
    # mentions to hand the subsystem. Before this story the fallback was
    # skipped for sealed-letter, so a dogfight could NEVER instantiate via
    # the live router even when the enemy pilot was right there in the scene
    # (ADR-116: "a confrontation requires an Other" — the Other existed but
    # was never seated).
    #
    # The leak this MUST avoid (Story 45-33): a 1v1 duel must not conscript a
    # neutral bystander who merely shares the room — that would pass the
    # arity check (count == 1) and silently seat the wrong Other. The arity
    # validator alone is NOT sufficient: it catches 0 and >1, but a lone
    # bystander (count == 1) sails through. So sealed-letter sourcing passes
    # ``adversary_only=True``, which filters candidates through
    # ``_npc_is_adversary`` (hostile disposition OR an adversarial role
    # token). A lone bystander ⇒ 0 candidates ⇒ the arity validator raises
    # loudly ("got 0 npcs_present"); a lone adversary ⇒ 1 ⇒ seated as blue.
    #
    # Rejected (Story 59-17 Architect consult): keying solely on
    # ``Disposition.attitude() == HOSTILE``. A narrator-declared opponent can
    # carry the DEFAULT (neutral) disposition, so disposition ALONE would
    # reject a real opponent — hence the role-token OR-branch in
    # ``_npc_is_adversary``. Category (``_is_adversarial``) is necessary but
    # not sufficient: it is encounter-level and cannot tell a pilot from a
    # bartender. The combined disposition-OR-role predicate is the
    # discriminator both Story 45-33 (bystander ⇒ skip) and Story 59-17
    # (hostile ⇒ seat) require.
    #
    # Story 45-52: ``location_available`` discriminates "empty location"
    # from "no location at all" — both produce an empty fallback, but only
    # the former is a legitimate empty-scene shape. The flag rides on the
    # ``encounter.no_opponent_available`` span below.
    location_available = True
    seating_source = "router_named"
    # 153-9 (ADR-116/143/144): resolve the Fate binding ONCE here. The 108-2
    # roster reconciliation below must DECLINE under a Fate binding (a Fate
    # conflict resolves on FateSheet stress, not bound hp), and the Fate-seating
    # de-nativization branch downstream (126-30) reuses the same flag.
    is_fate = bool(pack and pack.rules and pack.rules.ruleset == "fate")
    # ADR-153 §6 (158-34): the ship-scale firewall must cover EVERY seating door,
    # not only the location fallback. The intent router (ADR-113) can name a
    # co-located personal-scale creature (``is_creature``) as the dogfight
    # contact; it arrives in ``npcs_present`` and — being non-empty — would skip
    # both ship-scale branches below (each gated on ``not npcs_present``) and seat
    # as the enemy ship (the "Gengineered Killer" symptom, via the router door
    # instead of the location-fallback door #1084 already closed). A sealed-letter
    # confrontation is ship-scale: its Other is a ship/chassis, never a ground
    # creature. Drop personal-scale mentions here so an empty list flows into the
    # frame_default branch and seats a ship Other (ADR-116). NOT silent (CLAUDE.md
    # No Silent Fallbacks): the drop is logged, and the seat flips to
    # ``source="frame_default"`` on the participant.joined span — the GM-panel
    # lie-detector for where the Other came from.
    if cdef.resolution_mode == ResolutionMode.sealed_letter_lookup and npcs_present:
        _ship_scale_present = [m for m in npcs_present if not getattr(m, "is_creature", False)]
        if len(_ship_scale_present) != len(npcs_present):
            _rejected = [
                getattr(m, "name", "?") for m in npcs_present if getattr(m, "is_creature", False)
            ]
            _log.warning(
                "dogfight.ship_scale_firewall rejected personal-scale "
                "router-named opponent(s) %s for sealed-letter %r — sourcing a "
                "ship Other from the def frame instead (ADR-153 §6 / 158-34)",
                _rejected,
                encounter_type,
            )
            npcs_present = _ship_scale_present
    if materialized_threat is not None:
        # Story 59-23 (#C3 / ADR-116): the narrator/router named a threat that is
        # not an existing NPC entity. Seat THAT as the Other — never the location
        # fallback (which sources the player's own crew). The backing
        # CreatureCore (hull HP / AC) is created downstream by
        # ``_seed_combat_hp_depletion_to_npcs`` (Task 9) for the opponent actor
        # lacking an Npc. ``materialized`` distinguishes this seat from a
        # router-named or location-fallback one on the participant.joined span.
        #
        # 108-2: the router names the adversary as a FREE STRING. Before seating
        # (and persisting) an invention, reconcile it to a bound, statted
        # adversary present in the scene (ADR-059 Monster-Manual / ADR-116 the
        # Other). The narrator's prose already fights the bound creature; only
        # the router's separate call invented the placeholder name (the
        # Molgrath-vs-Hold-Dead split). When it resolves, seat the bound creature
        # — its WWN-statted HP reaches the fight instead of an HP-10 stub.
        # Rework round 1 (review [HIGH]): the router may name the threat by a
        # RECORDED alias / invented_from binding / case variant of a roster
        # NPC. Seat it under the CANONICAL name — every downstream consumer
        # resolves the opponent by exact actor name (``find_creature_core``:
        # HP bars, WN attack, query_encounter, edge publish), and a dial-path
        # confrontation never reaches the hp_depletion seeder that could
        # otherwise canonicalize. The resolver's own ``identity.resolved``
        # span makes the rebind observable; prose keeps the alias via the
        # ledger.
        known = resolve_roster_npc(snapshot.npcs, materialized_threat.name)
        if known is not None and known.core.name != materialized_threat.name:
            from sidequest.agents.orchestrator import NpcMention as _NpcMention

            materialized_threat = _NpcMention(
                name=known.core.name,
                pronouns=materialized_threat.pronouns,
                role=materialized_threat.role,
                appearance=materialized_threat.appearance,
                side=materialized_threat.side,
            )
        resolved_opponent = _resolve_opponent_from_roster(
            snapshot,
            threat_name=materialized_threat.name,
            acting_character_name=player_name,
            confrontation_category=cdef.category,
            is_fate=is_fate,
        )
        if resolved_opponent is not None:
            from sidequest.agents.orchestrator import NpcMention as _NpcMention

            with encounter_opponent_resolved_from_roster_span(
                router_name=materialized_threat.name,
                bound_name=resolved_opponent.core.name,
                creature_id=resolved_opponent.creature_id or "",
                match_scope="room",
            ):
                pass
            materialized_threat = _NpcMention(
                name=resolved_opponent.core.name,
                pronouns=resolved_opponent.pronouns or "",
                role=resolved_opponent.npc_role_id or "hostile",
                appearance=resolved_opponent.appearance or "",
                side="opponent",
            )
            seating_source = "roster_resolved"
        else:
            seating_source = "materialized"
        npcs_present = [materialized_threat]
    elif (
        not npcs_present
        and cdef.confrontation_type not in _SHIP_SCALE_CONFRONTATION_TYPES
        # ADR-153 §6: a sealed-letter dogfight is ship-scale — its Other is a
        # ship/chassis from the def frame (the frame_default branch below) or a
        # router-named contact, NEVER the personal-scale location fallback
        # (158-34: a co-located ground creature was conscripted as the enemy
        # vessel). Story 59-17 had enabled this fallback for sealed-letter so a
        # dogfight could seat at all; the frame_default branch replaces it with
        # the correct ship-scale source.
        and cdef.resolution_mode != ResolutionMode.sealed_letter_lookup
    ):
        seating_source = "location_fallback"
        npcs_present, location_available = _npc_fallback_at_location(
            snapshot,
            # opposed_check social confrontations (e.g. social_duel) need the
            # Other seated opponent-side so its dial can advance on its own
            # roll — _requires_opponent folds that in alongside the adversarial
            # categories (playtest 59-8).
            adversarial=_requires_opponent(cdef),
            acting_character_name=player_name,
            # Sealed-letter no longer reaches this branch (ADR-153 §6), so the
            # adversary-only narrowing it used to request is gone — a
            # beat_selection combat seats any co-located adversary per the
            # ``adversarial`` filter.
            adversary_only=False,
        )
    elif (
        not npcs_present
        and cdef.resolution_mode == ResolutionMode.sealed_letter_lookup
        and cdef.opponent_default_stats
    ):
        # ADR-153 §6: a sealed-letter dogfight with no router-named contact and
        # no located Other still requires an enemy ship (ADR-116). Source it from
        # the def frame — a generic enemy fighter whose hull/AC come from
        # opponent_default_stats via _seed_combat_hp_depletion_to_npcs downstream.
        # This is the ship-scale replacement for Story 59-17's personal location
        # fallback (removed for sealed-letter above). ``seating_source`` rides the
        # participant.joined span so the GM panel sees a frame-default seat.
        from sidequest.agents.orchestrator import NpcMention as _NpcMention

        seating_source = "frame_default"
        npcs_present = [
            _NpcMention(
                name=cdef.label or "Enemy Fighter",
                role="hostile",
                side="opponent",
            )
        ]
    # else (ship-scale + no materialized threat, OR a sealed-letter def lacking an
    # opponent_default_stats frame): leave npcs_present empty so the No-Opponent /
    # arity guard below fails loud — a ship fight needs an enemy ship, and the
    # player's own crew (who share the bridge) are never it.

    # Story 45-33 / ADR-116: adversarial empty+empty guard (CLAUDE.md "No
    # Silent Fallbacks"). If narrator's ``npcs_present`` was empty AND
    # ``_npc_fallback_at_location`` returned empty (no NPCs/mobs at the
    # player's location, or no resolved location), an adversarial encounter
    # would instantiate with ``actors=[player only]`` — the original
    # Playtest 3 (Orin) combat bug shape, and the frozen-dial chase shape
    # (story 59-13). Refuse here and surface the lie-detector signal via OTEL.
    #
    # Sealed-letter encounters bypass this guard — their own validator below
    # carries a more specific error message ("got 0 npcs_present").
    #
    # ADR-116 corrects story 45-33's movement exemption: "solo" means one
    # PLAYER, never "no opponent". A chase requires a pursuer; a one-sided
    # chase is not a confrontation — it's narration ("race against time"),
    # which the dispatch handler renders as prose when this raises. ``social``
    # / ``pre_combat`` (beat_selection) remain exempt from the category guard
    # (staged rollout — see ``_ADVERSARIAL_CATEGORIES``), BUT an opposed_check
    # confrontation of ANY category needs an Other to roll against — a duel of
    # wits with nobody on the other side cannot resolve — so _requires_opponent
    # folds opposed_check in here too (playtest 59-8).
    _pc_region = snapshot.region_for(perspective=player_name)
    with Span.open(
        SPAN_CONFRONTATION_COLOCATION,
        {
            "encounter_type": encounter_type,
            "pc_region": _pc_region or "",
            "match_mode": "region" if _pc_region else "scene",
            "opponent_count": len(npcs_present),
        },
    ):
        pass
    if (
        _requires_opponent(cdef)
        and cdef.resolution_mode != ResolutionMode.sealed_letter_lookup
        and not npcs_present
    ):
        with encounter_no_opponent_available_span(
            encounter_type=encounter_type,
            genre_slug=genre_slug or "",
            player_name=player_name,
            category=cdef.category,
            location_available=location_available,
        ):
            pass
        raise NoOpponentAvailableError(
            f"no opponent available for adversarial encounter {encounter_type!r} "
            f"(category={cdef.category!r}) after npc-location fallback "
            f"(player_name={player_name!r}, "
            f"location={snapshot.party_location(perspective=player_name)!r}, "
            f"location_available={location_available})"
        )

    # Story 59-35: source FRIENDLY allies for the combat-confrontation path.
    # Computed AFTER the no-opponent guard and kept SEPARATE from npcs_present
    # so an ally never counts toward "a confrontation requires an Other"
    # (ADR-116 invariant). Seated only in the generic branch below — never for
    # sealed-letter (strict 1v1 red/blue) or table_resolution (handled earlier).
    # ``friendly_seated_names`` lets the participant.joined loop tag these seats
    # source="friendly_fallback", distinct from PC seats (source="seat").
    friendly_allies: list[NpcMention] = []
    friendly_seated_names: set[str] = set()
    if cdef.category == "combat" and cdef.resolution_mode != ResolutionMode.sealed_letter_lookup:
        friendly_allies = _friendly_fallback_at_location(
            snapshot, acting_character_name=player_name
        )

    with encounter_confrontation_initiated_span(
        encounter_type=encounter_type,
        genre_slug=genre_slug or "",
    ) as _init_span:
        if cdef.resolution_mode == ResolutionMode.sealed_letter_lookup:
            # Sealed-letter encounters are commit-reveal duels addressed by
            # role tag ("red" / "blue") rather than the generic
            # "combatant" / "participant" labels. The handler at
            # ``server.dispatch.sealed_letter`` looks up actors by role —
            # if these tags drift, the handler raises a "missing role" error
            # that the GM panel can't trace back to the constructor.
            #
            # Validation (CLAUDE.md no-silent-fallbacks):
            #   - the def must carry an interaction_table — without it the
            #     downstream resolver has no cells to look up
            #   - exactly one opponent NPC must be supplied — the player is
            #     red, the opponent is blue, and there is no third role
            if not cdef.interaction_tables:
                # ADR-153 §3: the registry is the single lookup surface — a
                # legacy single interaction_table auto-registers into it at
                # model validation, so an empty registry means NO table of
                # either shape was authored.
                raise ValueError(
                    f"confrontation {encounter_type!r} declares "
                    f"resolution_mode=sealed_letter_lookup but has no "
                    f"interaction table — sealed-letter resolution requires a "
                    f"populated table (interaction_table or interaction_tables, "
                    f"loaded via `_from:` pointers)"
                )
            if len(npcs_present) != 1:
                with encounter_sealed_letter_arity_rejected_span(
                    encounter_type=encounter_type,
                    genre_slug=genre_slug or "",
                    player_name=player_name,
                    npc_count=len(npcs_present),
                ):
                    pass
                raise SealedLetterArityError(
                    f"sealed-letter encounter {encounter_type!r} requires "
                    f"exactly one opponent NPC (player=red, npc=blue); got "
                    f"{len(npcs_present)} npcs_present"
                )
            opponent = npcs_present[0]
            opponent_name = getattr(opponent, "name", None) or str(opponent)
            opponent_side_raw = getattr(opponent, "side", None) or "opponent"
            opponent_side = _validate_side(opponent_name, opponent_side_raw)
            actors = [
                EncounterActor(name=player_name, role=ROLE_RED, side="player"),
                EncounterActor(
                    name=opponent_name,
                    role=ROLE_BLUE,
                    side=opponent_side,
                ),
            ]
            # Seed transient fighter-frame HP into each pilot's per_actor_state
            # so the frame-HP resolver can read it during shot resolution and
            # depletion checks. Fail loud if the dogfight cdef lacks the HP
            # values — no silent default (CLAUDE.md no-silent-fallbacks).
            from sidequest.game.dogfight_shot import seed_frame_hp

            pc_frame_hp = cdef.player_hp
            opp_frame_hp = cdef.opponent_hp
            if pc_frame_hp is None or opp_frame_hp is None:
                raise ValueError(
                    f"dogfight ConfrontationDef {cdef.confrontation_type!r} missing "
                    f"fighter-frame HP "
                    f"(player_default_stats.hp={pc_frame_hp!r}, "
                    f"opponent_default_stats.hp={opp_frame_hp!r})"
                )
            for actor in actors:
                # side == "player" -> PC frame; otherwise the opponent
                # (ArityError above guarantees exactly one NPC).
                seed_frame_hp(
                    actor,
                    pc_frame_hp if actor.side == "player" else opp_frame_hp,
                )
        else:
            role = "combatant" if cdef.category == "combat" else "participant"
            actors = [
                EncounterActor(name=player_name, role=role, side="player"),
            ]
            seen_pc_names = {player_name}
            for extra in additional_player_names or []:
                if extra and extra not in seen_pc_names:
                    actors.append(EncounterActor(name=extra, role=role, side="player"))
                    seen_pc_names.add(extra)
            for npc in npcs_present:
                npc_name = getattr(npc, "name", None) or str(npc)
                side_raw = getattr(npc, "side", None) or "neutral"
                side = _validate_side(npc_name, side_raw)
                actors.append(EncounterActor(name=npc_name, role=role, side=side))
            # Story 59-35: seat scene-present FRIENDLY allies as side="player"
            # combatants (ADR-116 friendly half / SOUL Guitar Solo). Additive to
            # the opponent path; dedup against already-seated names so an ally the
            # narrator also named in npcs_present (or a PC) is not double-seated.
            already_seated = {a.name for a in actors}
            for ally in friendly_allies:
                if ally.name in already_seated:
                    continue
                actors.append(EncounterActor(name=ally.name, role=role, side="player"))
                already_seated.add(ally.name)
                friendly_seated_names.add(ally.name)

        # ADR-116: membership entry is observable. Emit a participant.joined
        # span per seated actor carrying side + source so the GM panel can
        # answer "why is this pursuer here?" (router-named vs sourced from the
        # location roster). Point-in-time span; ``: pass`` like the other
        # guard spans above.
        #
        # Story 72-12 ("presence means presence"): seating an NPC IS presence,
        # so stamp recency on every seated actor that resolves to a roster Npc —
        # not only combat opponents. 72-8 stamped just the combat seams below
        # (gated behind ``cdef.category == "combat"``), leaving non-combat
        # participants (a social duellist, an ally joining a parley) un-stamped
        # while demonstrably present. The stamp rides this same participant.joined
        # span so the GM panel sees it alongside the seating event. Combat
        # opponents are re-stamped by the 72-8 seams below with the SAME turn +
        # location, so the value stays consistent (no double-advance).
        _seat_turn = snapshot.turn_manager.interaction if hasattr(snapshot, "turn_manager") else 0
        _seat_loc = snapshot.party_location(perspective=player_name)
        _npc_by_name = {n.core.name: n for n in snapshot.npcs}
        for actor in actors:
            _seated_npc = _npc_by_name.get(actor.name)
            _stamp_attrs: dict[str, object] = {}
            if _seated_npc is not None:
                _stamp_encounter_presence(_seated_npc, turn=_seat_turn, location=_seat_loc)
                _stamp_attrs = {
                    "last_seen_turn": _seated_npc.last_seen_turn,
                    "last_seen_location": _seated_npc.last_seen_location or "",
                    # Story 59-35 (AC4): carry the seated NPC's disposition band so
                    # the GM panel can prove WHY the engine seated it — a
                    # friendly_fallback seat reads disposition_attitude="friendly",
                    # confirming the seat was disposition-driven, not narrator improv.
                    "disposition_attitude": _seated_npc.disposition.attitude().value,
                }
            # Story 59-35: a FRIENDLY ally seated by the friendly-seater carries
            # source="friendly_fallback" (the GM-panel lie-detector proving the
            # ENGINE seated the ally, not the narrator inventing one), distinct
            # from a PC seat (source="seat") and an opponent (seating_source).
            if actor.name in friendly_seated_names:
                _join_source = "friendly_fallback"
            elif actor.side == "player":
                _join_source = "seat"
            else:
                _join_source = seating_source
            with participant_joined_span(
                encounter_type=encounter_type,
                name=actor.name,
                side=actor.side,
                source=_join_source,
                **_stamp_attrs,
            ):
                pass

        # Story 45-18 AC3: GM-panel observability.
        # Decorate the init span with the registered combatants so Keith can
        # verify the actors array was populated end-to-end (and that the
        # registry fallback is firing on Playtest 3 shapes). OTEL string
        # attributes can't carry rich lists, so combatant_names is comma-joined.
        # ``set_attribute`` is a no-op on NoOp / non-recording spans — safe
        # to call unconditionally.
        _init_span.set_attribute("actor_count", len(actors))
        _init_span.set_attribute(
            "combatant_names",
            ",".join(a.name for a in actors),
        )
        # Playtest 2026-05-03 [BUG] — confrontation widget missing in-fiction
        # principal in MP. The GM panel needs to see how many side="player"
        # actors landed (sealed-letter is always 1; non-sealed-letter scales
        # with seated PC count). A regression to "always 1 PC" in MP shows up
        # here without grepping logs.
        _init_span.set_attribute(
            "pc_actor_count",
            sum(1 for a in actors if a.side == "player"),
        )
        _init_span.set_attribute(
            "pc_actor_names",
            ",".join(a.name for a in actors if a.side == "player"),
        )

        # Story 126-30 (Keith ruling 2026-06-19): de-nativize Fate confrontation SEATING.
        # Under a Fate binding a standoff/conflict resolves through Fate's OWN conflict
        # engine — 4dF + ladder, ablative stress toward taken-out, read off the Other's
        # FateSheet (ADR-143/144 "Bind the Ruleset, Don't Balance It") — so the native
        # ``opponent_metric.tension`` dial is REMOVED from the Fate path, never seated
        # alongside Fate (the upstream half of the #964 cleanup). A Fate Contest keeps its
        # OWN Fate path (``enc.contest``, below) with the metrics as its victory tally; a
        # sealed-letter duel is a commit-reveal table, not a conflict — both are excluded
        # from the conflict de-nativization. (``is_fate`` is resolved once above,
        # at the 108-2 roster-reconciliation gate — 153-9.)
        seat_as_fate_conflict = is_fate and cdef.resolution_mode not in (
            ResolutionMode.contest,
            ResolutionMode.sealed_letter_lookup,
        )

        # Synthesize inert metrics when a combat declares no dial (win_condition: hp_depletion).
        # apply_beat gates its dial-resolution branches on win_condition, so these placeholders
        # never gate resolution; the absurdly high threshold (1e6, never reached) is just
        # belt-and-suspenders that keeps the ~9 live-metric readers safe.
        pm = cdef.player_metric
        om = cdef.opponent_metric
        if seat_as_fate_conflict:
            # The native tension dial is REMOVED for a Fate conflict — inert placeholders
            # (the same belt-and-suspenders as hp_depletion below) keep the live-metric
            # readers safe; the authoritative win track is the opponent FateSheet stress
            # seeded by ``_seed_fate_opponents`` (AC-2), never these metrics.
            pm = MetricDef(name="fate_stress", starting=0, threshold=1_000_000)
            om = MetricDef(name="fate_stress", starting=0, threshold=1_000_000)
        elif pm is None and om is None:
            # hp_depletion: no dial authored — synthesize inert placeholders.
            pm = MetricDef(name="hp", starting=0, threshold=1_000_000)
            om = MetricDef(name="hp", starting=0, threshold=1_000_000)
        elif pm is None or om is None:
            raise ValueError(
                f"confrontation '{encounter_type}' has exactly one of "
                "player_metric/opponent_metric; provide both (dial_threshold) or "
                "neither (hp_depletion) — no silent discard"
            )

        # net_run (CWN hacking, spec 2026-05-29): resolve the security tier the
        # run targets. The "Other" is the alert dial, not an NPC, so this is the
        # only adversary metadata net_run needs. Non-hacking confrontations
        # leave security_tier=None.
        stamped_security_tier: str | None = None
        if cdef.category == "hacking":
            from sidequest.genre.models.rules import CwnConfig

            cfg = pack.rules.ruleset_config() if pack and pack.rules else None
            if not isinstance(cfg, CwnConfig) or cfg.hacking is None:
                raise ValueError(
                    f"net_run confrontation {encounter_type!r} requires "
                    "cwn.hacking config on the pack; none authored (No Silent "
                    "Fallbacks)"
                )
            stamped_security_tier = security_tier or cfg.hacking.default_tier
            if stamped_security_tier not in cfg.hacking.security_tiers:
                raise ValueError(
                    f"net_run security_tier {stamped_security_tier!r} is not in "
                    f"cwn.hacking.security_tiers {sorted(cfg.hacking.security_tiers)}"
                )

        enc = StructuredEncounter(
            encounter_type=encounter_type,
            # Story 126-30: a de-nativized Fate conflict carries the engine-only
            # ``fate_conflict`` win track (the native dial is removed); every other path
            # keeps the cdef-authored win condition.
            win_condition=("fate_conflict" if seat_as_fate_conflict else cdef.win_condition.value),
            category=cdef.category,
            player_metric=EncounterMetric(
                name=pm.name,
                current=pm.starting,
                starting=pm.starting,
                threshold=pm.threshold,
            ),
            opponent_metric=EncounterMetric(
                name=om.name,
                current=om.starting,
                starting=om.starting,
                threshold=om.threshold,
            ),
            beat=0,
            structured_phase=EncounterPhase.Setup,
            secondary_stats=None,
            actors=actors,
            outcome=None,
            resolved=False,
            mood_override=cdef.mood,
            narrator_hints=[],
            security_tier=stamped_security_tier,
        )
        # ADR-153 §3: a sealed-letter duel starts in its entry state — the
        # legacy single table's starting_state, else the first registered
        # state (registry preserves the content list order). Resume-safe:
        # the field serializes with the encounter.
        if cdef.resolution_mode == ResolutionMode.sealed_letter_lookup:
            enc.dogfight_state = (
                cdef.interaction_table.starting_state
                if cdef.interaction_table is not None
                else next(iter(cdef.interaction_tables), None)
            )
        # spec 2026-06-17 §2: a Fate Contest cdef stamps a first-to-N victory tally
        # onto the encounter. dispatch_fate_action reads encounter.contest to select
        # the Contest engine over the Conflict engine. target comes from the authored
        # metric threshold (the 0->3 victory tally that replaced the 0->7 dial).
        if cdef.resolution_mode == ResolutionMode.contest:
            from sidequest.game.encounter import ContestState
            from sidequest.telemetry.spans.fate import fate_contest_seeded_span

            # player_metric is guaranteed present in contest mode (ConfrontationDef
            # ._validate, Westley minor F1) — no silent ``else 3`` default.
            target = cdef.player_metric.threshold
            # spec 2026-06-17 §2 (Westley major M2): honor an authored victory
            # head-start. A content author can give either side a ``starting``
            # advantage (tea_and_murder negotiation opponent starts at 1); seating
            # MUST seed the tally from it or the authored asymmetry vanishes silently
            # (No Silent Fallbacks). opponent_metric is optional in contest mode, so
            # its head-start defaults to 0 when absent.
            player_start = cdef.player_metric.starting
            opponent_start = (
                cdef.opponent_metric.starting if cdef.opponent_metric is not None else 0
            )
            enc.contest = ContestState(
                target=target,
                player_victories=player_start,
                opponent_victories=opponent_start,
            )
            player_seats = sum(1 for a in actors if a.side == "player")
            fate_contest_seeded_span(
                encounter_type=encounter_type, target=target, player_seats=player_seats
            )
        elif seat_as_fate_conflict:
            # Story 126-30: the de-nativized Fate-conflict seat — the GM-panel
            # lie-detector that the native ``tension`` dial was REMOVED and resolution
            # runs through the 4dF conflict engine against the Other's FateSheet stress
            # (the upstream sibling of ``fate.contest.seeded``). Mutually exclusive with
            # the contest branch above (``seat_as_fate_conflict`` excludes contest mode).
            from sidequest.telemetry.spans.fate import fate_conflict_seeded_span

            fate_conflict_seeded_span(
                encounter_type=encounter_type,
                category=cdef.category,
                opponent_count=sum(1 for a in actors if a.side == "opponent"),
            )
        snapshot.encounter = enc
        # Story 150-3: stamp the birth turn (sibling of the table path above) so a
        # freshly-seated confrontation survives the location-change abandon ladder
        # on the turn it is created — see narration_apply's continued_fresh_this_turn.
        enc.created_turn = snapshot.turn_manager.interaction
        _watcher_publish(
            "state_transition",
            {
                "field": "encounter",
                "op": "started",
                "encounter_type": encounter_type,
                "player_metric_threshold": pm.threshold,
                "opponent_metric_threshold": om.threshold,
                "turn": snapshot.turn_manager.interaction
                if hasattr(snapshot, "turn_manager")
                else 0,
                "genre_slug": genre_slug or "",
            },
            component="encounter",
        )

        # ADR-144 F2d (playtest 150-2 re-brick fix): a Fate-bound pack's conflict/
        # contest engine resolves against the Other's FateSheet, so every seated
        # opponent must carry one (the Fate sibling of the hp_depletion/edge seeding
        # below). Runs for EVERY Fate confrontation category; no-op off a Fate pack.
        _seed_fate_opponents(
            snapshot=snapshot,
            actors=actors,
            pack=pack,
            turn=snapshot.turn_manager.interaction if hasattr(snapshot, "turn_manager") else 0,
            acting_character_name=player_name,
        )

        # Story 45-21 / 45-52: combat-stats emit → publish dial-derived edge
        # onto matching ``Npc.core.edge`` pools.
        #
        # Playtest 3 (Orin save): the Crawling Scavenger sat in the legacy
        # registry with hp=0/max_hp=0, making it appear always-dead to
        # HP-check subsystems. The handshake is the natural seam — by the
        # time we get here we know the actor list AND the dial threshold
        # (= per-side pool size), so we can publish a real edge block onto
        # the canonical Npc store (post-Wave-2A, the registry is gone).
        #
        # Per AC2 of the original story ("entry cannot report empty pool
        # unless the NPC is actually dead") we ONLY write when the encounter
        # is combat-category and only for opponent-side actors that have a
        # matching Npc. Non-combat encounters leave ``core.edge`` at its
        # standing value so the validator's dead-NPC check stays correct.
        #
        # Story 126-30 (ADR-143/144 "Bind the Ruleset"): a Fate-bound pack's combat is a
        # Fate CONFLICT — resolved by 4dF + ablative stress against the Other's FateSheet
        # (seeded by ``_seed_fate_opponents`` above), NOT native hp_depletion/edge. So the
        # whole native combat-seeding block is REMOVED from the Fate path: under Fate it
        # would leak ``_seed_combat_hp_depletion_to_npcs`` / ``_publish_combat_edge_to_npcs``
        # and ``_roll_and_persist_initiative`` reaches FateConfig with no DEXTERITY map and
        # crashes. Gated on ``is_fate`` (covers both the conflict AND contest Fate paths —
        # neither uses native combat seeding).
        if cdef.category == "combat" and not is_fate:
            turn_no = snapshot.turn_manager.interaction if hasattr(snapshot, "turn_manager") else 0
            if cdef.win_condition == WinCondition.hp_depletion:
                # Task 9: no dial — seed opponent core.hp + core.armor_class
                # from content opponent_default_stats. Creates a backing Npc
                # for any opponent lacking one so find_creature_core reaches
                # it and the SWN attack/hp_depletion pipeline resolves.
                # 153-20: resolve the bound ruleset (fail-loud, never a silent
                # 'dial' default) so the seater can consult the WN SRD unarmed
                # floor at seat time and stamp the discriminating span fields.
                ruleset_slug = (
                    pack.rules.ruleset
                    if pack and pack.rules
                    else _raise_missing_ruleset("hp_depletion_seating")
                )
                # 162-3: remember the roster length so a refusal can roll back any
                # opponent the seeder appended for an EARLIER actor in this same
                # pass before a LATER unbacked actor raised — a multi-opponent seat
                # (e.g. a pool-promoted Other seated ahead of a no-source Other in a
                # world with no generics) must leave NOTHING behind, not just the
                # single-opponent case.
                _npcs_before_seed = len(snapshot.npcs)
                try:
                    _seed_combat_hp_depletion_to_npcs(
                        snapshot=snapshot,
                        actors=actors,
                        cdef=cdef,
                        turn=turn_no,
                        source="encounter_handshake",
                        acting_character_name=player_name,
                        ruleset=get_ruleset_module(ruleset_slug),
                        pack=pack,
                        allow_synthetic_opponent=allow_synthetic_opponent,
                    )
                except ValueError:
                    # 162-3: the fabrication refusal must not half-seat — roll back
                    # any opponent Npc appended during THIS failed pass and restore
                    # the pre-trigger encounter slot (None, or the resolved husk this
                    # trigger was replacing) before propagating the raise.
                    del snapshot.npcs[_npcs_before_seed:]
                    snapshot.encounter = current
                    raise
                # ADR-153: a sealed-letter dogfight is a SIMULTANEOUS-COMMIT duel
                # resolved by the geometry-based shot path (resolve_dogfight_shots),
                # not the WN beat-loop round — it has no 1d8+DEX turn order and never
                # consumes a persisted initiative. It still needs the opponent core
                # seeded above (hp_depletion resolution), but WN initiative would
                # require a PC ability-score lookup the dogfight does not use, so skip
                # it. (Before 158-31 the dogfight was win_condition=dial_threshold and
                # never reached this hp_depletion block at all.)
                if cdef.resolution_mode != ResolutionMode.sealed_letter_lookup:
                    _roll_and_persist_initiative(
                        snapshot=snapshot,
                        enc=enc,
                        actors=actors,
                        cdef=cdef,
                        pack=pack,
                    )
            else:
                _publish_combat_edge_to_npcs(
                    snapshot=snapshot,
                    actors=actors,
                    opponent_metric=enc.opponent_metric,
                    turn=turn_no,
                    source="encounter_handshake",
                    acting_character_name=player_name,
                )
        return enc


def resolve_encounter_from_trope(
    *,
    snapshot: GameSnapshot,
    trope_id: str,
) -> StructuredEncounter | None:
    """Resolve the active encounter because a trope completed.

    Port of dispatch/tropes.rs:179-181. Returns the resolved encounter
    (for OTEL / payload emission) or ``None`` if nothing to resolve.

    IOU (story 3.4): this helper has no Python caller as of this commit. The
    trope engine has not yet been ported to Python (Phase 3 scope). When the
    trope tick/resolve path lands, hook this function at the completion site
    — match Rust's dispatch/tropes.rs:179-181 pattern. The helper + unit
    tests are here so the future port can just call it.
    """
    enc = snapshot.encounter
    if enc is None or enc.resolved:
        return None
    with encounter_resolved_span(
        encounter_type=enc.encounter_type,
        outcome=f"resolved by trope completion: {trope_id}",
        source="trope",
    ):
        enc.resolve_from_trope(trope_id)
    _watcher_publish(
        "state_transition",
        {
            "field": "encounter",
            "op": "resolved",
            "encounter_type": enc.encounter_type,
            "outcome": enc.outcome or f"resolved by trope completion: {trope_id}",
            "source": "trope",
            "final_player_metric": enc.player_metric.current,
            "final_opponent_metric": enc.opponent_metric.current,
        },
        component="encounter",
    )
    return enc


def _is_combat_category(pack: GenrePack, encounter_type: str) -> bool:
    """Return True when the ConfrontationDef for ``encounter_type`` declares
    category=='combat'. Port of state_mutations.rs:39 category check."""
    defs = pack.rules.confrontations if pack.rules else []
    for d in defs:
        if d.confrontation_type == encounter_type:
            return d.category == "combat"
    return False


def award_turn_xp(
    snapshot: GameSnapshot,
    *,
    in_combat: bool,
    ruleset: RulesetModule | None = None,
) -> None:
    """Award the per-turn XP tick to every seated PC (party-wide).

    25 XP when ``in_combat`` is True, 10 otherwise. No-op when the
    snapshot has no characters.

    ``ruleset`` gates the native ADR-021 tick (sq-playtest 2026-06-13). When a
    ruleset module is supplied and ``ruleset.awards_native_turn_xp`` is False —
    the Without Number family (SWN/WWN/CWN/AWN), which uses small-integer
    GM-awarded expedition XP, not an OSR-scale per-turn counter — this is a
    loud no-op: no ``core.xp`` mutation, and a ``xp`` suppression span fires so
    the GM panel proves the native tick was gated rather than silently dropped
    (No Silent Fallbacks). ``None`` (the default for legacy/test callers that
    pass no module) preserves the native tick.

    SideQuest MP is sealed-rounds (ADR-036): every seated PC submits an
    action each round and they resolve together — there is no single
    "acting player", so the per-turn tick is **party-wide**. The original
    Rust port (``state_mutations.rs:39``) awarded only ``characters[0]``
    ("party lead"); in MP that silently starved every non-host seat
    (playtest 2026-05-17 coyote_star-mp: Ritali 1180 XP / Catalina 0
    across 117 rounds). ADR-037 makes the character sheet per-player, so
    each seat accumulates its own XP.

    Seat resolution mirrors the established ``player_seats`` idiom
    (``GameSnapshot.party_location`` / ``character_locations``): award to
    each PC named in the seat manifest. With no manifest (single-player
    or a pre-chargen snapshot) the whole ``characters`` list is the
    party. A manifest that matches no character is a name-skew defect —
    award the full party and surface it loudly rather than silently
    starve XP (No Silent Fallbacks).
    """
    if not snapshot.characters:
        return
    if ruleset is not None and not ruleset.awards_native_turn_xp:
        # WN-family binding: the native per-turn XP tick does not apply. Emit a
        # loud suppression event (lie-detector) so the GM panel sees the gate
        # fired — WN advancement is GM-awarded expedition XP, surfaced through
        # its own path, not this native accumulator.
        _watcher_publish(
            "state_transition",
            {
                "field": "xp",
                "op": "award_turn_xp_suppressed",
                "ruleset": getattr(ruleset, "slug", ""),
                "in_combat": in_combat,
            },
            component="progression",
        )
        return
    delta = 25 if in_combat else 10
    seated = {name for name in snapshot.player_seats.values() if name}
    if seated:
        targets = [c for c in snapshot.characters if c.core.name in seated]
    else:
        targets = list(snapshot.characters)
    seat_mismatch = bool(seated) and not targets
    if seat_mismatch:
        targets = list(snapshot.characters)
    for c in targets:
        c.core.xp = c.core.xp + delta
    _watcher_publish(
        "state_transition",
        {
            "field": "xp",
            "op": "award_turn_xp",
            "delta": delta,
            "in_combat": in_combat,
            "recipients": [c.core.name for c in targets],
            "seated": sorted(seated),
            "seat_mismatch": seat_mismatch,
        },
        component="progression",
    )


# ADR-021 track 1: accumulated ``core.xp`` (the live per-turn accumulator from
# ``award_turn_xp``) is the progression measure — "wire up what exists" rather
# than minting a parallel milestone counter. One milestone is worth this many
# XP; ``milestones_per_level`` (authored 2–3 by the packs) then governs the
# level ladder. award_turn_xp grants 10 (calm) / 25 (combat) per turn, so a
# milestone is ~4–10 turns of play.
_XP_PER_MILESTONE = 100


def apply_level_ups(snapshot: GameSnapshot, progression: ProgressionConfig) -> list[LevelUp]:
    """Drive milestone → level-up for every PC and emit OTEL on each crossing.

    The missing consumer for ADR-021 track 1: ``award_turn_xp`` already
    accumulates ``core.xp`` each turn and tags its emit ``component=progression``,
    but nothing ever advanced the character from it. This runs immediately after
    ``award_turn_xp`` in the turn pipeline, converts accumulated XP into
    milestones, resolves the level via :func:`resolve_level`, and — on a real
    crossing — bumps ``core.level``, publishes a ``progression.level_up``
    ``state_transition`` watcher event (the GM-panel lie-detector, mirroring the
    ``award_turn_xp`` progression emit), and records a player-facing
    :class:`AdvancementDelta` on the character so PartyMember can show *what
    changed and why* (mechanics-first).

    Returns the list of crossings this turn (empty when nobody leveled). A pack
    that doesn't author progression resolves every character to level 1, so the
    loop is a clean no-op — No Silent Fallbacks, no phantom advancement.
    """
    crossings: list[LevelUp] = []
    for character in snapshot.characters:
        # Clear last turn's notification first: the delta is per-turn, so a
        # character that doesn't cross this turn surfaces no advancement.
        character.last_advancement = None

        milestones_completed = max(0, character.core.xp) // _XP_PER_MILESTONE
        new_level = resolve_level(milestones_completed, progression)
        before = character.core.level
        if new_level <= before:
            continue

        character.core.level = new_level
        delta = LevelUp(
            character_name=character.core.name,
            before=before,
            after=new_level,
            driver="milestone",
        )
        character.last_advancement = delta
        crossings.append(delta)
        _watcher_publish(
            "state_transition",
            {
                "field": "progression.level_up",
                "character_name": character.core.name,
                "before": before,
                "after": new_level,
                "driver": "milestone",
            },
            component="progression",
        )
    return crossings


def apply_affinity_tier_ups(
    snapshot: GameSnapshot, progression: ProgressionConfig
) -> list[AffinityTierUp]:
    """Drive affinity progress → tier promotion for every PC and emit OTEL on
    each crossing (ADR-021 track 2).

    The missing consumer for ADR-021 track 2: ``AffinityState`` (``tier`` /
    ``progress``) and ``Affinity.tier_thresholds`` exist as live data, but
    nothing ever advanced a character's affinity tier from accumulated progress.
    This runs in the turn pipeline alongside :func:`apply_level_ups`, resolves
    each affinity's tier via :func:`resolve_affinity_tier`, and — on a real
    crossing — bumps ``AffinityState.tier``, publishes a
    ``progression.affinity_tier_up`` ``state_transition`` watcher event (the
    GM-panel lie-detector, mirroring the track-1 ``progression.level_up`` emit),
    and records a player-facing :class:`AffinityTierUp` on the character so
    PartyMember can show *which affinity advanced and why* (mechanics-first).

    Each character's affinities are matched to the pack's authored ladders by
    ``AffinityState.affinity_id == Affinity.name``. An affinity with no matching
    authored ladder, or whose ladder declares no thresholds, is skipped — No
    Silent Fallbacks, no phantom promotion against another affinity's ladder.

    Returns the list of crossings this turn (empty when nobody advanced).
    """
    thresholds_by_name = {
        affinity.name: affinity.tier_thresholds for affinity in progression.affinities
    }
    crossings: list[AffinityTierUp] = []
    for character in snapshot.characters:
        # Clear last turn's notifications first: the deltas are per-turn, so a
        # character that doesn't advance this turn surfaces none.
        character.last_affinity_tier_ups = []

        for state in character.affinities:
            thresholds = thresholds_by_name.get(state.affinity_id)
            # No authored ladder for this affinity → nothing to climb.
            if not thresholds:
                continue

            new_tier = resolve_affinity_tier(state.progress, thresholds)
            before = state.tier
            if new_tier <= before:
                continue

            state.tier = new_tier
            delta = AffinityTierUp(
                character_name=character.core.name,
                affinity_id=state.affinity_id,
                before=before,
                after=new_tier,
                driver="affinity",
            )
            character.last_affinity_tier_ups.append(delta)
            crossings.append(delta)
            _watcher_publish(
                "state_transition",
                {
                    "field": "progression.affinity_tier_up",
                    "character_name": character.core.name,
                    "affinity_id": state.affinity_id,
                    "before": before,
                    "after": new_tier,
                    "driver": "affinity",
                },
                component="progression",
            )
    return crossings


def apply_resource_patches(
    snapshot: GameSnapshot,
    *,
    affinity_progress: list[tuple[str, int]],
    lore_store: LoreStore,
    turn: int,
) -> list[ResourceThreshold]:
    """Apply each (name, delta) to the named pool; mint threshold lore on crossings.

    Returns the flat list of all ResourceThreshold objects crossed across all
    patches (for OTEL / caller logging). The lore fragments themselves have
    already been added to ``lore_store`` — callers don't need to re-mint.

    Raises ``UnknownResource`` on unknown pool name (CLAUDE.md: no silent
    fallback in the helper). The session-handler caller wraps this call in
    a try/except to keep the narration turn resilient to LLM typos — strict
    helper, lenient caller.
    """
    from sidequest.game.resource_pool import ResourcePatchOp
    from sidequest.game.thresholds import mint_threshold_lore

    all_crossed: list[ResourceThreshold] = []
    for name, delta in affinity_progress:
        op = ResourcePatchOp.Add if delta >= 0 else ResourcePatchOp.Subtract
        value = float(abs(delta))
        result = snapshot.apply_resource_patch_by_name(name, op, value)
        mint_threshold_lore(
            result.crossed_thresholds,
            lore_store,
            turn,
        )
        all_crossed.extend(result.crossed_thresholds)
    return all_crossed
