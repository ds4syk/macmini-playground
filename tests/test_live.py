import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from paper_trader import live


class FakeClient:
    def __init__(self, deals, prices):
        self._deals = deals
        self._prices = prices

    def deals(self):
        return self._deals

    def quote(self, symbol):
        return self._prices.get(symbol, {})

    def account(self):
        return {"zzc": "200000", "kyje": "150000", "djje": "0"}

    def positions(self):
        return []


def sample_deal(**overrides):
    deal = {
        "d_2130": "deal-1",
        "d_2135": "contract-1",
        "d_2102": "600000",
        "d_2103": "浦发银行",
        "d_2109": "买入",
        "d_2128": "100",
        "d_2129": "10.00",
        "d_2131": "1000.00",
        "d_2142": "10:00:00",
    }
    deal.update(overrides)
    return deal


class LiveLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.orders = root / "orders.json"
        self.ledger = root / "ledger.json"
        self.live_json = root / "live.json"
        self.orders.write_text(json.dumps({
            "started_at": "2026-11-18T10:00:00",
            "capital": 30000,
            "models": {
                "demo": {
                    "family": "test",
                    "name": "Demo",
                    "orders": [{"symbol": "600000", "shares": 100, "htbh": "contract-1"}],
                }
            },
        }), encoding="utf-8")
        self.patches = [
            mock.patch.object(live, "ORDERS_PATH", self.orders),
            mock.patch.object(live, "LEDGER_PATH", self.ledger),
            mock.patch.object(live, "LIVE_JSON", self.live_json),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        self.tmp.cleanup()

    def test_sync_attributes_fills_and_deduplicates(self):
        client = FakeClient([sample_deal()], {})
        result = live.sync(client)
        self.assertEqual(result["added"], 1)
        ledger = json.loads(self.ledger.read_text(encoding="utf-8"))
        model = ledger["models"]["demo"]
        self.assertEqual(model["positions"]["600000"]["shares"], 100)
        self.assertAlmostEqual(model["cash"], 30000 - 1000 - 5, places=2)
        # 同一笔成交重复拉取不会重复记账
        self.assertEqual(live.sync(client)["added"], 0)
        self.assertEqual(len(json.loads(self.ledger.read_text(encoding="utf-8"))["models"]["demo"]["fills"]), 1)

    def test_value_writes_live_json(self):
        client = FakeClient(
            [sample_deal()],
            {"600000": {"st_price": "11.00", "st_pclose": "10.00"}},
        )
        payload = live.value(client)
        model = payload["models"][0]
        self.assertAlmostEqual(model["equity"], 28995 + 1100, places=2)  # 现金 28995 + 100*11
        self.assertAlmostEqual(model["ret_pct"], round((30095 / 30000 - 1) * 100, 2), places=2)
        self.assertTrue(self.live_json.exists())

    def test_sell_returns_cash(self):
        client = FakeClient([
            sample_deal(),
            sample_deal(d_2130="deal-2", d_2109="卖出", d_2129="12.00", d_2131="1200.00", d_2128="100"),
        ], {"600000": {"st_price": "12.00", "st_pclose": "11.00"}})
        live.sync(client)
        ledger = json.loads(self.ledger.read_text(encoding="utf-8"))
        model = ledger["models"]["demo"]
        self.assertNotIn("600000", model["positions"])
        # 买 1000+5，卖 1200 - fee(2.4) = 1197.6
        self.assertAlmostEqual(model["cash"], 30000 - 1005 + 1200 - 5, places=2)


if __name__ == "__main__":
    unittest.main()
