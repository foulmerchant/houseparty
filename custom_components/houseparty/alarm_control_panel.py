"""The HouseParty alarm entity: wires the pure engine to Home Assistant."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import replace

import voluptuous as vol

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
    CodeFormat,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_utc_time, async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from . import HousePartyConfigEntry
from .auth import Authenticator
from .const import API_VERSION, DOMAIN
from .engine import Engine, Mode, Outcome, Snapshot, State
from .options import area_config

_LOGGER = logging.getLogger(__name__)

FEATURES = {
    Mode.AWAY: AlarmControlPanelEntityFeature.ARM_AWAY,
    Mode.HOME: AlarmControlPanelEntityFeature.ARM_HOME,
    Mode.NIGHT: AlarmControlPanelEntityFeature.ARM_NIGHT,
    Mode.VACATION: AlarmControlPanelEntityFeature.ARM_VACATION,
    Mode.CUSTOM: AlarmControlPanelEntityFeature.ARM_CUSTOM_BYPASS,
}


async def async_setup_entry(hass: HomeAssistant, entry: HousePartyConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([HousePartyAlarm(entry)])
    entity_platform.async_get_current_platform().async_register_entity_service(
        "arm",
        {
            vol.Required("mode"): vol.In([m.value for m in Mode]),
            vol.Optional("code"): cv.string,
            vol.Optional("force", default=False): cv.boolean,
            vol.Optional("bypass", default=[]): vol.All(cv.ensure_list, [cv.entity_id]),
        },
        "async_houseparty_arm",
    )


def _error(key: str, **placeholders: str) -> ServiceValidationError:
    return ServiceValidationError(translation_domain=DOMAIN, translation_key=key,
                                  translation_placeholders=placeholders or None)


class HousePartyAlarm(AlarmControlPanelEntity, RestoreEntity):
    _attr_should_poll = False

    def __init__(self, entry: HousePartyConfigEntry) -> None:
        self._entry = entry
        self._config = area_config(entry.options)
        self._auth = Authenticator(entry.runtime_data.users)
        self._lockout = entry.runtime_data.lockout
        self._engine = Engine(self._config)
        self._auth_lock = asyncio.Lock()
        self._unsub_timer: callable | None = None

        self._attr_unique_id = entry.entry_id
        self._attr_name = entry.title
        has_users = bool(self._auth.users)
        self._attr_code_format = CodeFormat.NUMBER if has_users else None
        self._attr_code_arm_required = has_users and bool(entry.options.get("code_arm_required", True))
        features = AlarmControlPanelEntityFeature(0)
        for mode, feature in FEATURES.items():
            if self._config.mode(mode).enabled:
                features |= feature
        self._attr_supported_features = features

    # ---- what HA and the card see ----------------------------------------

    @property
    def alarm_state(self) -> AlarmControlPanelState:
        return AlarmControlPanelState(self._engine.snap.state.value)

    @property
    def changed_by(self) -> str | None:
        return self._engine.snap.changed_by

    @property
    def extra_state_attributes(self) -> dict:
        s = self._engine.snap
        return {
            "houseparty_api": API_VERSION,
            "arm_mode": s.arm_mode.armed_state.value if s.arm_mode else None,
            "delay": s.delay,
            "open_sensors": dict(s.open_sensors) or None,
            "bypassed_sensors": sorted(s.bypassed) or None,
            "triggered_by": s.triggered_by,
            "last_triggered": (dt_util.utc_from_timestamp(s.last_triggered).isoformat()
                               if s.last_triggered else None),
        }

    def code_or_default_code(self, code: str | None) -> str | None:  # type: ignore[override]
        # Deliberately ignore HA's per-entity "default code" option: with it
        # set, anyone who can reach a dashboard could disarm without a code.
        return code

    # ---- lifecycle --------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Belt and braces for code_or_default_code() above: if HA's per-entity
        # default code is set, say so loudly. It is ignored either way.
        reg = self.registry_entry
        if reg and (reg.options.get("alarm_control_panel") or {}).get("default_code"):
            _LOGGER.warning("%s: a default code is set in the entity settings. HouseParty "
                            "ignores it; remove it so nobody relies on it.", self.entity_id)
        now = time.time()
        if (last := await self.async_get_last_state()) is not None:
            self._engine = Engine.restored(self._config, self._snapshot_from(last), now)
        self.async_on_remove(async_track_state_change_event(
            self.hass, [s.entity_id for s in self._config.sensors], self._on_sensor))
        self.async_on_remove(self._cancel_timer)
        # A door opened while HA was down must not go unnoticed.
        self._apply(self._engine.reconcile(self._sensor_states(), now))

    def _snapshot_from(self, last) -> Snapshot:
        try:
            state = State(last.state)
        except ValueError:
            _LOGGER.warning("Unknown last state %r; restoring triggered", last.state)
            return Snapshot(state=State.TRIGGERED)
        if state is State.DISARMED:
            return Snapshot()

        attrs = last.attributes
        mode = None
        if isinstance(attrs.get("arm_mode"), str):
            try:
                mode = Mode.from_armed_state(State(attrs["arm_mode"]))
            except ValueError:
                mode = None
        mode = mode or Mode.from_armed_state(state)
        if state is not State.TRIGGERED and mode is None:
            # Arming, pending and armed states need a mode. Use any enabled
            # mode rather than guessing Away specifically; if none exists,
            # restore triggered instead of silently weakening the alarm.
            mode = next((m for m in Mode if self._config.mode(m).enabled), None)
            if mode is None:
                return Snapshot(state=State.TRIGGERED)
        bypassed = attrs.get("bypassed_sensors") or []
        return Snapshot(
            state=state,
            arm_mode=mode,
            bypassed=frozenset(b for b in bypassed if isinstance(b, str)),
            triggered_by=attrs.get("triggered_by") if isinstance(attrs.get("triggered_by"), str) else None,
            changed_by=attrs.get("changed_by"),
        )

    # ---- commands ---------------------------------------------------------

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        actor, duress = await self._authorize(code, disarm=True, mode=None)
        self._apply(self._engine.disarm(time.time(), actor=actor))
        if duress:
            # Looks like a normal disarm on every screen; automations can act.
            self.hass.bus.async_fire(f"{DOMAIN}_duress", {"entity_id": self.entity_id})

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        await self._arm(Mode.AWAY, code)

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        await self._arm(Mode.HOME, code)

    async def async_alarm_arm_night(self, code: str | None = None) -> None:
        await self._arm(Mode.NIGHT, code)

    async def async_alarm_arm_vacation(self, code: str | None = None) -> None:
        await self._arm(Mode.VACATION, code)

    async def async_alarm_arm_custom_bypass(self, code: str | None = None) -> None:
        await self._arm(Mode.CUSTOM, code)

    async def async_houseparty_arm(self, mode: str, code: str | None = None,
                                   force: bool = False, bypass: list[str] | None = None) -> None:
        await self._arm(Mode(mode), code, force=force, bypass=frozenset(bypass or ()))

    async def _arm(self, mode: Mode, code: str | None, *, force: bool = False,
                   bypass: frozenset[str] = frozenset()) -> None:
        actor, _ = await self._authorize(code, disarm=False, mode=mode)
        out = self._engine.arm(mode, self._sensor_states(), time.time(), actor=actor,
                               force=force, approved_bypass=bypass)
        self._apply(out)
        if not out.ok and out.reason in ("mode_disabled", "invalid_state"):
            raise _error(out.reason)
        # An open-sensor refusal is not an error: it's reported through the
        # open_sensors attribute and a houseparty_failed_to_arm event, so the
        # card can show the list and offer a force-arm.

    # ---- authorization ----------------------------------------------------

    def _caller(self) -> str:
        ctx = self._context
        return f"user:{ctx.user_id}" if ctx and ctx.user_id else "system"

    async def _authorize(self, code: str | None, *, disarm: bool,
                         mode: Mode | None) -> tuple[str | None, bool]:
        """Returns (who, duress). Raises on a bad code, lockout, or missing
        permission. Serialized, so parallel guesses can't all slip past the
        lockout check before any failure is recorded."""
        async with self._auth_lock:
            caller = self._caller()
            left = self._lockout.remaining(caller, time.time())
            if left > 0:
                raise _error("locked_out", seconds=str(math.ceil(left)))
            if not self._auth.users:
                # Missing or corrupt credentials must never turn into no auth.
                raise _error("auth_unavailable")
            if not (disarm or self._attr_code_arm_required or code):
                return None, False
            user = await self.hass.async_add_executor_job(self._auth.identify, code)
            if user is None:
                self._lockout.failure(caller, time.time())
                self.hass.bus.async_fire(f"{DOMAIN}_failed_code",
                                         {"entity_id": self.entity_id, "caller": caller})
                raise _error("invalid_code")
            if (disarm and not user.can_disarm) or (mode is not None and mode not in user.can_arm):
                # Counts as a failed attempt: otherwise "not allowed" would
                # confirm that a guessed code is real, with no lockout cost.
                self._lockout.failure(caller, time.time())
                raise _error("not_allowed")
            self._lockout.success(caller)
            return user.name, user.duress

    # ---- engine plumbing --------------------------------------------------

    def _sensor_states(self) -> dict[str, str | None]:
        return {s.entity_id: (st.state if (st := self.hass.states.get(s.entity_id)) else None)
                for s in self._config.sensors}

    @callback
    def _on_sensor(self, event: Event[EventStateChangedData]) -> None:
        old, new = event.data["old_state"], event.data["new_state"]
        before = replace(self._engine.snap)
        out = self._engine.sensor_changed(event.data["entity_id"], old.state if old else None,
                                          new.state if new else None, time.time())
        # Most sensor changes change nothing here: skip the state write.
        if out.events or self._engine.snap != before:
            self._apply(out)

    @callback
    def _on_deadline(self, _now) -> None:
        self._unsub_timer = None
        deadline = self._engine.snap.deadline or 0.0
        self._apply(self._engine.tick(max(time.time(), deadline)))

    @callback
    def _apply(self, out: Outcome) -> None:
        for ev in out.events:
            self.hass.bus.async_fire(f"{DOMAIN}_{ev.name}", {"entity_id": self.entity_id, **ev.data})
        self._schedule()
        self.async_write_ha_state()

    @callback
    def _schedule(self) -> None:
        self._cancel_timer()
        if (deadline := self._engine.snap.deadline) is not None:
            self._unsub_timer = async_track_point_in_utc_time(
                self.hass, self._on_deadline, dt_util.utc_from_timestamp(deadline))

    @callback
    def _cancel_timer(self) -> None:
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None
