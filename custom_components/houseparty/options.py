"""Options <-> engine config. No Home Assistant imports.

Options are stored flat (what HA's options forms produce):
  <mode>_enabled / _exit_delay / _entry_delay / _trigger_time
  <mode>_sensors, always_on_sensors
  immediate_sensors, allow_open_sensors, no_exit_delay_sensors,
  trigger_unavailable_sensors
  code_arm_required, disarm_after_trigger
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from .engine import AreaConfig, Mode, ModeConfig, SensorConfig

MAX_DELAY_S = 900
MAX_TRIGGER_S = 3600
MAX_SENSORS = 128
ROLE_KEYS = ("immediate_sensors", "allow_open_sensors", "no_exit_delay_sensors", "trigger_unavailable_sensors")
_ENTITY_RE = re.compile(r"^binary_sensor\.[a-z0-9_]+$")

DEFAULT_OPTIONS: dict = {
    "code_arm_required": True,
    "disarm_after_trigger": False,
    "always_on_sensors": [],
    **{k: [] for k in ROLE_KEYS},
    **{
        f"{m.value}_{k}": v
        for m, (enabled, exit_d, entry_d) in {
            Mode.AWAY: (True, 60, 30),
            Mode.HOME: (True, 0, 30),
            Mode.NIGHT: (True, 0, 30),
            Mode.VACATION: (False, 60, 30),
            Mode.CUSTOM: (False, 60, 30),
        }.items()
        for k, v in (("enabled", enabled), ("exit_delay", exit_d), ("entry_delay", entry_d),
                     ("trigger_time", 300), ("sensors", []))
    },
}


def _int(v: object, default: int, hi: int) -> int:
    try:
        return max(0, min(int(v), hi))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _ids(v: object) -> list[str]:
    if not isinstance(v, list):
        return []
    return [x for x in v if isinstance(x, str) and _ENTITY_RE.match(x)][:MAX_SENSORS]


def area_config(options: Mapping[str, object]) -> AreaConfig:
    o = {**DEFAULT_OPTIONS, **options}
    modes = {
        m: ModeConfig(
            enabled=bool(o[f"{m.value}_enabled"]),
            exit_delay=_int(o[f"{m.value}_exit_delay"], 60, MAX_DELAY_S),
            entry_delay=_int(o[f"{m.value}_entry_delay"], 30, MAX_DELAY_S),
            trigger_time=_int(o[f"{m.value}_trigger_time"], 300, MAX_TRIGGER_S),
        )
        for m in Mode
    }
    in_mode = {m: set(_ids(o[f"{m.value}_sensors"])) for m in Mode}
    always_on = set(_ids(o["always_on_sensors"]))
    roles = {k: set(_ids(o[k])) for k in ROLE_KEYS}
    every = sorted(set().union(*in_mode.values(), always_on))[:MAX_SENSORS]
    sensors = tuple(
        SensorConfig(
            entity_id=e,
            modes=frozenset(m for m in Mode if e in in_mode[m]),
            immediate=e in roles["immediate_sensors"],
            allow_open=e in roles["allow_open_sensors"],
            always_on=e in always_on,
            use_exit_delay=e not in roles["no_exit_delay_sensors"],
            trigger_unavailable=e in roles["trigger_unavailable_sensors"],
        )
        for e in every
    )
    return AreaConfig(modes=modes, sensors=sensors,
                      disarm_after_trigger=bool(o["disarm_after_trigger"]))


def all_sensor_ids(options: Mapping[str, object]) -> list[str]:
    return [s.entity_id for s in area_config(options).sensors]
