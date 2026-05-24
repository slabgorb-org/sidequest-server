"""UI theme types from theme.yaml.

Port of sidequest-genre/src/models/theme.rs.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Dinkus(BaseModel):
    """Section break (dinkus) glyphs."""

    model_config = {"extra": "forbid"}

    enabled: bool
    cooldown: int
    default_weight: str
    glyph: dict[str, str] = Field(default_factory=dict)


class SessionOpener(BaseModel):
    """Session opener configuration."""

    model_config = {"extra": "forbid"}

    enabled: bool


class GenreTheme(BaseModel):
    """UI theme colors and typography."""

    model_config = {"extra": "forbid"}

    primary: str
    secondary: str
    accent: str
    background: str
    surface: str
    text: str
    border_style: str
    web_font_family: str
    display_font_family: str
    # Structural archetype consumed by reference-page chrome (Story 63-4).
    # Values map to bundle [data-archetype="..."] CSS rules: "parchment"
    # (cosy/era), "rugged" (action/violence), "terminal" (sci-fi/dystopia).
    archetype: str
    dinkus: Dinkus
    session_opener: SessionOpener
