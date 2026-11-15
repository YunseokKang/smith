"""Holding tax (FR-53): 재산세 and 종합부동산세 against hand-checked and published figures. Synthetic homes only."""
import unittest
from decimal import Decimal
from pathlib import Path

from smith import holding_tax as ht
from smith import official_price

EOK = Decimal(100_000_000)


def home(ref, owner, price, **kw):
    return ht.Home(ref=ref, label=ref, owner=owner, price=None if price is None else Decimal(price), estimated=False,
                   basis="공시가격", **kw)


class PropertyTaxTests(unittest.TestCase):
    def test_a_one_home_under_the_special_cap_uses_the_lower_ratio_and_rates(self):
        tax = ht.property_tax(Decimal(700_000_000), one_home=True)
        # 45% of 700M = 315M; special rates: 420,000 + 15M x 0.35%; urban 0.14%; education 20% of the tax proper.
        self.assertEqual((tax["ratio"], tax["base"], tax["special"]), (Decimal("0.45"), Decimal(315_000_000), True))
        self.assertEqual((tax["main"], tax["urban"], tax["education"], tax["total"]),
                         (Decimal(472_500), Decimal(441_000), Decimal(94_500), Decimal(1_008_000)))
        self.assertEqual(ht.schedule(tax["main"], tax["total"]), [(7, Decimal(504_000)), (9, Decimal(504_000))])

    def test_the_ratio_band_follows_the_price_and_a_small_bill_comes_in_july_at_once(self):
        tax = ht.property_tax(Decimal(150_000_000), one_home=True)
        self.assertEqual(tax["ratio"], Decimal("0.43"))
        self.assertEqual(tax["main"], Decimal(34_500))                  # 30,000 + 4.5M x 0.1%
        self.assertEqual(ht.schedule(tax["main"], tax["total"]), [(7, tax["total"])])
        self.assertEqual(ht.property_tax(Decimal(500_000_000), one_home=True)["ratio"], Decimal("0.44"))

    def test_a_home_that_is_not_the_households_only_one_pays_the_general_ratio_and_rates(self):
        tax = ht.property_tax(Decimal(700_000_000), one_home=False)
        self.assertEqual((tax["ratio"], tax["special"], tax["main"]), (Decimal("0.60"), False, Decimal(1_050_000)))
        # Above the special cap a one-home household keeps the ratio but not the rates.
        dear = ht.property_tax(Decimal(1_000_000_000), one_home=True)
        self.assertEqual((dear["ratio"], dear["special"], dear["main"]), (Decimal("0.45"), False, Decimal(1_170_000)))


class ComprehensiveTaxTests(unittest.TestCase):
    def test_the_budget_offices_example_is_reproduced(self):
        # National Assembly Budget Office (reported 2026-11-10): market 3.0bn, official 2.07bn, one home, no credits,
        # current law: 2,114,000 won. Base (2.07bn - 1.2bn) x 60% = 522M; 3,054,000 less the property tax on the
        # overlapping slice, 522M x 45% x 0.4% = 939,600.
        price = Decimal(2_070_000_000)
        found = ht.comprehensive_tax([(price, ht.property_tax(price, one_home=True))], one_home=True)
        self.assertEqual((found["base"], found["computed"], found["overlap"]),
                         (Decimal(522_000_000), Decimal(3_054_000), Decimal(939_600)))
        self.assertEqual((found["tax"], found["rural"], found["total"]),
                         (Decimal(2_114_400), Decimal(422_880), Decimal(2_537_280)))

    def test_a_home_under_the_deduction_owes_nothing(self):
        price = Decimal(1_100_000_000)
        found = ht.comprehensive_tax([(price, ht.property_tax(price, one_home=True))], one_home=True)
        self.assertEqual((found["base"], found["total"]), (Decimal(0), Decimal(0)))
        # Not a one-home owner: the 9억 deduction applies.
        general = ht.comprehensive_tax([(price, ht.property_tax(price, one_home=False))], one_home=False)
        self.assertEqual(general["base"], Decimal(120_000_000))
        self.assertGreater(general["total"], 0)

    def test_age_and_holding_credits_add_up_to_the_cap(self):
        price = Decimal(2_070_000_000)
        prop = ht.property_tax(price, one_home=True)
        self.assertEqual(ht.comprehensive_tax([(price, prop)], one_home=True, age=66, held_years=12)["credit_rate"],
                         Decimal("0.70"))
        self.assertEqual(ht.comprehensive_tax([(price, prop)], one_home=True, age=72, held_years=16)["credit_rate"],
                         Decimal("0.80"))
        # Unknown age or years give no credit (the estimate can be too high, never too low); never for two homes.
        self.assertEqual(ht.comprehensive_tax([(price, prop)], one_home=True)["credit_rate"], 0)
        self.assertEqual(ht.comprehensive_tax([(price, prop)], one_home=False, age=72, held_years=16)["credit_rate"], 0)

    def test_three_homes_meet_the_heavier_table_above_a_twelve_hundred_million_base(self):
        price = Decimal(1_000_000_000)
        heavy = ht.comprehensive_tax([(price, ht.property_tax(price, one_home=False))] * 3, one_home=False)
        self.assertEqual(heavy["base"], Decimal(1_260_000_000))       # (3.0bn - 0.9bn) x 60%
        self.assertEqual(heavy["computed"], Decimal(9_600_000) + Decimal(60_000_000) * Decimal("0.02"))
        # The same total in two homes stays on the lighter table.
        pair = Decimal(1_500_000_000)
        light = ht.comprehensive_tax([(pair, ht.property_tax(pair, one_home=False))] * 2, one_home=False)
        self.assertEqual(light["base"], heavy["base"])
        self.assertEqual(light["computed"], Decimal(9_600_000) + Decimal(60_000_000) * Decimal("0.013"))


class HouseholdTests(unittest.TestCase):
    def test_unmarried_partners_are_each_a_one_home_household(self):
        homes = [home("h1", "self", 700_000_000, acquired_year=2021), home("h2", "partner", 450_000_000, acquired_year=2020)]
        found = ht.household(homes, {"marriage_registered": False, "birth_year": 1989, "birth_month": 8})
        self.assertTrue(all(row["one_home"] for row in found["homes"]))
        self.assertEqual(found["owners"]["self"]["property"], Decimal(1_008_000))
        self.assertEqual(found["owners"]["self"]["comprehensive"]["total"], 0)
        self.assertEqual(found["total"], found["owners"]["self"]["total"] + found["owners"]["partner"]["total"])
        self.assertIsNone(found["marriage_rule"])
        months = ht.due_on(found)
        self.assertEqual(set(months), {7, 9})                          # No December bill under the deduction.

    def test_after_registration_the_higher_priced_home_keeps_the_property_tax_rate_for_five_years(self):
        # 지방세법 시행령 제110조의2 ①9·②2, read through the law API 2026-11-15.
        homes = [home("h1", "self", 700_000_000, acquired_year=2021), home("h2", "partner", 450_000_000, acquired_year=2020)]
        found = ht.household(homes, {"marriage_registered": True, "marriage_date": "2026-09-01"}, year=2027)
        self.assertEqual({row["ref"]: row["one_home"] for row in found["homes"]}, {"h1": True, "h2": False})
        self.assertEqual(found["marriage_rule"], "until:2031-09-01")
        # Five years on, or a home bought in the wedding's year (not shown to be brought in), or no date: neither.
        later = ht.household(homes, {"marriage_registered": True, "marriage_date": "2026-09-01"}, year=2032)
        self.assertFalse(any(row["one_home"] for row in later["homes"]))
        self.assertEqual(later["marriage_rule"], "after")
        bought = [home("h1", "self", 700_000_000, acquired_year=2026), homes[1]]
        self.assertFalse(any(r["one_home"] for r in ht.household(bought, {"marriage_registered": True,
                                                                          "marriage_date": "2026-09-01"}, year=2027)["homes"]))
        undated = ht.household(homes, {"marriage_registered": True}, year=2027)
        self.assertEqual((undated["marriage_rule"], any(r["one_home"] for r in undated["homes"])), ("undated", False))
        # The comprehensive tax's marriage rule keeps each owner's one-home status for ten years from the wedding.
        self.assertTrue(found["owners"]["self"]["one_home"]["comprehensive"])
        # Codex review 2026-11-15: not after the ten years, and not on a guess when the date is not configured.
        dear = [home("h1", "self", 1_500_000_000), home("h2", "partner", 1_500_000_000)]
        old = ht.household(dear, {"marriage_registered": True, "marriage_date": "2000-05-01"}, year=2026)
        self.assertFalse(old["owners"]["self"]["one_home"]["comprehensive"])
        self.assertEqual(old["owners"]["self"]["comprehensive"]["deduction"], ht.GENERAL_DEDUCTION)
        undated = ht.household(dear, {"marriage_registered": True}, year=2026)
        self.assertFalse(undated["owners"]["self"]["one_home"]["comprehensive"])
        self.assertTrue(undated["owners"]["self"]["one_home"]["marriage_date_missing"])
        # The wedding's tenth anniversary ends it: married 2016-06-01 is outside on the 2026 assessment day.
        edge = ht.household(dear, {"marriage_registered": True, "marriage_date": "2016-06-01"}, year=2026)
        self.assertFalse(edge["owners"]["self"]["one_home"]["comprehensive"])
        # One jointly held home: one home for the property tax, each spouse on a share for the comprehensive tax.
        joint = ht.household([ht.Home(ref="h", label="h", owner="self", price=Decimal(2_070_000_000), estimated=False,
                                      basis="", shares={"self": Decimal("0.5"), "partner": Decimal("0.5")})],
                             {"marriage_registered": True}, year=2026)
        self.assertTrue(joint["homes"][0]["one_home"])
        self.assertEqual(joint["total"], ht.target(Decimal(2_070_000_000))["joint"])

    def test_an_unknown_price_makes_the_owner_and_the_total_unknown(self):
        found = ht.household([home("h1", "self", 700_000_000), home("h2", "partner", None)], {})
        self.assertIsNone(found["owners"]["partner"]["total"])
        self.assertIsNotNone(found["owners"]["self"]["total"])
        self.assertIsNone(found["total"])

    def test_a_target_bought_jointly_halves_the_comprehensive_tax_base_twice(self):
        price = Decimal(2_070_000_000)
        found = ht.target(price)
        self.assertEqual(found["single_comprehensive"], Decimal(2_537_280))
        # Each half 1.035bn - 0.9bn = 135M x 60% = 81M at 0.5% = 405,000, less 81M x 45% x 0.4% = 145,800; plus 20%.
        self.assertEqual(found["joint_comprehensive"], 2 * (Decimal(259_200) + Decimal(51_840)))
        self.assertEqual(found["single"] - found["joint"], Decimal(2_537_280) - Decimal(622_080))

    def test_the_estimate_is_the_market_price_at_the_realization_rate(self):
        self.assertEqual(ht.estimated_price(Decimal(3) * EOK * 10), Decimal(2_070_000_000))
        self.assertIsNone(ht.estimated_price(None))



class OfficialPriceTests(unittest.TestCase):
    """The V-World client against synthetic responses shaped like its documentation (no live call)."""

    def setUp(self):
        import tempfile
        self.folder = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.folder, ignore_errors=True)

    @staticmethod
    def page(rows, total=None):
        import json
        body = {"apartHousingPrices": {"field": rows, "totalCount": str(len(rows) if total is None else total)}}
        return lambda url: (200, json.dumps(body).encode("utf-8"))

    @staticmethod
    def unit(area, price, year=2026, _numbers=iter(range(1, 10**6))):
        # A unit number of its own each time: the client counts a (dong, ho) once.
        return {"stdrYear": str(year), "dongNm": "101", "hoNm": str(next(_numbers)), "prvuseAr": str(area),
                "pblntfPc": str(price)}

    def test_the_lot_number_is_built_from_the_trade_record(self):
        fields = {"sggCd": "11110", "umdCd": "18700", "landCd": "1", "bonbun": "0060", "bubun": "0000"}
        self.assertEqual(official_price.pnu(fields), "1111018700100600000")
        self.assertEqual(official_price.pnu({**fields, "bonbun": "60"}), "1111018700100600000")
        self.assertIsNone(official_price.pnu({**fields, "landCd": "0"}))
        self.assertIsNone(official_price.pnu({**fields, "umdCd": ""}))

    def test_the_same_area_median_of_the_year_is_kept_with_its_count(self):
        rows = official_price.fetch("1111018700100600000", 2026, "k", get=self.page(
            [self.unit("59.9", 700_000_000), self.unit("59.5", 720_000_000), self.unit("84.9", 990_000_000),
             self.unit("59.9", 650_000_000, year=2025)]))
        self.assertEqual(official_price.median_for(rows, area=Decimal("59.9"), year=2026), (Decimal(710_000_000), 2))
        self.assertEqual(official_price.median_for(rows, area=Decimal("120"), year=2026), (None, 0))
        # A unit listed twice (seen live 2026-11-15) counts once; a corrected row (later update) wins.
        twice = [{"dong": "101", "ho": "1001", "area": Decimal("59.9"), "price": Decimal(700_000_000), "year": 2026,
                  "updated": "2026-04-30"},
                 {"dong": "101", "ho": "1001", "area": Decimal("59.9"), "price": Decimal(700_000_000), "year": 2026,
                  "updated": "2026-04-30"},
                 {"dong": "101", "ho": "1002", "area": Decimal("59.9"), "price": Decimal(800_000_000), "year": 2026,
                  "updated": "2026-04-30"},
                 {"dong": "101", "ho": "1002", "area": Decimal("59.9"), "price": Decimal(760_000_000), "year": 2026,
                  "updated": "2026-05-14"}]
        self.assertEqual(official_price.median_for(twice, area=Decimal("59.9"), year=2026), (Decimal(730_000_000), 2))

    def test_an_error_or_an_empty_answer_fails_visibly(self):
        import json
        error = json.dumps({"response": {"status": "ERROR", "error": {"code": "INCORRECT_KEY"}}}).encode()
        for get, code in ((lambda url: (200, error), "api-INCORRECT_KEY"), (self.page([]), "no-rows"),
                          (lambda url: (503, b""), "http-503"), (lambda url: (200, b"<html>"), "invalid-response")):
            with self.subTest(code=code), self.assertRaises(official_price.OfficialPriceError) as caught:
                official_price.fetch("1111018700100600000", 2026, "k", get=get)
            self.assertEqual(caught.exception.code, code)

    def test_sync_stores_the_price_and_a_later_failure_keeps_it(self):
        from datetime import datetime, timezone
        now = datetime(2026, 11, 15, tzinfo=timezone.utc)
        official_price.remember_lots(self.folder, [{"property_ref": "h1", "deal_date": "2026-05-01",
                                                    "pnu": "1111018700100600000"}])
        specs = {"h1": {"exclusive_area_m2": 59.9}, "h2": {"exclusive_area_m2": 84.9}}
        outcomes = official_price.sync(self.folder, specs, "k", year=2026, now=now,
                                       get=self.page([self.unit("59.9", 700_000_000)]))
        self.assertEqual(outcomes, {"h1": "ok", "h2": "no-lot"})
        store = official_price.load(self.folder)
        self.assertEqual(official_price.price_of(store, "h1", 2026), (Decimal(700_000_000), 1))
        self.assertEqual(official_price.price_of(store, "h1", 2027), (None, 0))
        outcomes = official_price.sync(self.folder, specs, "k", year=2026, now=now, get=lambda url: (500, b""))
        self.assertEqual(outcomes["h1"], "http-500")
        self.assertEqual(official_price.price_of(official_price.load(self.folder), "h1", 2026)[0], Decimal(700_000_000))
        # The holding tax prefers the stored price and names it; without it, the trades around 1 January.
        prop = {"record_id": "h1", "label": "집", "owner": "self", "value": Decimal(1_500_000_000),
                "market": {"monthly": [("2025-12", Decimal(1_000_000_000), 2), ("2026-01", Decimal(1_100_000_000), 2)]}}
        official = ht.homes([prop], {}, official_price.load(self.folder), year=2026)[0]
        self.assertEqual((official.price, official.estimated), (Decimal(700_000_000), False))
        estimated = ht.homes([prop], {}, {"lots": {}, "prices": {}}, year=2026)[0]
        self.assertEqual((estimated.price, estimated.estimated), (Decimal(1_050_000_000) * Decimal("0.69"), True))
        self.assertIn("1월 1일 앞뒤 실거래 4건", estimated.basis)
        unknown = ht.homes([{**prop, "value": None, "market": None}], {}, {"lots": {}, "prices": {}}, year=2026)[0]
        self.assertIsNone(unknown.price)


    def test_a_partial_answer_or_a_row_without_its_fields_keeps_the_stored_price(self):
        # Codex review 2026-11-15: a first page of a stated 2000 and a second page in an unknown shape was stored.
        import json
        first = {"apartHousingPrices": {"field": [self.unit("59.9", 700_000_000)] * official_price.PAGE_ROWS,
                                        "totalCount": "2000"}}
        pages = iter([json.dumps(first).encode(), json.dumps({"unexpected": []}).encode()])
        with self.assertRaises(official_price.OfficialPriceError) as caught:
            official_price.fetch("1111018700100600000", 2026, "k", get=lambda url: (200, next(pages)))
        self.assertEqual(caught.exception.code, "incomplete")
        broken = [self.unit("59.9", 700_000_000), {**self.unit("59.9", 1), "pblntfPc": ""}]
        with self.assertRaises(official_price.OfficialPriceError) as caught:
            official_price.fetch("1111018700100600000", 2026, "k", get=self.page(broken))
        self.assertEqual(caught.exception.code, "invalid-row")

    def test_a_trade_sync_during_the_price_sync_keeps_both_writes(self):
        # Codex review 2026-11-15: the price sync wrote back the store it read before the call, dropping a lot number
        # the trade sync stored meanwhile.
        from datetime import datetime, timezone
        official_price.remember_lots(self.folder, [{"property_ref": "h1", "deal_date": "2026-05-01",
                                                    "pnu": "1111018700100600000"}])
        answer = self.page([self.unit("59.9", 700_000_000)])

        def get(url):
            official_price.remember_lots(self.folder, [{"property_ref": "h2", "deal_date": "2026-06-01",
                                                        "pnu": "1111018700100700000"}])
            return answer(url)

        official_price.sync(self.folder, {"h1": {"exclusive_area_m2": 59.9}}, "k", year=2026,
                            now=datetime(2026, 11, 15, tzinfo=timezone.utc), get=get)
        store = official_price.load(self.folder)
        self.assertEqual(set(store["lots"]), {"h1", "h2"})
        self.assertIn("h1", store["prices"])
        self.assertEqual([p.name for p in self.folder.iterdir() if p.suffix == ".tmp"], [])


class AssessmentDayTests(unittest.TestCase):
    """Codex review 2026-11-15: a home sold in July still owes the year's tax; the report read only today's homes."""

    def test_the_homes_held_on_the_first_of_june_are_the_ones_taxed(self):
        import json
        from datetime import datetime, timezone

        from smith import ledger, report_data
        from smith.importer import parse_import

        conn = ledger.connect(":memory:")
        self.addCleanup(conn.close)

        def apply(import_id, at, status):
            doc = {"schema_version": 1, "import_id": import_id, "source": "manual", "mode": "patch",
                   "as_of": at.isoformat(), "owners": [{"id": "self"}],
                   "records": [{"id": "house", "kind": "asset", "owner_id": "self", "effective_at": at.isoformat(),
                                "revision": 1 if status == "active" else 2, "status": status, "category": "real_estate",
                                "account_type": "none", "currency": "KRW", "value": "1000000000",
                                "valuation_method": "appraisal", "liquidity": "months"}]}
            ledger.apply_import(conn, parse_import(json.dumps(doc)), recorded_at=at)

        apply("bought", datetime(2026, 3, 1, tzinfo=timezone.utc), "active")
        apply("sold", datetime(2026, 7, 15, tzinfo=timezone.utc), "closed")
        august = datetime(2026, 8, 10, 9, tzinfo=timezone.utc)
        found = report_data._holding_tax(conn, [], {}, {}, as_of=august, known_at=august, audience="self")
        self.assertEqual([h["ref"] for h in found["homes"]], ["house"])
        self.assertFalse(found["projected"])
        self.assertIsNotNone(found["total"])
        # A home first recorded after 1 June but bought years before is still taxed this year; one of unknown purchase
        # is counted and says so.
        later = {"record_id": "flat", "label": "flat", "owner": "partner", "value": Decimal(500_000_000), "market": None}
        both = report_data._holding_tax(conn, [later], {"properties": {"flat": {"acquired_year": 2020}}}, {},
                                        as_of=august, known_at=august, audience="self")
        self.assertEqual(sorted(h["ref"] for h in both["homes"]), ["flat", "house"])
        self.assertNotIn("보유한 것으로 계산", next(h for h in both["homes"] if h["ref"] == "flat")["basis"])
        unknown = report_data._holding_tax(conn, [later], {}, {}, as_of=august, known_at=august, audience="self")
        self.assertIn("보유한 것으로 계산", next(h for h in unknown["homes"] if h["ref"] == "flat")["basis"])
        may = datetime(2026, 5, 10, 9, tzinfo=timezone.utc)
        early = report_data._holding_tax(conn, [], {}, {}, as_of=may, known_at=may, audience="self")
        self.assertTrue(early["projected"])
        self.assertEqual(early["homes"], [])


class RenderTests(unittest.TestCase):
    def test_the_tax_day_section_names_each_home_its_bills_and_the_pending_reform(self):
        from smith import report_html
        found = ht.household([home("h1", "self", 700_000_000), home("h2", "partner", 450_000_000)], {})
        page = report_html._holding_tax({"holding_tax": found})
        self.assertIn("집 보유세", page)
        self.assertIn("7월 50만 원", page)
        self.assertIn("두 분 모두 나오지 않습니다", page)
        self.assertIn("국회 심의 중", page)
        self.assertEqual(report_html._holding_tax({"holding_tax": None}), "")



class LoginTests(unittest.TestCase):
    def test_a_key_already_stored_for_another_service_is_refused(self):
        # 2026-11-15: the V-World key typed into the data.go.kr prompt replaced that key and stopped the trade sync.
        import argparse
        import io
        from contextlib import redirect_stdout
        from unittest import mock

        from smith import realestate_command

        stored = {"vworld": "11111111-2222-3333-4444-555555555555", "datagokr": "old", "rone": "r"}
        saved = {}
        with mock.patch.object(realestate_command.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(realestate_command.getpass, "getpass", return_value=stored["vworld"]), \
                mock.patch.object(realestate_command.credentials, "load_api_key", side_effect=stored.get), \
                mock.patch.object(realestate_command.credentials, "save_api_key", side_effect=saved.__setitem__), \
                redirect_stdout(io.StringIO()) as out:
            realestate_command.run(argparse.Namespace(action="login", only="datagokr"))
        self.assertEqual(saved, {})
        self.assertIn("already stored as vworld", out.getvalue())


class DailyNoteTests(unittest.TestCase):
    """The daily mail's tax dates carry the household's bill (user decision 2026-11-15)."""

    def test_each_holding_tax_line_names_its_bill(self):
        from smith import daily

        found = ht.household([home("h1", "self", 700_000_000), home("h2", "partner", 150_000_000)], {})
        self.assertEqual(daily.holding_note("재산세 1기분 납부 시작", 2026, found), "우리 집 약 64만 원")   # 50.4만 + the small home's whole 13.2만
        self.assertEqual(daily.holding_note("재산세 2기분 납부 마감", 2026, found), "우리 집 약 50만 원")
        self.assertEqual(daily.holding_note("종합부동산세 납부 시작", 2026, found), "우리 집은 대상 아님")
        self.assertIsNone(daily.holding_note("재산세 2기분 납부 시작", 2027, found))      # Another year's bill.
        self.assertIsNone(daily.holding_note("자동차세 2기분 납부 시작", 2026, found))
        self.assertIsNone(daily.holding_note("종합부동산세 납부 시작", 2026, None))
        # A small bill comes whole in July; an owing household names the December amount; estimates say so.
        small = ht.household([home("h", "self", 150_000_000)], {})
        self.assertEqual(daily.holding_note("재산세 2기분 납부 시작", 2026, small), "우리 집은 7월에 한 번에 냄")
        dear = ht.household([home("h", "self", 2_070_000_000)], {})
        self.assertEqual(daily.holding_note("종합부동산세 납부 시작", 2026, dear), "우리 집 약 254만 원")
        guessed = ht.household([ht.Home(ref="h", label="h", owner="self", price=Decimal(700_000_000), estimated=True,
                                        basis="")], {})
        self.assertEqual(daily.holding_note("재산세 1기분 납부 시작", 2026, guessed), "우리 집 약 50만 원(추정)")

    def test_the_card_shows_the_note_and_says_what_it_is(self):
        from datetime import date as day

        from smith import daily_html

        card = daily_html._finance({"new_filings": [], "finance_dates": [
            {"date": day(2026, 12, 1), "label": "종합부동산세 납부 시작", "days": 11, "note": "우리 집은 대상 아님"}]})
        self.assertIn("종합부동산세 납부 시작 · D-11 · 우리 집은 대상 아님", card)
        self.assertIn("보유세 금액은 Smith가 계산한 예상액", card)
        plain = daily_html._finance({"new_filings": [], "finance_dates": [
            {"date": day(2026, 11, 26), "label": "한국은행 금통위", "days": 6}]})
        self.assertIn("날짜만 적습니다", plain)


if __name__ == "__main__":
    unittest.main()
