"""Tests for tools/run_env_audit.py."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tools import run_env_audit


def _scope_report(status: str = "OK", finding_count: int = 0) -> dict:
    findings = []
    if finding_count:
        findings = [
            {
                "code": "FORBIDDEN_IN_SCOPE",
                "vps": "maincua",
                "path": "/home/rdios/.env",
                "scope": "host-shared",
                "variable": "OPENAI_API_KEY",
                "message": "wrong scope",
            }
        ]
    return {
        "ok": status == "OK",
        "status": status,
        "matrix_path": "docs/env-scope-matrix.md",
        "selected_vps": ["maincua"],
        "file_count": 1,
        "discovered_count": 1,
        "finding_count": finding_count,
        "results": [
            {
                "vps": "maincua",
                "label": "MAINCUA VPS",
                "status": status,
                "file_count": 1,
                "discovered_count": 1,
                "finding_count": finding_count,
                "files": [],
                "findings": findings,
            }
        ],
        "findings": findings,
    }


def _wiring_report(status: str = "OK", finding_count: int = 0) -> dict:
    findings = []
    if finding_count:
        findings = [
            {
                "code": "UNEXPECTED_SYMLINK_TARGET",
                "vps": "maincua",
                "target": "maincua.env symlink",
                "kind": "symlink",
                "path": "/opt/secrets/maincua.env",
                "message": "wrong symlink",
            }
        ]
    return {
        "ok": status == "OK",
        "status": status,
        "matrix_path": "docs/env-scope-matrix.md",
        "selected_vps": ["maincua"],
        "target_count": 1,
        "finding_count": finding_count,
        "results": [
            {
                "vps": "maincua",
                "label": "MAINCUA VPS",
                "status": status,
                "target_count": 1,
                "finding_count": finding_count,
                "targets": [],
                "findings": findings,
            }
        ],
        "findings": findings,
    }


def _discovery_report(status: str = "OK", finding_count: int = 0) -> dict:
    findings = []
    if finding_count:
        findings = [
            {
                "code": "UNMAPPED_SYSTEMD_SERVICE",
                "vps": "maincua",
                "path": "/etc/systemd/system/protocolo-backend.service",
                "service": "protocolo-backend.service",
                "message": "service not mapped",
            }
        ]
    return {
        "ok": status == "OK",
        "status": status,
        "matrix_path": "docs/env-scope-matrix.md",
        "selected_vps": ["maincua"],
        "finding_count": finding_count,
        "env_candidate_count": 1,
        "systemd_candidate_count": 1,
        "compose_candidate_count": 0,
        "results": [
            {
                "vps": "maincua",
                "label": "MAINCUA VPS",
                "status": status,
                "env_candidate_count": 1,
                "systemd_candidate_count": 1,
                "compose_candidate_count": 0,
                "finding_count": finding_count,
                "findings": findings,
            }
        ],
        "findings": findings,
    }


class TestRunEnvAudit(unittest.TestCase):
    def test_runner_green_in_advisory_mode(self):
        with mock.patch.object(run_env_audit.env_scope_checker, "generate_report", return_value=_scope_report()):
            with mock.patch.object(run_env_audit.env_wiring_checker, "generate_report", return_value=_wiring_report()):
                with mock.patch.object(run_env_audit.env_discovery_checker, "generate_report", return_value=_discovery_report()):
                    exit_code, report = run_env_audit.generate_report(
                        matrix_path="docs/env-scope-matrix.md",
                        requested_vps="maincua",
                        timeout_seconds=15,
                        mode="advisory",
                    )
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "OK")
        self.assertEqual(report["finding_count"], 0)
        self.assertEqual(report["error_count"], 0)
        self.assertEqual(report["checker_count"], 3)

    def test_runner_scope_finding_in_advisory_mode(self):
        with mock.patch.object(run_env_audit.env_scope_checker, "generate_report", return_value=_scope_report(status="DRIFT", finding_count=1)):
            with mock.patch.object(run_env_audit.env_wiring_checker, "generate_report", return_value=_wiring_report()):
                with mock.patch.object(run_env_audit.env_discovery_checker, "generate_report", return_value=_discovery_report()):
                    exit_code, report = run_env_audit.generate_report(
                        matrix_path="docs/env-scope-matrix.md",
                        requested_vps="maincua",
                        timeout_seconds=15,
                        mode="advisory",
                    )
        self.assertEqual(exit_code, 10)
        self.assertEqual(report["status"], "DRIFT")
        self.assertEqual(report["finding_count"], 1)

    def test_runner_wiring_finding_in_advisory_mode(self):
        with mock.patch.object(run_env_audit.env_scope_checker, "generate_report", return_value=_scope_report()):
            with mock.patch.object(run_env_audit.env_wiring_checker, "generate_report", return_value=_wiring_report(status="DRIFT", finding_count=1)):
                with mock.patch.object(run_env_audit.env_discovery_checker, "generate_report", return_value=_discovery_report()):
                    exit_code, report = run_env_audit.generate_report(
                        matrix_path="docs/env-scope-matrix.md",
                        requested_vps="maincua",
                        timeout_seconds=15,
                        mode="advisory",
                    )
        self.assertEqual(exit_code, 10)
        self.assertEqual(report["status"], "DRIFT")
        self.assertEqual(report["finding_count"], 1)

    def test_runner_discovery_finding_in_advisory_mode(self):
        with mock.patch.object(run_env_audit.env_scope_checker, "generate_report", return_value=_scope_report()):
            with mock.patch.object(run_env_audit.env_wiring_checker, "generate_report", return_value=_wiring_report()):
                with mock.patch.object(run_env_audit.env_discovery_checker, "generate_report", return_value=_discovery_report(status="DRIFT", finding_count=1)):
                    exit_code, report = run_env_audit.generate_report(
                        matrix_path="docs/env-scope-matrix.md",
                        requested_vps="maincua",
                        timeout_seconds=15,
                        mode="advisory",
                    )
        self.assertEqual(exit_code, 10)
        self.assertEqual(report["status"], "DRIFT")
        self.assertEqual(report["finding_count"], 1)

    def test_runner_all_findings_accumulate(self):
        with mock.patch.object(run_env_audit.env_scope_checker, "generate_report", return_value=_scope_report(status="DRIFT", finding_count=1)):
            with mock.patch.object(run_env_audit.env_wiring_checker, "generate_report", return_value=_wiring_report(status="DRIFT", finding_count=1)):
                with mock.patch.object(run_env_audit.env_discovery_checker, "generate_report", return_value=_discovery_report(status="DRIFT", finding_count=1)):
                    exit_code, report = run_env_audit.generate_report(
                        matrix_path="docs/env-scope-matrix.md",
                        requested_vps="maincua",
                        timeout_seconds=15,
                        mode="advisory",
                    )
        self.assertEqual(exit_code, 10)
        self.assertEqual(report["finding_count"], 3)
        self.assertEqual(report["vps_results"][0]["status"], "DRIFT")

    def test_runner_checker_error_returns_execution_error(self):
        with mock.patch.object(run_env_audit.env_scope_checker, "generate_report", side_effect=ValueError("bad scope spec")):
            with mock.patch.object(run_env_audit.env_wiring_checker, "generate_report", return_value=_wiring_report()):
                with mock.patch.object(run_env_audit.env_discovery_checker, "generate_report", return_value=_discovery_report()):
                    exit_code, report = run_env_audit.generate_report(
                        matrix_path="docs/env-scope-matrix.md",
                        requested_vps="maincua",
                        timeout_seconds=15,
                        mode="advisory",
                    )
        self.assertEqual(exit_code, 2)
        self.assertEqual(report["status"], "ERROR")
        self.assertEqual(report["error_count"], 1)
        self.assertIn("bad scope spec", report["checkers"][0]["error"])

    def test_markdown_report_is_consolidated(self):
        report = {
            "ok": False,
            "status": "DRIFT",
            "mode": "advisory",
            "timestamp_utc": "2026-03-27T00:00:00+00:00",
            "matrix_path": "docs/env-scope-matrix.md",
            "selected_vps": ["maincua"],
            "checker_count": 3,
            "finding_count": 3,
            "error_count": 0,
            "exit_code": 10,
            "vps_results": [
                {
                    "vps": "maincua",
                    "status": "DRIFT",
                    "scope_status": "DRIFT",
                    "scope_finding_count": 1,
                    "wiring_status": "DRIFT",
                    "wiring_finding_count": 1,
                    "discovery_status": "DRIFT",
                    "discovery_finding_count": 1,
                }
            ],
            "checkers": [
                {
                    "checker": "scope",
                    "status": "DRIFT",
                    "finding_count": 1,
                    "error": "",
                    "report": _scope_report(status="DRIFT", finding_count=1),
                },
                {
                    "checker": "wiring",
                    "status": "DRIFT",
                    "finding_count": 1,
                    "error": "",
                    "report": _wiring_report(status="DRIFT", finding_count=1),
                },
                {
                    "checker": "discovery",
                    "status": "DRIFT",
                    "finding_count": 1,
                    "error": "",
                    "report": _discovery_report(status="DRIFT", finding_count=1),
                },
            ],
        }
        rendered = run_env_audit.render_report(report, "markdown")
        self.assertIn("# Env Audit", rendered)
        self.assertIn("scope=DRIFT", rendered)
        self.assertIn("discovery=DRIFT", rendered)
        self.assertIn("checker=scope", rendered)
        self.assertIn("checker=wiring", rendered)
        self.assertIn("checker=discovery", rendered)

    def test_strict_mode_preserves_drift_exit_code(self):
        with mock.patch.object(run_env_audit.env_scope_checker, "generate_report", return_value=_scope_report(status="DRIFT", finding_count=1)):
            with mock.patch.object(run_env_audit.env_wiring_checker, "generate_report", return_value=_wiring_report()):
                with mock.patch.object(run_env_audit.env_discovery_checker, "generate_report", return_value=_discovery_report()):
                    exit_code, report = run_env_audit.generate_report(
                        matrix_path="docs/env-scope-matrix.md",
                        requested_vps="maincua",
                        timeout_seconds=15,
                        mode="strict",
                    )
        self.assertEqual(exit_code, 1)
        self.assertEqual(report["exit_code"], 1)

    def test_output_path_writes_rendered_report(self):
        report = {
            "ok": True,
            "status": "OK",
            "mode": "advisory",
            "timestamp_utc": "2026-03-27T00:00:00+00:00",
            "matrix_path": "docs/env-scope-matrix.md",
            "selected_vps": ["maincua"],
            "checker_count": 3,
            "finding_count": 0,
            "error_count": 0,
            "exit_code": 0,
            "vps_results": [],
            "checkers": [],
        }
        rendered = run_env_audit.render_report(report, "json")
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "audit.json"
            run_env_audit._write_output(str(target), rendered)
            self.assertEqual(target.read_text(encoding="utf-8"), rendered)

    def test_main_prints_json_and_returns_advisory_exit_code(self):
        fake_report = {
            "ok": False,
            "status": "DRIFT",
            "mode": "advisory",
            "timestamp_utc": "2026-03-27T00:00:00+00:00",
            "matrix_path": "docs/env-scope-matrix.md",
            "selected_vps": ["maincua"],
            "checker_count": 3,
            "finding_count": 1,
            "error_count": 0,
            "exit_code": 10,
            "vps_results": [],
            "checkers": [],
        }
        with mock.patch.object(run_env_audit, "generate_report", return_value=(10, fake_report)):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = run_env_audit.main(["--vps", "maincua", "--report", "json"])
        self.assertEqual(exit_code, 10)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["exit_code"], 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
