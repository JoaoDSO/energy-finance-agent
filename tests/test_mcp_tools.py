import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mcp_server import ResearchTools


class MCPToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "data").mkdir()
        (self.root / "data/pages.jsonl").write_text(json.dumps({
            "source_id": "fixture", "page": 1,
            "text": "Fictional test: these cash flow figures require a proper reporting-period check."}) + "\n", encoding="utf-8")
        self.research = {"question": "Fictional question", "findings": [
            {"claim": "Fictional claim", "source_id": "fixture", "page": 1,
             "excerpt": "these cash flow figures require a proper reporting-period check", "caveat": "Test fixture"}
            for _ in range(3)], "gaps": [], "next_checks": []}
        self.tools = ResearchTools(self.root)

    def test_submission_requires_page_read_in_session(self):
        result = self.tools.submit_research_pack(self.research)
        self.assertFalse(result["saved"])
        self.assertFalse(list(self.root.rglob("research.json")))

    def test_read_then_submit_creates_auditable_handoff(self):
        self.tools.read_report_page("fixture", 1)
        result = self.tools.submit_research_pack(self.research)
        self.assertTrue(result["saved"])
        self.assertEqual(json.loads((self.root / result["research_path"]).read_text()), self.research)
        events = (self.tools.run_dir / "events.jsonl").read_text()
        self.assertIn("read_report_page", events)
        self.assertIn("submit_research_pack", events)

    def test_second_submission_preserves_first_version(self):
        self.tools.read_report_page("fixture", 1)
        one = self.tools.submit_research_pack(self.research)
        two = self.tools.submit_research_pack(self.research)
        self.assertNotEqual(one["research_path"], two["research_path"])
        self.assertTrue((self.root / one["research_path"]).exists())

    def test_invented_evidence_cannot_be_saved(self):
        self.tools.read_report_page("fixture", 1)
        invalid = copy.deepcopy(self.research)
        invalid["findings"][0]["excerpt"] = "This long quotation is invented and cannot be found on the page."
        self.assertFalse(self.tools.submit_research_pack(invalid)["saved"])

    def test_search_results_are_logged(self):
        self.assertEqual(len(self.tools.search_reports("cash flow", "fixture")), 1)
        self.assertIn("search_reports", (self.tools.run_dir / "events.jsonl").read_text())

    def test_malformed_findings_return_correction_instead_of_crashing(self):
        self.assertFalse(self.tools.submit_research_pack({"findings": None})["saved"])

    def test_codex_client_label_does_not_claim_a_claude_run(self):
        tools = ResearchTools(self.root, client_name="codex")
        tools.read_report_page("fixture", 1)
        result = tools.submit_research_pack(self.research)
        status = json.loads((self.root / result["research_path"]).with_name("status.json").read_text())
        self.assertEqual(status["origin"], "codex_mcp")
        self.assertIn("codex_mcp", result["research_path"])


if __name__ == "__main__":
    unittest.main()
