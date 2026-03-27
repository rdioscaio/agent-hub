#!/usr/bin/env python3
"""Audit static service wiring against docs/env-scope-matrix.md without reading secret values."""

from __future__ import annotations

import argparse
import base64
import json
import sys
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.env_audit_access import apply_access_overrides, load_access_overrides_from_env

WIRING_SPEC_RE = re.compile(
    r"## Wiring Spec\s+```json\s*(?P<payload>.*?)\s*```",
    re.DOTALL,
)
TABLE_REQUIRED_HEADERS = {"vps", "target", "kind", "path", "mutation rule"}
VALID_ACCESS_MODES = {"local", "ssh"}
VALID_TARGET_KINDS = {"systemd-unit", "symlink", "path-patterns", "compose-env-file"}


@dataclass(frozen=True)
class TargetSpec:
    name: str
    kind: str
    path: str
    mutation_rule: str
    must_exist: bool
    service_name: str
    expected_environment_files: tuple[str, ...]
    allow_extra_environment_files: bool
    expected_target: str
    required_patterns: tuple[str, ...]


@dataclass(frozen=True)
class VpsSpec:
    vps_id: str
    label: str
    access_mode: str
    host_alias: str
    sudo: bool
    targets: tuple[TargetSpec, ...]


class ProbeExecutionError(RuntimeError):
    """Raised when transport fails before a target can be audited."""


def _normalize_cell(value: str) -> str:
    return value.strip().strip("`").strip()


def _normalize_contract_text(value: str) -> str:
    return " ".join(value.replace("`", "").split())


def _parse_markdown_tables(text: str) -> list[dict[str, object]]:
    tables: list[dict[str, object]] = []
    lines = text.splitlines()
    idx = 0
    while idx < len(lines):
        line = lines[idx].strip()
        if not line.startswith("|"):
            idx += 1
            continue
        block: list[str] = []
        while idx < len(lines) and lines[idx].strip().startswith("|"):
            block.append(lines[idx].rstrip())
            idx += 1
        if len(block) < 2:
            continue
        separator = block[1].replace("|", "").replace(":", "").replace("-", "").strip()
        if separator:
            continue
        headers = [_normalize_cell(cell) for cell in block[0].strip().strip("|").split("|")]
        rows: list[dict[str, str]] = []
        for raw_row in block[2:]:
            cells = [_normalize_cell(cell) for cell in raw_row.strip().strip("|").split("|")]
            if len(cells) != len(headers):
                continue
            rows.append(dict(zip(headers, cells)))
        tables.append({"headers": headers, "rows": rows})
    return tables


def _parse_wiring_table(text: str) -> dict[tuple[str, str, str], dict[str, str]]:
    inventory: dict[tuple[str, str, str], dict[str, str]] = {}
    for table in _parse_markdown_tables(text):
        headers = {str(item).lower() for item in table["headers"]}
        if not TABLE_REQUIRED_HEADERS.issubset(headers):
            continue
        for row in table["rows"]:
            vps = row.get("VPS", "")
            target = row.get("Target", "")
            path = row.get("Path", "")
            kind = row.get("Kind", "")
            mutation_rule = row.get("Mutation rule", "")
            if not vps or not target or not path:
                continue
            inventory[(vps, target, path)] = {
                "kind": kind,
                "mutation_rule": mutation_rule,
            }
    return inventory


def _parse_wiring_spec(text: str) -> dict:
    match = WIRING_SPEC_RE.search(text)
    if not match:
        raise ValueError("missing '## Wiring Spec' JSON block in matrix document")
    try:
        payload = json.loads(match.group("payload"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid wiring spec JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("wiring spec must be a JSON object")
    return payload


def _normalize_names(items: object) -> tuple[str, ...]:
    if items is None:
        return ()
    if not isinstance(items, list):
        raise ValueError("name list must be a JSON array")
    result: list[str] = []
    for item in items:
        value = str(item).strip()
        if not value:
            raise ValueError("name list contains an empty item")
        if value not in result:
            result.append(value)
    return tuple(result)


def _load_vps_specs(text: str) -> list[VpsSpec]:
    spec = _parse_wiring_spec(text)
    raw_vps = spec.get("vps")
    if not isinstance(raw_vps, list):
        raise ValueError("'vps' must be a JSON array")

    table_inventory = _parse_wiring_table(text)
    seen_ids: set[str] = set()
    seen_labels: set[str] = set()
    vps_specs: list[VpsSpec] = []

    for index, raw in enumerate(raw_vps, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"vps entry #{index} must be a JSON object")
        vps_id = str(raw.get("id", "")).strip()
        label = str(raw.get("label", "")).strip()
        if not vps_id:
            raise ValueError(f"vps entry #{index} is missing 'id'")
        if not label:
            raise ValueError(f"vps entry #{index} is missing 'label'")
        if vps_id in seen_ids:
            raise ValueError(f"duplicate vps id: {vps_id}")
        if label in seen_labels:
            raise ValueError(f"duplicate vps label: {label}")
        seen_ids.add(vps_id)
        seen_labels.add(label)

        access = raw.get("access") or {}
        if not isinstance(access, dict):
            raise ValueError(f"vps {vps_id} has invalid 'access'")
        access_mode = str(access.get("mode", "")).strip()
        if access_mode not in VALID_ACCESS_MODES:
            raise ValueError(f"vps {vps_id} has invalid access mode: {access_mode}")
        host_alias = str(access.get("host_alias", "")).strip()
        sudo = bool(access.get("sudo", False))
        if access_mode == "ssh" and not host_alias:
            raise ValueError(f"vps {vps_id} requires host_alias for ssh mode")

        raw_targets = raw.get("targets")
        if not isinstance(raw_targets, list) or not raw_targets:
            raise ValueError(f"vps {vps_id} must define a non-empty 'targets' array")

        targets: list[TargetSpec] = []
        seen_paths: set[str] = set()
        seen_names: set[str] = set()
        for target_index, raw_target in enumerate(raw_targets, start=1):
            if not isinstance(raw_target, dict):
                raise ValueError(f"vps {vps_id} target #{target_index} must be a JSON object")
            name = str(raw_target.get("name", "")).strip()
            kind = str(raw_target.get("kind", "")).strip()
            path = str(raw_target.get("path", "")).strip()
            mutation_rule = str(raw_target.get("mutation_rule", "")).strip()
            must_exist = bool(raw_target.get("must_exist", True))
            if not name:
                raise ValueError(f"vps {vps_id} target #{target_index} is missing 'name'")
            if not kind:
                raise ValueError(f"vps {vps_id} target {name} is missing 'kind'")
            if kind not in VALID_TARGET_KINDS:
                raise ValueError(f"vps {vps_id} target {name} has invalid kind: {kind}")
            if not path:
                raise ValueError(f"vps {vps_id} target {name} is missing 'path'")
            if not mutation_rule:
                raise ValueError(f"vps {vps_id} target {name} is missing 'mutation_rule'")
            if name in seen_names:
                raise ValueError(f"vps {vps_id} has duplicate target name {name}")
            if path in seen_paths:
                raise ValueError(f"vps {vps_id} has duplicate target path {path}")
            seen_names.add(name)
            seen_paths.add(path)

            table_row = table_inventory.get((label, name, path))
            if table_row is None:
                raise ValueError(f"vps {vps_id} target {name} is missing from the human wiring table")
            if table_row["kind"] != kind:
                raise ValueError(
                    f"vps {vps_id} target {name} kind mismatch between wiring spec ({kind}) and matrix table ({table_row['kind']})"
                )
            if _normalize_contract_text(table_row["mutation_rule"]) != _normalize_contract_text(mutation_rule):
                raise ValueError(
                    f"vps {vps_id} target {name} mutation_rule mismatch between wiring spec and matrix table"
                )

            service_name = str(raw_target.get("service_name", "")).strip()
            expected_environment_files = _normalize_names(raw_target.get("expected_environment_files", []))
            allow_extra_environment_files = bool(raw_target.get("allow_extra_environment_files", False))
            expected_target = str(raw_target.get("expected_target", "")).strip()
            required_patterns = _normalize_names(raw_target.get("required_patterns", []))

            if kind in {"systemd-unit", "compose-env-file"}:
                if not service_name:
                    raise ValueError(f"vps {vps_id} target {name} requires 'service_name'")
                if not expected_environment_files:
                    raise ValueError(f"vps {vps_id} target {name} requires 'expected_environment_files'")
            if kind == "symlink" and not expected_target:
                raise ValueError(f"vps {vps_id} target {name} requires 'expected_target'")
            if kind == "path-patterns" and not required_patterns:
                raise ValueError(f"vps {vps_id} target {name} requires 'required_patterns'")

            targets.append(
                TargetSpec(
                    name=name,
                    kind=kind,
                    path=path,
                    mutation_rule=mutation_rule,
                    must_exist=must_exist,
                    service_name=service_name,
                    expected_environment_files=expected_environment_files,
                    allow_extra_environment_files=allow_extra_environment_files,
                    expected_target=expected_target,
                    required_patterns=required_patterns,
                )
            )

        vps_specs.append(
            VpsSpec(
                vps_id=vps_id,
                label=label,
                access_mode=access_mode,
                host_alias=host_alias,
                sudo=sudo,
                targets=tuple(targets),
            )
        )
    return vps_specs


def _run_command(vps: VpsSpec, argv: list[str], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    if vps.access_mode == "local":
        return subprocess.run(
            argv,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )

    remote_cmd = " ".join(shlex.quote(part) for part in argv)
    if vps.sudo:
        remote_cmd = f"sudo -n {remote_cmd}"
    completed = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={timeout_seconds}",
            vps.host_alias,
            remote_cmd,
        ],
        text=True,
        capture_output=True,
        timeout=timeout_seconds + 10,
        check=False,
    )
    if completed.returncode == 255:
        raise ProbeExecutionError(
            f"ssh transport failed for {vps.vps_id}: {completed.stderr.strip() or completed.stdout.strip() or 'unknown error'}"
        )
    return completed


def _read_path_payload(vps: VpsSpec, path: str, timeout_seconds: int) -> dict[str, object]:
    if vps.access_mode == "local":
        target = Path(path)
        payload = {
            "exists": target.exists(),
            "is_file": target.is_file(),
            "is_symlink": target.is_symlink(),
            "resolved": "",
            "text_b64": "",
            "error": "",
        }
        try:
            if target.exists():
                payload["resolved"] = str(target.resolve())
            if target.is_file():
                payload["text_b64"] = base64.b64encode(target.read_bytes()).decode("ascii")
        except Exception as exc:
            payload["error"] = f"{type(exc).__name__}: {exc}"
        return payload

    script = (
        "import base64\n"
        "import json\n"
        "from pathlib import Path\n"
        f"path = Path({path!r})\n"
        "payload = {\n"
        "    'exists': path.exists(),\n"
        "    'is_file': path.is_file(),\n"
        "    'is_symlink': path.is_symlink(),\n"
        "    'resolved': '',\n"
        "    'text_b64': '',\n"
        "    'error': '',\n"
        "}\n"
        "try:\n"
        "    if path.exists():\n"
        "        payload['resolved'] = str(path.resolve())\n"
        "    if path.is_file():\n"
        "        payload['text_b64'] = base64.b64encode(path.read_bytes()).decode('ascii')\n"
        "except Exception as exc:\n"
        "    payload['error'] = f'{type(exc).__name__}: {exc}'\n"
        "print(json.dumps(payload))\n"
    )
    completed = _run_command(vps, ["python3", "-c", script], timeout_seconds)
    if completed.returncode != 0:
        raise ProbeExecutionError(
            f"path probe failed for {vps.vps_id} {path}: {completed.stderr.strip() or completed.stdout.strip() or 'unknown error'}"
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeExecutionError(f"invalid path probe payload for {vps.vps_id} {path}: {exc}") from exc


def _extract_systemd_source_paths(text: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("# "):
            continue
        candidate = line[2:].strip()
        if not candidate.startswith("/"):
            continue
        if candidate not in seen:
            seen.add(candidate)
            result.append(candidate)
    return result


def _extract_systemd_environment_files(text: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("EnvironmentFile="):
            continue
        candidate = line.split("=", 1)[1].strip()
        if candidate not in seen:
            seen.add(candidate)
            result.append(candidate)
    return result


def _extract_compose_env_files(text: str, service_name: str) -> list[str] | None:
    lines = text.splitlines()
    in_services = False
    services_indent = 0
    current_service: str | None = None
    env_file_indent: int | None = None
    saw_target_service = False
    result: list[str] = []

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
            if stripped == "services:":
                continue
            in_services = False
            continue

        if indent == services_indent + 2 and stripped.endswith(":"):
            if saw_target_service and current_service == service_name:
                return result
            current_service = stripped[:-1]
            if current_service == service_name:
                saw_target_service = True
            env_file_indent = None
            result = []
            continue

        if current_service != service_name:
            continue

        if env_file_indent is not None:
            if indent <= env_file_indent:
                env_file_indent = None
            elif stripped.startswith("- "):
                value = stripped[2:].strip().strip("'").strip('"')
                result.append(value)
                continue
            else:
                env_file_indent = None

        if indent == services_indent + 4 and stripped.startswith("env_file:"):
            inline = stripped[len("env_file:"):].strip()
            if inline:
                result = [inline.strip().strip("'").strip('"')]
                return result
            env_file_indent = indent
            result = []

        if current_service == service_name and indent == services_indent + 2 and stripped.endswith(":"):
            return result

    if saw_target_service:
        return result
    return None


def _make_finding(
    code: str,
    vps: VpsSpec,
    target: TargetSpec,
    message: str,
) -> dict[str, str]:
    return {
        "code": code,
        "vps": vps.vps_id,
        "target": target.name,
        "kind": target.kind,
        "path": target.path,
        "message": message,
    }


def _audit_systemd_state(
    vps: VpsSpec,
    target: TargetSpec,
    raw_text: str,
    error: str,
    exists: bool,
) -> list[dict[str, str]]:
    if not exists:
        return [_make_finding("SERVICE_NOT_MAPPED", vps, target, error or f"systemd service not found: {target.service_name}")]

    findings: list[dict[str, str]] = []
    source_paths = _extract_systemd_source_paths(raw_text)
    env_files = _extract_systemd_environment_files(raw_text)

    if target.path not in source_paths:
        findings.append(
            _make_finding(
                "WIRING_PATH_MISMATCH",
                vps,
                target,
                f"expected unit source path missing from systemctl cat output: {target.path}",
            )
        )

    missing = [item for item in target.expected_environment_files if item not in env_files]
    for item in missing:
        findings.append(
            _make_finding(
                "MISSING_ENVIRONMENT_FILE",
                vps,
                target,
                f"expected EnvironmentFile missing from {target.service_name}: {item}",
            )
        )

    ordered = [item for item in env_files if item in target.expected_environment_files]
    if not missing and ordered != list(target.expected_environment_files):
        findings.append(
            _make_finding(
                "UNEXPECTED_ENVIRONMENT_FILE_ORDER",
                vps,
                target,
                f"EnvironmentFile order mismatch for {target.service_name}: expected {list(target.expected_environment_files)} got {ordered}",
            )
        )

    if not target.allow_extra_environment_files:
        extras = [item for item in env_files if item not in target.expected_environment_files]
        if extras:
            findings.append(
                _make_finding(
                    "WIRING_PATH_MISMATCH",
                    vps,
                    target,
                    f"unexpected EnvironmentFile entries in {target.service_name}: {extras}",
                )
            )

    return findings


def _audit_symlink_state(
    vps: VpsSpec,
    target: TargetSpec,
    payload: dict[str, object],
) -> list[dict[str, str]]:
    exists = bool(payload.get("exists"))
    if target.must_exist and not exists:
        return [_make_finding("WIRING_PATH_MISMATCH", vps, target, f"expected symlink path is missing: {target.path}")]
    if not exists:
        return []

    if str(payload.get("error", "")).strip():
        return [_make_finding("WIRING_PATH_MISMATCH", vps, target, f"symlink probe failed: {payload['error']}")]
    if not bool(payload.get("is_symlink")):
        return [_make_finding("WIRING_PATH_MISMATCH", vps, target, f"expected symlink but found another file type: {target.path}")]

    resolved = str(payload.get("resolved", "")).strip()
    if resolved != target.expected_target:
        return [
            _make_finding(
                "UNEXPECTED_SYMLINK_TARGET",
                vps,
                target,
                f"symlink target mismatch for {target.path}: expected {target.expected_target} got {resolved or '<empty>'}",
            )
        ]
    return []


def _audit_pattern_state(
    vps: VpsSpec,
    target: TargetSpec,
    payload: dict[str, object],
) -> list[dict[str, str]]:
    exists = bool(payload.get("exists"))
    if target.must_exist and not exists:
        return [_make_finding("WIRING_PATH_MISMATCH", vps, target, f"expected file path is missing: {target.path}")]
    if not exists:
        return []
    if str(payload.get("error", "")).strip():
        return [_make_finding("WIRING_PATH_MISMATCH", vps, target, f"file probe failed: {payload['error']}")]
    if not bool(payload.get("is_file")):
        return [_make_finding("WIRING_PATH_MISMATCH", vps, target, f"expected regular file but found another file type: {target.path}")]

    text_b64 = str(payload.get("text_b64", ""))
    text = base64.b64decode(text_b64).decode("utf-8", errors="replace")
    findings: list[dict[str, str]] = []
    for pattern in target.required_patterns:
        if pattern not in text:
            findings.append(
                _make_finding(
                    "WIRING_PATH_MISMATCH",
                    vps,
                    target,
                    f"required wiring pattern missing from {target.path}: {pattern}",
                )
            )
    return findings


def _audit_compose_state(
    vps: VpsSpec,
    target: TargetSpec,
    payload: dict[str, object],
) -> list[dict[str, str]]:
    exists = bool(payload.get("exists"))
    if target.must_exist and not exists:
        return [_make_finding("WIRING_PATH_MISMATCH", vps, target, f"expected compose file is missing: {target.path}")]
    if not exists:
        return []
    if str(payload.get("error", "")).strip():
        return [_make_finding("WIRING_PATH_MISMATCH", vps, target, f"compose probe failed: {payload['error']}")]
    if not bool(payload.get("is_file")):
        return [_make_finding("WIRING_PATH_MISMATCH", vps, target, f"expected compose file but found another file type: {target.path}")]

    text_b64 = str(payload.get("text_b64", ""))
    text = base64.b64decode(text_b64).decode("utf-8", errors="replace")
    env_files = _extract_compose_env_files(text, target.service_name)
    if env_files is None:
        return [_make_finding("SERVICE_NOT_MAPPED", vps, target, f"compose service not found in {target.path}: {target.service_name}")]

    findings: list[dict[str, str]] = []
    missing = [item for item in target.expected_environment_files if item not in env_files]
    for item in missing:
        findings.append(
            _make_finding(
                "MISSING_ENVIRONMENT_FILE",
                vps,
                target,
                f"expected env_file missing from compose service {target.service_name}: {item}",
            )
        )

    ordered = [item for item in env_files if item in target.expected_environment_files]
    if not missing and ordered != list(target.expected_environment_files):
        findings.append(
            _make_finding(
                "UNEXPECTED_ENVIRONMENT_FILE_ORDER",
                vps,
                target,
                f"env_file order mismatch for compose service {target.service_name}: expected {list(target.expected_environment_files)} got {ordered}",
            )
        )

    if not target.allow_extra_environment_files:
        extras = [item for item in env_files if item not in target.expected_environment_files]
        if extras:
            findings.append(
                _make_finding(
                    "WIRING_PATH_MISMATCH",
                    vps,
                    target,
                    f"unexpected env_file entries in compose service {target.service_name}: {extras}",
                )
            )

    return findings


def _audit_target(vps: VpsSpec, target: TargetSpec, timeout_seconds: int) -> list[dict[str, str]]:
    if target.kind == "systemd-unit":
        completed = _run_command(vps, ["systemctl", "cat", target.service_name], timeout_seconds)
        exists = completed.returncode == 0
        error = ""
        if not exists:
            error = completed.stderr.strip() or completed.stdout.strip()
        raw_text = completed.stdout if exists else ""
        return _audit_systemd_state(vps, target, raw_text, error, exists)

    payload = _read_path_payload(vps, target.path, timeout_seconds)
    if target.kind == "symlink":
        return _audit_symlink_state(vps, target, payload)
    if target.kind == "path-patterns":
        return _audit_pattern_state(vps, target, payload)
    if target.kind == "compose-env-file":
        return _audit_compose_state(vps, target, payload)
    raise ValueError(f"unsupported target kind: {target.kind}")


def _audit_vps(vps: VpsSpec, timeout_seconds: int) -> dict:
    findings: list[dict[str, str]] = []
    target_results: list[dict[str, object]] = []

    for target in vps.targets:
        target_findings = _audit_target(vps, target, timeout_seconds)
        findings.extend(target_findings)
        target_results.append(
            {
                "target": target.name,
                "kind": target.kind,
                "path": target.path,
                "status": "OK" if not target_findings else "DRIFT",
                "findings": target_findings,
            }
        )

    return {
        "vps": vps.vps_id,
        "label": vps.label,
        "status": "OK" if not findings else "DRIFT",
        "target_count": len(vps.targets),
        "finding_count": len(findings),
        "targets": target_results,
        "findings": findings,
    }


def _render_markdown(report: dict) -> str:
    lines = [
        "# Env Wiring Audit",
        "",
        f"- status: {report['status']}",
        f"- vps: {', '.join(report['selected_vps'])}",
        f"- findings: {report['finding_count']}",
        f"- monitored_targets: {report['target_count']}",
        "",
        "## Findings",
    ]
    if not report["findings"]:
        lines.append("- OK")
    else:
        for finding in report["findings"]:
            lines.append(
                f"- {finding['code']} vps={finding['vps']} target={finding['target']} kind={finding['kind']} path={finding['path']}"
            )
    lines.extend(["", "## Targets"])
    for result in report["results"]:
        for item in result["targets"]:
            lines.append(
                f"- {item['status']} vps={result['vps']} target={item['target']} kind={item['kind']} path={item['path']}"
            )
    return "\n".join(lines) + "\n"


def generate_report(matrix_path: str | Path, requested_vps: str, timeout_seconds: int) -> dict:
    path = Path(matrix_path)
    text = path.read_text(encoding="utf-8")
    vps_specs = _load_vps_specs(text)
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
        "target_count": sum(result["target_count"] for result in results),
        "finding_count": len(findings),
        "results": results,
        "findings": findings,
    }


def render_report(report: dict, report_format: str) -> str:
    if report_format == "markdown":
        return _render_markdown(report)
    if report_format == "json":
        return json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    raise ValueError(f"unsupported report format: {report_format}")


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
    except (OSError, ValueError, ProbeExecutionError, subprocess.TimeoutExpired) as exc:
        payload = {
            "ok": False,
            "error": str(exc),
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
