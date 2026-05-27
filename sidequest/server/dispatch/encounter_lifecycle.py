"""Encounter lifecycle — instantiation, resolution.

Port of sidequest-api/crates/sidequest-server/src/dispatch/
{state_mutations,tropes,response}.rs combat-sensitive paths (Story 3.4).
"""

from __future__ import annotations

from typing import cast

from sidequest.game.encounter import (
    ActorSide,
    EncounterActor,
    EncounterPhase,
    StructuredEncounter,
)
from sidequest.game.lore_store import LoreStore
from sidequest.game.resource_pool import ResourceThreshold
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import ResolutionMode, WinCondition
from sidequest.server.dispatch.confrontation import find_confrontation_def
from sidequest.server.dispatch.sealed_letter import ROLE_BLUE, ROLE_RED
from sidequest.telemetry.spans import (
    encounter_confrontation_initiated_span,
    encounter_no_opponent_available_span,
    encounter_resolved_span,
    encounter_sealed_letter_arity_rejected_span,
    npc_edge_published_span,
    participant_joined_span,
)
from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

_VALID_SIDES = ("player", "opponent", "neutral")


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


def _seed_combat_hp_depletion_to_npcs(
    *,
    snapshot: GameSnapshot,
    actors: list[EncounterActor],
    cdef,
    turn: int,
    source: str,
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
    """
    from sidequest.game.creature_core import CreatureCore, Inventory, hp_pool_from_hp
    from sidequest.game.session import Npc

    hp = cdef.opponent_hp
    ac = cdef.opponent_armor_class

    by_name = {npc.core.name: npc for npc in snapshot.npcs}
    for actor in actors:
        if actor.side != "opponent":
            continue
        npc = by_name.get(actor.name)
        created = npc is None
        if created:
            # Item 3 wiring: no backing Npc.core for this opponent. Create
            # one seeded with the content stats so find_creature_core can
            # reach it and hp_depletion can resolve. The flavor fields are
            # placeholders (the narrator owns prose); the mechanical surface
            # (hp pool, AC) is the load-bearing part.
            core = CreatureCore(
                name=actor.name,
                description="Combat opponent",
                personality="Adversary",
                inventory=Inventory(),
                hp=hp_pool_from_hp(hp),
                armor_class=ac,
            )
            npc = Npc(core=core)
            snapshot.npcs.append(npc)
        else:
            # Overwrite branch: reset the existing opponent's pool to FULL at
            # combat START. This is a start-of-fight assumption — a re-entry
            # would heal the opponent, but the ADR-116 no-reopen flow (an
            # encounter resolves and is not re-instantiated) prevents that.
            npc.core.hp = hp_pool_from_hp(hp)
            npc.core.armor_class = ac
        # OTEL doctrine: distinguish the CREATE branch (a narrator-improv /
        # router-named opponent materialized fresh — the GM panel must see
        # this as an NPC-materialization event) from the OVERWRITE branch.
        with npc_edge_published_span(
            npc_name=actor.name,
            current=npc.core.hp.current,
            max=npc.core.hp.max,
            source=source,
            turn_number=turn,
            created=created,
            seed_source="opponent_default_stats",
        ):
            pass


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

    cfg = pack.rules.swn
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
                raise ValueError(
                    f"player actor '{actor.name}' not found among snapshot.characters "
                    "— cannot resolve DEX for initiative (no silent fallback)"
                )
            score = ch.stats.get(dex_key)
            if score is None:
                raise ValueError(
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
        with npc_edge_published_span(
            npc_name=actor.name,
            current=hp_current,
            max=hp_max,
            source=source,
            turn_number=turn,
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


def _is_adversarial(category: str) -> bool:
    return category in _ADVERSARIAL_CATEGORIES


def _npc_fallback_at_location(
    snapshot: GameSnapshot,
    *,
    adversarial: bool,
    acting_character_name: str | None = None,
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
    default_side = "opponent" if adversarial else "neutral"
    fallback: list = []
    for npc in snapshot.npcs:
        if npc.last_seen_location != location:
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


def instantiate_encounter_from_trigger(
    *,
    snapshot: GameSnapshot,
    pack: GenrePack,
    encounter_type: str,
    player_name: str,
    npcs_present: list,
    genre_slug: str | None,
    additional_player_names: list[str] | None = None,
) -> StructuredEncounter | None:
    """Create a StructuredEncounter when the narrator emits ``confrontation=T``.

    Writes the new encounter to ``snapshot.encounter`` and returns it.
    Returns ``None`` when an active (unresolved) encounter already exists —
    caller leaves the current encounter alone.

    Raises ``ValueError`` when ``encounter_type`` doesn't match any
    ConfrontationDef in the pack (CLAUDE.md: no silent fallback).

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

    defs = pack.rules.confrontations if pack.rules else []
    cdef = find_confrontation_def(defs, encounter_type)
    if cdef is None:
        raise ValueError(f"unknown encounter_type {encounter_type!r} — not in pack confrontations")

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
    # was never seated). The arity validator below is the gate that makes
    # this safe: exactly one location candidate ⇒ seat it as blue; zero or
    # >1 ⇒ keep the loud ``SealedLetterArityError`` (no silent bystander
    # leak, no phantom opponent).
    #
    # Considered and rejected (Story 59-17 Architect consult): narrowing the
    # sealed-letter candidates by ``Disposition.attitude() == HOSTILE`` to
    # pick the single adversary out of a crowd. A freshly narrator-declared
    # dogfight opponent carries the DEFAULT (neutral) disposition — only
    # bestiary-materialized creatures default hostile — so a disposition
    # gate would reject the very opponent it is meant to seat. Hostility in
    # a dogfight is contextual to the scene, not a stored score; the
    # ``_is_adversarial`` CATEGORY check + arity validation is the correct
    # discriminator. The >1-bystander case is handled conservatively (loud
    # arity refusal); smarter single-adversary selection is a future story.
    #
    # Story 45-52: ``location_available`` discriminates "empty location"
    # from "no location at all" — both produce an empty fallback, but only
    # the former is a legitimate empty-scene shape. The flag rides on the
    # ``encounter.no_opponent_available`` span below.
    location_available = True
    seating_source = "router_named"
    if not npcs_present:
        seating_source = "location_fallback"
        npcs_present, location_available = _npc_fallback_at_location(
            snapshot,
            adversarial=_is_adversarial(cdef.category),
            acting_character_name=player_name,
        )

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
    # / ``pre_combat`` remain exempt for now (staged rollout — see
    # ``_ADVERSARIAL_CATEGORIES``).
    if (
        _is_adversarial(cdef.category)
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
            if cdef.interaction_table is None:
                raise ValueError(
                    f"confrontation {encounter_type!r} declares "
                    f"resolution_mode=sealed_letter_lookup but has no "
                    f"interaction_table — sealed-letter resolution requires a "
                    f"populated table (loaded via the `_from:` pointer)"
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

        # ADR-116: membership entry is observable. Emit a participant.joined
        # span per seated actor carrying side + source so the GM panel can
        # answer "why is this pursuer here?" (router-named vs sourced from the
        # location roster). Point-in-time span; ``: pass`` like the other
        # guard spans above.
        for actor in actors:
            with participant_joined_span(
                encounter_type=encounter_type,
                name=actor.name,
                side=actor.side,
                source="seat" if actor.side == "player" else seating_source,
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

        # Synthesize inert metrics when a combat declares no dial (win_condition: hp_depletion).
        # apply_beat gates its dial-resolution branches on win_condition, so these placeholders
        # never gate resolution; the absurdly high threshold (1e6, never reached) is just
        # belt-and-suspenders that keeps the ~9 live-metric readers safe.
        pm = cdef.player_metric
        om = cdef.opponent_metric
        if pm is None and om is None:
            # hp_depletion: no dial authored — synthesize inert placeholders.
            pm = MetricDef(name="hp", starting=0, threshold=1_000_000)
            om = MetricDef(name="hp", starting=0, threshold=1_000_000)
        elif pm is None or om is None:
            raise ValueError(
                f"confrontation '{encounter_type}' has exactly one of "
                "player_metric/opponent_metric; provide both (dial_threshold) or "
                "neither (hp_depletion) — no silent discard"
            )
        enc = StructuredEncounter(
            encounter_type=encounter_type,
            win_condition=cdef.win_condition.value,
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
        )
        snapshot.encounter = enc
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
        if cdef.category == "combat":
            turn_no = snapshot.turn_manager.interaction if hasattr(snapshot, "turn_manager") else 0
            if cdef.win_condition == WinCondition.hp_depletion:
                # Task 9: no dial — seed opponent core.hp + core.armor_class
                # from content opponent_default_stats. Creates a backing Npc
                # for any opponent lacking one so find_creature_core reaches
                # it and the SWN attack/hp_depletion pipeline resolves.
                _seed_combat_hp_depletion_to_npcs(
                    snapshot=snapshot,
                    actors=actors,
                    cdef=cdef,
                    turn=turn_no,
                    source="encounter_handshake",
                )
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


def award_turn_xp(snapshot: GameSnapshot, *, in_combat: bool) -> None:
    """Award the per-turn XP tick to every seated PC (party-wide).

    25 XP when ``in_combat`` is True, 10 otherwise. No-op when the
    snapshot has no characters.

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
