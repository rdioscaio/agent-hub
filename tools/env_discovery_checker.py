#!/usr/bin/env python3
"""Discover unmapped env-sensitive files and services outside the current matrix/spec."""

from __future__ import annotations

import argparse
import base64
import fnmatch
import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import env_scope_checker, env_wiring_checker
from tools.env_audit_access import apply_access_overrides, load_access_overrides_from_env


@dataclass(frozen=True)
class KnownComposeTarget:
    path: str
    service_name: str


@dataclass(frozen=True)
class DiscoveryVps:
    vps_id: str
    label: str
    access_mode: str
    host_alias: str
    sudo: bool
    env_roots: tuple[str, ...]
    systemd_dirs: tuple[str, ...]
    compose_roots: tuple[str, ...]
    ignore_globs: tuple[str, ...]
    known_env_files: tuple[str, ...]
    known_systemd_services: tuple[str, ...]
    known_compose_targets: tuple[KnownComposeTarget, ...]


def _normalize_names(items: list[str]) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = str(item).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return tuple(ordered)


def _build_discovery_specs(matrix_text: str) -> list[DiscoveryVps]:
    scope_specs = {
        item.vps_id: item
        for item in env_scope_checker._load_vps_specs(matrix_text)
    }
    wiring_specs = {
        item.vps_id: item
        for item in env_wiring_checker._load_vps_specs(matrix_text)
    }

    discovery_specs: list[DiscoveryVps] = []
    for vps_id, scope_spec in scope_specs.items():
        wiring_spec = wiring_specs.get(vps_id)
        if wiring_spec is None:
            raise ValueError(f"missing wiring spec for vps id: {vps_id}")

        if (
            scope_spec.access_mode != wiring_spec.access_mode
            or scope_spec.host_alias != wiring_spec.host_alias
            or bool(scope_spec.sudo) != bool(wiring_spec.sudo)
        ):
            raise ValueError(f"access mismatch between checker specs for vps id: {vps_id}")

        env_roots = _normalize_names(list(scope_spec.discovery_roots))
        compose_roots = _normalize_names(
            list(scope_spec.discovery_roots)
            + [
                str(Path(target.path).parent)
                for target in wiring_spec.targets
                if target.kind == "compose-env-file"
            ]
        )
        systemd_dirs = _normalize_names(
            [
                str(Path(target.path).parent)
                for target in wiring_spec.targets
                if target.kind == "systemd-unit"
            ]
            or ["/etc/systemd/system"]
        )
        known_env_files = _normalize_names([file_spec.path for file_spec in scope_spec.files])
        known_systemd_services = _normalize_names(
            [
                target.service_name
                for target in wiring_spec.targets
                if target.kind == "systemd-unit"
            ]
        )
        known_compose_targets = tuple(
            KnownComposeTarget(path=target.path, service_name=target.service_name)
            for target in wiring_spec.targets
            if target.kind == "compose-env-file"
        )

        discovery_specs.append(
            DiscoveryVps(
                vps_id=vps_id,
                label=scope_spec.label,
                access_mode=scope_spec.access_mode,
                host_alias=scope_spec.host_alias,
                sudo=bool(scope_spec.sudo),
                env_roots=env_roots,
                systemd_dirs=systemd_dirs,
                compose_roots=compose_roots,
                ignore_globs=scope_spec.ignore_globs,
                known_env_files=known_env_files,
                known_systemd_services=known_systemd_services,
                known_compose_targets=known_compose_targets,
            )
        )
    return discovery_specs


def _matches_ignore(path: str, ignore_globs: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in ignore_globs)


def _is_env_like_file(name: str) -> bool:
    return name.endswith(".env")


def _is_compose_file(name: str) -> bool:
    lower = name.lower()
    return lower.endswith((".yml", ".yaml")) and (
        lower.startswith("docker-compose")
        or lower == "compose.yml"
        or lower == "compose.yaml"
        or lower.startswith("compose.")
    )


def _is_sensitive_env_reference(path: str) -> bool:
    lower = path.lower()
    name = Path(lower).name
    return (
        lower.endswith(".env")
        or "/opt/secrets/" in lower
        or name.startswith(".env")
        or "secret" in name
    )


def _normalize_environment_file_ref(value: str, base_dir: str = "") -> str:
    normalized = value.strip().strip("'").strip('"')
    while normalized.startswith("-"):
        normalized = normalized[1:].strip()
    if not normalized:
        return normalized
    if normalized.startswith("/"):
        return normalized
    if base_dir:
        return str((Path(base_dir) / normalized).resolve())
    return normalized


def _split_inline_env_files(raw: str) -> list[str]:
    text = raw.strip()
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        body = text[1:-1].strip()
        if not body:
            return []
        return [item.strip().strip("'").strip('"') for item in body.split(",") if item.strip()]
    return [text.strip().strip("'").strip('"')]


def _extract_systemd_environment_files(text: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("EnvironmentFile="):
            continue
        value = _normalize_environment_file_ref(line.split("=", 1)[1])
        if value and _is_sensitive_env_reference(value) and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _extract_compose_services(path: str, text: str) -> list[dict[str, object]]:
    lines = text.splitlines()
    in_services = False
    services_indent = 0
    current_service: str | None = None
    env_file_indent: int | None = None
    result: dict[str, list[str]] = {}

    for raw_line in lines:
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = raw_line.strip()

        if not in_services:
            if stripped == "services:":
                in_services = True
                services_indent = indent
            continue

        if indent <= services_indent and stripped.endswith(":"):
            current_service = None
            env_file_indent = None
            in_services = False
            continue

        if indent == services_indent + 2 and stripped.endswith(":"):
            current_service = stripped[:-1]
            result.setdefault(current_service, [])
            env_file_indent = None
            continue

        if current_service is None:
            continue

        if env_file_indent is not None:
            if indent <= env_file_indent:
                env_file_indent = None
            elif stripped.startswith("- "):
                value = _normalize_environment_file_ref(stripped[2:], str(Path(path).parent))
                if value:
                    result[current_service].append(value)
                continue
            else:
                env_file_indent = None

        if indent == services_indent + 4 and stripped.startswith("env_file:"):
            inline = stripped[len("env_file:"):].strip()
            if inline:
                for item in _split_inline_env_files(inline):
                    value = _normalize_environment_file_ref(item, str(Path(path).parent))
                    if value:
                        result[current_service].append(value)
            else:
                env_file_indent = indent

    payload: list[dict[str, object]] = []
    for service_name, env_files in result.items():
        normalized = _normalize_names(env_files)
        if normalized:
            payload.append(
                {
                    "service_name": service_name,
                    "path": path,
                    "env_files": list(normalized),
                }
            )
    return payload


def _collect_local_probe(vps: DiscoveryVps) -> dict[str, object]:
    env_files: set[str] = set()
    systemd_services: list[dict[str, object]] = []
    compose_services: list[dict[str, object]] = []
    seen_compose_files: set[str] = set()
    seen_systemd_files: set[str] = set()

    for root in vps.env_roots:
        if _matches_ignore(root, vps.ignore_globs):
            continue
        root_path = Path(root)
        if not root_path.is_dir():
            continue
        for dirpath, _, filenames in os.walk(root_path):
            for name in filenames:
                if not _is_env_like_file(name):
                    continue
                candidate = str(Path(dirpath) / name)
                if not _matches_ignore(candidate, vps.ignore_globs):
                    env_files.add(candidate)

    for raw_dir in vps.systemd_dirs:
        if _matches_ignore(raw_dir, vps.ignore_globs):
            continue
        dir_path = Path(raw_dir)
        if not dir_path.is_dir():
            continue
        for candidate in sorted(dir_path.glob("*.service")):
            path = str(candidate)
            if path in seen_systemd_files or _matches_ignore(path, vps.ignore_globs):
                continue
            seen_systemd_files.add(path)
            text = candidate.read_text(encoding="utf-8", errors="replace")
            env_refs = _extract_systemd_environment_files(text)
            if env_refs:
                systemd_services.append(
                    {
                        "service_name": candidate.name,
                        "path": path,
                        "env_files": env_refs,
                    }
                )

    for root in vps.compose_roots:
        if _matches_ignore(root, vps.ignore_globs):
            continue
        root_path = Path(root)
        candidates: list[Path] = []
        if root_path.is_file() and _is_compose_file(root_path.name):
            candidates = [root_path]
        elif root_path.is_dir():
            for dirpath, _, filenames in os.walk(root_path):
                for name in filenames:
                    if _is_compose_file(name):
                        candidates.append(Path(dirpath) / name)
        for candidate in candidates:
            path = str(candidate)
            if path in seen_compose_files or _matches_ignore(path, vps.ignore_globs):
                continue
            seen_compose_files.add(path)
            text = candidate.read_text(encoding="utf-8", errors="replace")
            compose_services.extend(_extract_compose_services(path, text))

    return {
        "env_files": sorted(env_files),
        "systemd_services": systemd_services,
        "compose_services": compose_services,
    }


def _remote_probe(vps: DiscoveryVps, timeout_seconds: int) -> dict[str, object]:
    payload = json.dumps(
        {
            "env_roots": list(vps.env_roots),
            "systemd_dirs": list(vps.systemd_dirs),
            "compose_roots": list(vps.compose_roots),
            "ignore_globs": list(vps.ignore_globs),
        }
    )
    probe = (
        "import fnmatch\n"
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        f"cfg = json.loads({payload!r})\n"
        "def ignored(path):\n"
        "    return any(fnmatch.fnmatch(path, pattern) for pattern in cfg['ignore_globs'])\n"
        "def is_env_like_file(name):\n"
        "    return name.endswith('.env')\n"
        "def is_compose_file(name):\n"
        "    lower = name.lower()\n"
        "    return lower.endswith(('.yml', '.yaml')) and (lower.startswith('docker-compose') or lower == 'compose.yml' or lower == 'compose.yaml' or lower.startswith('compose.'))\n"
        "def is_sensitive_env_reference(path):\n"
        "    lower = path.lower()\n"
        "    name = Path(lower).name\n"
        "    return lower.endswith('.env') or '/opt/secrets/' in lower or name.startswith('.env') or 'secret' in name\n"
        "def normalize_environment_file_ref(value, base_dir=''):\n"
        "    normalized = value.strip().strip(\"'\").strip('\"')\n"
        "    while normalized.startswith('-'):\n"
        "        normalized = normalized[1:].strip()\n"
        "    if not normalized:\n"
        "        return normalized\n"
        "    if normalized.startswith('/'):\n"
        "        return normalized\n"
        "    if base_dir:\n"
        "        return str((Path(base_dir) / normalized).resolve())\n"
        "    return normalized\n"
        "def split_inline_env_files(raw):\n"
        "    text = raw.strip()\n"
        "    if not text:\n"
        "        return []\n"
        "    if text.startswith('[') and text.endswith(']'):\n"
        "        body = text[1:-1].strip()\n"
        "        if not body:\n"
        "            return []\n"
        "        return [item.strip().strip(\"'\").strip('\"') for item in body.split(',') if item.strip()]\n"
        "    return [text.strip().strip(\"'\").strip('\"')]\n"
        "def extract_systemd_environment_files(text):\n"
        "    result = []\n"
        "    seen = set()\n"
        "    for raw_line in text.splitlines():\n"
        "        line = raw_line.strip()\n"
        "        if not line.startswith('EnvironmentFile='):\n"
        "            continue\n"
        "        value = normalize_environment_file_ref(line.split('=', 1)[1])\n"
        "        if value and is_sensitive_env_reference(value) and value not in seen:\n"
        "            seen.add(value)\n"
        "            result.append(value)\n"
        "    return result\n"
        "def extract_compose_services(path, text):\n"
        "    lines = text.splitlines()\n"
        "    in_services = False\n"
        "    services_indent = 0\n"
        "    current_service = None\n"
        "    env_file_indent = None\n"
        "    result = {}\n"
        "    for raw_line in lines:\n"
        "        if not raw_line.strip() or raw_line.lstrip().startswith('#'):\n"
        "            continue\n"
        "        indent = len(raw_line) - len(raw_line.lstrip(' '))\n"
        "        stripped = raw_line.strip()\n"
        "        if not in_services:\n"
        "            if stripped == 'services:':\n"
        "                in_services = True\n"
        "                services_indent = indent\n"
        "            continue\n"
        "        if indent <= services_indent and stripped.endswith(':'):\n"
        "            current_service = None\n"
        "            env_file_indent = None\n"
        "            in_services = False\n"
        "            continue\n"
        "        if indent == services_indent + 2 and stripped.endswith(':'):\n"
        "            current_service = stripped[:-1]\n"
        "            result.setdefault(current_service, [])\n"
        "            env_file_indent = None\n"
        "            continue\n"
        "        if current_service is None:\n"
        "            continue\n"
        "        if env_file_indent is not None:\n"
        "            if indent <= env_file_indent:\n"
        "                env_file_indent = None\n"
        "            elif stripped.startswith('- '):\n"
        "                value = normalize_environment_file_ref(stripped[2:], str(Path(path).parent))\n"
        "                if value:\n"
        "                    result[current_service].append(value)\n"
        "                continue\n"
        "            else:\n"
        "                env_file_indent = None\n"
        "        if indent == services_indent + 4 and stripped.startswith('env_file:'):\n"
        "            inline = stripped[len('env_file:'):].strip()\n"
        "            if inline:\n"
        "                for item in split_inline_env_files(inline):\n"
        "                    value = normalize_environment_file_ref(item, str(Path(path).parent))\n"
        "                    if value:\n"
        "                        result[current_service].append(value)\n"
        "            else:\n"
        "                env_file_indent = indent\n"
        "    payload = []\n"
        "    for service_name, env_files in result.items():\n"
        "        normalized = []\n"
        "        seen = set()\n"
        "        for item in env_files:\n"
        "            if item and item not in seen:\n"
        "                seen.add(item)\n"
        "                normalized.append(item)\n"
        "        if normalized:\n"
        "            payload.append({'service_name': service_name, 'path': path, 'env_files': normalized})\n"
        "    return payload\n"
        "env_files = set()\n"
        "systemd_services = []\n"
        "compose_services = []\n"
        "seen_compose_files = set()\n"
        "seen_systemd_files = set()\n"
        "for root in cfg['env_roots']:\n"
        "    if ignored(root):\n"
        "        continue\n"
        "    root_path = Path(root)\n"
        "    if not root_path.is_dir():\n"
        "        continue\n"
        "    for dirpath, _, filenames in os.walk(root_path):\n"
        "        for name in filenames:\n"
        "            if not is_env_like_file(name):\n"
        "                continue\n"
        "            candidate = str(Path(dirpath) / name)\n"
        "            if not ignored(candidate):\n"
        "                env_files.add(candidate)\n"
        "for raw_dir in cfg['systemd_dirs']:\n"
        "    if ignored(raw_dir):\n"
        "        continue\n"
        "    dir_path = Path(raw_dir)\n"
        "    if not dir_path.is_dir():\n"
        "        continue\n"
        "    for candidate in sorted(dir_path.glob('*.service')):\n"
        "        path = str(candidate)\n"
        "        if path in seen_systemd_files or ignored(path):\n"
        "            continue\n"
        "        seen_systemd_files.add(path)\n"
        "        text = candidate.read_text(encoding='utf-8', errors='replace')\n"
        "        env_refs = extract_systemd_environment_files(text)\n"
        "        if env_refs:\n"
        "            systemd_services.append({'service_name': candidate.name, 'path': path, 'env_files': env_refs})\n"
        "for root in cfg['compose_roots']:\n"
        "    if ignored(root):\n"
        "        continue\n"
        "    root_path = Path(root)\n"
        "    candidates = []\n"
        "    if root_path.is_file() and is_compose_file(root_path.name):\n"
        "        candidates = [root_path]\n"
        "    elif root_path.is_dir():\n"
        "        for dirpath, _, filenames in os.walk(root_path):\n"
        "            for name in filenames:\n"
        "                if is_compose_file(name):\n"
        "                    candidates.append(Path(dirpath) / name)\n"
        "    for candidate in candidates:\n"
        "        path = str(candidate)\n"
        "        if path in seen_compose_files or ignored(path):\n"
        "            continue\n"
        "        seen_compose_files.add(path)\n"
        "        text = candidate.read_text(encoding='utf-8', errors='replace')\n"
        "        compose_services.extend(extract_compose_services(path, text))\n"
        "print(json.dumps({'env_files': sorted(env_files), 'systemd_services': systemd_services, 'compose_services': compose_services}))\n"
    )
    encoded_probe = base64.b64encode(probe.encode("utf-8")).decode("ascii")
    remote_python = shlex.quote(
        f"import base64; exec(base64.b64decode('{encoded_probe}').decode('utf-8'))"
    )
    remote_cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={timeout_seconds}",
        vps.host_alias,
    ]
    if vps.sudo:
        remote_cmd.extend(["sudo", "-n"])
    remote_cmd.extend(["python3", "-c", remote_python])
    completed = subprocess.run(
        remote_cmd,
        text=True,
        capture_output=True,
        timeout=timeout_seconds + 10,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"ssh discovery probe failed for {vps.vps_id}: {completed.stderr.strip() or completed.stdout.strip() or 'unknown error'}"
        )
    return json.loads(completed.stdout)


def _probe_vps(vps: DiscoveryVps, timeout_seconds: int) -> dict[str, object]:
    if vps.access_mode == "local":
        return _collect_local_probe(vps)
    return _remote_probe(vps, timeout_seconds)


def _make_finding(code: str, vps: DiscoveryVps, path: str, message: str, **extra: str) -> dict[str, str]:
    finding = {
        "code": code,
        "vps": vps.vps_id,
        "path": path,
        "message": message,
    }
    for key, value in extra.items():
        finding[key] = value
    return finding


def _dedupe_findings(findings: list[dict[str, str]]) -> list[dict[str, str]]:
    ordered: list[dict[str, str]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for finding in findings:
        marker = tuple(sorted((key, str(value)) for key, value in finding.items()))
        if marker in seen:
            continue
        seen.add(marker)
        ordered.append(finding)
    return ordered


def _audit_vps(vps: DiscoveryVps, timeout_seconds: int) -> dict[str, object]:
    probe = _probe_vps(vps, timeout_seconds)
    known_env_files = set(vps.known_env_files)
    known_systemd_services = set(vps.known_systemd_services)
    known_compose_targets = {(item.path, item.service_name) for item in vps.known_compose_targets}

    findings: list[dict[str, str]] = []

    for path in probe.get("env_files", []):
        if path not in known_env_files:
            findings.append(
                _make_finding(
                    "UNMAPPED_ENV_FILE",
                    vps,
                    path,
                    f"env-like file exists outside the current matrix: {path}",
                )
            )

    for item in probe.get("systemd_services", []):
        service_name = str(item.get("service_name", "")).strip()
        path = str(item.get("path", "")).strip()
        env_files = [str(entry).strip() for entry in item.get("env_files", []) if str(entry).strip()]
        if service_name and service_name not in known_systemd_services:
            findings.append(
                _make_finding(
                    "UNMAPPED_SYSTEMD_SERVICE",
                    vps,
                    path,
                    f"systemd service with EnvironmentFile is not mapped: {service_name}",
                    service=service_name,
                )
            )
        for env_file in env_files:
            if env_file not in known_env_files:
                findings.append(
                    _make_finding(
                        "UNMAPPED_ENVIRONMENT_FILE",
                        vps,
                        env_file,
                        f"EnvironmentFile reference is outside the current matrix: {env_file}",
                        source_kind="systemd-unit",
                        source=service_name or path,
                    )
                )

    for item in probe.get("compose_services", []):
        service_name = str(item.get("service_name", "")).strip()
        path = str(item.get("path", "")).strip()
        env_files = [str(entry).strip() for entry in item.get("env_files", []) if str(entry).strip()]
        if service_name and (path, service_name) not in known_compose_targets:
            findings.append(
                _make_finding(
                    "UNMAPPED_COMPOSE_SERVICE",
                    vps,
                    path,
                    f"compose service with env_file is not mapped: {service_name}",
                    service=service_name,
                )
            )
        for env_file in env_files:
            if env_file not in known_env_files:
                findings.append(
                    _make_finding(
                        "UNMAPPED_ENVIRONMENT_FILE",
                        vps,
                        env_file,
                        f"compose env_file reference is outside the current matrix: {env_file}",
                        source_kind="compose-service",
                        source=service_name or path,
                    )
                )

    findings = _dedupe_findings(findings)
    return {
        "vps": vps.vps_id,
        "label": vps.label,
        "status": "OK" if not findings else "DRIFT",
        "env_candidate_count": len(probe.get("env_files", [])),
        "systemd_candidate_count": len(probe.get("systemd_services", [])),
        "compose_candidate_count": len(probe.get("compose_services", [])),
        "finding_count": len(findings),
        "findings": findings,
    }


def generate_report(matrix_path: str | Path, requested_vps: str, timeout_seconds: int) -> dict[str, object]:
    path = Path(matrix_path)
    text = path.read_text(encoding="utf-8")
    vps_specs = _build_discovery_specs(text)
    vps_specs = apply_access_overrides(vps_specs, load_access_overrides_from_env())

    requested = requested_vps.strip().lower()
    if requested == "all":
        selected = vps_specs
    else:
        selected = [spec for spec in vps_specs if spec.vps_id == requested]
        if not selected:
            raise ValueError(f"unknown vps id: {requested_vps}")

    results = [_audit_vps(spec, timeout_seconds) for spec in selected]
    findings = [item for result in results for item in result["findings"]]
    return {
        "ok": not findings,
        "status": "OK" if not findings else "DRIFT",
        "matrix_path": str(path),
        "selected_vps": [result["vps"] for result in results],
        "finding_count": len(findings),
        "env_candidate_count": sum(int(result["env_candidate_count"]) for result in results),
        "systemd_candidate_count": sum(int(result["systemd_candidate_count"]) for result in results),
        "compose_candidate_count": sum(int(result["compose_candidate_count"]) for result in results),
        "results": results,
        "findings": findings,
    }


def render_report(report: dict[str, object], report_format: str) -> str:
    if report_format == "json":
        return json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if report_format != "markdown":
        raise ValueError(f"unsupported report format: {report_format}")

    lines = [
        "# Env Discovery Audit",
        "",
        f"- status: {report['status']}",
        f"- vps: {', '.join(report['selected_vps'])}",
        f"- findings: {report['finding_count']}",
        f"- env_candidates: {report['env_candidate_count']}",
        f"- systemd_candidates: {report['systemd_candidate_count']}",
        f"- compose_candidates: {report['compose_candidate_count']}",
        "",
        "## Findings",
    ]
    if not report["findings"]:
        lines.append("- OK")
    else:
        for finding in report["findings"]:
            suffix: list[str] = []
            if finding.get("service"):
                suffix.append(f"service={finding['service']}")
            if finding.get("source_kind"):
                suffix.append(f"source_kind={finding['source_kind']}")
            if finding.get("source"):
                suffix.append(f"source={finding['source']}")
            extra = f" {' '.join(suffix)}" if suffix else ""
            lines.append(
                f"- {finding['code']} vps={finding['vps']} path={finding['path']}{extra}"
            )

    lines.extend(["", "## By VPS"])
    for result in report["results"]:
        lines.append(
            f"- {result['status']} vps={result['vps']} env_candidates={result['env_candidate_count']} systemd_candidates={result['systemd_candidate_count']} compose_candidates={result['compose_candidate_count']} findings={result['finding_count']}"
        )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix-path", default="docs/env-scope-matrix.md")
    parser.add_argument("--vps", default="all", help="hub, next, maincua, or all")
    parser.add_argument("--report", choices=["json", "markdown"], default="json")
    parser.add_argument("--ssh-timeout-seconds", type=int, default=15)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        report = generate_report(args.matrix_path, args.vps, args.ssh_timeout_seconds)
        print(render_report(report, args.report), end="")
        return 0 if report["ok"] else 1
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        payload = {
            "ok": False,
            "error": str(exc),
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
