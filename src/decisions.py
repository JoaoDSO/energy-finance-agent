"""Explicit, append-only decisions bound to a job, artifact hash and limited scope."""
from datetime import datetime, timezone
from uuid import uuid4

from analysis_stage import resolve_run
from run_control import check_files, inputs
import workflow


def selected_job(job=None, root=workflow.ROOT):
    return resolve_run(job or workflow.read_json(root / "runs/LATEST.json")["job"], root)


def current_job(job=None, root=workflow.ROOT):
    path = selected_job(job, root)
    state = workflow.read_json(path / "status.json")
    if state["inputs"] != inputs(root):
        raise ValueError("Inputs changed: decision cannot apply to this old workflow version")
    if state["status"] not in ("awaiting_scoped_human_review", "awaiting_human_review") or not state.get("report"):
        raise ValueError("Failed, incomplete or unresolved workflow cannot be accepted")
    check_files(state["report"]["files"], root)
    return path, state


def record(job, decision, scope, expected_sha, reviewer, note, conditions=None, root=workflow.ROOT):
    if decision not in ("approve_historical", "approve_with_conditions", "request_changes", "reject"):
        raise ValueError("Unknown human decision")
    if scope not in ("historical", "expanded") or not reviewer.strip() or not note.strip():
        raise ValueError("Explicit scope, reviewer and note are required")
    path = selected_job(job, root)
    state = workflow.read_json(path / "status.json")
    # Reject/change requests can be recorded for an existing draft; acceptance is stricter.
    accepting = decision in ("approve_historical", "approve_with_conditions")
    if accepting:
        path, state = current_job(path, root)
    if not state.get("report"):
        raise ValueError("No versioned draft exists for this decision")
    check_files(state["report"]["files"], root)
    report = state["report"]
    artifact = resolve_run(report["historical_file" if scope == "historical" else "expanded_file"], root)
    actual_sha = workflow.sha256(artifact)
    if expected_sha != actual_sha:
        raise ValueError("Expected artifact SHA-256 differs; re-read the exact current version")
    if accepting and scope == "expanded" and not report["expanded_review_complete"]:
        raise ValueError("Expanded supplements lack the required model review; historical acceptance cannot approve them")
    retained = report.get("conditions", [])
    extra = conditions or []
    if not isinstance(extra, list) or any(not isinstance(text, str) or not text.strip() for text in extra):
        raise ValueError("Extra conditions must be a list of nonempty text strings")
    if accepting and decision == "approve_historical" and (retained or extra):
        raise ValueError("Known conditions must remain: use approve_with_conditions")
    if decision == "approve_with_conditions" and not retained and not extra:
        raise ValueError("Conditional acceptance requires explicit conditions")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "_" + uuid4().hex[:8]
    output = path / "decisions" / stamp / "decision.json"
    record = {"at": workflow.utc_now(), "reviewer": reviewer, "identity_note": "Self-reported local decision; not authenticated identity or a digital signature.",
              "decision": decision, "scope": "bounded_historical_financial_review", "artifact_selection": scope,
              "artifact": str(artifact.relative_to(root)), "artifact_sha256": actual_sha,
              "workflow": str(path.relative_to(root)), "input_version": state["input_version"],
              "report_version": state["report_version"], "note": note, "retained_conditions": retained,
              "additional_conditions": extra, "historical_scope_accepted": accepting,
              "human_final_approval": False, "publication_authorized": False, "funding_sufficiency_approved": False}
    workflow.write_json(output, record)
    print("Version-specific decision saved: " + str(output))
    return output


def status(job=None, root=workflow.ROOT):
    path = selected_job(job, root)
    state = workflow.read_json(path / "status.json")
    valid, error = True, None
    try:
        current_job(path, root)
    except (ValueError, KeyError, OSError) as problem:
        valid, error = False, str(problem)
    decisions = []
    for file in sorted(path.glob("decisions/*/decision.json")):
        item = workflow.read_json(file)
        artifact = resolve_run(item["artifact"], root)
        item["currently_applicable"] = (valid and item["input_version"] == state["input_version"]
                                         and item["report_version"] == state.get("report_version")
                                         and artifact.is_file() and workflow.sha256(artifact) == item["artifact_sha256"])
        decisions.append(item)
    return {"workflow": str(path.relative_to(root)), "status": state["status"], "current_inputs_and_report_valid": valid,
            "validation_error": error, "input_version": state["input_version"], "model_stages_started": state["model_stages_started"],
            "reused_from": state.get("reused_from"), "report": state.get("report"), "decisions": decisions,
            "publication_authorized": False, "funding_sufficiency_approved": False}


def readable_status(item):
    lines = ["Workflow: " + item["workflow"], "Status: " + item["status"],
             "Current inputs/report valid: " + str(item["current_inputs_and_report_valid"]),
             "Model stages started in this job: " + str(len(item["model_stages_started"])),
             "Reused from: " + str(item["reused_from"] or "none")]
    report = item.get("report")
    if report:
        lines.extend(["Historical file: " + report["historical_file"], "Historical SHA-256: " + report["historical_sha256"],
                      "Expanded file: " + report["expanded_file"], report["scope_note"]])
    lines.extend(["New decision records on this job: " + str(len(item["decisions"])),
                  "Publication/funding-sufficiency approval: not authorized"])
    if item["validation_error"]:
        lines.append("Stop reason: " + item["validation_error"])
    return "\n".join(lines)
