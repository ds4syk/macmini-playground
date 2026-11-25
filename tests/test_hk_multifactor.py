import pathlib
import sys
import unittest

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:  # pandas is only in the research venv
    from experiments.hk_models import multifactor as mf
except ModuleNotFoundError:  # pragma: no cover
    mf = None


@unittest.skipIf(mf is None, "pandas unavailable (use the research venv)")
class FactorMathTest(unittest.TestCase):
    def test_zscore_standardises_and_winsorises(self):
        values = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 100.0, np.nan])
        mask = np.isfinite(values)
        z = mf.zscore(values, mask)
        self.assertTrue(np.isfinite(z[mask]).all())
        self.assertLess(np.nanmax(np.abs(z)), 5.0)
        self.assertAlmostEqual(float(np.nanmean(z)), 0.0, places=6)
        self.assertAlmostEqual(float(np.nanstd(z)), 1.0, places=6)

    def test_month_end_indices(self):
        dates = np.array(["2024-01-30", "2024-01-31", "2024-02-01", "2024-02-29", "2024-03-01"])
        months = mf.month_end_indices(dates, "2024-01-01", "2024-12-31")
        self.assertEqual(months, [1, 3])

    def test_holding_map(self):
        self.assertEqual([mf.N_HOLD[c] for c in (100000, 300000, 500000, 1000000)], [4, 8, 10, 40])

    def test_factor_set_and_signs(self):
        self.assertEqual(len(mf.FACTOR_NAMES), 10)
        self.assertEqual(set(mf.FACTOR_NAMES), set(mf.SIGNS))
        self.assertEqual(mf.SIGNS["size"], -1.0)


if __name__ == "__main__":
    unittest.main()
