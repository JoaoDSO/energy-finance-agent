"""Local research tools for free Claude Desktop; no model API calls."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import workflow


class ResearchTools:
    def __init__(self, root: Path = workflow.ROOT, client_name: str = "claude_desktop"):
        if client_name not in ("claude_desktop", "codex"):
            raise ValueError("Unknown MCP client label")
        self.root = root
        self.client_name = client_name
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:10]
        self.run_dir = root / "runs" / ("claude" if client_name == "claude_desktop" else "codex_mcp") / self.run_id

    def _log(self, name: str, arguments: dict[str, Any], outcome: str):
        self.run_dir.mkdir(parents=True, exist_ok=True)
        event = {"at": workflow.utc_now(), "client": self.client_name, "tool": name, "arguments": arguments, "outcome": outcome}
        with (self.run_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def get_research_task(self) -> dict[str, Any]:
        result = {"configuration": workflow.read_json(self.root / "config/sources.json"),
                  "instructions": (self.root / "agents/claude_research.md").read_text(encoding="utf-8"),
                  "output_schema": workflow.read_json(workflow.ROOT / "config/research.schema.json"),
                  "run_id": self.run_id}
        self._log("get_research_task", {}, "ok")
        return result

    def search_reports(self, query: str, source_id: str, limit: int = 6) -> list[dict[str, Any]]:
        if not 1 <= limit <= 12:
            raise ValueError("Limit must be between 1 and 12")
        result = workflow.search(query, limit=limit, root=self.root, source_id=source_id)
        self._log("search_reports", {"query": query, "source_id": source_id, "limit": limit}, f"{len(result)} matches")
        return result

    def read_report_page(self, source_id: str, page: int) -> dict[str, Any]:
        matches = [item for item in workflow.pages(self.root) if item["source_id"] == source_id and item["page"] == page]
        if not matches:
            self._log("read_report_page", {"source_id": source_id, "page": page}, "not found")
            raise ValueError("Unknown source/page")
        self._log("read_report_page", {"source_id": source_id, "page": page}, "ok")
        return matches[0]

    def submit_research_pack(self, research: dict[str, Any]) -> dict[str, Any]:
        checked = workflow.check_evidence(research, root=self.root)
        if not checked["passed"]:
            self._log("submit_research_pack", {}, "rejected invalid structure or excerpts")
            return {"saved": False, "run_id": self.run_id, "checks": checked}
        # Every accepted finding must have been read through this session's page tool.
        log_path = self.run_dir / "events.jsonl"
        events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()] if log_path.exists() else []
        read_pages = {(event["arguments"]["source_id"], event["arguments"]["page"])
                      for event in events if event["tool"] == "read_report_page" and event["outcome"] == "ok"}
        for finding in research.get("findings", []):
            if isinstance(finding, dict) and (finding.get("source_id"), finding.get("page")) not in read_pages:
                checked["errors"].append("A cited page was not read through read_report_page in this session")
        checked["passed"] = not checked["errors"]
        if not checked["passed"]:
            self._log("submit_research_pack", {"findings": len(research.get("findings", []))}, "rejected")
            return {"saved": False, "run_id": self.run_id, "checks": checked}
        # Do not replace a previous accepted version on a second tool call.
        submission = self.run_dir / "submissions" / uuid4().hex
        workflow.write_json(submission / "research.json", research)
        workflow.write_json(submission / "evidence_check.json", checked)
        workflow.write_json(submission / "status.json", {
            "origin": self.client_name + "_mcp", "status": "awaiting_analysis",
            "run_id": self.run_id, "created_at": workflow.utc_now(),
            "note": "Local tools executed; model identity and financial correctness are not established by this status."})
        fingerprint = workflow.sha256(submission / "research.json")
        self._log("submit_research_pack", {"findings": len(research["findings"]),
                  "submission": str(submission.relative_to(self.root)), "research_sha256": fingerprint}, "saved")
        return {"saved": True, "run_id": self.run_id,
                "research_path": str((submission / "research.json").relative_to(self.root)),
                "research_sha256": fingerprint, "checks": checked}


def main():
    from mcp.server import MCPServer

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", choices=("claude_desktop", "codex"), default="claude_desktop")
    parser.add_argument("--stage", choices=("research", "analysis", "review"), default="research")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--case", help="Case ID under cases/; research stage only")
    args = parser.parse_args()
    try:
        case_root = workflow.case_root(args.case)
    except ValueError as error:
        parser.error(str(error))
    server = MCPServer("energy-finance")
    if args.stage == "review":
        if args.run_dir is None:
            parser.error("Review stage requires --run-dir")
        from review_tools import ReviewTools
        review = ReviewTools(args.run_dir, root=case_root)
        tools = review.source

        @server.tool()
        def get_review_task() -> dict[str, Any]:
            """Receive the draft, research and calculation records for an independent evidence review."""
            return review.get_review_task()

        @server.tool()
        def get_metrics(period: str) -> list[dict[str, Any]]:
            """Inspect source-checked values, periods, units, definitions and original row excerpts."""
            return review.get_metrics(period)

        @server.tool()
        def check_calculation(calculation_id: str) -> dict[str, Any]:
            """Independently recompute one saved calculation against the source-checked ledger."""
            return review.check_calculation(calculation_id)

        @server.tool()
        def submit_review_pack(review_pack: dict[str, Any]) -> dict[str, Any]:
            """Validate and save claim-by-claim findings and a pass/revise recommendation; never human approval."""
            return review.submit_review_pack(review_pack)
    elif args.stage == "analysis":
        if args.run_dir is None:
            parser.error("Analysis stage requires --run-dir")
        from analysis_tools import AnalysisTools
        analysis = AnalysisTools(args.run_dir, root=case_root)
        tools = analysis.source

        @server.tool()
        def get_analysis_task() -> dict[str, Any]:
            """Receive the validated research handoff, financial question, periods and analysis instructions."""
            return analysis.get_analysis_task()

        @server.tool()
        def get_metrics(period: str) -> list[dict[str, Any]]:
            """Read the source-checked metric ledger for a reporting period, with IDs, units and source references."""
            return analysis.get_metrics(period)

        @server.tool()
        def calculate_financial(operation: str, period: str, reference_period: str | None = None) -> dict[str, Any]:
            """Execute a checked financial calculation using ledger IDs. Returns reproducible inputs, result and reconciliation residual where applicable."""
            return analysis.calculate_financial(operation, period, reference_period)

        @server.tool()
        def submit_analysis_pack(analysis_pack: dict[str, Any]) -> dict[str, Any]:
            """Validate and save a draft financial analysis for independent review; does not approve its interpretation."""
            return analysis.submit_analysis_pack(analysis_pack)
    else:
        tools = ResearchTools(root=case_root, client_name=args.client)

        @server.tool()
        def get_research_task() -> dict[str, Any]:
            """Get the configured financial question, source IDs, instructions and research output schema."""
            return tools.get_research_task()

        @server.tool()
        def submit_research_pack(research: dict[str, Any]) -> dict[str, Any]:
            """Validate citations and save a new evidence pack locally for Codex. Does not publish or call a model."""
            return tools.submit_research_pack(research)

    @server.tool()
    def search_reports(query: str, source_id: str, limit: int = 6) -> list[dict[str, Any]]:
        """Find report pages matching a phrase. Select one source ID from the research configuration."""
        return tools.search_reports(query, source_id, limit)

    @server.tool()
    def read_report_page(source_id: str, page: int) -> dict[str, Any]:
        """Read a complete indexed report page before citing it; page is the one-based PDF page."""
        return tools.read_report_page(source_id, page)

    server.run(transport="stdio")


if __name__ == "__main__":
    main()
