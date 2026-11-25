import pathlib
import sys
import unittest

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from experiments.hk_models import fees  # noqa: E402
from paper_trader.export import _json_safe  # noqa: E402

try:  # pandas is only available in the research venv; fee tests stay stdlib-only
    from experiments.hk_models import backtest as hk_backtest  # noqa: E402
except ModuleNotFoundError:  # pragma: no cover - environment dependent
    hk_backtest = None


class FeeScheduleTest(unittest.TestCase):
    def test_statutory_charges(self):
        charges = fees.order_fees(100_000)
        self.assertAlmostEqual(charges["stamp_duty"], 100.0)
        self.assertAlmostEqual(charges["trading_fee"], 5.65)
        self.assertAlmostEqual(charges["sfc_levy"], 2.7)
        self.assertAlmostEqual(charges["afrc_levy"], 0.15)
        self.assertAlmostEqual(charges["settlement_fee"], 2.0)
        self.assertAlmostEqual(charges["statutory_total"], 110.5)

    def test_stamp_duty_rounds_up(self):
        self.assertEqual(fees.order_fees(1_000.5)["stamp_duty"], 2.0)

    def test_trade25_commission_threshold(self):
        self.assertEqual(fees.commission(100_000, 0), 0.0)
        self.assertEqual(fees.commission(100_000, 200_000), 250.0)
        self.assertEqual(fees.commission(100_000, 240_000), 250.0)
        self.assertEqual(fees.commission(100, 250_000), 100.0)

    def test_cost_multiplier(self):
        base = fees.total_order_cost(100_000, 0, multiplier=1.0)["total"]
        doubled = fees.total_order_cost(100_000, 0, multiplier=2.0)["total"]
        self.assertAlmostEqual(doubled, base * 2)

    def test_board_lot_target(self):
        self.assertEqual(fees.target_shares(100_000, 10.0, 500), 10_000)
        self.assertEqual(fees.target_shares(100_000, 10.0, 2000), 10_000)
        self.assertEqual(fees.target_shares(1_000, 10.0, 500), 0)


class PayloadSanitisationTest(unittest.TestCase):
    def test_non_finite_floats_become_zero(self):
        self.assertEqual(_json_safe({"a": float("nan"), "b": [float("inf"), -1.5]}),
                         {"a": 0.0, "b": [0.0, -1.5]})


@unittest.skipIf(hk_backtest is None, "pandas unavailable (use the research venv)")
class HongKongBacktestTest(unittest.TestCase):
    def _panel(self, days=10, stocks=2):
        shape = (stocks, days)
        panel = {
            "raw_open": np.full(shape, 10.0),
            "raw_close": np.full(shape, 10.0),
            "preclose": np.full(shape, 10.0),
            "amount": np.full(shape, 1e9),
            "tradable": np.ones(shape),
            "delisted": np.zeros(shape),
            "is_st": np.zeros(shape),
            "returns": np.zeros(shape),
        }
        metadata = {
            "dates": [f"2026-09-{day + 1:02d}" for day in range(days)],
            "stocks": ["00001", "00002"],
            "names": {"00001": "长和", "00002": "中电控股"},
            "lots": {"00001": 500, "00002": 500},
        }
        return panel, metadata

    def test_whole_lots_and_fees(self):
        panel, metadata = self._panel()
        scores = np.full((2, 10), np.nan)
        scores[:, 0] = [1.0, 0.0]
        result = hk_backtest.backtest(
            panel, metadata, scores, 1_000_000, 0, 9,
            cost_multiplier=1.0,
        )
        self.assertTrue(np.all(result["units"] % 500 == 0))
        self.assertGreater(result["total_fees"], 0)
        self.assertGreater(result["rows"][-1]["equity"], 0)
        self.assertLessEqual(result["rows"][-1]["cash"], 1_000_000)

    def test_zero_cost_leaves_cash_and_equity_flat(self):
        panel, metadata = self._panel()
        scores = np.full((2, 10), np.nan)
        scores[:, 0] = [1.0, 0.0]
        result = hk_backtest.backtest(
            panel, metadata, scores, 1_000_000, 0, 9,
            cost_multiplier=0.0,
        )
        np.testing.assert_allclose(result["rows"][-1]["equity"], 1_000_000, atol=1e-6)

    def test_metrics_shape(self):
        rows = [{"equity": 100.0, "holdings": 1} for _ in range(20)]
        stats = hk_backtest.metrics(rows, [], 100.0)
        for key in ("ret_pct", "max_drawdown", "sharpe", "turnover", "annualized_cost"):
            self.assertIn(key, stats)


if __name__ == "__main__":
    unittest.main()
