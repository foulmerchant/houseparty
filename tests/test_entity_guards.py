"""Security-critical entity guards, tested without a Home Assistant install."""

import ast
import asyncio
import importlib.util
import pathlib
import sys
import time
import types
import unittest
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "houseparty"

pkg = types.ModuleType("hp_entity_guards")
pkg.__path__ = [str(ROOT)]
sys.modules[pkg.__name__] = pkg
for name in ("engine", "auth", "options"):
    spec = importlib.util.spec_from_file_location(f"{pkg.__name__}.{name}", ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

from hp_entity_guards.engine import Mode, Snapshot, State  # noqa: E402
from hp_entity_guards.options import DEFAULT_OPTIONS, area_config  # noqa: E402


class GuardError(Exception):
    def __init__(self, key):
        super().__init__(key)
        self.key = key


def _entity_method(name, globals_):
    tree = ast.parse((ROOT / "alarm_control_panel.py").read_text())
    entity = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "HousePartyAlarm"
    )
    method = next(
        node for node in entity.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )
    method.decorator_list = []
    module = ast.Module(body=[method], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(ROOT / "alarm_control_panel.py"), "exec"), globals_)
    return globals_[name]


class _Logger:
    def warning(self, *args):
        pass


_restore = _entity_method("_snapshot_from", {
    "State": State,
    "Mode": Mode,
    "Snapshot": Snapshot,
    "_LOGGER": _Logger(),
})
_authorize = _entity_method("_authorize", {
    "asyncio": asyncio,
    "time": time,
    "Mode": Mode,
    "_error": lambda key, **kwargs: GuardError(key),
})


class FakeEntity:
    def __init__(self, **option_changes):
        self._config = area_config({**DEFAULT_OPTIONS, **option_changes})

    _snapshot_from = _restore


class RestoreGuards(unittest.TestCase):
    def test_always_on_trigger_does_not_need_an_arm_mode(self):
        entity = FakeEntity(away_enabled=False)
        last = SimpleNamespace(state="triggered", attributes={"arm_mode": None})
        snapshot = entity._snapshot_from(last)
        self.assertIs(snapshot.state, State.TRIGGERED)
        self.assertIsNone(snapshot.arm_mode)

    def test_unknown_state_restores_triggered(self):
        last = SimpleNamespace(state="unavailable", attributes={})
        self.assertIs(FakeEntity()._snapshot_from(last).state, State.TRIGGERED)

    def test_disarmed_discards_stale_arm_mode(self):
        last = SimpleNamespace(state="disarmed", attributes={"arm_mode": "armed_away"})
        snapshot = FakeEntity()._snapshot_from(last)
        self.assertIs(snapshot.state, State.DISARMED)
        self.assertIsNone(snapshot.arm_mode)

    def test_mode_restore_uses_an_enabled_mode_not_only_away(self):
        entity = FakeEntity(away_enabled=False)
        last = SimpleNamespace(state="pending", attributes={})
        snapshot = entity._snapshot_from(last)
        self.assertIs(snapshot.arm_mode, Mode.HOME)


class _NoLockout:
    def remaining(self, key, now):
        return 0


class AuthorizationGuards(unittest.IsolatedAsyncioTestCase):
    async def test_no_users_never_means_no_authorization(self):
        entity = SimpleNamespace(
            _auth_lock=asyncio.Lock(),
            _auth=SimpleNamespace(users=[]),
            _lockout=_NoLockout(),
            _caller=lambda: "user:test",
            _attr_code_arm_required=True,
        )
        for disarm in (False, True):
            with self.subTest(disarm=disarm), self.assertRaises(GuardError) as caught:
                await _authorize(entity, None, disarm=disarm, mode=None)
            self.assertEqual(caught.exception.key, "auth_unavailable")


if __name__ == "__main__":
    unittest.main()
