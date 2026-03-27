"""Tests for tools/env_wiring_checker.py."""

from __future__ import annotations

import base64
import unittest

from tools import env_wiring_checker as checker


LOCAL_VPS = checker.VpsSpec(
    vps_id="maincua",
    label="MAINCUA VPS",
    access_mode="local",
    host_alias="",
    sudo=False,
    targets=(),
)


def _target(
    *,
    name: str,
    kind: str,
    path: str,
    mutation_rule: str = "keep wiring stable",
    must_exist: bool = True,
    service_name: str = "",
    expected_environment_files: tuple[str, ...] = (),
    allow_extra_environment_files: bool = False,
    expected_target: str = "",
    required_patterns: tuple[str, ...] = (),
) -> checker.TargetSpec:
    return checker.TargetSpec(
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


def _payload(text: str) -> dict[str, object]:
    return {
        "exists": True,
        "is_file": True,
        "is_symlink": False,
        "resolved": "",
        "text_b64": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        "error": "",
    }


class TestEnvWiringChecker(unittest.TestCase):
    def test_systemd_environment_file_order_mismatch(self):
        target = _target(
            name="gateway.service",
            kind="systemd-unit",
            path="/etc/systemd/system/gateway.service",
            service_name="gateway.service",
            expected_environment_files=("-/home/rdios/.env", "/home/rdios/cua/config/.env"),
        )
        raw = """# /etc/systemd/system/gateway.service
[Service]
EnvironmentFile=/home/rdios/cua/config/.env
EnvironmentFile=-/home/rdios/.env
"""
        findings = checker._audit_systemd_state(LOCAL_VPS, target, raw, "", True)
        codes = {item["code"] for item in findings}
        self.assertIn("UNEXPECTED_ENVIRONMENT_FILE_ORDER", codes)
        self.assertNotIn("MISSING_ENVIRONMENT_FILE", codes)

    def test_systemd_missing_environment_file(self):
        target = _target(
            name="agent-hub-sse.service",
            kind="systemd-unit",
            path="/etc/systemd/system/agent-hub-sse.service",
            service_name="agent-hub-sse.service",
            expected_environment_files=("-/home/rdios/.env", "/etc/agent-hub-mcp/server_sse.env"),
        )
        raw = """# /etc/systemd/system/agent-hub-sse.service
[Service]
EnvironmentFile=-/home/rdios/.env
"""
        findings = checker._audit_systemd_state(LOCAL_VPS, target, raw, "", True)
        codes = {item["code"] for item in findings}
        self.assertIn("MISSING_ENVIRONMENT_FILE", codes)
        self.assertNotIn("UNEXPECTED_ENVIRONMENT_FILE_ORDER", codes)

    def test_unexpected_symlink_target(self):
        target = _target(
            name="maincua.env symlink",
            kind="symlink",
            path="/opt/secrets/maincua.env",
            expected_target="/home/rdios/cua/config/.env",
        )
        payload = {
            "exists": True,
            "is_file": False,
            "is_symlink": True,
            "resolved": "/tmp/wrong.env",
            "text_b64": "",
            "error": "",
        }
        findings = checker._audit_symlink_state(LOCAL_VPS, target, payload)
        codes = {item["code"] for item in findings}
        self.assertIn("UNEXPECTED_SYMLINK_TARGET", codes)

    def test_compose_env_file_divergence(self):
        target = _target(
            name="evolution_api compose",
            kind="compose-env-file",
            path="/home/rdios/evolution-api/docker-compose.yaml",
            service_name="evolution_api",
            expected_environment_files=(".env", ".shared.env"),
        )
        payload = _payload(
            """
services:
  evolution_api:
    env_file:
      - .shared.env
      - .env
"""
        )
        findings = checker._audit_compose_state(LOCAL_VPS, target, payload)
        codes = {item["code"] for item in findings}
        self.assertIn("UNEXPECTED_ENVIRONMENT_FILE_ORDER", codes)

    def test_service_not_mapped_for_systemd(self):
        target = _target(
            name="missing.service",
            kind="systemd-unit",
            path="/etc/systemd/system/missing.service",
            service_name="missing.service",
            expected_environment_files=("/tmp/example.env",),
        )
        findings = checker._audit_systemd_state(
            LOCAL_VPS,
            target,
            "",
            "No files found for missing.service.",
            False,
        )
        codes = {item["code"] for item in findings}
        self.assertEqual(codes, {"SERVICE_NOT_MAPPED"})

    def test_path_pattern_mismatch(self):
        target = _target(
            name="gateway/config.py",
            kind="path-patterns",
            path="/home/rdios/gateway/config.py",
            required_patterns=(
                'SECRETS_FILE = Path("/opt/secrets/maincua.env")',
                "load_dotenv(SECRETS_FILE)",
            ),
        )
        payload = _payload("from dotenv import load_dotenv\nload_dotenv()\n")
        findings = checker._audit_pattern_state(LOCAL_VPS, target, payload)
        codes = {item["code"] for item in findings}
        self.assertEqual(codes, {"WIRING_PATH_MISMATCH"})

    def test_markdown_report_lists_findings(self):
        report = {
            "status": "DRIFT",
            "selected_vps": ["maincua"],
            "finding_count": 1,
            "target_count": 1,
            "findings": [
                {
                    "code": "UNEXPECTED_SYMLINK_TARGET",
                    "vps": "maincua",
                    "target": "maincua.env symlink",
                    "kind": "symlink",
                    "path": "/opt/secrets/maincua.env",
                    "message": "mismatch",
                }
            ],
            "results": [
                {
                    "vps": "maincua",
                    "targets": [
                        {
                            "status": "DRIFT",
                            "target": "maincua.env symlink",
                            "kind": "symlink",
                            "path": "/opt/secrets/maincua.env",
                        }
                    ],
                }
            ],
        }
        rendered = checker._render_markdown(report)
        self.assertIn("UNEXPECTED_SYMLINK_TARGET", rendered)
        self.assertIn("maincua.env symlink", rendered)


if __name__ == "__main__":
    unittest.main(verbosity=2)
