import unittest

from pados.operations.projections import decision_inbox_projection, mission_control_projection


class OperationsProjectionTests(unittest.TestCase):
    def inputs(self):
        projects = [
            {"id": "p1", "name": "One", "status": "active"},
            {"id": "p2", "name": "Two", "status": "blocked"},
        ]
        goals = [{"project_id": "p1", "id": "g1", "objective": "Ship feature", "status": "ACTIVE"}]
        blockers = [{"project_id": "p1", "id": "b1", "description": "DB credential", "severity": "HIGH", "status": "OPEN"}]
        decisions = [{"project_id": "p1", "id": "d1", "question": "Choose provider", "status": "PROPOSED"}]
        runs = [
            {"project_id": "p1", "id": "r1", "status": "RUNNING"},
            {"project_id": "p2", "id": "r2", "status": "BLOCKED", "human_action_required": True},
        ]
        accepted = [{"project_id": "p1", "id": "a1", "title": "Result", "accepted_scope_version": 4}]
        health = {"alive": True, "configured": True, "usable": False, "ready": False, "degraded": False, "blocked": True}
        return projects, goals, blockers, decisions, runs, accepted, health

    def test_mission_control_uses_project_bound_authority_data(self):
        payload = mission_control_projection(*self.inputs())
        one = next(row for row in payload["projects"] if row["project_id"] == "p1")
        two = next(row for row in payload["projects"] if row["project_id"] == "p2")
        self.assertEqual(one["current_goal"]["id"], "g1")
        self.assertEqual(one["blockers"][0]["id"], "b1")
        self.assertEqual(one["recent_accepted_results"][0]["id"], "a1")
        self.assertEqual(one["active_runs"][0]["id"], "r1")
        self.assertEqual(two["blockers"], [])
        self.assertEqual(two["current_goal"], None)
        self.assertEqual(payload["system_health"]["state"], "blocked")

    def test_decision_inbox_derives_required_human_actions_and_links(self):
        projects, goals, blockers, decisions, runs, _, _ = self.inputs()
        del goals
        approvals = [{"project_id": "p1", "id": "ap1", "item": "Release package", "status": "PENDING_APPROVAL"}]
        failed = [{"project_id": "p1", "id": "r3", "status": "FAILED", "validation_status": "FAILED", "human_action_required": True}]
        waiting = [{"project_id": "p2", "id": "r4", "status": "WAITING_HUMAN", "name": "Paused goal loop"}]
        items = decision_inbox_projection(projects, decisions, blockers, [*runs, *failed, *waiting], approvals)
        types = {item["type"] for item in items}
        self.assertTrue({"DECISION", "BLOCKER", "EXECUTION", "APPROVAL", "VALIDATION"} <= types)
        self.assertTrue(all(item["project_id"] in {"p1", "p2"} for item in items))
        self.assertTrue(all(item["required_human_action"] and item["target"] for item in items))
        self.assertIn("execution:r4", {item["id"] for item in items})

    def test_inbox_items_carry_required_categories_and_provenance(self):
        projects, _, blockers, decisions, runs, _, _ = self.inputs()
        approvals = [{"project_id": "p1", "id": "ap1", "item": "Release package", "status": "PENDING_APPROVAL"}]
        failed = [{"project_id": "p1", "id": "r3", "status": "FAILED", "validation_status": "FAILED", "human_action_required": True}]
        items = decision_inbox_projection(projects, decisions, blockers, [*runs, *failed], approvals)
        categories = {item["category"] for item in items}
        self.assertEqual(categories, {
            "HUMAN_DECISION_REQUIRED", "APPROVAL_REQUIRED", "VALIDATION_FAILED",
            "EXECUTION_BLOCKED", "PROJECT_BLOCKER",
        })
        for item in items:
            self.assertEqual(item["provenance"]["project_id"], item["project_id"])
            self.assertTrue(item["provenance"]["record"] and item["provenance"]["record_id"])

    def test_unknown_project_records_are_not_disclosed(self):
        projects, _, blockers, decisions, runs, _, _ = self.inputs()
        foreign = [{"project_id": "p3", "id": "foreign", "description": "outside"}]
        items = decision_inbox_projection(projects, decisions, [*blockers, *foreign], runs, [])
        self.assertNotIn("p3", {item["project_id"] for item in items})


if __name__ == "__main__":
    unittest.main()
