"""HouseParty alarm engine.

Pure logic: no Home Assistant imports, no I/O, no clock. The caller passes in
the time and sensor states, and gets back what happened. That makes every
security decision here deterministic and unit-testable.

Fail-closed rules:
- A sensor whose state is anything but "on"/"off" (unavailable, unknown,
  missing) blocks arming, exactly like an open one.
- A force-arm bypasses only the sensors the person approved. If anything else
  is blocking at that moment, the arm is refused and the new list reported.
- Restoring after a restart never lands in a less-protected state: an
  interrupted entry delay or alarm comes back as triggered.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum

OPEN = "on"
CLOSED = "off"


class Mode(str, Enum):
    AWAY = "away"
    HOME = "home"
    NIGHT = "night"
    VACATION = "vacation"
    CUSTOM = "custom"

    @property
    def armed_state(self) -> State:
        suffix = "custom_bypass" if self is Mode.CUSTOM else self.value
        return State(f"armed_{suffix}")

    @classmethod
    def from_armed_state(cls, state: State) -> Mode | None:
        return next((m for m in cls if m.armed_state is state), None)


class State(str, Enum):
    DISARMED = "disarmed"
    ARMING = "arming"
    ARMED_AWAY = "armed_away"
    ARMED_HOME = "armed_home"
    ARMED_NIGHT = "armed_night"
    ARMED_VACATION = "armed_vacation"
    ARMED_CUSTOM_BYPASS = "armed_custom_bypass"
    PENDING = "pending"
    TRIGGERED = "triggered"

    @property
    def is_armed(self) -> bool:
        return self.value.startswith("armed_")


@dataclass(frozen=True)
class ModeConfig:
    enabled: bool = False
    exit_delay: int = 60
    entry_delay: int = 30
    trigger_time: int = 300  # 0 = stay triggered until disarmed


@dataclass(frozen=True)
class SensorConfig:
    entity_id: str
    modes: frozenset[Mode] = frozenset()
    immediate: bool = False  # trips straight to triggered, no entry delay
    allow_open: bool = False  # may be open when arming (e.g. motion)
    always_on: bool = False  # trips even when disarmed (tamper, smoke, water)
    use_exit_delay: bool = True  # False: tripping during the exit delay triggers
    trigger_unavailable: bool = False  # going unavailable while armed trips it


@dataclass(frozen=True)
class AreaConfig:
    modes: Mapping[Mode, ModeConfig]
    sensors: tuple[SensorConfig, ...] = ()
    disarm_after_trigger: bool = False
    always_on_trigger_time: int = 300

    def __post_init__(self) -> None:
        object.__setattr__(self, "_index", {s.entity_id: s for s in self.sensors})

    def mode(self, mode: Mode) -> ModeConfig:
        return self.modes.get(mode, ModeConfig())

    def sensor(self, entity_id: str) -> SensorConfig | None:
        return self._index.get(entity_id)  # type: ignore[attr-defined]


@dataclass(frozen=True)
class Event:
    """Fired on the HA event bus as `houseparty_<name>`."""

    name: str
    data: Mapping[str, object]


@dataclass
class Outcome:
    ok: bool
    reason: str | None = None  # open_sensors | mode_disabled | invalid_state
    events: list[Event] = field(default_factory=list)


@dataclass
class Snapshot:
    """Everything the entity exposes; enough to restore after a restart."""

    state: State = State.DISARMED
    arm_mode: Mode | None = None
    deadline: float | None = None  # end of the current delay or trigger time
    delay: int | None = None  # length of the current exit/entry delay
    bypassed: frozenset[str] = frozenset()
    open_sensors: dict[str, str] = field(default_factory=dict)  # last refusal
    triggered_by: str | None = None
    changed_by: str | None = None
    last_triggered: float | None = None


def _blocking_state(state: str | None) -> bool:
    return state != CLOSED


class Engine:
    def __init__(self, config: AreaConfig, snapshot: Snapshot | None = None) -> None:
        self.config = config
        self.snap = snapshot or Snapshot()

    # ---- commands -------------------------------------------------------

    def arm(
        self,
        mode: Mode,
        sensor_states: Mapping[str, str | None],
        now: float,
        *,
        actor: str | None,
        force: bool = False,
        approved_bypass: frozenset[str] = frozenset(),
    ) -> Outcome:
        cfg = self.config.mode(mode)
        if not cfg.enabled:
            return Outcome(False, "mode_disabled")
        s = self.snap
        if s.state is mode.armed_state:
            return Outcome(True)  # already there
        if not (s.state is State.DISARMED or s.state.is_armed):
            return Outcome(False, "invalid_state")

        blocking = {
            sc.entity_id: sensor_states.get(sc.entity_id) or "unavailable"
            for sc in self.config.sensors
            if mode in sc.modes
            and not sc.always_on
            and _blocking_state(sensor_states.get(sc.entity_id))
            and not (sensor_states.get(sc.entity_id) == OPEN and sc.allow_open)
        }
        if blocking and (not force or not blocking.keys() <= approved_bypass):
            # Not forced, or forced but something the person didn't approve
            # is blocking now: refuse and report the current full list.
            s.open_sensors = dict(sorted(blocking.items()))
            return Outcome(
                False,
                "open_sensors",
                [Event("failed_to_arm", {"reason": "open_sensors", "mode": mode.value,
                                         "sensors": sorted(blocking)})],
            )

        s.open_sensors = {}
        s.triggered_by = None
        s.bypassed = frozenset(blocking)
        s.arm_mode = mode
        s.changed_by = actor
        if cfg.exit_delay > 0:
            s.state, s.deadline, s.delay = State.ARMING, now + cfg.exit_delay, cfg.exit_delay
        else:
            s.state, s.deadline, s.delay = mode.armed_state, None, None
        return Outcome(True, events=[Event("armed", {
            "mode": mode.value, "by": actor, "bypassed": sorted(s.bypassed)})])

    def disarm(self, now: float, *, actor: str | None) -> Outcome:
        s = self.snap
        if s.state is State.DISARMED:
            return Outcome(True)
        s.state = State.DISARMED
        s.arm_mode = None
        s.deadline = s.delay = None
        s.bypassed = frozenset()
        s.open_sensors = {}
        s.changed_by = actor
        return Outcome(True, events=[Event("disarmed", {"by": actor})])

    # ---- inputs ---------------------------------------------------------

    def sensor_changed(self, entity_id: str, old: str | None, new: str | None, now: float) -> Outcome:
        sc = self.config.sensor(entity_id)
        if sc is None:
            return Outcome(True)
        s = self.snap
        opened = new == OPEN and old != OPEN
        went_dark = new not in (OPEN, CLOSED) and old in (OPEN, CLOSED)
        lost = sc.trigger_unavailable and went_dark
        if went_dark and not lost:
            # A sensor dropping offline (jammed radio, pulled battery) while it
            # matters is always reported, even if it isn't set to trigger.
            watching = sc.always_on or (
                s.arm_mode is not None and s.arm_mode in sc.modes and entity_id not in s.bypassed
                and s.state is not State.DISARMED)
            if watching:
                return Outcome(True, events=[Event("sensor_lost", {"sensor": entity_id})])
        if not (opened or lost):
            return Outcome(True)

        if sc.always_on:
            if s.state is State.TRIGGERED:
                return Outcome(True)
            return self._trigger(entity_id, now)
        if entity_id in s.bypassed or s.arm_mode is None or s.arm_mode not in sc.modes:
            return Outcome(True)

        if s.state is State.ARMING:
            # Leaving through a door is fine; tripping a sensor that isn't on
            # the exit path is not.
            return Outcome(True) if sc.use_exit_delay else self._trigger(entity_id, now)
        if s.state.is_armed:
            entry = self.config.mode(s.arm_mode).entry_delay
            if sc.immediate or entry <= 0:
                return self._trigger(entity_id, now)
            s.state, s.deadline, s.delay = State.PENDING, now + entry, entry
            s.triggered_by = entity_id
            return Outcome(True, events=[Event("entry", {"sensor": entity_id, "delay": entry})])
        if s.state is State.PENDING and sc.immediate:
            return self._trigger(entity_id, now)
        return Outcome(True)

    def tick(self, now: float) -> Outcome:
        """Call when `snap.deadline` passes."""
        s = self.snap
        if s.deadline is None or now < s.deadline:
            return Outcome(True)
        if s.state is State.ARMING and s.arm_mode is not None:
            s.state, s.deadline, s.delay = s.arm_mode.armed_state, None, None
            return Outcome(True)
        if s.state is State.PENDING:
            return self._trigger(s.triggered_by, now)
        if s.state is State.TRIGGERED:
            s.deadline = None
            if self.config.disarm_after_trigger or s.arm_mode is None:
                s.state, s.arm_mode, s.bypassed = State.DISARMED, None, frozenset()
            else:
                # Back to armed. Sensors still open won't re-trip until they
                # close and open again (triggers are edge-based).
                s.state = s.arm_mode.armed_state
            return Outcome(True, events=[Event("trigger_ended", {"state": s.state.value})])
        s.deadline = None
        return Outcome(True)

    # ---- restore --------------------------------------------------------

    @classmethod
    def restored(cls, config: AreaConfig, snap: Snapshot, now: float) -> Engine:
        """Rebuild after a restart. Never restores into a weaker state."""
        engine = cls(config, snap)
        s = engine.snap
        if s.state is State.ARMING and s.arm_mode is not None:
            # Restart the exit delay rather than guess how much was left.
            exit_delay = config.mode(s.arm_mode).exit_delay
            s.deadline, s.delay = now + exit_delay, exit_delay
        elif s.state in (State.PENDING, State.TRIGGERED):
            # Someone may be inside: come back sounding.
            engine._trigger(s.triggered_by, now)
        else:
            s.deadline = s.delay = None
        return engine

    def reconcile(self, sensor_states: Mapping[str, str | None], now: float) -> Outcome:
        """After a restart, treat any sensor that's open *now* as just opened.
        Triggers are edge-based, so a door opened while HA was down would
        otherwise go unnoticed until it closed and opened again."""
        events: list[Event] = []
        if self.snap.state.is_armed or self.snap.state is State.DISARMED:
            for sc in self.config.sensors:
                if sensor_states.get(sc.entity_id) == OPEN:
                    out = self.sensor_changed(sc.entity_id, None, OPEN, now)
                    events += out.events
        return Outcome(True, events=events)

    # ---- internals ------------------------------------------------------

    def _trigger(self, entity_id: str | None, now: float) -> Outcome:
        s = self.snap
        s.state = State.TRIGGERED
        s.triggered_by = entity_id
        s.last_triggered = now
        s.delay = None
        tt = (self.config.mode(s.arm_mode).trigger_time if s.arm_mode
              else self.config.always_on_trigger_time)
        s.deadline = now + tt if tt > 0 else None
        return Outcome(True, events=[Event("triggered", {
            "sensor": entity_id, "mode": s.arm_mode.value if s.arm_mode else None})])
