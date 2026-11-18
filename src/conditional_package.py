"""Versioned conditional acceptance, visual evidence and separately scoped supplements."""
import argparse
from datetime import datetime, timezone
from pathlib import Path

from analysis_stage import resolve_run
from liquidity_check import source_checked_bridge
import workflow


def remove_item10_extra_source(text):
    marker = "### 10 · interpretation"
    if marker not in text:
        raise ValueError("Expected item 10 is missing")
    before, section = text.split(marker, 1)
    item, after = section.split("## Risks to watch", 1)
    lines = item.splitlines()
    source_lines = [i for i, line in enumerate(lines) if line.startswith("Sources: ")]
    if len(source_lines) != 1 or "orsted_fy2025.pdf#page=141" not in lines[source_lines[0]]:
        raise ValueError("Item 10 source line is unexpected")
    lines[source_lines[0]] = "Sources: [orsted_fy2025 PDF p. 141](../../data/raw/orsted_fy2025.pdf#page=141)."
    return before + marker + "\n".join(lines) + "\n\n## Risks to watch" + after


def build(base_package, root=workflow.ROOT):
    base = resolve_run(base_package, root)
    prior = workflow.read_json(base / "status.json")
    if workflow.sha256(base / "BRIEFING.md") != prior["briefing_sha256"]:
        raise ValueError("Prior briefing fingerprint changed")
    bridge = source_checked_bridge(root)
    manifest = workflow.read_json(root / "data/manifest.json")["sources"]
    call = manifest["orsted_q32026_call"]
    if workflow.sha256(root / "data/raw" / call["filename"]) != call["sha256"]:
        raise ValueError("Transcript fingerprint changed")
    call_page = next(p for p in workflow.pages(root) if p["source_id"] == "orsted_q32026_call" and p["page"] == 7)
    text = workflow.normalise(call_page["text"])
    evidence = ["total liquidity reserve of more than DKK 105 billion", "target of more than 30 %", "reduced by approximately DKK 25 billion"]
    if call_page["sha256"] != call["sha256"] or any(phrase not in text for phrase in evidence):
        raise ValueError("Transcript statements could not be verified")
    output = root / "runs" / ("conditional_review_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    output.mkdir(parents=True)
    original = (base / "BRIEFING.md").read_text(encoding="utf-8")
    corrected = remove_item10_extra_source(original[original.index("# Ørsted:"):])
    corrected = corrected.replace("no final human approval is recorded.", "conditional acceptance applies to this bounded historical baseline only; publication and funding sufficiency are not approved.", 1)
    corrected = corrected.replace("model review is not financial truth or human approval.", "model review is not financial truth; the user's conditional acceptance is limited to this historical baseline.")
    (output / "HISTORICAL_BASELINE.md").write_text(corrected, encoding="utf-8")
    workflow.write_json(output / "C06_CALCULATION.json", bridge)
    source_path = root / "data/raw/orsted_q32026.pdf"
    image_path = root / "_qa/source_pages/q32026-p40.png"
    visual = {"performed_by": "Codex builder visual inspection, not the prior review model or human reviewer",
              "checked_at": workflow.utc_now(), "source_pdf": str(source_path.relative_to(root)),
              "source_sha256": workflow.sha256(source_path), "pdf_page": 40,
              "render": str(image_path.relative_to(root)), "render_sha256": workflow.sha256(image_path),
              "physical_columns_left_to_right": ["30 September 2026", "31 December 2025", "30 September 2025"],
              "finding": "Indexed reading order is not physical column order. Current/prior values in items 6 and 9 are correctly mapped.",
              "prior_model_visual_review": False}
    workflow.write_json(output / "VISUAL_CHECK.json", visual)
    visual_md = """# Visual check of note 13, PDF page 40

The builder inspected the rendered original PDF. This is an actual visual check, not an inference from indexed text. The prior reviewer did not view the image; its limitation remains accurate.

Physical columns run **September 2026, December 2025, September 2025**, left to right - the reverse of the current/prior-year order suggested by the scrambled extraction.

| DKKm unless stated | 30 Sep 2026 (left) | 31 Dec 2025 (middle) | 30 Sep 2025 (right) |
| --- | ---: | ---: | ---: |
| Cash | 13,519 | 53,448 | 8,677 |
| Securities | 63,101 | 38,317 | 21,376 |
| Reported net debt | 37,742 | 18,978 | 83,154 |
| Adjusted net debt | 51,109 | 28,731 | 93,559 |
| LTM FFO / adjusted debt | 37.3% | 42.9% | 13.9% |

Items 6 and 9 therefore use the correct dated columns. December cash/securities also agree with FY2025 note 5.4, page 171. The four row inputs used by C06 are rechecked against layout-preserving original-page extraction, with the PDF fingerprint verified.

[Original note 13](../../data/raw/orsted_q32026.pdf#page=40). The source/render hashes and checker identity are in VISUAL_CHECK.json. No claim is made that a human or the previous model reviewer performed this visual check.
"""
    (output / "VISUAL_CHECK.md").write_text(visual_md, encoding="utf-8")
    calc = """## Optional calculated addition C06 - cash and securities movement

This is a new deterministic, source-checked calculation, not part of the previous agent review or implicitly covered by the user's prior conditional acceptance.

All amounts DKKm; note 13, PDF page 40:

- 30 September 2026: 13,519 cash + 63,101 securities = **76,620** (DKK 76.620bn).
- 31 December 2025: 53,448 cash + 38,317 securities = **91,765** (DKK 91.765bn).
- Change: 76,620 - 91,765 = **-15,145** (a DKK 15.145bn decrease).
- Component bridge: cash -39,929 plus securities +24,784 = -15,145. Internal movement check residual: **0 DKKm**.

This reconciles the derived total's movement to its components, not to an independently reported combined subtotal. It is a stock comparison across balance dates, not an annual-versus-nine-month cash-flow comparison. These gross balances are **not unrestricted usable liquidity or the total liquidity reserve**; do not add them to management's reserve figure.

[Source: note 13, p.40](../../data/raw/orsted_q32026.pdf#page=40). Formula, dates, units, input metric IDs, source fingerprint and output are saved in C06_CALCULATION.json. Columns are visually curated, not autonomously identified.
"""
    (output / "C06_CALCULATION.md").write_text(calc, encoding="utf-8")
    call_md = """## New management context - Q3 earnings call

Management reported quarter-end net debt around DKK 38bn, a total liquidity reserve above DKK 105bn, and FFO/adjusted debt around 37% against its target above 30%. It expected the pending 55% Greater Changhua 2 stake sale to reduce net debt by about DKK 25bn at closing, around year-end following final commissioning of Greater Changhua 2b and 4.

These are dated management statements, not confirmation of current unrestricted liquidity, a covenant/rating threshold, transaction closing or cash proceeds already received. Expected net-debt reduction is not interchangeable with cash sale proceeds. Full closing conditions, receipt/use-of-proceeds details and the six forward-looking conditions remain open.

[Official earnings-call transcript, PDF p.7](../../data/raw/orsted_q32026_call.pdf#page=7). This source-checked supplement was added after the prior agent review and needs confirmation for inclusion in an expanded accepted version.
"""
    (output / "EARNINGS_CALL_CONTEXT.md").write_text(call_md, encoding="utf-8")
    workflow.write_json(output / "TRANSCRIPT_CHECK.json", {"source_id": "orsted_q32026_call", "page": 7,
                        "source_sha256": call["sha256"], "url": call["url"], "verified_fragments": evidence,
                        "scope": "Existence of management statements and visual inspection of complete page; not proof outcomes occurred."})
    conditions = [
        {"topic": "Usable liquidity", "status": "open", "evidence": "Management's quarter-end total reserve is disclosed, but a current usable/unrestricted reconciliation, committed undrawn facilities and forward collateral/margin needs are not established."},
        {"topic": "October 2026 facilities", "status": "open", "evidence": "Outcome of the 8bn facilities - repayment, refinancing, extension, expiry or ongoing availability - is not confirmed."},
        {"topic": "Cash timing", "status": "open", "evidence": "Dated remaining construction outflows and partner/farm-down/tax-equity receipt schedule is not established; dated 2025 obligations/milestones are not an updated schedule."},
        {"topic": "Greater Changhua 2", "status": "partially evidenced", "evidence": "Management expects a 55% sale and about 25bn net-debt reduction around year-end after final commissioning. Actual closing, full conditions, cash proceeds and use of proceeds are not confirmed."},
        {"topic": "US exposures", "status": "open", "evidence": "ITC, tariffs, interest rates, delay and cancellation effects remain risks; a quantified cash scenario with timing is not established."},
        {"topic": "Rating/covenant constraints", "status": "partially evidenced", "evidence": "Management target FFO/adjusted debt above 30% is identified; do not treat it as a binding covenant or rating-agency threshold. Other restrictions remain unverified."}]
    decision = {"decision": "approve_with_conditions", "human_conditional_acceptance": True, "human_final_approval": False,
                "accepted_scope": "Bounded historical analysis supplied by the user, subject to the requested visual and item-10 source-line fixes; not funding sufficiency or publication.",
                "user_baseline_package": str(base.relative_to(root)), "user_baseline_sha256": prior["briefing_sha256"],
                "corrected_baseline_sha256": workflow.sha256(output / "HISTORICAL_BASELINE.md"),
                "requested_pre_signoff_checks": {"page40_visual": "completed_by_builder", "item10_source_line": "p141_only"},
                "new_supplements": "C06 and earnings-call context are source-checked additions, not yet model-reviewed or accepted on the user's behalf. Explicit confirmation is required for the expanded version.",
                "publication_authorized": False, "funding_sufficiency_approved": False, "conditions": conditions}
    workflow.write_json(output / "HUMAN_DECISION.json", decision)
    condition_md = "# Conditions retained\n\nConditional acceptance applies to bounded historical review, not publication or funding sufficiency.\n\n"
    for condition in conditions:
        condition_md += "## " + condition["topic"] + " - " + condition["status"] + "\n\n" + condition["evidence"] + "\n\n"
    (output / "CONDITIONS.md").write_text(condition_md, encoding="utf-8")
    header = ("# Conditionally accepted historical review - scoped record\n\n"
              "Your conditional acceptance is recorded for the historical baseline after the two requested checks. **Publication and any funding-sufficiency conclusion remain unapproved.**\n\n"
              "[Visual check](VISUAL_CHECK.md) | [Prior agent review](REVIEW.md) | [Decision](HUMAN_DECISION.json) | [Six conditions](CONDITIONS.md)\n\n"
              "The optional calculation and earnings-call context below are new, separately labelled supplements pending your confirmation; conditional acceptance is not silently extended to new material.\n\n")
    (output / "BRIEFING.md").write_text(header + corrected + "\n\n# Separately scoped post-review supplements\n\n" + calc + "\n\n" + call_md, encoding="utf-8")
    (output / "REVIEW.md").write_text("# Scope of attached review\n\nThe preserved agent review below assessed the prior ten-claim structured draft. It did not perform the PDF visual check and does not cover new C06 or earnings-call context. Builder visual/source checks are attached separately. Conditional human acceptance is limited as recorded in HUMAN_DECISION.json.\n\n"
        + (base / "REVIEW.md").read_text(encoding="utf-8"), encoding="utf-8")
    tracked = ["BRIEFING.md", "HISTORICAL_BASELINE.md", "REVIEW.md", "VISUAL_CHECK.json", "C06_CALCULATION.json", "TRANSCRIPT_CHECK.json", "HUMAN_DECISION.json"]
    workflow.write_json(output / "status.json", {"status": "conditionally_accepted_historical_scope_supplements_pending_confirmation",
                        "human_conditional_acceptance": True, "human_final_approval": False, "publication_authorized": False,
                        "funding_sufficiency_approved": False, "artifacts": {name: workflow.sha256(output / name) for name in tracked},
                        "original_analysis_sha256": prior["analysis_sha256"], "original_review_sha256": prior["review_sha256"]})
    print("Conditional package: " + str(output))
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-package", type=Path, required=True)
    args = parser.parse_args()
    build(args.base_package)
