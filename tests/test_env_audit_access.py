"""Tests for tools/env_audit_access.py."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from tools import env_audit_access
from tools import env_scope_checker


class TestEnvAuditAccess(unittest.TestCase):
    def test_load_access_overrides_from_env_empty(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            self.assertEqual(env_audit_access.load_access_overrides_from_env(), {})

    def test_load_access_overrides_from_env_valid(self):
        payload = '{"hub":{"mode":"ssh","host_alias":"hub","sudo":false}}'
        with mock.patch.dict(os.environ, {env_audit_access.ENV_NAME: payload}, clear=False):
            overrides = env_audit_access.load_access_overrides_from_env()
        self.assertEqual(overrides["hub"]["mode"], "ssh")
        self.assertEqual(overrides["hub"]["host_alias"], "hub")
        self.assertFalse(overrides["hub"]["sudo"])

    def test_load_access_overrides_from_env_invalid_mode(self):
        payload = '{"hub":{"mode":"remote","host_alias":"hub"}}'
        with mock.patch.dict(os.environ, {env_audit_access.ENV_NAME: payload}, clear=False):
            with self.assertRaises(ValueError):
                env_audit_access.load_access_overrides_from_env()

    def test_apply_access_overrides_replaces_vps_access(self):
        original = [
            env_scope_checker.VpsSpec(
                vps_id="hub",
                label="HUB VPS",
                access_mode="local",
                host_alias="",
                sudo=False,
                discovery_paths=(),
                discovery_roots=(),
                ignore_globs=(),
                files=(),
            )
        ]
        updated = env_audit_access.apply_access_overrides(
            original,
            {"hub": {"mode": "ssh", "host_alias": "hub", "sudo": True}},
        )
        self.assertEqual(updated[0].access_mode, "ssh")
        self.assertEqual(updated[0].host_alias, "hub")
        self.assertTrue(updated[0].sudo)


if __name__ == "__main__":
    unittest.main(verbosity=2)
