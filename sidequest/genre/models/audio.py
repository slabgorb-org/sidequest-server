"""Audio configuration types from audio.yaml and voice_presets.yaml.

Port of sidequest-genre/src/models/audio.rs.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

# ADR-033 Pillar 3: alias chains are bounded; a chain that does not terminate
# in a real mood_tracks key within this many hops is a broken pack.
MAX_ALIAS_HOPS = 5


class MoodTrack(BaseModel):
    """A single music track."""

    model_config = {"extra": "forbid"}

    path: str
    title: str
    bpm: int
    energy: float = 0.5


class AudioEffect(BaseModel):
    """An audio effect in a processing chain."""

    model_config = {"extra": "forbid", "populate_by_name": True}

    effect_type: str = Field(alias="type", serialization_alias="type")
    params: dict[str, float] = Field(default_factory=dict)


class CreatureVoicePreset(BaseModel):
    """Voice preset for a creature type."""

    model_config = {"extra": "forbid"}

    creature_type: str
    description: str
    pitch: float
    rate: float
    effects: list[AudioEffect] = Field(default_factory=list)


class MixerConfig(BaseModel):
    """Mixer volume configuration."""

    model_config = {"extra": "forbid"}

    music_volume: float
    sfx_volume: float
    voice_volume: float = 1.0
    crossfade_default_ms: int


class AudioVariation(BaseModel):
    """A single variation within an audio theme."""

    model_config = {"extra": "forbid", "populate_by_name": True}

    variation_type: str = Field(alias="type", serialization_alias="type")
    path: str


class AudioTheme(BaseModel):
    """A themed music collection with variations."""

    model_config = {"extra": "forbid"}

    name: str
    mood: str
    base_prompt: str
    variations: list[AudioVariation] = Field(default_factory=list)


class AudioAiGeneration(BaseModel):
    """AI music generation configuration."""

    model_config = {"extra": "forbid"}

    enabled: bool = False
    model: str | None = None
    max_generation_time_s: int | None = None
    cache_generated: bool | None = None


class FactionTriggers(BaseModel):
    """Trigger conditions for a faction theme."""

    model_config = {"extra": "forbid"}

    location: bool = False
    npc_present: bool = False
    reputation_threshold: int | None = None


class FactionThemeDef(BaseModel):
    """A faction-specific music theme with trigger conditions."""

    model_config = {"extra": "forbid"}

    faction_id: str
    track: MoodTrack
    triggers: FactionTriggers


class AudioConfig(BaseModel):
    """Audio configuration for music, SFX, and voice."""

    model_config = {"extra": "forbid"}

    mood_tracks: dict[str, list[MoodTrack]] = Field(default_factory=dict)
    sfx_library: dict[str, list[str]] = Field(default_factory=dict)
    creature_voice_presets: dict[str, CreatureVoicePreset] = Field(default_factory=dict)
    mixer: MixerConfig
    themes: list[AudioTheme] = Field(default_factory=list)
    ai_generation: AudioAiGeneration | None = None
    mood_keywords: dict[str, list[str]] = Field(default_factory=dict)
    mixer_defaults: MixerConfig | None = None
    mood_aliases: dict[str, str] = Field(default_factory=dict)
    faction_themes: list[FactionThemeDef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_mood_aliases(self) -> AudioConfig:
        """ADR-033 Pillar 3 AC-2: every declared ``mood_aliases`` chain MUST
        terminate in a real ``mood_tracks`` key within ``MAX_ALIAS_HOPS``.

        Fail loud at pack load — no silent substitution or default-filling.
        A broken target, a cycle, or an over-deep chain makes the pack
        unloadable and names the offender. Runtime resolution may therefore
        assume every declared alias is good; the only runtime fallback is an
        *undeclared* unknown mood.
        """
        tracks = self.mood_tracks
        aliases = self.mood_aliases
        for start in aliases:
            seen = {start}
            cur = aliases[start]
            hops = 1
            while True:
                if cur in tracks:
                    break
                if cur in seen:
                    raise ValueError(
                        f"mood_aliases: alias chain starting at {start!r} forms "
                        f"a cycle/loop at {cur!r} (loop_detected) — declared "
                        f"alias chains must terminate in a mood_tracks key"
                    )
                if cur not in aliases:
                    raise ValueError(
                        f"mood_aliases: alias {start!r} -> ... -> {cur!r} does "
                        f"not resolve (broken_chain): {cur!r} is neither a "
                        f"mood_tracks key nor a declared alias. "
                        f"Known mood_tracks: {sorted(tracks)}"
                    )
                hops += 1
                if hops > MAX_ALIAS_HOPS:
                    raise ValueError(
                        f"mood_aliases: alias chain from {start!r} exceeds the "
                        f"{MAX_ALIAS_HOPS}-hop limit (depth_exceeded) before "
                        f"reaching a mood_tracks key"
                    )
                seen.add(cur)
                cur = aliases[cur]
        return self


class TrackVariation(StrEnum):
    """Typed track variation — cinematic score cue categories."""

    full = "full"
    overture = "overture"
    ambient = "ambient"
    sparse = "sparse"
    tension_build = "tension_build"
    resolution = "resolution"


class VoiceConfig(BaseModel):
    """A single TTS voice configuration."""

    model_config = {"extra": "forbid"}

    model: str
    pitch: float
    rate: float
    effects: list[AudioEffect] = Field(default_factory=list)


class VoicePresets(BaseModel):
    """TTS voice preset configuration."""

    model_config = {"extra": "forbid"}

    narrator: VoiceConfig
    characters: dict[str, VoiceConfig] = Field(default_factory=dict)
