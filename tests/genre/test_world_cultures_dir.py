"""Verify cultures and legends can be loaded from directories."""

from pathlib import Path
import yaml

from sidequest.genre.models.culture import Culture
from sidequest.genre.models.legends import Legend


def _write_yaml(path: Path, data: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True))


def test_load_cultures_from_directory(tmp_path: Path):
    cultures_dir = tmp_path / "cultures"
    cultures_dir.mkdir()

    _write_yaml(cultures_dir / "highland_scots.yaml", {
        "name": "Highland Scots",
        "summary": "Hardy highland culture",
        "description": "Hardy folk of the northern hills",
    })
    _write_yaml(cultures_dir / "english_gentry.yaml", {
        "name": "English Gentry",
        "summary": "The landed aristocracy",
        "description": "The landed class",
    })

    cultures: list[Culture] = []
    for culture_file in sorted(cultures_dir.glob("*.yaml")):
        with culture_file.open() as f:
            raw = yaml.safe_load(f)
        if isinstance(raw, dict):
            cultures.append(Culture.model_validate(raw))

    assert len(cultures) == 2
    assert cultures[0].name == "English Gentry"
    assert cultures[1].name == "Highland Scots"


def test_load_legends_from_directory(tmp_path: Path):
    legends_dir = tmp_path / "legends"
    legends_dir.mkdir()

    _write_yaml(legends_dir / "the_silver_blade.yaml", {
        "name": "The Silver Blade",
        "summary": "A legendary weapon forged in moonlight",
    })
    _write_yaml(legends_dir / "the_lost_city.yaml", {
        "name": "The Lost City",
        "summary": "An ancient city swallowed by the earth",
    })

    legends: list[Legend] = []
    for legend_file in sorted(legends_dir.glob("*.yaml")):
        if legend_file.name == "_meta.yaml":
            continue
        with legend_file.open() as f:
            raw = yaml.safe_load(f)
        if isinstance(raw, dict):
            legends.append(Legend.model_validate(raw))

    assert len(legends) == 2
    assert legends[0].name == "The Lost City"
    assert legends[1].name == "The Silver Blade"
