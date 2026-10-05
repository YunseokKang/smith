import unittest
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal

from smith import ledger, realestate

PROPERTIES = {"home": {"lawd_cd": "11110", "dong": "가동", "apt_name": "예시마을(1단지)", "exclusive_area_m2": 59.9}}


def xml(items, total=None):
    body = "".join("<item>" + "".join(f"<{k}>{v}</{k}>" for k, v in item.items()) + "</item>" for item in items)
    count = len(items) if total is None else total
    return (f"<response><header><resultCode>000</resultCode><resultMsg>OK</resultMsg></header><body><items>{body}"
            f"</items><totalCount>{count}</totalCount></body></response>").encode("utf-8")


def trade(month, day, amount, area="59.94", name="예시마을(1단지)", dong="가동", cancelled=""):
    return {"aptNm": name, "umdNm": dong, "excluUseAr": area, "dealAmount": amount, "dealYear": 2026,
            "dealMonth": month, "dealDay": day, "floor": "5", "cdealType": cancelled}


def rent(month, day, deposit, monthly="0", area="59.94"):
    return {"aptNm": "예시마을(1단지)", "umdNm": "가동", "excluUseAr": area, "deposit": deposit, "monthlyRent": monthly,
            "dealYear": 2026, "dealMonth": month, "dealDay": day, "floor": "3", "contractType": "신규"}


class RealEstateTests(unittest.TestCase):
    def test_sync_keeps_only_the_same_complex_and_size(self):
        seen = []

        def get(url):
            seen.append(url)
            month = int(url.split("DEAL_YMD=")[1][4:6])
            if "Trade" in url:
                items = [trade(month, 1, "150,000"), trade(month, 2, "200,000", area="84.9"),
                         trade(month, 3, "90,000", name="다른단지")] if month in (9, 3) else []
            else:
                items = [rent(month, 4, "50,000"), rent(month, 5, "10,000", monthly="120")] if month == 9 else []
            return 200, xml(items)
        deals, fetched = realestate.sync(PROPERTIES, "key+with/slash", today=date(2026, 10, 5), get=get)
        self.assertEqual(len(seen), 26)                                   # 13 months x trade and rent, one district.
        self.assertIn(("home", "trade", "202510"), fetched)               # The oldest month of the previous half-year.
        self.assertIn("serviceKey=key%2Bwith%2Fslash", seen[0])           # Raw keys are encoded once.
        self.assertEqual(sorted((d["kind"], d["price"]) for d in deals),
                         [("rent", "100000000"), ("rent", "500000000"), ("trade", "1500000000"), ("trade", "1500000000")])

    def test_analysis_uses_recent_uncancelled_trades_and_pure_jeonse(self):
        def deal(kind, day, price, rent_amount="0", active=1, cancelled=0):
            return {"property_ref": "home", "kind": kind, "deal_date": day, "area": "59.94", "price": price,
                    "monthly_rent": rent_amount, "floor": "5", "contract_type": None, "active_count": active,
                    "cancelled_count": cancelled}
        deals = [deal("trade", "2026-09-01", "1600000000", active=2), deal("trade", "2026-08-01", "1500000000"),
                 deal("trade", "2026-07-01", "9900000000", active=0, cancelled=1),
                 deal("trade", "2026-01-15", "1400000000", active=3),
                 deal("rent", "2026-09-10", "300000000"), deal("rent", "2026-08-10", "340000000"),
                 deal("rent", "2026-08-11", "100000000", rent_amount="1000000")]
        result = realestate.analyze(deals, today=date(2026, 10, 5))["home"]
        self.assertEqual((result["estimate"], result["estimate_basis"]), (Decimal(1_600_000_000), 3))  # 1.6, 1.6, 1.5.
        self.assertEqual(result["jeonse"], Decimal(320_000_000))          # Monthly-rent contracts are excluded.
        self.assertEqual(result["change_6m"].quantize(Decimal("0.0001")), Decimal("0.1429"))  # Both sides have 3.
        thin = realestate.analyze(deals[1:2] + deals[3:4], today=date(2026, 10, 5))["home"]
        self.assertIsNone(thin["change_6m"])                              # Too few deals to compare.

    def test_a_sync_replaces_whole_months_and_counts_identical_deals(self):
        conn = ledger.connect(":memory:")
        self.addCleanup(conn.close)
        at = datetime(2026, 10, 5, tzinfo=timezone.utc)

        def deal(day, cancelled=False, ref="home"):
            return {"property_ref": ref, "kind": "trade", "deal_date": day, "area": "59.94", "price": "1500000000",
                    "monthly_rent": "0", "floor": "5", "contract_type": None, "cancelled": cancelled}
        months = {("home", "trade", "202609"), ("home", "trade", "202608")}
        ledger.replace_deals(conn, [deal("2026-09-01"), deal("2026-09-01"), deal("2026-08-01"), deal("2026-08-02", ref="gone")],
                             months=months, properties={"home", "gone"}, fetched_at=at)
        twin = [r for r in ledger.load_deals(conn, since=date(2026, 1, 1)) if r["deal_date"] == "2026-09-01"]
        self.assertEqual((len(twin), twin[0]["active_count"]), (1, 2))     # Two identical deals, counted.
        # Next sync: the August deal was withdrawn, one September twin was cancelled (listed in any order),
        # and the "gone" property left the config.
        ledger.replace_deals(conn, [deal("2026-09-01", cancelled=True), deal("2026-09-01")], months=months,
                             properties={"home"}, fetched_at=at)
        rows = ledger.load_deals(conn, since=date(2026, 1, 1))
        self.assertEqual([(r["deal_date"], r["active_count"], r["cancelled_count"]) for r in rows],
                         [("2026-09-01", 1, 1)])

    def test_bad_property_config_is_reported_by_field(self):
        with self.assertRaises(realestate.RealEstateError) as caught:
            realestate.validate_properties({"home": dict(PROPERTIES["home"], exclusive_area_m2="59.9")})
        self.assertEqual(caught.exception.code, "bad-config:home.exclusive_area_m2")

    def test_api_errors_are_reported_by_code(self):
        bad = b"<response><header><resultCode>30</resultCode></header></response>"
        with self.assertRaises(realestate.RealEstateError) as caught:
            realestate.fetch_month("trade", "11110", "202609", "k", get=lambda url: (200, bad))
        self.assertEqual(caught.exception.code, "api-30")


if __name__ == "__main__":
    unittest.main()
