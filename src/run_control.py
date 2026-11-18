"""On-demand orchestration, fingerprinted reuse and a version-specific review queue."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

from analysis_stage import analyze, audit_analysis, resolve_run, select_research
from finance import FinancialLedger
from liquidity_check import source_checked_bridge
from review_stage import audit_review, review_cycle
import workflow


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def inputs(root=workflow.ROOT):
    """Fail on altered cached PDFs; this is not remote new-report discovery."""
    manifest = workflow.read_json(root / "data/manifest.json")["sources"]
    indexed = workflow.pages(root)
    fingerprints = {}
    source_config = workflow.read_json(root / "config/sources.json")
    for source in source_config["sources"]:
        workflow.validate_source(source, source_config.get("allowed_hosts", ["cdn.orsted.com"]))
        entry = manifest[source["id"]]
        path = root / "data/raw" / source["filename"]
        if entry["url"] != source["url"] or workflow.sha256(path) != entry["sha256"]:
            raise ValueError("Source changed or is unverified; preserve it and review the new source mapping")
        source_pages = [page for page in indexed if page["source_id"] == source["id"]]
        if not source_pages or any(page["sha256"] != entry["sha256"] for page in source_pages):
            raise ValueError("Index is missing or belongs to another source version")
        fingerprints[str(path.relative_to(root))] = entry["sha256"]
    for folder, pattern in (("config", "*.json"), ("agents", "*.md")):
        for path in sorted((root / folder).glob(pattern)):
            fingerprints[str(path.relative_to(root))] = workflow.sha256(path)
    for path in sorted((workflow.ROOT / "src").glob("*.py")):
        fingerprints["code/src/" + path.name] = workflow.sha256(path)
    for name in ("research.schema.json", "analysis.schema.json", "review.schema.json"):
        schema = root / "config" / name
        if not schema.is_file():
            schema = workflow.ROOT / "config" / name
        fingerprints["schema/" + name] = workflow.sha256(schema)
    fingerprints["data/pages.jsonl"] = workflow.sha256(root / "data/pages.jsonl")
    FinancialLedger(root)  # Recheck the curated numeric mapping before any reuse.
    return fingerprints


def package_check(package, root=workflow.ROOT):
    package = resolve_run(package, root)
    state = workflow.read_json(package / "status.json")
    if state.get("status") != "conditionally_accepted_historical_scope_supplements_pending_confirmation":
        raise ValueError("Only the checked conditional package can be adopted through this path")
    hashes = state.get("artifacts", {})
    if not hashes or any(workflow.sha256(package / name) != value for name, value in hashes.items()
                         if (package / name).resolve().is_relative_to(package)):
        raise ValueError("Package artifact fingerprint changed or is missing")
    if any(not (package / name).resolve().is_relative_to(package) for name in hashes):
        raise ValueError("Package contains an external artifact path")
    decision = workflow.read_json(package / "HUMAN_DECISION.json")
    if workflow.sha256(package / "HISTORICAL_BASELINE.md") != decision["corrected_baseline_sha256"]:
        raise ValueError("Conditional decision does not match the historical baseline")
    base = resolve_run(decision["user_baseline_package"], root)
    previous = workflow.read_json(base / "status.json")
    if workflow.sha256(base / "BRIEFING.md") != decision["user_baseline_sha256"]:
        raise ValueError("Original user-reviewed briefing changed")
    if not audit_analysis(previous["analysis_run"], root)["passed"] or not audit_review(previous["review_run"], root)["passed"]:
        raise ValueError("Preserved analysis or review failed audit")
    visual = workflow.read_json(package / "VISUAL_CHECK.json")
    for key, sha_key in (("source_pdf", "source_sha256"), ("render", "render_sha256")):
        path = (root / visual[key]).resolve()
        if not path.is_relative_to(root.resolve()) or workflow.sha256(path) != visual[sha_key]:
            raise ValueError("Visual-check source or render changed")
    if source_checked_bridge(root) != workflow.read_json(package / "C06_CALCULATION.json"):
        raise ValueError("Optional cash/securities calculation no longer reproduces")
    call = workflow.read_json(package / "TRANSCRIPT_CHECK.json")
    text = next(page for page in workflow.pages(root) if page["source_id"] == call["source_id"] and page["page"] == call["page"])
    if text["sha256"] != call["source_sha256"] or any(fragment not in workflow.normalise(text["text"]) for fragment in call["verified_fragments"]):
        raise ValueError("Transcript evidence changed")
    tracked = {str(path.relative_to(root)): workflow.sha256(path) for path in sorted(package.iterdir()) if path.is_file()}
    return {"type": "conditional_package", "path": str(package.relative_to(root)), "files": tracked,
            "historical_file": str((package / "HISTORICAL_BASELINE.md").relative_to(root)),
            "expanded_file": str((package / "BRIEFING.md").relative_to(root)),
            "historical_sha256": decision["corrected_baseline_sha256"], "conditions": decision["conditions"],
            "existing_acceptance": {"decision": decision["decision"], "scope": decision["accepted_scope"],
                                    "record": str((package / "HUMAN_DECISION.json").relative_to(root))},
            "expanded_review_complete": False, "human_final_approval": False,
            "scope_note": "Historical baseline has scoped conditional acceptance. C06 and transcript supplement are source-checked, not included in the previous agent review or silently accepted."}


def check_files(files, root):
    for name, fingerprint in files.items():
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()) or workflow.sha256(path) != fingerprint:
            raise ValueError("A versioned report artifact changed: " + name)


def save_state(job, state, root):
    workflow.write_json(job / "status.json", state)
    workflow.write_json(root / "runs/LATEST.json", {"job": str(job.relative_to(root))})
    # This queue is a navigation view, not the source of approval decisions.
    lines = ["# Review queue", "", "On demand only. No background schedule, publication or funding-sufficiency approval.", ""]
    for path in sorted((root / "runs").glob("workflow_*/status.json"), reverse=True):
        entry = workflow.read_json(path)
        stale = " (older input version; recheck required)" if entry["input_version"] != state["input_version"] else ""
        lines.append(f"- [{path.parent.name}]({path.parent.name}/SUMMARY.md) - {entry['status']}{stale}")
    (root / "runs/REVIEW_QUEUE.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def summary(job, state, root):
    report = state.get("report")
    lines = ["# " + state["label"], "", "Status: **" + state["status"] + "**", "",
             "Input version: `" + state["input_version"] + "`.", "",
             "Model work started by this job: " + str(state["model_stages_started"]) + ".",
             "This count describes stages launched, not model invocations internal to a correction cycle.", ""]
    if report:
        for label, key in (("Historical baseline", "historical_file"), ("Expanded briefing", "expanded_file")):
            relative = os.path.relpath(root / report[key], job).replace("\\", "/")
            lines.extend([f"[{label}]({relative})", ""])
        lines.extend([report["scope_note"], "", "Historical artifact SHA-256: `" + report["historical_sha256"] + "`.", "",
                      "Publication and funding sufficiency: **not authorized**.", "", "## Remaining conditions", ""])
        lines.extend("- " + (item.get("topic", item.get("text", "Unresolved condition")) if isinstance(item, dict) else item) for item in report["conditions"])
        if report.get("existing_acceptance"):
            acceptance = report["existing_acceptance"]
            link = os.path.relpath(root / acceptance["record"], job).replace("\\", "/")
            lines.extend(["", "## Pre-existing scoped decision", "", f"[{acceptance['decision']}]({link})", "", acceptance["scope"], "",
                          "This is a link to the preserved user decision, not a new acceptance created by the runner.", ""])
    if state.get("claude_prompt"):
        lines.extend(["## Your Claude step", "", state["claude_prompt"], "", "Resume using --research-run with the saved session ID. No report text needs copying.", ""])
    if state.get("error"):
        lines.extend(["## Stop reason", "", state["error"], ""])
    lines.extend(["## Human decision gate", "", "No new human decision is created by running or resuming. Decisions require the exact artifact SHA-256 and remain limited to historical review. Do not treat the pre-existing conditional decision as approval of the expanded briefing.", ""])
    (job / "SUMMARY.md").write_text("\n".join(lines), encoding="utf-8")


def coverage(research_run, root):
    _, _, pack = select_research(research_run, root)
    expected = {source["id"] for source in workflow.read_json(root / "config/sources.json")["sources"]}
    if expected - {finding["source_id"] for finding in pack["findings"]}:
        raise ValueError("Research does not cover every configured source; obtain a complete fresh research pack")


def run(package=None, fresh=False, research_client=None, research_run=None, resume=None, root=workflow.ROOT):
    config = workflow.read_json(root / "config/workflow.json")
    if research_client not in (None, "codex", "claude_desktop"):
        raise ValueError("Unknown research client")
    try:
        fingerprints = inputs(root)
    except Exception as error:
        job = root / "runs" / ("workflow_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
        job.mkdir(parents=True)
        state = {"status": "failed", "mode": "input_validation", "label": config["display_label"],
                 "inputs": {}, "input_version": "unverified", "model_stages_started": [], "error": str(error),
                 "human_final_approval": False, "publication_authorized": False, "funding_sufficiency_approved": False}
        save_state(job, state, root)
        summary(job, state, root)
        raise
    mode = "fresh" if fresh else "reuse"
    if resume is not None:
        job = resolve_run(resume, root)
        state = workflow.read_json(job / "status.json")
        if state["inputs"] != fingerprints:
            raise ValueError("Inputs changed since this job; start a new version rather than resuming")
        mode = state["mode"]
        if state["status"] == "failed" and state.get("review_started"):
            raise ValueError("Review failed after starting; automatic resume cannot reset the correction allowance. Inspect its saved cycle and start an explicit new review if needed.")
    else:
        job = root / "runs" / ("workflow_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
        job.mkdir(parents=True)
        state = {"mode": mode, "status": "running", "label": config["display_label"], "created_at": workflow.utc_now(),
                 "inputs": fingerprints, "input_version": digest(fingerprints), "model_stages_started": [],
                 "research_client": research_client or config["research_client"], "human_final_approval": False,
                 "publication_authorized": False, "funding_sufficiency_approved": False}
    save_state(job, state, root)
    try:
        if mode == "reuse":
            if not package and not config.get("current_package"):
                raise ValueError("This case has no reviewed package to reuse; start an explicit fresh run")
            report = package_check(package or config["current_package"], root)
            report_version = digest(report["files"])
            state.update(report=report, report_version=report_version, status="awaiting_scoped_human_review")
            for earlier in sorted((root / "runs").glob("workflow_*/status.json"), reverse=True):
                if earlier.parent == job:
                    continue
                old = workflow.read_json(earlier)
                if old["input_version"] == state["input_version"] and old.get("report_version") == report_version and old.get("report"):
                    check_files(old["report"]["files"], root)
                    state["reused_from"] = str(earlier.parent.relative_to(root))
                    break
        else:
            selected = research_run or state.get("research_run")
            if selected is None and state["research_client"] == "claude_desktop":
                state["status"] = "awaiting_claude_research"
                state["claude_prompt"] = "Use the selected energy-finance case connector's get_research_task. Investigate every configured source; read every page you cite. Save with submit_research_pack and return the run ID. Do not claim a final financial verdict."
                save_state(job, state, root)
                summary(job, state, root)
                print(f"Claude user action required; no model launched: {job}")
                return job
            if selected is None:
                state["model_stages_started"].append("research")
                save_state(job, state, root)
                selected = workflow.research(root=root)
            coverage(selected, root)
            state["research_run"] = str(resolve_run(selected, root).relative_to(root))
            save_state(job, state, root)
            if not state.get("analysis_run"):
                state["model_stages_started"].append("analysis")
                save_state(job, state, root)
                state["analysis_run"] = str(analyze(selected, root=root).relative_to(root))
                save_state(job, state, root)
            elif not audit_analysis(state["analysis_run"], root)["passed"]:
                raise ValueError("Completed analysis failed recheck on resume")
            if not state.get("review_cycle"):
                state["review_started"] = True
                state["model_stages_started"].append("review_cycle")
                save_state(job, state, root)
                state["review_cycle"] = str(review_cycle(state["analysis_run"], root=root).relative_to(root))
            cycle = resolve_run(state["review_cycle"], root)
            cycle_state = workflow.read_json(cycle / "status.json")
            reviewed = resolve_run(cycle_state["reviews"][-1], root)
            if not audit_review(reviewed, root)["passed"]:
                raise ValueError("Review cycle output failed audit")
            latest_analysis = resolve_run(cycle_state["latest_analysis"], root)
            baseline = config.get("current_package")
            baseline_conditions = workflow.read_json(root / baseline / "HUMAN_DECISION.json").get("conditions", []) if baseline else []
            draft_questions = workflow.read_json(latest_analysis / "analysis.json").get("open_questions", [])
            tracked = {str(path.relative_to(root)): workflow.sha256(path) for path in cycle.iterdir() if path.is_file()}
            tracked[str((latest_analysis / "analysis.json").relative_to(root))] = workflow.sha256(latest_analysis / "analysis.json")
            tracked[str((reviewed / "review.json").relative_to(root))] = workflow.sha256(reviewed / "review.json")
            report = {"type": "model_review_cycle", "path": str(cycle.relative_to(root)), "files": tracked,
                      "historical_file": str((cycle / "BRIEFING.md").relative_to(root)), "expanded_file": str((cycle / "BRIEFING.md").relative_to(root)),
                      "historical_sha256": workflow.sha256(cycle / "BRIEFING.md"),
                      "conditions": baseline_conditions + [{"topic": question, "status": "open"} for question in draft_questions],
                      "expanded_review_complete": cycle_state["status"] == "awaiting_human_review",
                      "scope_note": "Fresh model-reviewed draft; no human acceptance or publication permission has been recorded."}
            state.update(report=report, report_version=digest(tracked), status=cycle_state["status"])
        state.pop("error", None)
        save_state(job, state, root)
        summary(job, state, root)
        print(f"Workflow: {state['status']}; reused={state.get('reused_from', 'no')}; model stages={len(state['model_stages_started'])}; {job}")
        return job
    except Exception as error:
        state.update(status="failed", error=str(error))
        save_state(job, state, root)
        summary(job, state, root)
        raise
