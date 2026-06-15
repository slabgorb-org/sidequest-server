"""World-authored NPC model.

Lives in ``worlds/{slug}/npcs.yaml``. Instantiated as runtime ``Npc`` at
world materialization (fresh sessions only) and pre-loaded into the
registry — authored NPCs are 'present from session start.' Distinct
from ``ScenarioNpc`` (mystery-pack-specialized — keeps its own subset).

Voice mannerisms / distinctive verbal tics: write them as
``history_seeds`` prose. Narrator extracts and uses them. Names are
produced via the namegen tool (``python -m sidequest.cli.namegen``),
NEVER invented at design or authoring time.

See ``docs/relationship-systems.md`` for the full story on how
``initial_disposition`` interacts with ADR-020.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AuthoredNpc(BaseModel):
    model_config = {"extra": "forbid"}

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    pronouns: str = ""
    role: str = ""
    ocean: dict[str, float] | None = None
    appearance: str = ""
    age: str = ""
    distinguishing_features: list[str] = Field(default_factory=list)
    history_seeds: list[str] = Field(default_factory=list)
    initial_disposition: int = Field(default=0, ge=-100, le=100)
    location_tags: list[str] = Field(default_factory=list)
    """Lowercase location/biome substrings anchoring this NPC's placement.

    Optional. When set, the Monster Manual surfaces this NPC as "nearby (not yet
    met)" ONLY where one of these tags matches the current location (substring,
    case-insensitive, either direction). When empty, the NPC is *unplaced* and
    eligible everywhere (legacy behavior). Carried through to
    ``ManualNpc.location_tags`` at seed time — see
    ``sidequest.server.dispatch.pregen.seed_manual``. Fixes the wry_whimsy/oz
    bug where authored companions never surfaced at the right spot because
    placement was ignored until an NPC had already been narrated.
    """
