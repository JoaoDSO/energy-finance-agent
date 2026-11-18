"""Fictional controller/approval fixtures; no model calls or genuine human approvals."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import test_finance as fixtures
import decisions
import run_control
import workflow


class RunControlTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.FinanceTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        workflow.write_json(self.root / "config/financial_tables.json", self.fixture.config)
        manifest = workflow.read_json(self.root / "data/manifest.json")
        url = "https://cdn.orsted.com/fictional-unit-test.pdf"
        manifest["sources"]["fixture"]["url"] = url
        workflow.write_json(self.root / "data/manifest.json", manifest)
        workflow.write_json(self.root / "config/sources.json", {"sources": [{"id": "fixture", "filename": "fixture.pdf", "url": url}]})
        self.package = self.root / "runs/package_fixture"
        workflow.write_json(self.package / "HUMAN_DECISION.json", {"conditions": [{"topic": "Fictional condition", "status": "open"}]})
        (self.package / "HISTORICAL_BASELINE.md").write_text("Fictional historical report", encoding="utf-8")
        (self.package / "BRIEFING.md").write_text("Fictional expanded report", encoding="utf-8")
        workflow.write_json(self.root / "config/workflow.json", {"display_label": "Fictional case", "current_package": "runs/package_fixture", "research_client": "claude_desktop"})
        self.report = {"type": "fixture", "path": "runs/package_fixture",
                       "files": {str(p.relative_to(self.root)): workflow.sha256(p) for p in self.package.iterdir()},
                       "historical_file": "runs/package_fixture/HISTORICAL_BASELINE.md", "expanded_file": "runs/package_fixture/BRIEFING.md",
                       "historical_sha256": workflow.sha256(self.package / "HISTORICAL_BASELINE.md"),
                       "conditions": [{"topic": "Fictional condition", "status": "open"}], "expanded_review_complete": False,
                       "scope_note": "Fictional historical acceptance only; expanded output unreviewed"}

    def reuse(self):
        with patch("run_control.package_check", return_value=copy.deepcopy(self.report)):
            return run_control.run(root=self.root)

    def test_second_run_reuses_unchanged_artifact_without_model_work(self):
        first, second = self.reuse(), self.reuse()
        one, two = workflow.read_json(first / "status.json"), workflow.read_json(second / "status.json")
        self.assertEqual(one["input_version"], two["input_version"])
        self.assertEqual(two["reused_from"], str(first.relative_to(self.root)))
        self.assertEqual(two["model_stages_started"], [])
        self.assertTrue((self.root / "runs/REVIEW_QUEUE.md").exists())

    def test_changed_configuration_produces_new_input_version(self):
        first = self.reuse()
        config = workflow.read_json(self.root / "config/workflow.json")
        config["display_label"] = "Changed fictional label"
        workflow.write_json(self.root / "config/workflow.json", config)
        second = self.reuse()
        self.assertNotEqual(workflow.read_json(first / "status.json")["input_version"], workflow.read_json(second / "status.json")["input_version"])
        self.assertNotIn("reused_from", workflow.read_json(second / "status.json"))

    def test_tampered_source_stops_and_logs_failure_before_models(self):
        (self.root / "data/raw/fixture.pdf").write_bytes(b"Changed fictional source")
        with patch("workflow.research") as researcher, self.assertRaisesRegex(ValueError, "Source changed"):
            run_control.run(fresh=True, research_client="codex", root=self.root)
        researcher.assert_not_called()
        job = next((self.root / "runs").glob("workflow_*"))
        self.assertEqual(workflow.read_json(job / "status.json")["status"], "failed")

    def test_claude_fresh_run_waits_for_user_instead_of_claiming_automation(self):
        with patch("workflow.research") as researcher, patch("run_control.analyze") as analyst:
            job = run_control.run(fresh=True, root=self.root)
        researcher.assert_not_called()
        analyst.assert_not_called()
        self.assertEqual(workflow.read_json(job / "status.json")["status"], "awaiting_claude_research")

    def test_resume_refuses_changed_inputs(self):
        job = self.reuse()
        path = self.root / "config/extra_fixture.json"
        workflow.write_json(path, {"fictional_input": "changed"})
        with self.assertRaisesRegex(ValueError, "Inputs changed"):
            run_control.run(resume=job, root=self.root)

    def test_coverage_rejects_older_incomplete_research(self):
        with patch("run_control.select_research", return_value=(None, None, {"findings": []})):
            with self.assertRaisesRegex(ValueError, "every configured source"):
                run_control.coverage("runs/fixture", self.root)

    def test_decision_wrong_hash_is_rejected(self):
        job = self.reuse()
        with self.assertRaisesRegex(ValueError, "SHA-256 differs"):
            decisions.record(job, "approve_with_conditions", "historical", "wrong", "Fictional tester", "Fictional decision", root=self.root)

    def test_historical_conditional_decision_is_version_bound_not_publication(self):
        job = self.reuse()
        record = decisions.record(job, "approve_with_conditions", "historical", self.report["historical_sha256"], "Fictional tester", "Synthetic approval test only", root=self.root)
        saved = workflow.read_json(record)
        self.assertEqual(saved["artifact_sha256"], self.report["historical_sha256"])
        self.assertFalse(saved["publication_authorized"])
        self.assertFalse(saved["funding_sufficiency_approved"])
        self.assertEqual(saved["retained_conditions"], self.report["conditions"])
        self.assertTrue(decisions.status(job, self.root)["decisions"][0]["currently_applicable"])

    def test_unreviewed_expanded_material_cannot_inherit_acceptance(self):
        job = self.reuse()
        sha = workflow.sha256(self.package / "BRIEFING.md")
        with self.assertRaisesRegex(ValueError, "lack the required model review"):
            decisions.record(job, "approve_with_conditions", "expanded", sha, "Fictional tester", "Synthetic", root=self.root)

    def test_failed_and_incomplete_jobs_cannot_be_accepted(self):
        for status in ("failed", "awaiting_claude_research", "needs_human_resolution"):
            job = self.reuse()
            state = workflow.read_json(job / "status.json")
            state["status"] = status
            workflow.write_json(job / "status.json", state)
            with self.assertRaisesRegex(ValueError, "cannot be accepted"):
                decisions.record(job, "approve_with_conditions", "historical", self.report["historical_sha256"], "Fictional tester", "Synthetic", root=self.root)

    def test_known_conditions_cannot_be_silently_removed(self):
        job = self.reuse()
        with self.assertRaisesRegex(ValueError, "Known conditions"):
            decisions.record(job, "approve_historical", "historical", self.report["historical_sha256"], "Fictional tester", "Synthetic", root=self.root)

    def test_changed_report_invalidates_existing_decision(self):
        job = self.reuse()
        decisions.record(job, "approve_with_conditions", "historical", self.report["historical_sha256"], "Fictional tester", "Synthetic", root=self.root)
        (self.package / "HISTORICAL_BASELINE.md").write_text("Changed fictional report", encoding="utf-8")
        status = decisions.status(job, self.root)
        self.assertFalse(status["current_inputs_and_report_valid"])
        self.assertFalse(status["decisions"][0]["currently_applicable"])

    def test_review_failure_cannot_reset_correction_allowance_on_resume(self):
        job = self.reuse()
        state = workflow.read_json(job / "status.json")
        state.update(status="failed", mode="fresh", review_started=True)
        workflow.write_json(job / "status.json", state)
        with patch("run_control.review_cycle") as review, self.assertRaisesRegex(ValueError, "correction allowance"):
            run_control.run(resume=job, root=self.root)
        review.assert_not_called()

    def test_fresh_codex_route_and_completed_resume_do_not_repeat_stages(self):
        research = self.root / "runs/research_fixture"
        research.mkdir()
        analysis = self.root / "runs/analysis_fixture"
        workflow.write_json(analysis / "analysis.json", {"open_questions": ["Fictional question"]})
        review = self.root / "runs/review_fixture"
        workflow.write_json(review / "review.json", {"verdict": "pass"})
        cycle = self.root / "runs/cycle_fixture"
        workflow.write_json(cycle / "status.json", {"reviews": [str(review.relative_to(self.root))], "latest_analysis": str(analysis.relative_to(self.root)), "status": "awaiting_human_review"})
        (cycle / "BRIEFING.md").write_text("Fictional reviewed draft", encoding="utf-8")
        with patch("workflow.research", return_value=research) as researcher, patch("run_control.coverage"), \
                patch("run_control.analyze", return_value=analysis) as analyst, patch("run_control.review_cycle", return_value=cycle) as reviewer, \
                patch("run_control.audit_review", return_value={"passed": True}), patch("run_control.audit_analysis", return_value={"passed": True}):
            job = run_control.run(fresh=True, research_client="codex", root=self.root)
            run_control.run(resume=job, root=self.root)
        researcher.assert_called_once()
        analyst.assert_called_once()
        reviewer.assert_called_once()
        state = workflow.read_json(job / "status.json")
        self.assertEqual(state["status"], "awaiting_human_review")
        self.assertFalse(state["human_final_approval"])
        self.assertGreater(len(state["report"]["conditions"]), 0)


if __name__ == "__main__":
    unittest.main()
