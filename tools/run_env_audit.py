#!/usr/bin/env python3
"""Run env scope and wiring audits in one advisory-friendly command."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import env_scope_checker, env_wiring_checker

EXIT_OK = 0
EXIT_STRICT_DRIFT = 1
EXIT_ERROR = 2
EXIT_ADVISORY_DRIFT = 10


def _run_checker(
    checker_name: str,
    generator,
    matrix_path: str | Path,
    requested_vps: str,
    timeout_seconds: int,
) -> dict:
    try:
        report = generator(matrix_path, requested_vps, timeout_seconds)
        return {
            "checker": checker_name,
            "status": report["status"],
            "ok": report["ok"],
            "finding_count": report["finding_count"],
            "report": report,
            "error": "",
        }
    except Exception as exc:
        return {
            "checker": checker_name,
            "status": "ERROR",
            "ok": False,
            "finding_count": 0,
            "report": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _ordered_selected_vps(checker_runs: list[dict], requested_vps: str) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for checker_run in checker_runs:
        report = checker_run.get("report")
        if not report:
            continue
        for vps_id in report.get("selected_vps", []):
            if vps_id not in seen:
                seen.add(vps_id)
                ordered.append(vps_id)
    if ordered:
        return ordered
    if requested_vps.strip().lower() == "all":
        return []
    return [requested_vps.strip().lower()]


def _index_checker_results(report: dict | None) -> dict[str, dict]:
    if not report:
        return {}
    return {
        item["vps"]: item
        for item in report.get("results", [])
    }


def _build_vps_results(selected_vps: list[str], checker_runs: list[dict]) -> list[dict]:
    scope_run = next((item for item in checker_runs if item["checker"] == "scope"), None)
    wiring_run = next((item for item in checker_runs if item["checker"] == "wiring"), None)
    scope_index = _index_checker_results(scope_run["report"] if scope_run else None)
    wiring_index = _index_checker_results(wiring_run["report"] if wiring_run else None)

    results: list[dict] = []
    for vps_id in selected_vps:
        scope_status = "ERROR" if scope_run and scope_run["status"] == "ERROR" else scope_index.get(vps_id, {}).get("status", "UNMAPPED")
        wiring_status = "ERROR" if wiring_run and wiring_run["status"] == "ERROR" else wiring_index.get(vps_id, {}).get("status", "UNMAPPED")
        scope_findings = scope_index.get(vps_id, {}).get("finding_count", 0)
        wiring_findings = wiring_index.get(vps_id, {}).get("finding_count", 0)
        if "ERROR" in {scope_status, wiring_status}:
            status = "ERROR"
        elif "DRIFT" in {scope_status, wiring_status}:
            status = "DRIFT"
        else:
            status = "OK"
        results.append(
            {
                "vps": vps_id,
                "status": status,
                "scope_status": scope_status,
                "scope_finding_count": scope_findings,
                "wiring_status": wiring_status,
                "wiring_finding_count": wiring_findings,
            }
        )
    return results


def _compute_exit_code(mode: str, status: str) -> int:
    if status == "ERROR":
        return EXIT_ERROR
    if status == "DRIFT":
        return EXIT_ADVISORY_DRIFT if mode == "advisory" else EXIT_STRICT_DRIFT
    return EXIT_OK


def generate_report(
    matrix_path: str | Path,
    requested_vps: str,
    timeout_seconds: int,
    mode: str,
) -> tuple[int, dict]:
    checker_runs = [
        _run_checker("scope", env_scope_checker.generate_report, matrix_path, requested_vps, timeout_seconds),
        _run_checker("wiring", env_wiring_checker.generate_report, matrix_path, requested_vps, timeout_seconds),
    ]
    selected_vps = _ordered_selected_vps(checker_runs, requested_vps)
    error_count = sum(1 for item in checker_runs if item["status"] == "ERROR")
    finding_count = sum(item["finding_count"] for item in checker_runs)
    if error_count:
        status = "ERROR"
    elif finding_count:
        status = "DRIFT"
    else:
        status = "OK"

    report = {
        "ok": status == "OK",
        "status": status,
        "mode": mode,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "matrix_path": str(Path(matrix_path)),
        "selected_vps": selected_vps,
        "checker_count": len(checker_runs),
        "finding_count": finding_count,
        "error_count": error_count,
        "vps_results": _build_vps_results(selected_vps, checker_runs),
        "checkers": checker_runs,
    }
    exit_code = _compute_exit_code(mode, status)
    report["exit_code"] = exit_code
    return exit_code, report


def render_report(report: dict, report_format: str) -> str:
    if report_format == "json":
        return json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if report_format != "markdown":
        raise ValueError(f"unsupported report format: {report_format}")

    lines = [
        "# Env Audit",
        "",
        f"- status: {report['status']}",
        f"- mode: {report['mode']}",
        f"- exit_code: {report['exit_code']}",
        f"- timestamp_utc: {report['timestamp_utc']}",
        f"- vps: {', '.join(report['selected_vps']) if report['selected_vps'] else '<unknown>'}",
        f"- findings: {report['finding_count']}",
        f"- errors: {report['error_count']}",
        "",
        "## By VPS",
    ]
    if not report["vps_results"]:
        lines.append("- UNAVAILABLE")
    else:
        for item in report["vps_results"]:
            lines.append(
                f"- {item['status']} vps={item['vps']} scope={item['scope_status']} scope_findings={item['scope_finding_count']} wiring={item['wiring_status']} wiring_findings={item['wiring_finding_count']}"
            )

    lines.extend(["", "## Checkers"])
    for checker_run in report["checkers"]:
        lines.append(
            f"- {checker_run['status']} checker={checker_run['checker']} findings={checker_run['finding_count']}"
        )
        if checker_run["error"]:
            lines.append(f"  error: {checker_run['error']}")

    lines.extend(["", "## Findings"])
    if report["finding_count"] == 0:
        lines.append("- OK")
    else:
        for checker_run in report["checkers"]:
            nested_report = checker_run["report"]
            if not nested_report or not nested_report.get("findings"):
                continue
            for finding in nested_report["findings"]:
                suffix = []
                if "scope" in finding and finding["scope"]:
                    suffix.append(f"scope={finding['scope']}")
                if "variable" in finding and finding["variable"]:
                    suffix.append(f"variable={finding['variable']}")
                if "target" in finding and finding["target"]:
                    suffix.append(f"target={finding['target']}")
                if "kind" in finding and finding["kind"]:
                    suffix.append(f"kind={finding['kind']}")
                extra = f" {' '.join(suffix)}" if suffix else ""
                lines.append(
                    f"- {finding['code']} checker={checker_run['checker']} vps={finding['vps']} path={finding['path']}{extra}"
                )

    if report["error_count"]:
        lines.extend(["", "## Errors"])
        for checker_run in report["checkers"]:
            if checker_run["error"]:
                lines.append(f"- checker={checker_run['checker']} error={checker_run['error']}")

    return "\n".join(lines) + "\n"


def _write_output(output_path: str | None, body: str) -> None:
    if not output_path:
        return
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix-path", default="docs/env-scope-matrix.md")
    parser.add_argument("--vps", default="all", help="hub, next, maincua, or all")
    parser.add_argument("--report", choices=["json", "markdown"], default="json")
    parser.add_argument("--mode", choices=["advisory", "strict"], default="advisory")
    parser.add_argument("--ssh-timeout-seconds", type=int, default=15)
    parser.add_argument("--output-path", default="", help="optional path to write the rendered report")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    exit_code, report = generate_report(
        matrix_path=args.matrix_path,
        requested_vps=args.vps,
        timeout_seconds=args.ssh_timeout_seconds,
        mode=args.mode,
    )
    rendered = render_report(report, args.report)
    _write_output(args.output_path or None, rendered)
    print(rendered, end="")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
