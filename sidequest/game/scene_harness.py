"""Scene-harness fixture hydrator (ADR-092 §Implementation).

Reads a YAML fixture from ``scenarios/fixtures/{name}.yaml`` and hydrates
it into a :class:`GameSnapshot` the save repository (``PgSaveRepository``,
ADR-115) can persist.
The dev-gated HTTP route in :mod:`sidequest.server.scene_harness_router`
wraps this hydrator; this module owns no I/O beyond the single fixture
read.

Two distinct error types let the HTTP layer map failures to 404 vs 422:

* :exc:`FixtureNotFoundError` — the fixture file does not exist in
  ``fixtures_dir``. HTTP layer surfaces as 404.
* :exc:`FixtureValidationError` — the fixture exists but its contents
  are invalid (missing ``genre``/``world``, malformed YAML, unsafe YAML
  payload, field-level validation rejection). HTTP layer surfaces as 422.

The hydrator inherits ADR-069's YAML schema and ADR-092's "Failure is
loud" discipline: no silent defaults for required identity fields, no
``yaml.load`` on untrusted input, no path traversal out of
``fixtures_dir``.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from sidequest.game.character import Character, KnownFact
from sidequest.game.creature_core import CreatureCore
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.scenario_state import (
    ScenarioRole,
    ScenarioState,
)
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.wwn_magic import EffortPool, SpellcastingState
from sidequest.genre.models.scenario import ClueGraph
from sidequest.magic.state import MagicState
from sidequest.protocol.models import AbilityDefinition
from sidequest.telemetry import watcher_hub as _hub

logger = logging.getLogger(__name__)


class FixtureNotFoundError(Exception):
    """The requested fixture YAML does not exist at the resolved path."""


class FixtureValidationError(Exception):
    """The fixture exists but its contents fail validation.

    Wraps ``yaml.YAMLError`` and ``pydantic.ValidationError`` so the
    HTTP layer never sees parser-internal exception types.
    """


# ``fixture_name`` must look like a safe filename stem: lowercase
# alphanumerics, underscores, hyphens. Rejects path traversal
# (``../``), absolute paths (``/foo``), separators, NUL bytes, and
# the empty string.
_FIXTURE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def hydrate_fixture(*, name: str, fixtures_dir: Path) -> GameSnapshot:
    """Load and hydrate ``{fixtures_dir}/{name}.yaml`` into a GameSnapshot.

    :param name: Fixture stem (no extension). Must match
        ``[A-Za-z0-9][A-Za-z0-9_-]*`` — rejects path traversal and
        absolute paths before any I/O.
    :param fixtures_dir: Directory containing fixture YAMLs. Production
        wiring points at ``orc-quest/scenarios/fixtures/``.
    :raises FixtureNotFoundError: ``name`` is invalid or the resolved
        file does not exist.
    :raises FixtureValidationError: YAML parse error or schema violation.
    """
    if not _FIXTURE_NAME_RE.match(name):
        # Path-traversal guard. Mapping to FixtureNotFoundError keeps
        # the HTTP 404 contract uniform — never reveals whether a
        # traversal target *would* have existed.
        raise FixtureNotFoundError(
            f"fixture name {name!r} is invalid — must match [A-Za-z0-9][A-Za-z0-9_-]*"
        )

    fixture_path = (fixtures_dir / f"{name}.yaml").resolve()
    fixtures_dir_resolved = fixtures_dir.resolve()
    # Belt + suspenders: even if the regex above somehow lets a
    # traversal slip through (it should not), the resolved path must
    # live under ``fixtures_dir``. Anything else → not found.
    if not str(fixture_path).startswith(str(fixtures_dir_resolved)):
        raise FixtureNotFoundError(f"fixture {name!r} resolves outside fixtures_dir")

    if not fixture_path.is_file():
        raise FixtureNotFoundError(f"fixture {name!r} not found at {fixture_path!s}")

    try:
        raw_text = fixture_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FixtureValidationError(
            f"failed to read fixture {name!r} at {fixture_path!s}: {exc}"
        ) from exc

    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        # ``yaml.safe_load`` raises subclasses of YAMLError on both
        # parse errors AND on the ``!!python/object``-style payloads
        # that ``yaml.load`` would have happily instantiated. The
        # security-test path lands here.
        logger.warning("scene_harness.yaml_parse_error name=%s err=%s", name, exc)
        raise FixtureValidationError(f"fixture {name!r}: YAML parse error — {exc}") from exc

    if data is None:
        raise FixtureValidationError(f"fixture {name!r} is empty")
    if not isinstance(data, dict):
        raise FixtureValidationError(
            f"fixture {name!r}: top level must be a YAML mapping, got {type(data).__name__}"
        )

    genre = data.get("genre")
    world = data.get("world")
    if not isinstance(genre, str) or not genre.strip():
        raise FixtureValidationError(
            f"fixture {name!r}: required field 'genre' is missing or empty"
        )
    if not isinstance(world, str) or not world.strip():
        raise FixtureValidationError(
            f"fixture {name!r}: required field 'world' is missing or empty"
        )

    snapshot_kwargs: dict[str, Any] = {
        "genre_slug": genre,
        "world_slug": world,
    }

    # Optional location (current_region) — fixtures use a free-text place
    # name. Wave 2B per-PC character_locations isn't wired here; the
    # narrator prompt builder consults current_region as a fallback.
    location = data.get("location")
    if isinstance(location, str) and location:
        snapshot_kwargs["current_region"] = location

    # Optional turn counter — combat_brawl_wasteland sets turn=3 so the
    # dispatcher doesn't think it's turn 1.
    turn = data.get("turn")
    if isinstance(turn, int) and turn > 0:
        from sidequest.game.session import TurnManager

        snapshot_kwargs["turn_manager"] = TurnManager(interaction=turn)

    # Hydrate PCs (story 50-23, ADR-092 follow-on).
    #
    # Two mutually-exclusive shapes are supported:
    #   character:        — legacy singular form (ADR-069 §Hydration rule 2),
    #                       lands at ``characters[0]``.
    #   characters: [...] — multi-PC list, each entry the same shape as the
    #                       legacy singular block; projects to
    #                       ``characters[N]`` in fixture-declared order.
    #
    # Both blocks present is a fixture authoring bug — fail loudly per
    # CLAUDE.md "No Silent Fallbacks" rather than silently pick one.
    singular_character = data.get("character")
    characters_list = data.get("characters")

    if singular_character is not None and characters_list is not None:
        raise FixtureValidationError(
            f"fixture {name!r}: cannot declare both 'character' and 'characters' "
            "blocks — pick one (legacy singular form or multi-PC list)"
        )

    if isinstance(singular_character, dict):
        try:
            snapshot_kwargs["characters"] = [_hydrate_character(singular_character)]
        except ValidationError as exc:
            raise FixtureValidationError(
                f"fixture {name!r}: character field validation failed — {exc}"
            ) from exc
    elif characters_list is not None:
        if not isinstance(characters_list, list):
            raise FixtureValidationError(
                f"fixture {name!r}: 'characters' must be a YAML list, "
                f"got {type(characters_list).__name__}"
            )
        hydrated: list[Character] = []
        for index, entry in enumerate(characters_list):
            if not isinstance(entry, dict):
                raise FixtureValidationError(
                    f"fixture {name!r}: characters[{index}] must be a YAML mapping, "
                    f"got {type(entry).__name__}"
                )
            try:
                hydrated.append(_hydrate_character(entry))
            except ValidationError as exc:
                raise FixtureValidationError(
                    f"fixture {name!r}: characters[{index}] validation failed — {exc}"
                ) from exc
            except FixtureValidationError as exc:
                # ``_hydrate_character()`` raises FixtureValidationError for
                # malformed ``known_facts`` shape (story 50-19). Re-raise
                # with the entry index so the fixture author knows which
                # PC to fix.
                raise FixtureValidationError(
                    f"fixture {name!r}: characters[{index}] — {exc}"
                ) from exc
        snapshot_kwargs["characters"] = hydrated

    # Hydrate NPC roster.
    npcs_data = data.get("npcs")
    if isinstance(npcs_data, list):
        try:
            snapshot_kwargs["npcs"] = [_hydrate_npc(n) for n in npcs_data if isinstance(n, dict)]
        except ValidationError as exc:
            raise FixtureValidationError(
                f"fixture {name!r}: npcs field validation failed — {exc}"
            ) from exc

    # Hydrate scenario_state (story 50-20, ADR-092 follow-on).
    #
    # Optional top-level ``scenario_state:`` block projects to
    # ``GameSnapshot.scenario_state`` (a ``ScenarioState``). Missing block →
    # snapshot field stays at its pydantic default (None) so the four
    # canonical pre-50-20 fixtures keep working unchanged. Malformed block →
    # FixtureValidationError per ADR-092 "Failure is loud" — no silent skip.
    if "scenario_state" in data and data.get("scenario_state") is not None:
        snapshot_kwargs["scenario_state"] = _hydrate_scenario_state(
            data["scenario_state"],
            npcs=snapshot_kwargs.get("npcs", []),
            fixture_name=name,
        )

    # Hydrate the encounter (story 50-21, ADR-092 follow-on).
    #
    # Optional top-level ``encounter:`` block projects to
    # ``GameSnapshot.encounter`` (a ``StructuredEncounter``, ADR-033). Missing
    # block (or an explicit ``encounter:`` with a null value) → snapshot field
    # stays at its pydantic default (None) so the three canonical fixtures
    # without an encounter block keep working. Malformed block →
    # FixtureValidationError per ADR-092 "Failure is loud".
    if "encounter" in data and data.get("encounter") is not None:
        snapshot_kwargs["encounter"] = _hydrate_encounter(data["encounter"], fixture_name=name)

    # Hydrate magic_state (story 50-22, ADR-092 follow-on).
    #
    # Optional top-level ``magic_state:`` block projects to
    # ``GameSnapshot.magic_state`` (a ``MagicState``). Missing block →
    # field stays at its pydantic default (None) so pre-50-22 fixtures keep
    # working. Present-but-malformed (including a present-but-empty ``{}``
    # with no ``config:``) → FixtureValidationError per ADR-014 (magic
    # state is Diamond — no synthetic/empty fallback) and ADR-092
    # "Failure is loud". The ``is not None`` guard deliberately lets an
    # empty ``{}`` through to _hydrate_magic_state so the missing-config
    # ValidationError surfaces rather than being silently treated as absent.
    if "magic_state" in data and data.get("magic_state") is not None:
        snapshot_kwargs["magic_state"] = _hydrate_magic_state(
            data["magic_state"],
            fixture_name=name,
        )

    try:
        snapshot = GameSnapshot(**snapshot_kwargs)
    except ValidationError as exc:
        raise FixtureValidationError(
            f"fixture {name!r}: snapshot construction failed — {exc}"
        ) from exc

    return snapshot


# ── Helpers ─────────────────────────────────────────────────────────────────


def _hydrate_character(data: dict[str, Any]) -> Character:
    """Map the fixture's flat character shape into ``Character`` + nested ``core``.

    Fixture YAMLs follow the legacy Rust shape where CreatureCore fields
    (name/description/personality/level/hp/max_hp/ac/inventory/statuses)
    are flattened to the top level. The Python port nests CreatureCore
    under ``Character.core``; this helper un-flattens.

    Legacy ``hp``/``max_hp`` integers are mapped onto :class:`HpPool`
    (current/max/base_max). ``ac`` has no current home in the Python
    Character shape and is dropped.

    The ``or ""`` patterns on required string fields keep pyright happy
    (``data.get(...)`` is ``str | None`` but the pydantic constructors
    require ``str``). Pydantic's non-blank field validators then reject
    the empty strings at construction time, so an omitted required
    field still surfaces as a ``ValidationError`` that ``hydrate_fixture``
    re-wraps as ``FixtureValidationError`` (HTTP 422).
    """
    core_kwargs: dict[str, Any] = {
        "name": data.get("name") or "",
        "description": data.get("description") or "",
        "personality": data.get("personality") or "",
        "level": int(data.get("level", 1)),
    }
    inv = data.get("inventory")
    if isinstance(inv, dict):
        core_kwargs["inventory"] = inv
    statuses = data.get("statuses")
    if isinstance(statuses, list):
        core_kwargs["statuses"] = statuses

    hp = data.get("hp")
    max_hp = data.get("max_hp")
    if isinstance(hp, int) and isinstance(max_hp, int):
        core_kwargs["hp"] = {"current": hp, "max": max_hp, "base_max": max_hp}

    # Hydrate WWN magic (story 90-4, Epic 90 / ADR-114/117/126).
    #
    # ``effort:`` and ``spellcasting:`` seed the per-CreatureCore WWN magic
    # the cast spine reads (``core.effort.get(source)`` / ``core.spellcasting``
    # in ruleset/wwn.py). Both are save-bearing — a malformed shape fails
    # loudly (FixtureValidationError → HTTP 422) rather than silently skipping,
    # which would ship a non-casting "caster" and let a deterministic proof
    # pass for the wrong reason (CLAUDE.md "No Silent Fallbacks", ADR-092
    # "Failure is loud", lang-review #1/#11). Field-level pydantic
    # ValidationError (extra=forbid typo, bad value type) propagates to the
    # ``hydrate_fixture`` caller, which re-wraps it as FixtureValidationError
    # on both the singular and multi-PC paths.
    #
    # DETERMINISTIC note: unlike chargen's ``seed_wwn_magic`` (which seeds
    # ``prepared == []`` — spells chosen at rest), a fixture seeds ``prepared``
    # VERBATIM so a cast can succeed with no rest beat in the loop.
    raw_effort = data.get("effort")
    if raw_effort is not None:
        if not isinstance(raw_effort, dict):
            raise FixtureValidationError(
                f"character.effort must be a YAML mapping keyed by source, "
                f"got {type(raw_effort).__name__}"
            )
        effort: dict[str, EffortPool] = {}
        for source, pool_raw in raw_effort.items():
            if not isinstance(pool_raw, dict):
                raise FixtureValidationError(
                    f"character.effort[{source!r}] must be a YAML mapping, "
                    f"got {type(pool_raw).__name__}"
                )
            # ``source`` is derived from the mapping key — strip any stray
            # in-value ``source`` so the key is authoritative.
            pool_kwargs = {k: v for k, v in pool_raw.items() if k != "source"}
            effort[str(source)] = EffortPool(source=str(source), **pool_kwargs)
        core_kwargs["effort"] = effort

    raw_spellcasting = data.get("spellcasting")
    if raw_spellcasting is not None:
        if not isinstance(raw_spellcasting, dict):
            raise FixtureValidationError(
                f"character.spellcasting must be a YAML mapping, "
                f"got {type(raw_spellcasting).__name__}"
            )
        core_kwargs["spellcasting"] = SpellcastingState(**raw_spellcasting)

    core = CreatureCore(**core_kwargs)

    # OTEL: when a fixture stages WWN crunch, emit a watcher event so the GM
    # panel can confirm the deterministic fixture seeded real spellcasting/
    # effort rather than the narrator improvising it (CLAUDE.md OTEL
    # Observability Principle — the GM panel is the lie detector). Module-
    # qualified call so the standard ``_capture_events`` harness intercepts it,
    # matching the ``magic.state_hydrated`` emitter convention below.
    if core.spellcasting is not None or core.effort:
        _hub.publish_event(
            "wwn.magic_hydrated",
            {
                "actor": core.name,
                "has_spellcasting": core.spellcasting is not None,
                "prepared": len(core.spellcasting.prepared) if core.spellcasting else 0,
                "casts_per_day": core.spellcasting.casts_per_day if core.spellcasting else 0,
                "effort_sources": sorted(core.effort),
            },
            component="magic",
            severity="info",
        )

    # Hydrate known_facts (story 50-19, ADR-092 follow-on).
    #
    # Unlike inventory/statuses, known_facts is save-bearing — a malformed
    # shape must fail loudly (FixtureValidationError → HTTP 422) rather
    # than silently skip the block. KnownFact's pydantic constructor owns
    # confidence-tier validation post-50-17 (the legacy "confirmed" value
    # is rejected by the Literal); we let ValidationError propagate up to
    # the caller's wrap-as-FixtureValidationError block.
    known_facts: list[KnownFact] = []
    raw_facts = data.get("known_facts")
    if raw_facts is not None:
        if not isinstance(raw_facts, list):
            raise FixtureValidationError(
                f"character.known_facts must be a YAML list, got {type(raw_facts).__name__}"
            )
        for index, entry in enumerate(raw_facts):
            if not isinstance(entry, dict):
                raise FixtureValidationError(
                    f"character.known_facts[{index}] must be a YAML mapping, "
                    f"got {type(entry).__name__}"
                )
            # fact_id is the UI dedup key (JournalResponsePayload.fact_id);
            # a fixture-supplied id matching a real ScenarioClue.id would
            # silently suppress the legitimate discovery from the journal
            # UI. Always mint fresh — fixture authors do not need to control
            # this identifier, and we don't want them to.
            scrubbed = {k: v for k, v in entry.items() if k != "fact_id"}
            known_facts.append(KnownFact(**scrubbed))

    # Hydrate abilities (story 50-22, ADR-092/095 follow-on).
    #
    # Like known_facts, abilities is save-bearing and must fail loudly on a
    # malformed shape rather than silently skip. The non-list shape guard
    # raises FixtureValidationError directly; per-entry pydantic
    # ValidationError (missing/bad ``source``, ``extra="forbid"`` typo)
    # propagates to the ``hydrate_fixture`` caller, which wraps it as
    # FixtureValidationError on both the singular and multi-PC paths.
    abilities: list[AbilityDefinition] = []
    raw_abilities = data.get("abilities")
    if raw_abilities is not None:
        if not isinstance(raw_abilities, list):
            raise FixtureValidationError(
                f"character.abilities must be a YAML list, got {type(raw_abilities).__name__}"
            )
        for index, entry in enumerate(raw_abilities):
            if not isinstance(entry, dict):
                raise FixtureValidationError(
                    f"character.abilities[{index}] must be a YAML mapping, "
                    f"got {type(entry).__name__}"
                )
            abilities.append(AbilityDefinition(**entry))

    return Character(
        core=core,
        backstory=data.get("backstory") or "",
        narrative_state=data.get("narrative_state", ""),
        hooks=list(data.get("hooks") or []),
        char_class=data.get("char_class") or "",
        race=data.get("race") or "",
        pronouns=data.get("pronouns", ""),
        stats=dict(data.get("stats") or {}),
        known_facts=known_facts,
        abilities=abilities,
    )


def _hydrate_npc(data: dict[str, Any]) -> Npc:
    """Map the fixture's flat NPC shape into ``Npc`` + nested ``core``.

    Fixture shape (ADR-069):
        - name: str
        - role: str       (informational — folded into description/personality)
        - disposition: int (Disposition coerces from int)

    ``CreatureCore.description`` and ``.personality`` have non-blank
    validators; fixtures rarely set them for NPCs (the narrator fills
    in flavor as needed). We seed from ``role`` when present so the
    validators are satisfied without ginning up prose the narrator
    might contradict.
    """
    # ``name`` is required and pydantic catches missing/blank. ``description``
    # and ``personality`` defaults are LOAD-BEARING: canonical fixtures
    # (combat_brawl_wasteland, combat_dogfight_space) define NPCs with only
    # name/role/disposition and rely on the narrator to fill in flavor;
    # without seeded values CreatureCore's non-blank validators would reject
    # every fixture NPC.
    role = data.get("role") or "fixture NPC"
    core = CreatureCore(
        name=data.get("name") or "",
        description=data.get("description") or f"NPC ({role})",
        personality=data.get("personality") or role,
    )
    npc_kwargs: dict[str, Any] = {"core": core}
    if "disposition" in data:
        npc_kwargs["disposition"] = data["disposition"]
    return Npc(**npc_kwargs)


_ALLOWED_SCENARIO_ROLES: frozenset[str] = frozenset(
    {ScenarioRole.Guilty, ScenarioRole.Witness, ScenarioRole.Innocent}
)


def _hydrate_scenario_state(
    raw: Any,
    *,
    npcs: list[Npc],
    fixture_name: str,
) -> ScenarioState:
    """Hydrate the fixture's ``scenario_state:`` block into a ScenarioState.

    Validation discipline mirrors the rest of the hydrator: every malformed
    shape raises ``FixtureValidationError`` so the dev-gated HTTP layer
    returns 422 with field detail. DAG prerequisite enforcement is
    final-set-membership against ``discovered_clues`` — the same predicate
    :meth:`ScenarioState.discover_clue` applies at runtime, but checked
    against the final declared set so YAML order is irrelevant (the
    field is documented as ``set[str]``; reordering must not change the
    validation verdict).

    Tension is the only field that adjusts silently — clamping to
    ``[0.0, 1.0]`` matches :meth:`ScenarioState.set_tension`. Fixture authors
    declaring a value outside the range are not penalized at the wire.
    """
    if not isinstance(raw, dict):
        raise FixtureValidationError(
            f"fixture {fixture_name!r}: 'scenario_state' must be a YAML mapping, "
            f"got {type(raw).__name__}"
        )

    # ── clue_graph (pydantic deserialization owns nested validation) ────────
    clue_graph_raw = raw.get("clue_graph")
    if clue_graph_raw is None:
        clue_graph = ClueGraph()
    else:
        try:
            clue_graph = ClueGraph.model_validate(clue_graph_raw)
        except ValidationError as exc:
            raise FixtureValidationError(
                f"fixture {fixture_name!r}: scenario_state.clue_graph validation failed — {exc}"
            ) from exc

    # ── discovered_clues (DAG enforcement via final-set membership, no replay) ─
    discovered_raw = raw.get("discovered_clues")
    if discovered_raw is None:
        discovered_ids: list[str] = []
    elif isinstance(discovered_raw, (list, tuple, set)):
        discovered_ids = [str(c) for c in discovered_raw]
    else:
        raise FixtureValidationError(
            f"fixture {fixture_name!r}: scenario_state.discovered_clues must be "
            f"a YAML list, got {type(discovered_raw).__name__}"
        )

    # ── npc_roles (allowed values: ScenarioRole constants) ─────────────────
    npc_roles_raw = raw.get("npc_roles")
    if npc_roles_raw is None:
        npc_roles: dict[str, str] = {}
    elif not isinstance(npc_roles_raw, dict):
        raise FixtureValidationError(
            f"fixture {fixture_name!r}: scenario_state.npc_roles must be a YAML "
            f"mapping, got {type(npc_roles_raw).__name__}"
        )
    else:
        for npc_name, role_value in npc_roles_raw.items():
            if role_value not in _ALLOWED_SCENARIO_ROLES:
                raise FixtureValidationError(
                    f"fixture {fixture_name!r}: scenario_state.npc_roles[{npc_name!r}]: "
                    f"role {role_value!r} not in allowed set "
                    f"{sorted(_ALLOWED_SCENARIO_ROLES)}"
                )
        npc_roles = {str(k): str(v) for k, v in npc_roles_raw.items()}

    # ── guilty_npc resolution: accept name OR id against the npc roster ────
    guilty_raw = raw.get("guilty_npc", "")
    if guilty_raw is None or guilty_raw == "":
        guilty_npc = ""
    elif not isinstance(guilty_raw, str):
        raise FixtureValidationError(
            f"fixture {fixture_name!r}: scenario_state.guilty_npc must be a "
            f"string, got {type(guilty_raw).__name__}"
        )
    else:
        roster_names = [n.core.name for n in npcs]
        if guilty_raw in roster_names:
            guilty_npc = guilty_raw
        else:
            raise FixtureValidationError(
                f"fixture {fixture_name!r}: scenario_state.guilty_npc "
                f"{guilty_raw!r} not found in npcs roster; "
                f"available: {roster_names}"
            )

    # ── tension: silent clamp to [0.0, 1.0] per ScenarioState.set_tension() ─
    tension_raw = raw.get("tension")
    if tension_raw is None:
        tension = 0.0
    else:
        try:
            tension = max(0.0, min(1.0, float(tension_raw)))
        except (TypeError, ValueError) as exc:
            raise FixtureValidationError(
                f"fixture {fixture_name!r}: scenario_state.tension must be a "
                f"number, got {type(tension_raw).__name__}"
            ) from exc

    # ``discovered_clues`` is a set in the ScenarioState model (AC#2), so the
    # DAG check must validate against the FINAL declared set rather than the
    # intermediate state of a per-clue replay. Walking through
    # ``ScenarioState.discover_clue`` in YAML order would reject DAG-valid
    # fixtures listed in non-topological order — for instance
    # ``[clue_b, clue_a]`` (Reviewer [HIGH-1] regression). Validate
    # set-membership instead, then assign the validated set directly.
    declared = set(discovered_ids)
    node_by_id = {n.id: n for n in clue_graph.nodes}
    for clue_id in declared:
        node = node_by_id.get(clue_id)
        if node is None:
            # Preserves ``ScenarioState.discover_clue``'s empty-graph
            # idempotency: clues absent from the graph pass through unchanged.
            continue
        missing = [r for r in node.requires if r not in declared]
        if missing:
            raise FixtureValidationError(
                f"fixture {fixture_name!r}: scenario_state.discovered_clues: "
                f"cannot pre-discover clue {clue_id!r} — missing "
                f"prerequisites {missing!r}"
            )

    return ScenarioState(
        clue_graph=clue_graph,
        discovered_clues=declared,
        npc_roles=npc_roles,
        guilty_npc=guilty_npc,
        tension=tension,
    )


def _hydrate_magic_state(raw: Any, *, fixture_name: str) -> MagicState:
    """Hydrate the fixture's ``magic_state:`` block into a ``MagicState``.

    ``MagicState`` is ``extra="forbid"`` with a required ``config:``
    (``WorldMagicConfig`` — also ``extra="forbid"``, 11 required fields).
    Pydantic owns all nested validation; this helper's only jobs are the
    YAML-shape guard and the ``ValidationError`` → ``FixtureValidationError``
    wrap so the dev-gated HTTP layer returns 422 (never a leaked 500).

    No silent fallback (ADR-014 "magic state is Diamond"; ADR-092 "Failure
    is loud"; CLAUDE.md "No Silent Fallbacks"): a present-but-empty ``{}``
    or a missing ``config:`` raises rather than producing an empty
    MagicState. An OTEL watcher event is emitted on success so the GM
    panel can confirm a fixture staged real magic state rather than the
    narrator improvising one.
    """
    if not isinstance(raw, dict):
        raise FixtureValidationError(
            f"fixture {fixture_name!r}: 'magic_state' must be a YAML mapping, "
            f"got {type(raw).__name__}"
        )

    try:
        magic_state = MagicState.model_validate(raw)
    except ValidationError as exc:
        raise FixtureValidationError(
            f"fixture {fixture_name!r}: magic_state validation failed — {exc}"
        ) from exc

    # Module-qualified call (not a bound import) so the standard
    # ``_capture_events`` test harness — which monkeypatches
    # ``watcher_hub.publish_event`` — intercepts this event, matching the
    # established ``scene_harness_router`` emitter convention.
    _hub.publish_event(
        "magic.state_hydrated",
        {
            "fixture": fixture_name,
            "world_slug": magic_state.config.world_slug,
            "genre_slug": magic_state.config.genre_slug,
            "ledger_bars": len(magic_state.ledger),
            "confrontations": len(magic_state.confrontations),
            "control_tier_actors": len(magic_state.control_tier),
        },
        component="magic",
        severity="info",
    )
    return magic_state


_DEFAULT_METRIC_THRESHOLD = 10


def _hydrate_encounter(raw: Any, *, fixture_name: str) -> StructuredEncounter:
    """Hydrate the fixture's ``encounter:`` block into a StructuredEncounter.

    Validation discipline mirrors the rest of the hydrator: every malformed
    shape raises ``FixtureValidationError`` so the dev-gated HTTP layer
    returns 422 with field detail — no silent default for the required
    ``type``, no leaked ``pydantic.ValidationError``.

    Fixture shape (canonical ``combat_brawl_wasteland`` is the ground truth):

        encounter:
          type: combat              # → StructuredEncounter.encounter_type
          player_metric:            # optional per-metric override
            threshold: 25
          opponent_metric:
            threshold: 7

    ``type`` is the short fixture key for ``encounter_type`` (AC-4: the
    canonical fixture uses ``type: combat`` and is frozen by AC-8). Each
    metric defaults to ``current=0, starting=0, threshold=10``; a per-metric
    mapping may override ``threshold``. The legacy single-dial ``metric``
    key is rejected up front with a clear message rather than letting
    ``StructuredEncounter._reject_legacy_metric`` raise an unwrapped
    ``ValueError`` past the module boundary.
    """
    if not isinstance(raw, dict):
        raise FixtureValidationError(
            f"fixture {fixture_name!r}: 'encounter' must be a YAML mapping, "
            f"got {type(raw).__name__}"
        )

    if "metric" in raw:
        raise FixtureValidationError(
            f"fixture {fixture_name!r}: encounter.metric is the legacy single-dial "
            "field — StructuredEncounter uses player_metric + opponent_metric; "
            "remove 'metric'"
        )

    encounter_type = raw.get("type")
    if not isinstance(encounter_type, str) or not encounter_type.strip():
        raise FixtureValidationError(
            f"fixture {fixture_name!r}: encounter.type is required and must be a non-empty string"
        )

    def _threshold(key: str) -> Any:
        override = raw.get(key)
        if override is None:
            return _DEFAULT_METRIC_THRESHOLD
        if not isinstance(override, dict):
            raise FixtureValidationError(
                f"fixture {fixture_name!r}: encounter.{key} must be a YAML "
                f"mapping, got {type(override).__name__}"
            )
        return override.get("threshold", _DEFAULT_METRIC_THRESHOLD)

    # WWN hp_depletion seeding (story 90-4, Epic 90 / ADR-114/116/117).
    #
    # ``win_condition`` (ADR-114 HP-kill channel vs the default dial race),
    # ``category`` (confrontation category — combat/social/movement/...), and
    # ``actors`` (seated combatants the hp_depletion side-scan walks) are the
    # three keys a deterministic WWN combat fixture needs. Omitting them keeps
    # the StructuredEncounter pydantic defaults ("dial_threshold" / "" / []) so
    # the canonical combat_brawl_wasteland fixture (type: combat only) is
    # unchanged. An out-of-Literal win_condition or out-of-Literal actor side
    # surfaces as the wrapped ValidationError below — no silent default.
    extra_kwargs: dict[str, Any] = {}

    win_condition = raw.get("win_condition")
    if win_condition is not None:
        # Pass through verbatim — StructuredEncounter's Literal rejects typos
        # (caught by the ValidationError wrap), so no manual allow-list here.
        extra_kwargs["win_condition"] = win_condition

    category = raw.get("category")
    if category is not None:
        extra_kwargs["category"] = category

    raw_actors = raw.get("actors")
    if raw_actors is not None:
        if not isinstance(raw_actors, list):
            raise FixtureValidationError(
                f"fixture {fixture_name!r}: encounter.actors must be a YAML list, "
                f"got {type(raw_actors).__name__}"
            )
        actors: list[EncounterActor] = []
        for index, actor_raw in enumerate(raw_actors):
            if not isinstance(actor_raw, dict):
                raise FixtureValidationError(
                    f"fixture {fixture_name!r}: encounter.actors[{index}] must be a "
                    f"YAML mapping, got {type(actor_raw).__name__}"
                )
            try:
                actors.append(EncounterActor(**actor_raw))
            except ValidationError as exc:
                raise FixtureValidationError(
                    f"fixture {fixture_name!r}: encounter.actors[{index}] validation failed — {exc}"
                ) from exc
        extra_kwargs["actors"] = actors

    try:
        return StructuredEncounter(
            encounter_type=encounter_type,
            player_metric=EncounterMetric(
                name="player",
                current=0,
                starting=0,
                threshold=_threshold("player_metric"),
            ),
            opponent_metric=EncounterMetric(
                name="opponent",
                current=0,
                starting=0,
                threshold=_threshold("opponent_metric"),
            ),
            **extra_kwargs,
        )
    except ValidationError as exc:
        raise FixtureValidationError(
            f"fixture {fixture_name!r}: encounter validation failed — {exc}"
        ) from exc
