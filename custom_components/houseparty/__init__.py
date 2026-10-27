"""HouseParty: a self-contained alarm system for Home Assistant."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .auth import Lockout, User
from .const import DOMAIN
from .store import UserStore

PLATFORMS = [Platform.ALARM_CONTROL_PANEL]


@dataclass
class HousePartyData:
    store: UserStore
    users: list[User]
    lockout: Lockout


type HousePartyConfigEntry = ConfigEntry[HousePartyData]


async def async_setup_entry(hass: HomeAssistant, entry: HousePartyConfigEntry) -> bool:
    store = UserStore(hass, entry.entry_id)
    users = await store.load()

    # The first user is created in the config flow, before the entry (and so
    # its private store) exists. Move it into the store, then out of entry data.
    if initial := entry.data.get("initial_user"):
        initial_user = User.from_dict(initial)
        # The store save and config-entry update are not transactional. An
        # interrupted first setup must not append the same user on every retry.
        by_id = {user.id: user for user in users}
        by_id[initial_user.id] = initial_user
        users = list(by_id.values())
        await store.save(users)
        hass.config_entries.async_update_entry(
            entry, data={k: v for k, v in entry.data.items() if k != "initial_user"})

    if not any(user.enabled and user.can_disarm and not user.duress for user in users):
        raise ConfigEntryNotReady(
            "HouseParty has no enabled non-duress user who can disarm; "
            "repair the private user store before operating the alarm"
        )

    # Lockouts survive an options reload (in memory; a restart clears them).
    lockouts = hass.data.setdefault(DOMAIN, {})
    lockout = lockouts.setdefault(entry.entry_id, Lockout())

    entry.runtime_data = HousePartyData(store=store, users=users, lockout=lockout)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: HousePartyConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: HousePartyConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await UserStore(hass, entry.entry_id).remove()
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
