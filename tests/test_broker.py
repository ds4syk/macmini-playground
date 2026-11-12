import unittest

from paper_trader import broker


def sample_portfolio():
    return {
        "model_id": "mf-lgbm",
        "model_name": "多因子 LightGBM",
        "capital": 30000,
        "total": 30000,
        "cash": 3000,
        "positions": [
            {"symbol": "sz000001", "name": "A", "shares": 1000, "price": 10.0, "value": 10000, "weight": 10000 / 30000},
            {"symbol": "sh600000", "name": "B", "shares": 500, "price": 20.0, "value": 10000, "weight": 10000 / 30000},
            {"symbol": "sh688001", "name": "C", "shares": 250, "price": 20.0, "value": 5000, "weight": 5000 / 30000},
        ],
    }


class BrokerOrderTest(unittest.TestCase):
    def test_lot_rules(self):
        self.assertEqual(broker.round_buy_shares("600000", 149.9), 100)
        self.assertEqual(broker.round_buy_shares("688001", 199), 0)
        self.assertEqual(broker.round_buy_shares("688001", 200), 200)
        self.assertEqual(broker.round_buy_shares("688001", 250), 250)
        self.assertEqual(broker.trade_fee(1000, 0.001), 5.0)
        self.assertEqual(broker.trade_fee(100000, 0.001), 100.0)

    def test_fresh_account_stays_invested(self):
        plan = broker.plan_orders(sample_portfolio(), capital=30000)
        buys = [order for order in plan["orders"] if order.side == "买入"]
        self.assertTrue(buys)
        total = sum(order.amount + order.fee for order in buys)
        self.assertLessEqual(total, 30000)
        self.assertLess(plan["end_cash"], 0.15 * 30000)
        self.assertAlmostEqual(plan["end_cash"], 30000 - total, places=2)
        for order in buys:
            if order.symbol.startswith(("688", "689")):
                self.assertGreaterEqual(order.shares, 200)
            else:
                self.assertEqual(order.shares % 100, 0)
        self.assertEqual(plan["end_cash"] > 0, True)

    def test_delta_sells_extra_and_buys_underweight(self):
        current = {"sz000001": 600, "sz000002": 1000}
        plan = broker.plan_orders(sample_portfolio(), current=current, cash=25000)
        by_symbol = {order.symbol: order for order in plan["orders"]}
        self.assertEqual(by_symbol["000002"].side, "卖出")
        self.assertEqual(by_symbol["000002"].shares, 1000)
        self.assertEqual(by_symbol["000001"].side, "买入")
        self.assertGreater(by_symbol["000001"].shares, 0)
        self.assertEqual(by_symbol["000001"].shares % 100, 0)

    def test_target_weights_are_replicated(self):
        plan = broker.plan_orders(sample_portfolio(), capital=30000)
        weights = {
            order.symbol: order.amount / 30000
            for order in plan["orders"]
            if order.side == "买入"
        }
        self.assertAlmostEqual(weights["000001"], 1 / 3, delta=0.04)
        self.assertAlmostEqual(weights["600000"], 1 / 3, delta=0.04)
        self.assertAlmostEqual(weights["688001"], 1 / 6, delta=0.04)


if __name__ == "__main__":
    unittest.main()
