"""Create a traceable presentation copy of an unapproved model-reviewed draft."""
import argparse
from pathlib import Path

from analysis_stage import audit_analysis, resolve_run
from review_stage import audit_review, merge_review_source_lines, new_run
import workflow


def prepare(cycle_name, root):
    cycle = resolve_run(cycle_name, root)
    state = workflow.read_json(cycle / "status.json")
    if state["status"] not in ("awaiting_human_review", "needs_human_resolution") or state["human_approval"] is not False:
        raise ValueError("Only unapproved review cycles can be prepared for delivery")
    analysis_run = resolve_run(state["latest_analysis"], root)
    review_run = resolve_run(state["reviews"][-1], root)
    if workflow.sha256(analysis_run / "analysis.json") != state["analysis_sha256"]:
        raise ValueError("Analysis changed since the review cycle")
    if workflow.sha256(review_run / "review.json") != state["latest_review_sha256"]:
        raise ValueError("Review changed since the review cycle")
    if not audit_analysis(analysis_run, root)["passed"] or not audit_review(review_run, root)["passed"]:
        raise ValueError("Analysis or review audit failed")
    review = workflow.read_json(review_run / "review.json")
    briefing = (cycle / "BRIEFING.md").read_text(encoding="utf-8")
    corrected = merge_review_source_lines(briefing, review)
    if corrected == briefing:
        raise ValueError("No reviewed source references needed adding")
    delivery = new_run("delivery_", root)
    note = ("Presentation update: source lines now include pages checked in the saved model review. "
            "The claims, calculations, model outputs, and approval status are unchanged. "
            "[Review record](REVIEW.md).\n\n")
    (delivery / "BRIEFING.md").write_text(corrected.replace("\n\n", "\n\n" + note, 1), encoding="utf-8")
    (delivery / "REVIEW.md").write_text((review_run / "REVIEW.md").read_text(encoding="utf-8"), encoding="utf-8")
    workflow.write_json(delivery / "status.json", {
        "stage": "presentation_delivery", "status": state["status"], "human_approval": False,
        "source_cycle": str(cycle.relative_to(root)),
        "source_briefing_sha256": workflow.sha256(cycle / "BRIEFING.md"),
        "analysis_sha256": state["analysis_sha256"], "review_sha256": state["latest_review_sha256"],
        "briefing_sha256": workflow.sha256(delivery / "BRIEFING.md"),
        "review_record_sha256": workflow.sha256(delivery / "REVIEW.md"),
        "presentation_change": "Added reviewed page references to displayed source lines only."
    })
    return delivery


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", help="Configured case ID")
    parser.add_argument("--cycle", required=True, help="Review-cycle path relative to the case root")
    args = parser.parse_args()
    print(prepare(args.cycle, workflow.case_root(args.case)))


if __name__ == "__main__":
    main()
