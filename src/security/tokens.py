"""High-entropy token generation and one-way token hashing."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import string
from dataclasses import dataclass

TOKEN_ALPHABET = string.ascii_letters + string.digits + "_-"


@dataclass(frozen=True, slots=True)
class Principal:
    kind: str
    id: str
    name: str
    role: str | None = None
    token_id: str | None = None
    csrf_secret: str | None = None

    @property
    def is_agent(self) -> bool:
        return self.kind == "agent"

    @property
    def is_user(self) -> bool:
        return self.kind == "user"

    @property
    def is_admin(self) -> bool:
        return self.is_user and self.role == "admin"


def new_agent_token() -> tuple[str, str, str]:
    prefix = "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(8))
    secret = secrets.token_urlsafe(32)
    raw = f"ah_{prefix}_{secret}"
    return raw, prefix, sha256_hex(raw)


def new_lease_token() -> tuple[str, str]:
    raw = secrets.token_urlsafe(24)
    return raw, sha256_hex(raw)


def sha256_hex(value: str | bytes) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def safe_compare(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
