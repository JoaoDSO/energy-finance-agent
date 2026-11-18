"""Read-only evaluation of the saved cross-provider case; no model or approval calls."""
from collections import Counter
from datetime import datetime, timezone
import argparse
import json
from pathlib import Path
import sys
from time import perf_counter

import workflow
from analysis_tools import validate_analysis
from finance import FinancialLedger
from review_tools import validate_review

ROOT = Path(__file__).resolve().parents[1]


def inside(root, name):
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Path outside project: " + str(name))
    return path


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def trace(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def mcp_calls(items):
    return [e["item"] for e in items if e.get("type") == "item.completed"
            and e.get("item", {}).get("type") == "mcp_tool_call"
            and e["item"].get("server") == "energy_finance"
            and e["item"].get("status") == "completed" and not e["item"].get("error")]


def through_submission(items, tool):
    selected = mcp_calls(items)
    accepted = [(index, item) for index, item in enumerate(selected)
                if item["tool"] == tool and item.get("result", {}).get("structured_content", {}).get("saved") is True]
    if accepted:
        index, item = accepted[-1]
        return selected[:index + 1], item
    return selected, None


def check(name, condition, detail):
    return {"name": name, "passed": bool(condition), "detail": detail}


def research(case, root):
    run = inside(root, case["run"])
    source = run / case["submission"]
    pack, items = read(source), trace(run / "events.jsonl")
    evidence = workflow.check_evidence(pack, root)
    cited = {(f["source_id"], f["page"]) for f in pack["findings"]}
    submitted = [e for e in items if e.get("tool") == "submit_research_pack" and e.get("outcome") == "saved"]
    before_handoff = items[:items.index(submitted[0])] if submitted else []
    pages = {(e["arguments"]["source_id"], e["arguments"]["page"])
             for e in before_handoff if e.get("tool") == "read_report_page" and e.get("outcome") == "ok"}
    times = [datetime.fromisoformat(e["at"]) for e in items if "at" in e]
    checks = [check("source_excerpts", evidence["passed"], evidence.get("errors", [])),
              check("pages_read_before_handoff", cited <= pages, f"{len(cited)} cited; {len(pages)} read"),
              check("saved_handoff_hash", len(submitted) == 1 and submitted[0]["arguments"].get("research_sha256") == workflow.sha256(source), "saved SHA-256"),
              check("named_scope", len(pack["findings"]) == case["expected_findings"] and set(case["expected_sources"]) <= {f["source_id"] for f in pack["findings"]}, "two original reports")]
    return checks, {"findings": len(pack["findings"]), "citation_excerpts_checked": len(pack["findings"]),
                    "tool_calls": len(items), "tool_mix": dict(Counter(e.get("tool") for e in items)),
                    "distinct_pages_read": len(pages),
                    "observed_tool_window_seconds": round((max(times) - min(times)).total_seconds(), 3) if len(times) > 1 else None,
                    "note": "Tool window is not total Claude session time; local client label does not attest model identity."}


def analysis(case, root):
    run = inside(root, case["run"])
    handoff, draft = read(run / "handoff.json"), read(run / "analysis.json")
    calculations = read(run / "calculations.json")
    ledger = FinancialLedger(root, read(run / "inputs/financial_tables.json"))
    validation = validate_analysis(draft, ledger, calculations, read(run / "inputs/research.json"), root)
    reproduced = {cid: ledger.calculate(c["operation"], c["period"], c["reference_period"]) == c
                  for cid, c in calculations.items()}
    calls, submission = through_submission(trace(run / "events.jsonl"), "submit_analysis_pack")
    names = Counter(c["tool"] for c in calls)
    calc_ids = {c.get("result", {}).get("structured_content", {}).get("calculation_id")
                for c in calls if c["tool"] == "calculate_financial"}
    fingerprints = (workflow.sha256(run / "inputs/research.json") == handoff["research_snapshot_sha256"]
                    and workflow.sha256(inside(root, handoff["research_file"])) == handoff["research_sha256"]
                    and workflow.sha256(run / "inputs/financial_tables.json") == handoff["financial_tables_snapshot_sha256"])
    checks = [check("handoff_fingerprints", fingerprints, "source and snapshots"),
              check("claims_and_citations", validation["passed"], validation.get("errors", [])),
              check("reproducible_arithmetic", len(calculations) == case["expected_calculations"] and all(reproduced.values()), reproduced),
              check("agent_tool_use", submission is not None and set(calculations) <= calc_ids and names["get_metrics"] > 0 and names["read_report_page"] > 0 and 1 <= names["submit_analysis_pack"] <= 2 and read(inside(root, submission["result"]["structured_content"]["analysis_path"])) == draft, dict(names)),
              check("claim_count", len(draft["claims"]) == case["expected_claims"], len(draft["claims"])),
              check("no_analysis_approval", read(run / "status.json")["status"] == "awaiting_independent_review", "stopped before review")]
    return checks, {"claims": len(draft["claims"]), "calculations_reproduced": sum(reproduced.values()),
                    "tool_calls": len(calls), "tool_mix": dict(names), "model_runtime_seconds": None}


def review(case, root):
    run = inside(root, case["run"])
    handoff, result = read(run / "handoff.json"), read(run / "review.json")
    draft, calculations = read(run / "inputs/analysis.json"), read(run / "inputs/calculations.json")
    calls, submission = through_submission(trace(run / "events.jsonl"), "submit_review_pack")
    names = Counter(c["tool"] for c in calls)
    pages, calculation_checks = set(), {}
    for call in calls:
        args = call.get("arguments", {})
        content = call.get("result", {}).get("structured_content", {})
        if call["tool"] == "read_report_page":
            pages.add((args.get("source_id"), args.get("page")))
        if call["tool"] == "check_calculation":
            cid = args.get("calculation_id")
            calculation_checks[cid] = content.get("matches") is True and content.get("recomputed") == calculations.get(cid)
    validation = validate_review(result, draft, calculations, calculation_checks, pages, root)
    fingerprints = all(workflow.sha256(inside(root, f["source"])) == f["source_sha256"]
                       and workflow.sha256(inside(root, f["snapshot"])) == f["snapshot_sha256"]
                       for f in handoff["fingerprints"])
    checks = [check("review_handoff", fingerprints, "source and snapshots"),
              check("review_citations_and_coverage", validation["passed"], validation.get("errors", [])),
              check("all_calculations_rechecked", len(calculations) == case["expected_calculations"] and set(calculation_checks) == set(calculations) and all(calculation_checks.values()), calculation_checks),
              check("review_agent_tool_use", submission is not None and names["get_review_task"] > 0 and names["read_report_page"] > 0 and names["submit_review_pack"] == 1 and read(inside(root, submission["result"]["structured_content"]["review_path"])) == result, dict(names)),
              check("claim_count", len(result["claim_checks"]) == case["expected_claims"], len(result["claim_checks"])),
              check("model_pass_not_human_approval", result["verdict"] == "pass" and read(run / "review_check.json")["human_approval"] is False, "model recommendation only")]
    return checks, {"claim_checks": len(result["claim_checks"]), "calculation_checks": len(calculation_checks),
                    "tool_calls": len(calls), "tool_mix": dict(names), "model_runtime_seconds": None}


def scope(case, root):
    request = read(inside(root, case["human_request"]))
    revision = read(inside(root, case["revision"]) / "status.json")
    package = inside(root, case["conditional_package"])
    status, decision = read(package / "status.json"), read(package / "HUMAN_DECISION.json")
    hashes = all(workflow.sha256(package / name) == value for name, value in status["artifacts"].items())
    checks = [check("human_override_recorded", request["decision"] == "changes_requested" and len(request["requested_changes"]) > 0 and revision["corrections"] == 1, "first model pass was not final"),
              check("historical_acceptance_scoped", decision["decision"] == "approve_with_conditions" and not decision["human_final_approval"] and len(decision["conditions"]) == case["expected_open_conditions"], "six forward conditions"),
              check("supplements_still_pending", "not yet model-reviewed" in decision["new_supplements"] and not status["human_final_approval"], "C06 and transcript context"),
              check("no_publication_or_sufficiency", not status["publication_authorized"] and not status["funding_sufficiency_approved"], "release blocked"),
              check("package_artifacts_unchanged", hashes, "saved SHA-256 values")]
    return checks, {"human_requested_change_items": len(request["requested_changes"]),
                    "remaining_conditions": len(decision["conditions"]), "human_review_minutes": None}


HANDLERS = {"research": research, "analysis": analysis, "review": review, "scope": scope}


def evaluate(spec_path=ROOT / "evals/cases.json", root=ROOT):
    spec = read(spec_path)
    results = []
    for case in spec["cases"]:
        started = perf_counter()
        try:
            checks, metrics = HANDLERS[case["kind"]](case, root)
        except (OSError, ValueError, KeyError, TypeError, AssertionError) as exc:
            checks, metrics = [check("evaluator_completed", False, f"{type(exc).__name__}: {exc}")], {}
        results.append({"id": case["id"], "passed": all(item["passed"] for item in checks),
                        "checks": checks, "metrics": metrics, "audit_runtime_seconds": round(perf_counter() - started, 3)})
    return {"name": spec["name"], "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "passed": all(item["passed"] for item in results), "cases": results,
            "measurement_limits": spec.get("measurement_limits", "Saved-artifact checks do not measure model accuracy, financial truth, human review time or time savings."),
            "artifact_checks_passed": sum(c["passed"] for item in results for c in item["checks"]),
            "artifact_checks_total": sum(len(item["checks"]) for item in results),
            "model_calls_started": 0, "human_approval_created": False}


def render(result):
    lines = ["# Phase 6 evaluation", "", f"Named set: `{result['name']}`.", "",
             f"Result: **{'pass' if result['passed'] else 'fail'}**; {result['artifact_checks_passed']}/{result['artifact_checks_total']} artifact checks passed.",
             "These are artifact checks, not model accuracy or financial truth. No model call or human approval was created.", ""]
    for case in result["cases"]:
        lines += [f"## {case['id']}", "", f"Status: {'pass' if case['passed'] else 'fail'}. Audit runtime: {case['audit_runtime_seconds']:.3f}s.", ""]
        lines += [f"- {'PASS' if c['passed'] else 'FAIL'} {c['name']}: {c['detail']}" for c in case["checks"]]
        lines += ["", "Metrics: `" + json.dumps(case["metrics"], ensure_ascii=False, sort_keys=True) + "`", ""]
    lines += ["## Measurement limits", "", result["measurement_limits"], ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", help="Configured case ID; omit for the original Ørsted evaluation")
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    root = workflow.case_root(args.case)
    result = evaluate(args.cases or root / "evals/cases.json", root)
    output = (args.output_dir or root / "evals/results" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")).resolve()
    if not output.is_relative_to((root / "evals/results").resolve()):
        parser.error("Output must be inside evals/results")
    output.mkdir(parents=True, exist_ok=False)
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "REPORT.md").write_text(render(result), encoding="utf-8")
    print(output / "REPORT.md")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
