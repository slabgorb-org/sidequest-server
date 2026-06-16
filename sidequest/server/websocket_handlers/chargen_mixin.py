"""Character-creation worker methods for the WebSocket session handler.

Extracted from ``websocket_session_handler`` as a mixin. These are the chargen
scene/arrange/story phase handlers plus archetype resolution, the archetype
gate, the confirmation finaliser, and the next-frame helper. They move out of
the 6.8k-line handler without changing a single call site:
``WebSocketSessionHandler`` inherits this mixin, so the ``session._chargen_*``
instance calls from ``handlers/character_creation.py`` and the class-access
test calls (``WebSocketSessionHandler._chargen_scene`` etc.) resolve unchanged
via the MRO. ``inspect.getsource`` follows the function object, so the gate
source-inspection test keeps working.

OTEL tracer name (``sidequest.server.session_handler``) is preserved so span
sources do not rename.
"""

from __future__ import annotations

import logging
import random
from typing import TYPE_CHECKING

from opentelemetry import trace

if TYPE_CHECKING:
    pass

from sidequest.game.archetype_apply import apply_archetype_resolved
from sidequest.game.builder import (
    BuilderError,
    CharacterBuilder,
    NoQualifyingClassesError,
    PoolValueNotPresentError,
    StoryInput,
    UnfilledArrangementError,
)
from sidequest.game.character import Character
from sidequest.game.chassis import (
    init_chassis_registry,
    rebind_chassis_bonds_to_character,
)
from sidequest.game.lore_seeding import (
    seed_lore_from_char_creation,
    seed_world_lore,
)
from sidequest.game.quest_seed import seed_quest_spine
from sidequest.game.region_init import RegionInitError, init_region_location
from sidequest.game.room_movement import (
    RoomGraphInitError,
    init_room_graph_location,
    process_session_open,
)
from sidequest.game.ruleset.fate_chargen import FateChargenError
from sidequest.game.session import (
    GameSnapshot,
)
from sidequest.game.vessel_tags import find_vessel_item
from sidequest.game.world_materialization import (
    CampaignMaturity,
    HistoryParseError,
    materialize_from_genre_pack,
    parse_history_chapters,
    preload_authored_npcs,
)
from sidequest.genre.archetype.shim import resolve_archetype
from sidequest.genre.error import GenreValidationError
from sidequest.genre.models.pack import picker_portrait_slugs
from sidequest.genre.models.world import NavigationMode
from sidequest.protocol.dice import (
    DiceResultPayload,
    DieGroupResult,
    DieSpec,
    RollOutcome,
    ThrowParams,
)
from sidequest.protocol.messages import (
    CharacterCreationMessage,
    CharacterCreationPayload,
    DiceResultMessage,
    StockDeltas,
    StockOption,
)
from sidequest.server import views
from sidequest.server.dispatch.chargen_loadout import (
    apply_starting_loadout,
    equip_starting_armor,
)
from sidequest.server.dispatch.chargen_summary import render_confirmation_summary
from sidequest.server.dispatch.premise_bind import bind_political_state
from sidequest.server.dispatch.scenario_bind import bind_scenario
from sidequest.server.magic_init import init_magic_state_for_session
from sidequest.server.mutation_init import init_mutation_state_for_session
from sidequest.server.session_helpers import (
    _error_msg,
    _world_history_value,
)
from sidequest.server.session_state import (
    _SessionData,
    _State,
)
from sidequest.telemetry.spans import (
    SPAN_CHARGEN_ARCHETYPE_GATE_BLOCKED,
    SPAN_CHARGEN_ARCHETYPE_GATE_EVALUATED,
    SPAN_CHARGEN_ARCHETYPE_INFERRED,
    SPAN_CHARGEN_PORTRAIT_SELECT,
)
from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

logger = logging.getLogger(__name__)

# Preserve the original tracer name so OTEL span sources do not rename when
# this class moved out of session_handler.py. Phase-3 plan principle.
tracer = trace.get_tracer("sidequest.server.session_handler")
from sidequest.server.websocket_handlers.map_emit import (  # noqa: E402
    _maybe_emit_location_description,
    _maybe_emit_tactical_grid,
)
from sidequest.server.websocket_handlers.opening_helpers import (  # noqa: E402
    _populate_opening_directive_on_chargen_complete,
    _should_fire_opening_narration,
)


def _bones_dice_messages(
    builder: CharacterBuilder,
    sd: _SessionData,
    player_id: str,
) -> list[DiceResultMessage]:
    """Build visible DiceResult broadcasts from queued Roll the Bones rolls.

    Reuses the existing ADR-074 result payload — no new message types.
    ``request_id`` carries the stat (``chargen.bones.<STAT>``) so overlays
    and tests can attribute faces to scores. Chargen rolls are server-
    rolled with no check semantics: difficulty pins at 1, outcome Unknown,
    and the throw params are neutral (no gesture to replay).
    """
    return [
        DiceResultMessage(
            payload=DiceResultPayload(
                request_id=f"chargen.bones.{stat}",
                rolling_player_id=player_id,
                character_name=sd.player_name or "",
                rolls=[
                    DieGroupResult(
                        spec=DieSpec(sides=6, count=3),
                        faces=list(faces),
                    )
                ],
                modifier=0,
                total=sum(faces),
                difficulty=1,
                outcome=RollOutcome.Unknown,
                seed=0,
                throw_params=ThrowParams(
                    velocity=(0.0, 0.0, 0.0),
                    angular=(0.0, 0.0, 0.0),
                    position=(0.0, 0.0),
                ),
            ),
            player_id=player_id,
        )
        for stat, faces in builder.consume_bones_broadcasts()
    ]


def _stockify_scene_message(
    msg: CharacterCreationMessage,
    builder: CharacterBuilder,
    sd: _SessionData,
) -> None:
    """Upgrade a stock-selection scene frame to ``input_type="stock"`` (103-2).

    When the builder's current scene carries ``stock_id`` choices and the
    active world ships a stock roster, attach per-stock mechanical deltas so
    the client can show the math BEFORE confirmation. Options stay aligned
    1:1 with the scene's choices — the standard ``{phase: "scene", choice:
    "<index+1>"}`` response maps unchanged. Choices WITHOUT a stock_id (the
    Wild path) ride along with empty deltas.

    Misconfigurations fail LOUD at the first wrong moment (review rework
    2026-06-11, No Silent Fallbacks): stock_id choices against a world
    with no stock roster raise naming the world and the offending ids
    (char_creation.yaml and stocks.yaml are separate files the load-time
    validation never cross-checks); a stock_id that doesn't resolve
    against the roster raises KeyError from ``StockRegistry.by_id``; and
    a granting stock with no mutation catalog raises rather than render
    a false empty-mutations preview — a loaded registry guarantees a
    catalog, so that combination is an upstream bug, never a no-op.
    """
    if not builder.is_in_progress():
        return
    scene = builder.current_scene()
    stock_ids = [
        c.mechanical_effects.stock_id
        for c in scene.choices
        if c.mechanical_effects.stock_id is not None
    ]
    if not stock_ids:
        return
    world = sd.genre_pack.worlds.get(sd.world_slug)
    registry = world.stocks if world is not None else None
    if registry is None:
        raise ValueError(
            f"chargen scene {scene.id!r} offers stock choices {sorted(stock_ids)} but "
            f"world {sd.world_slug!r} ships no stock roster (worlds/<slug>/stocks.yaml) — "
            "a stock_id in char_creation.yaml requires a matching stocks.yaml entry"
        )
    catalog = sd.genre_pack.mutations
    options: list[StockOption] = []
    for choice in scene.choices:
        sid = choice.mechanical_effects.stock_id
        if sid is None:
            options.append(
                StockOption(
                    id=choice.label.lower().replace(" ", "_"),
                    label=choice.label,
                    description=choice.description,
                )
            )
            continue
        stock = registry.by_id(sid)
        if catalog is None and stock.granted_mutations:
            raise ValueError(
                f"stock {stock.id!r} grants mutations but the pack has no mutation "
                "catalog to resolve display names against — a loaded stock registry "
                "guarantees a catalog; this is an upstream load bug"
            )
        granted_names = (
            [catalog.positive_by_id(mid).name for mid in stock.granted_mutations]
            if catalog is not None
            else []
        )
        options.append(
            StockOption(
                id=stock.id,
                label=choice.label,
                description=choice.description,
                deltas=StockDeltas(
                    attr_mods=dict(stock.attr_mods),
                    move=stock.move,
                    ac=stock.ac,
                    trauma_target_mod=stock.trauma_target_mod,
                    granted_mutations=granted_names,
                ),
            )
        )
    msg.payload.input_type = "stock"
    msg.payload.stock_options = options


class CharGenMixin:
    """Character-creation phase handlers and archetype resolution."""

    # ---- phase=scene ----------------------------------------------------
    def _chargen_scene(
        self,
        builder: CharacterBuilder,
        payload: CharacterCreationPayload,
        sd: _SessionData,
        player_id: str,
        span: trace.Span,
    ) -> list[object]:
        choice_str = payload.choice if payload.choice is not None else "1"

        # AwaitingFollowup has no choice list — the entire input (numeric or
        # prose) is the answer to the scene's hook_prompt and routes to
        # answer_followup, NOT apply_choice/apply_freeform (both InProgress-only;
        # they raise WrongPhaseError). Playtest 2026-05-26 [road_warrior/
        # the_circuit]: an ambiguous free-text names answer pushed the builder
        # into AwaitingFollowup and the UI re-sent freeform, hard-blocking
        # chargen with WrongPhaseError(expected=InProgress, got=AwaitingFollowup).
        if builder.is_awaiting_followup():
            span.add_event(
                "character_creation.followup",
                {
                    "phase": "followup",
                    "choice_raw": choice_str,
                    "player_id": player_id,
                },
            )
            try:
                builder.answer_followup(choice_str)
            except BuilderError as exc:
                return [_error_msg(f"Invalid followup answer: {exc!r}")]
            return self._next_message(builder, sd, player_id)

        resolved_index: int | None
        try:
            # 1-based numeric index (Rust: saturating_sub(1))
            n = int(choice_str)
            resolved_index = max(0, n - 1)
        except ValueError:
            # Label-match (case-insensitive) against the current scene's
            # choices. Only applies when we're in InProgress; AwaitingFollowup
            # has no choice list and will fall through to freeform below.
            resolved_index = None
            if builder.is_in_progress():
                current = builder.current_scene()
                for i, c in enumerate(current.choices):
                    if c.label.casefold() == choice_str.casefold():
                        resolved_index = i
                        break

        span.add_event(
            "character_creation.scene",
            {
                "phase": "scene",
                "choice_raw": choice_str,
                "resolved_index": str(resolved_index),
                "player_id": player_id,
            },
        )

        if resolved_index is not None:
            try:
                builder.apply_choice(resolved_index)
            except BuilderError as exc:
                return [_error_msg(f"Invalid choice: {exc!r}")]
        else:
            try:
                builder.apply_freeform(choice_str)
            except BuilderError as exc:
                return [_error_msg(f"Invalid freeform input: {exc!r}")]

        return self._next_message(builder, sd, player_id)

    # ---- phase=continue -------------------------------------------------
    def _chargen_continue(
        self,
        builder: CharacterBuilder,
        sd: _SessionData,
        player_id: str,
        span: trace.Span,
    ) -> list[object]:
        logger.info("chargen.continue player_id=%s", player_id)
        span.add_event(
            "character_creation.continue",
            {"phase": "continue", "player_id": player_id},
        )
        try:
            builder.apply_auto_advance()
        except BuilderError as exc:
            return [_error_msg(f"Cannot continue from current scene: {exc!r}")]
        return self._next_message(builder, sd, player_id)

    # ---- phase=bones_reroll (103-3) --------------------------------------
    def _chargen_bones_reroll(
        self,
        builder: CharacterBuilder,
        payload: CharacterCreationPayload,
        sd: _SessionData,
        player_id: str,
        span: trace.Span,
    ) -> list[object]:
        if payload.stat is None:
            return [_error_msg("bones_reroll requires a stat field")]
        span.add_event(
            "character_creation.bones_reroll",
            {"stat": payload.stat, "player_id": player_id},
        )
        try:
            builder.reroll_stat(payload.stat)
        except (BuilderError, ValueError, RuntimeError) as exc:
            return [_error_msg(f"bones_reroll rejected: {exc!r}")]
        return self._next_message(builder, sd, player_id)

    # ---- phase=bones_confirm (103-3) -------------------------------------
    def _chargen_bones_confirm(
        self,
        builder: CharacterBuilder,
        sd: _SessionData,
        player_id: str,
        span: trace.Span,
    ) -> list[object]:
        span.add_event(
            "character_creation.bones_confirm",
            {"player_id": player_id},
        )
        try:
            builder.apply_bones_confirm()
        except BuilderError as exc:
            return [_error_msg(f"bones_confirm rejected: {exc!r}")]
        except RuntimeError as exc:
            return [_error_msg(f"bones_confirm failed: {exc!r}")]
        return self._next_message(builder, sd, player_id)

    # ---- phase=arrange_assign ------------------------------------------
    def _chargen_arrange_assign(
        self,
        builder: CharacterBuilder,
        payload: CharacterCreationPayload,
        sd: _SessionData,
        player_id: str,
        span: trace.Span,
    ) -> list[object]:
        if payload.stat is None or payload.value is None:
            return [_error_msg("arrange_assign requires stat and value fields")]
        span.add_event(
            "character_creation.arrange_assign",
            {
                "stat": payload.stat,
                "value": payload.value,
                "player_id": player_id,
            },
        )
        try:
            builder.assign_stat(stat_name=payload.stat, value=payload.value)
        except PoolValueNotPresentError as exc:
            return [_error_msg(f"arrange_assign rejected: {exc!r}")]
        except BuilderError as exc:
            return [_error_msg(f"arrange_assign failed: {exc!r}")]
        return self._next_message(builder, sd, player_id)

    # ---- phase=arrange_clear -------------------------------------------
    def _chargen_arrange_clear(
        self,
        builder: CharacterBuilder,
        payload: CharacterCreationPayload,
        sd: _SessionData,
        player_id: str,
        span: trace.Span,
    ) -> list[object]:
        if payload.stat is None:
            return [_error_msg("arrange_clear requires stat field")]
        span.add_event(
            "character_creation.arrange_clear",
            {"stat": payload.stat, "player_id": player_id},
        )
        try:
            builder.clear_stat(stat_name=payload.stat)
        except BuilderError as exc:
            return [_error_msg(f"arrange_clear failed: {exc!r}")]
        return self._next_message(builder, sd, player_id)

    # ---- phase=arrange_confirm -----------------------------------------
    def _chargen_arrange_confirm(
        self,
        builder: CharacterBuilder,
        sd: _SessionData,
        player_id: str,
        span: trace.Span,
    ) -> list[object]:
        span.add_event(
            "character_creation.arrange_confirm",
            {"player_id": player_id},
        )
        try:
            builder.apply_arrangement_confirm()
        except UnfilledArrangementError as exc:
            return [_error_msg(f"arrange_confirm rejected: {exc!r}")]
        except NoQualifyingClassesError as exc:
            return [_error_msg(f"arrange_confirm has no qualifying class: {exc!r}")]
        except BuilderError as exc:
            return [_error_msg(f"arrange_confirm failed: {exc!r}")]
        return self._next_message(builder, sd, player_id)

    # ---- phase=arrange_reject ------------------------------------------
    def _chargen_arrange_reject(
        self,
        builder: CharacterBuilder,
        sd: _SessionData,
        player_id: str,
        span: trace.Span,
    ) -> list[object]:
        span.add_event(
            "character_creation.arrange_reject",
            {"player_id": player_id},
        )
        try:
            builder.apply_arrangement_reject()
        except BuilderError as exc:
            return [_error_msg(f"arrange_reject failed: {exc!r}")]
        return self._next_message(builder, sd, player_id)

    # ---- phase=fate_aspects_confirm (ADR-144 F4a3 / story 121-8) -------
    def _chargen_fate_aspects_confirm(
        self,
        builder: CharacterBuilder,
        payload: CharacterCreationPayload,
        sd: _SessionData,
        player_id: str,
        span: trace.Span,
    ) -> list[object]:
        if payload.fate_high_concept is None or payload.fate_trouble is None:
            return [_error_msg("fate_aspects_confirm requires fate_high_concept and fate_trouble")]
        # Mandatory pair must be non-empty (No Silent Fallbacks): re-prompt loud and
        # EARLY at this step rather than letting an empty High Concept / Trouble slip
        # through to build() (where validate_fate_sheet would reject it). Free aspects
        # are optional at chargen (seeded + refined in play — story AC1 / epic 121).
        if not payload.fate_high_concept.strip() or not payload.fate_trouble.strip():
            return [
                _error_msg(
                    "fate_aspects_confirm: High Concept and Trouble are required and "
                    "cannot be empty"
                )
            ]
        free_aspects = payload.fate_free_aspects or []
        span.add_event(
            "character_creation.fate_aspects_confirm",
            {"player_id": player_id, "free_count": len(free_aspects)},
        )
        try:
            builder.apply_fate_aspects(
                high_concept=payload.fate_high_concept,
                trouble=payload.fate_trouble,
                free_aspects=free_aspects,
            )
        except (BuilderError, RuntimeError) as exc:
            logger.warning("chargen.fate_aspects_confirm_failed err=%r", exc)
            return [_error_msg("fate_aspects_confirm: could not record aspects")]
        return self._next_message(builder, sd, player_id)

    # ---- phase=fate_pyramid_confirm -----------------------------------
    def _chargen_fate_pyramid_confirm(
        self,
        builder: CharacterBuilder,
        payload: CharacterCreationPayload,
        sd: _SessionData,
        player_id: str,
        span: trace.Span,
    ) -> list[object]:
        from sidequest.game.ruleset.fate_chargen import pyramid_violations
        from sidequest.genre.models.rules import FateConfig

        if payload.fate_allocation is None:
            return [_error_msg("fate_pyramid_confirm requires fate_allocation")]
        cfg = builder.rules.ruleset_config()
        if not isinstance(cfg, FateConfig):
            return [_error_msg("fate_pyramid_confirm: pack carries no FateConfig")]
        violations = pyramid_violations(payload.fate_allocation, cfg)
        span.add_event(
            "character_creation.fate_pyramid_confirm",
            {"player_id": player_id, "legal": not violations},
        )
        if violations:
            # No Silent Fallbacks: never silently accept an illegal pyramid. Echo
            # the submission and re-prompt the same step with its violations.
            builder.preview_fate_pyramid(payload.fate_allocation)
            return self._next_message(builder, sd, player_id)
        try:
            builder.apply_fate_pyramid(payload.fate_allocation)
        except (BuilderError, RuntimeError) as exc:
            logger.warning("chargen.fate_pyramid_confirm_failed err=%r", exc)
            return [_error_msg("fate_pyramid_confirm: could not record allocation")]
        return self._next_message(builder, sd, player_id)

    # ---- phase=fate_stunts_confirm ------------------------------------
    def _chargen_fate_stunts_confirm(
        self,
        builder: CharacterBuilder,
        payload: CharacterCreationPayload,
        sd: _SessionData,
        player_id: str,
        span: trace.Span,
    ) -> list[object]:
        from sidequest.game.ruleset.fate_chargen import stunt_catalog_violations
        from sidequest.genre.models.rules import FateConfig

        selected = payload.fate_selected_stunts or []
        cfg = builder.rules.ruleset_config()
        if not isinstance(cfg, FateConfig):
            return [_error_msg("fate_stunts_confirm: pack carries no FateConfig")]
        # Re-validate at the boundary (server is the authority): an out-of-catalog
        # stunt is rejected here, not deferred to build().
        violations = stunt_catalog_violations(selected, cfg)
        span.add_event(
            "character_creation.fate_stunts_confirm",
            {"player_id": player_id, "count": len(selected), "legal": not violations},
        )
        if violations:
            return [_error_msg(f"fate_stunts_confirm: {'; '.join(violations)}")]
        try:
            builder.apply_fate_stunts(selected)
        except (FateChargenError, BuilderError, RuntimeError) as exc:
            logger.warning("chargen.fate_stunts_confirm_failed err=%r", exc)
            return [_error_msg("fate_stunts_confirm: could not record stunts")]
        return self._next_message(builder, sd, player_id)

    # ---- phase=story_autogen -------------------------------------------
    def _chargen_story_autogen(
        self,
        builder: CharacterBuilder,
        payload: CharacterCreationPayload,
        sd: _SessionData,
        player_id: str,
        span: trace.Span,
    ) -> list[object]:
        seed = payload.seed if payload.seed is not None else random.randint(0, 2**31 - 1)
        span.add_event(
            "character_creation.story_autogen",
            {"seed": seed, "player_id": player_id},
        )
        try:
            autogen_result = builder.autogen_backstory(seed=seed)
        except BuilderError as exc:
            return [_error_msg(f"story_autogen failed: {exc!r}")]
        # Render the next scene message and inject autogen_result into the
        # payload. Builder state remains in the_story (autogen is rerollable;
        # we don't commit it to the builder).
        msgs = self._next_message(builder, sd, player_id)
        if msgs and isinstance(msgs[0], CharacterCreationMessage):
            msgs[0].payload.autogen_result = autogen_result
        return msgs

    # ---- phase=story_confirm -------------------------------------------
    def _chargen_story_confirm(
        self,
        builder: CharacterBuilder,
        payload: CharacterCreationPayload,
        sd: _SessionData,
        player_id: str,
        span: trace.Span,
    ) -> list[object]:
        span.add_event(
            "character_creation.story_confirm",
            {
                "pronouns_present": bool(payload.pronouns),
                "background_present": bool(payload.background),
                "description_present": bool(payload.description),
                "player_id": player_id,
            },
        )
        story = StoryInput(
            pronouns=payload.pronouns or "",
            background=payload.background or "",
            description=payload.description or "",
        )
        try:
            builder.apply_response(story)
        except UnfilledArrangementError as exc:
            return [_error_msg(f"story_confirm rejected: {exc!r}")]
        except BuilderError as exc:
            return [_error_msg(f"story_confirm failed: {exc!r}")]
        return self._next_message(builder, sd, player_id)

    # ---- the portrait picker step (Epic 66) ------------------------------
    def _render_portrait_scene(
        self,
        builder: CharacterBuilder,
        sd: _SessionData,
        player_id: str,
    ) -> CharacterCreationMessage:
        """Render the one-time ``pick_portrait`` frame (Epic 66).

        Emitted by ``_next_message`` on first arrival at the Confirmation
        boundary. ``portraits_available`` tells the client whether this
        world ships any ``type=player_picker`` portrait-manifest entries
        (the client fetches the actual roster via
        ``GET /api/chargen/portraits/{genre}/{world}``); the soft-suggest
        hints surface the in-progress build's archetype/culture so the UI
        can pre-sort the roster. Both hints are best-effort — ``None``
        when the builder accumulated nothing usable.
        """
        # World-tier lookup matches the module idiom (see
        # ``world_for_authored`` in ``_chargen_confirmation``) and the Epic
        # 66 REST endpoint (``GET /api/chargen/portraits``): a world slug
        # with no ``worlds/<slug>/`` override directory is a legitimate
        # genre-tier-only world (e.g. caverns_and_claudes/grimvault), NOT a
        # config error — it simply ships no picker portraits.
        world_obj = sd.genre_pack.worlds.get(sd.world_slug)
        has_pickers = world_obj is not None and any(
            entry.character_type == "player_picker" for entry in world_obj.portrait_manifest
        )
        acc = builder.accumulated()
        return CharacterCreationMessage(
            payload=CharacterCreationPayload(
                phase="scene",
                prompt="Choose a portrait for your character — or skip to continue.",
                input_type="pick_portrait",
                portraits_available=has_pickers,
                suggest_archetype=acc.jungian_hint or acc.rpg_role_hint or acc.class_hint,
                suggest_culture=acc.race_hint,
            ),
            player_id=player_id,
        )

    # ---- phase=portrait_confirm ------------------------------------------
    def _chargen_portrait_confirm(
        self,
        builder: CharacterBuilder,
        payload: CharacterCreationPayload,
        sd: _SessionData,
        player_id: str,
        span: trace.Span,
    ) -> list[object]:
        """Record the player's portrait choice and advance to the summary.

        ``selected_portrait_ref`` is the picker entry's slug; ``None`` (or
        empty) means the player skipped. The choice is parked on the
        session until ``_chargen_confirmation`` copies it onto the built
        Character. Fires ``chargen.portrait_select`` on every pass —
        skip included — so the GM panel sees the step engage (OTEL
        Observability Principle).

        An unknown ref is WARN-AND-ACCEPT, never rejected: the field is
        cosmetic, and a stale client roster (e.g. content updated mid-
        session) shouldn't dead-end chargen. The warning + ``ref_known``
        span attribute keep the mismatch visible to the GM panel.
        """
        # Out-of-order guard: portrait_confirm is only meaningful after the
        # pick_portrait frame was interposed at the confirmation boundary.
        # Reject wrong-state sends the same way siblings surface
        # BuilderError-style rejections.
        if not sd.portrait_step_shown:
            return [
                _error_msg(
                    "portrait_confirm rejected: portrait step has not been "
                    "shown (builder has not reached the confirmation boundary)"
                )
            ]

        selected = payload.selected_portrait_ref or None

        # Validate against the world's player_picker slugs — same derivation
        # the REST roster endpoint serves (picker_portrait_slug[s], Epic 66).
        # The set is computed on the skip path too: its size is the
        # ``pool_size`` span attribute (spec §8) either way.
        world_obj = sd.genre_pack.worlds.get(sd.world_slug)
        known = picker_portrait_slugs(world_obj) if world_obj is not None else set()
        ref_known = True
        if selected is not None:
            ref_known = selected in known
            if not ref_known:
                logger.warning(
                    "chargen.portrait_select_unknown_ref selected=%s genre=%s "
                    "world=%s known_count=%d player_id=%s "
                    "(warn-and-accept: portrait_ref is cosmetic)",
                    selected,
                    sd.genre_slug,
                    sd.world_slug,
                    len(known),
                    player_id,
                )

        sd.selected_portrait_ref = selected

        # ``was_suggested`` (spec §8): a portrait was picked AND a soft-suggest
        # archetype hint had been sent on the pick_portrait scene. Best-effort
        # semantics: the hint isn't parked on the session, so it is re-derived
        # here with the exact expression ``_render_portrait_scene`` used
        # (``jungian_hint or rpg_role_hint or class_hint``) — builder state
        # cannot change between the scene frame and this confirm, so the
        # re-derivation equals what the client was actually sent. It does NOT
        # claim the player *followed* the suggestion, only that one existed.
        acc = builder.accumulated()
        suggest_sent = (acc.jungian_hint or acc.rpg_role_hint or acc.class_hint) is not None
        was_suggested = selected is not None and suggest_sent

        span.add_event(
            "character_creation.portrait_confirm",
            {
                "event": "portrait_confirm",
                "selected_portrait_ref": selected or "",
                "skipped": selected is None,
                "ref_known": ref_known,
                "pool_size": len(known),
                "was_suggested": was_suggested,
                "player_id": player_id,
            },
        )
        with tracer.start_as_current_span(
            SPAN_CHARGEN_PORTRAIT_SELECT,
            attributes={
                "genre": sd.genre_slug,
                "world": sd.world_slug,
                "selected_portrait_ref": selected or "",
                "skipped": selected is None,
                "ref_known": ref_known,
                "pool_size": len(known),
                "was_suggested": was_suggested,
                "player_id": player_id,
            },
        ):
            pass
        return self._next_message(builder, sd, player_id)

    # ---- archetype resolution helper (Story 2.3 Slice A) ----------------
    def _resolve_character_archetype(
        self,
        character: Character,
        sd: _SessionData,
        player_id: str,
        span: trace.Span,
    ) -> None:
        """Resolve a raw ``jungian/rpg_role`` pair through the archetype shim.

        The builder encodes accumulated archetype hints as
        ``f"{jungian}/{rpg_role}"`` on ``character.resolved_archetype`` (see
        ``builder.py:1585-1590``). This helper detects that raw form, runs
        the four-tier resolve (base → constraints → world funnels), and
        replaces the raw pair with the resolved display name via
        ``apply_archetype_resolved`` — keeping ``archetype_provenance`` in
        lockstep.

        Resolution failures emit a ``character_creation.archetype_resolution_failed``
        span event and leave the raw pair in place. The downstream
        archetype-resolution gate in ``_chargen_confirmation`` (Story
        45-6, ``_gate_archetype_resolution``) detects the partial state
        and rejects the commit with a typed ERROR frame
        (``code="chargen_archetype_unresolved"``); this helper is
        no-op-on-failure intentionally so the gate can decide. Missing
        axis data on the pack (no ``base_archetypes`` or
        ``archetype_constraints``) also silently no-ops here — the
        gate then routes to ``OK_NO_AXES`` (pass) if the builder
        produced no pair, or ``raw_pair_unresolved`` (block) if the
        builder produced a pair the resolver couldn't run.

        Rust parity: ``connect.rs:1644-1737``.
        """
        raw = character.resolved_archetype
        if raw is None or "/" not in raw:
            return

        jungian, rpg_role = raw.split("/", 1)
        pack = sd.genre_pack
        if pack.base_archetypes is None or pack.archetype_constraints is None:
            return

        world = pack.worlds.get(sd.world_slug)
        funnels = world.archetype_funnels if world is not None else None

        try:
            resolution = resolve_archetype(
                jungian=jungian,
                rpg_role=rpg_role,
                base=pack.base_archetypes,
                constraints=pack.archetype_constraints,
                funnels=funnels,
                genre=sd.genre_slug,
                world=sd.world_slug,
            )
        except GenreValidationError as exc:
            span.add_event(
                "character_creation.archetype_resolution_failed",
                {
                    "event": "archetype.resolution_failed",
                    "error": str(exc),
                    "jungian": jungian,
                    "rpg_role": rpg_role,
                    "player_id": player_id,
                },
            )
            logger.warning(
                "chargen.archetype_resolution_failed jungian=%s rpg_role=%s error=%s",
                jungian,
                rpg_role,
                exc,
            )
            return

        apply_archetype_resolved(character, resolution)
        span.add_event(
            "character_creation.archetype_resolved",
            {
                "event": "archetype.resolved",
                "jungian": jungian,
                "rpg_role": rpg_role,
                "resolved_name": resolution.resolved.name,
                "source": resolution.source.value,
                "source_tier": resolution.provenance.source_tier.value,
                "weight": resolution.weight.value,
                "faction": resolution.resolved.faction or "none",
                "genre": sd.genre_slug,
                "world": sd.world_slug,
                "player_id": player_id,
            },
        )

    # ---- archetype-resolution gate (Story 45-6) -------------------------
    def _gate_archetype_resolution(
        self,
        character: Character,
        sd: _SessionData,
        player_id: str,
        span: trace.Span,
    ) -> tuple[bool, str | None]:
        """Inspect the post-resolve character state and decide whether
        chargen is allowed to ship.

        The gate distinguishes three states:

        - **OK_RESOLVED** — ``apply_archetype_resolved`` ran and stamped
          ``archetype_provenance``. The discriminator keys on
          ``character.archetype_provenance is not None``, NOT on the
          shape of ``resolved_archetype`` — that survives display
          names that legitimately contain ``"/"``. Pass.
        - **OK_NO_AXES** — ``resolved_archetype is None`` AND the pack
          opted out of the archetype system
          (``base_archetypes is None and archetype_constraints is None``).
          The builder didn't form a pair and the resolver had nothing
          to resolve. Pass.
        - **BLOCKED_PARTIAL** — anything else. Three failure modes
          (Story 45-6 / playtest 3 ``pumblestone`` corpus):

          1. ``raw_pair_unresolved`` — a literal ``"jungian/rpg_role"``
             string is still on the character AND the pack lacks axes.
             ``_resolve_character_archetype`` short-circuited at the
             pack-axes check (line 579) before calling
             ``resolve_archetype``; the raw pair stayed.
          2. ``missing_axes_with_pack_axes`` — pack declares axes but
             the chargen scenes accumulated at most one hint, so the
             builder set ``resolved_archetype = None``. This is the
             ``pumblestone`` failure: chargen scenes malformed.
          3. ``resolver_raised`` — pack has axes AND a raw pair is
             still on the character. Pure shape inference: the
             pack-lacks-axes short-circuit at line 579 would have
             returned before calling ``resolve_archetype``, so a raw
             pair with pack-axes-set can only mean the resolver was
             called and raised — the catch-block at line 595 caught
             ``GenreValidationError`` and returned, leaving the raw
             pair.

        The evaluator span fires on every chargen-confirm; the blocked
        span fires only on BLOCKED_PARTIAL. Both go to the GM panel via
        ``SPAN_ROUTES`` (Sebastien's lie-detector — CLAUDE.md OTEL
        Observability Principle). On the blocked branch a
        ``logger.warning()`` entry is also emitted to the structured
        server log so ops debugging is independent of the OTEL pipeline
        (python.md rule 4).

        Returns ``(is_blocked, block_reason)`` — ``block_reason`` is
        one of ``"raw_pair_unresolved"``,
        ``"missing_axes_with_pack_axes"``, ``"resolver_raised"``, or
        ``None`` on a pass.
        """
        pack = sd.genre_pack
        pack_has_axes = pack.base_archetypes is not None and pack.archetype_constraints is not None
        ra = character.resolved_archetype
        provenance_set = character.archetype_provenance is not None
        # ``had_both_hints`` is the only granularity the gate has access
        # to: the builder writes ``resolved_archetype = f"{j}/{r}"`` iff
        # both hints were set, else ``None``. After the resolver runs,
        # ``apply_archetype_resolved`` may overwrite the raw pair with a
        # display name — but ``apply_archetype_resolved`` only fires
        # when both hints fed the resolver in the first place. So:
        # ``ra is not None`` ⇒ both hints; ``ra is None`` ⇒ at most one
        # hint. The gate cannot tell *which* hint was missing without
        # threading the builder accumulator through, which would be
        # invasive — the OTEL signal is intentionally per-pair, not
        # per-axis.
        had_both_hints = ra is not None

        # Decide. The discriminator keys on ``provenance_set`` (the
        # OK_RESOLVED signal) rather than ``"/"`` in ``ra`` — the latter
        # would misclassify display names that legitimately contain
        # ``"/"`` (no validator on ``ArchetypeResolved.name`` forbids
        # it). ``apply_archetype_resolved`` writes both
        # ``resolved_archetype`` and ``archetype_provenance`` together,
        # so ``provenance_set`` is the durable lockstep signal.
        gate_state: str
        block_reason: str | None
        if provenance_set:
            # OK_RESOLVED — resolver succeeded and stamped provenance.
            gate_state = "ok_resolved"
            block_reason = None
        elif ra is None and not pack_has_axes:
            # OK_NO_AXES — pack opted out of the archetype system.
            gate_state = "ok_no_axes"
            block_reason = None
        elif ra is None:
            # ra is None AND pack_has_axes — pumblestone case (chargen
            # scenes malformed; the builder didn't form a pair).
            block_reason = "missing_axes_with_pack_axes"
            gate_state = "blocked_partial"
        else:
            # Raw pair on the character (no provenance ⇒ resolver did
            # not run successfully). With pack-axes-set the resolver
            # was called and raised; without pack-axes it short-
            # circuited at line 579.
            block_reason = "resolver_raised" if pack_has_axes else "raw_pair_unresolved"
            gate_state = "blocked_partial"

        # Evaluator span — fires on every chargen-confirm. ``state``
        # carries the decision so the GM panel sees the choice on every
        # path, including the success branches (negative confirmation
        # that the gate ran).
        with tracer.start_as_current_span(
            SPAN_CHARGEN_ARCHETYPE_GATE_EVALUATED,
            attributes={
                "state": gate_state,
                "resolved_archetype": ra if ra is not None else "",
                "pack_has_axes": pack_has_axes,
                "had_both_hints": had_both_hints,
                "provenance_set": provenance_set,
                "genre": sd.genre_slug,
                "world": sd.world_slug,
                "player_id": player_id,
            },
        ):
            pass

        if block_reason is None:
            return False, None

        # python.md rule 4: error paths MUST log to the structured
        # server log surface. The OTEL span is independent (it goes to
        # the watcher dashboard / GM panel); this WARNING entry lands
        # in journald / file logs so ops debugging works without the
        # OTEL pipeline.
        logger.warning(
            "chargen.archetype_gate_blocked player_id=%s block_reason=%s "
            "genre=%s world=%s pack_has_axes=%s resolved_archetype=%s",
            player_id,
            block_reason,
            sd.genre_slug,
            sd.world_slug,
            pack_has_axes,
            ra if ra is not None else "<none>",
        )

        # Blocked span — fires only on BLOCKED_PARTIAL. This is the
        # explicit lie-detector entry that says "a chargen would have
        # shipped broken; the gate caught it." The legacy
        # ``character_creation.archetype_resolution_failed`` event is
        # the inner-resolver event; the blocked span is the outer-gate
        # event.
        with tracer.start_as_current_span(
            SPAN_CHARGEN_ARCHETYPE_GATE_BLOCKED,
            attributes={
                "state": "blocked_partial",
                "block_reason": block_reason,
                "resolved_archetype": ra if ra is not None else "",
                "pack_has_axes": pack_has_axes,
                "had_both_hints": had_both_hints,
                "provenance_set": provenance_set,
                "genre": sd.genre_slug,
                "world": sd.world_slug,
                "player_id": player_id,
            },
        ) as gate_span:
            # Also record on the parent span for correlation in the
            # existing ``character_creation.*`` event stream.
            span.add_event(
                "character_creation.archetype_gate_blocked",
                {
                    "event": "archetype_gate_blocked",
                    "block_reason": block_reason,
                    "resolved_archetype": ra if ra is not None else "",
                    "pack_has_axes": pack_has_axes,
                    "player_id": player_id,
                },
            )
            # Mirror onto the gate span as well so ReadableSpan
            # consumers (test exporter) see the same payload either
            # way.
            gate_span.add_event(
                "character_creation.archetype_gate_blocked",
                {"block_reason": block_reason},
            )

        return True, block_reason

    # ---- archetype inference (Story 93-1) --------------------------------
    async def _maybe_infer_archetype_from_freeform(
        self,
        builder: CharacterBuilder,
        character: Character,
        sd: _SessionData,
        player_id: str,
        span: trace.Span,
    ) -> bool:
        """Run the 93-1 Haiku inference when the gate would block on
        ``missing_axes_with_pack_axes`` and freeform fodder exists.

        Returns whether an inference was ATTEMPTED (used by the caller to
        word the blocked error). On success the character carries a resolved
        archetype + provenance (via the same ``_resolve_character_archetype``
        path the preset flow uses) and the ``chargen.archetype_inferred``
        span has fired; on any failure the character is left untouched so
        the 45-6 gate blocks loudly downstream.
        """
        pack = sd.genre_pack
        pack_has_axes = pack.base_archetypes is not None and pack.archetype_constraints is not None
        would_block_missing_axes = (
            character.archetype_provenance is None
            and character.resolved_archetype is None
            and pack_has_axes
        )
        if not would_block_missing_axes:
            return False
        freeform_answers = builder.freeform_answer_texts()
        if not freeform_answers:
            # No fodder: the existing fail-loud block stands, zero SDK calls.
            return False

        acc = builder.accumulated()
        existing_hints: dict[str, str | None] = {
            "jungian_hint": acc.jungian_hint,
            "rpg_role_hint": acc.rpg_role_hint,
        }
        # Same session-identity ladder as the intent-router / seed-deck
        # seams: room slug > game slug > deterministic composite.
        if getattr(self, "_room", None) is not None:
            session_id = self._room.slug  # type: ignore[attr-defined]
        elif sd.game_slug is not None:
            session_id = sd.game_slug
        else:
            session_id = f"{sd.genre_slug}::{sd.world_slug}::{player_id}"

        # Late-bound function import (story 91-1 monkeypatch doctrine): the
        # tests fake the SDK construction site underneath this call.
        from sidequest.agents.llm_factory import infer_archetype_from_freeform

        try:
            inferred = await infer_archetype_from_freeform(
                freeform_text="\n\n".join(freeform_answers),
                base=pack.base_archetypes,
                constraints=pack.archetype_constraints,
                existing_hints=existing_hints,
                session_id=session_id,
            )
        except Exception as exc:  # noqa: BLE001 — see rationale below
            # Review rework [HIGH]: the real SDK raises ``anthropic.*``
            # exceptions (transport blips, 429/529, context-length 400s)
            # that are NOT in our LlmClientError family, and a response-
            # shape change can raise TypeError. ANY failure at this seam
            # must degrade to the loud inference-failed chargen block —
            # letting it propagate reaches websocket.py's outer catch,
            # which sends a generic "Server error" and tears the player's
            # WS session down mid-chargen. This is explicit handling, not
            # swallowing (python.md #1/#4): the failure is logged WARNING
            # here and the 45-6 gate blocks loudly downstream.
            logger.warning(
                "chargen.archetype_inference_failed player_id=%s session_id=%s error=%s",
                player_id,
                session_id,
                exc,
            )
            return True

        if inferred is None:
            # Out-of-enum (already logged at WARNING inside the inference)
            # or empty fodder after stripping. No coercion, no fallback.
            return True
        if not inferred:
            # Haiku declined every missing axis — nothing shippable.
            logger.warning(
                "chargen.archetype_inference_declined player_id=%s session_id=%s "
                "— no axis inferred from freeform answers",
                player_id,
                session_id,
            )
            return True

        jungian = existing_hints["jungian_hint"] or inferred.get("jungian_hint")
        rpg_role = existing_hints["rpg_role_hint"] or inferred.get("rpg_role_hint")
        if jungian is None or rpg_role is None:
            logger.warning(
                "chargen.archetype_inference_incomplete player_id=%s session_id=%s "
                "jungian=%s rpg_role=%s",
                player_id,
                session_id,
                jungian,
                rpg_role,
            )
            return True

        # Resolve through the SAME path the preset flow uses (Don't
        # Reinvent): write the raw pair and let _resolve_character_archetype
        # run the four-tier shim + apply_archetype_resolved. On resolver
        # rejection (e.g. forbidden pairing) it leaves the raw pair and the
        # gate blocks with resolver_raised — still loud.
        character.resolved_archetype = f"{jungian}/{rpg_role}"
        self._resolve_character_archetype(character, sd, player_id, span)
        if character.archetype_provenance is None:
            logger.warning(
                "chargen.archetype_inference_unresolvable player_id=%s session_id=%s "
                "pair=%s/%s — inferred pair rejected by the resolver",
                player_id,
                session_id,
                jungian,
                rpg_role,
            )
            return True

        # Story 93-2: badge the provenance entries whose words fed this
        # inference — marked iff the entry's text is in the fodder set.
        # ``freeform_answer_texts`` IS the fodder definition (name-entry
        # answers already excluded there), so the marking can never claim
        # a scene the Haiku call didn't consume. build() never marks;
        # this seam is the only writer of archetype_inferred=True.
        fodder = set(freeform_answers)
        provenance_marked = 0
        for entry in character.creation_answers:
            if entry.kind == "freeform" and entry.value in fodder:
                entry.archetype_inferred = True
                provenance_marked += 1

        # Success: the lie-detector span. Fires ONLY here — the GM panel
        # must distinguish "inferred from the player's words" from "preset
        # accumulation" (OTEL Observability Principle).
        with tracer.start_as_current_span(
            SPAN_CHARGEN_ARCHETYPE_INFERRED,
            attributes={
                "inferred_axes": sorted(inferred.keys()),
                "jungian_hint": jungian,
                "rpg_role_hint": rpg_role,
                "source": "freeform",
                "genre": sd.genre_slug,
                "world": sd.world_slug,
                "player_id": player_id,
                "provenance_marked": provenance_marked,
            },
        ):
            pass
        logger.info(
            "chargen.archetype_inferred player_id=%s session_id=%s inferred_axes=%s "
            "jungian=%s rpg_role=%s",
            player_id,
            session_id,
            sorted(inferred.keys()),
            jungian,
            rpg_role,
        )
        return True

    # ---- phase=confirmation (commit) ------------------------------------
    async def _chargen_confirmation(
        self,
        builder: CharacterBuilder,
        sd: _SessionData,
        player_id: str,
        span: trace.Span,
    ) -> list[object]:
        # Outbound accumulator must be bound BEFORE any nested emit closure
        # (e.g. _chargen_emit_tactical_grid / _chargen_emit_region_location)
        # is invoked — those fire on the room_graph and region init seams,
        # well before the CharacterCreationMessage is computed below. Binding
        # it here (rather than mid-method) keeps `out` from becoming an
        # unbound free variable in those closures (Playtest 2026-05-21
        # region-mode NameError crash).
        out: list[object] = []

        # Name resolution: scene > lobby > "Player". Do NOT fall back to
        # payload.choice — that's the UI button index (e.g. "1"), not a
        # name (Rust comment at connect.rs:1607).
        name_from_scene = builder.character_name()
        char_name = name_from_scene or sd.player_name or "Player"
        source = "name_scene" if name_from_scene is not None else "player_name_fallback"
        span.add_event(
            "character_creation.name_resolved",
            {
                "event": "name_resolved",
                "char_name": char_name,
                "source": source,
                "player_id": player_id,
            },
        )

        try:
            character = builder.build(char_name)
        except BuilderError as exc:
            return [_error_msg(f"Character build failed: {exc!r}")]
        except FateChargenError as exc:
            # ADR-144 F4a3: a Fate pack's build() routes through apply_fate_chargen,
            # which fails loud on an illegal sheet (e.g. a cleared mandatory aspect
            # that slipped past the per-step guards). Catch it here so the WebSocket
            # never sees an uncaught exception (handler contract) — structured error,
            # not a leaked repr.
            logger.warning("chargen.fate_build_rejected err=%r", exc)
            return [_error_msg("Character build failed: the Fate sheet is incomplete or invalid")]

        # Epic 66: copy the parked portrait-picker choice onto the built
        # Character before it lands on the snapshot. None = skipped picker
        # (or a world with no sample portraits) — cosmetic only.
        character.portrait_ref = sd.selected_portrait_ref

        # ADR-114: emit hp_current/hp_max (HP is the ablative pool per
        # ADR-114, reversing ADR-078). `schema=adr-114` lets us find
        # this seam in audits.
        span.add_event(
            "character_creation.character_built",
            {
                "event": "character_built",
                "schema": "adr-114",
                "name": character.core.name,
                "class": character.char_class,
                "race": character.race,
                "hp_current": character.core.hp.current,
                "hp_max": character.core.hp.max,
                "player_id": player_id,
            },
        )

        # Archetype resolution (Story 2.3 Slice A). The builder writes
        # a raw "jungian/rpg_role" pair into ``resolved_archetype`` when
        # both axis hints were accumulated during chargen. Resolve it
        # through the four-tier shim (base → constraints → world funnels)
        # and replace the raw pair with the resolved display name, also
        # stamping ``archetype_provenance`` so the GM panel can show the
        # source tier. Rust parity: connect.rs:1644-1737.
        self._resolve_character_archetype(character, sd, player_id, span)

        # Story 93-1: Haiku archetype inference for all-freeform chargen
        # ([BAR-1]). The builder only forms a pair when preset choices set
        # BOTH hints; a player who answered hint-bearing scenes freeform
        # accumulated none, so axis-bearing packs would dead-end at the
        # 45-6 gate below with ``missing_axes_with_pack_axes``. Intercept
        # that exact would-block state BEFORE the gate evaluates: when the
        # player supplied real freeform answers (name-entry text excluded —
        # see ``CharacterBuilder.freeform_answer_texts``), run a single
        # Haiku call to infer the MISSING axis value(s), constrained to the
        # pack's valid ids, then resolve through the same four-tier shim as
        # the preset path. Preset-set hints are never overridden. With no
        # freeform fodder the gate blocks exactly as before — zero SDK
        # calls, no pack-default fallback (No Silent Fallbacks).
        inference_attempted = await self._maybe_infer_archetype_from_freeform(
            builder, character, sd, player_id, span
        )

        # Story 45-6: archetype-resolution gate. After resolution runs
        # (or silently no-ops via one of the three early-return branches
        # at lines 574, 579, 595 of ``_resolve_character_archetype``),
        # this gate inspects the post-state and rejects partial
        # commits — the ``pumblestone`` regression from Playtest 3
        # evrópí. See ``_gate_archetype_resolution`` docstring for the
        # three pass / fail paths.
        is_blocked, block_reason = self._gate_archetype_resolution(character, sd, player_id, span)
        if is_blocked:
            if inference_attempted:
                # The inference ran and did not produce a shippable pair
                # (out-of-enum, declined axes, resolver rejection, or call
                # failure — each already logged at WARNING). Name inference
                # in the player-facing error so the operator can tell this
                # apart from the plain missing-axes block.
                return [
                    _error_msg(
                        "Character creation incomplete: archetype inference "
                        f"failed ({block_reason}). Please re-run chargen.",
                        code="chargen_archetype_unresolved",
                    )
                ]
            return [
                _error_msg(
                    "Character creation incomplete: archetype resolution "
                    f"failed ({block_reason}). Please re-run chargen.",
                    code="chargen_archetype_unresolved",
                )
            ]

        # Starting equipment loadout (Story 2.3 Slice A). The builder only
        # holds item_hints; the class-specific loadout from inventory.yaml
        # is wired in here. Rust parity: connect.rs:1745-1864.
        # Story 45-12: pass session identity so the dedup-evaluated /
        # dedup-fired spans carry genre/world/player_id for GM-panel
        # attribution. Snapshot slugs are populated at connect time.
        # Epic 94: inventory is a world-tier CAST/CATALOG surface. Resolve
        # world-first (worlds/<slug>/inventory.yaml) with genre-tier fallback —
        # the genre-tier ``sd.genre_pack.inventory`` is ``None`` for migrated
        # packs (space_opera, heavy_metal, spaghetti_western), so reading it
        # directly silently shipped empty loadouts (playtest 2026-06-06: blank
        # inventory tab in coyote_star + evropi).
        from sidequest.server.dispatch.inventory_resolve import resolve_inventory

        resolved_inventory = resolve_inventory(sd.genre_pack, sd.snapshot.world_slug)
        apply_starting_loadout(
            character,
            resolved_inventory,
            genre=sd.snapshot.genre_slug,
            world=sd.snapshot.world_slug,
            player_id=player_id,
        )

        # Story 106-1: equip the kit-rolled armor and derive core.armor_class
        # from its content (WWN SRD) armor_class. Runs AFTER the loadout/dedup
        # pass so the full inventory (kit-roll + starting_equipment) is present.
        # Without this every Warrior shipped with Leather Armor equipped:false
        # and fought at the unarmored base AC 10 (beneath_sunden meat-grinder,
        # 2026-06-13). Reuses the SAME resolved inventory config as the loadout.
        equip_starting_armor(
            character,
            resolved_inventory,
            genre=sd.snapshot.genre_slug,
            world=sd.snapshot.world_slug,
            player_id=player_id,
        )

        # Playtest 2026-06-05 (RW-2): the rig-name half of the name scene
        # ("What do they call the rig?"). When the player named their vessel,
        # rename the loadout's vessel-tagged item to match — tags (and thus
        # the rig-pool binding) are untouched; only the display name changes.
        # The content itself promises this (inventory.yaml: "The player names
        # their rig at chargen"). Both outcomes are observable (OTEL
        # Observability Principle): the rename, and the warn when a vessel
        # name was given but no vessel item exists in the loadout.
        vessel_name = builder.vessel_name()
        if vessel_name:
            vessel_item = find_vessel_item(character.core.inventory.items)
            if vessel_item is not None:
                template_name = str(vessel_item.get("name", ""))
                vessel_item["name"] = vessel_name
                span.add_event(
                    "character_creation.vessel_named",
                    {
                        "event": "vessel_named",
                        "item_id": str(vessel_item.get("id", "")),
                        "template_name": template_name,
                        "vessel_name": vessel_name,
                        "player_id": player_id,
                    },
                )
            else:
                span.add_event(
                    "character_creation.vessel_name_unbound",
                    {
                        "event": "vessel_name_unbound",
                        "vessel_name": vessel_name,
                        "severity": "warn",
                        "player_id": player_id,
                    },
                )

        # MP: peer may have already committed on the same slug. The
        # ADR-037 Python port has every WS session on a slug share the
        # room's canonical ``GameSnapshot`` reference, so an in-memory
        # check is the authoritative one. Disk reload here used to
        # paper over the snapshot-orphaning bug (sd.snapshot reassignment
        # below) by re-fetching the peer's persisted state, but the
        # peer's ``room.save()`` could only write the stale (empty)
        # bound snapshot — so the disk-read returned no characters and
        # the second player wrongly took the first-commit branch. With
        # the canonical snapshot mutated in place (``replace_with``
        # below), in-memory ``sd.snapshot.characters`` is always live.
        existing_chars: list = list(sd.snapshot.characters)
        is_first_commit = not existing_chars

        if is_first_commit:
            # World materialization (Story 2.3 Slice C / Rust connect.rs:1892).
            # Parse failure → empty-snapshot fallback (must not hard-fail mid-
            # commit when the character is already built).
            history_value = _world_history_value(sd.genre_pack, sd.world_slug)
            try:
                # Story 45-19: parse once at chargen and cache the typed
                # chapter list on _SessionData so the per-turn arc-recompute
                # tick doesn't re-parse history.yaml on every interaction.
                # Same input drives ``materialize_from_genre_pack`` below;
                # both calls share the same HistoryParseError fate.
                sd.cached_history_chapters = parse_history_chapters(history_value)
                materialized = materialize_from_genre_pack(
                    history_value,
                    CampaignMaturity.Fresh,
                    sd.genre_slug,
                    sd.world_slug,
                )
            except HistoryParseError as exc:
                # Loud failure (CLAUDE.md "No Silent Fallbacks"): log at
                # ERROR and emit an OTEL span event so the GM panel /
                # watcher dashboard surfaces the malformed shipping
                # content. We retain the empty-snapshot fallback because
                # the character has already been chargen-built and
                # hard-failing here would orphan the commit; but the
                # error is no longer invisible.
                logger.error(
                    "world_materialization.parse_failed genre=%s world=%s error=%s",
                    sd.genre_slug,
                    sd.world_slug,
                    exc,
                    exc_info=True,
                )
                span.add_event(
                    "history.parse_failed",
                    {
                        "event": "history.parse_failed",
                        "genre": sd.genre_slug,
                        "world": sd.world_slug,
                        "error": str(exc),
                        "exception_type": type(exc).__name__,
                        "exception_repr": repr(exc),
                    },
                )
                materialized = GameSnapshot(genre_slug=sd.genre_slug, world_slug=sd.world_slug)
                # Parse failed: cache an empty chapter list so the
                # arc-recompute tick is a graceful no-op rather than
                # propagating the parse error per turn.
                sd.cached_history_chapters = []
            # Canned-openings Phase 3 (Task 13): pre-load authored NPCs
            # before the chargen character is attached. Gate inside
            # ``preload_authored_npcs`` checks
            # ``state.characters == [] AND turn_manager.interaction == 0``;
            # the materialized snapshot satisfies both at this seam (the
            # PC is appended on the next line). Resumed sessions never
            # reach this branch — ``is_first_commit`` is False.
            world_for_authored = sd.genre_pack.worlds.get(sd.world_slug)
            if world_for_authored is not None:
                preload_authored_npcs(materialized, list(world_for_authored.authored_npcs))
            # ADR-033 resource pools (e.g. caverns_and_claudes' `light`) were
            # dead in production — init_resource_pools had no caller. Wire them
            # onto the fresh materialized snapshot here so a pack that declares
            # a pool starts the session with it populated.
            from sidequest.game.resource_wiring import wire_genre_resources

            wire_genre_resources(materialized, sd.genre_pack)
            # Discard the "Adventurer" placeholder the fresh chapter may
            # author — the chargen-built character owns that slot.
            materialized.characters = [character]
            # Playtest 2026-06-07 (No Silent Fallbacks): a chapter-seeded
            # trope id with no definition in genre OR world tropes.yaml is
            # a content bug — fail loud ONCE here at seed time (ERROR log +
            # watcher event) instead of entity_sync warn-spamming
            # ``project_failed error=no_definition`` every turn forever.
            from sidequest.server.dispatch.entity_sync import (
                audit_seeded_trope_definitions,
            )

            audit_seeded_trope_definitions(sd, materialized)
            # Story 77-1 (ADR-137 Option A): seed the campaign spine —
            # quest_log + quest_anchors + active_stakes — from the chargen
            # PC's drive/calling so the session starts with a non-empty
            # mechanical spine from turn 1 instead of narrator improvisation.
            # Emits quest.seeded_at_creation (severity="warning" on an empty
            # drive, e.g. prose packs) so the GM panel sees the seed engage.
            seed_quest_spine(materialized, character)
            # Wave 2B (story 45-48): the chapter's location was previously
            # written to the materialized snapshot's ``location`` field;
            # that field is gone. Backfill the now-attached PC's
            # per-character entry from the latest chapter that authored a
            # ``location`` so chargen-confirmation lands the player at the
            # scene the chapter described. Room-graph worlds overwrite this
            # below via ``init_room_graph_location`` (entrance room id),
            # which is intentional — the chapter's free-text location is
            # the fallback for non-room-graph worlds.
            for ch in reversed(materialized.world_history):
                if ch.location:
                    materialized.character_locations[character.core.name] = ch.location
                    break
            # Mutate the canonical room snapshot in place rather than
            # reassigning ``sd.snapshot``. Reassignment orphans the
            # ``room._snapshot`` reference: ``room.save()`` then
            # persists the stale pre-chargen snapshot, the next
            # connecting peer loads an empty save, treats themselves
            # as first-commit, materializes their own world, and the
            # slug ends up running two parallel solo games. Mutating
            # in place keeps every existing ``sd.snapshot`` /
            # ``room.snapshot`` reference live and pointing at the
            # same authoritative object — including any peer session
            # already bound to this slug.
            sd.snapshot.replace_with(materialized)

            # S1 (2026-05-04 split-brain cleanup): magic_state MUST be
            # initialized before init_chassis_registry runs, because the
            # chassis loader now writes confrontations directly into
            # snapshot.magic_state.confrontations (the canonical home).
            # The legacy world_confrontations stash is gone — see design
            # spec 2026-05-04-snapshot-split-brain-cleanup-design.md S1.
            #
            # Magic Phase 4: instantiate MagicState on the canonical
            # snapshot for worlds that ship a magic.yaml pair (genre +
            # world). This is the production hook that pairs Phase 1's
            # loader with Phase 2's snapshot field — without it,
            # snapshot.magic_state stays None and the LedgerPanel never
            # surfaces bars even though the engine can apply workings
            # correctly. ``add_character`` instantiates per-character
            # bars (sanity / notice / vitality on Coyote Star) keyed
            # to the actor name the narrator emits in magic_working.
            init_magic_state_for_session(
                snapshot=sd.snapshot,
                genre_pack_source_dir=sd.genre_pack.source_dir,
                world_slug=sd.world_slug,
                character_id=character.core.name,
                character_class=character.char_class,
            )
            init_mutation_state_for_session(
                sd.snapshot,
                catalog=sd.genre_pack.mutations,
                character_name=character.core.name,
                character_class=character.char_class,
                session_id=str(sd.repository.session_id),
                # Story 103-1: the world's Saint canon; story 103-2: the
                # stock roster + the builder-accumulated selections (the
                # stock scene's stock_id and any Saint-branch saint_id).
                # A stock-less walk leaves both None and the classic AWN
                # seeding path fires exactly as before.
                saints=(
                    sd.genre_pack.worlds[sd.world_slug].saints
                    if sd.world_slug in sd.genre_pack.worlds
                    else None
                ),
                saint_id=builder.chosen_saint_id,
                stocks=(
                    sd.genre_pack.worlds[sd.world_slug].stocks
                    if sd.world_slug in sd.genre_pack.worlds
                    else None
                ),
                stock_id=builder.chosen_stock_id,
                character=character,
            )

            init_chassis_registry(sd.snapshot, sd.genre_pack)
            # Story 47-6: bond_seeds in rigs.yaml use the
            # ``"player_character"`` placeholder. Rewrite every chassis's
            # bond_ledger to the real chargen character id so
            # process_room_entry can find the bond on later transitions.
            rebind_chassis_bonds_to_character(sd.snapshot, character.core.name)
            # Story 47-6: evaluate room-entry eligibility for the starting
            # interior_room so a session that opens INTO the galley fires
            # the_tea_brew on turn 1 instead of waiting for a later
            # narrator-driven location update.
            process_session_open(
                sd.snapshot,
                character_id=character.core.name,
                current_turn=sd.snapshot.turn_manager.interaction,
            )
            span.add_event(
                "character_creation.world_materialized",
                {
                    "event": "world_materialized",
                    "genre": sd.genre_slug,
                    "world": sd.world_slug,
                    "chapters_applied": len(materialized.world_history),
                    "maturity": materialized.campaign_maturity,
                    "trigger": "new_player_chargen",
                    "player_id": player_id,
                },
            )

            # Scenario binding (Slice D / connect.rs:1948). No-op without
            # scenarios; sets active_scenario on the session for later
            # pressure / scene-budget / accusation consumers.
            bind_result = bind_scenario(
                sd.genre_pack,
                sd.snapshot,
                genre_slug=sd.genre_slug,
                world_slug=sd.world_slug,
            )
            if bind_result is not None:
                _, active_pack = bind_result
                sd.active_scenario = active_pack

            # Political substrate hydration (wry_whimsy, Plan 2). No-op unless
            # the active world declares premises/blocs; sets snapshot.political_state
            # so the witnessed_act subsystem stops being inert.
            bind_political_state(
                sd.genre_pack,
                sd.snapshot,
                genre_slug=sd.genre_slug,
                world_slug=sd.world_slug,
            )

            world = sd.genre_pack.worlds.get(sd.world_slug)

            # Region init (Story 37-31). Runs for every world with
            # cartography. Init errors are pack-authoring bugs — log loud,
            # don't hard-fail the confirmation frame.
            if world is not None:
                try:
                    region_id = init_region_location(sd.snapshot, world.cartography)
                    span.add_event(
                        "region.initialized",
                        {
                            "event": "region.initialized",
                            "region": region_id,
                            "mode": world.cartography.navigation_mode.value,
                            "source": "starting_region",
                            "genre": sd.genre_slug,
                            "world": sd.world_slug,
                        },
                    )
                    logger.info(
                        "region.init genre=%s world=%s region=%s discovered_regions=%d",
                        sd.genre_slug,
                        sd.world_slug,
                        region_id,
                        len(sd.snapshot.discovered_regions),
                    )
                except RegionInitError as exc:
                    logger.error(
                        "region.init_failed genre=%s world=%s error=%s",
                        sd.genre_slug,
                        sd.world_slug,
                        exc,
                    )
                    span.add_event(
                        "region.init_failed",
                        {
                            "event": "region.init_failed",
                            "mode": world.cartography.navigation_mode.value,
                            "genre": sd.genre_slug,
                            "world": sd.world_slug,
                            "error": str(exc),
                        },
                    )

            if (
                world is not None
                and world.cartography.navigation_mode == NavigationMode.room_graph
                and world.cartography.rooms
            ):
                try:
                    entrance_id = init_room_graph_location(
                        sd.snapshot, list(world.cartography.rooms)
                    )
                    span.add_event(
                        "location.initialized",
                        {
                            "event": "location.initialized",
                            "location": entrance_id,
                            "mode": "room_graph",
                            "source": "entrance_room",
                            "genre": sd.genre_slug,
                            "world": sd.world_slug,
                        },
                    )
                    logger.info(
                        "room_graph.init genre=%s world=%s entrance=%s discovered_rooms=%d",
                        sd.genre_slug,
                        sd.world_slug,
                        entrance_id,
                        len(sd.snapshot.discovered_rooms),
                    )

                    # ADR-096 Task 20b: emit TACTICAL_GRID for the entrance room so
                    # the UI Automapper has grid data from session start, not only on
                    # the first narrator-driven location change.
                    def _chargen_emit_tactical_grid(msg: object, _kind: str) -> None:
                        out.append(msg)

                    _maybe_emit_tactical_grid(
                        self,
                        sd=sd,
                        snapshot=sd.snapshot,
                        actor=None,
                        emit_fn=_chargen_emit_tactical_grid,
                        room_id_override=entrance_id,
                    )
                    # Story 54-2 / ADR-109: emit LOCATION_DESCRIPTION on the
                    # chargen / session-resume init path. room_id_override
                    # carries the entrance room so a resumed client gets the
                    # manifest snapshot without polling.
                    _maybe_emit_location_description(
                        self,
                        sd=sd,
                        snapshot=sd.snapshot,
                        actor=None,
                        emit_fn=_chargen_emit_tactical_grid,
                        room_id_override=entrance_id,
                    )
                except RoomGraphInitError as exc:
                    logger.error(
                        "room_graph.init_failed genre=%s world=%s error=%s",
                        sd.genre_slug,
                        sd.world_slug,
                        exc,
                    )
                    span.add_event(
                        "location.init_failed",
                        {
                            "event": "location.init_failed",
                            "mode": "room_graph",
                            "genre": sd.genre_slug,
                            "world": sd.world_slug,
                            "error": str(exc),
                        },
                    )
            elif (
                world is not None
                and world.cartography.navigation_mode == NavigationMode.region
                and sd.snapshot.current_region
            ):
                # Playtest 2026-05-20 — Story 54-2 / ADR-109 chargen seam
                # only fired for room_graph mode, so region-mode worlds
                # (beneath_sunden surface, glenross, etc.) never received
                # an opening LOCATION_DESCRIPTION → the UI Location tab
                # never appears. Emit one here using current_region as the
                # room_id. The emit's Path-2 cartography fallback resolves
                # the prose + entities from ``cartography.regions``. No
                # tactical grid — that's room-graph / cavern territory.
                def _chargen_emit_region_location(msg: object, _kind: str) -> None:
                    out.append(msg)

                _maybe_emit_location_description(
                    self,
                    sd=sd,
                    snapshot=sd.snapshot,
                    actor=None,
                    emit_fn=_chargen_emit_region_location,
                    room_id_override=sd.snapshot.current_region,
                )
        else:
            # MP second commit. ADR-037 Python port: sd.snapshot is the
            # canonical room snapshot (already populated by the first
            # committer's bind_world); just append our PC if not already
            # present. No reload from store — the in-memory snapshot is
            # authoritative.
            existing_names = {c.core.name for c in sd.snapshot.characters}
            if character.core.name not in existing_names:
                sd.snapshot.characters.append(character)
            # Pingpong 2026-05-07 ("magic.init only fires for the host"):
            # the host's chargen-complete branch (above) calls
            # ``init_magic_state_for_session`` to register the actor in
            # the magic ledger AND emit the ``magic.init`` OTEL span;
            # MP joiners never reached that call site. Result: late
            # joiners (Donut, Katia) had no actor row in
            # ``snapshot.magic_state.ledger``, so any narrator-emitted
            # working against them raised ``unknown actor; call
            # add_character first`` (the same shape as the 2026-04-30
            # bug fixed in magic_init.py — the idempotence is now
            # required at TWO seams, not one). Mirror the call here so
            # every committer gets ``add_character`` + the
            # observable ``magic.init`` span. Idempotent on
            # ``snapshot.magic_state`` per the existing
            # first_commit-vs-reuse branch.
            init_magic_state_for_session(
                snapshot=sd.snapshot,
                genre_pack_source_dir=sd.genre_pack.source_dir,
                world_slug=sd.world_slug,
                character_id=character.core.name,
                character_class=character.char_class,
            )
            init_mutation_state_for_session(
                sd.snapshot,
                catalog=sd.genre_pack.mutations,
                character_name=character.core.name,
                character_class=character.char_class,
                session_id=str(sd.repository.session_id),
                # Story 103-1: the world's Saint canon; story 103-2: the
                # stock roster + the builder-accumulated selections (the
                # stock scene's stock_id and any Saint-branch saint_id).
                # A stock-less walk leaves both None and the classic AWN
                # seeding path fires exactly as before.
                saints=(
                    sd.genre_pack.worlds[sd.world_slug].saints
                    if sd.world_slug in sd.genre_pack.worlds
                    else None
                ),
                saint_id=builder.chosen_saint_id,
                stocks=(
                    sd.genre_pack.worlds[sd.world_slug].stocks
                    if sd.world_slug in sd.genre_pack.worlds
                    else None
                ),
                stock_id=builder.chosen_stock_id,
                character=character,
            )
            # Re-bind active_scenario on this socket from whatever the
            # peer wrote — its presence on sd.active_scenario is what
            # downstream pressure / accusation code keys off.
            if sd.active_scenario is None:
                bind_result = bind_scenario(
                    sd.genre_pack,
                    sd.snapshot,
                    genre_slug=sd.genre_slug,
                    world_slug=sd.world_slug,
                )
                if bind_result is not None:
                    _, active_pack = bind_result
                    sd.active_scenario = active_pack
            # Political substrate hydration (wry_whimsy, Plan 2). No-op unless
            # the active world declares premises/blocs; sets snapshot.political_state
            # so the witnessed_act subsystem stops being inert. Guard mirrors the
            # adjacent bind_scenario guard: a rejoiner must NOT overwrite live
            # belief/defiance dials with the authored initial values.
            if sd.snapshot.political_state is None:
                bind_political_state(
                    sd.genre_pack,
                    sd.snapshot,
                    genre_slug=sd.genre_slug,
                    world_slug=sd.world_slug,
                )
            span.add_event(
                "character_creation.mp_world_reused",
                {
                    "event": "mp_world_reused",
                    "genre": sd.genre_slug,
                    "world": sd.world_slug,
                    "existing_pc_count": len(existing_chars),
                    "appended_pc": character.core.name,
                    "total_pc_count": len(sd.snapshot.characters),
                    "player_id": player_id,
                },
            )
            logger.info(
                "session.mp_second_commit genre=%s world=%s existing_pcs=%d new_pc=%s total=%d",
                sd.genre_slug,
                sd.world_slug,
                len(existing_chars),
                character.core.name,
                len(sd.snapshot.characters),
            )

        # Pingpong 2026-04-30 ("Lore RAG returns empty_query_or_store
        # for all 4 PCs every turn — no lore reaching narration"): the
        # world's ``WorldLore`` was never seeded into the per-session
        # lore store — only chargen-choice fragments via
        # ``seed_lore_from_char_creation``. Result: every
        # ``lore_embedding.retrieve`` came back with
        # ``store_size=0 outcome=empty_query_or_store`` and the narrator
        # composed every turn with zero hits from the lore corpus.
        # Fix: ``seed_world_lore`` seeds the world-level lore.yaml
        # (e.g. ``coyote_star`` has its own history/geography/factions
        # distinct from ``space_opera``'s). Epic 74 made lore world-only;
        # the genre-tier seeder was removed in story 74-4, so
        # ``seed_world_lore`` reports ``genre_fragments_added=0`` always.
        # It runs BEFORE ``seed_lore_from_char_creation`` so the world
        # fragments land first; chargen choices layer on top with
        # ``Character`` category so they're scoped distinctly in the
        # LoreStore index. Idempotent: re-seeding on a reconnect
        # silently skips duplicate ids (``DuplicateLoreId`` guard).
        # OTEL lie-detector: per the user's pingpong note request, expose
        # ``lore.store_loaded count=N world=X`` so the GM panel can
        # distinguish "lore is empty by design for this scenario" from
        # "lore was supposed to load and didn't" — both pre-fix manifest
        # as ``outcome=empty_query_or_store`` at retrieve time. Sebastien's
        # State / Subsystems tabs read this watcher event. Shared with the
        # slug-resume connect path via ``seed_world_lore`` so a resumed
        # save's lore_store is re-seeded with the SAME deterministic
        # genre+world fragments and emits the SAME ``lore_store_loaded``
        # event (the resume gap commit 72750db exposed).
        def _emit_lore_store_loaded(
            *,
            genre_fragments_added: int,
            world_fragments_added: int,
            total_fragments: int,
            total_tokens: int,
        ) -> None:
            logger.info(
                "lore.store_loaded genre=%s world=%s genre_fragments=%d "
                "world_fragments=%d total=%d total_tokens=%d",
                sd.genre_slug,
                sd.world_slug,
                genre_fragments_added,
                world_fragments_added,
                total_fragments,
                total_tokens,
            )
            _watcher_publish(
                "lore_store_loaded",
                {
                    "genre_slug": sd.genre_slug,
                    "world_slug": sd.world_slug,
                    "genre_fragments_added": genre_fragments_added,
                    "world_fragments_added": world_fragments_added,
                    "total_fragments": total_fragments,
                    "total_tokens": total_tokens,
                    "player_id": player_id,
                },
                component="rag",
            )

        genre_lore_added, world_lore_added = seed_world_lore(
            sd.lore_store,
            sd.genre_pack,
            sd.world_slug,
            emit=_emit_lore_store_loaded,
        )

        # Lore seeding (Slice F / connect.rs:2196). Must run BEFORE
        # clearing the builder — the seeder reads scene choices to
        # build Character-category lore fragments for narrator RAG.
        lore_added = seed_lore_from_char_creation(sd.lore_store, list(builder.scenes()))
        span.add_event(
            "lore.char_creation_seeded",
            {
                "event": "char_creation_lore_seeded",
                "fragments_added": lore_added,
                "total_fragments": len(sd.lore_store),
                "total_tokens": sd.lore_store.total_tokens(),
                "genre": sd.genre_slug,
                "world": sd.world_slug,
                "player_id": player_id,
            },
        )
        logger.info(
            "rag.character_creation_lore_seeded count=%d total=%d",
            lore_added,
            len(sd.lore_store),
        )
        _watcher_publish(
            "lore_retrieval",
            {
                "reason": "character_creation_seed",
                "fragments_added": lore_added,
                "total_fragments": len(sd.lore_store),
                "total_tokens": sd.lore_store.total_tokens(),
                "genre_slug": sd.genre_slug,
                "world_slug": sd.world_slug,
                "player_id": player_id,
            },
            component="rag",
        )

        # NPC registry reset removed in story 45-52. The pre-Wave-2A clear
        # targeted the legacy ``npc_registry`` (chargen-tier name extractions
        # — player's own name, lobby filler). With the registry dropped and
        # the post-Wave-2A pool channel intentionally persistent across
        # chargen (world-authored cast pre-seeds; recurring narrator-cited
        # NPCs survive the seam), there is nothing to clear here.

        # MP per-player chargen binding (playtest 2026-04-25). Maps
        # player_id → character_name so slug-resume routes new players
        # to chargen instead of auto-claiming an existing PC.
        if sd.player_id and character.core.name:
            sd.snapshot.player_seats[sd.player_id] = character.core.name
            # Wave 2B (story 45-48) + sq-playtest 2026-05-09 [OBS]: seed
            # the joiner's per-character location from another seated PC.
            # The original branch used strict ``party_location()`` consensus
            # (all seated PCs agree on the same location), which fails the
            # moment a new player commits because *their* slot is still
            # empty — so MP commits 2..N never inherited and the
            # post-chargen window had no consensus at all. Loose
            # inheritance: take any other seated PC's known location. The
            # chargen-complete bootstrap from
            # ``_bootstrap_character_locations_from_opening`` ensures the
            # FIRST committer has a location to inherit.
            if character.core.name not in sd.snapshot.character_locations:
                inherited_from: str | None = None
                inherited_loc: str | None = None
                for seated_name in sd.snapshot.player_seats.values():
                    if not seated_name or seated_name == character.core.name:
                        continue
                    candidate = sd.snapshot.character_locations.get(seated_name)
                    if candidate:
                        inherited_from = seated_name
                        inherited_loc = candidate
                        break
                if inherited_loc:
                    sd.snapshot.character_locations[character.core.name] = inherited_loc
                    span.add_event(
                        "snapshot.character_location_inherited",
                        {
                            "event": "snapshot.character_location_inherited",
                            "joiner": character.core.name,
                            "inherited_from": inherited_from or "",
                        },
                    )
            span.add_event(
                "session.player_seat_bound",
                {
                    "event": "session.player_seat_bound",
                    "genre": sd.genre_slug,
                    "world": sd.world_slug,
                    "player_id": sd.player_id,
                    "character_name": character.core.name,
                    "seat_count": len(sd.snapshot.player_seats),
                },
            )
            logger.info(
                "session.player_seat_bound player_id=%s character=%s seat_count=%d",
                sd.player_id,
                character.core.name,
                len(sd.snapshot.player_seats),
            )
            _watcher_publish(
                "session_player_seat_bound",
                {
                    "genre_slug": sd.genre_slug,
                    "world_slug": sd.world_slug,
                    "player_id": sd.player_id,
                    "character_name": character.core.name,
                    "seat_count": len(sd.snapshot.player_seats),
                    "seated_player_ids": list(sd.snapshot.player_seats.keys()),
                },
                component="session",
            )

        # Persist (Slice G / connect.rs:2174). Snapshot save makes the
        # next slug-resume hit has_character=True; failure must not
        # strand the player mid-commit (log loud, continue).
        try:
            # ADR-037 Python port: route through the canonical room save
            # so concurrent chargen-commits from peers are serialized by
            # the room lock. Fallback for legacy non-slug paths.
            if self._room is not None:
                self._room.save()
            else:
                sd.repository.save(sd.snapshot)
            span.add_event(
                "session.persisted_at_chargen_complete",
                {
                    "event": "session.persisted",
                    "genre": sd.genre_slug,
                    "world": sd.world_slug,
                    "player": sd.player_name,
                    "turn": sd.snapshot.turn_manager.interaction,
                },
            )
            logger.info(
                "session.persisted_at_chargen_complete genre=%s world=%s player=%s",
                sd.genre_slug,
                sd.world_slug,
                sd.player_name,
            )
        except Exception as exc:
            # Don't strand mid-commit — log + OTEL event, then proceed.
            # On next reconnect the save will be absent so chargen repeats.
            logger.error(
                "session.persist_failed_at_chargen_complete genre=%s world=%s error=%s",
                sd.genre_slug,
                sd.world_slug,
                exc,
            )
            span.add_event(
                "session.persist_failed_at_chargen_complete",
                {
                    "event": "session.persist_failed",
                    "genre": sd.genre_slug,
                    "world": sd.world_slug,
                    "player": sd.player_name,
                    "error": str(exc),
                },
            )

        # Flip to Playing atomically with persistence (Slice G /
        # connect.rs:2183). Pre-fix, a disconnect between confirm and
        # first action lost the save-state flag.
        self._state = _State.Playing

        # Story 45-2: chargen committed → seat transitions CHARGEN → PLAYING
        # so the turn barrier counts this peer. No-op if no room (solo
        # path with no MP room) or if the peer was already PLAYING (safe
        # under double-confirmation).
        if self._room is not None:
            self._room.transition_to_playing(player_id)

        sd.builder = None
        # Epic 66: the parked portrait pick was applied to the built Character
        # above; clear it (and re-arm the one-time portrait step) alongside the
        # builder so a future re-chargen on this session can't inherit a stale
        # pick or skip the pick_portrait frame.
        sd.selected_portrait_ref = None
        sd.portrait_step_shown = False
        # ADR-114 (supersedes ADR-078): HP is back as the personal vitality track.
        # Log surface-level mechanical state as hp=current/max.
        # `schema=adr-114` is grep-able so future audits can find this seam.
        logger.info(
            "chargen.complete schema=adr-114 char_name=%s class=%s race=%s hp=%d/%d",
            character.core.name,
            character.char_class,
            character.race,
            character.core.hp.current,
            character.core.hp.max,
        )

        payload = CharacterCreationPayload(
            phase="complete",
            total_scenes=builder.total_scenes(),
            character=character.model_dump(mode="json"),
        )
        # Prepend (not re-bind) so the CharacterCreationMessage stays first in
        # the returned list while preserving any LOCATION_DESCRIPTION /
        # TACTICAL_GRID messages already appended by the init closures above.
        out.insert(0, CharacterCreationMessage(payload=payload, player_id=player_id))

        # PARTY_STATUS snapshot (Slice H / connect.rs:2533). Lands the
        # Character tab populated at session-start. MP: also broadcast
        # to peers so they see the new arrival without waiting for their
        # own turn-end refresh.
        try:
            party_status_msg = views.build_session_start_party_status(
                self, sd, character, player_id
            )
            out.append(party_status_msg)
            from sidequest.game.persistence import (  # noqa: PLC0415 — break import cycle
                GameMode as _GameMode,
            )

            if (
                self._room is not None
                and sd.mode == _GameMode.MULTIPLAYER
                and self._socket_id is not None
            ):
                # Story 71-13: only the opening NARRATION moved to the
                # emit_event pipeline. party_status is a separate concern and
                # stays on room.broadcast, which carries the
                # broadcast.recipient_dropped watcher/WARNING for players in
                # _connected with no outbound queue (No Silent Fallbacks).
                self._room.broadcast(party_status_msg, exclude_socket_id=self._socket_id)
            span.add_event(
                "session.start.character_snapshot_emitted",
                {
                    "event": "session.start.character_snapshot_emitted",
                    "player_id": player_id,
                    "character_name": character.core.name,
                    "genre": sd.genre_slug,
                    "world": sd.world_slug,
                    "sheet_class": character.char_class,
                    "inventory_count": len(
                        [
                            i
                            for i in character.core.inventory.items
                            if str(i.get("state", "Carried")) == "Carried"
                        ]
                    ),
                },
            )
        except Exception as exc:
            # Snapshot frame is UI convenience — log loud, don't block.
            logger.error(
                "session.start.character_snapshot_failed player=%s error=%s",
                sd.player_name,
                exc,
            )
            span.add_event(
                "session.start.character_snapshot_failed",
                {
                    "event": "session.start.character_snapshot_failed",
                    "error": str(exc),
                    "player_id": player_id,
                },
            )

        # Canned-openings Phase 4 (Task 19): resolve + stash the
        # opening directive at chargen-completion.
        #
        # Playtest 2026-05-03 [BUG] Opening narration skips Kestrel
        # beat: previously gated on ``is_first_commit``, which fired
        # the resolver when only 1 PC was seated in MP — the canned
        # ``mp_galley_jumprest`` opening declares ``min_players: 2``
        # so resolution always failed and the narrator improvised
        # Vaskov Centrum customs. Now the populator runs on EVERY
        # commit (idempotent — bails when ``opening_directive`` is
        # already set, emits ``opening.skipped`` watcher events
        # otherwise), so the second committer's call succeeds with
        # the full party seated.
        rebound_region = _populate_opening_directive_on_chargen_complete(
            session_data=sd,
            snapshot=sd.snapshot,
            pack=sd.genre_pack,
            world_slug=sd.world_slug,
            mode=sd.mode,
        )

        # Playtest 2026-05-25 [BUG] flickering_reach: the region-mode
        # LOCATION_DESCRIPTION emit above (line ~2595) fired against the
        # spawn region (``starting_region``), but the opening anchors the
        # party at a different cartography node. When the opening rebound
        # ``current_region`` to its authored ``setting.region_id``, fire the
        # corrected LOCATION_DESCRIPTION for the opening's region so the UI
        # Location tab + Map agree with the prose. ``rebound_region`` is the
        # canonical node id (already validated against ``cartography.regions``
        # inside the populator) — only set when it actually changed.
        if rebound_region:

            def _opening_emit_region_location(msg: object, _kind: str) -> None:
                out.append(msg)

            _maybe_emit_location_description(
                self,
                sd=sd,
                snapshot=sd.snapshot,
                actor=None,
                emit_fn=_opening_emit_region_location,
                room_id_override=rebound_region,
            )

        # Opening-turn bootstrap (Slice H / connect.rs:2270). Fires
        # narrator with opening_seed + opening_directive (Early zone),
        # consumed once so subsequent turns run directive-free.
        #
        # Deferral gate (playtest 2026-05-03): in MP, skip the opening
        # narration on first commit so the canned MP opening can
        # resolve at second commit with full party context. The last
        # committer's narration is broadcast to peers below so the
        # first committer still sees the opening.
        if _should_fire_opening_narration(sd, self._room):
            # Story 71-13: the opening turn is event-sourced + projected
            # through the EventLog the same way a normal narration turn is —
            # but that emission happens INSIDE _run_opening_turn_narration now
            # (narrator NARRATION via _execute_narration_turn's internal
            # _emit_event; cold-open prose via its own _emit_event). The
            # returned frames are already emitted/built, so the caller appends
            # them exactly like _handle_player_action does — it must NOT
            # re-emit. The prior re-emit loop (sq-playtest 2026-05-28 #G1)
            # blindly persisted RENDER_QUEUED / NARRATION_END / AUDIO_CUE
            # frames under kind="NARRATION", bricking reconnect on replay.
            opening_messages = await self._run_opening_turn_narration(sd, player_id, span)
            out.extend(opening_messages)
        else:
            # Defer: don't fire opening narration yet — the next
            # committer will populate the directive and fire it for
            # everyone via the broadcast path above.
            seat_count = self._room.non_abandoned_player_count() if self._room is not None else 0
            _watcher_publish(
                "opening.deferred_party_incomplete",
                {
                    "genre": sd.genre_slug,
                    "world": sd.world_slug,
                    "player_id": player_id,
                    "characters_committed": len(sd.snapshot.characters),
                    "non_abandoned_seats": seat_count,
                },
                component="opening_hook",
                severity="info",
            )
            logger.info(
                "opening.deferred_party_incomplete genre=%s world=%s player=%s "
                "committed=%d expected=%d",
                sd.genre_slug,
                sd.world_slug,
                sd.player_name,
                len(sd.snapshot.characters),
                seat_count,
            )
        return out

    # ---- helper: next scene message OR confirmation summary -------------
    def _next_message(
        self,
        builder: CharacterBuilder,
        sd: _SessionData,
        player_id: str,
    ) -> list[object]:
        """After apply_choice / apply_freeform / apply_auto_advance, emit the
        appropriate next frame: either the next scene message, or the
        confirmation summary if the builder has transitioned to Confirmation.

        Rust parity: ``dispatch::chargen_summary::render_confirmation_summary``
        takes the pack + lobby_name directly because the builder does not
        own them.
        """
        # Roll the Bones (103-3, ADR-074): drain queued bones rolls into
        # visible DiceResult broadcasts — the dice land on the wire, not
        # in improvised prose. Empty for every non-bones flow.
        dice_msgs = _bones_dice_messages(builder, sd, player_id)
        if builder.is_confirmation():
            # Epic 66: interpose the one-time pick_portrait step at the
            # confirmation boundary. First arrival at Confirmation renders
            # the portrait scene; the client answers with
            # phase=portrait_confirm (_chargen_portrait_confirm), which
            # routes back through here with the gate flipped and falls
            # through to the confirmation summary. Per-player by
            # construction — _SessionData is per-session state.
            if not sd.portrait_step_shown:
                sd.portrait_step_shown = True
                return [*dice_msgs, self._render_portrait_scene(builder, sd, player_id)]
            return [
                *dice_msgs,
                render_confirmation_summary(
                    builder,
                    sd.genre_pack,
                    sd.player_name,
                    player_id,
                    world_slug=sd.snapshot.world_slug,
                ),
            ]
        msg = builder.to_scene_message(player_id)
        _stockify_scene_message(msg, builder, sd)
        return [*dice_msgs, msg]
