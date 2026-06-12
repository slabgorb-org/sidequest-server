"""``pf validate locations`` — location-manifest validator (Story 54-3 / ADR-109).

Three checks:

1. **Well-formedness (hard error):** every ``entities[*]`` row parses as
   ``LocationEntity``; no duplicate ids per region/room; ``real_object``
   carries a binding; ``flavor_only`` does not.
2. **Binding resolution (hard error):** ``binding.ref`` resolves in the
   target subsystem (``npcs.yaml`` for ``kind=npc``; ``scenarios/*.yaml``
   ``clues[].id`` for ``kind=clue`` / ``kind=scenario_clue``).
   ``kind=location_feature`` is free-form (id uniqueness within the
   region is the only constraint). ``kind=item`` is intentionally
   deferred — see comment in :func:`_check_binding`.
3. **Prose-manifest coherence (warning):** scan the region's
   ``description`` for ``the X`` / ``a X`` / ``an X`` phrases and
   proper-noun-shaped tokens; warn on tokens that don't resolve to an
   entity label, an NPC id/name, or a per-pack ``generic_allowlist[]``
   entry.

Hard errors gate CI. Warnings are surfaced but never block the exit
code. The server's runtime loader does NOT re-validate — it trusts
content that passed this validator (the validator is content-time,
not runtime).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import click
import yaml

from sidequest.cli.validate.common import packs_in
from sidequest.protocol.models import LocationEntity

Severity = Literal["error", "warning"]


@dataclass(frozen=True)
class Issue:
    code: str
    severity: Severity
    message: str
    pack: str
    world: str
    region_id: str | None
    file: str
    line: int | None = None


@dataclass
class ValidationResult:
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)

    def record(self, issue: Issue) -> None:
        (self.errors if issue.severity == "error" else self.warnings).append(issue)

    @property
    def success(self) -> bool:
        return not self.errors


# ---------------------------------------------------------------------------
# Safe YAML load (Story 63-13)
# ---------------------------------------------------------------------------


def _safe_load_yaml(
    path: Path,
    result: ValidationResult,
    *,
    pack: str,
    world: str,
    region_id: str | None = None,
) -> Any:
    """Parse ``path`` as YAML, routing a parse failure into a clean
    ``MALFORMED_YAML`` error Issue instead of letting ``yaml.YAMLError``
    propagate.

    The validator is dev tooling whose whole job is to report authoring
    errors — a tab/indent typo in a world file is exactly such an error, so it
    must surface as an Issue (with ``file`` + ``line`` from ``problem_mark``),
    never a traceback that aborts the run. Returns the parsed data, or ``None``
    on a parse failure (callers already coalesce ``None`` to a safe default).
    """
    try:
        return yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        # pyyaml's problem_mark.line is 0-indexed; 1-index it so the structured
        # field matches the 1-indexed line the embedded {exc} prose reports (and
        # author/editor conventions). A self-consistent Issue for the content
        # author reading it.
        mark = getattr(exc, "problem_mark", None)
        line = mark.line + 1 if mark is not None else None
        result.record(
            Issue(
                code="MALFORMED_YAML",
                severity="error",
                message=f"{path.name}: malformed YAML — {exc}",
                pack=pack,
                world=world,
                region_id=region_id,
                file=str(path),
                line=line,
            )
        )
        return None


# ---------------------------------------------------------------------------
# Prose-coherence regexes
# ---------------------------------------------------------------------------

_DEFINITE_NOUN_RE = re.compile(r"\b(the|a|an)\s+([a-z][a-z\-']{0,40})", re.IGNORECASE)
_PROPER_NOUN_RE = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b")
_LEADING_ARTICLE_RE = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)
# Sentence-end punctuation followed by a separator. Used to skip
# capitalized words at the start of a sentence (which the proper-noun
# regex would otherwise flag — "Tuesday." being the canonical example).
_SENTENCE_BOUNDARIES = (". ", "? ", "! ", ".\n", "?\n", "!\n")


def _norm(s: str) -> str:
    return s.strip().lower()


def _strip_article(phrase: str) -> str:
    return _LEADING_ARTICLE_RE.sub("", phrase.strip())


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _worlds_in(pack: Path) -> list[Path]:
    worlds_dir = pack / "worlds"
    if not worlds_dir.is_dir():
        return []
    return sorted(p for p in worlds_dir.iterdir() if p.is_dir())


# ---------------------------------------------------------------------------
# Side-band loaders (NPCs, clues, allowlist)
# ---------------------------------------------------------------------------


def _load_npc_tokens(
    world_dir: Path, result: ValidationResult, *, pack: str, world: str
) -> set[str]:
    """Return the set of normalized NPC ids and names declared by ``npcs.yaml``.

    Both ``id`` and ``name`` are accepted so that an entity binding by id
    (``ref: cassia``) and a prose mention by name (``Cassia``) both
    resolve cleanly.
    """
    path = world_dir / "npcs.yaml"
    if not path.is_file():
        return set()
    raw = _safe_load_yaml(path, result, pack=pack, world=world) or {}
    npcs = raw.get("npcs") or []
    tokens: set[str] = set()
    for npc in npcs:
        if isinstance(npc, dict):
            for key in ("id", "slug", "name"):
                val = npc.get(key)
                if isinstance(val, str) and val.strip():
                    tokens.add(_norm(val))
    return tokens


def _load_clue_ids(world_dir: Path, result: ValidationResult, *, pack: str, world: str) -> set[str]:
    """Return clue ids declared by any ``scenarios/*.yaml`` clue list."""
    ids: set[str] = set()
    scen_dir = world_dir / "scenarios"
    if not scen_dir.is_dir():
        return ids
    for scenario in sorted(scen_dir.glob("*.yaml")):
        data = _safe_load_yaml(scenario, result, pack=pack, world=world) or {}
        for clue in data.get("clues") or []:
            if isinstance(clue, dict):
                cid = clue.get("id")
                if isinstance(cid, str):
                    ids.add(cid)
    return ids


def _load_allowlist(pack_dir: Path, result: ValidationResult, *, pack: str) -> set[str]:
    """Return the per-pack ``generic_allowlist[]``, normalized + article-stripped."""
    cfg = pack_dir / "pack.yaml"
    if not cfg.is_file():
        return set()
    # pack.yaml is a pack-tier file — no world context, so world="".
    data = _safe_load_yaml(cfg, result, pack=pack, world="") or {}
    raw = data.get("generic_allowlist") or []
    out: set[str] = set()
    for item in raw:
        if isinstance(item, str) and item.strip():
            full = _norm(item)
            out.add(full)
            out.add(_norm(_strip_article(item)))
    return out


# ---------------------------------------------------------------------------
# Check 1 — well-formedness
# ---------------------------------------------------------------------------


def _check_well_formed_region(
    result: ValidationResult,
    *,
    pack: str,
    world: str,
    region_id: str,
    raw_entities: list[Any],
    source_file: str,
) -> list[LocationEntity]:
    """Return parsed entities; record an Issue for every malformed row."""
    parsed: list[LocationEntity] = []
    seen: set[str] = set()
    for entry in raw_entities or []:
        try:
            entity = LocationEntity.model_validate(entry)
        except Exception as exc:  # pydantic ValidationError or similar
            result.record(
                Issue(
                    code="MALFORMED_ENTITY",
                    severity="error",
                    message=f"entity failed validation: {exc}",
                    pack=pack,
                    world=world,
                    region_id=region_id,
                    file=source_file,
                )
            )
            continue
        if entity.id in seen:
            result.record(
                Issue(
                    code="DUPLICATE_ENTITY_ID",
                    severity="error",
                    message=f"duplicate entity id {entity.id!r} in region {region_id!r}",
                    pack=pack,
                    world=world,
                    region_id=region_id,
                    file=source_file,
                )
            )
            continue
        seen.add(entity.id)
        if entity.tier == "real_object" and entity.binding is None:
            result.record(
                Issue(
                    code="REAL_OBJECT_REQUIRES_BINDING",
                    severity="error",
                    message=f"entity {entity.id!r} is real_object but has no binding",
                    pack=pack,
                    world=world,
                    region_id=region_id,
                    file=source_file,
                )
            )
        if entity.tier == "flavor_only" and entity.binding is not None:
            result.record(
                Issue(
                    code="FLAVOR_ONLY_FORBIDS_BINDING",
                    severity="error",
                    message=(
                        f"entity {entity.id!r} is flavor_only but carries a binding; "
                        "drop the binding or change the tier to real_object/yes_and"
                    ),
                    pack=pack,
                    world=world,
                    region_id=region_id,
                    file=source_file,
                )
            )
        parsed.append(entity)
    return parsed


# ---------------------------------------------------------------------------
# Check 2 — binding resolution
# ---------------------------------------------------------------------------


def _check_binding(
    result: ValidationResult,
    entity: LocationEntity,
    *,
    pack: str,
    world: str,
    region_id: str,
    source_file: str,
    npc_tokens: set[str],
    clue_ids: set[str],
) -> None:
    if entity.binding is None:
        return
    kind = entity.binding.kind
    ref = entity.binding.ref
    if kind == "location_feature":
        # Free-form; uniqueness within the region is already covered by
        # the well-formedness duplicate-id check.
        return
    if kind == "npc":
        if _norm(ref) not in npc_tokens:
            result.record(
                Issue(
                    code="BINDING_UNRESOLVED",
                    severity="error",
                    message=f"entity {entity.id!r} binds to unknown npc {ref!r}",
                    pack=pack,
                    world=world,
                    region_id=region_id,
                    file=source_file,
                )
            )
        return
    if kind in {"clue", "scenario_clue"}:
        if ref not in clue_ids:
            result.record(
                Issue(
                    code="BINDING_UNRESOLVED",
                    severity="error",
                    message=f"entity {entity.id!r} binds to unknown {kind} {ref!r}",
                    pack=pack,
                    world=world,
                    region_id=region_id,
                    file=source_file,
                )
            )
        return
    if kind == "item":
        # ADR-109 implementation guidance: the canonical item corpus
        # interface hasn't stabilised yet (post-Epic-54 work). v1 is
        # intentionally permissive — item-binding resolution is a no-op.
        # Expand when the item subsystem firms up.
        return


# ---------------------------------------------------------------------------
# Check 3 — prose-manifest coherence (warning only)
# ---------------------------------------------------------------------------


def _is_sentence_initial(prose: str, start: int) -> bool:
    if start == 0:
        return True
    return prose[max(0, start - 2) : start] in _SENTENCE_BOUNDARIES


def _check_prose(
    result: ValidationResult,
    *,
    pack: str,
    world: str,
    region_id: str,
    prose: str,
    entities: list[LocationEntity],
    npc_tokens: set[str],
    allowlist: set[str],
    source_file: str,
) -> None:
    if not prose:
        return

    entity_labels = {_norm(e.label) for e in entities}
    entity_heads = {_norm(_strip_article(lbl)) for lbl in entity_labels}
    seen: set[str] = set()

    for match in _DEFINITE_NOUN_RE.finditer(prose):
        article, head = match.group(1), match.group(2)
        full = _norm(f"{article} {head}")
        head_norm = _norm(head)
        if full in seen:
            continue
        seen.add(full)
        if (
            full in entity_labels
            or head_norm in entity_heads
            or full in allowlist
            or head_norm in allowlist
        ):
            continue
        result.record(
            Issue(
                code="PROSE_DRIFT",
                severity="warning",
                message=(
                    f"description references {full!r} but no matching entity, "
                    "NPC, or generic_allowlist entry"
                ),
                pack=pack,
                world=world,
                region_id=region_id,
                file=source_file,
            )
        )

    for match in _PROPER_NOUN_RE.finditer(prose):
        token = match.group(1)
        norm = _norm(token)
        if norm in seen:
            continue
        if _is_sentence_initial(prose, match.start()):
            # False-positive avoidance: capitalized words at sentence
            # starts are ambiguous (proper noun vs ordinary noun).
            continue
        seen.add(norm)
        if norm in npc_tokens or norm in entity_labels or norm in entity_heads or norm in allowlist:
            continue
        result.record(
            Issue(
                code="PROSE_DRIFT",
                severity="warning",
                message=(
                    f"description references proper noun {token!r} but no "
                    "matching NPC, entity, or allowlist entry"
                ),
                pack=pack,
                world=world,
                region_id=region_id,
                file=source_file,
            )
        )


# ---------------------------------------------------------------------------
# Per-world walk
# ---------------------------------------------------------------------------


def _location_card_slugs(
    world_dir: Path, result: ValidationResult, *, pack: str, world: str
) -> set[str]:
    """Slugs of the location cards the lore page actually renders from
    ``locations.yaml`` — normalised exactly as the reference renderer does."""
    from sidequest.server.reference_slug import slugify

    path = world_dir / "locations.yaml"
    if not path.is_file():
        return set()
    data = _safe_load_yaml(path, result, pack=pack, world=world) or {}
    items: Any = data
    if isinstance(data, dict):
        items = next((v for v in data.values() if isinstance(v, list)), [])
    slugs: set[str] = set()
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                raw = item.get("id") or item.get("name")
                if raw and (slug := slugify(str(raw))):
                    slugs.add(slug)
    return slugs


def _history_poi_slugs(
    world_dir: Path, result: ValidationResult, *, pack: str, world: str
) -> set[str]:
    """Story 63-8: POI landscape-image manifest slugs from ``history.yaml``
    (``chapters[].points_of_interest[]`` and/or top-level), slugify-normalised."""
    from sidequest.server.reference_slug import slugify

    path = world_dir / "history.yaml"
    if not path.is_file():
        return set()
    data = _safe_load_yaml(path, result, pack=pack, world=world) or {}
    if not isinstance(data, dict):
        return set()
    pois: list[Any] = []
    chapters = data.get("chapters")
    if isinstance(chapters, list):
        for chapter in chapters:
            if isinstance(chapter, dict) and isinstance(chapter.get("points_of_interest"), list):
                pois.extend(chapter["points_of_interest"])
    if isinstance(data.get("points_of_interest"), list):
        pois.extend(data["points_of_interest"])
    slugs: set[str] = set()
    for poi in pois:
        if isinstance(poi, dict):
            raw = poi.get("slug") or poi.get("name")
            if raw and (slug := slugify(str(raw))):
                slugs.add(slug)
    return slugs


def _check_poi_image_slugs(
    result: ValidationResult,
    *,
    world_dir: Path,
    pack: str,
    world: str,
) -> None:
    """Story 63-8 AC-6: every history.yaml POI image-manifest slug must map to
    a renderable location card. A dangling slug means a generated landscape
    image that can never attach to anything — flag it loudly."""
    poi_slugs = _history_poi_slugs(world_dir, result, pack=pack, world=world)
    if not poi_slugs:
        return
    location_slugs = _location_card_slugs(world_dir, result, pack=pack, world=world)
    for slug in sorted(poi_slugs - location_slugs):
        result.record(
            Issue(
                code="POI_IMAGE_SLUG_UNMATCHED",
                severity="warning",
                message=(
                    f"history.yaml POI slug {slug!r} matches no location card in "
                    "locations.yaml; its landscape image can never render"
                ),
                pack=pack,
                world=world,
                region_id=None,
                file=str(world_dir / "history.yaml"),
            )
        )


def _validate_one_world(
    result: ValidationResult,
    *,
    pack_dir: Path,
    world_dir: Path,
    pack_slug: str,
    allowlist: set[str],
) -> None:
    world_slug = world_dir.name
    npc_tokens = _load_npc_tokens(world_dir, result, pack=pack_slug, world=world_slug)
    clue_ids = _load_clue_ids(world_dir, result, pack=pack_slug, world=world_slug)

    def _check_region(region_id: str, raw_entities: Any, prose: str, source_file: str) -> None:
        entities = _check_well_formed_region(
            result,
            pack=pack_slug,
            world=world_slug,
            region_id=region_id,
            raw_entities=raw_entities or [],
            source_file=source_file,
        )
        for entity in entities:
            _check_binding(
                result,
                entity,
                pack=pack_slug,
                world=world_slug,
                region_id=region_id,
                source_file=source_file,
                npc_tokens=npc_tokens,
                clue_ids=clue_ids,
            )
        _check_prose(
            result,
            pack=pack_slug,
            world=world_slug,
            region_id=region_id,
            prose=prose or "",
            entities=entities,
            npc_tokens=npc_tokens,
            allowlist=allowlist,
            source_file=source_file,
        )

    # POI / cartography path
    cart = world_dir / "cartography.yaml"
    if cart.is_file():
        data = _safe_load_yaml(cart, result, pack=pack_slug, world=world_slug) or {}
        regions = data.get("regions") or {}
        if isinstance(regions, dict):
            for region_id, region_data in regions.items():
                rd = region_data or {}
                _check_region(
                    str(region_id),
                    rd.get("entities") or [],
                    rd.get("description") or "",
                    str(cart),
                )

    # Procedural path — per-room YAMLs the materializer writes
    rooms_dir = world_dir / "rooms"
    if rooms_dir.is_dir():
        for room_path in sorted(rooms_dir.glob("*.yaml")):
            room_data = _safe_load_yaml(room_path, result, pack=pack_slug, world=world_slug) or {}
            _check_region(
                room_path.stem,
                room_data.get("entities") or [],
                room_data.get("description") or "",
                str(room_path),
            )

    # Story 63-8: POI landscape-image slug consistency (history.yaml manifest
    # must map to renderable location cards).
    _check_poi_image_slugs(result, world_dir=world_dir, pack=pack_slug, world=world_slug)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def validate_locations_in_world(world_dir: Path) -> ValidationResult:
    """Per-world programmatic entry — AC-5 surface.

    ``world_dir`` is the absolute path to a single
    ``<pack>/worlds/<world>/`` directory. This is the entry point
    consumed by Story 55-1's post-materialize integration test
    (``test_pf_validate_locations_on_materialized.py``) and by any
    other future caller that wants to validate a single world.

    A directory with no ``cartography.yaml`` and no ``rooms/`` returns
    an empty ``ValidationResult`` (no error, no warning) — that's the
    pre-materialization state of every procedural world and a clean
    signal, not a silent fallback.
    """
    result = ValidationResult()
    if not world_dir.is_dir():
        return result
    pack_dir = world_dir.parent.parent
    pack_slug = pack_dir.name if pack_dir.exists() else ""
    allowlist = _load_allowlist(pack_dir, result, pack=pack_slug) if pack_dir.exists() else set()
    _validate_one_world(
        result,
        pack_dir=pack_dir,
        world_dir=world_dir,
        pack_slug=pack_slug,
        allowlist=allowlist,
    )
    return result


def validate_packs(pack_roots: list[Path]) -> ValidationResult:
    """Multi-pack entry — walk every pack found under each given root.

    Each root may be a single pack directory (``pack.yaml`` at root) or
    a directory of many packs (each child with ``pack.yaml``).
    """
    result = ValidationResult()
    for root in pack_roots:
        for pack in packs_in(root):
            allowlist = _load_allowlist(pack, result, pack=pack.name)
            for world_dir in _worlds_in(pack):
                _validate_one_world(
                    result,
                    pack_dir=pack,
                    world_dir=world_dir,
                    pack_slug=pack.name,
                    allowlist=allowlist,
                )
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.command()
@click.option(
    "--genre-packs-root",
    "roots",
    multiple=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Genre-pack directory or single pack root. May be passed multiple times.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
@click.pass_context
def main(ctx: click.Context, roots: tuple[Path, ...], as_json: bool) -> None:
    """Validate location manifests across every wired genre pack."""
    if not roots:
        # Default: every wired pack via the loader's search paths.
        from sidequest.genre.loader import DEFAULT_GENRE_PACK_SEARCH_PATHS

        roots = tuple(DEFAULT_GENRE_PACK_SEARCH_PATHS)

    result = validate_packs(list(roots))

    if as_json:
        payload = {
            "passed": result.success,
            "errors": [asdict(i) for i in result.errors],
            "warnings": [asdict(i) for i in result.warnings],
        }
        click.echo(json.dumps(payload, indent=2))
    else:
        for issue in result.errors:
            click.echo(f"[ERROR] {issue.code} {issue.file}: {issue.message}", err=True)
        for issue in result.warnings:
            click.echo(f"[WARN] {issue.code} {issue.file}: {issue.message}", err=True)
        click.echo(
            f"locations: {len(result.errors)} errors, {len(result.warnings)} warnings",
            err=True,
        )

    ctx.exit(0 if result.success else 1)


if __name__ == "__main__":  # pragma: no cover
    main()
