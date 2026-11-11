import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from paper_trader import local_models  # noqa: E402


class LocalModelRulesTest(unittest.TestCase):
    def test_lot_size(self):
        self.assertEqual(local_models._lot("sh.688001"), 200)
        self.assertEqual(local_models._lot("sh.600000"), 100)
        self.assertEqual(local_models._lot("sz.300750"), 100)

    def test_minimum_fee(self):
        self.assertAlmostEqual(local_models._fee(1000, 0.001), 5.0)
        self.assertAlmostEqual(local_models._fee(10000, 0.002), 20.0)


if __name__ == "__main__":
    unittest.main()
