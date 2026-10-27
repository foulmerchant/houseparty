"""User storage: a private (owner-only) file, separate from config entries,
so code hashes never appear in config-entry dumps or diagnostics."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .auth import User
from .const import DOMAIN


class UserStore:
    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict] = Store(hass, 1, f"{DOMAIN}.{entry_id}.users",
                                         private=True, atomic_writes=True)

    async def load(self) -> list[User]:
        data = await self._store.async_load() or {}
        users = []
        for raw in data.get("users", []):
            try:
                users.append(User.from_dict(raw))
            except (KeyError, ValueError, TypeError):
                continue  # skip a corrupt record rather than fail open
        return users

    async def save(self, users: list[User]) -> None:
        await self._store.async_save({"users": [u.to_dict() for u in users]})

    async def remove(self) -> None:
        await self._store.async_remove()
