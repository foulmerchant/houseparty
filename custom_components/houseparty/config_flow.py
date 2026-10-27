"""Setup and options flows. Both are admin-only in Home Assistant."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .auth import Authenticator, User, hash_code, valid_code
from .const import DOMAIN
from .engine import Mode
from .options import DEFAULT_OPTIONS, MAX_DELAY_S, MAX_TRIGGER_S, ROLE_KEYS, all_sensor_ids
from .store import UserStore

PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
MAX_USERS = 10  # Bound serialized scrypt work on low-powered HA hosts.
MODES = SelectSelector(SelectSelectorConfig(
    options=[SelectOptionDict(value=m.value, label=m.value) for m in Mode],
    multiple=True, translation_key="mode"))


def _seconds(hi: int) -> NumberSelector:
    return NumberSelector(NumberSelectorConfig(min=0, max=hi, step=5, unit_of_measurement="s",
                                               mode=NumberSelectorMode.BOX))


def _code_errors(data: dict[str, Any]) -> dict[str, str]:
    if not valid_code(data["code"]):
        return {"code": "invalid_code_format"}
    if data["code"] != data["code_confirm"]:
        return {"code_confirm": "codes_differ"}
    return {}


class HousePartyConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        # An alarm without a code is one anyone with dashboard access can
        # disarm, so the first user and code are required up front.
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _code_errors(user_input)
            if not errors:
                code_hash = await self.hass.async_add_executor_job(hash_code, user_input["code"])
                user = User(id=uuid4().hex, name=user_input["user_name"].strip()[:40], code_hash=code_hash)
                return self.async_create_entry(title=user_input["name"].strip()[:40],
                                               data={"initial_user": user.to_dict()},
                                               options=dict(DEFAULT_OPTIONS))
        return self.async_show_form(step_id="user", errors=errors, data_schema=vol.Schema({
            vol.Required("name", default="House"): str,
            vol.Required("user_name"): str,
            vol.Required("code"): PASSWORD,
            vol.Required("code_confirm"): PASSWORD,
        }))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        return HousePartyOptionsFlow()


class HousePartyOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input=None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=[
            "modes", "sensors", "sensor_roles", "add_user", "remove_user"])

    def _save(self, changes: dict[str, Any]) -> ConfigFlowResult:
        return self.async_create_entry(data={**DEFAULT_OPTIONS, **self.config_entry.options, **changes})

    # ---- modes and delays ---------------------------------------------------

    async def async_step_modes(self, user_input=None) -> ConfigFlowResult:
        if user_input is not None:
            return self._save({k: (int(v) if isinstance(v, float) else v) for k, v in user_input.items()})
        o = {**DEFAULT_OPTIONS, **self.config_entry.options}
        fields: dict = {
            vol.Required("code_arm_required", default=o["code_arm_required"]): BooleanSelector(),
            vol.Required("disarm_after_trigger", default=o["disarm_after_trigger"]): BooleanSelector(),
        }
        for m in Mode:
            fields[vol.Required(f"{m.value}_enabled", default=o[f"{m.value}_enabled"])] = BooleanSelector()
            for key, hi in (("exit_delay", MAX_DELAY_S), ("entry_delay", MAX_DELAY_S),
                            ("trigger_time", MAX_TRIGGER_S)):
                fields[vol.Required(f"{m.value}_{key}", default=o[f"{m.value}_{key}"])] = _seconds(hi)
        return self.async_show_form(step_id="modes", data_schema=vol.Schema(fields))

    # ---- which sensors protect which mode -------------------------------------

    async def async_step_sensors(self, user_input=None) -> ConfigFlowResult:
        if user_input is not None:
            return self._save(user_input)
        o = {**DEFAULT_OPTIONS, **self.config_entry.options}
        pick = EntitySelector(EntitySelectorConfig(domain="binary_sensor", multiple=True))
        fields = {vol.Optional(f"{m.value}_sensors", default=o[f"{m.value}_sensors"]): pick for m in Mode}
        fields[vol.Optional("always_on_sensors", default=o["always_on_sensors"])] = pick
        return self.async_show_form(step_id="sensors", data_schema=vol.Schema(fields))

    async def async_step_sensor_roles(self, user_input=None) -> ConfigFlowResult:
        if user_input is not None:
            return self._save(user_input)
        o = {**DEFAULT_OPTIONS, **self.config_entry.options}
        configured = all_sensor_ids(o)
        if not configured:
            return self.async_abort(reason="no_sensors")
        pick = EntitySelector(EntitySelectorConfig(include_entities=configured, multiple=True))
        return self.async_show_form(step_id="sensor_roles", data_schema=vol.Schema({
            vol.Optional(k, default=[e for e in o[k] if e in configured]): pick for k in ROLE_KEYS
        }))

    # ---- users and codes --------------------------------------------------------

    async def async_step_add_user(self, user_input=None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _code_errors(user_input)
            store = UserStore(self.hass, self.config_entry.entry_id)
            users = await store.load()
            if not errors:
                auth_users = [u for u in users if u.enabled]
                if len(auth_users) >= MAX_USERS:
                    errors["base"] = "user_limit"
                elif await self.hass.async_add_executor_job(Authenticator(auth_users).identify,
                                                          user_input["code"]):
                    errors["code"] = "code_in_use"  # codes identify users; they must be unique
            if not errors:
                code_hash = await self.hass.async_add_executor_job(hash_code, user_input["code"])
                users.append(User(
                    id=uuid4().hex, name=user_input["name"].strip()[:40], code_hash=code_hash,
                    can_arm=frozenset(Mode(m) for m in user_input.get("can_arm", [])),
                    can_disarm=user_input.get("can_disarm", True), duress=user_input.get("duress", False)))
                await store.save(users)
                return self._save({"users_rev": self.config_entry.options.get("users_rev", 0) + 1})
        return self.async_show_form(step_id="add_user", errors=errors, data_schema=vol.Schema({
            vol.Required("name"): str,
            vol.Required("code"): PASSWORD,
            vol.Required("code_confirm"): PASSWORD,
            vol.Optional("can_arm", default=[m.value for m in Mode]): MODES,
            vol.Required("can_disarm", default=True): BooleanSelector(),
            vol.Required("duress", default=False): BooleanSelector(),
        }))

    async def async_step_remove_user(self, user_input=None) -> ConfigFlowResult:
        store = UserStore(self.hass, self.config_entry.entry_id)
        users = await store.load()
        errors: dict[str, str] = {}
        if user_input is not None:
            remaining = [u for u in users if u.id != user_input["user"]]
            # Never leave the alarm with nobody able to disarm it.
            if not any(u.enabled and u.can_disarm and not u.duress for u in remaining):
                errors["user"] = "last_disarmer"
            else:
                await store.save(remaining)
                return self._save({"users_rev": self.config_entry.options.get("users_rev", 0) + 1})
        return self.async_show_form(step_id="remove_user", errors=errors, data_schema=vol.Schema({
            vol.Required("user"): SelectSelector(SelectSelectorConfig(options=[
                SelectOptionDict(value=u.id, label=u.name + (" (duress)" if u.duress else ""))
                for u in users])),
        }))
