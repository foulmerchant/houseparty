"""Run: python3 -m unittest discover -s tests (no Home Assistant needed)."""

import importlib.util
import pathlib
import sys
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "houseparty"

# Load engine.py and auth.py without running the package __init__ (which
# imports Home Assistant).
pkg = types.ModuleType("hp")
pkg.__path__ = [str(ROOT)]
sys.modules["hp"] = pkg
for name in ("engine", "auth"):
    spec = importlib.util.spec_from_file_location(f"hp.{name}", ROOT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"hp.{name}"] = mod
    spec.loader.exec_module(mod)

from hp.engine import AreaConfig, Engine, Mode, ModeConfig, SensorConfig, Snapshot, State  # noqa: E402

AWAY = Mode.AWAY


def area(**kw):
    sensors = kw.pop("sensors", (
        SensorConfig("binary_sensor.front", frozenset({AWAY})),
        SensorConfig("binary_sensor.back", frozenset({AWAY})),
        SensorConfig("binary_sensor.hall_motion", frozenset({AWAY}), allow_open=True, use_exit_delay=True),
        SensorConfig("binary_sensor.window", frozenset({AWAY}), use_exit_delay=False),
        SensorConfig("binary_sensor.glass", frozenset({AWAY}), immediate=True),
        SensorConfig("binary_sensor.smoke", always_on=True),
    ))
    return AreaConfig(modes={AWAY: ModeConfig(enabled=True, exit_delay=30, entry_delay=20, trigger_time=60)},
                      sensors=sensors, **kw)


ALL_CLOSED = {s: "off" for s in ("binary_sensor.front", "binary_sensor.back", "binary_sensor.hall_motion",
                                 "binary_sensor.window", "binary_sensor.glass", "binary_sensor.smoke")}


def armed(now=0.0):
    e = Engine(area())
    e.arm(AWAY, ALL_CLOSED, now, actor="Sam")
    e.tick(now + 30)
    return e


class Arming(unittest.TestCase):
    def test_disabled_mode(self):
        e = Engine(area())
        self.assertEqual(e.arm(Mode.HOME, ALL_CLOSED, 0, actor=None).reason, "mode_disabled")

    def test_exit_delay_then_armed(self):
        e = Engine(area())
        self.assertTrue(e.arm(AWAY, ALL_CLOSED, 0, actor="Sam").ok)
        self.assertEqual((e.snap.state, e.snap.delay, e.snap.deadline), (State.ARMING, 30, 30))
        e.tick(29.9)
        self.assertIs(e.snap.state, State.ARMING)
        e.tick(30)
        self.assertIs(e.snap.state, State.ARMED_AWAY)
        self.assertEqual(e.snap.changed_by, "Sam")

    def test_open_sensor_refuses(self):
        e = Engine(area())
        out = e.arm(AWAY, {**ALL_CLOSED, "binary_sensor.back": "on"}, 0, actor=None)
        self.assertEqual(out.reason, "open_sensors")
        self.assertEqual(e.snap.open_sensors, {"binary_sensor.back": "on"})
        self.assertIs(e.snap.state, State.DISARMED)
        self.assertEqual(out.events[0].name, "failed_to_arm")

    def test_unavailable_and_missing_block(self):
        e = Engine(area())
        states = {k: v for k, v in ALL_CLOSED.items() if k != "binary_sensor.front"}
        states["binary_sensor.back"] = "unavailable"
        out = e.arm(AWAY, states, 0, actor=None)
        self.assertEqual(set(e.snap.open_sensors), {"binary_sensor.front", "binary_sensor.back"})
        self.assertFalse(out.ok)

    def test_allow_open_only_when_open_not_when_unavailable(self):
        e = Engine(area())
        self.assertTrue(e.arm(AWAY, {**ALL_CLOSED, "binary_sensor.hall_motion": "on"}, 0, actor=None).ok)
        e2 = Engine(area())
        self.assertFalse(e2.arm(AWAY, {**ALL_CLOSED, "binary_sensor.hall_motion": "unavailable"}, 0, actor=None).ok)

    def test_always_on_never_blocks(self):
        e = Engine(area())
        self.assertTrue(e.arm(AWAY, {**ALL_CLOSED, "binary_sensor.smoke": "on"}, 0, actor=None).ok)

    def test_force_bypasses_only_approved(self):
        open_back = {**ALL_CLOSED, "binary_sensor.back": "on"}
        e = Engine(area())
        self.assertTrue(e.arm(AWAY, open_back, 0, actor=None, force=True,
                              approved_bypass=frozenset({"binary_sensor.back"})).ok)
        self.assertEqual(e.snap.bypassed, frozenset({"binary_sensor.back"}))
        # Another sensor opened since approval: refused, full list reported.
        e2 = Engine(area())
        out = e2.arm(AWAY, {**open_back, "binary_sensor.front": "on"}, 0, actor=None, force=True,
                     approved_bypass=frozenset({"binary_sensor.back"}))
        self.assertEqual(out.reason, "open_sensors")
        self.assertEqual(set(e2.snap.open_sensors), {"binary_sensor.back", "binary_sensor.front"})
        self.assertIs(e2.snap.state, State.DISARMED)

    def test_cannot_arm_while_triggered(self):
        e = armed()
        e.sensor_changed("binary_sensor.glass", "off", "on", 100)
        self.assertEqual(e.arm(AWAY, ALL_CLOSED, 101, actor=None).reason, "invalid_state")


class Tripping(unittest.TestCase):
    def test_entry_delay_then_trigger(self):
        e = armed()
        e.sensor_changed("binary_sensor.front", "off", "on", 100)
        self.assertEqual((e.snap.state, e.snap.delay), (State.PENDING, 20))
        e.tick(120)
        self.assertIs(e.snap.state, State.TRIGGERED)
        self.assertEqual(e.snap.triggered_by, "binary_sensor.front")

    def test_disarm_during_entry_delay(self):
        e = armed()
        e.sensor_changed("binary_sensor.front", "off", "on", 100)
        e.disarm(105, actor="Sam")
        e.tick(200)
        self.assertIs(e.snap.state, State.DISARMED)

    def test_immediate_sensor(self):
        e = armed()
        e.sensor_changed("binary_sensor.glass", "off", "on", 100)
        self.assertIs(e.snap.state, State.TRIGGERED)

    def test_immediate_during_entry_delay(self):
        e = armed()
        e.sensor_changed("binary_sensor.front", "off", "on", 100)
        e.sensor_changed("binary_sensor.glass", "off", "on", 101)
        self.assertEqual((e.snap.state, e.snap.triggered_by), (State.TRIGGERED, "binary_sensor.glass"))

    def test_exit_delay_path(self):
        e = Engine(area())
        e.arm(AWAY, ALL_CLOSED, 0, actor=None)
        e.sensor_changed("binary_sensor.front", "off", "on", 5)  # leaving: fine
        self.assertIs(e.snap.state, State.ARMING)
        e.sensor_changed("binary_sensor.window", "off", "on", 6)  # not an exit
        self.assertIs(e.snap.state, State.TRIGGERED)

    def test_bypassed_sensor_ignored(self):
        e = Engine(area())
        e.arm(AWAY, {**ALL_CLOSED, "binary_sensor.back": "on"}, 0, actor=None, force=True,
              approved_bypass=frozenset({"binary_sensor.back"}))
        e.tick(30)
        e.sensor_changed("binary_sensor.back", "off", "on", 100)
        self.assertIs(e.snap.state, State.ARMED_AWAY)

    def test_always_on_trips_when_disarmed(self):
        e = Engine(area())
        e.sensor_changed("binary_sensor.smoke", "off", "on", 0)
        self.assertIs(e.snap.state, State.TRIGGERED)
        e.tick(300)
        self.assertIs(e.snap.state, State.DISARMED)

    def test_edge_not_level(self):
        e = armed()
        e.sensor_changed("binary_sensor.front", "on", "on", 100)
        self.assertIs(e.snap.state, State.ARMED_AWAY)

    def test_unavailable_trips_only_when_configured(self):
        sensors = (SensorConfig("binary_sensor.a", frozenset({AWAY})),
                   SensorConfig("binary_sensor.b", frozenset({AWAY}), trigger_unavailable=True, immediate=True))
        e = Engine(area(sensors=sensors))
        e.arm(AWAY, {"binary_sensor.a": "off", "binary_sensor.b": "off"}, 0, actor=None)
        e.tick(30)
        e.sensor_changed("binary_sensor.a", "off", "unavailable", 40)
        self.assertIs(e.snap.state, State.ARMED_AWAY)
        e.sensor_changed("binary_sensor.b", "off", "unavailable", 41)
        self.assertIs(e.snap.state, State.TRIGGERED)

    def test_trigger_time_returns_to_armed_or_disarms(self):
        e = armed()
        e.sensor_changed("binary_sensor.glass", "off", "on", 100)
        e.tick(160)
        self.assertIs(e.snap.state, State.ARMED_AWAY)
        e2 = Engine(area(disarm_after_trigger=True))
        e2.arm(AWAY, ALL_CLOSED, 0, actor=None)
        e2.tick(30)
        e2.sensor_changed("binary_sensor.glass", "off", "on", 100)
        e2.tick(160)
        self.assertIs(e2.snap.state, State.DISARMED)


class SensorLost(unittest.TestCase):
    def test_offline_while_armed_is_reported(self):
        e = armed()
        out = e.sensor_changed("binary_sensor.front", "off", "unavailable", 100)
        self.assertEqual([ev.name for ev in out.events], ["sensor_lost"])
        self.assertIs(e.snap.state, State.ARMED_AWAY)

    def test_offline_while_disarmed_is_quiet_except_always_on(self):
        e = Engine(area())
        self.assertEqual(e.sensor_changed("binary_sensor.front", "off", "unavailable", 0).events, [])
        out = e.sensor_changed("binary_sensor.smoke", "off", "unavailable", 0)
        self.assertEqual([ev.name for ev in out.events], ["sensor_lost"])


class Restore(unittest.TestCase):
    def test_pending_comes_back_triggered(self):
        snap = Snapshot(state=State.PENDING, arm_mode=AWAY, triggered_by="binary_sensor.front")
        e = Engine.restored(area(), snap, 1000)
        self.assertIs(e.snap.state, State.TRIGGERED)

    def test_arming_restarts_exit_delay(self):
        e = Engine.restored(area(), Snapshot(state=State.ARMING, arm_mode=AWAY), 1000)
        self.assertEqual((e.snap.state, e.snap.deadline), (State.ARMING, 1030))

    def test_armed_stays_armed(self):
        e = Engine.restored(area(), Snapshot(state=State.ARMED_AWAY, arm_mode=AWAY), 1000)
        self.assertIs(e.snap.state, State.ARMED_AWAY)


if __name__ == "__main__":
    unittest.main()


class Options(unittest.TestCase):
    def test_build_and_sanitise(self):
        spec = importlib.util.spec_from_file_location("hp.options", ROOT / "options.py")
        options = importlib.util.module_from_spec(spec)
        sys.modules["hp.options"] = options
        spec.loader.exec_module(options)
        cfg = options.area_config({
            "away_sensors": ["binary_sensor.front", "binary_sensor.back", "light.kitchen", 7],
            "home_sensors": ["binary_sensor.front"],
            "always_on_sensors": ["binary_sensor.smoke"],
            "immediate_sensors": ["binary_sensor.back"],
            "away_exit_delay": 99999,
            "away_entry_delay": "x",
        })
        ids = [s.entity_id for s in cfg.sensors]
        self.assertEqual(ids, ["binary_sensor.back", "binary_sensor.front", "binary_sensor.smoke"])
        front = cfg.sensor("binary_sensor.front")
        self.assertEqual(front.modes, frozenset({Mode.AWAY, Mode.HOME}))
        self.assertTrue(cfg.sensor("binary_sensor.back").immediate)
        self.assertTrue(cfg.sensor("binary_sensor.smoke").always_on)
        self.assertEqual(cfg.mode(Mode.AWAY).exit_delay, 900)  # clamped
        self.assertEqual(cfg.mode(Mode.AWAY).entry_delay, 30)  # bad value -> default
        self.assertFalse(cfg.mode(Mode.VACATION).enabled)


class Reconcile(unittest.TestCase):
    def test_sensor_open_at_restore_starts_entry(self):
        e = Engine.restored(area(), Snapshot(state=State.ARMED_AWAY, arm_mode=AWAY), 1000)
        e.reconcile({**ALL_CLOSED, "binary_sensor.front": "on"}, 1000)
        self.assertIs(e.snap.state, State.PENDING)

    def test_bypassed_open_at_restore_ignored(self):
        snap = Snapshot(state=State.ARMED_AWAY, arm_mode=AWAY, bypassed=frozenset({"binary_sensor.back"}))
        e = Engine.restored(area(), snap, 1000)
        e.reconcile({**ALL_CLOSED, "binary_sensor.back": "on"}, 1000)
        self.assertIs(e.snap.state, State.ARMED_AWAY)
