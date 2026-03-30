"""Static assertions for the advisory env audit GitHub workflow."""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "env-audit-advisory.yml"


class TestEnvAuditWorkflow(unittest.TestCase):
    def test_workflow_exists(self):
        self.assertTrue(WORKFLOW.exists(), str(WORKFLOW))

    def test_workflow_contains_manual_trigger_and_artifacts(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", text)
        self.assertIn("actions/upload-artifact@v4", text)
        self.assertIn("ENV_AUDIT_SSH_PRIVATE_KEY", text)
        self.assertIn("ENV_AUDIT_ACCESS_OVERRIDES", text)

    def test_workflow_fixates_advisory_mode_and_exit_code_policy(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('AUDIT_MODE: advisory', text)
        self.assertIn('if [ "${AUDIT_EXIT_CODE}" = "10" ]; then', text)
        self.assertIn('if [ "${AUDIT_EXIT_CODE}" = "0" ]; then', text)
        self.assertIn('exit "${AUDIT_EXIT_CODE}"', text)

    def test_workflow_summary_mentions_discovery_layer(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("### By VPS", text)
        self.assertIn("discovery=`{item['discovery_status']}`", text)

    def test_workflow_uses_expected_artifact_names(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("name: ${{ steps.audit.outputs.artifact_base }}-json", text)
        self.assertIn("name: ${{ steps.audit.outputs.artifact_base }}-markdown", text)
        self.assertIn("f\"- `{artifact_base}.json`\"", text)
        self.assertIn("lines.append(f\"- `{artifact_base}.md`\")", text)

    def test_workflow_summary_policy_section_has_expected_exit_codes(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("### Policy", text)
        self.assertIn("- `0`: clean run", text)
        self.assertIn("- `10`: advisory warning, review required, no hard gate", text)
        self.assertIn("- `2`: execution error, treat as failure", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
