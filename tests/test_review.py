"""Fictional review fixtures and controller safety tests; no model calls."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import test_finance as fixtures
from analysis_tools import validate_analysis
from review_tools import ReviewTools, validate_review
import review_stage
import workflow


class ReviewTests(unittest.TestCase):
    def test_reviewed_source_lines_merge_without_changing_claim(self):
        briefing = ("## Evidence and analysis\n\n### A1 · reported\n\nFictional claim.\n\n"
                    "Sources: [fixture PDF p. 1](../../data/raw/fixture.pdf#page=1).\n\n"
                    "### A2 · reported\n\nOther claim.\n\nSources: [fixture PDF p. 3](../../data/raw/fixture.pdf#page=3).\n")
        review = {"claim_checks": [{"claim_id": "A1", "evidence": [
            {"source_id": "fixture", "page": 1}, {"source_id": "fixture", "page": 2}]}]}
        result = review_stage.merge_review_source_lines(briefing, review)
        self.assertIn("Fictional claim.", result)
        self.assertIn("[fixture PDF p. 2](../../data/raw/fixture.pdf#page=2)", result)
        self.assertEqual(result.count("[fixture PDF p. 1]"), 1)
        self.assertIn("### A2 · reported\n\nOther claim.", result)

    def setUp(self):
        self.fixture = fixtures.FinanceTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        workflow.write_json(self.root / "config/review.schema.json", workflow.read_json(workflow.ROOT / "config/review.schema.json"))
        self.calculations = self.fixture.calculations()
        self.analysis = self.fixture.draft(self.calculations)
        self.run = self.root / "runs/review_fixture"
        for name, value in (("analysis", self.analysis), ("research", self.fixture.research),
                            ("calculations", self.calculations), ("financial_tables", self.fixture.config)):
            workflow.write_json(self.run / f"inputs/{name}.json", value)
        self.tools = ReviewTools(self.run, self.root)
        self.review = {"verdict": "pass", "summary": "Fictional source-backed review",
                       "claim_checks": [{"claim_id": c["id"], "result": "supported", "rationale": "Checked fictional source and period",
                                         "evidence": [{"source_id": "fixture", "page": 1, "excerpt": "Operating cash flow 80 60\nGross investments (120) (110)"}]}
                                        for c in self.analysis["claims"]],
                       "findings": [], "sections_checked": ["assessment", "risks_to_watch", "open_questions", "limitations"],
                       "limitations": ["Fictional test, not a financial conclusion"]}
        self.checked = {cid: True for cid in self.calculations}

    def validate(self, review=None, checked=None, read=None):
        return validate_review(self.review if review is None else review, self.analysis, self.calculations,
                               self.checked if checked is None else checked, {("fixture", 1)} if read is None else read, self.root)

    def test_complete_review_passes(self):
        self.assertTrue(self.validate()["passed"])

    def test_missing_claim_is_rejected(self):
        self.review["claim_checks"].pop()
        self.assertFalse(self.validate()["passed"])

    def test_duplicate_claim_is_rejected(self):
        self.review["claim_checks"].append(self.review["claim_checks"][0])
        self.assertFalse(self.validate()["passed"])

    def test_skipped_calculation_is_rejected(self):
        self.assertFalse(self.validate(checked={})["passed"])

    def test_unread_source_cannot_pass(self):
        self.assertFalse(self.validate(read=set())["passed"])

    def test_invented_excerpt_cannot_pass(self):
        self.review["claim_checks"][0]["evidence"][0]["excerpt"] = "This fictional source never actually included these words."
        self.assertFalse(self.validate()["passed"])

    def test_material_finding_blocks_pass(self):
        self.review["findings"] = [{"id": "F1", "severity": "material", "claim_id": "C01", "issue": "Wrong sign",
                                    "requested_change": "Correct sign", "evidence": []}]
        self.assertFalse(self.validate()["passed"])
        self.review["verdict"] = "revise"
        self.assertTrue(self.validate()["passed"])

    def test_revision_requires_specific_action(self):
        self.review["verdict"] = "revise"
        self.assertFalse(self.validate()["passed"])
        self.review["findings"] = [{"id": "F1", "severity": "minor", "claim_id": None, "issue": "Missing caveat",
                                    "requested_change": "", "evidence": []}]
        self.assertFalse(self.validate()["passed"])

    def test_unchecked_narrative_blocks_pass(self):
        self.review["sections_checked"].remove("assessment")
        self.assertFalse(self.validate()["passed"])

    def test_needs_revision_claim_blocks_pass_even_if_finding_minor(self):
        self.review["claim_checks"][0]["result"] = "needs_revision"
        self.assertFalse(self.validate()["passed"])
        self.review["findings"] = [{"id": "F1", "severity": "minor", "claim_id": "C01", "issue": "Clarify description",
                                    "requested_change": "Clarify the period", "evidence": []}]
        self.assertFalse(self.validate()["passed"])

    def test_tool_submission_requires_own_reads_and_calculations(self):
        self.assertFalse(self.tools.submit_review_pack(self.review)["saved"])
        self.tools.source.read_report_page("fixture", 1)
        for cid in self.calculations:
            self.assertTrue(self.tools.check_calculation(cid)["matches"])
        first = self.tools.submit_review_pack(self.review)
        second = self.tools.submit_review_pack(self.review)
        self.assertTrue(first["saved"])
        self.assertFalse(first["human_approval"])
        self.assertNotEqual(first["review_path"], second["review_path"])
        self.assertTrue((self.root / first["review_path"]).is_file())

    def test_seeded_primary_sign_error_is_caught_before_model_review(self):
        draft = copy.deepcopy(self.analysis)
        draft["claims"][0]["value"] = "20.00"  # Correct fictional FCF is -20.
        self.assertFalse(validate_analysis(draft, self.fixture.ledger, self.calculations, self.fixture.research, self.root)["passed"])

    def test_seeded_missing_evidence_is_caught_before_model_review(self):
        draft = copy.deepcopy(self.analysis)
        draft["claims"][-1]["metric_ids"] = []
        self.assertFalse(validate_analysis(draft, self.fixture.ledger, self.calculations, self.fixture.research, self.root)["passed"])

    def test_seeded_mismatched_period_is_blocked_by_calculator(self):
        ledger = self.fixture.ledger
        ledger.periods["FY2025"] = {**ledger.periods["9M2025"], "months": 12, "end": "2025-12-31"}
        with self.assertRaisesRegex(ValueError, "matching reporting intervals"):
            ledger.calculate("cfo_growth_pct", "9M2026", "FY2025")

    def test_audit_checks_trace_and_input_fingerprints(self):
        analysis_run = self.root / "runs/analysis_fixture"
        workflow.write_json(analysis_run / "analysis.json", self.analysis)
        source, snapshot = analysis_run / "analysis.json", self.run / "inputs/analysis.json"
        workflow.write_json(self.run / "handoff.json", {"analysis_run": str(analysis_run.relative_to(self.root)), "fingerprints": [
            {"source": str(source.relative_to(self.root)), "source_sha256": workflow.sha256(source),
             "snapshot": str(snapshot.relative_to(self.root)), "snapshot_sha256": workflow.sha256(snapshot)}]})
        self.tools.source.read_report_page("fixture", 1)
        calls = [{"tool": "get_review_task", "arguments": {}, "result": {}},
                 {"tool": "get_metrics", "arguments": {"period": "9M2026"}, "result": {}},
                 {"tool": "read_report_page", "arguments": {"source_id": "fixture", "page": 1}, "result": {}}]
        for cid in self.calculations:
            result = self.tools.check_calculation(cid)
            calls.append({"tool": "check_calculation", "arguments": {"calculation_id": cid}, "result": {"structured_content": result}})
        result = self.tools.submit_review_pack(self.review)
        calls.append({"tool": "submit_review_pack", "arguments": {}, "result": {"structured_content": result}})
        workflow.write_json(self.run / "review.json", self.review)
        (self.run / "events.jsonl").write_text("".join(json.dumps({"type": "item.completed", "item": {
            "type": "mcp_tool_call", "server": "energy_finance", "status": "completed", "error": None, **c}}) + "\n" for c in calls), encoding="utf-8")
        self.assertTrue(review_stage.audit_review(self.run, self.root)["passed"])
        changed = copy.deepcopy(self.analysis)
        changed["assessment"] = "Changed after review"
        workflow.write_json(source, changed)
        self.assertFalse(review_stage.audit_review(self.run, self.root)["passed"])

    def cycle_setup(self):
        analysis_run = self.root / "runs/analysis_cycle_fixture"
        workflow.write_json(analysis_run / "analysis.json", self.analysis)
        (analysis_run / "BRIEFING.md").write_text("Fictional draft, not a company result", encoding="utf-8")
        review_one, review_two = self.root / "runs/review_one", self.root / "runs/review_two"
        workflow.write_json(review_one / "review.json", {"verdict": "revise"})
        workflow.write_json(review_two / "review.json", {"verdict": "revise"})
        return analysis_run, review_one, review_two

    def test_cycle_stops_after_one_correction_even_if_reviewer_requests_more(self):
        analysis, one, two = self.cycle_setup()
        with patch("review_stage.run_review", side_effect=[one, two]) as reviewer, patch("review_stage.revise_analysis", return_value=analysis) as correction:
            cycle = review_stage.review_cycle(analysis, root=self.root)
        self.assertEqual(reviewer.call_count, 2)
        correction.assert_called_once()
        state = workflow.read_json(cycle / "status.json")
        self.assertEqual(state["status"], "needs_human_resolution")
        self.assertFalse(state["human_approval"])

    def test_cycle_pass_does_not_start_unnecessary_correction(self):
        analysis, one, _ = self.cycle_setup()
        workflow.write_json(one / "review.json", {"verdict": "pass"})
        with patch("review_stage.run_review", return_value=one), patch("review_stage.revise_analysis") as correction:
            cycle = review_stage.review_cycle(analysis, root=self.root)
        correction.assert_not_called()
        self.assertEqual(workflow.read_json(cycle / "status.json")["status"], "awaiting_human_review")

    def test_failed_reviewer_does_not_start_correction(self):
        analysis, _, _ = self.cycle_setup()
        with patch("review_stage.run_review", side_effect=ValueError("Fictional failure")), patch("review_stage.revise_analysis") as correction:
            with self.assertRaisesRegex(ValueError, "Fictional failure"):
                review_stage.review_cycle(analysis, root=self.root)
        correction.assert_not_called()
        cycle = next((self.root / "runs").glob("review_cycle_*"))
        self.assertEqual(workflow.read_json(cycle / "status.json")["status"], "failed")

    def test_pipeline_can_route_analysis_directly_to_review(self):
        analysis, _, _ = self.cycle_setup()
        with patch.object(sys, "argv", ["workflow.py", "pipeline", "--research-run", "runs/fixture", "--with-review"]), \
                patch("analysis_stage.analyze", return_value=analysis) as analyst, \
                patch("review_stage.review_cycle") as reviewer, patch("workflow.research") as research:
            self.assertEqual(workflow.main(), 0)
        research.assert_not_called()
        analyst.assert_called_once_with(Path("runs/fixture"), preview=False, root=workflow.ROOT)
        reviewer.assert_called_once_with(analysis, root=workflow.ROOT)


if __name__ == "__main__":
    unittest.main()
