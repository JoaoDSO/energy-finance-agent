"""Automatic validated-research handoff into a separate Codex analysis session."""
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess

from analysis_tools import render_brief, validate_analysis
from finance import FinancialLedger
import workflow


def resolve_run(path, root):
    path = Path(path)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if not path.is_relative_to((root / "runs").resolve()):
        raise ValueError("Run must be inside this project's runs directory")
    return path


def select_claude_research(run, root):
    """Verify a Desktop tool submission, not a claimed Claude/model identity."""
    events = [json.loads(line) for line in (run / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    submissions = [(index, event) for index, event in enumerate(events)
                   if event.get("client") == "claude_desktop"
                   and event.get("tool") == "submit_research_pack" and event.get("outcome") == "saved"]
    if not submissions:
        raise ValueError("Claude session has no accepted research submission")
    index, event = submissions[-1]
    saved = resolve_run(Path(event["arguments"]["submission"]) / "research.json", root)
    if saved.parent.parent != run / "submissions":
        raise ValueError("Claude submission points outside its session")
    if workflow.sha256(saved) != event["arguments"].get("research_sha256"):
        raise ValueError("Claude research fingerprint changed or was not recorded")
    result = workflow.read_json(saved)
    checked = workflow.check_evidence(result, root)
    if not checked["passed"]:
        raise ValueError("Claude research failed evidence checks")
    read_pages = {(e["arguments"]["source_id"], e["arguments"]["page"]) for e in events[:index]
                  if e.get("client") == "claude_desktop" and e.get("tool") == "read_report_page" and e.get("outcome") == "ok"}
    if any((finding["source_id"], finding["page"]) not in read_pages for finding in result["findings"]):
        raise ValueError("Claude research cited a page not read before submission")
    return run, saved, result


def select_research(selected=None, root=workflow.ROOT):
    claude_root = root / "runs/claude"
    available = [p for p in (root / "runs").iterdir()
                 if p.is_dir() and (p / "research.json").is_file() and (p / "events.jsonl").is_file()]
    if claude_root.is_dir():
        available.extend(p for p in claude_root.iterdir() if p.is_dir() and (p / "events.jsonl").is_file())
    candidates = [resolve_run(selected, root)] if selected is not None else sorted(
        available,
        key=lambda p: p.name, reverse=True)
    for run in candidates:
        if run.parent == claude_root:
            try:
                return select_claude_research(run, root)
            except (ValueError, KeyError, OSError):
                if selected is not None:
                    raise
                continue
        result = workflow.read_json(run / "research.json")
        checked = workflow.check_evidence(result, root)
        trace = workflow.check_research_trace(run / "events.jsonl", result) if checked["passed"] else {"passed": False}
        if not checked["passed"] or not trace["passed"]:
            if selected is not None:
                raise ValueError("Selected research failed evidence/trace checks; analysis was not started")
            continue
        saved = resolve_run(trace["handoffs"][-1], root)
        if workflow.read_json(saved) != result:
            raise ValueError("Final research response differs from its validated tool handoff")
        return run, saved, result
    raise ValueError("No validated research handoff is available")


def prepare_analysis(selected=None, root=workflow.ROOT):
    research_run, research_file, result = select_research(selected, root)
    ledger = FinancialLedger(root)
    output = root / "runs" / ("analysis_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    output.mkdir(parents=True)
    workflow.write_json(output / "inputs/research.json", result)
    workflow.write_json(output / "inputs/financial_tables.json", ledger.config)
    workflow.write_json(output / "metric_ledger.json", ledger.metrics)
    workflow.write_json(output / "handoff.json", {
        "research_run": str(research_run.relative_to(root)), "research_file": str(research_file.relative_to(root)),
        "research_sha256": workflow.sha256(research_file),
        "research_snapshot_sha256": workflow.sha256(output / "inputs/research.json"),
        "financial_tables_snapshot_sha256": workflow.sha256(output / "inputs/financial_tables.json"),
        "created_at": workflow.utc_now(), "input_evidence_and_trace_passed": True})
    prompt = (root / "agents/analysis.md").read_text(encoding="utf-8")
    (output / "analysis_prompt.md").write_text(prompt, encoding="utf-8")
    workflow.write_json(output / "status.json", {"stage": "analysis", "status": "prepared", "model_called": False})
    return output, prompt


def canonicalize_tool_output(run_dir, artifact, tool_name, root=workflow.ROOT):
    """Use a validated MCP submission, preserving any differing final reply."""
    run_dir = resolve_run(run_dir, root)
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    accepted = [event["item"]["result"]["structured_content"] for event in events
                if event.get("type") == "item.completed"
                and event.get("item", {}).get("type") == "mcp_tool_call"
                and event["item"].get("server") == "energy_finance"
                and event["item"].get("tool") == tool_name
                and event["item"].get("status") == "completed"
                and not event["item"].get("error")
                and event["item"].get("result", {}).get("structured_content", {}).get("saved") is True]
    if not accepted:
        raise ValueError(f"No successful {artifact} tool submission to hand off")
    item = accepted[-1]
    saved = resolve_run(item[f"{artifact}_path"], root)
    if saved.name != f"{artifact}.json" or saved.parent.parent != run_dir / "submissions":
        raise ValueError(f"{artifact.capitalize()} submission points outside this run")
    expected = item.get(f"{artifact}_sha256")
    if expected and workflow.sha256(saved) != expected:
        raise ValueError(f"{artifact.capitalize()} submission fingerprint changed after tool acceptance")
    if not workflow.read_json(saved.parent / "validation.json").get("passed"):
        raise ValueError(f"{artifact.capitalize()} tool submission was not validated")
    final = run_dir / f"{artifact}.json"
    mismatch = final.is_file() and workflow.read_json(final) != workflow.read_json(saved)
    if mismatch:
        shutil.copy2(final, run_dir / "agent_final_response.json")
    shutil.copy2(saved, final)
    record = {"authoritative_submission": str(saved.relative_to(root)),
              f"{artifact}_sha256": workflow.sha256(saved),
              "final_response_mismatch": mismatch,
              "final_response_file": "agent_final_response.json" if mismatch else None,
              "rule": "Validated MCP submission is authoritative; differing final reply is retained for inspection."}
    workflow.write_json(run_dir / "output_handoff.json", record)
    return record


def canonicalize_analysis_output(run_dir, root=workflow.ROOT):
    return canonicalize_tool_output(run_dir, "analysis", "submit_analysis_pack", root)


def audit_analysis(run_dir, root=workflow.ROOT):
    run_dir = resolve_run(run_dir, root)
    handoff = workflow.read_json(run_dir / "handoff.json")
    research = workflow.read_json(run_dir / "inputs/research.json")
    ledger = FinancialLedger(root, workflow.read_json(run_dir / "inputs/financial_tables.json"))
    errors = []
    for path, expected in (
        (resolve_run(handoff["research_file"], root), handoff["research_sha256"]),
        (run_dir / "inputs/research.json", handoff["research_snapshot_sha256"]),
        (run_dir / "inputs/financial_tables.json", handoff["financial_tables_snapshot_sha256"]),
    ):
        if workflow.sha256(path) != expected:
            errors.append("Handoff/input fingerprint changed")
    for item in handoff.get("revision_inputs", []):
        if workflow.sha256(resolve_run(item["path"], root)) != item["sha256"]:
            errors.append("Revision request fingerprint changed")
    calculations = workflow.read_json(run_dir / "calculations.json")
    for cid, calculation in calculations.items():
        repeated = ledger.calculate(calculation["operation"], calculation["period"], calculation["reference_period"])
        if repeated != calculation:
            errors.append(f"Stored calculation cannot be reproduced: {cid}")
    analysis = workflow.read_json(run_dir / "analysis.json")
    checked = validate_analysis(analysis, ledger, calculations, research, root)
    errors.extend(checked["errors"])
    output_handoff = workflow.read_json(run_dir / "output_handoff.json") if (run_dir / "output_handoff.json").is_file() else None
    if output_handoff:
        saved_output = resolve_run(output_handoff["authoritative_submission"], root)
        if saved_output.name != "analysis.json" or saved_output.parent.parent != run_dir / "submissions":
            errors.append("Analysis handoff points outside this run")
        elif workflow.sha256(saved_output) != output_handoff["analysis_sha256"] or workflow.sha256(run_dir / "analysis.json") != output_handoff["analysis_sha256"]:
            errors.append("Analysis handoff fingerprint changed")
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    calls = [event["item"] for event in events if event.get("type") == "item.completed"
             and event.get("item", {}).get("type") == "mcp_tool_call"
             and event["item"].get("server") == "energy_finance"
             and event["item"].get("status") == "completed" and not event["item"].get("error")]
    names = [item["tool"] for item in calls]
    if "get_analysis_task" not in names or "get_metrics" not in names:
        errors.append("Analysis task/metric tool use was not recorded")
    traced_calculations = set()
    saved = []
    for item in calls:
        content = item.get("result", {}).get("structured_content", {})
        if item["tool"] == "calculate_financial" and content.get("calculation_id"):
            traced_calculations.add(content["calculation_id"])
            stored = calculations.get(content["calculation_id"])
            if stored is None or any(content.get(key) != value for key, value in stored.items()):
                errors.append("Calculation trace differs from saved result")
        if item["tool"] == "submit_analysis_pack" and content.get("saved") is True:
            saved.append(content["analysis_path"])
    if set(calculations) - traced_calculations:
        errors.append("A saved calculation lacks a successful tool-call trace")
    if not saved or workflow.read_json(resolve_run(saved[-1], root)) != analysis:
        errors.append("No matching successful analysis submission was recorded")
    summary = {"passed": not errors, "errors": errors, "successful_mcp_calls": len(calls),
               "calculation_count": len(calculations), "claim_count": len(analysis.get("claims", [])),
               "final_response_mismatch": output_handoff.get("final_response_mismatch", False) if output_handoff else False,
               "research_handoff": handoff, "scope": checked.get("scope", ""),
               "status": "awaiting_independent_review" if not errors else "needs_correction"}
    workflow.write_json(run_dir / "analysis_check.json", summary)
    workflow.write_json(run_dir / "status.json", {"stage": "analysis", "model_called": True, "status": summary["status"]})
    if not errors:
        (run_dir / "BRIEFING.md").write_text(render_brief(analysis, ledger, calculations, research, handoff["research_run"]), encoding="utf-8")
    return summary


def analyze(selected=None, preview=False, root=workflow.ROOT):
    run_dir, prompt = prepare_analysis(selected, root)
    if preview:
        print(f"Validated research selected and analysis prepared; no model called: {run_dir}")
        return run_dir
    return execute_analysis(run_dir, prompt, root)


def execute_analysis(run_dir, prompt, root=workflow.ROOT):
    """Run either a new analysis or its single, separately prepared correction."""
    executable = workflow.find_codex()
    auth = subprocess.run([executable, "login", "status"], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=workflow.subscription_env(), timeout=30)
    if auth.returncode or "chatgpt" not in (auth.stdout + auth.stderr).casefold():
        workflow.write_json(run_dir / "status.json", {"stage": "analysis", "status": "awaiting_chatgpt_login", "model_called": False})
        raise ValueError("ChatGPT subscription login required; no API fallback is enabled")
    workflow.write_json(run_dir / "status.json", {"stage": "analysis", "status": "running", "model_called": True})
    try:
        with (run_dir / "events.jsonl").open("w", encoding="utf-8") as events, (run_dir / "stderr.txt").open("w", encoding="utf-8") as stderr:
            process = subprocess.run(workflow.research_command(executable, root, run_dir, stage="analysis"),
                                     input=prompt, text=True, encoding="utf-8", cwd=root, env=workflow.subscription_env(),
                                     stdout=events, stderr=stderr, timeout=900)
        if process.returncode:
            raise ValueError(f"Analysis agent failed; inspect logs in {run_dir}")
        canonicalize_analysis_output(run_dir, root)
        checked = audit_analysis(run_dir, root)
        if not checked["passed"]:
            raise ValueError("Analysis needs correction; independent review was not started")
        print(f"Analysis completed: {checked['calculation_count']} checked calculations; briefing={run_dir / 'BRIEFING.md'}")
        return run_dir
    except Exception as error:
        state = workflow.read_json(run_dir / "status.json")
        if state["status"] == "running":
            workflow.write_json(run_dir / "status.json", {"stage": "analysis", "status": "failed", "model_called": True, "error": str(error)})
        raise
