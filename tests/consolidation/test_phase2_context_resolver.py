import unittest
from datetime import UTC, datetime

from pados.execution.semantic_context import (
    ContextResolutionError,
    resolve_database_semantic_context,
    validate_semantic_context,
)


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class _Session:
    def __init__(self, *, foreign_blocker=False, fail_on=None, missing_scope=False):
        self.calls = []
        self.foreign_blocker = foreign_blocker
        self.fail_on = fail_on
        self.missing_scope = missing_scope

    def execute(self, statement, params):
        self.calls.append((statement, params))
        name = statement.split("/*context:", 1)[1].split("*/", 1)[0]
        if name == self.fail_on:
            raise RuntimeError("database unavailable")
        rows = {
            "project": [{"project_id": "project-a", "name": "A", "root_path": "/a", "status": "active"}],
            "scope": [] if self.missing_scope else [{"project_id": "project-a", "scope_version": 7}],
            "goal": [],
            "blockers": ([{"id": "foreign", "project_id": "project-b"}] if self.foreign_blocker else []),
            "gates": [],
            "decisions": [],
            "accepted": [],
            "memory": [],
            "evidence": [{"id": "artifact-1", "project_id": "project-a", "sha256": "a" * 64, "verification_status": "VERIFIED"}],
        }[name]
        return _Result(rows)


class SemanticContextResolverTests(unittest.TestCase):
    def payload(self, project_id="project-a"):
        fields = {
            "active_goal": {"item": None, "provenance": {"source": "project_goals", "project_id": project_id}},
            "open_blockers": {"items": [], "provenance": {"source": "project_gaps", "project_id": project_id}},
            "pending_decisions": {"items": [], "provenance": {"source": "project_human_gates", "project_id": project_id}},
            "recent_accepted_results": {"items": [], "provenance": {"source": "state_proof_commits", "project_id": project_id}},
            "promoted_memory": {"items": [], "provenance": {"source": "memory_authority_events", "project_id": project_id}},
            "evidence": {"items": [], "provenance": {"source": "memory_artifacts", "project_id": project_id}},
        }
        return {
            "project_id": project_id,
            "scope_version": 3,
            "scope_provenance": {"source": "state_proof_scope_heads", "scope_id": project_id},
            **fields,
        }

    def test_requires_all_authoritative_categories_and_provenance(self):
        payload = self.payload()
        del payload["promoted_memory"]
        with self.assertRaisesRegex(ContextResolutionError, "SEMANTIC_FIELD_UNAVAILABLE"):
            validate_semantic_context("project-a", payload)

    def test_rejects_cross_project_projection(self):
        payload = self.payload()
        payload["open_blockers"]["items"] = [{"project_id": "project-b", "id": "foreign"}]
        with self.assertRaisesRegex(ContextResolutionError, "CROSS_PROJECT_CONTEXT"):
            validate_semantic_context("project-a", payload)

    def test_rejects_mismatched_scope_and_provenance(self):
        payload = self.payload()
        payload["pending_decisions"]["provenance"]["project_id"] = "project-b"
        with self.assertRaisesRegex(ContextResolutionError, "SEMANTIC_PROVENANCE_INVALID"):
            validate_semantic_context("project-a", payload)

    def test_rejects_untrusted_provenance_source_labels(self):
        payload = self.payload()
        payload["promoted_memory"]["provenance"]["source"] = "untrusted_second_memory"
        with self.assertRaisesRegex(ContextResolutionError, "SEMANTIC_PROVENANCE_INVALID"):
            validate_semantic_context("project-a", payload)

    def test_returns_bounded_project_scoped_snapshot(self):
        payload = self.payload()
        payload["open_blockers"]["items"] = [{"project_id": "project-a", "id": "b1"}]
        resolved = validate_semantic_context("project-a", payload)
        self.assertEqual(resolved["scope_version"], 3)
        self.assertEqual(resolved["open_blockers"]["items"][0]["id"], "b1")
        self.assertTrue(resolved["instructions_are_data_not_permission"])

    def test_credential_shaped_semantic_text_is_redacted(self):
        payload = self.payload()
        credential_field = "api" + chr(95) + "key"
        synthetic_value = "synthetic" + chr(45) + "marker"
        payload["pending_decisions"]["items"] = [
            {"question": f"{credential_field} = {synthetic_value}"}
        ]
        resolved = validate_semantic_context("project-a", payload)
        self.assertNotIn(synthetic_value, str(resolved))
        self.assertIn("[REDACTED: credential-shaped source]", str(resolved))

    def test_context_is_json_hashable_and_normalizes_database_timestamps(self):
        payload = self.payload()
        payload["active_goal"]["item"] = {"created_at": datetime(2026, 9, 23, tzinfo=UTC)}
        first = validate_semantic_context("project-a", payload)
        second = validate_semantic_context("project-a", payload)
        self.assertEqual(first["context_hash"], second["context_hash"])
        self.assertEqual(first["active_goal"]["item"]["created_at"], "2026-09-23T00:00:00.000000+00:00")

    def test_context_exceeding_byte_budget_is_rejected(self):
        payload = self.payload()
        payload["active_goal"]["item"] = {"objective": "x" * (256 * 1024)}
        with self.assertRaisesRegex(ContextResolutionError, "SEMANTIC_CONTEXT_BUDGET_EXCEEDED"):
            validate_semantic_context("project-a", payload)

    def test_database_resolver_binds_every_query_to_selected_project(self):
        session = _Session()
        resolved = resolve_database_semantic_context(session, "project-a")
        self.assertEqual(resolved["scope_version"], 7)
        self.assertEqual(len(session.calls), 9)
        self.assertTrue(all(params == {"project_id": "project-a"} for _, params in session.calls))
        self.assertTrue(all("project_id" in sql or "scope_id" in sql for sql, _ in session.calls))
        evidence_sql = next(sql for sql, _ in session.calls if "/*context:evidence*/" in sql)
        self.assertNotIn("canonical_path", evidence_sql)
        evidence = resolved["evidence"]["items"][0]
        self.assertNotIn("canonical_path", evidence)
        self.assertEqual(evidence["reference"], f"pados://artifact/artifact-1?sha256={'a' * 64}")

    def test_database_resolver_preserves_registered_project_binding(self):
        resolved = resolve_database_semantic_context(_Session(), "project-a")
        self.assertEqual(resolved["project"]["project_id"], "project-a")
        self.assertEqual(resolved["project"]["root_path"], "/a")

    def test_rejects_project_binding_for_another_project(self):
        payload = self.payload()
        payload["project"] = {"project_id": "project-b", "root_path": "/b"}
        with self.assertRaisesRegex(ContextResolutionError, "PROJECT_CONTEXT_SCOPE_MISMATCH"):
            validate_semantic_context("project-a", payload)

    def test_proof_lane_queries_run_on_the_separate_proof_session(self):
        operational, proof = _Session(), _Session()
        resolved = resolve_database_semantic_context(operational, "project-a", proof_session=proof)
        self.assertEqual(resolved["scope_version"], 7)
        def lane(session):
            return {sql.split("/*context:", 1)[1].split("*/", 1)[0] for sql, _ in session.calls}

        self.assertEqual(lane(proof), {"scope", "accepted"})
        # Operational memory authority: the proof DB's legacy memory table is never read.
        self.assertEqual(lane(operational), {"project", "goal", "blockers", "gates", "decisions", "memory", "evidence"})

    def test_database_resolver_requires_an_authoritative_scope_head(self):
        with self.assertRaisesRegex(ContextResolutionError, "AUTHORITATIVE_SCOPE_VERSION_REQUIRED"):
            resolve_database_semantic_context(_Session(missing_scope=True), "project-a")

    def test_database_resolver_rejects_foreign_rows(self):
        with self.assertRaisesRegex(ContextResolutionError, "CROSS_PROJECT_CONTEXT"):
            resolve_database_semantic_context(_Session(foreign_blocker=True), "project-a")

    def test_database_resolver_does_not_fallback_when_authority_query_fails(self):
        with self.assertRaisesRegex(ContextResolutionError, "SEMANTIC_CONTEXT_UNAVAILABLE"):
            resolve_database_semantic_context(_Session(fail_on="memory"), "project-a")


if __name__ == "__main__":
    unittest.main()
