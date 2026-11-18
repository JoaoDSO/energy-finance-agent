"""Fictional arithmetic/column fixtures for the optional liquidity-context bridge."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from liquidity_check import calculate_bridge, checked_rows


class LiquidityTests(unittest.TestCase):
    def setUp(self):
        self.config = {"column_dates_left_to_right": ["2026-09-30", "2025-12-31", "2025-09-30"],
                       "unit": "DKKm", "source_id": "fictional", "page": 1, "source_sha256": "fictional",
                       "rows": [{"key": "cash", "label": "Cash", "values": ["10", "40", "5"]},
                                {"key": "securities", "label": "Securities", "values": ["60", "30", "10"]}]}
        self.text = "Cash    10 40 5\nSecurities    60 30 10\n"

    def test_same_total_can_hide_cash_to_securities_change(self):
        result = calculate_bridge(checked_rows(self.config, self.text))
        self.assertEqual(result["value"], "0.00")
        self.assertEqual(result["cash_change"], "-30.00")
        self.assertEqual(result["securities_change"], "30.00")
        self.assertEqual(result["movement_check_residual"], "0.00")
        self.assertIn("Not usable liquidity", result["scope"])

    def test_wrong_column_or_sign_is_rejected(self):
        for text in ("Cash 40 10 5\nSecurities 60 30 10", "Cash (10) 40 5\nSecurities 60 30 10"):
            with self.assertRaisesRegex(ValueError, "sign/column"):
                checked_rows(self.config, text)

    def test_missing_or_duplicate_row_is_rejected(self):
        for text in ("Cash 10 40 5", self.text + "Cash 10 40 5"):
            with self.assertRaisesRegex(ValueError, "absent or ambiguous"):
                checked_rows(self.config, text)

    def test_cash_description_is_not_mistaken_for_numeric_cash_row(self):
        metrics = checked_rows(self.config, self.text + "Cash and securities not available for distribution\n")
        self.assertEqual(metrics["2026-09-30.cash"]["value"], "10")

    def test_wrong_unit_or_date_is_rejected(self):
        metrics = checked_rows(self.config, self.text)
        for field, value in (("unit", "USDm"), ("balance_date", "2025-01-01")):
            altered = copy.deepcopy(metrics)
            altered["2026-09-30.cash"][field] = value
            with self.assertRaisesRegex(ValueError, "mismatch"):
                calculate_bridge(altered)
