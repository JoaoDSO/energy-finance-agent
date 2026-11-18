"""Synthetic financial fixtures; no invented numbers are attributed to Orsted."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from analysis_tools import AnalysisTools, render_brief, validate_analysis
from finance import FinancialLedger
import workflow


class FinanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        raw = self.root / "data/raw/fixture.pdf"
        raw.parent.mkdir(parents=True)
        raw.write_bytes(b"Fictional binary source for unit tests only")
        fingerprint = workflow.sha256(raw)
        rows = [
            ("cfo", "Operating cash flow", "cash_flow", "80 60", ["80", "60"]),
            ("investment", "Gross investments", "cash_flow", "(120) (110)", ["-120", "-110"]),
            ("divestment", "Divestments", "cash_flow", "20 10", ["20", "10"]),
            ("fcf", "Free cash flow", "cash_flow", "(20) (40)", ["-20", "-40"]),
            ("debt_open", "Opening net debt", "opening_balance", "100 70", ["100", "70"]),
            ("dividends_hybrid", "Distributions", "debt_effect", "2 1", ["2", "1"]),
            ("leases", "New leases", "debt_effect", "3 2", ["3", "2"]),
            ("fx_other", "Other changes", "debt_effect", "5 7", ["5", "7"]),
            ("debt_close", "Closing net debt", "closing_balance", "130 120", ["130", "120"]),
        ]
        self.config = {"provenance": "Fictional fixture", "unit": "DKKm", "currency": "DKK", "tables": [{
            "id": "fixture", "source_id": "fixture", "page": 1, "source_sha256": fingerprint,
            "columns": [
                {"period": "9M2026", "start": "2026-01-01", "end": "2026-09-30", "opening": "2025-12-31", "months": 9},
                {"period": "9M2025", "start": "2025-01-01", "end": "2025-09-30", "opening": "2024-12-31", "months": 9}],
            "extra_debt_effects": [], "rows": [
                {"key": key, "label": label, "basis": basis, "row": label + " " + text, "values": values}
                for key, label, basis, text, values in rows]}]}
        text = "\n".join(row["row"] for row in self.config["tables"][0]["rows"])
        (self.root / "data/pages.jsonl").write_text(__import__("json").dumps({"source_id": "fixture", "page": 1, "sha256": fingerprint, "text": text}) + "\n", encoding="utf-8")
        workflow.write_json(self.root / "data/manifest.json", {"sources": {"fixture": {"filename": "fixture.pdf", "sha256": fingerprint}}})
        workflow.write_json(self.root / "config/analysis.schema.json", workflow.read_json(workflow.ROOT / "config/analysis.schema.json"))
        self.ledger = FinancialLedger(self.root, self.config)
        self.research = {"findings": []}

    def calculations(self):
        return {"C01": self.ledger.calculate("fcf_bridge", "9M2026"),
                "C02": self.ledger.calculate("net_debt_bridge", "9M2026"),
                "C03": self.ledger.calculate("cfo_growth_pct", "9M2026", "9M2025")}

    def draft(self, calculations):
        claims = [{"id": cid, "kind": "calculated", "text": "Fictional calculated statement", "value": calc["value"], "unit": calc["unit"], "calculation_id": cid,
                   "metric_ids": calc["metric_ids"], "research_refs": [], "caveat": "Fictional test"} for cid, calc in calculations.items()]
        claims.append({"id": "R01", "kind": "reported", "text": "Fictional reported CFO", "value": "80", "unit": "DKKm", "calculation_id": None,
                       "metric_ids": ["9M2026.cfo"], "research_refs": [], "caveat": "Fictional test"})
        return {"title": "Fixture", "assessment": "Fictional assessment", "claims": claims,
                "risks_to_watch": [], "open_questions": [], "limitations": ["Test only"]}

    def test_cashflow_and_debt_bridges_use_correct_signs(self):
        cash = self.ledger.calculate("fcf_bridge", "9M2026")
        debt = self.ledger.calculate("net_debt_bridge", "9M2026")
        self.assertEqual(cash["value"], "-20.00")
        self.assertEqual(debt["value"], "130.00")
        self.assertEqual((cash["residual"], debt["residual"]), ("0.00", "0.00"))

    def test_comparable_growth_and_signed_fcf_change(self):
        self.assertEqual(self.ledger.calculate("cfo_growth_pct", "9M2026", "9M2025")["value"], "33.33")
        self.assertEqual(self.ledger.calculate("fcf_change", "9M2026", "9M2025")["value"], "20.00")

    def test_mixed_duration_comparison_is_blocked(self):
        self.ledger.periods["FY2025"] = {**self.ledger.periods["9M2025"], "months": 12, "end": "2025-12-31"}
        with self.assertRaisesRegex(ValueError, "matching reporting intervals"):
            self.ledger.calculate("cfo_growth_pct", "9M2026", "FY2025")

    def test_zero_denominator_is_blocked(self):
        self.ledger.metrics["9M2026.investment"]["value"] = "0"
        with self.assertRaisesRegex(ValueError, "zero"):
            self.ledger.calculate("investment_coverage_pct", "9M2026")

    def test_source_column_or_sign_tampering_is_rejected(self):
        config = copy.deepcopy(self.config)
        config["tables"][0]["rows"][1]["values"][0] = "120"
        with self.assertRaisesRegex(ValueError, "sign"):
            FinancialLedger(self.root, config)

    def test_changed_source_fingerprint_is_rejected(self):
        (self.root / "data/raw/fixture.pdf").write_bytes(b"Changed fixture")
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            FinancialLedger(self.root, self.config)

    def test_valid_numeric_claims_pass(self):
        calculations = self.calculations()
        self.assertTrue(validate_analysis(self.draft(calculations), self.ledger, calculations, self.research, self.root)["passed"])

    def test_review_source_links_are_added_without_rewriting_the_claim(self):
        calculations = self.calculations()
        draft = self.draft(calculations)
        before = copy.deepcopy(draft)
        text = render_brief(draft, self.ledger, calculations, self.research, "runs/fixture", {"C01": {("fixture", 2)}})
        self.assertIn("fixture.pdf#page=2", text)
        self.assertEqual(draft, before)

    def test_invented_calculation_result_is_rejected(self):
        calculations = self.calculations()
        draft = self.draft(calculations)
        draft["claims"][0]["value"] = "999"
        self.assertFalse(validate_analysis(draft, self.ledger, calculations, self.research, self.root)["passed"])

    def test_missing_reconciliation_is_rejected(self):
        calculations = self.calculations()
        draft = self.draft(calculations)
        del calculations["C02"]
        self.assertFalse(validate_analysis(draft, self.ledger, calculations, self.research, self.root)["passed"])

    def test_wrong_units_and_invented_reported_values_rejected(self):
        calculations = self.calculations()
        for field, value in (("unit", "USDm"), ("value", "800")):
            draft = self.draft(calculations)
            draft["claims"][-1][field] = value
            self.assertFalse(validate_analysis(draft, self.ledger, calculations, self.research, self.root)["passed"])

    def test_analysis_tool_reuses_identical_calculation(self):
        run = self.root / "runs/analysis_fixture"
        workflow.write_json(run / "handoff.json", {"research_run": "runs/fixture"})
        workflow.write_json(run / "inputs/research.json", self.research)
        workflow.write_json(run / "inputs/financial_tables.json", self.config)
        tools = AnalysisTools(run, self.root)
        first = tools.calculate_financial("fcf_bridge", "9M2026")
        second = tools.calculate_financial("fcf_bridge", "9M2026")
        self.assertEqual(first["calculation_id"], second["calculation_id"])
        self.assertEqual(len(tools.calculations), 1)

    def test_case_configured_calculations_do_not_require_orsted_operations(self):
        config = copy.deepcopy(self.config)
        config["operations"] = {
            "cash_change": {"kind": "difference", "key": "cfo", "formula": "current CFO - comparable prior CFO", "caveat": "Historical only"},
            "cash_growth": {"kind": "growth_pct", "key": "cfo", "formula": "CFO / comparable prior CFO - 1", "caveat": "Historical only"},
            "cash_bridge": {"kind": "reconcile", "terms": [{"key": "cfo", "coefficient": "1"}, {"key": "investment", "coefficient": "1"}, {"key": "divestment", "coefficient": "1"}], "target": "fcf", "formula": "CFO + investment + divestment", "caveat": "Fixture bridge"},
        }
        config["required_checks"] = [
            {"operation": "cash_growth", "period": "9M2026", "reference_period": "9M2025"},
            {"operation": "cash_bridge", "period": "9M2026", "reconciled": True},
        ]
        ledger = FinancialLedger(self.root, config)
        calculations = {
            "C01": ledger.calculate("cash_change", "9M2026", "9M2025"),
            "C02": ledger.calculate("cash_growth", "9M2026", "9M2025"),
            "C03": ledger.calculate("cash_bridge", "9M2026"),
        }
        self.assertEqual(calculations["C01"]["value"], "20.00")
        self.assertEqual(calculations["C02"]["value"], "33.33")
        self.assertEqual(calculations["C03"]["residual"], "0.00")
        self.assertTrue(validate_analysis(self.draft(calculations), ledger, calculations, self.research, self.root)["passed"])
        del calculations["C03"]
        self.assertFalse(validate_analysis(self.draft(calculations), ledger, calculations, self.research, self.root)["passed"])
        with self.assertRaisesRegex(ValueError, "Unknown operation"):
            ledger.calculate("net_debt_bridge", "9M2026")

    def test_configured_case_requires_explicit_checks(self):
        config = copy.deepcopy(self.config)
        config["operations"] = {}
        with self.assertRaisesRegex(ValueError, "required_checks"):
            FinancialLedger(self.root, config)


if __name__ == "__main__":
    unittest.main()
