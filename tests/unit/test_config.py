from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from vlsim.config import DEFAULT_CONFIG_DIR, load_config


@pytest.fixture
def cfg_dir(tmp_path: Path) -> Path:
    dst = tmp_path / "config"
    shutil.copytree(DEFAULT_CONFIG_DIR, dst)
    return dst


def _edit(path: Path, fn: object) -> None:
    data = yaml.safe_load(path.read_text())
    fn(data)  # type: ignore[operator]
    path.write_text(yaml.safe_dump(data))


@pytest.mark.parametrize(("preset", "vehicles", "per_day"), [("s", 50, 1200), ("m", 200, 5000)])
def test_presets_match_spec(preset: str, vehicles: int, per_day: int) -> None:
    cfg = load_config(preset=preset)
    assert cfg.scale.vehicle_count == vehicles
    assert cfg.scale.shipments_per_day == per_day


def test_network_matches_spec() -> None:
    net = load_config().network
    assert [d.code for d in net.depots] == [
        "HAN",
        "HPH",
        "VII",
        "DAD",
        "HUI",
        "NHA",
        "SGN",
        "BHA",
        "VCA",
    ]
    assert {d.code for d in net.depots if d.type == "hub"} == {"HAN", "DAD", "SGN"}
    regions = {d.code: d.region for d in net.depots}
    assert [c for c in regions if regions[c] == "central"] == ["DAD", "HUI", "NHA"]


def test_start_time_has_utc7_offset() -> None:
    base = load_config().base
    assert base.start_time.utcoffset() is not None
    assert base.start_time.isoformat().endswith("+07:00")


def test_unknown_preset(cfg_dir: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_config(cfg_dir, "xl")


def test_naive_start_time_rejected(cfg_dir: Path) -> None:
    _edit(cfg_dir / "base.yaml", lambda d: d.update(start_time="2026-10-01T00:00:00"))
    with pytest.raises(ValidationError, match="explicit UTC offset"):
        load_config(cfg_dir)


def test_lane_to_unknown_depot_rejected(cfg_dir: Path) -> None:
    _edit(
        cfg_dir / "network.yaml",
        lambda d: d["lanes"].append({"a": "HAN", "b": "XXX", "kind": "feeder"}),
    )
    with pytest.raises(ValidationError, match="unknown depot"):
        load_config(cfg_dir)


def test_feeder_between_hubs_rejected(cfg_dir: Path) -> None:
    def mutate(d: dict[str, list[dict[str, str]]]) -> None:
        d["lanes"][0]["kind"] = "feeder"

    _edit(cfg_dir / "network.yaml", mutate)
    with pytest.raises(ValidationError, match="feeders join"):
        load_config(cfg_dir)


def test_unknown_key_rejected(cfg_dir: Path) -> None:
    _edit(cfg_dir / "scale" / "s.yaml", lambda d: d.update(typo_field=1))
    with pytest.raises(ValidationError):
        load_config(cfg_dir)
