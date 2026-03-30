from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from tools import env_discovery_checker


class EnvDiscoveryCheckerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

        self.env_root = self.root / "apps"
        self.env_root.mkdir(parents=True, exist_ok=True)
        self.systemd_dir = self.root / "etc" / "systemd" / "system"
        self.systemd_dir.mkdir(parents=True, exist_ok=True)
        self.compose_root = self.root / "stacks"
        self.compose_root.mkdir(parents=True, exist_ok=True)

        self.known_env = self.env_root / "known" / ".env"
        self.known_env.parent.mkdir(parents=True, exist_ok=True)
        self.known_env.write_text("PROXY_AUTH_TOKEN=known-secret\n", encoding="utf-8")

        self.compose_known_env = self.compose_root / ".env"
        self.compose_known_env.write_text("SAFE_VAR=1\n", encoding="utf-8")

        self.unmapped_env = self.env_root / "rogue" / ".env"
        self.unmapped_env.parent.mkdir(parents=True, exist_ok=True)
        self.unmapped_env.write_text("DO_NOT_LEAK=super-secret-value\n", encoding="utf-8")

        mapped_service = self.systemd_dir / "mapped.service"
        mapped_service.write_text(
            f"[Service]\nEnvironmentFile={self.known_env}\nExecStart=/bin/true\n",
            encoding="utf-8",
        )

        rogue_service = self.systemd_dir / "rogue.service"
        rogue_service.write_text(
            "[Service]\nEnvironmentFile=-/etc/rogue.env\nExecStart=/bin/true\n",
            encoding="utf-8",
        )

        self.compose_path = self.compose_root / "docker-compose.yml"
        (self.compose_root / "rogue.env").write_text("API_KEY=compose-secret\n", encoding="utf-8")
        self.compose_path.write_text(
            textwrap.dedent(
                """
                services:
                  mapped:
                    env_file:
                      - .env
                  rogue:
                    env_file:
                      - rogue.env
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )

        self.spec = env_discovery_checker.DiscoveryVps(
            vps_id="local",
            label="LOCAL VPS",
            access_mode="local",
            host_alias="",
            sudo=False,
            env_roots=(str(self.env_root), str(self.compose_root)),
            systemd_dirs=(str(self.systemd_dir),),
            compose_roots=(str(self.compose_root),),
            ignore_globs=(),
            known_env_files=(str(self.known_env), str(self.compose_known_env)),
            known_systemd_services=("mapped.service",),
            known_compose_targets=(
                env_discovery_checker.KnownComposeTarget(
                    path=str(self.compose_path),
                    service_name="mapped",
                ),
            ),
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_audit_vps_flags_unmapped_candidates(self) -> None:
        report = env_discovery_checker._audit_vps(self.spec, timeout_seconds=5)
        codes = [finding["code"] for finding in report["findings"]]

        self.assertEqual(report["status"], "DRIFT")
        self.assertIn("UNMAPPED_ENV_FILE", codes)
        self.assertIn("UNMAPPED_SYSTEMD_SERVICE", codes)
        self.assertIn("UNMAPPED_COMPOSE_SERVICE", codes)
        self.assertIn("UNMAPPED_ENVIRONMENT_FILE", codes)

        rogue_env_findings = [
            finding for finding in report["findings"] if finding["path"] == str(self.unmapped_env)
        ]
        self.assertTrue(rogue_env_findings)

    def test_markdown_report_does_not_leak_secret_values(self) -> None:
        report = env_discovery_checker._audit_vps(self.spec, timeout_seconds=5)
        rendered = env_discovery_checker.render_report(
            {
                "ok": False,
                "status": "DRIFT",
                "matrix_path": "fake-matrix.md",
                "selected_vps": ["local"],
                "finding_count": len(report["findings"]),
                "env_candidate_count": report["env_candidate_count"],
                "systemd_candidate_count": report["systemd_candidate_count"],
                "compose_candidate_count": report["compose_candidate_count"],
                "results": [report],
                "findings": report["findings"],
            },
            "markdown",
        )

        self.assertNotIn("super-secret-value", rendered)
        self.assertNotIn("compose-secret", rendered)
        self.assertIn("UNMAPPED_ENV_FILE", rendered)
        self.assertIn("UNMAPPED_SYSTEMD_SERVICE", rendered)

    def test_generate_report_aggregates_local_specs(self) -> None:
        matrix_path = self.root / "matrix.md"
        matrix_path.write_text("# fake\n", encoding="utf-8")

        with mock.patch.object(
            env_discovery_checker,
            "_build_discovery_specs",
            return_value=[self.spec],
        ):
            report = env_discovery_checker.generate_report(matrix_path, "all", timeout_seconds=5)

        self.assertFalse(report["ok"])
        self.assertEqual(report["status"], "DRIFT")
        self.assertEqual(report["selected_vps"], ["local"])
        self.assertGreaterEqual(report["finding_count"], 4)

    def test_clean_vps_returns_ok(self) -> None:
        clean_root = self.root / "clean"
        clean_env_root = clean_root / "apps"
        clean_env_root.mkdir(parents=True, exist_ok=True)
        clean_systemd_dir = clean_root / "etc" / "systemd" / "system"
        clean_systemd_dir.mkdir(parents=True, exist_ok=True)
        clean_compose_root = clean_root / "stack"
        clean_compose_root.mkdir(parents=True, exist_ok=True)

        env_file = clean_env_root / ".env"
        env_file.write_text("SAFE=1\n", encoding="utf-8")
        service_file = clean_systemd_dir / "mapped.service"
        service_file.write_text(
            f"[Service]\nEnvironmentFile={env_file}\nExecStart=/bin/true\n",
            encoding="utf-8",
        )
        compose_path = clean_compose_root / "docker-compose.yml"
        compose_path.write_text(
            textwrap.dedent(
                """
                services:
                  mapped:
                    env_file:
                      - ../apps/.env
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )

        clean_spec = env_discovery_checker.DiscoveryVps(
            vps_id="clean",
            label="CLEAN VPS",
            access_mode="local",
            host_alias="",
            sudo=False,
            env_roots=(str(clean_env_root), str(clean_compose_root)),
            systemd_dirs=(str(clean_systemd_dir),),
            compose_roots=(str(clean_compose_root),),
            ignore_globs=(),
            known_env_files=(str(env_file),),
            known_systemd_services=("mapped.service",),
            known_compose_targets=(
                env_discovery_checker.KnownComposeTarget(
                    path=str(compose_path),
                    service_name="mapped",
                ),
            ),
        )

        report = env_discovery_checker._audit_vps(clean_spec, timeout_seconds=5)
        self.assertEqual(report["status"], "OK")
        self.assertEqual(report["finding_count"], 0)


if __name__ == "__main__":
    unittest.main()
