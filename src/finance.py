"""Source-checked benchmark ledger and deterministic financial arithmetic."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re

import workflow


def amount(token):
    token = str(token).replace(",", "").strip()
    if token == "-":
        return Decimal(0)
    if token.startswith("(") and token.endswith(")"):
        token = "-" + token[1:-1]
    value = Decimal(token)
    if not value.is_finite():
        raise ValueError("Financial inputs must be finite")
    return value


def formatted(value):
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class FinancialLedger:
    def __init__(self, root=workflow.ROOT, config=None):
        self.root = root
        self.config = config or workflow.read_json(root / "config/financial_tables.json")
        self.metrics = {}
        self.periods = {}
        self.extra_effects = {}
        if "operations" in self.config and not self.config.get("required_checks"):
            raise ValueError("A configured case must declare nonempty required_checks")
        indexed = {(p["source_id"], p["page"]): p for p in workflow.pages(root)}
        manifest = workflow.read_json(root / "data/manifest.json")["sources"]
        for table in self.config["tables"]:
            source = manifest[table["source_id"]]
            page = indexed[(table["source_id"], table["page"])]
            if workflow.sha256(root / "data/raw" / source["filename"]) != table["source_sha256"]:
                raise ValueError("Financial source fingerprint changed")
            if source["sha256"] != table["source_sha256"] or page["sha256"] != table["source_sha256"]:
                raise ValueError("Financial index/source fingerprints disagree")
            for column in table["columns"]:
                if column["period"] in self.periods:
                    raise ValueError("Duplicate reporting period")
                self.periods[column["period"]] = column
                self.extra_effects[column["period"]] = table["extra_debt_effects"]
            for row in table["rows"]:
                excerpt = workflow.normalise(row["row"])
                if excerpt not in workflow.normalise(page["text"]):
                    raise ValueError(f"Ledger row not found on source page: {row['key']}")
                label = workflow.normalise(row["label"])
                if not excerpt.startswith(label + " "):
                    raise ValueError("Ledger row label does not match")
                tokens = re.findall(r"\([\d,.]+\)|-?[\d,]+(?:\.\d+)?|(?<!\S)-(?!\S)", excerpt[len(label):])
                if len(row["values"]) != len(table["columns"]):
                    raise ValueError("Ledger column count mismatch")
                for index, column in enumerate(table["columns"]):
                    value = amount(row["values"][index])
                    if index >= len(tokens) or value != amount(tokens[index]):
                        raise ValueError(f"Value/sign disagrees with source column: {row['key']}")
                    metric_id = column["period"] + "." + row["key"]
                    if metric_id in self.metrics:
                        raise ValueError("Duplicate metric ID")
                    self.metrics[metric_id] = {
                        "id": metric_id, "key": row["key"], "label": row["label"],
                        "value": str(value), "unit": row.get("unit", self.config["unit"]), "currency": row.get("currency", self.config["currency"]),
                        "basis": row["basis"], "period": column["period"],
                        "start": column["start"], "end": column["end"], "months": column["months"],
                        "balance_date": column["opening"] if row["basis"] == "opening_balance" else column["end"] if row["basis"] == "closing_balance" else None,
                        "source_id": table["source_id"], "page": table["page"], "excerpt": excerpt,
                        "source_sha256": table["source_sha256"]}

    def for_period(self, period):
        if period not in self.periods:
            raise ValueError("Unknown reporting period")
        return [item for item in self.metrics.values() if item["period"] == period]

    def calculate(self, operation, period, reference_period=None):
        if "operations" in self.config:
            return self.calculate_configured(operation, period, reference_period)
        used = []

        def get(key, selected=period):
            metric = self.metrics[selected + "." + key]
            used.append(metric["id"])
            return amount(metric["value"])

        if period not in self.periods:
            raise ValueError("Unknown reporting period")
        if reference_period is not None and reference_period not in self.periods:
            raise ValueError("Unknown reference period")
        residual = None
        unit = self.config["unit"]
        caveat = "Historical arithmetic; it does not establish forward liquidity sufficiency."
        if operation == "fcf_bridge":
            result = get("cfo") + get("investment") + get("divestment")
            residual = result - get("fcf")
            formula = "operating cash flow + signed gross investment cash flow + divestment proceeds"
        elif operation == "net_debt_bridge":
            result = get("debt_open") - get("fcf") + get("dividends_hybrid") + get("leases") + get("fx_other")
            for key in self.extra_effects[period]:
                result += get(key)
            residual = result - get("debt_close")
            formula = "opening net debt - free cash flow + distributions/hybrid coupons + leases + FX/other + disclosed extra financing effects"
        elif operation == "funding_gap":
            result = -(get("cfo") + get("investment"))
            formula = "-(operating cash flow + signed gross investment cash flow)"
            caveat = "Positive value is the historical gap before divestments and financing; not a forecast funding requirement."
        elif operation == "investment_coverage_pct":
            numerator, denominator = get("cfo"), abs(get("investment"))
            if denominator == 0:
                raise ValueError("Cannot divide by zero investment")
            result, unit = numerator / denominator * 100, "%"
            formula = "operating cash flow / absolute gross investment cash flow * 100"
            caveat = "Coverage within this reporting interval, not a solvency test or a covenant metric."
        elif operation == "net_debt_change":
            result = get("debt_close") - get("debt_open")
            formula = "closing net debt - opening net debt"
        elif operation in ("cfo_growth_pct", "fcf_change"):
            if reference_period is None:
                raise ValueError("A matching reference period is required")
            current, previous = self.periods[period], self.periods[reference_period]
            if current["months"] != previous["months"] or current["start"][5:] != previous["start"][5:] or current["end"][5:] != previous["end"][5:]:
                raise ValueError("Flow comparison requires matching reporting intervals; annual versus nine-month comparison is blocked")
            if date.fromisoformat(current["end"]) <= date.fromisoformat(previous["end"]):
                raise ValueError("Reference period must precede the current period")
            key = "cfo" if operation == "cfo_growth_pct" else "fcf"
            current_value, baseline = get(key), get(key, reference_period)
            if operation == "cfo_growth_pct":
                if baseline <= 0:
                    raise ValueError("Growth percentage requires a positive baseline")
                result, unit = (current_value / baseline - 1) * 100, "%"
                formula = "(current operating cash flow / comparable prior operating cash flow - 1) * 100"
            else:
                result = current_value - baseline
                formula = "current free cash flow - comparable prior free cash flow"
        else:
            raise ValueError("Unknown calculation operation")
        return {"operation": operation, "period": period, "reference_period": reference_period,
                "value": formatted(result), "unit": unit, "formula": formula,
                "metric_ids": list(dict.fromkeys(used)),
                "residual": formatted(residual) if residual is not None else None,
                "reconciled": residual == 0 if residual is not None else None, "caveat": caveat}

    def calculate_configured(self, operation, period, reference_period=None):
        """Run one declared operation; no arbitrary formulas or Python evaluation."""
        spec = self.config["operations"].get(operation)
        if spec is None or period not in self.periods:
            raise ValueError("Unknown operation or reporting period")
        if reference_period is not None and reference_period not in self.periods:
            raise ValueError("Unknown reference period")
        used = []

        def get(key, selected=period):
            metric = self.metrics[selected + "." + key]
            if metric["unit"] != self.config["unit"]:
                raise ValueError("Configured calculation mixes units")
            used.append(metric["id"])
            return amount(metric["value"])

        kind = spec["kind"]
        residual = None
        unit = self.config["unit"]
        if kind in ("difference", "growth_pct"):
            if reference_period is None:
                raise ValueError("Comparable reference period is required")
            current, previous = self.periods[period], self.periods[reference_period]
            if current["months"] != previous["months"] or current["start"][5:] != previous["start"][5:] or current["end"][5:] != previous["end"][5:]:
                raise ValueError("Flow comparison requires matching reporting intervals")
            if date.fromisoformat(current["end"]) <= date.fromisoformat(previous["end"]):
                raise ValueError("Reference period must precede the current period")
            current_value, baseline = get(spec["key"]), get(spec["key"], reference_period)
            if kind == "growth_pct":
                if baseline <= 0:
                    raise ValueError("Growth percentage requires a positive baseline")
                result, unit = (current_value / baseline - 1) * 100, "%"
            else:
                result = current_value - baseline
        elif kind == "ratio_pct":
            numerator, denominator = get(spec["numerator"]), get(spec["denominator"])
            if denominator == 0:
                raise ValueError("Cannot divide by zero")
            result, unit = numerator / denominator * 100, "%"
        elif kind in ("sum", "reconcile"):
            terms = spec["terms"]
            if not terms:
                raise ValueError("A sum requires terms")
            result = sum((amount(term["coefficient"]) * get(term["key"]) for term in terms), Decimal(0))
            if kind == "reconcile":
                residual = result - get(spec["target"])
        else:
            raise ValueError("Unsupported configured calculation kind")
        return {"operation": operation, "period": period, "reference_period": reference_period,
                "value": formatted(result), "unit": unit, "formula": spec["formula"],
                "metric_ids": list(dict.fromkeys(used)),
                "residual": formatted(residual) if residual is not None else None,
                "reconciled": residual == 0 if residual is not None else None,
                "caveat": spec["caveat"]}
