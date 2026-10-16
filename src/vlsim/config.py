"""Typed loader for the versioned YAML under config/."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Region = Literal["north", "central", "south"]
DepotType = Literal["hub", "depot"]
LaneKind = Literal["main", "direct", "feeder"]
VehicleType = Literal["motorbike", "van", "truck"]

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DistanceConfig(_Frozen):
    circuity_factor: float = Field(gt=1.0)


class CheckpointConfig(_Frozen):
    interval_sim_minutes: int = Field(gt=0)


class ScalePreset(_Frozen):
    name: str
    shipments_per_day: int = Field(gt=0)
    vehicles: dict[VehicleType, int]
    drivers: int = Field(gt=0)
    merchants: int = Field(gt=0)
    customers: int = Field(gt=0)

    @property
    def vehicle_count(self) -> int:
        return sum(self.vehicles.values())


class Depot(_Frozen):
    code: str = Field(pattern=r"^[A-Z]{3}$")
    name: str
    type: DepotType
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    region: Region
    dock_doors: int = Field(gt=0)


class Lane(_Frozen):
    a: str
    b: str
    kind: LaneKind


class Network(_Frozen):
    depots: tuple[Depot, ...]
    lanes: tuple[Lane, ...]

    @model_validator(mode="after")
    def _check_refs(self) -> Network:
        codes = [d.code for d in self.depots]
        if len(codes) != len(set(codes)):
            raise ValueError("duplicate depot code")
        known = set(codes)
        hubs = {d.code for d in self.depots if d.type == "hub"}
        seen: set[frozenset[str]] = set()
        for lane in self.lanes:
            if lane.a not in known or lane.b not in known:
                raise ValueError(f"lane {lane.a}-{lane.b} references unknown depot")
            if lane.a == lane.b:
                raise ValueError(f"lane {lane.a}-{lane.b} is a self-loop")
            pair = frozenset((lane.a, lane.b))
            if pair in seen:
                raise ValueError(f"duplicate lane {lane.a}-{lane.b}")
            seen.add(pair)
            ends_at_hub = {lane.a, lane.b} <= hubs
            if (lane.kind == "feeder") == ends_at_hub:
                raise ValueError(f"lane {lane.a}-{lane.b}: feeders join a depot to a hub")
        return self

    def depot(self, code: str) -> Depot:
        for d in self.depots:
            if d.code == code:
                return d
        raise KeyError(code)


class BaseConfig(_Frozen):
    config_version: int
    seed: int = Field(ge=0)
    timezone_offset: str = Field(pattern=r"^[+-]\d{2}:\d{2}$")
    start_time: datetime
    scale_preset: str
    distance: DistanceConfig
    checkpoint: CheckpointConfig

    @field_validator("start_time")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("start_time must carry an explicit UTC offset")
        return v

    @model_validator(mode="after")
    def _offset_matches(self) -> BaseConfig:
        sign = 1 if self.timezone_offset[0] == "+" else -1
        hh, mm = self.timezone_offset[1:].split(":")
        expected = timedelta(hours=int(hh), minutes=int(mm)) * sign
        if self.start_time.utcoffset() != expected:
            raise ValueError("start_time offset must equal timezone_offset")
        return self


class SimConfig(_Frozen):
    base: BaseConfig
    scale: ScalePreset
    network: Network


def _read_yaml(path: Path) -> object:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_config(config_dir: Path = DEFAULT_CONFIG_DIR, preset: str | None = None) -> SimConfig:
    """Load base, scale preset and network. `preset` overrides base.scale_preset."""
    base = BaseConfig.model_validate(_read_yaml(config_dir / "base.yaml"))
    name = preset or base.scale_preset
    scale_path = config_dir / "scale" / f"{name}.yaml"
    if not scale_path.is_file():
        raise FileNotFoundError(f"unknown scale preset {name!r} ({scale_path})")
    scale = ScalePreset.model_validate(_read_yaml(scale_path))
    if scale.name != name:
        raise ValueError(f"preset file {scale_path.name} declares name {scale.name!r}")
    network = Network.model_validate(_read_yaml(config_dir / "network.yaml"))
    return SimConfig(base=base, scale=scale, network=network)
