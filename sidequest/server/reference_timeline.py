"""Story 65-12 — lore-page world Timeline section.

The lore reference page (``GET /reference/lore/{pack}/{world}``, ADR-135 public
projection) gains a **Timeline** section: a world-historical spine built from the
world's legends. Per the 65-12 design the spine is **legends only** — campaign
``history.yaml:chapters`` ride a different (play-time) axis and carry dormant-trope
spoiler seeds (ADR-135 D1), and POI founding has no authored field (deferred to
74-3).

The one genuinely new behaviour is an **honest conditional sort**. The temporal
value authors write (``era``, falling back to ``period``) is free-text and
bespoke per world — absolute years, relative phrases, fantasy calendars, prose.
So the spine sorts dated entries ascending ONLY when every one exposes a
uniformly-parseable key (a signed integer year); otherwise it preserves authored
order. The render records which mode fired in an OTEL span so the page never
claims a chronology it could not compute. Undated legends (no era and no period)
always follow the dated spine, in authored order.

Story 100-12 (Phase 4 cutover) retired the HTML emitter (``present_lore_timeline``);
the Timeline section is now React-rendered from the JSON projection. What survives
here are the data helpers ``reference_projection.py`` imports: the legend/history
loaders (reusing the genre ``_load_legends_flexible`` parser) and the temporal /
year-key primitives that drive the projection's honest conditional sort.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from sidequest.genre.loader import _load_legends_flexible, _load_yaml_raw
from sidequest.genre.models.legends import Legend

# A temporal value is "uniformly parseable" only if it is a bare signed integer
# year (e.g. "1612", "-11540"). Relative phrases ("15 years ago"), named ages
# ("early Second Rising"), and prose dates are intentionally NOT parsed: the
# spine falls back to authored order rather than fabricating a cross-dialect
# chronology (see Design Deviation — relative-family sorting is not implemented).
_YEAR_RE = re.compile(r"^-?\d+$")


def load_legends(world_dir: Path) -> list[Legend]:
    """Load the world's legends into typed ``Legend`` records.

    Handles BOTH authoring forms the genre loader accepts (see
    ``genre/loader.py:_load_single_world`` lines ~1037-1048), because real worlds
    use both — and the dominant form is the per-file directory:

    - a per-file ``legends/`` **directory** (one legend per ``*.yaml``; the form
      used by five_points, evropi, franchise_nations, … — most live worlds), or
    - a flat ``legends.yaml`` (``Vec<Legend>`` or a ``{legends: [...]}`` map).

    Returns ``[]`` when the world authors no legends (the Timeline section is
    purely additive — a world without legends renders unchanged). Fails **loud**
    on a malformed file (No Silent Fallbacks): ``Legend`` validation raises for a
    shape the model rejects, which the lore route surfaces as HTTP 500 rather
    than a silently timeline-less page.
    """
    legends_dir = world_dir / "legends"
    if legends_dir.is_dir():
        files = [
            f
            for f in sorted(legends_dir.glob("*.yaml"))
            if f.name not in ("_meta.yaml", ".gitkeep")
        ]
        return [Legend.model_validate(_load_yaml_raw(f)) for f in files]
    legends, _raw = _load_legends_flexible(world_dir / "legends.yaml")
    return legends


def load_lore_history(world_dir: Path) -> str | None:
    """Return the ``history`` prose from ``world_dir/lore.yaml``, or ``None``.

    Used only as the Timeline section's framing preamble — never decomposed into
    entries. Absent file, absent field, or non-string value all yield ``None``
    (no preamble renders).
    """
    path = world_dir / "lore.yaml"
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if isinstance(data, dict):
        history = data.get("history")
        if isinstance(history, str) and history.strip():
            return history
    return None


def _temporal_of(legend: Legend) -> str | None:
    """The verbatim temporal value: ``era`` if non-empty, else ``period``."""
    era = (legend.era or "").strip()
    if era:
        return era
    period = (legend.period or "").strip()
    return period or None


def _year_key(temporal: str) -> int | None:
    """Parse a bare signed-integer year, or ``None`` if not a clean year."""
    if _YEAR_RE.match(temporal.strip()):
        return int(temporal.strip())
    return None
