"""Fresh-session review and one bounded analysis correction; no automatic approval."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess

from analysis_stage import audit_analysis, canonicalize_tool_output, execute_analysis, prepare_analysis, resolve_run
from finance import FinancialLedger
from review_tools import validate_review
import workflow


def new_run(prefix, root):
    output = root / "runs" / (prefix + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    output.mkdir(parents=True)
    return output


def merge_review_source_lines(briefing, review):
    """Add reviewed page references to displayed source lines without editing claims."""
    evidence = {item["claim_id"]: {(page["source_id"], page["page"]) for page in item["evidence"]}
                for item in review.get("claim_checks", [])}
    parts = re.split(r"(?=^### ([^\s]+) · )", briefing, flags=re.MULTILINE)
    if len(parts) == 1:
        return briefing
    output = [parts[0]]
    for index in range(1, len(parts), 2):
        claim_id, section = parts[index:index + 2]
        shown = {(source, int(page)) for source, page in
                 re.findall(r"\[([^\]]+) PDF p\. (\d+)\]", section)}
        missing = sorted(evidence.get(claim_id, set()) - shown)
        if missing:
            additions = "; ".join(f"[{source} PDF p. {page}](../../data/raw/{source}.pdf#page={page})"
                                  for source, page in missing)
            section, count = re.subn(r"^(Sources: .*)\.$", lambda match: match.group(1) + "; " + additions + ".",
                                     section, count=1, flags=re.MULTILINE)
            if count != 1:
                raise ValueError(f"Missing source line for reviewed claim {claim_id}")
        output.append(section)
    return "".join(output)


def prepare_review(analysis_run, previous_review=None, root=workflow.ROOT):
    analysis_run = resolve_run(analysis_run, root)
    if not audit_analysis(analysis_run, root)["passed"]:
        raise ValueError("Analysis failed its checks; review was not started")
    output = new_run("review_", root)
    files = [(analysis_run / "analysis.json", "analysis.json"),
             (analysis_run / "inputs/research.json", "research.json"),
             (analysis_run / "inputs/financial_tables.json", "financial_tables.json"),
             (analysis_run / "calculations.json", "calculations.json")]
    if (analysis_run / "inputs/human_feedback.json").exists():
        files.append((analysis_run / "inputs/human_feedback.json", "human_feedback.json"))
    if previous_review is not None:
        previous_review = resolve_run(previous_review, root)
        if not audit_review(previous_review, root)["passed"]:
            raise ValueError("Previous review failed validation")
        files.append((previous_review / "review.json", "previous_review.json"))
    fingerprints = []
    for source, name in files:
        target = output / "inputs" / name
        workflow.write_json(target, workflow.read_json(source))
        fingerprints.append({"source": str(source.relative_to(root)), "source_sha256": workflow.sha256(source),
                             "snapshot": str(target.relative_to(root)), "snapshot_sha256": workflow.sha256(target)})
    workflow.write_json(output / "handoff.json", {"analysis_run": str(analysis_run.relative_to(root)),
                        "created_at": workflow.utc_now(), "fingerprints": fingerprints})
    prompt = (root / "agents/review.md").read_text(encoding="utf-8")
    (output / "review_prompt.md").write_text(prompt, encoding="utf-8")
    workflow.write_json(output / "status.json", {"stage": "review", "status": "prepared", "model_called": False})
    return output, prompt


def completed_calls(path):
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [e["item"] for e in events if e.get("type") == "item.completed"
            and e.get("item", {}).get("type") == "mcp_tool_call"
            and e["item"].get("server") == "energy_finance"
            and e["item"].get("status") == "completed" and not e["item"].get("error")]


def audit_review(run_dir, root=workflow.ROOT):
    run_dir = resolve_run(run_dir, root)
    handoff = workflow.read_json(run_dir / "handoff.json")
    errors = []
    for item in handoff["fingerprints"]:
        if (workflow.sha256(resolve_run(item["source"], root)) != item["source_sha256"]
                or workflow.sha256(resolve_run(item["snapshot"], root)) != item["snapshot_sha256"]):
            errors.append("Review input/source fingerprint changed")
    analysis = workflow.read_json(run_dir / "inputs/analysis.json")
    calculations = workflow.read_json(run_dir / "inputs/calculations.json")
    ledger = FinancialLedger(root, workflow.read_json(run_dir / "inputs/financial_tables.json"))
    for cid, calc in calculations.items():
        if ledger.calculate(calc["operation"], calc["period"], calc["reference_period"]) != calc:
            errors.append(f"Review input calculation cannot be reproduced: {cid}")
    calls = completed_calls(run_dir / "events.jsonl")
    names = [call["tool"] for call in calls]
    if "get_review_task" not in names:
        errors.append("Independent review task retrieval is missing")
    checked_calculations, read_pages, saved = {}, set(), []
    # Retain only checks performed before the accepted submission, not later calls.
    accepted_state = None
    for call in calls:
        content = call.get("result", {}).get("structured_content", {})
        args = call.get("arguments", {})
        if call["tool"] == "read_report_page":
            read_pages.add((args["source_id"], args["page"]))
        if call["tool"] == "check_calculation":
            cid = args["calculation_id"]
            if content.get("calculation_id") != cid or content.get("recomputed") != calculations.get(cid):
                errors.append("Review calculation trace differs from saved input")
            checked_calculations[cid] = content.get("matches") is True
        if call["tool"] == "submit_review_pack" and content.get("saved") is True:
            saved.append(resolve_run(content["review_path"], root))
            accepted_state = dict(checked_calculations), set(read_pages)
    review = workflow.read_json(run_dir / "review.json")
    output_handoff = workflow.read_json(run_dir / "output_handoff.json") if (run_dir / "output_handoff.json").is_file() else None
    if output_handoff:
        saved_output = resolve_run(output_handoff["authoritative_submission"], root)
        if saved_output.name != "review.json" or saved_output.parent.parent != run_dir / "submissions":
            errors.append("Review handoff points outside this run")
        elif workflow.sha256(saved_output) != output_handoff["review_sha256"] or workflow.sha256(run_dir / "review.json") != output_handoff["review_sha256"]:
            errors.append("Review handoff fingerprint changed")
    if not saved or not saved[-1].is_relative_to(run_dir / "submissions") or workflow.read_json(saved[-1]) != review:
        errors.append("Final review does not match an accepted session-local tool submission")
    state_checks, state_reads = accepted_state or ({}, set())
    checked = validate_review(review, analysis, calculations, state_checks, state_reads, root)
    errors.extend(checked["errors"])
    metric_inspection_route = "get_metrics" if "get_metrics" in names else "check_calculation_inputs"
    if "get_metrics" not in names and not calculations:
        errors.append("Independent review did not retrieve metrics or calculation inputs")
    summary = {"passed": not errors, "errors": errors, "successful_mcp_calls": len(calls),
               "claim_count": len(review.get("claim_checks", [])), "verdict": review.get("verdict"),
               "final_response_mismatch": output_handoff.get("final_response_mismatch", False) if output_handoff else False,
               "metric_inspection_route": metric_inspection_route,
               "finding_count": len(review.get("findings", [])), "human_approval": False,
               "scope": checked.get("scope", ""), "analysis_run": handoff["analysis_run"]}
    workflow.write_json(run_dir / "review_check.json", summary)
    workflow.write_json(run_dir / "status.json", {"stage": "review", "model_called": True,
                        "status": "review_checked" if not errors else "needs_correction", "verdict": review.get("verdict")})
    if not errors:
        (run_dir / "REVIEW.md").write_text(render_review(review), encoding="utf-8")
    return summary


def render_review(review):
    lines = ["# Independent model review", "", "Recommendation: **" + review["verdict"] + "**. This is not human approval or proof of financial truth.",
             "", review["summary"], "", "## Claim checks", ""]
    for check in review["claim_checks"]:
        lines.extend([f"### {check['claim_id']}: {check['result']}", "", check["rationale"], ""])
        for cite in check["evidence"]:
            lines.extend([f"[{cite['source_id']} PDF p. {cite['page']}](../../data/raw/{cite['source_id']}.pdf#page={cite['page']}): {cite['excerpt']}", ""])
    lines.extend(["## Requested corrections / notes", ""])
    for finding in review["findings"]:
        lines.extend([f"### {finding['id']} · {finding['severity']} · {finding['claim_id'] or 'narrative'}", "",
                      finding["issue"], "", "Requested change: " + finding["requested_change"], ""])
    if not review["findings"]:
        lines.extend(["No correction findings.", ""])
    lines.extend(["## Limitations", ""] + ["- " + s for s in review["limitations"]] + [""])
    return "\n".join(lines)


def run_review(analysis_run, previous_review=None, root=workflow.ROOT):
    output, prompt = prepare_review(analysis_run, previous_review, root)
    executable = workflow.find_codex()
    auth = subprocess.run([executable, "login", "status"], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=workflow.subscription_env(), timeout=30)
    if auth.returncode or "chatgpt" not in (auth.stdout + auth.stderr).casefold():
        workflow.write_json(output / "status.json", {"stage": "review", "status": "awaiting_chatgpt_login", "model_called": False})
        raise ValueError("ChatGPT subscription login required; no paid API fallback is enabled")
    workflow.write_json(output / "status.json", {"stage": "review", "status": "running", "model_called": True})
    try:
        with (output / "events.jsonl").open("w", encoding="utf-8") as events, (output / "stderr.txt").open("w", encoding="utf-8") as stderr:
            process = subprocess.run(workflow.research_command(executable, root, output, stage="review"),
                                     input=prompt, text=True, encoding="utf-8", cwd=root, env=workflow.subscription_env(),
                                     stdout=events, stderr=stderr, timeout=900)
        if not process.returncode:
            canonicalize_tool_output(output, "review", "submit_review_pack", root)
        if process.returncode or not audit_review(output, root)["passed"]:
            raise ValueError(f"Review agent or validation failed; inspect {output}")
        return output
    except Exception as error:
        workflow.write_json(output / "status.json", {"stage": "review", "status": "failed", "model_called": True, "error": str(error)})
        raise


def revise_analysis(analysis_run, review_run, root=workflow.ROOT):
    analysis_run, review_run = resolve_run(analysis_run, root), resolve_run(review_run, root)
    if not audit_review(review_run, root)["passed"]:
        raise ValueError("Cannot correct from an invalid review")
    if resolve_run(workflow.read_json(review_run / "handoff.json")["analysis_run"], root) != analysis_run:
        raise ValueError("Correction review belongs to a different analysis")
    prior_handoff = workflow.read_json(analysis_run / "handoff.json")
    output, prompt = prepare_analysis(prior_handoff["research_run"], root)
    # Retain the numerical benchmark actually reviewed, not a subsequently changed mapping.
    ledger = FinancialLedger(root, workflow.read_json(analysis_run / "inputs/financial_tables.json"))
    workflow.write_json(output / "inputs/financial_tables.json", ledger.config)
    workflow.write_json(output / "metric_ledger.json", ledger.metrics)
    workflow.write_json(output / "inputs/revision_request.json", {
        "previous_analysis": workflow.read_json(analysis_run / "analysis.json"),
        "independent_review": workflow.read_json(review_run / "review.json"),
        "instruction": "This is the one permitted correction pass. Address the findings; keep unresolved evidence gaps explicit."})
    handoff = workflow.read_json(output / "handoff.json")
    handoff["financial_tables_snapshot_sha256"] = workflow.sha256(output / "inputs/financial_tables.json")
    handoff["revision_inputs"] = [{"path": str(path.relative_to(root)), "sha256": workflow.sha256(path)}
                                  for path in (output / "inputs/revision_request.json", analysis_run / "analysis.json", review_run / "review.json")]
    handoff["previous_analysis_run"] = str(analysis_run.relative_to(root))
    handoff["independent_review_run"] = str(review_run.relative_to(root))
    workflow.write_json(output / "handoff.json", handoff)
    return execute_analysis(output, prompt, root)


def review_cycle(analysis_run, preview=False, root=workflow.ROOT):
    analysis_run = resolve_run(analysis_run, root)
    if preview:
        output, _ = prepare_review(analysis_run, root=root)
        print(f"Review prepared; no model called: {output}")
        return output
    cycle = new_run("review_cycle_", root)
    state = {"stage": "review_cycle", "status": "running", "original_analysis": str(analysis_run.relative_to(root)),
             "reviews": [], "revisions": [], "max_corrections": 1, "human_approval": False}
    workflow.write_json(cycle / "status.json", state)
    previous = None
    try:
        for attempt in range(2):
            review_run = run_review(analysis_run, previous_review=previous, root=root)
            state["reviews"].append(str(review_run.relative_to(root)))
            workflow.write_json(cycle / "status.json", state)
            review = workflow.read_json(review_run / "review.json")
            if review["verdict"] == "pass":
                state["status"] = "awaiting_human_review"
                break
            if attempt == 1:
                state["status"] = "needs_human_resolution"
                break
            analysis_run = revise_analysis(analysis_run, review_run, root)
            state["revisions"].append(str(analysis_run.relative_to(root)))
            workflow.write_json(cycle / "status.json", state)
            previous = review_run
        state["latest_analysis"] = str(analysis_run.relative_to(root))
        state["analysis_sha256"] = workflow.sha256(analysis_run / "analysis.json")
        state["latest_review_sha256"] = workflow.sha256(review_run / "review.json")
        workflow.write_json(cycle / "status.json", state)
        heading = "Model-reviewed draft — awaiting human review" if state["status"] == "awaiting_human_review" else "Unresolved draft — needs a human decision"
        text = (analysis_run / "BRIEFING.md").read_text(encoding="utf-8")
        text = merge_review_source_lines(text, review)
        text = text.replace("Draft for independent review. Historical source checks and arithmetic checks have passed; the financial interpretation is not approved.",
                            "Independent model review is recorded separately. Source and arithmetic checks passed; a human has not approved the financial interpretation.", 1)
        (cycle / "BRIEFING.md").write_text("# " + heading + "\n\nNo human approval has been recorded. See the linked review and remaining limitations.\n\n" + text, encoding="utf-8")
        state["reviewed_source_lines_merged"] = True
        state["briefing_sha256"] = workflow.sha256(cycle / "BRIEFING.md")
        workflow.write_json(cycle / "status.json", state)
        (cycle / "SUMMARY.md").write_text("# Review cycle\n\n" + state["status"] + "\n\n"
            + "Latest analysis: [draft](../" + analysis_run.name + "/BRIEFING.md).\n\n"
            + "Latest review: [findings](../" + review_run.name + "/REVIEW.md).\n\n"
            + f"Correction passes: {len(state['revisions'])}/1. Human approval: not recorded.\n", encoding="utf-8")
        print(f"Review cycle stopped at {state['status']}; corrections={len(state['revisions'])}/1; run={cycle}")
        return cycle
    except Exception as error:
        state.update(status="failed", error=str(error))
        workflow.write_json(cycle / "status.json", state)
        raise
