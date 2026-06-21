"""Opening-directive setup helpers for chargen-complete.

Extracted from ``websocket_session_handler`` (module-level free functions).
These resolve and stash a canned Opening at chargen-completion time, bind
``current_region`` to the opening's authored cartography node, bootstrap
per-PC locations, and gate whether opening narration fires this commit.

See ``docs/superpowers/specs/2026-05-01-canned-openings-design.md``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from opentelemetry import trace

from sidequest.game.quest_offer import stash_quest_offers
from sidequest.game.region_init import RegionInitError
from sidequest.game.session import RoomState
from sidequest.server.dispatch.opening import (
    OpeningResolutionError,
    _resolve_opening_post_chargen,
    build_directive,
)
from sidequest.telemetry.spans import SPAN_OPENING_PROPS_PERSISTED, Span
from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

if TYPE_CHECKING:
    from sidequest.game.session import GameSnapshot
    from sidequest.genre.models.pack import GenrePack
    from sidequest.server.session_state import _SessionData


def _populate_opening_directive_on_chargen_complete(
    session_data: _SessionData,
    snapshot: GameSnapshot,
    pack: GenrePack,
    world_slug: str,
    mode: object,
) -> str | None:
    """Resolve and stash an Opening directive at chargen-completion time.

    Called from the ``is_first_commit`` branch of
    :meth:`WebSocketSessionHandler._handle_character_creation` after
    authored NPCs have been pre-loaded but before persistence and the
    first narrator turn fires. Picks one Opening from the world's bank
    via :func:`_resolve_opening_post_chargen`, builds a directive via
    :func:`build_directive`, and stashes both the seed and directive on
    ``session_data`` so :meth:`_run_opening_turn_narration` can consume
    them on the very next call.

    Side effects:
    - sets ``session_data.opening_seed`` to the chosen Opening's
      first_turn_invitation
    - sets ``session_data.opening_directive`` to the rendered directive
    - sets ``session_data._resolved_opening_id`` for the played-span
    - binds ``snapshot.current_region`` to the opening's authored
      ``setting.region_id`` (a real cartography node) when it differs from
      the spawn region, emitting a ``state_patch.current_region`` OTEL span

    Returns the cartography region id the opening rebound ``current_region``
    to (so the caller can fire the opening LOCATION_DESCRIPTION for that
    region), or ``None`` when no rebind happened — no ``region_id`` declared,
    the region was already bound, or an early bail-out path was taken.

    No-ops gracefully when:
    - ``opening_directive`` is already populated (idempotency for
      double-confirmation guards)
    - the snapshot has no characters (defensive — chargen should have
      appended one already)
    - the world has no openings authored (Validator 7 should have
      caught this at world load)
    - resolution fails (Validators 7+8 should make this unreachable)

    The MP-joiner case is handled by the *caller*: this helper is only
    invoked from the ``is_first_commit`` branch, so a peer joining a
    snapshot that already has characters never reaches this code path.

    See ``docs/superpowers/specs/2026-05-01-canned-openings-design.md``
    §2.4 + §2.6.
    """
    if getattr(session_data, "opening_directive", None) is not None:
        return  # already populated — idempotent (no event: silent on replay)

    # Loud-fail bail-out paths (playtest 2026-05-03 — opening narration
    # skips Kestrel beat). Each ``return`` previously was a silent
    # ``# defensive`` bail; the GM panel had no signal that the canned
    # opening was even attempted. Now every skip emits an
    # ``opening.skipped`` watcher event so Sebastien sees the lie-
    # detector fire when the narrator improvises in place of the
    # canned opening. Severity=warning because the resolver running and
    # finding no match is the exact bug that surfaces as "PCs land in
    # Vaskov Centrum customs instead of the Kestrel galley".
    _genre_slug = getattr(session_data, "genre_slug", "")
    _world_slug = getattr(session_data, "world_slug", world_slug)

    def _emit_skip(reason: str, **extra: object) -> None:
        _watcher_publish(
            "opening.skipped",
            {
                "reason": reason,
                "genre": _genre_slug,
                "world": _world_slug,
                "characters_committed": len(snapshot.characters),
                **extra,
            },
            component="opening_hook",
            severity="warning",
        )

    if not snapshot.characters:
        _emit_skip("empty_snapshot")
        return

    pc = snapshot.characters[0]
    pc_background = getattr(pc, "background", "") or ""

    world = pack.worlds.get(world_slug)
    if world is None or not world.openings:
        _emit_skip(
            "world_or_openings_missing",
            world_present=world is not None,
            opening_bank_size=len(getattr(world, "openings", []) or []) if world else 0,
        )
        return

    mode_str = mode.value if hasattr(mode, "value") else str(mode)
    try:
        opening = _resolve_opening_post_chargen(
            world.openings,
            mode=mode_str,
            player_count=len(snapshot.characters),
            pc_background=pc_background,
            world_slug=world_slug,
        )
    except OpeningResolutionError as exc:
        # The active cause of the Kestrel-skip bug: ``mp_galley_jumprest``
        # declares ``min_players: 2`` and the resolver runs at first
        # commit when only 1 PC is seated. The deferral gate
        # (``_should_fire_opening_narration``) catches this and waits
        # for the second commit; the skip event tells Sebastien
        # *why* the resolver gave up.
        _emit_skip(
            "resolution_failed",
            mode=mode_str,
            player_count=len(snapshot.characters),
            pc_background=pc_background,
            opening_bank_size=len(world.openings),
            error=str(exc),
        )
        return

    # Chassis lookup — World may not store chassis_instances directly
    # (loader populates them as a sibling structure for validators).
    # Use getattr to fall through gracefully when the field is absent;
    # location-anchored Openings won't need it anyway.
    chassis = None
    authored_crew: list = []
    bond_tier: str = "neutral"
    if opening.setting.chassis_instance is not None:
        chassis_instances = getattr(world, "chassis_instances", []) or []
        chassis = next(
            (c for c in chassis_instances if c.id == opening.setting.chassis_instance),
            None,
        )
        if chassis is not None:
            npc_by_id = {n.id: n for n in world.authored_npcs}
            authored_crew = [npc_by_id[i] for i in chassis.crew_npcs if i in npc_by_id]
            for seed in chassis.bond_seeds:
                if seed.character_role == "player_character":
                    bond_tier = seed.bond_tier_chassis
                    break

    present_npcs: list = []
    if opening.setting.chassis_instance is None:
        npc_by_id = {n.id: n for n in world.authored_npcs}
        present_npcs = [npc_by_id[i] for i in opening.setting.present_npcs if i in npc_by_id]

    per_pc_beat = None
    pc_drive = getattr(pc, "drive", "") or ""
    for beat in opening.per_pc_beats:
        applies = beat.applies_to
        if applies.get("background") == pc_background:
            per_pc_beat = beat
            break
        if applies.get("drive") == pc_drive:
            per_pc_beat = beat
            break

    magic_register = getattr(world, "magic_register", "") or ""

    pc_name_parts = pc.core.name.split() if hasattr(pc, "core") else [""]
    directive = build_directive(
        opening=opening,
        chassis=chassis,
        authored_crew=authored_crew,
        magic_register=magic_register,
        bond_tier_for_pc=bond_tier,
        per_pc_beat=per_pc_beat,
        pc_first_name=getattr(pc, "first_name", None) or pc_name_parts[0],
        pc_last_name=getattr(pc, "last_name", "") or "",
        pc_nickname=getattr(pc, "nickname", "") or "",
        present_npcs=present_npcs,
    )

    session_data.opening_seed = opening.first_turn_invitation
    session_data.opening_directive = directive
    # Story 153-2: stash the authored establishing_narration so the cold-open
    # turn can emit it to the player VERBATIM (the narrator previously owned it
    # via the "play this scene" directive and dropped it). Consumed + cleared
    # with the seed/directive in ``_run_opening_turn_narration``.
    session_data.opening_establishing_narration = opening.establishing_narration
    session_data._resolved_opening_id = opening.id

    # Story 117-3 (ADR-146): stash the resolved opening's authored quest_seed
    # (if any) as a pending offer on the snapshot, next to the drive-spine seed.
    # Bait, not a mint — minting waits for the router to classify acceptance
    # (``quest_offer`` subsystem). Resume-safe: ``pending_quest_offers`` is
    # persisted snapshot state, not the ephemeral _SessionData directive.
    stash_quest_offers(snapshot, opening)

    # sq-playtest 2026-05-09 [OBS] projection.party_zone_absent_with_characters:
    # ``party_location()`` returned None at game start because no seated PC
    # had a ``character_locations`` entry — the perception rewriter then
    # fell back to "you can't identify them" mode and the narrator had to
    # call PCs *"another figure — armed, by the silhouette"* instead of by
    # name. Bootstrap every seated PC's location to the resolved Opening's
    # ``setting.location_label`` so ``visible_to()`` / ``in_same_zone()``
    # return true on turn 1. Idempotent: only writes when an entry is
    # absent. Both PCs land on the same string, so consensus matches.
    _bootstrap_character_locations_from_opening(snapshot, opening)

    # Playtest 2026-05-25 [BUG] flickering_reach MP: the active location never
    # advances from the spawn region. ``init_region_location`` seeds
    # ``current_region`` from ``cartography.starting_region`` (toods_dome),
    # but the opening anchors the party at Salt Camp / Blind Reach canyon — a
    # different cartography node. The narrator only emits a free-text
    # ``location_label``, never a region_id, so the Location panel + Map +
    # OTEL State stayed pinned to the spawn region. The opening now declares an
    # explicit ``setting.region_id`` (an authored binding to a real cartography
    # node); bind ``current_region`` to it here so the region-mode
    # LOCATION_DESCRIPTION + Map + OTEL State all agree with the prose.
    rebound_region = _bind_current_region_from_opening(snapshot, pack, world_slug, opening)

    # Story 126-18: persist the opening's interactable inanimate props
    # (envelope/Pernod/ashtray) into room_states so the next stateless per-turn
    # narrator/router (ADR-098/-113) sees them. Keyed to the same room string
    # the party resolves to (``setting.location_label``, written to
    # ``character_locations`` by the bootstrap above; ``interior_room`` for a
    # chassis anchor) so snapshot slimming projects the props into the per-turn
    # state. Without this, narrated-but-unpersisted props trip the
    # ``must_not_narrate`` guard and an opening-established hook is retracted
    # (SOUL Yes-And / Diamonds-and-Coal). The object-world twin of the authored-
    # NPC preload.
    present_props = list(getattr(opening.setting, "present_props", []) or [])
    if present_props:
        room_id = opening.setting.location_label or opening.setting.interior_room
        if room_id:
            persist_opening_props(snapshot, present_props, room_id=room_id)
        else:
            # Anchor invariant (OpeningSetting._exactly_one_anchor) guarantees
            # exactly one of location_label / (chassis + interior_room), so this
            # is unreachable — but never drop props silently (No Silent Fallbacks).
            _emit_skip("props_no_resolvable_room", present_props=len(present_props))

    return rebound_region


def persist_opening_props(
    snapshot: GameSnapshot,
    props: list[str],
    *,
    room_id: str,
) -> None:
    """Persist opening-scene interactable props into ``room_states[room_id]``.

    Story 126-18 — the object-world twin of ``preload_authored_npcs``. Writes
    the props into the room's ``RoomState.props`` (creating the entry if absent,
    preserving any existing container state) so the next stateless per-turn
    narrator/router (ADR-098/-113) sees them in the projected snapshot and the
    ``must_not_narrate`` guard never retracts an opening-established hook.

    Emits one flat-only ``opening.props_persisted`` span carrying the count and
    the prop ids (GM-panel lie-detector for the inverted failure mode: good
    narration, empty state — CLAUDE.md OTEL Observability Principle). Empty
    ``props`` is a hard no-op: nothing to persist, nothing to observe (no
    phantom room state, no empty span).
    """
    if not props:
        return

    room_state = snapshot.room_states.get(room_id)
    if room_state is None:
        room_state = RoomState(room_id=room_id)
        snapshot.room_states[room_id] = room_state
    for prop in props:
        if prop not in room_state.props:
            room_state.props.append(prop)

    with Span.open(
        SPAN_OPENING_PROPS_PERSISTED,
        {
            "props_persisted": len(props),
            "prop_ids": ",".join(props),
            "room_id": room_id,
            "genre_slug": getattr(snapshot, "genre_slug", "") or "",
            "world_slug": getattr(snapshot, "world_slug", "") or "",
        },
    ):
        pass


def _bind_current_region_from_opening(
    snapshot: GameSnapshot,
    pack: GenrePack,
    world_slug: str,
    opening: object,
) -> str | None:
    """Bind ``snapshot.current_region`` to the region the opening lands in.

    The opening picker can anchor the party in a region OTHER than
    ``cartography.starting_region`` (oz: the Emerald City gate; burning_peace:
    hakone). Region-resolution precedence — all DETERMINISTIC, never a fuzzy
    prose match (CLAUDE.md No-Silent-Fallbacks):

    1. ``setting.region_id`` — the authored binding to a real cartography graph
       node. If declared but NOT a real region, FAIL LOUD: emit a
       ``current_region.bind_failed`` ERROR span and raise (a pack-authoring
       bug). Bound with ``source=opening.setting.region_id``.
    2. ``setting.location_label`` when it is itself an EXACT match for a
       declared cartography region id (the burning_peace ``location_label:
       hakone`` authoring shape). An exact id match is deterministic — the
       narrow, documented exception to "location_label is prose only", NOT a
       fuzzy lookup. Bound with ``source=opening.location_label_region_match``
       so the GM panel can tell it apart from an authored region_id.

    When a region is resolved, set ``snapshot.current_region`` (dedup-append to
    ``discovered_regions``, seed per-PC regions), emit a
    ``state_patch.current_region`` OTEL span, and return the bound id so the
    caller can fire the opening LOCATION_DESCRIPTION for it — but only when the
    canonical id actually CHANGED (re-binding to the same region is a silent
    no-op, no redundant patch).

    When NEITHER resolves but the opening anchors a real place in a
    multi-region world (``location_label`` set + cartography declares regions —
    the oz prose-label shape), the region cannot be safely inferred and stays
    pinned to the static ``cartography.starting_region`` (possibly wrong). That
    is the OPENING-REGION-NO-PROPAGATE bug class, so make the gap LOUD: emit an
    ``opening.region_unbound`` WARNING span flagging that the opening needs an
    authored ``region_id`` (No Silent Fallbacks + OTEL Observability).
    Chassis-anchored / single-region / flavor-only-cartography openings have no
    region to bind and stay silent.

    Returns the bound region id when it changed, else ``None`` (no region
    resolved, or already bound to that region).
    """
    setting = getattr(opening, "setting", None)
    region_id = getattr(setting, "region_id", None)
    location_label = getattr(setting, "location_label", None)

    world = pack.worlds.get(world_slug)
    cartography = getattr(world, "cartography", None) if world is not None else None
    regions = getattr(cartography, "regions", {}) or {}
    opening_id = getattr(opening, "id", "") or ""

    resolved_region: str | None = None
    source = ""
    if region_id:
        if region_id not in regions:
            _watcher_publish(
                "current_region.bind_failed",
                {
                    "opening_id": opening_id,
                    "declared_region_id": region_id,
                    "world": world_slug,
                    "declared_regions": sorted(regions),
                    "reason": "region_id_not_a_cartography_node",
                },
                component="opening_hook",
                severity="error",
            )
            trace.get_current_span().add_event(
                "current_region.bind_failed",
                {
                    "event": "current_region.bind_failed",
                    "opening_id": opening_id,
                    "declared_region_id": region_id,
                    "world": world_slug,
                    "declared_regions": ",".join(sorted(regions)),
                },
            )
            raise RegionInitError(
                f"opening '{opening_id}' setting.region_id '{region_id}' is not a "
                f"declared cartography region (declared: {sorted(regions)})"
            )
        resolved_region = region_id
        source = "opening.setting.region_id"
    elif location_label and location_label in regions:
        # Deterministic exact-id fallback: the opening authored its region as
        # the location_label (burning_peace ``location_label: hakone``).
        resolved_region = location_label
        source = "opening.location_label_region_match"

    if resolved_region is None:
        # No region resolved. If the opening anchors a real place in a
        # multi-region world but declared no usable region binding, that is the
        # OPENING-REGION-NO-PROPAGATE gap — current_region stays at the static
        # starting_region (possibly wrong). Make it loud so the GM panel flags
        # the missing region_id.
        if regions and location_label:
            _watcher_publish(
                "opening.region_unbound",
                {
                    "opening_id": opening_id,
                    "location_label": location_label,
                    "current_region": snapshot.current_region or "",
                    "world": world_slug,
                    "declared_regions": sorted(regions),
                    "reason": "no_region_id_and_location_label_not_a_region",
                },
                component="opening_hook",
                severity="warning",
            )
            trace.get_current_span().add_event(
                "opening.region_unbound",
                {
                    "event": "opening.region_unbound",
                    "opening_id": opening_id,
                    "location_label": location_label,
                    "current_region": snapshot.current_region or "",
                    "world": world_slug,
                    "declared_regions": ",".join(sorted(regions)),
                },
            )
        return None

    prior_region = snapshot.current_region or ""
    if prior_region == resolved_region:
        return None  # already bound — no redundant patch

    snapshot.current_region = resolved_region
    if resolved_region not in snapshot.discovered_regions:
        snapshot.discovered_regions.append(resolved_region)
    # Movement subsystem §Q0: seed seated PCs' per-PC region (chargen complete,
    # seats known) so region_for(perspective=pc) resolves by movement time.
    snapshot.seed_pc_regions(resolved_region)

    _watcher_publish(
        "state_patch.current_region",
        {
            "opening_id": opening_id,
            "current_region": resolved_region,
            "prior_current_region": prior_region,
            "source": source,
            "world": world_slug,
        },
        component="opening_hook",
        severity="info",
    )
    trace.get_current_span().add_event(
        "state_patch.current_region",
        {
            "event": "state_patch.current_region",
            "opening_id": opening_id,
            "current_region": resolved_region,
            "prior_current_region": prior_region,
            "source": source,
            "world": world_slug,
        },
    )
    return resolved_region


def _bootstrap_character_locations_from_opening(snapshot: GameSnapshot, opening: object) -> None:
    """Write the opening's ``setting.location_label`` to every seated PC
    without a ``character_locations`` entry.

    Idempotent — preserves any prior entry (turn-1 narration apply or a
    resumed save's last-known location). Only fills the *empty* slots that
    cause ``party_location()`` to return None at game start.

    Emits ``snapshot.character_locations_bootstrapped`` (watcher event +
    span event) so the GM panel can verify the chargen-complete bootstrap
    fired and which seats were populated.
    """
    location = getattr(getattr(opening, "setting", None), "location_label", None)
    if not location:
        return
    seated = [name for name in snapshot.player_seats.values() if name]
    bootstrapped: list[str] = []
    for name in seated:
        if name not in snapshot.character_locations:
            snapshot.character_locations[name] = location
            bootstrapped.append(name)
    if bootstrapped:
        opening_id = getattr(opening, "id", "") or ""
        _watcher_publish(
            "snapshot.character_locations_bootstrapped",
            {
                "source": "opening.setting.location_label",
                "opening_id": opening_id,
                "bootstrapped_pcs": bootstrapped,
                "bootstrapped_count": len(bootstrapped),
                "seated_count": len(seated),
            },
            component="opening_hook",
            severity="info",
        )
        trace.get_current_span().add_event(
            "snapshot.character_locations_bootstrapped",
            {
                "event": "snapshot.character_locations_bootstrapped",
                "source": "opening.setting.location_label",
                "opening_id": opening_id,
                "bootstrapped_pcs": ",".join(bootstrapped),
                "bootstrapped_count": len(bootstrapped),
                "seated_count": len(seated),
            },
        )


def _should_fire_opening_narration(session_data: object, room: object) -> bool:
    """Decide whether to run opening narration on this chargen.complete.

    Playtest 2026-05-03: in MP, the canned ``mp_galley_jumprest`` opening
    declares ``min_players: 2`` but the populator runs at FIRST commit
    when only 1 PC is seated. Resolution fails → no directive → narrator
    improvises Vaskov Centrum customs instead of the Kestrel galley. The
    second committer hits ``mp_joiner_opening_suppressed_at_consume``
    and gets joiner-orientation anchored on the (improvised) host
    location — the canned MP opening is never used.

    Fix: the populator is now called on every commit (idempotent — bails
    if directive already set). This gate decides whether to *fire* the
    opening narration based on whether the party is complete enough
    that the canned opening can resolve. Solo paths fire on first
    commit (preserves prior behavior). MP first committers DEFER until
    the last committer can re-run the populator with the full party
    seated.

    Returns ``True`` when:
      - There is no MP room (solo headless path).
      - Room reports ``non_abandoned_player_count <= 1`` (solo via room).
      - The directive is already populated (resolver succeeded —
        either because we're solo or because this is the last
        committer and the party is complete).
      - All non-abandoned seats have committed chargen
        (``len(characters) >= non_abandoned_player_count``) — fire as a
        belt-and-suspenders fallback even if the populator hasn't
        managed to set the directive (e.g. world has no openings — the
        narrator's "I look around" fallback still beats silence).

    Returns ``False`` only in the explicit MP-defer case: room exists,
    >1 seat expected, party not complete, and no directive populated.
    """
    if room is None:
        return True
    try:
        seat_count = int(room.non_abandoned_player_count())
    except Exception:  # noqa: BLE001 — fail open to current behavior on contract drift
        return True
    if seat_count <= 1:
        return True
    if getattr(session_data, "opening_directive", None) is not None:
        return True
    snapshot = getattr(session_data, "snapshot", None)
    chars = getattr(snapshot, "characters", []) if snapshot is not None else []
    return len(chars) >= seat_count
