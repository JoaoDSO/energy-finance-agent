"""Source-checked optional cash/securities bridge, with visually curated columns."""
from decimal import Decimal
import re

import workflow


def checked_rows(config, layout_text):
    """Check signs and column values against layout-preserving original-page text."""
    result = {}
    for row in config["rows"]:
        pattern = re.compile(r"^" + re.escape(row["label"]) + r"\s+[\d(\-]")
        matching = [workflow.normalise(line) for line in layout_text.splitlines()
                    if pattern.match(workflow.normalise(line))]
        if len(matching) != 1:
            raise ValueError("Financial source row is absent or ambiguous: " + row["label"])
        tokens = re.findall(r"\([\d,]+\)|-?[\d,]+", matching[0][len(row["label"]):])[:len(config["column_dates_left_to_right"])]
        values = [Decimal(token.replace(",", "").replace("(", "-").replace(")", "")) for token in tokens]
        expected = [Decimal(value) for value in row["values"]]
        if values != expected or len(values) != len(config["column_dates_left_to_right"]):
            raise ValueError("Source sign/column value differs for " + row["label"])
        for date, value in zip(config["column_dates_left_to_right"], values):
            result[date + "." + row["key"]] = {"value": str(value), "unit": config["unit"],
                "balance_date": date, "source_id": config["source_id"], "page": config["page"],
                "row_label": row["label"], "source_sha256": config["source_sha256"]}
    return result


def calculate_bridge(metrics, current="2026-09-30", reference="2025-12-31"):
    def get(date, key):
        metric = metrics[date + "." + key]
        if metric["balance_date"] != date or metric["unit"] != "DKKm":
            raise ValueError("Balance date or unit mismatch")
        return Decimal(metric["value"])
    current_cash, current_securities = get(current, "cash"), get(current, "securities")
    prior_cash, prior_securities = get(reference, "cash"), get(reference, "securities")
    now, prior = current_cash + current_securities, prior_cash + prior_securities
    cash_change, securities_change = current_cash - prior_cash, current_securities - prior_securities
    return {"calculation_id": "C06", "operation": "cash_securities_bridge", "unit": "DKKm",
            "current_date": current, "reference_date": reference,
            "formula": "(current cash + current securities) - (reference cash + reference securities)",
            "current_total": f"{now:.2f}", "reference_total": f"{prior:.2f}", "value": f"{now-prior:.2f}",
            "cash_change": f"{cash_change:.2f}", "securities_change": f"{securities_change:.2f}",
            "movement_check_residual": f"{(now-prior)-(cash_change+securities_change):.2f}",
            "metric_ids": [date + "." + key for date in (current, reference) for key in ("cash", "securities")],
            "scope": "Total-vs-component arithmetic cross-check only; the combined total is derived, not independently reported. Not usable liquidity, total liquidity reserve or a cash-flow/funding-sufficiency reconciliation."}


def source_checked_bridge(root=workflow.ROOT):
    from pypdf import PdfReader
    config = workflow.read_json(root / "config/liquidity_table.json")
    source = workflow.read_json(root / "data/manifest.json")["sources"][config["source_id"]]
    path = root / "data/raw" / source["filename"]
    if source["sha256"] != config["source_sha256"] or workflow.sha256(path) != config["source_sha256"]:
        raise ValueError("Liquidity source fingerprint changed")
    text = PdfReader(path).pages[config["page"]-1].extract_text(extraction_mode="layout")
    metrics = checked_rows(config, text)
    return {"provenance": config, "metrics": metrics, "calculation": calculate_bridge(metrics)}
