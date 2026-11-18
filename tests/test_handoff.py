"""Handoff and routing tests; all input findings are explicitly fictional."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from analysis_stage import select_research
from mcp_server import ResearchTools
import workflow


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        text = "These are fictional source words created exclusively for workflow tests."
        (self.root / "data").mkdir()
        (self.root / "data/pages.jsonl").write_text(json.dumps({"source_id": "fixture", "page": 1, "text": text}) + "\n", encoding="utf-8")
        self.research = {"question": "Fictional question", "findings": [
            {"claim": "Fictional finding", "source_id": "fixture", "page": 1, "excerpt": text, "caveat": "Test only"}
            for _ in range(3)], "gaps": [], "next_checks": []}
        self.run = self.root / "runs/20260101T000000Z"
        self.saved = self.root / "runs/codex_mcp/fixture/submissions/fixture/research.json"
        workflow.write_json(self.run / "research.json", self.research)
        workflow.write_json(self.saved, self.research)
        calls = [
            {"tool": "read_report_page", "arguments": {"source_id": "fixture", "page": 1}, "result": {}},
            {"tool": "submit_research_pack", "arguments": {}, "result": {"structured_content": {"saved": True, "research_path": str(self.saved.relative_to(self.root))}}}]
        (self.run / "events.jsonl").write_text("".join(json.dumps({"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "energy_finance", "status": "completed", "error": None, **call}}) + "\n" for call in calls), encoding="utf-8")

    def test_selects_the_matching_validated_tool_handoff(self):
        run, saved, research = select_research(root=self.root)
        self.assertEqual((run, saved, research), (self.run, self.saved, self.research))

    def test_rejects_final_answer_that_differs_from_saved_handoff(self):
        altered = copy.deepcopy(self.research)
        altered["question"] = "Changed question"
        workflow.write_json(self.saved, altered)
        with self.assertRaisesRegex(ValueError, "differs"):
            select_research(root=self.root)

    def test_invalid_evidence_stops_selection(self):
        self.research["findings"][0]["excerpt"] = "Invented quotation not present in the fictional source."
        workflow.write_json(self.run / "research.json", self.research)
        with self.assertRaisesRegex(ValueError, "failed evidence"):
            select_research(self.run, self.root)

    def claude_submission(self):
        tools = ResearchTools(self.root)
        tools.read_report_page("fixture", 1)
        accepted = tools.submit_research_pack(self.research)
        self.assertTrue(accepted["saved"])
        return tools.run_dir, self.root / accepted["research_path"]

    def test_claude_saved_pack_is_selected_without_copying_text(self):
        run, saved = self.claude_submission()
        self.assertEqual(select_research(run, self.root), (run, saved, self.research))
        self.assertEqual(select_research(root=self.root)[0], run)

    def test_claude_tampered_pack_is_rejected(self):
        run, saved = self.claude_submission()
        altered = copy.deepcopy(self.research)
        altered["question"] = "Changed after submission"
        workflow.write_json(saved, altered)
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            select_research(run, self.root)

    def test_claude_missing_read_trace_is_rejected(self):
        run, _ = self.claude_submission()
        lines = [json.loads(line) for line in (run / "events.jsonl").read_text().splitlines()]
        (run / "events.jsonl").write_text("".join(json.dumps(event) + "\n" for event in lines if event["tool"] != "read_report_page"))
        with self.assertRaisesRegex(ValueError, "not read"):
            select_research(run, self.root)

    def test_unfinished_claude_session_is_skipped_by_automatic_selection(self):
        tools = ResearchTools(self.root)
        tools.read_report_page("fixture", 1)
        self.assertEqual(select_research(root=self.root)[0], self.run)
        with self.assertRaisesRegex(ValueError, "no accepted"):
            select_research(tools.run_dir, self.root)

    def test_claude_trace_cannot_point_to_another_session(self):
        run, saved = self.claude_submission()
        another = self.root / "runs/claude/another/submissions/fixture/research.json"
        workflow.write_json(another, self.research)
        lines = [json.loads(line) for line in (run / "events.jsonl").read_text().splitlines()]
        lines[-1]["arguments"]["submission"] = str(another.parent.relative_to(self.root))
        (run / "events.jsonl").write_text("".join(json.dumps(event) + "\n" for event in lines))
        with self.assertRaisesRegex(ValueError, "outside"):
            select_research(run, self.root)

    def test_pipeline_passes_research_output_directly_to_analysis(self):
        with patch.object(sys, "argv", ["workflow.py", "pipeline"]), patch("workflow.research", return_value=self.run) as research, patch("analysis_stage.analyze") as analyze:
            self.assertEqual(workflow.main(), 0)
            research.assert_called_once_with(root=workflow.ROOT)
            analyze.assert_called_once_with(self.run, preview=False, root=workflow.ROOT)

    def test_pipeline_reuses_selected_research_without_another_model_run(self):
        with patch.object(sys, "argv", ["workflow.py", "pipeline", "--research-run", str(self.run)]), patch("workflow.research") as research, patch("analysis_stage.analyze") as analyze:
            self.assertEqual(workflow.main(), 0)
            research.assert_not_called()
            analyze.assert_called_once_with(self.run, preview=False, root=workflow.ROOT)

    def test_pipeline_does_not_start_analysis_after_research_failure(self):
        with patch.object(sys, "argv", ["workflow.py", "pipeline"]), patch("workflow.research", side_effect=ValueError("intentional test failure")), patch("analysis_stage.analyze") as analyze:
            self.assertEqual(workflow.main(), 1)
            analyze.assert_not_called()


if __name__ == "__main__":
    unittest.main()
