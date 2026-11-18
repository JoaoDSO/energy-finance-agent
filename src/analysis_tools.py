"""Analysis-stage tools, validation and readable draft generation."""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from jsonschema import Draft202012Validator

from finance import FinancialLedger, amount
from mcp_server import ResearchTools
import workflow


def validate_analysis(analysis, ledger, calculations, research, root=workflow.ROOT):
    schema_path = root / "config/analysis.schema.json"
    schema = workflow.read_json(schema_path if schema_path.is_file() else workflow.ROOT / "config/analysis.schema.json")
    errors = [f"{'.'.join(map(str, error.path))}: {error.message}" for error in Draft202012Validator(schema).iter_errors(analysis)]
    if errors:
        return {"passed": False, "errors": errors}
    if len(analysis["claims"]) < 4:
        errors.append("At least four evidence-linked claims are required")
    if not analysis["assessment"].strip() or not analysis["limitations"]:
        errors.append("An assessment and explicit limitations are required")
    seen = set()
    for claim in analysis["claims"]:
        if not claim["id"] or claim["id"] in seen:
            errors.append("Claim IDs must be nonempty and unique")
        seen.add(claim["id"])
        if not claim["text"].strip():
            errors.append(f"{claim['id']}: empty claim")
        unknown = set(claim["metric_ids"]) - set(ledger.metrics)
        if unknown:
            errors.append(f"{claim['id']}: unknown metric IDs")
            continue
        if any(ref < 1 or ref > len(research["findings"]) for ref in claim["research_refs"]):
            errors.append(f"{claim['id']}: unknown research finding reference")
        if not claim["metric_ids"] and not claim["research_refs"]:
            errors.append(f"{claim['id']}: claim has no evidence")
        if claim["kind"] == "calculated":
            calculation = calculations.get(claim["calculation_id"])
            if calculation is None:
                errors.append(f"{claim['id']}: calculation was not executed by the tool")
                continue
            try:
                if amount(claim["value"]) != amount(calculation["value"]) or claim["unit"] != calculation["unit"]:
                    errors.append(f"{claim['id']}: primary value/unit differs from tool result")
            except Exception:
                errors.append(f"{claim['id']}: invalid primary calculation value")
            if not set(calculation["metric_ids"]).issubset(claim["metric_ids"]):
                errors.append(f"{claim['id']}: missing calculation input references")
        elif claim["kind"] == "reported":
            if claim["calculation_id"] is not None:
                errors.append(f"{claim['id']}: reported figure must not claim a calculation")
            try:
                if not any(amount(ledger.metrics[mid]["value"]) == amount(claim["value"]) and ledger.metrics[mid]["unit"] == claim["unit"] for mid in claim["metric_ids"]):
                    errors.append(f"{claim['id']}: reported primary value/unit is absent from cited metrics")
            except Exception:
                errors.append(f"{claim['id']}: invalid reported value")
        elif any(claim[key] is not None for key in ("value", "unit", "calculation_id")):
            errors.append(f"{claim['id']}: interpretation must not invent a primary numeric value")
    # New cases declare their own required checks; legacy runs keep their historical gate.
    required = ledger.config.get("required_checks")
    if required is None:
        required = [
            {"operation": "fcf_bridge", "period": "9M2026", "reconciled": True},
            {"operation": "net_debt_bridge", "period": "9M2026", "reconciled": True},
            {"operation": "cfo_growth_pct", "period": "9M2026", "reference_period": "9M2025"},
        ]
    for item in required:
        matches = [calc for calc in calculations.values() if all(calc.get(key) == value for key, value in item.items())]
        if not matches:
            errors.append("Missing required case calculation: " + json.dumps(item, sort_keys=True))
    return {"passed": not errors, "errors": errors,
            "scope": "Schema, metric references, primary values, executed calculations and required reconciliations. Free-text reasoning still requires independent review."}


def render_brief(analysis, ledger, calculations, research, research_run, source_additions=None):
    lines = ["# " + analysis["title"], "", "Draft for independent review. Historical source checks and arithmetic checks have passed; the financial interpretation is not approved.", "", analysis["assessment"], "", "## Evidence and analysis", ""]
    for claim in analysis["claims"]:
        lines.extend([f"### {claim['id']} · {claim['kind']}", "", claim["text"], ""])
        if claim["value"] is not None:
            lines.extend([f"Checked primary value: **{claim['value']} {claim['unit']}**.", ""])
        sources = {(ledger.metrics[mid]["source_id"], ledger.metrics[mid]["page"]) for mid in claim["metric_ids"]}
        for ref in claim["research_refs"]:
            finding = research["findings"][ref - 1]
            sources.add((finding["source_id"], finding["page"]))
        # Delivery rendering can include the validated reviewer's source evidence too.
        # This fixes source-line omissions without altering the saved agent response.
        sources.update((source_additions or {}).get(claim["id"], set()))
        links = [f"[{source} PDF p. {page}](../../data/raw/{source}.pdf#page={page})" for source, page in sorted(sources)]
        lines.extend(["Sources: " + "; ".join(links) + ".", ""])
        if claim["calculation_id"]:
            calc = calculations[claim["calculation_id"]]
            lines.extend([f"Calculation {claim['calculation_id']}: {calc['formula']}.", ""])
        if claim["caveat"]:
            lines.extend(["Qualification: " + claim["caveat"], ""])
    for title, key in (("Risks to watch", "risks_to_watch"), ("Open questions", "open_questions"), ("Limitations", "limitations")):
        lines.extend(["## " + title, ""] + ["- " + text for text in analysis[key]] + [""])
    lines.extend(["## Reproducibility", "", f"Research handoff: `{research_run}`.", "", "The initial metric tables were transcribed and visually checked by the project builder. Automated refresh/extraction of metrics is not implemented. Source fingerprints, excerpts and numeric column values are rechecked by code.", "", "| Calculation | Period | Result | Residual |", "| --- | --- | ---: | ---: |"])
    for cid, item in calculations.items():
        lines.append(f"| {cid}: {item['operation']} | {item['period']} | {item['value']} {item['unit']} | {item['residual'] if item['residual'] is not None else 'n/a'} |")
    return "\n".join(lines) + "\n"


class AnalysisTools:
    def __init__(self, run_dir, root=workflow.ROOT):
        self.root = root
        self.run_dir = Path(run_dir).resolve()
        if not self.run_dir.is_relative_to((root / "runs").resolve()):
            raise ValueError("Analysis output must be inside project runs")
        self.handoff = workflow.read_json(self.run_dir / "handoff.json")
        self.research = workflow.read_json(self.run_dir / "inputs/research.json")
        self.ledger = FinancialLedger(root, workflow.read_json(self.run_dir / "inputs/financial_tables.json"))
        self.source = ResearchTools(root, client_name="codex")
        self.source.run_dir = self.run_dir / "tools"
        self.calculations_path = self.run_dir / "calculations.json"
        self.calculations = workflow.read_json(self.calculations_path) if self.calculations_path.exists() else {}

    def get_analysis_task(self):
        self.source._log("get_analysis_task", {}, "ok")
        revision = self.run_dir / "inputs/revision_request.json"
        return {"question": self.research["question"], "research": self.research,
                "revision_request": workflow.read_json(revision) if revision.exists() else None,
                "periods": self.ledger.periods, "instructions": (self.root / "agents/analysis.md").read_text(encoding="utf-8"),
                "metric_provenance": self.ledger.config["provenance"],
                "operations": list(self.ledger.config.get("operations", {name: None for name in ("fcf_bridge", "net_debt_bridge", "funding_gap", "investment_coverage_pct", "net_debt_change", "cfo_growth_pct", "fcf_change")})),
                "required_checks": self.ledger.config.get("required_checks"),
                "schema": workflow.read_json((self.root / "config/analysis.schema.json") if (self.root / "config/analysis.schema.json").is_file() else (workflow.ROOT / "config/analysis.schema.json"))}

    def get_metrics(self, period):
        metrics = self.ledger.for_period(period)
        self.source._log("get_metrics", {"period": period}, f"{len(metrics)} metrics")
        return metrics

    def calculate_financial(self, operation, period, reference_period=None):
        try:
            result = self.ledger.calculate(operation, period, reference_period)
        except (ValueError, KeyError) as error:
            self.source._log("calculate_financial", {"operation": operation, "period": period, "reference_period": reference_period}, "rejected: " + str(error))
            raise
        existing = next((cid for cid, item in self.calculations.items() if item == result), None)
        calculation_id = existing or f"C{len(self.calculations) + 1:02d}"
        self.calculations[calculation_id] = result
        workflow.write_json(self.calculations_path, self.calculations)
        self.source._log("calculate_financial", {"operation": operation, "period": period, "reference_period": reference_period, "calculation_id": calculation_id}, "ok")
        return {"calculation_id": calculation_id, **result,
                "inputs": [self.ledger.metrics[mid] for mid in result["metric_ids"]]}

    def submit_analysis_pack(self, analysis):
        checked = validate_analysis(analysis, self.ledger, self.calculations, self.research, self.root)
        if not checked["passed"]:
            self.source._log("submit_analysis_pack", {}, "rejected")
            return {"saved": False, "checks": checked}
        submission = self.run_dir / "submissions" / uuid4().hex
        workflow.write_json(submission / "analysis.json", analysis)
        workflow.write_json(submission / "validation.json", checked)
        artifact_sha256 = workflow.sha256(submission / "analysis.json")
        self.source._log("submit_analysis_pack", {"submission": str(submission.relative_to(self.root)),
                                                  "analysis_sha256": artifact_sha256}, "saved")
        return {"saved": True, "analysis_path": str((submission / "analysis.json").relative_to(self.root)),
                "analysis_sha256": artifact_sha256, "status": "awaiting_independent_review", "checks": checked}
