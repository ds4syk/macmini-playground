import pathlib
import sys
import unittest

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from experiments.multifactor_smallcap import account  # noqa: E402


class MultifactorAccountTest(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(42)
        self.days = 260
        self.stocks = ["sh600%03d" % index for index in range(10)]
        base = 10 + rng.random(10) * 20
        returns = rng.normal(0, 0.01, (10, self.days))
        self.close = base[:, None] * np.cumprod(1 + returns, axis=1)
        self.pct = returns.astype(np.float32)
        self.mcap = np.full((10, self.days), 2e9, dtype=np.float32)
        self.is_st = np.zeros((10, self.days), dtype=bool)
        self.tradable = np.ones((10, self.days), dtype=bool)
        self.dates = ["2026-%02d-%02d" % (1 + index // 28, 1 + index % 28) for index in range(self.days)]
        self.score = np.full((10, self.days), np.nan, dtype=np.float32)
        for day in range(20, self.days, 21):
            self.score[:3, day] = 1.0
            self.score[3:6, day] = 0.5
            self.score[6:, day] = 0.0

    def test_run_keeps_invariants(self):
        rows, units, cost_basis, trades, fees = account.run(
            self.dates,
            self.stocks,
            self.close,
            self.pct,
            self.mcap,
            self.is_st,
            self.tradable,
            self.score,
            100000,
            20,
            self.days - 1,
            {"portfolio_size": 3, "buffer_size": 3, "max_replace": 3},
        )
        self.assertEqual(len(rows), self.days - 20)
        self.assertTrue(all(row["equity"] > 0 for row in rows))
        self.assertTrue(all(row["cash"] >= -1e-6 for row in rows))
        self.assertTrue((units >= 0).all())
        self.assertGreater(len(trades), 0)

    def test_suspended_prices_do_not_break_marks(self):
        close = self.close.copy()
        close[0, 40:45] = np.nan
        close[1, 60:70] = np.nan
        self.tradable[0, 40:45] = False
        self.tradable[1, 60:70] = False
        rows, units, cost_basis, trades, fees = account.run(
            self.dates,
            self.stocks,
            close,
            self.pct,
            self.mcap,
            self.is_st,
            self.tradable,
            self.score,
            100000,
            20,
            self.days - 1,
            {"portfolio_size": 3, "buffer_size": 3, "max_replace": 3},
        )
        self.assertTrue(all(np.isfinite(row["equity"]) and row["equity"] > 0 for row in rows))

    def test_lot_and_fee(self):
        self.assertEqual(account.lot_size("sh.688001"), 200)
        self.assertEqual(account.lot_size("sh.600000"), 100)
        self.assertAlmostEqual(account.fee(1000, 0.001, 5.0), 5.0)
        self.assertAlmostEqual(account.fee(10000, 0.002, 5.0), 20.0)
        self.assertAlmostEqual(account.price_limit("sz.300750", False), 0.20)
        self.assertAlmostEqual(account.price_limit("sh.600000", True), 0.05)


if __name__ == "__main__":
    unittest.main()
