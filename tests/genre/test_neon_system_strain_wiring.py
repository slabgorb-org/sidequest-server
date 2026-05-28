"""End-to-end wiring test: neon_dystopia → CWN System Strain (Task 9).

Proves the FULL chain without Postgres:
  real neon YAML → loader → validated cwn.system_strain config
  → chargen seeds pool (max = real Body score)
  → apply_system_strain on that real character mutates pool AND emits OTEL span.

No stubs; no synthetic packs.  Content must be on disk — the test RUNS (not
skips) on this machine.  If load_pack raises, the test surfaces the real error.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.builder import CharacterBuilder
from sidequest.game.ruleset.cwn import CwnRulesetModule
from sidequest.game.ruleset.registry import get_ruleset_module
from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.pack import GenrePack
from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _load_neon() -> GenrePack:
    try:
        pack_path = find_pack_path("neon_dystopia")
    except PackNotFound as exc:
        pytest.skip(str(exc))
    return load_genre_pack(pack_path)


def _make_tracer() -> tuple[InMemorySpanExporter, object]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test.neon_wiring")


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_neon_cwn_config_wiring() -> None:
    """Pack loads with correct cwn.system_strain config values."""
    pack = _load_neon()

    assert pack.rules.ruleset == "cwn"
    cfg = pack.rules.ruleset_config()
    assert cfg is pack.rules.cwn

    ss = cfg.system_strain
    assert ss.max_source == "CONSTITUTION"
    assert ss.rest_recovery_per_night == 1
    assert ss.first_aid_cost == 1


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_neon_chargen_seed_and_engine_otel() -> None:
    """Real neon character built, strain seeded, engine-method mutates + emits OTEL."""
    from sidequest.game.builder import Confirmation  # noqa: F401 — existence check

    pack = _load_neon()

    builder = (
        CharacterBuilder(
            scenes=list(pack.char_creation),  # type: ignore[attr-defined]
            rules=pack.rules,  # type: ignore[attr-defined]
            backstory_tables=pack.backstory_tables,  # type: ignore[attr-defined]
        )
        .with_lobby_name("Nia Vex")
        .with_equipment_tables(pack.equipment_tables)  # type: ignore[attr-defined]
        .with_classes(pack.classes)  # type: ignore[attr-defined]
    )

    # Walk every non-confirmation scene choosing option 0 each time.
    while not builder.is_confirmation():
        scene = builder.current_scene()
        if scene.choices:
            builder.apply_choice(0)
        else:
            builder.apply_auto_advance()

    character = builder.build("Nia Vex")

    body_score = character.stats.get("Body", 0)
    assert character.core.system_strain is not None, (
        "neon_dystopia cwn character must have a SystemStrainPool after build()"
    )
    assert character.core.system_strain.current == 0
    assert character.core.system_strain.permanent == 0
    assert character.core.system_strain.max == max(1, body_score), (
        f"strain max ({character.core.system_strain.max}) must equal Body ({body_score})"
    )

    # --- Engine method on real character ---
    module = get_ruleset_module(pack.rules.ruleset)
    assert isinstance(module, CwnRulesetModule)
    cfg = pack.rules.ruleset_config()

    exporter, tracer = _make_tracer()

    # 1. Permanent add within max: should apply.
    result = module.apply_system_strain(
        core=character.core,
        kind="permanent",
        amount=2,
        source="cyberarm",
        cfg=cfg,
        _tracer=tracer,  # type: ignore[arg-type]
    )
    assert result.applied is True
    assert character.core.system_strain.permanent == 2
    assert character.core.system_strain.current == 2
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    assert spans[0].name == "cwn.system_strain.delta"
    assert attrs["applied"] is True
    assert attrs["new_total"] == 2

    # 2. Temporary add that exceeds max: should be refused.
    over_max_amount = character.core.system_strain.max + 5
    result2 = module.apply_system_strain(
        core=character.core,
        kind="temporary",
        amount=over_max_amount,
        source="overclock",
        cfg=cfg,
        _tracer=tracer,  # type: ignore[arg-type]
    )
    assert result2.applied is False
    assert character.core.system_strain.current == 2  # unchanged from before
    spans2 = exporter.get_finished_spans()
    assert len(spans2) == 2
    attrs2 = dict(spans2[1].attributes or {})
    assert spans2[1].name == "cwn.system_strain.delta"
    assert attrs2["applied"] is False
    assert attrs2["new_total"] == character.core.system_strain.current  # refusal leaves total unchanged (== 2)
