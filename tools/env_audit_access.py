"""Shared access override helpers for env audit tools."""

from __future__ import annotations

import json
import os
from dataclasses import replace

VALID_ACCESS_MODES = {"local", "ssh"}
ENV_NAME = "ENV_AUDIT_ACCESS_OVERRIDES"


def load_access_overrides_from_env(env_name: str = ENV_NAME) -> dict[str, dict]:
    raw = os.getenv(env_name, "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{env_name} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{env_name} must be a JSON object keyed by vps id")

    normalized: dict[str, dict] = {}
    for vps_id, raw_entry in payload.items():
        if not isinstance(raw_entry, dict):
            raise ValueError(f"{env_name} entry for {vps_id!r} must be a JSON object")
        mode = str(raw_entry.get("mode", "")).strip()
        if mode not in VALID_ACCESS_MODES:
            raise ValueError(f"{env_name} entry for {vps_id!r} has invalid mode: {mode!r}")
        host_alias = str(raw_entry.get("host_alias", "")).strip()
        sudo = bool(raw_entry.get("sudo", False))
        if mode == "ssh" and not host_alias:
            raise ValueError(f"{env_name} entry for {vps_id!r} requires host_alias for ssh mode")
        normalized[str(vps_id).strip()] = {
            "mode": mode,
            "host_alias": host_alias,
            "sudo": sudo,
        }
    return normalized


def apply_access_overrides(vps_specs: list, overrides: dict[str, dict]) -> list:
    if not overrides:
        return vps_specs
    updated = []
    for spec in vps_specs:
        override = overrides.get(spec.vps_id)
        if not override:
            updated.append(spec)
            continue
        updated.append(
            replace(
                spec,
                access_mode=override["mode"],
                host_alias=override["host_alias"],
                sudo=override["sudo"],
            )
        )
    return updated
