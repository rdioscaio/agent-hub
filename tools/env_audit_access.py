from __future__ import annotations

import os
from dataclasses import dataclass

_VALID_VPS = ("hub", "next", "maincua")


@dataclass(frozen=True)
class AccessTarget:
    vps: str
    host: str
    user: str = "rdios"
    port: int = 22

    @property
    def ssh_target(self) -> str:
        return f"{self.user}@{self.host}"


def normalize_vps(vps: str) -> str:
    normalized = (vps or "").strip().lower()
    if normalized not in _VALID_VPS:
        raise ValueError(f"unsupported vps: {vps!r}")
    return normalized


def _env_key(vps: str, suffix: str) -> str:
    return f"ENV_AUDIT_{normalize_vps(vps).upper()}_{suffix}"


def resolve_target(vps: str) -> AccessTarget:
    normalized = normalize_vps(vps)
    host = os.getenv(_env_key(normalized, "HOST"), normalized)
    user = os.getenv(_env_key(normalized, "USER"), "rdios")
    port_raw = os.getenv(_env_key(normalized, "PORT"), "22")
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise ValueError(f"invalid port for {normalized}: {port_raw!r}") from exc
    return AccessTarget(vps=normalized, host=host, user=user, port=port)


def expand_vps(selection: str) -> list[str]:
    normalized = (selection or "all").strip().lower()
    if normalized == "all":
        return list(_VALID_VPS)
    return [normalize_vps(normalized)]
