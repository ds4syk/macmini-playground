import tempfile
import unittest
from pathlib import Path

from paper_trader import ths_web


class ParseMoneyTest(unittest.TestCase):
    def test_plain_and_grouped(self):
        self.assertEqual(ths_web.parse_money("1,234.56"), 1234.56)
        self.assertEqual(ths_web.parse_money(" 98 元 "), 98.0)
        self.assertEqual(ths_web.parse_money("-12.5"), -12.5)

    def test_missing_values(self):
        self.assertEqual(ths_web.parse_money(""), 0.0)
        self.assertEqual(ths_web.parse_money("-"), 0.0)
        self.assertEqual(ths_web.parse_money(None), 0.0)


class SheetTest(unittest.TestCase):
    def write(self, text):
        handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
        handle.write(text)
        handle.close()
        self.addCleanup(Path(handle.name).unlink)
        return Path(handle.name)

    def test_read_sheet_normalizes_symbols_and_sides(self):
        path = self.write(
            "symbol,name,side,shares,price,amount,fee,reason\n"
            "sh600000,浦发银行,买入,1000,10.00,10000.00,10.00,补足目标权重\n"
            "000002,万科A,卖出,400,8.00,3200.00,6.40,降低权重\n"
        )
        orders = ths_web.read_sheet(path)
        self.assertEqual([order.symbol for order in orders], ["600000", "000002"])
        self.assertEqual([order.side for order in orders], ["buy", "sell"])
        self.assertEqual(orders[0].shares, 1000)

    def test_read_sheet_rejects_missing_columns(self):
        path = self.write("code,qty\n600000,100\n")
        with self.assertRaises(ValueError):
            ths_web.read_sheet(path)

    def test_validate_accepts_clean_sheet(self):
        orders = [
            ths_web.SheetOrder("600000", "浦发银行", "buy", 1000, 10.0, 10000.0, 10.0),
            ths_web.SheetOrder("000002", "万科A", "sell", 400, 8.0, 3200.0, 6.0),
        ]
        self.assertEqual(ths_web.validate_orders(orders), [])

    def test_validate_star_board_and_odd_sell(self):
        orders = [
            ths_web.SheetOrder("688001", "科创板", "buy", 250, 20.0, 5000.0, 5.0),
            ths_web.SheetOrder("600000", "浦发银行", "sell", 37, 10.0, 370.0, 5.0),
        ]
        self.assertEqual(ths_web.validate_orders(orders), [])
        self.assertTrue(ths_web.validate_orders([ths_web.SheetOrder("688001", "x", "buy", 150, 20.0, 3000.0, 5.0)]))

    def test_validate_rejects_lot_side_and_duplicates(self):
        orders = [
            ths_web.SheetOrder("600000", "A", "buy", 150, 10.0, 1500.0, 5.0),
            ths_web.SheetOrder("000002", "B", "hold", 100, 8.0, 800.0, 5.0),
            ths_web.SheetOrder("000002", "B", "sell", 100, 8.0, 800.0, 5.0),
            ths_web.SheetOrder("000002", "B", "sell", 100, 8.0, 800.0, 5.0),
        ]
        problems = ths_web.validate_orders(orders)
        self.assertEqual(len(problems), 3)
        joined = "\n".join(problems)
        self.assertIn("100 股整数倍", joined)
        self.assertIn("方向", joined)
        self.assertIn("出现多次", joined)


class AffordabilityTest(unittest.TestCase):
    def test_buys_over_cash_are_blocked(self):
        orders = [ths_web.SheetOrder("600000", "A", "buy", 1000, 100.0, 100000.0, 5.0)]
        blocked = ths_web._check_affordable(orders, {"可用余额": 50000.0}, skip=False)
        self.assertTrue(blocked)
        self.assertIn("超过可用余额", blocked[0])

    def test_sell_proceeds_cover_buys(self):
        orders = [
            ths_web.SheetOrder("600000", "A", "sell", 1000, 100.0, 100000.0, 5.0),
            ths_web.SheetOrder("000002", "B", "buy", 1000, 90.0, 90000.0, 5.0),
        ]
        self.assertEqual(ths_web._check_affordable(orders, {"可用余额": 0.0}, skip=False), [])
        self.assertEqual(ths_web._check_affordable(orders, {}, skip=True), [])


class PositionTest(unittest.TestCase):
    def test_position_rows_pick_headers_and_keep_lots(self):
        records = [
            {
                "证券代码": "600000", "证券名称": "浦发银行",
                "股票余额": "1,000", "可用余额": "1,000", "市价": "10.12",
            },
            {
                "证券代码": "000002", "证券名称": "万科A",
                "股票余额": "400", "可用余额": "400", "市价": "8.30",
            },
            {"证券代码": "", "证券名称": "合计", "股票余额": "1,400", "市价": "-"},
        ]
        rows = ths_web.position_rows(records)
        self.assertEqual([row.symbol for row in rows], ["600000", "000002"])
        self.assertEqual(rows[0].shares, 1000)
        self.assertEqual(rows[0].price, 10.12)

    def test_positions_csv_matches_broker_reader(self):
        rows = [ths_web.PositionRow("600000", "浦发银行", 1000, 10.123)]
        text = ths_web.positions_csv(rows)
        self.assertEqual(text, "symbol,shares,price\n600000,1000,10.12\n")

    def test_account_summary_converts_values(self):
        summary = ths_web.account_summary({"可用余额": "12,345.67", "总资产": "200,000.00"})
        self.assertEqual(summary["可用余额"], 12345.67)
        self.assertEqual(summary["总资产"], 200000.0)


if __name__ == "__main__":
    unittest.main()
