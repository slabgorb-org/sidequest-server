"""Tests for StreamFenceParser."""

from __future__ import annotations

import pytest

from sidequest.agents.stream_fence import (
    FenceParseResult,  # noqa: F401 — imported to verify public API export
    StreamFenceParser,
)


def _collector():
    """Returns (callback, recorded_chunks_list)."""
    chunks: list[str] = []

    async def cb(chunk: str) -> None:
        chunks.append(chunk)

    return cb, chunks


@pytest.mark.asyncio
async def test_prose_only_no_fence():
    cb, chunks = _collector()
    parser = StreamFenceParser(on_prose_delta=cb)
    await parser.feed("Hello, world. ")
    await parser.feed("This is all prose.")
    result = await parser.finalize()

    assert result.status == "no_fence"
    assert result.game_patch_json is None
    assert "".join(chunks) == "Hello, world. This is all prose."
    assert result.prose == "Hello, world. This is all prose."


@pytest.mark.asyncio
async def test_clean_split():
    cb, chunks = _collector()
    parser = StreamFenceParser(on_prose_delta=cb)
    full = (
        "**The Collapsed Overpass**\n\n"
        "Rust dust drifts down.\n"
        "\n```game_patch\n"
        '{"items_lost": [{"name": "key"}]}\n'
        "```\n"
    )
    await parser.feed(full)
    result = await parser.finalize()

    assert result.status == "complete"
    assert "Rust dust drifts down" in "".join(chunks)
    assert "game_patch" not in "".join(chunks)
    assert result.game_patch_json is not None
    assert "items_lost" in result.game_patch_json


@pytest.mark.asyncio
async def test_fence_in_prose_passthrough_python_block():
    """Triple-backticks with non-game_patch label must pass through."""
    cb, chunks = _collector()
    parser = StreamFenceParser(on_prose_delta=cb)
    full = (
        "She reads the terminal:\n"
        "\n```python\n"
        "if access_denied:\n"
        "    raise Exception\n"
        "```\n"
        "She turns away.\n"
    )
    await parser.feed(full)
    result = await parser.finalize()

    assert result.status == "no_fence"
    full_prose = "".join(chunks)
    assert "```python" in full_prose
    assert "raise Exception" in full_prose
    assert "She turns away" in full_prose


@pytest.mark.asyncio
async def test_unclosed_fence():
    cb, chunks = _collector()
    parser = StreamFenceParser(on_prose_delta=cb)
    await parser.feed("prose\n\n```game_patch\n")
    await parser.feed('{"items_lost": [')
    # Stream ends before close fence
    result = await parser.finalize()

    assert result.status == "unclosed_fence"
    assert result.game_patch_json == '{"items_lost": ['
    assert "".join(chunks) == "prose\n"


@pytest.mark.asyncio
async def test_truncated_at_open_fence_label():
    """If stream ends mid-fence-label, partial-fence bytes must NOT
    be emitted as prose during streaming. They flush at finalize as no_fence content."""
    cb, chunks = _collector()
    parser = StreamFenceParser(on_prose_delta=cb)
    await parser.feed("prose ending\n\n``game_pa")
    result = await parser.finalize()

    assert result.status == "no_fence"
    assert "prose ending" in "".join(chunks)
    # The partial-fence bytes flush at finalize
    assert "``game_pa" in "".join(chunks)


@pytest.mark.asyncio
async def test_chunk_at_every_byte_boundary():
    """Splitting input at every byte boundary must yield identical result."""
    full = 'prose A.\n\n```game_patch\n{"items_lost": [{"name": "key"}]}\n```\n'

    # Reference run: feed all at once
    cb_ref, chunks_ref = _collector()
    p_ref = StreamFenceParser(on_prose_delta=cb_ref)
    await p_ref.feed(full)
    ref = await p_ref.finalize()

    # Boundary runs: split at every position
    for split in range(1, len(full)):
        cb_x, chunks_x = _collector()
        p_x = StreamFenceParser(on_prose_delta=cb_x)
        await p_x.feed(full[:split])
        await p_x.feed(full[split:])
        out = await p_x.finalize()
        assert out.status == ref.status, f"status mismatch at split={split}"
        assert out.prose == ref.prose, f"prose mismatch at split={split}"
        assert out.game_patch_json == ref.game_patch_json, f"json mismatch at split={split}"


@pytest.mark.asyncio
async def test_pretty_printed_json_round_trips():
    cb, _ = _collector()
    parser = StreamFenceParser(on_prose_delta=cb)
    pretty = '\n```game_patch\n{\n  "items_lost": [\n    {"name": "key"}\n  ]\n}\n```\n'
    await parser.feed("prose" + pretty)
    result = await parser.finalize()
    assert result.status == "complete"
    assert "items_lost" in result.game_patch_json
    assert "key" in result.game_patch_json


@pytest.mark.asyncio
async def test_crlf_line_endings():
    cb, chunks = _collector()
    parser = StreamFenceParser(on_prose_delta=cb)
    full = 'prose\r\n\r\n```game_patch\r\n{"a":1}\r\n```\r\n'
    await parser.feed(full)
    result = await parser.finalize()
    assert result.status == "complete"
    assert result.game_patch_json is not None and '"a":1' in result.game_patch_json


@pytest.mark.asyncio
async def test_trailing_garbage_after_close():
    cb, chunks = _collector()
    parser = StreamFenceParser(on_prose_delta=cb)
    full = 'prose\n\n```game_patch\n{"a":1}\n```\nGARBAGE AFTER\n'
    await parser.feed(full)
    result = await parser.finalize()
    assert result.status == "trailing_garbage"
    assert result.game_patch_json is not None and '"a":1' in result.game_patch_json
    # Garbage discarded, not appended to JSON
    assert "GARBAGE" not in (result.game_patch_json or "")


# ---------------------------------------------------------------------------
# on_fence_detected callback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_fence_detected_callback_fires_with_prose_bytes():
    cb_prose, _ = _collector()
    fence_calls: list[int] = []

    async def on_fence(prose_bytes: int) -> None:
        fence_calls.append(prose_bytes)

    parser = StreamFenceParser(on_prose_delta=cb_prose, on_fence_detected=on_fence)
    full = "Hello world.\n\n```game_patch\n{}\n```\n"
    await parser.feed(full)
    await parser.finalize()

    assert len(fence_calls) == 1
    # prose_total at transition is the prose flushed before the fence open-pattern.
    # The open fence is "\n```game_patch\n" so the prefix includes "Hello world.\n"
    # (the leading \n of the double-blank-line belongs to the fence match itself,
    # but the first \n before it is part of prose).
    assert fence_calls[0] == len("Hello world.\n")


@pytest.mark.asyncio
async def test_on_fence_detected_does_not_fire_when_no_fence():
    cb_prose, _ = _collector()
    fence_calls: list[int] = []

    async def on_fence(prose_bytes: int) -> None:
        fence_calls.append(prose_bytes)

    parser = StreamFenceParser(on_prose_delta=cb_prose, on_fence_detected=on_fence)
    await parser.feed("just prose, no fence")
    await parser.finalize()

    assert len(fence_calls) == 0
