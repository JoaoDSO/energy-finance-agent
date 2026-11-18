"""The accepted analysis tool artifact outranks a different final chat reply."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from analysis_stage import canonicalize_analysis_output, canonicalize_tool_output
import workflow


class AnalysisOutputHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = self.root / "runs/analysis_fixture"
        self.saved = self.run / "submissions/accepted/analysis.json"
        self.submitted = {"title": "Complete fictional analysis", "claims": [1, 2, 3, 4]}
        workflow.write_json(self.saved, self.submitted)
        workflow.write_json(self.saved.parent / "validation.json", {"passed": True})
        self.reply = {"title": "Shortened final reply", "claims": [1]}
        workflow.write_json(self.run / "analysis.json", self.reply)
        self.result = {"saved": True, "analysis_path": str(self.saved.relative_to(self.root)),
                       "analysis_sha256": workflow.sha256(self.saved)}
        self.write_trace()

    def write_trace(self):
        event = {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "energy_finance",
                 "tool": "submit_analysis_pack", "status": "completed", "error": None,
                 "result": {"structured_content": self.result}}}
        (self.run / "events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")

    def test_preserves_mismatched_final_reply_and_uses_validated_submission(self):
        record = canonicalize_analysis_output(self.run, self.root)
        self.assertTrue(record["final_response_mismatch"])
        self.assertEqual(workflow.read_json(self.run / "analysis.json"), self.submitted)
        self.assertEqual(workflow.read_json(self.run / "agent_final_response.json"), self.reply)
        self.assertEqual(workflow.read_json(self.run / "output_handoff.json"), record)

    def test_matching_reply_needs_no_archive(self):
        workflow.write_json(self.run / "analysis.json", self.submitted)
        record = canonicalize_analysis_output(self.run, self.root)
        self.assertFalse(record["final_response_mismatch"])
        self.assertFalse((self.run / "agent_final_response.json").exists())

    def test_rejects_changed_submission(self):
        self.submitted["title"] = "Changed after acceptance"
        workflow.write_json(self.saved, self.submitted)
        with self.assertRaisesRegex(ValueError, "fingerprint changed"):
            canonicalize_analysis_output(self.run, self.root)

    def test_rejects_submission_from_another_run(self):
        other = self.root / "runs/another/submissions/accepted/analysis.json"
        workflow.write_json(other, self.submitted)
        self.result["analysis_path"] = str(other.relative_to(self.root))
        self.result["analysis_sha256"] = workflow.sha256(other)
        self.write_trace()
        with self.assertRaisesRegex(ValueError, "outside this run"):
            canonicalize_analysis_output(self.run, self.root)

    def test_review_uses_the_same_validated_handoff_rule(self):
        saved = self.run / "submissions/reviewed/review.json"
        review = {"verdict": "pass", "claim_checks": [1, 2, 3, 4]}
        workflow.write_json(saved, review)
        workflow.write_json(saved.parent / "validation.json", {"passed": True})
        workflow.write_json(self.run / "review.json", {"verdict": "pass", "claim_checks": [1]})
        self.result = {"saved": True, "review_path": str(saved.relative_to(self.root)),
                       "review_sha256": workflow.sha256(saved)}
        event = {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "energy_finance",
                 "tool": "submit_review_pack", "status": "completed", "error": None,
                 "result": {"structured_content": self.result}}}
        (self.run / "events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
        record = canonicalize_tool_output(self.run, "review", "submit_review_pack", self.root)
        self.assertTrue(record["final_response_mismatch"])
        self.assertEqual(workflow.read_json(self.run / "review.json"), review)
        self.assertEqual(workflow.read_json(self.run / "agent_final_response.json")["claim_checks"], [1])


if __name__ == "__main__":
    unittest.main()
