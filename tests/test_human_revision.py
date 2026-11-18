"""Human edit requests do not become approval or open an unlimited correction loop."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from human_revision import human_revision, prepare_human_revision
import workflow


class HumanRevisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.original = self.root / "runs/analysis_original"
        self.revised = self.root / "runs/analysis_revised"
        self.previous = self.root / "runs/review_previous"
        self.reviewed = self.root / "runs/review_new"
        workflow.write_json(self.original / "analysis.json", {"title": "Fictional original"})
        workflow.write_json(self.revised / "analysis.json", {"title": "Fictional corrected draft"})
        workflow.write_json(self.revised / "inputs/human_feedback.json", {"remaining_gaps": ["Fictional unresolved condition"]})
        (self.revised / "BRIEFING.md").write_text("Fictional draft only", encoding="utf-8")
        workflow.write_json(self.previous / "handoff.json", {"analysis_run": str(self.original.relative_to(self.root))})
        workflow.write_json(self.reviewed / "review.json", {"verdict": "pass"})
        (self.reviewed / "REVIEW.md").write_text("Fictional model review", encoding="utf-8")
        self.request_file = self.root / "request.json"

    def run_case(self, verdict):
        workflow.write_json(self.reviewed / "review.json", {"verdict": verdict})
        with patch("human_revision.prepare_human_revision", return_value=(self.revised, "Fictional prompt")), \
                patch("human_revision.execute_analysis") as editor, \
                patch("human_revision.run_review", return_value=self.reviewed) as reviewer, \
                patch("human_revision.refresh_reviewed_package") as package_renderer:
            package = human_revision(self.original, self.previous, self.request_file, root=self.root)
        editor.assert_called_once()
        reviewer.assert_called_once()
        package_renderer.assert_called_once()
        return package

    def test_model_pass_keeps_final_approval_false_and_attaches_review(self):
        package = self.run_case("pass")
        self.assertEqual(workflow.read_json(package / "status.json")["status"], "awaiting_human_review")
        decision = workflow.read_json(package / "HUMAN_DECISION.json")
        self.assertFalse(decision["human_final_approval"])
        self.assertEqual(decision["conditions_still_for_user_decision"], ["Fictional unresolved condition"])
        self.assertIn("Attached review record", (package / "BRIEFING.md").read_text())
        self.assertIn("Analysis SHA-256", (package / "REVIEW.md").read_text())

    def test_second_requested_edit_stops_for_user_instead_of_looping(self):
        package = self.run_case("revise")
        state = workflow.read_json(package / "status.json")
        self.assertEqual(state["status"], "needs_human_resolution")
        self.assertEqual(state["corrections"], 1)
        self.assertFalse(state["human_approval"])

    def test_approval_cannot_be_submitted_as_edit_request(self):
        workflow.write_json(self.request_file, {"decision": "approved", "human_final_approval": True, "requested_changes": ["Fictional edit"]})
        with patch("human_revision.audit_analysis", return_value={"passed": True}), \
                patch("human_revision.audit_review", return_value={"passed": True}):
            with self.assertRaisesRegex(ValueError, "not approval"):
                prepare_human_revision(self.original, self.previous, self.request_file, self.root)


if __name__ == "__main__":
    unittest.main()
