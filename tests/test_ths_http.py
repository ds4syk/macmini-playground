import json
import tempfile
import unittest
from pathlib import Path

from paper_trader import ths_http


class CookieTest(unittest.TestCase):
    def test_cookie_header_filters_by_domain(self):
        cookies = [
            {"name": "user", "value": "u1", "domain": ".10jqka.com.cn"},
            {"name": "ticket", "value": "t1", "domain": "mncg.10jqka.com.cn"},
            {"name": "PHPSESSID", "value": "p1", "domain": "upass.10jqka.com.cn"},
            {"name": "other", "value": "o1", "domain": ".example.com"},
        ]
        header = ths_http.cookie_header(cookies)
        self.assertIn("user=u1", header)
        self.assertIn("ticket=t1", header)
        self.assertNotIn("PHPSESSID", header)
        self.assertNotIn("other=o1", header)

    def test_load_cookies_missing_file(self):
        with self.assertRaises(ths_http.CookieMissing):
            ths_http.load_cookies(Path("/tmp/definitely-not-here-cookies.json"))

    def test_load_cookies_rejects_empty(self):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        handle.write(json.dumps({"cookies": []}))
        handle.close()
        self.addCleanup(Path(handle.name).unlink)
        with self.assertRaises(ths_http.CookieMissing):
            ths_http.load_cookies(Path(handle.name))


class PureHelperTest(unittest.TestCase):
    def test_pick_prefers_first_present(self):
        self.assertEqual(ths_http._pick({"a": "", "b": 0, "c": "x"}, "a", "b", "c"), 0)
        self.assertIsNone(ths_http._pick({"a": ""}, "a", "b"))

    def test_positions_csv_from_http_rows(self):
        rows = [
            {"symbol": "600000", "name": "浦发银行", "shares": 1000, "available": 1000, "price": 10.126},
        ]
        self.assertEqual(
            ths_http._positions_csv(rows),
            "symbol,shares,price\n600000,1000,10.13\n",
        )


if __name__ == "__main__":
    unittest.main()
