import unittest

from pados.execution.authorization import authorize_execution_capabilities
from pados.execution.handoff import task_envelope


class ExecutionAuthorizationTests(unittest.TestCase):
    def contract(self):
        return {
            "work_contract_id": "work-1",
            "project_id": "project-1",
            "required_capabilities": ["MAKE"],
            "skill_ids": ["skill:python-review"],
            "mcp_capabilities": ["filesystem.read"],
            "secret_refs": ["secretref://provider/repo-token"],
        }

    def grant(self, **overrides):
        return {
            "work_contract_id": "work-1",
            "project_id": "project-1",
            "capabilities": ["MAKE"],
            "skills": ["skill:python-review"],
            "mcp_capabilities": ["filesystem.read"],
            "secret_refs": ["secretref://provider/repo-token"],
            "approval_ref": "approval-17",
            **overrides,
        }

    def test_installed_skill_and_registered_mcp_still_need_explicit_grants(self):
        with self.assertRaisesRegex(PermissionError, "SKILL_NOT_AUTHORIZED"):
            authorize_execution_capabilities(
                self.contract(), self.grant(skills=[]),
                installed_skills={"skill:python-review"},
                registered_mcp_capabilities={"filesystem.read"},
            )
        with self.assertRaisesRegex(PermissionError, "MCP_CAPABILITY_NOT_AUTHORIZED"):
            authorize_execution_capabilities(
                self.contract(), self.grant(mcp_capabilities=[]),
                installed_skills={"skill:python-review"},
                registered_mcp_capabilities={"filesystem.read"},
            )

    def test_agent_role_does_not_grant_missing_capabilities(self):
        with self.assertRaisesRegex(PermissionError, "CAPABILITY_NOT_AUTHORIZED"):
            authorize_execution_capabilities(
                self.contract(), self.grant(capabilities=[]),
                installed_skills={"skill:python-review"},
                registered_mcp_capabilities={"filesystem.read"},
                agent_role="builder",
            )

    def test_secretrefs_are_returned_as_pointers_only(self):
        authorization = authorize_execution_capabilities(
            self.contract(), self.grant(),
            installed_skills={"skill:python-review"},
            registered_mcp_capabilities={"filesystem.read"},
        )
        self.assertEqual(authorization["secret_refs"], ["secretref://provider/repo-token"])
        self.assertNotIn("repo-token-value", str(authorization))
        self.assertEqual(authorization["approval_ref"], "approval-17")

    def test_secret_value_cannot_be_substituted_for_a_reference(self):
        with self.assertRaisesRegex(PermissionError, "SECRET_REFERENCE_REQUIRED"):
            authorize_execution_capabilities(
                {**self.contract(), "secret_refs": ["repo-token-value"]}, self.grant(),
                installed_skills={"skill:python-review"},
                registered_mcp_capabilities={"filesystem.read"},
            )

    def test_secretref_path_traversal_is_rejected(self):
        refs = ["secretref://provider/../private"]
        with self.assertRaisesRegex(PermissionError, "SECRET_REFERENCE_REQUIRED"):
            authorize_execution_capabilities(
                {**self.contract(), "secret_refs": refs}, self.grant(secret_refs=refs),
                installed_skills={"skill:python-review"},
                registered_mcp_capabilities={"filesystem.read"},
            )

    def test_cross_project_grant_is_rejected(self):
        with self.assertRaisesRegex(PermissionError, "GRANT_SCOPE_MISMATCH"):
            authorize_execution_capabilities(
                self.contract(), self.grant(project_id="project-2"),
                installed_skills={"skill:python-review"},
                registered_mcp_capabilities={"filesystem.read"},
            )

    def test_task_handoff_requires_explicit_grant_for_extensions(self):
        contract = self.contract() | {
            "state": "AUTHORIZED",
            "scope": {"isolated": True, "allowed_paths": ["src/a.py"], "workspace_root": "/approved"},
            "changes": [{"path": "src/a.py"}],
            "allowed_actions": ["WRITE_APPROVED_WORKSPACE"],
            "context_fingerprint": "fp",
            "semantic_base_scope_version": 2,
            "human_gate_required": False,
            "retry_policy": {"max_attempts": 1},
        }
        with self.assertRaisesRegex(PermissionError, "EXECUTION_GRANT_REQUIRED"):
            task_envelope(contract)
        envelope = task_envelope(
            contract,
            execution_grant=self.grant(),
            installed_skills=frozenset({"skill:python-review"}),
            registered_mcp_capabilities=frozenset({"filesystem.read"}),
        )
        self.assertEqual(envelope["secret_refs"], ["secretref://provider/repo-token"])
        self.assertEqual(envelope["approval_ref"], "approval-17")


if __name__ == "__main__":
    unittest.main()
