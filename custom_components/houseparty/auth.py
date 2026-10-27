"""HouseParty codes, users and lockout.

No Home Assistant imports. Hashing is CPU-heavy on purpose: call `hash_code`
and `Authenticator.identify` from an executor, never the event loop.

Honest limits: a 6-digit code has a million possibilities, so anyone who can
read HouseParty's storage file can brute-force it offline whatever the hash.
The hash protects against casual reading; the storage file (written owner-
only) and HA's own access control protect the rest. Online guessing is what
the lockout stops, and that's the realistic attack.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass, field

from .engine import Mode

MIN_CODE_LEN = 6
MAX_CODE_LEN = 12

# scrypt cost: ~16 MiB and tens of milliseconds per check.
_N, _R, _P = 2**14, 8, 1
_DUMMY_HASH: str | None = None


def valid_code(code: str) -> bool:
    return code.isascii() and code.isdigit() and MIN_CODE_LEN <= len(code) <= MAX_CODE_LEN


def hash_code(code: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(code.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${_N}${_R}${_P}${b64(salt)}${b64(digest)}"


def verify_code(code: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = encoded.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(code.encode(), salt=base64.b64decode(salt),
                                n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


@dataclass(frozen=True)
class User:
    id: str
    name: str
    code_hash: str
    can_arm: frozenset[Mode] = frozenset(Mode)
    can_disarm: bool = True
    duress: bool = False  # disarms normally, but fires houseparty_duress
    enabled: bool = True

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "code_hash": self.code_hash,
                "can_arm": sorted(m.value for m in self.can_arm),
                "can_disarm": self.can_disarm, "duress": self.duress, "enabled": self.enabled}

    @classmethod
    def from_dict(cls, d: dict) -> User:
        return cls(id=str(d["id"]), name=str(d["name"]), code_hash=str(d["code_hash"]),
                   can_arm=frozenset(Mode(m) for m in d.get("can_arm", [])),
                   can_disarm=bool(d.get("can_disarm", True)), duress=bool(d.get("duress", False)),
                   enabled=bool(d.get("enabled", True)))


class Authenticator:
    def __init__(self, users: list[User]) -> None:
        self.users = [u for u in users if u.enabled]

    def identify(self, code: str | None) -> User | None:
        """The user whose code this is, or None. Checks every user every
        time, so the time taken doesn't reveal which user (if any) matched."""
        global _DUMMY_HASH
        if not code or not valid_code(code):
            # Same cost as a real check, so malformed codes aren't faster.
            _DUMMY_HASH = _DUMMY_HASH or hash_code("000000")
            verify_code("000000", _DUMMY_HASH)
            return None
        match = None
        for user in self.users:
            if verify_code(code, user.code_hash) and match is None:
                match = user
        return match


@dataclass
class Lockout:
    """Per-caller lockout, keyed by the HA user making the call, so someone
    hammering the wall tablet locks out the tablet's account, not yours."""

    free_tries: int = 3
    base_s: float = 30.0
    max_s: float = 900.0
    _state: dict[str, tuple[int, float]] = field(default_factory=dict)

    def remaining(self, key: str, now: float) -> float:
        _, until = self._state.get(key, (0, 0.0))
        return max(0.0, until - now)

    def failure(self, key: str, now: float) -> float:
        failures = self._state.get(key, (0, 0.0))[0] + 1
        extra = failures - self.free_tries
        lock = 0.0 if extra < 0 else min(self.base_s * 2**extra, self.max_s)
        self._state[key] = (failures, now + lock)
        return lock

    def success(self, key: str) -> None:
        self._state.pop(key, None)
