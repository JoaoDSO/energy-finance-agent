"""One user-requested revision plus fresh model review; never final approval."""
from pathlib import Path

from analysis_stage import audit_analysis, execute_analysis, prepare_analysis, resolve_run
from analysis_tools import render_brief
from finance import FinancialLedger
from review_stage import audit_review, new_run, run_review
import workflow


def refresh_reviewed_package(package, root=workflow.ROOT):
    """Attach checked review sources and current lifecycle labels; no financial rewrite."""
    package = resolve_run(package, root)
    state = workflow.read_json(package / "status.json")
    analysis_run, review_run = resolve_run(state["analysis_run"], root), resolve_run(state["review_run"], root)
    if state["human_approval"] is not False or state["status"] not in ("awaiting_human_review", "needs_human_resolution"):
        raise ValueError("Only unapproved reviewed drafts can be packaged")
    if workflow.sha256(analysis_run / "analysis.json") != state["analysis_sha256"] or workflow.sha256(review_run / "review.json") != state["review_sha256"]:
        raise ValueError("Cannot package a changed draft or review")
    if not audit_analysis(analysis_run, root)["passed"] or not audit_review(review_run, root)["passed"]:
        raise ValueError("Draft or review checks failed")
    analysis = workflow.read_json(analysis_run / "analysis.json")
    review = workflow.read_json(review_run / "review.json")
    request = workflow.read_json(analysis_run / "inputs/human_feedback.json")
    ledger = FinancialLedger(root, workflow.read_json(analysis_run / "inputs/financial_tables.json"))
    additions = {c["claim_id"]: {(e["source_id"], e["page"]) for e in c["evidence"]} for c in review["claim_checks"]}
    text = render_brief(analysis, ledger, workflow.read_json(analysis_run / "calculations.json"),
                        workflow.read_json(analysis_run / "inputs/research.json"),
                        workflow.read_json(analysis_run / "handoff.json")["research_run"], source_additions=additions)
    text = text.replace("Draft for independent review. Historical source checks and arithmetic checks have passed; the financial interpretation is not approved.",
                        "Arithmetic and source checks passed. The fresh model review is attached; no final human approval is recorded.", 1)
    for limitation in analysis["limitations"]:
        if "No readable independent review linked to this exact replacement exists yet" in limitation:
            text = text.replace("- " + limitation, "- Human comments supersede the original pass. This package includes the fresh review linked to this exact structured draft; model review is not financial truth or human approval.")
    for claim in analysis["claims"]:
        heading = request.get("display_headings", {}).get(claim["id"])
        if heading is None and claim["id"] == "6" and any(change.get("id") == "H2" for change in request.get("requested_changes", [])):
            heading = "Net debt and credit metric"
        if heading:
            text = text.replace(f"### {claim['id']} · {claim['kind']}", f"### {claim['id']} · {heading}", 1)
    header = ("# Revised draft for human review\n\nStatus: **" + state["status"] + "**. No final human approval.\n\n"
              + "[Attached review record](REVIEW.md) | [Human decision / conditions](HUMAN_DECISION.json)\n\n"
              + "Source lines include the validated reviewer's original-page evidence. Evidence combines the original Claude pack with explicitly builder-checked follow-up findings.\n\n")
    (package / "BRIEFING.md").write_text(header + text, encoding="utf-8")
    state["briefing_sha256"] = workflow.sha256(package / "BRIEFING.md")
    state["presentation_note"] = "Source lines merged with validated review evidence; requested heading and lifecycle labels updated. Agent JSON and financial statements unchanged."
    workflow.write_json(package / "status.json", state)
    record = ("# Review record for the revised draft\n\nStructured analysis SHA-256: `" + state["analysis_sha256"]
              + "`.\n\nDelivered briefing SHA-256: `" + state["briefing_sha256"] + "`.\n\nReview SHA-256: `" + state["review_sha256"]
              + "`.\n\nPresentation-only delivery changes: " + state["presentation_note"] + "\n\n")
    (package / "REVIEW.md").write_text(record + (review_run / "REVIEW.md").read_text(encoding="utf-8"), encoding="utf-8")
    return state


def prepare_human_revision(analysis_run, review_run, request_file, root=workflow.ROOT):
    analysis_run, review_run = resolve_run(analysis_run, root), resolve_run(review_run, root)
    if not audit_analysis(analysis_run, root)["passed"] or not audit_review(review_run, root)["passed"]:
        raise ValueError("Original analysis/review failed checks")
    if resolve_run(workflow.read_json(review_run / "handoff.json")["analysis_run"], root) != analysis_run:
        raise ValueError("Review belongs to a different draft")
    path = Path(request_file)
    path = (root / path).resolve() if not path.is_absolute() else path.resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Revision request must be a project-local file")
    request = workflow.read_json(path)
    if request.get("decision") != "changes_requested" or request.get("human_final_approval") is not False or not request.get("requested_changes"):
        raise ValueError("This command accepts changes requested, not approval")
    handoff_before = workflow.read_json(analysis_run / "handoff.json")
    original = workflow.read_json(analysis_run / "inputs/research.json")
    supplement = request.get("additional_evidence", [])
    enriched = {**original, "findings": original["findings"] + supplement,
                "gaps": request.get("remaining_gaps", original["gaps"])}
    if not workflow.check_evidence(enriched, root)["passed"]:
        raise ValueError("Supplementary source excerpts failed checks")
    output, prompt = prepare_analysis(handoff_before["research_run"], root)
    ledger = FinancialLedger(root, workflow.read_json(analysis_run / "inputs/financial_tables.json"))
    workflow.write_json(output / "inputs/financial_tables.json", ledger.config)
    workflow.write_json(output / "metric_ledger.json", ledger.metrics)
    workflow.write_json(output / "inputs/research.json", enriched)
    workflow.write_json(output / "inputs/human_feedback.json", request)
    workflow.write_json(output / "inputs/revision_request.json", {
        "previous_analysis": workflow.read_json(analysis_run / "analysis.json"),
        "independent_review": workflow.read_json(review_run / "review.json"), "human_feedback": request,
        "instruction": "Human changes supersede the previous model pass. Make this one correction, read the cited follow-up pages, retain open evidence gaps and do not claim final approval."})
    handoff = workflow.read_json(output / "handoff.json")
    handoff.update(research_snapshot_sha256=workflow.sha256(output / "inputs/research.json"),
                   financial_tables_snapshot_sha256=workflow.sha256(output / "inputs/financial_tables.json"),
                   previous_analysis_run=str(analysis_run.relative_to(root)), independent_review_run=str(review_run.relative_to(root)),
                   supplemental_evidence_origin=request.get("additional_evidence_origin"), human_approval=False)
    handoff["revision_inputs"] = [{"path": str(p.relative_to(root)), "sha256": workflow.sha256(p)} for p in (
        output / "inputs/human_feedback.json", output / "inputs/revision_request.json", analysis_run / "analysis.json", review_run / "review.json")]
    workflow.write_json(output / "handoff.json", handoff)
    return output, prompt


def human_revision(analysis_run, review_run, request_file, preview=False, root=workflow.ROOT):
    output, prompt = prepare_human_revision(analysis_run, review_run, request_file, root)
    if preview:
        print(f"Human revision prepared; no model called: {output}")
        return output
    package = new_run("human_revision_", root)
    state = {"status": "changes_requested", "human_approval": False, "max_corrections": 1,
             "original_analysis": str(resolve_run(analysis_run, root).relative_to(root)), "analysis_run": str(output.relative_to(root))}
    workflow.write_json(package / "status.json", state)
    try:
        execute_analysis(output, prompt, root)
        # Exactly one edit: fresh review cannot trigger a further automatic correction.
        reviewed = run_review(output, previous_review=review_run, root=root)
        verdict = workflow.read_json(reviewed / "review.json")["verdict"]
        state.update(status="awaiting_human_review" if verdict == "pass" else "needs_human_resolution",
                     review_run=str(reviewed.relative_to(root)), corrections=1,
                     analysis_sha256=workflow.sha256(output / "analysis.json"), review_sha256=workflow.sha256(reviewed / "review.json"))
        workflow.write_json(package / "status.json", state)
        request = workflow.read_json(output / "inputs/human_feedback.json")
        workflow.write_json(package / "HUMAN_DECISION.json", {"decision": "changes_requested", "human_final_approval": False,
                            "applies_to_original_draft": state["original_analysis"], "revised_draft": state["analysis_run"],
                            "conditions_still_for_user_decision": request.get("remaining_gaps", []),
                            "note": "Model review is not final human approval. Revised version requires an explicit new user decision."})
        header = ("# Revised draft for human review\n\nStatus: **" + state["status"] + "**. No final human approval.\n\n"
                  + "[Attached review record](REVIEW.md) | [Human decision / conditions](HUMAN_DECISION.json)\n\n")
        text = (output / "BRIEFING.md").read_text(encoding="utf-8").replace(
            "Draft for independent review. Historical source checks and arithmetic checks have passed; the financial interpretation is not approved.",
            "Arithmetic and source checks passed. See the attached fresh model review; this is not human approval.", 1)
        (package / "BRIEFING.md").write_text(header + text, encoding="utf-8")
        review_header = ("# Review record for this revised draft\n\nAnalysis SHA-256: `" + state["analysis_sha256"]
                         + "`.\n\nReview SHA-256: `" + state["review_sha256"] + "`.\n\n")
        (package / "REVIEW.md").write_text(review_header + (reviewed / "REVIEW.md").read_text(encoding="utf-8"), encoding="utf-8")
        refresh_reviewed_package(package, root)
        print(f"Human-requested revision and fresh review completed: {state['status']}; package={package}")
        return package
    except Exception as error:
        state.update(status="failed", error=str(error))
        workflow.write_json(package / "status.json", state)
        raise
