"""Checks use fictional fixtures, not claimed Orsted financial results."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import workflow


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "data").mkdir()
        text = "Fictional fixture: operating cash flow is reported here, with construction cash payments discussed separately."
        (self.root / "data/pages.jsonl").write_text(json.dumps({"source_id": "fixture", "page": 1, "text": text}) + "\n", encoding="utf-8")
        self.valid = {"question": "Test question", "findings": [
            {"claim": "Fixture claim", "source_id": "fixture", "page": 1,
             "excerpt": "operating cash flow is reported here", "caveat": "Fictional test"}
            for _ in range(3)], "gaps": [], "next_checks": []}

    def test_search_is_case_insensitive_and_preserves_page(self):
        result = workflow.search("CASH FLOW", root=self.root)
        self.assertEqual((result[0]["source_id"], result[0]["page"]), ("fixture", 1))

    def test_valid_source_excerpt_passes(self):
        self.assertTrue(workflow.check_evidence(self.valid, self.root)["passed"])

    def test_source_filter_excludes_other_reports(self):
        self.assertEqual(workflow.search("cash flow", root=self.root, source_id="other_report"), [])
        self.assertEqual(len(workflow.search("cash flow", root=self.root, source_id="fixture")), 1)

    def test_wrong_page_is_rejected(self):
        result = copy.deepcopy(self.valid)
        result["findings"][0]["page"] = 2
        self.assertFalse(workflow.check_evidence(result, self.root)["passed"])

    def test_invented_excerpt_is_rejected(self):
        result = copy.deepcopy(self.valid)
        result["findings"][0]["excerpt"] = "This text was invented and is absent from the source."
        self.assertFalse(workflow.check_evidence(result, self.root)["passed"])

    def test_empty_findings_do_not_pass(self):
        self.assertFalse(workflow.check_evidence({"findings": []}, self.root)["passed"])

    def test_external_domain_and_path_traversal_rejected(self):
        for source in (
            {"id": "test", "filename": "test.pdf", "url": "https://example.com/test.pdf"},
            {"id": "test", "filename": "../test.pdf", "url": "https://cdn.orsted.com/test.pdf"},
        ):
            with self.assertRaises(ValueError):
                workflow.validate_source(source)

    def test_api_credentials_not_forwarded(self):
        with patch.dict(workflow.os.environ, {"OPENAI_API_KEY": "fictional-test-key", "CODEX_API_KEY": "fictional-test-key"}):
            self.assertNotIn("OPENAI_API_KEY", workflow.subscription_env())
            self.assertNotIn("CODEX_API_KEY", workflow.subscription_env())

    def test_trace_requires_page_reads_and_successful_handoff(self):
        trace = self.root / "events.jsonl"
        trace.write_text("", encoding="utf-8")
        self.assertFalse(workflow.check_research_trace(trace, self.valid)["passed"])
        items = [
            {"tool": "read_report_page", "arguments": {"source_id": "fixture", "page": 1}, "result": {}},
            {"tool": "submit_research_pack", "arguments": {}, "result": {"structured_content": {"saved": True, "research_path": "runs/fixture/research.json"}}},
        ]
        events = [{"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "energy_finance", "status": "completed", "error": None, **item}} for item in items]
        trace.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        self.assertTrue(workflow.check_research_trace(trace, self.valid)["passed"])

    def test_runtime_uses_local_mcp_without_relaxing_shell_sandbox(self):
        python = self.root / (".venv/Scripts/python.exe" if workflow.os.name == "nt" else ".venv/bin/python")
        python.parent.mkdir(parents=True)
        python.write_bytes(b"test fixture only")
        with patch.object(workflow, "ROOT", self.root):
            command = workflow.research_command("codex", self.root, self.root / "runs/test")
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertTrue(any("mcp_servers.energy_finance.command=" in arg for arg in command))
        self.assertNotIn("danger-full-access", command)


if __name__ == "__main__":
    unittest.main()
