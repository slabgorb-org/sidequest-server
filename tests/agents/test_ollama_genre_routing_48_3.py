"""Failing tests for story 48-3 substep (e): genre-tagged Ollama routing.

RED phase (TEA / Radar O'Reilly). Substep (e) of 48-3 is:

    "Update sidequest-server's OllamaClient model_map so genre-tagged
     requests route to the new adapter-fused model."

These tests fail until Dev adds, to ``sidequest/agents/ollama_client.py``:

  - ``genre_model_map(genres, *, base=None) -> dict[str, str]``
      Builds a model_map that merges the default hint map with one
      ``"genre:<g>" -> "sidequest-narrator-<g>:latest"`` entry per genre.
      A blank genre is rejected loudly (input validation).

CI-safe: every test drives the **real** ``OllamaClient``; only the HTTP
boundary is faked via the existing ``http_fn`` injection point. We do NOT
monkeypatch the client itself -- that 48-4-review anti-pattern leaves the
production resolution path untested. The live model is M3-Ultra only; the
fake HTTP keeps this suite hermetic.

Authoritative spec source: ``.session/48-3-session.md`` Technical Context
substep (e) + project 'No Silent Fallbacks' (an unmapped genre must raise
``UnknownModel``, never silently fall back to the generic narrator).

Rule-enforcement (.pennyfarthing/gates/lang-review/python.md):
  #1  silent-exceptions -- test_real_client_unknown_genre_fails_loudly
  #2  mutable-defaults  -- test_rule2_genre_model_map_no_mutable_default
  #3  type-annotations  -- test_rule3_genre_model_map_annotated
  #11 input-validation  -- test_genre_model_map_blank_genre_rejected
"""

from __future__ import annotations

import inspect
import json
from typing import Any

import pytest

from sidequest.agents.ollama_client import (
    DEFAULT_MODEL_MAP,
    OllamaClient,
    UnknownModel,
    genre_model_map,
)


class _FakeHttpResponse:
    """Context-manager HTTP response stub (status + read())."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.status = 200

    def __enter__(self) -> _FakeHttpResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._payload


def _capturing_http(captured: list[dict[str, Any]]):
    """Return an http_fn that records each request's decoded JSON body."""

    def _http(req: Any) -> _FakeHttpResponse:
        captured.append(json.loads(req.data.decode("utf-8")))
        return _FakeHttpResponse(json.dumps({"response": "ok"}).encode())

    return _http


# ----------------------------------------------------------------------- #
# genre_model_map builder
# ----------------------------------------------------------------------- #


def test_genre_model_map_builds_genre_keys() -> None:
    mapping = genre_model_map(["caverns_and_claudes"])
    assert mapping["genre:caverns_and_claudes"] == "sidequest-narrator-caverns_and_claudes:latest"


def test_genre_model_map_preserves_default_hints() -> None:
    """Regression: adding genre routing must not drop the existing
    sonnet/haiku/opus hints (other backends still rely on them)."""
    mapping = genre_model_map(["space_opera"])
    for hint, model in DEFAULT_MODEL_MAP.items():
        assert mapping[hint] == model


@pytest.mark.parametrize("bad", ["", "   "])
def test_genre_model_map_blank_genre_rejected(bad: str) -> None:
    """Rule #11: a blank genre at this boundary is operator nonsense."""
    with pytest.raises(ValueError):
        genre_model_map([bad])


# ----------------------------------------------------------------------- #
# Real OllamaClient resolution (HTTP boundary faked, client is real)
# ----------------------------------------------------------------------- #


async def test_real_client_routes_genre_tag_to_specialized_model() -> None:
    captured: list[dict[str, Any]] = []
    client = OllamaClient(
        model_map=genre_model_map(["caverns_and_claudes"]),
        http_fn=_capturing_http(captured),
    )
    await client.send_with_model("hello", "genre:caverns_and_claudes")
    assert len(captured) == 1
    assert captured[0]["model"] == "sidequest-narrator-caverns_and_claudes:latest"


async def test_real_client_unknown_genre_fails_loudly() -> None:
    """Project 'No Silent Fallbacks' + rule #1: an unmapped genre must
    raise UnknownModel, NOT silently fall back to the generic narrator
    model (a silent fallback would mask a misconfigured genre route).
    """
    captured: list[dict[str, Any]] = []
    client = OllamaClient(
        model_map=genre_model_map(["caverns_and_claudes"]),
        http_fn=_capturing_http(captured),
    )
    with pytest.raises(UnknownModel):
        await client.send_with_model("hello", "genre:not_a_real_genre")
    assert captured == []


async def test_real_client_existing_hints_still_resolve() -> None:
    """Regression: a genre-aware map must still resolve the legacy
    'sonnet' hint to its configured model through the real client."""
    captured: list[dict[str, Any]] = []
    client = OllamaClient(
        model_map=genre_model_map(["space_opera"]),
        http_fn=_capturing_http(captured),
    )
    await client.send_with_model("hello", "sonnet")
    assert captured[0]["model"] == DEFAULT_MODEL_MAP["sonnet"]


# ----------------------------------------------------------------------- #
# Rule-enforcement (signature scans)
# ----------------------------------------------------------------------- #


def test_rule2_genre_model_map_no_mutable_default() -> None:
    sig = inspect.signature(genre_model_map)
    for name, param in sig.parameters.items():
        assert not isinstance(param.default, (list, dict, set)), (
            f"genre_model_map param {name!r} has mutable default"
        )


def test_rule3_genre_model_map_annotated() -> None:
    sig = inspect.signature(genre_model_map)
    assert sig.return_annotation is not inspect.Signature.empty
    for name, param in sig.parameters.items():
        assert param.annotation is not inspect.Parameter.empty, (
            f"genre_model_map param {name!r} missing annotation"
        )


# ----------------------------------------------------------------------- #
# Wiring (CLAUDE.md): the genre map builder ships with the client it
# configures and is importable from the production agents package.
# ----------------------------------------------------------------------- #


def test_wiring_genre_map_importable_with_client() -> None:
    from sidequest.agents import ollama_client as mod

    assert callable(mod.genre_model_map)
    assert mod.DEFAULT_MODEL_MAP, "default hint map must remain intact"
