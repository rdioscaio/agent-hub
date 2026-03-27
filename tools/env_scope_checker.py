#!/usr/bin/env python3
"""Audit env-file placement against docs/env-scope-matrix.md without reading values."""

from __future__ import annotations

import argparse
import base64
import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.env_audit_access import apply_access_overrides, load_access_overrides_from_env

CHECKER_SPEC_RE = re.compile(
    r"## Checker Spec\s+```json\s*(?P<payload>.*?)\s*```",
    re.DOTALL,
)
TABLE_REQUIRED_HEADERS = {"vps", "file", "scope", "mutation rule"}
VALID_ACCESS_MODES = {"local", "ssh"}


@dataclass(frozen=True)
class FileSpec:
    path: str
    scope: str
    mutation_rule: str
    must_exist: bool
    strict_allowlist: bool
    required_vars: tuple[str, ...]
    allowed_vars: tuple[str, ...]
    forbidden_vars: tuple[str, ...]


@dataclass(frozen=True)
class VpsSpec:
    vps_id: str
    label: str
    access_mode: str
    host_alias: str
    sudo: bool
    discovery_paths: tuple[str, ...]
    discovery_roots: tuple[str, ...]
    ignore_globs: tuple[str, ...]
    files: tuple[FileSpec, ...]


@dataclass(frozen=True)
class FileSnapshot:
    exists: bool
    keys: tuple[str, ...]
    error: str


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


def _parse_table_inventory(text: str) -> dict[tuple[str, str], dict[str, str]]:
    inventory: dict[tuple[str, str], dict[str, str]] = {}
    for table in _parse_markdown_tables(text):
        headers = {str(item).lower() for item in table["headers"]}
        if not TABLE_REQUIRED_HEADERS.issubset(headers):
            continue
        for row in table["rows"]:
            vps = row.get("VPS", "")
            path = row.get("File", "")
            scope = row.get("Scope", "")
            mutation_rule = row.get("Mutation rule", "")
            if not vps or not path:
                continue
            inventory[(vps, path)] = {
                "scope": scope,
                "mutation_rule": mutation_rule,
            }
    return inventory


def _parse_checker_spec(text: str) -> dict:
    match = CHECKER_SPEC_RE.search(text)
    if not match:
        raise ValueError("missing '## Checker Spec' JSON block in matrix document")
    try:
        payload = json.loads(match.group("payload"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid checker spec JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("checker spec must be a JSON object")
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
    spec = _parse_checker_spec(text)
    raw_vps = spec.get("vps")
    if not isinstance(raw_vps, list):
        raise ValueError("'vps' must be a JSON array")

    table_inventory = _parse_table_inventory(text)
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

        discovery = raw.get("discovery") or {}
        if not isinstance(discovery, dict):
            raise ValueError(f"vps {vps_id} has invalid 'discovery'")
        discovery_paths = _normalize_names(discovery.get("paths", []))
        discovery_roots = _normalize_names(discovery.get("roots", []))
        ignore_globs = _normalize_names(discovery.get("ignore_globs", []))

        raw_files = raw.get("files")
        if not isinstance(raw_files, list) or not raw_files:
            raise ValueError(f"vps {vps_id} must define a non-empty 'files' array")

        files: list[FileSpec] = []
        seen_paths: set[str] = set()
        for file_index, raw_file in enumerate(raw_files, start=1):
            if not isinstance(raw_file, dict):
                raise ValueError(f"vps {vps_id} file #{file_index} must be a JSON object")
            path = str(raw_file.get("path", "")).strip()
            scope = str(raw_file.get("scope", "")).strip()
            mutation_rule = str(raw_file.get("mutation_rule", "")).strip()
            if not path:
                raise ValueError(f"vps {vps_id} file #{file_index} is missing 'path'")
            if not scope:
                raise ValueError(f"vps {vps_id} file {path} is missing 'scope'")
            if not mutation_rule:
                raise ValueError(f"vps {vps_id} file {path} is missing 'mutation_rule'")
            if path in seen_paths:
                raise ValueError(f"vps {vps_id} has duplicate file path {path}")
            seen_paths.add(path)

            table_row = table_inventory.get((label, path))
            if table_row is None:
                raise ValueError(f"vps {vps_id} file {path} is missing from the human matrix table")
            if table_row["scope"] != scope:
                raise ValueError(
                    f"vps {vps_id} file {path} scope mismatch between checker spec ({scope}) and matrix table ({table_row['scope']})"
                )
            if _normalize_contract_text(table_row["mutation_rule"]) != _normalize_contract_text(mutation_rule):
                raise ValueError(
                    f"vps {vps_id} file {path} mutation_rule mismatch between checker spec and matrix table"
                )

            allowed_vars = _normalize_names(raw_file.get("allowed_vars", []))
            required_vars = _normalize_names(raw_file.get("required_vars", []))
            forbidden_vars = _normalize_names(raw_file.get("forbidden_vars", []))
            strict_allowlist = bool(raw_file.get("strict_allowlist", bool(allowed_vars)))
            must_exist = bool(raw_file.get("must_exist", True))

            if required_vars and not allowed_vars:
                raise ValueError(f"vps {vps_id} file {path} declares required_vars without allowed_vars")
            if not set(required_vars).issubset(set(allowed_vars)):
                raise ValueError(f"vps {vps_id} file {path} has required_vars outside allowed_vars")

            files.append(
                FileSpec(
                    path=path,
                    scope=scope,
                    mutation_rule=mutation_rule,
                    must_exist=must_exist,
                    strict_allowlist=strict_allowlist,
                    required_vars=required_vars,
                    allowed_vars=allowed_vars,
                    forbidden_vars=forbidden_vars,
                )
            )

        vps_specs.append(
            VpsSpec(
                vps_id=vps_id,
                label=label,
                access_mode=access_mode,
                host_alias=host_alias,
                sudo=sudo,
                discovery_paths=discovery_paths,
                discovery_roots=discovery_roots,
                ignore_globs=ignore_globs,
                files=tuple(files),
            )
        )
    return vps_specs


def _matches_ignore(path: str, ignore_globs: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in ignore_globs)


def _collect_local_probe(vps: VpsSpec) -> dict:
    monitored_paths = {file_spec.path for file_spec in vps.files}
    discovered: set[str] = set()

    for raw in vps.discovery_paths:
        if _matches_ignore(raw, vps.ignore_globs):
            continue
        path = Path(raw)
        if path.exists():
            discovered.add(str(path))

    for root in vps.discovery_roots:
        if _matches_ignore(root, vps.ignore_globs):
            continue
        root_path = Path(root)
        if root_path.is_file() and root_path.name.endswith(".env"):
            discovered.add(str(root_path))
            continue
        if not root_path.is_dir():
            continue
        for dirpath, _, filenames in os.walk(root_path):
            for name in filenames:
                if not name.endswith(".env"):
                    continue
                candidate = str(Path(dirpath) / name)
                if not _matches_ignore(candidate, vps.ignore_globs):
                    discovered.add(candidate)

    snapshots: dict[str, FileSnapshot] = {}
    for raw in sorted(monitored_paths | discovered):
        path = Path(raw)
        exists = path.exists()
        keys: list[str] = []
        error = ""
        if exists and path.is_file():
            try:
                keys = _parse_env_keys(path.read_text(encoding="utf-8", errors="replace"))
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
        snapshots[raw] = FileSnapshot(exists=exists, keys=tuple(keys), error=error)
    return {
        "discovered_paths": sorted(discovered),
        "snapshots": snapshots,
    }


def _parse_env_keys(text: str) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key = line.split("=", 1)[0].strip()
        if not key or key in seen:
            continue
        seen.add(key)
        keys.append(key)
    return keys


def _remote_probe(vps: VpsSpec, timeout_seconds: int) -> dict:
    payload = json.dumps({
        "paths": list(vps.discovery_paths),
        "roots": list(vps.discovery_roots),
        "ignore_globs": list(vps.ignore_globs),
        "monitored_paths": [file_spec.path for file_spec in vps.files],
    })
    probe = (
        "import fnmatch\n"
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        f"cfg = json.loads({payload!r})\n"
        "def ignored(path):\n"
        "    return any(fnmatch.fnmatch(path, pattern) for pattern in cfg['ignore_globs'])\n"
        "def parse_keys(text):\n"
        "    keys = []\n"
        "    seen = set()\n"
        "    for raw_line in text.splitlines():\n"
        "        line = raw_line.strip()\n"
        "        if not line or line.startswith('#') or '=' not in line:\n"
        "            continue\n"
        "        if line.startswith('export '):\n"
        "            line = line[len('export '):].strip()\n"
        "        key = line.split('=', 1)[0].strip()\n"
        "        if not key or key in seen:\n"
        "            continue\n"
        "        seen.add(key)\n"
        "        keys.append(key)\n"
        "    return keys\n"
        "discovered = set()\n"
        "for raw in cfg['paths']:\n"
        "    if ignored(raw):\n"
        "        continue\n"
        "    path = Path(raw)\n"
        "    if path.exists():\n"
        "        discovered.add(str(path))\n"
        "for root in cfg['roots']:\n"
        "    if ignored(root):\n"
        "        continue\n"
        "    root_path = Path(root)\n"
        "    if root_path.is_file() and root_path.name.endswith('.env'):\n"
        "        discovered.add(str(root_path))\n"
        "        continue\n"
        "    if not root_path.is_dir():\n"
        "        continue\n"
        "    for dirpath, _, filenames in os.walk(root_path):\n"
        "        for name in filenames:\n"
        "            if not name.endswith('.env'):\n"
        "                continue\n"
        "            candidate = str(Path(dirpath) / name)\n"
        "            if not ignored(candidate):\n"
        "                discovered.add(candidate)\n"
        "snapshots = {}\n"
        "for raw in sorted(set(cfg['monitored_paths']) | discovered):\n"
        "    path = Path(raw)\n"
        "    entry = {'exists': path.exists(), 'keys': [], 'error': ''}\n"
        "    if entry['exists'] and path.is_file():\n"
        "        try:\n"
        "            entry['keys'] = parse_keys(path.read_text(encoding='utf-8', errors='replace'))\n"
        "        except Exception as exc:\n"
        "            entry['error'] = f'{type(exc).__name__}: {exc}'\n"
        "    snapshots[raw] = entry\n"
        "print(json.dumps({'discovered_paths': sorted(discovered), 'snapshots': snapshots}))\n"
    )
    remote_cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={timeout_seconds}",
        vps.host_alias,
    ]
    encoded_probe = base64.b64encode(probe.encode("utf-8")).decode("ascii")
    remote_python = shlex.quote(
        f"import base64; exec(base64.b64decode('{encoded_probe}').decode('utf-8'))"
    )
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
            f"ssh probe failed for {vps.vps_id}: {completed.stderr.strip() or completed.stdout.strip() or 'unknown error'}"
        )
    raw = json.loads(completed.stdout)
    snapshots = {
        path: FileSnapshot(
            exists=bool(item.get("exists")),
            keys=tuple(str(key) for key in item.get("keys", [])),
            error=str(item.get("error", "")),
        )
        for path, item in raw.get("snapshots", {}).items()
    }
    return {
        "discovered_paths": list(raw.get("discovered_paths", [])),
        "snapshots": snapshots,
    }


def _probe_vps(vps: VpsSpec, timeout_seconds: int) -> dict:
    if vps.access_mode == "local":
        return _collect_local_probe(vps)
    return _remote_probe(vps, timeout_seconds)


def _build_declared_map(vps: VpsSpec) -> dict[str, list[dict[str, str]]]:
    declared: dict[str, list[dict[str, str]]] = {}
    for file_spec in vps.files:
        names = set(file_spec.allowed_vars) | set(file_spec.required_vars)
        for name in names:
            declared.setdefault(name, []).append({
                "path": file_spec.path,
                "scope": file_spec.scope,
            })
    return declared


def _audit_vps(vps: VpsSpec, timeout_seconds: int) -> dict:
    probe = _probe_vps(vps, timeout_seconds)
    snapshots: dict[str, FileSnapshot] = probe["snapshots"]
    discovered_paths = set(probe["discovered_paths"])
    declared_map = _build_declared_map(vps)
    monitored_paths = {file_spec.path for file_spec in vps.files}

    findings: list[dict[str, str]] = []
    file_results: list[dict[str, object]] = []

    for unknown_path in sorted(discovered_paths - monitored_paths):
        findings.append({
            "code": "UNKNOWN_PATH",
            "vps": vps.vps_id,
            "path": unknown_path,
            "scope": "",
            "variable": "",
            "message": f"discovered env file outside matrix scope: {unknown_path}",
        })

    for file_spec in vps.files:
        snapshot = snapshots.get(file_spec.path, FileSnapshot(exists=False, keys=(), error=""))
        file_findings: list[dict[str, str]] = []
        present = set(snapshot.keys)

        if file_spec.must_exist and not snapshot.exists:
            file_findings.append({
                "code": "MISSING_ALLOWED_ENTRY",
                "vps": vps.vps_id,
                "path": file_spec.path,
                "scope": file_spec.scope,
                "variable": "",
                "message": f"listed file is missing: {file_spec.path}",
            })
        elif snapshot.error:
            file_findings.append({
                "code": "MISSING_ALLOWED_ENTRY",
                "vps": vps.vps_id,
                "path": file_spec.path,
                "scope": file_spec.scope,
                "variable": "",
                "message": f"listed file could not be parsed: {snapshot.error}",
            })

        for name in file_spec.required_vars:
            if name not in present:
                file_findings.append({
                    "code": "MISSING_ALLOWED_ENTRY",
                    "vps": vps.vps_id,
                    "path": file_spec.path,
                    "scope": file_spec.scope,
                    "variable": name,
                    "message": f"required variable missing from {file_spec.path}: {name}",
                })

        for name in snapshot.keys:
            if name in file_spec.forbidden_vars:
                file_findings.append({
                    "code": "FORBIDDEN_IN_SCOPE",
                    "vps": vps.vps_id,
                    "path": file_spec.path,
                    "scope": file_spec.scope,
                    "variable": name,
                    "message": f"forbidden variable in {file_spec.path}: {name}",
                })
                continue
            if name in file_spec.allowed_vars:
                continue
            declared_elsewhere = [
                item for item in declared_map.get(name, [])
                if item["path"] != file_spec.path
            ]
            if declared_elsewhere:
                leaks_into_shared_scope = any(item["scope"] != "app-local" for item in declared_elsewhere)
                current_is_non_app_local = file_spec.scope != "app-local"
            else:
                leaks_into_shared_scope = False
                current_is_non_app_local = file_spec.scope != "app-local"
            if declared_elsewhere and (leaks_into_shared_scope or current_is_non_app_local):
                file_findings.append({
                    "code": "FORBIDDEN_IN_SCOPE",
                    "vps": vps.vps_id,
                    "path": file_spec.path,
                    "scope": file_spec.scope,
                    "variable": name,
                    "message": f"variable declared for another monitored scope appears in {file_spec.path}: {name}",
                })
                continue
            if file_spec.strict_allowlist:
                file_findings.append({
                    "code": "UNDECLARED_VARIABLE",
                    "vps": vps.vps_id,
                    "path": file_spec.path,
                    "scope": file_spec.scope,
                    "variable": name,
                    "message": f"undeclared variable in strict file {file_spec.path}: {name}",
                })

        findings.extend(file_findings)
        file_results.append({
            "path": file_spec.path,
            "scope": file_spec.scope,
            "status": "OK" if not file_findings else "DRIFT",
            "findings": file_findings,
        })

    return {
        "vps": vps.vps_id,
        "label": vps.label,
        "status": "OK" if not findings else "DRIFT",
        "file_count": len(vps.files),
        "discovered_count": len(discovered_paths),
        "finding_count": len(findings),
        "files": file_results,
        "findings": findings,
    }


def _render_markdown(report: dict) -> str:
    lines = [
        "# Env Scope Audit",
        "",
        f"- status: {report['status']}",
        f"- vps: {', '.join(report['selected_vps'])}",
        f"- findings: {report['finding_count']}",
        f"- monitored_files: {report['file_count']}",
        f"- discovered_files: {report['discovered_count']}",
        "",
        "## Findings",
    ]
    if not report["findings"]:
        lines.append("- OK")
    else:
        for finding in report["findings"]:
            suffix = f" variable={finding['variable']}" if finding["variable"] else ""
            scope = f" scope={finding['scope']}" if finding["scope"] else ""
            lines.append(f"- {finding['code']} vps={finding['vps']} path={finding['path']}{scope}{suffix}")
    lines.extend(["", "## Files"])
    for item in report["results"]:
        lines.append(f"- {item['status']} vps={item['vps']} path={item['path']} scope={item['scope']}")
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
        "file_count": sum(result["file_count"] for result in results),
        "discovered_count": sum(result["discovered_count"] for result in results),
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
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        payload = {
            "ok": False,
            "error": str(exc),
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
