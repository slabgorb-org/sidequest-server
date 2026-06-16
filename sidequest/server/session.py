"""Per-slug Session aggregate — strangler-fig over the post-port server tier.

Owned by SessionRoom; constructed when the room's snapshot binds.
Reads/writes session state through GameSnapshot (the persistent boundary).

Today this class owns only the orbital clock and scene-end coordination.
Future migrations move more behavior inward one method at a time.

Per spec docs/superpowers/specs/2026-05-01-session-aggregate-design.md.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

from sidequest.orbital.beats import StoryBeat, StoryBeatKind, advance_clock_via_beat
from sidequest.orbital.clock import Clock
from sidequest.orbital.render import Scope
from sidequest.server.status_clear import clear_scratch_on_scene_end

if TYPE_CHECKING:
    from sidequest.game.session import GameSnapshot
    from sidequest.orbital.loader import OrbitalContent


RECENT_BODY_MENTIONS_LEN = 4
"""Plot-a-course ring buffer size. Bodies named in the last N turns
get surfaced into <courses> as RECENT_MENTION. Larger = more forgiving
across digressions; smaller = tighter focus on the current scene."""


class Session:
    """Per-slug behavior aggregate.

    Constructed by ``SessionRoom.bind_world`` (and any future re-bind
    paths) over the canonical ``GameSnapshot``. The snapshot is the
    persistence boundary; ``Session`` is a thin behavior layer over it.
    """

    def __init__(
        self,
        snapshot: GameSnapshot,
        *,
        orbital_content: OrbitalContent | None = None,
    ) -> None:
        self._snapshot = snapshot
        self._orbital_content = orbital_content
        # Orbital scope is transient session UI state — defaults to system
        # root on each connect rather than persisting across reconnects.
        self._orbital_scope: Scope | None = None
        self._recent_body_mentions: deque[str] = deque(maxlen=RECENT_BODY_MENTIONS_LEN)

    @property
    def clock(self) -> Clock:
        """Read-only Clock view over ``snapshot.clock_t_hours``.

        Mutations on the returned Clock do NOT persist. To advance the
        clock, call ``advance_via_beat`` (which validates the beat,
        emits the OTEL span, and writes back to the snapshot).
        """
        return Clock(t_hours=self._snapshot.clock_t_hours)

    def advance_via_beat(self, beat: StoryBeat) -> float:
        """Advance the clock per the beat. Persists to snapshot. Emits span."""
        local = Clock(t_hours=self._snapshot.clock_t_hours)
        duration = advance_clock_via_beat(local, beat)
        self._snapshot.clock_t_hours = local.t_hours
        return duration

    def end_scene(self, reason: str, *, turn: int) -> None:
        """Scene-end signal: scratch sweep first, then ENCOUNTER beat.

        Called by encounter-resolution sites (narrator beat resolution,
        dice resolution, yielded). The location_change site stays on
        ``clear_scratch_on_scene_end`` directly — not a scene end
        semantically.
        """
        clear_scratch_on_scene_end(self._snapshot, reason=reason, turn=turn)
        self.advance_via_beat(StoryBeat(kind=StoryBeatKind.ENCOUNTER, trigger=f"scene-{reason}"))

    # ------------------------------------------------------------------
    # Orbital map (Task 15) — content + scope state for the chart UI.
    # ------------------------------------------------------------------

    @property
    def orbital_content(self) -> OrbitalContent | None:
        """Loaded ``orbits.yaml`` + ``chart.yaml`` for the bound world.

        ``None`` for worlds without an orbital tier (caverns_and_claudes,
        tea_and_murder, etc.). Set once at room bind time; never mutated.
        """
        return self._orbital_content

    @property
    def orbital_scope(self) -> Scope:
        """Current chart scope — defaults to system root on first read."""
        return self._orbital_scope or Scope.system_root()

    @orbital_scope.setter
    def orbital_scope(self, scope: Scope) -> None:
        self._orbital_scope = scope

    @property
    def recent_body_mentions(self) -> deque[str]:
        """Read-only-ish view of the recent body-mention buffer.

        Returns the actual deque (not a copy); callers should not
        mutate it. Iterate or list() it for a snapshot.
        """
        return self._recent_body_mentions

    def note_body_mentioned(self, body_id: str) -> None:
        """Record a body name as mentioned this turn.

        Dedupe-and-refresh: if the body is already in the buffer,
        remove and re-append so it sits at the most-recent end and
        survives subsequent evictions. This keeps a body the player
        keeps referencing in scope across many turns.
        """
        import contextlib

        if body_id in self._recent_body_mentions:
            with contextlib.suppress(ValueError):
                self._recent_body_mentions.remove(body_id)
        self._recent_body_mentions.append(body_id)

    @property
    def party_body_id(self) -> str | None:
        """Party's orbital body id (from ``orbits.yaml``), or ``None``."""
        return self._snapshot.party_body_id
