import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from paper_trader import config, engine, models, store  # noqa: E402


class FeeTest(unittest.TestCase):
    def test_buy_fee_has_minimum_commission(self):
        self.assertAlmostEqual(engine.fees("buy", 1000), 5.01, places=2)

    def test_sell_fee_includes_stamp_duty(self):
        self.assertAlmostEqual(engine.fees("sell", 10000), 5 + 5 + 0.1, places=2)


class TargetNormalizationTest(unittest.TestCase):
    def test_clamps_weight_and_drops_unknown(self):
        targets, problems = models.normalize_targets(
            [
                {"symbol": "600519", "weight": 0.5, "reason": "x"},
                {"symbol": "999999", "weight": 0.2, "reason": "bad"},
            ],
            {"600519"},
        )
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["weight"], 0.25)
        self.assertTrue(any("unknown symbol" in p for p in problems))

    def test_scales_when_sum_exceeds_one(self):
        targets, problems = models.normalize_targets(
            [
                {"symbol": "600519", "weight": 0.25},
                {"symbol": "600036", "weight": 0.25},
                {"symbol": "000725", "weight": 0.25},
                {"symbol": "601318", "weight": 0.25},
                {"symbol": "600900", "weight": 0.25},
            ],
            {"600519", "600036", "000725", "601318", "600900"},
        )
        self.assertLessEqual(sum(t["weight"] for t in targets), 1.0)
        self.assertTrue(any("scaled" in p for p in problems))

    def test_empty_targets_allowed(self):
        targets, problems = models.normalize_targets([], {"600519"})
        self.assertEqual(targets, [])
        self.assertEqual(problems, [])


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._old_data_dir = config.DATA_DIR
        self._old_db_path = config.DB_PATH
        config.DATA_DIR = pathlib.Path(self.tmp.name)
        config.DB_PATH = config.DATA_DIR / "test.db"
        self.conn = store.connect()
        store.init_portfolios(self.conn)
        for date, open_, close in [
            ("2026-01-05", 100.0, 102.0),
            ("2026-01-06", 110.0, 108.0),
        ]:
            store.bulk_upsert_bars(
                self.conn,
                [("600519", date, open_, close, open_ + 2, open_ - 2, 10000)],
            )

    def tearDown(self):
        self.conn.close()
        config.DATA_DIR = self._old_data_dir
        config.DB_PATH = self._old_db_path

    def _portfolio(self):
        return self.conn.execute(
            "SELECT * FROM portfolios WHERE id = 'deepseek-flash__100000'"
        ).fetchone()

    def test_buy_and_mark(self):
        model = config.model_by_id("deepseek-flash")
        result = engine.execute_targets(
            self.conn,
            self._portfolio(),
            model,
            "2026-01-05",
            [{"symbol": "600519", "weight": 0.2, "reason": "test"}],
        )
        self.assertEqual(result["trades"], 1)
        row = self.conn.execute("SELECT * FROM positions").fetchone()
        self.assertEqual(row["shares"], 200)
        self.conn.execute("UPDATE portfolios SET cash = ? WHERE id = ?", (79794.8, self._portfolio()["id"]))
        snapshot = engine.mark_to_market(
            self.conn,
            self.conn.execute(
                "SELECT * FROM portfolios WHERE id = 'deepseek-flash__100000'"
            ).fetchone(),
            "2026-01-05",
        )
        self.assertAlmostEqual(snapshot["market_value"], 20400.0, places=1)
        self.assertAlmostEqual(snapshot["total"], 100194.8, places=1)

    def test_no_same_day_sell(self):
        model = config.model_by_id("deepseek-flash")
        engine.execute_targets(
            self.conn,
            self._portfolio(),
            model,
            "2026-01-05",
            [{"symbol": "600519", "weight": 0.2}],
        )
        # Second execution on the same session must keep T+1 protection.
        engine.execute_targets(
            self.conn,
            self.conn.execute(
                "SELECT * FROM portfolios WHERE id = 'deepseek-flash__100000'"
            ).fetchone(),
            model,
            "2026-01-05",
            [],
        )
        row = self.conn.execute("SELECT * FROM positions").fetchone()
        self.assertEqual(row["shares"], 200)


if __name__ == "__main__":
    unittest.main()
