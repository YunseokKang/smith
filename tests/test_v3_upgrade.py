"""The v3.0 upgrade (report design §18, user spec 2026-10-18): the mail's hero names the action and the
balance sheet's composition, sections collapse, opinions are colour pills, the week's change is briefed in two
figures with dates, a zero-balance credit line is liquidity (not a repayment target), the mortgage is judged
against the market average for refinancing, the four pension pipelines stack against the retirement target,
the move-target spread is tracked against both homes, the month splits into fixed and discretionary spending,
and the household day carries curated experiences. Synthetic household; figures checked by hand arithmetic."""
import json
import re
import tempfile
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from smith import editions, evidence, ledger, narrative, pension, proposals, report_html, research, tax
from smith.config import load_config
from smith.importer import parse_import
from smith.records import ImportBatch, ImportRejected, Observation
from smith.report_data import build_report
from smith import delivery, pension_chart
from smith.report_html import render

AT = datetime(2026, 10, 18, tzinfo=timezone.utc)
HOUSEHOLD = {
    "annual_gross_salary": 100_000_000, "marriage_registered": False, "birth_year": 1990,
    "retirement_monthly_spend": 5_500_000, "retirement_age": 60,
    "properties": {"home": {"acquired_year": 2020, "acquired_price": 1_000_000_000},
                   "rental": {"acquired_year": 2019, "acquired_price": 600_000_000}},
    "targets": {"alpha": {"label": "가 단지", "lawd_cd": "11110", "apt_name": "A", "exclusive_area_m2": 84.9,
                            "region": "서울특별시 종로구", "reference_price": 2_500_000_000},
                "beta": {"label": "나 단지", "lawd_cd": "26350", "apt_name": "B", "exclusive_area_m2": 84.9,
                           "region": "부산광역시 해운대구"}},
}


def rec(record_id, kind, owner="self", **fields):
    return {"id": record_id, "kind": kind, "owner_id": owner, "effective_at": AT.isoformat(), "revision": 1,
            "status": "active", **fields}


def flow(record_id, category, amount, direction="outflow", **extra):
    start = extra.pop("start_date", "2024-01-01")
    return rec(record_id, "cashflow", category=category, direction=direction, currency="KRW", amount=amount,
               frequency="monthly", start_date=start, **extra)


def stock(record_id, symbol, quantity, price, cost):
    return rec(record_id, "asset", category="stock", account_type="brokerage", currency="KRW", value=str(quantity * price),
               valuation_method="market", liquidity="days", symbol=symbol, market="KR", quantity=str(quantity),
               unit_price=str(price), average_cost=str(cost), instrument_name=symbol)


RECORDS = [
    rec("cash", "asset", category="cash", account_type="bank", currency="KRW", value="30000000",
        valuation_method="manual", liquidity="immediate"),
    stock("samsung", "005930", 1000, 90_000, 70_000),
    stock("voo", "VOO", 600, 100_000, 90_000),
    rec("home", "asset", category="real_estate", account_type="none", currency="KRW", value="1500000000",
        valuation_method="manual", liquidity="months", occupancy="owner_occupied"),
    rec("rental", "asset", "partner", category="real_estate", account_type="none", currency="KRW", value="1100000000",
        valuation_method="appraisal", liquidity="months", occupancy="leased_out"),
    rec("lease", "liability", "partner", category="lease_deposit_obligation", currency="KRW", outstanding_principal="336000000",
        annual_rate="0", rate_type="fixed", repayment_method="bullet", maturity="2028-08-01", collateral_record_id="rental"),
    rec("mortgage", "liability", category="mortgage", currency="KRW", outstanding_principal="240000000",
        annual_rate="0.0352", rate_type="variable", repayment_method="equal_payment", maturity="2061-05-01",
        collateral_record_id="home"),
    rec("credit-line", "liability", category="credit_line", currency="KRW", outstanding_principal="0", annual_rate="0.0549",
        rate_type="variable", repayment_method="revolving", credit_limit="30000000", maturity="2027-10-16"),
    rec("irp", "asset", category="fund", account_type="irp", currency="KRW", value="20000000", valuation_method="statement",
        liquidity="restricted"),
    rec("pension-savings", "asset", category="fund", account_type="pension_savings", currency="KRW", value="30000000",
        valuation_method="statement", liquidity="restricted"),
    rec("annuity", "asset", category="insurance_surrender_value", account_type="insurance", currency="KRW", value="40000000",
        valuation_method="statement", liquidity="restricted"),
    rec("goal", "goal", category="home", currency="KRW", target_amount="3000000000", target_date="2029-10-01", priority="high"),
    flow("salary", "salary", "9000000", "inflow"),
    flow("living", "living_expense", "3000000"),
    flow("repay", "loan_payment", "1200000", liability_record_id="mortgage"),
    flow("insurance", "insurance_premium", "240000"),
    flow("to-irp", "internal_transfer", "500000", target_record_id="irp"),
    flow("to-pension", "internal_transfer", "300000", target_record_id="pension-savings"),
    rec("grant", "cashflow", category="stock_compensation", direction="inflow", currency="KRW", amount="30000000",
        frequency="once", start_date="2027-03-01", certainty="expected", amount_basis="gross", symbol="005930",
        market="KR", quantity="300"),
]


def deal(ref, kind, day, price):
    return {"property_ref": ref, "kind": kind, "deal_date": day, "area": "84.9", "price": price, "monthly_rent": "0",
            "floor": "7", "contract_type": None, "cancelled": False}


RECENT, OLDER = ("2026-08-10", "2026-09-05", "2026-10-01"), ("2025-12-10", "2026-02-05", "2026-03-20")
DEALS = ([deal("rental", "rent", d, "315000000") for d in RECENT]
         + [deal("rental", "trade", d, "1100000000") for d in RECENT] + [deal("rental", "trade", d, "1000000000") for d in OLDER]
         + [deal("home", "trade", d, "1600000000") for d in RECENT] + [deal("home", "trade", d, "1500000000") for d in OLDER]
         + [deal("target:beta", "trade", d, "2800000000") for d in RECENT]
         + [deal("target:beta", "trade", d, "2600000000") for d in OLDER])
CD, BASE, MARKET = "817Y002/010502000", "722Y001/0101000", proposals.MORTGAGE_MARKET_SERIES


class Upgrade(unittest.TestCase):
    def setUp(self):
        self.conn = ledger.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.apply("v1", RECORDS)
        months = {(d["property_ref"], d["kind"], d["deal_date"][:7].replace("-", "")) for d in DEALS}
        ledger.replace_deals(self.conn, DEALS, months=months, properties={"home", "rental", "target:beta", "target:alpha"},
                             fetched_at=AT)
        ledger.store_evidence(self.conn, [("ecos", CD, date(2026, 10, 1), "3.00", None),
                                          ("ecos", BASE, date(2026, 10, 1), "2.50", None),
                                          ("ecos", MARKET, date(2026, 9, 1), "3.95", None)], retrieved_at=AT)

    def apply(self, import_id, records, owners=("self", "partner")):
        doc = {"schema_version": 1, "import_id": import_id, "source": "manual", "mode": "patch", "as_of": AT.isoformat(),
               "owners": [{"id": o} for o in owners], "records": records}
        ledger.apply_import(self.conn, parse_import(json.dumps(doc)), recorded_at=AT)

    def report(self, kind="overview"):
        return build_report(self.conn, as_of=AT, known_at=AT, baseline=None, kind=kind, household=HOUSEHOLD)

    def proposal(self, data, key):
        return next(p for p in data["advice"]["proposals"] if p.key == key)


class PerspectiveTests(Upgrade):
    def test_the_spouse_letter_is_the_household_seen_from_the_spouse(self):
        # Report design §42 (user decisions 2026-11-08): the spouse's own pensions, credit room, overseas gains and
        # proposals; the household figures the same as in the client's letter; the client's policy is not hers.
        self.apply("family", [
            flow("np-partner", "pension_income", "1000000", "inflow", owner="partner", pension_source="national",
                 start_date="2055-03-01"),
            rec("irp-partner", "asset", "partner", category="fund", account_type="irp", currency="KRW", value="10000000",
                valuation_method="statement", liquidity="restricted"),
            flow("salary-partner", "salary", "4000000", "inflow", owner="partner"),
            rec("aapl-partner", "asset", "partner", category="stock", account_type="brokerage", currency="KRW",
                value="3000000", valuation_method="market", liquidity="days", symbol="AAPL", market="US",
                quantity="10", unit_price="300000", average_cost="150000", instrument_name="Apple")])
        household = dict(HOUSEHOLD, partner_birth_year=1990)
        mine = build_report(self.conn, as_of=AT, known_at=AT, baseline=None, kind="tax", household=household)
        hers = build_report(self.conn, as_of=AT, known_at=AT, baseline=None, kind="tax", household=household,
                            audience="partner")
        self.assertEqual(mine["pension_stack"]["total_net"], hers["pension_stack"]["total_net"])
        own = {p["id"] for p in hers["pension_stack"]["pipelines"] if p["owner"] == "self"}
        self.assertEqual(own, {"national", "irp"})                               # Hers, seen as the reader's.
        self.assertEqual(hers["tax_plan"]["irp_room"], tax.PENSION_TOTAL_LIMIT)  # Nothing paid in this year.
        self.assertEqual(hers["tax_plan"]["us_positions"], 1)
        keys = [p.key for p in hers["advice"]["proposals"]]
        self.assertIn("overseas-harvest", keys)
        self.assertNotIn("insurance-review", keys)                               # The client's premiums, not hers.
        self.assertEqual(hers["view"].owner_aliases["partner"], "household_member_1")   # The client, from her side.
        page, own_page = render(hers)[1], render(mine)[1]
        for text in ("나의 세금·연금", "고객님 명의의 계좌·연금·보험만 봅니다", "우리 집 노후", "두 분을 합쳐 봅니다"):
            self.assertIn(text, page)
            self.assertIn(text, own_page)
        self.assertIn("이 레터의 답장 질문에는 아직 답하지 않습니다", page)
        self.assertNotIn("이 레터의 답장 질문에는", own_page)
        # The credit split's share for an account not opened yet is projected from zero, not dropped (Codex review).
        self.apply("savings-only", [rec("ps-partner", "asset", "partner", category="fund", account_type="pension_savings",
                                        currency="KRW", value="0", valuation_method="statement", liquidity="restricted"),
                                    flow("to-ps-partner", "internal_transfer", "900000", owner="partner",
                                         target_record_id="ps-partner")])
        split = build_report(self.conn, as_of=AT, known_at=AT, baseline=None, kind="tax", household=household,
                             audience="partner")["pension_stack"]
        contributions = split["contributions"]
        self.assertEqual((contributions["pension_savings"], contributions["irp"]), (Decimal(500_000), Decimal(250_000)))
        irp = next(p for p in split["pipelines"] if p["id"] == "irp")
        self.assertEqual(irp["monthly_contribution"], Decimal(250_000))
        self.assertIsNotNone(irp["net_monthly"])
        # Her home is the one she owns but does not live in; the client's is the household's residence.
        self.assertIn("고객님 주택은 2년 거주 여부에 따라", page)
        self.assertIn("배우자분 명의 거주 주택을 판다면", page)
        self.assertIn("거주 주택을 지금 판다면", own_page)

    def test_the_markets_day_shows_the_reader_s_own_holdings_then_the_household_s(self):
        # Report design §42: the reader's holdings and proposals, the household's combined holdings with whose they are.
        self.apply("family", [rec("aapl-partner", "asset", "partner", category="stock", account_type="brokerage",
                                  currency="KRW", value="300000000", valuation_method="market", liquidity="days",
                                  symbol="AAPL", market="US", quantity="1000", unit_price="300000", average_cost="150000",
                                  instrument_name="Apple"),
                              stock("voo-partner", "VOO", 10, 100_000, 90_000)])
        self.apply("owner", [dict(rec("voo-p", "asset", "partner", category="stock", account_type="brokerage",
                                      currency="KRW", value="1000000", valuation_method="market", liquidity="days",
                                      symbol="VOO", market="KR", quantity="10", unit_price="100000", average_cost="90000",
                                      instrument_name="VOO"))])
        mine = build_report(self.conn, as_of=AT, known_at=AT, baseline=None, kind="markets", household=HOUSEHOLD)
        hers = build_report(self.conn, as_of=AT, known_at=AT, baseline=None, kind="markets", household=HOUSEHOLD,
                            audience="partner")
        self.assertEqual({p["symbol"] for p in hers["own_holdings"]["positions"]}, {"AAPL", "VOO"})
        self.assertNotIn("005930", {p["symbol"] for p in hers["own_holdings"]["positions"]})
        self.assertEqual(mine["holdings"]["total"], hers["holdings"]["total"])          # One household, either side.
        # Her AAPL is above the single-company limit of her own portfolio: the trim is hers, the client's is his.
        self.assertIn("AAPL", [p["symbol"] for p in (hers["advice"]["concentration"] or {}).get("positions", [])])
        self.assertNotIn("AAPL", [p["symbol"] for p in (mine["advice"]["concentration"] or {}).get("positions", [])])
        page = render(hers)[1]
        order = [page.index(t) for t in ("나의 투자·시장", "보유 종목 점검", "우리 집 투자", "두 분의 보유 종목")]
        self.assertEqual(order, sorted(order))
        self.assertIn("나의 보유 종목 평가액", page)
        self.assertIn("두 분이 함께 가진 종목은 VOO", page)
        # Codex review 2026-11-08: the household-wide opinions are checked again on the reader's own holdings. An add
        # on her AAPL (above her own limit) gives way to the code's trim; her card's gain is hers alone.
        add = {"stance": "add", "reason": "실적이 좋습니다.", "alternative": "", "reconsider": "", "memo": "",
               "condition": "", "principles": [], "links": [], "by_code": False, "owner": "partner"}
        shown = narrative.own_opinions(hers, {"AAPL": add, "VOO": dict(add, stance="hold")})
        self.assertEqual((shown["AAPL"]["stance"], shown["AAPL"]["by_code"]), ("trim", True))
        self.assertEqual(shown["VOO"]["stance"], "hold")
        self.assertNotIn("005930", shown)                                              # The client's holding.
        # A holding both members hold is opined on per member (Codex review 2026-11-08): each letter takes its own.
        keys = {p["key"] for p in mine["narrative_holdings"]["positions"] if p["symbol"] == "VOO"}
        self.assertEqual(keys, {"VOO@self", "VOO@household_member_1"})
        per_member = {"VOO@self": dict(add, stance="hold", reason="고객 쪽", owner="self"), "VOO@household_member_1": dict(
            add, stance="trim", reason="배우자 쪽")}
        # An opinion written on the other member's position never shows under the reader's same plain key.
        self.assertNotIn("VOO", narrative.own_opinions(hers, {"VOO": dict(add, stance="hold", owner="self")}))
        self.assertEqual(narrative.own_opinions(mine, per_member)["VOO"]["reason"], "고객 쪽")
        self.assertEqual(narrative.own_opinions(hers, per_member)["VOO"]["reason"], "배우자 쪽")
        # An opinion put back after failing verification is not shown; a must-opine holding gets the code's instead.
        caveated = {"VOO@household_member_1": dict(add, stance="hold", memo="원장이나 출처에서 확인되지 않은 수치가 있습니다"),
                    "AAPL": dict(add, stance="hold", reason="9876억 원", memo="확인되지 않은 수치")}
        shown = narrative.own_opinions(hers, caveated)
        self.assertNotIn("VOO", shown)
        self.assertEqual((shown["AAPL"]["by_code"], shown["AAPL"]["stance"]), (True, "trim"))
        # Restored for a reason with no memo text (ref codes in the prose): the explicit mark keeps it out too.
        restored, _ = narrative.restore(dict(narrative.with_blocks({}), holdings=[], proposal_notes=[], order=[],
                                             insights=[], watch=[], experiences=[]),
                                        ["holdings[0]:ref-in-prose"],
                                        {"holdings": [{"symbol": "VOO@household_member_1", "stance": "hold",
                                                       "reason": "R1에 따르면 보유액은 9876억 원입니다.", "alternative": "",
                                                       "reconsider": "", "refs": [], "principles": []}]})
        self.assertTrue(restored["holdings"][0]["restored"])
        quiet = {"VOO@household_member_1": dict(add, stance="hold", memo="", restored=True)}
        self.assertNotIn("VOO", narrative.own_opinions(hers, quiet))
        own = report_html._own(hers)["sectors"]["securities"]
        self.assertEqual(own["unrealized_gain"], sum((p["unrealized_gain"] for p in hers["own_holdings"]["positions"]),
                                                     Decimal(0)))
        self.assertIsNone(own["hermes"])

    def test_household_facts_swap_and_proposal_history_is_kept_apart(self):
        from smith import perspective
        household = {"birth_year": 1989, "birth_month": 8, "partner_birth_year": 1992, "occupational_plan": "db",
                     "retirement_monthly_spend": 1, "annual_gross_salary": 9}
        seen = perspective.household_for(household, "partner")
        self.assertEqual((seen["birth_year"], seen.get("birth_month"), seen["partner_birth_year"], seen["partner_birth_month"]),
                         (1992, None, 1989, 8))
        self.assertEqual((seen.get("occupational_plan"), seen["partner_occupational_plan"]), (None, "db"))
        self.assertEqual((seen.get("annual_gross_salary"), seen["retirement_monthly_spend"]), (None, 1))
        self.assertIs(perspective.household_for(household, "self"), household)
        active = {"overseas-harvest": {"status": "open"}, "partner:overseas-harvest": {"status": "declined"}}
        self.assertEqual(perspective.proposals_for(active, "self"), {"overseas-harvest": {"status": "open"}})
        self.assertEqual(perspective.proposals_for(active, "partner"), {"overseas-harvest": {"status": "declined"}})
        shown = perspective.scoped([{"key": "isa-open", "identity": "isa-open", "renewed": True, "figures": {}}], "partner")
        self.assertEqual((shown[0]["key"], shown[0]["identity"]), ("partner:isa-open", "partner:isa-open"))


class LayoutTests(Upgrade):
    def test_the_hero_names_the_action_and_the_composition_and_the_mail_ends_short(self):
        data = self.report("overview")
        page = render(data)[1]
        first = data["advice"]["proposals"][0]
        # The banner is a short badge and the first card is the only place the full title appears (user review
        # 2026-10-18: one sentence repeated three times on the first screen); no "one thing" box under the tiles.
        self.assertIn("⚡ 금주 핵심 액션: " + report_html.escape(first.headline), page)
        self.assertEqual(page.count(report_html.escape(first.title)), 1)
        for box in ("오늘의 한 가지", "가장 중요한 한 가지"):
            self.assertNotIn(box, page)
        for p in data["advice"]["proposals"]:
            with self.subTest(p.key):
                self.assertTrue(0 < len(p.headline) <= 32 and not p.headline.endswith("."), p.headline)
        self.assertNotIn("판정 사유는 맨 아래", page)
        # The composition bar under the judgement: the balance sheet by kind, property split by owner. The
        # legend items are divided by a bar so they never run together, even as plain text.
        self.assertIn("자산의 구성", page)
        self.assertIn("부동산", page)
        self.assertIn("본인 15.0억 원 · 배우자 11.0억 원", page)
        legend = re.sub(r"<[^>]+>", "", page.split("자산의 구성")[1].split("순자산")[0])
        self.assertRegex(legend, r"● \S+ \d+%.*\s\|\s+●")
        # Body ink is near-black; the grey inks of the design reference are gone.
        self.assertIn("#111111", page)
        for grey in ("#5b616e", "#7c828a", "#a8acb3"):
            self.assertNotIn(f"color:{grey}", page)
        # The long tails are out of the mail: system notes and the principles' source list.
        for text in ("데이터 상태 및 시스템 노트", "이번 보고에 쓴 원칙과 출처", "상태 판정:"):
            self.assertNotIn(text, page)
        self.assertIn("Smith는 조회 전용 자문입니다", page)
        self.assertIn("세금·법령은 2026년 기준으로 계산했으니 실행 전 현행 법령을 확인하십시오", page)   # Said once, here.
        self.assertNotIn("현행 법령 확인 필요", page)
        self.assertNotIn("현행 법령 확인이 필요", page)
        notes = report_html.system_notes(data)
        self.assertTrue(any(n.startswith("상태 판정: ") for n in notes) and any(n.startswith("가정: ") for n in notes))

    def test_sections_collapse_and_a_theme_opens_its_lead_view_before_the_proposals(self):
        overview = render(self.report("overview"))[1]
        self.assertGreaterEqual(overview.count("<details style"), 3)
        self.assertNotIn("<details open", overview.split("고객님께 드리는 제안")[1].split("함께 검토한")[-1])
        self.assertIn("background:#f8f9fa;padding:16px 20px;border-radius:16px;", overview)
        self.assertIn("▼ 펼쳐서 상세 보기", overview)
        # A phone reads the home scenarios as cards and the gauge label on its own line (user review 2026-11-02).
        self.assertEqual(overview.count("시나리오 · "), 3)
        self.assertNotIn(">시나리오</th>", overview)
        self.assertNotIn('white-space:nowrap">주택 목표 준비율', overview)
        # Gmail drops <details> and shows every card open: the hint is hidden inline and only the stylesheet,
        # inside a real <details>, shows it (client report 2026-11-02).
        hints = re.findall(r'<span class="mk-hint[^"]*" style="([^"]*)"', overview)
        self.assertTrue(hints and all(style.startswith("display:none;") for style in hints))
        self.assertEqual(len(hints), overview.count("▼ 펼쳐서 상세 보기"))
        self.assertIn("details>summary .mk-hint{display:inline!important}", overview)
        rates = render(self.report("rates"))[1]
        self.assertIn("<details open style", rates)
        self.assertLess(rates.index("부채·금리 현황 및 금리 변동 민감도"), rates.index("고객님께 드리는 제안"))
        # The tax day is a per-person letter (report design §42, user decision 2026-11-08): the reader's own pensions
        # and proposals first, then the household's pension stack.
        tax_page = render(self.report("tax"))[1]
        order = [tax_page.index(text) for text in ("나의 세금·연금", "나의 연금", "고객님께 드리는 제안", "우리 집 노후",
                                                   "노후 현금흐름 준비율")]
        self.assertEqual(order, sorted(order))
        self.assertIn("노후 준비율(연금 세후)", tax_page)

    def test_opinions_and_policy_checks_are_colour_pills(self):
        data = self.report("markets")
        data["narrative"] = {"status": {"outcome": "success", "dropped": []}, "situation": "", "direction": "", "portfolio": "",
                             "insights": [], "watch": [], "experiences": [],
                             "holdings": {"005930": {"stance": "trim", "reason": "비중이 큽니다.", "alternative": "", "condition": "",
                                                     "reconsider": "비중이 내려오면.", "principles": [], "links": [], "memo": ""},
                                          "VOO": {"stance": "add", "reason": "코어입니다.", "alternative": "", "condition": "",
                                                  "reconsider": "", "principles": [], "links": [], "memo": ""}}}
        page = render(data)[1]
        self.assertIn("background:#fef7e0;color:#b06000;font-size:12px;font-weight:700", page)   # 일부 정리
        self.assertIn("background:#e6f4ea;color:#137333;font-size:12px;font-weight:700", page)   # 추가 매수 고려
        self.assertTrue("background:#fce8e6;color:#c5221f" in page or "background:#eef0f3;color:#3c4043" in page)  # checks
        self.assertIn("보유 종목 구성", page)


class CardTests(Upgrade):
    def test_tranches_of_one_payment_are_one_timeline_row_and_cards_open_one_at_a_time(self):
        tranches = [rec(f"opi-{part}", "cashflow", category="bonus", direction="inflow", currency="KRW", amount="90000000",
                        frequency="once", start_date="2027-04-10", available_from=usable, certainty="expected",
                        amount_basis="gross", symbol="005930", market="KR")
                    for part, usable in (("a", "2027-04-10"), ("b", "2028-04-10"), ("c", "2029-04-10"))]
        self.apply("v2", tranches)
        data = self.report("overview")
        rows = [t for t in data["timeline"] if t["kind"] == "inflow" and t["date"] == date(2027, 4, 10)]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["count"], rows[0]["gross"]), (3, Decimal(270_000_000)))
        self.assertEqual(sorted(rows[0]["usable"]), [date(2028, 4, 10), date(2029, 4, 10)])
        page = render(data)[1]
        self.assertIn("3회 분할, 일부는 2028.04 · 2029.04부터 사용 가능", page)
        self.assertEqual(page.count("2027.04.10"), 1)
        # Proposal and strategy cards: the first proposal open, the rest closed until tapped.
        self.assertIn("<details open style", page.split("고객님께 드리는 제안")[1])
        self.assertGreaterEqual(page.count("펼쳐서 상세 보기"), 3)
        self.assertIn("함께 검토한 다른 제안", page)


class ChangeTests(unittest.TestCase):
    def test_the_week_is_briefed_in_two_figures_with_dates(self):
        t0, t1 = datetime(2026, 10, 10, tzinfo=timezone.utc), datetime(2026, 10, 17, tzinfo=timezone.utc)
        conn = ledger.connect(":memory:")
        self.addCleanup(conn.close)

        def position(revision, at, price):
            return {"id": "us", "kind": "asset", "owner_id": "self", "effective_at": at.isoformat(), "revision": revision,
                    "status": "active", "category": "stock", "account_type": "brokerage", "currency": "USD",
                    "value": str(10 * price), "valuation_method": "market", "liquidity": "days", "symbol": "XYZ",
                    "market": "US", "quantity": "10", "unit_price": str(price), "average_cost": "90"}
        for import_id, at, records in (("a", t0, [position(1, t0, 100)]),
                                       ("b", t1, [position(2, t1, 80), rec("new-cash", "asset", category="cash", account_type="bank",
                                                                            currency="KRW", value="5000000", valuation_method="manual",
                                                                            liquidity="immediate", effective_at=t1.isoformat())])):
            doc = {"schema_version": 1, "import_id": import_id, "source": "manual", "mode": "patch", "as_of": at.isoformat(),
                   "owners": [{"id": "self"}], "records": records}
            ledger.apply_import(conn, parse_import(json.dumps(doc)), recorded_at=at)
            ledger.apply_import(conn, ImportBatch(f"fx-{import_id}", "toss", "snapshot", at, ("self",), (), "x",
                                                  (Observation("USD", "fx_mid_rate", "KRW", "1300", at),)), recorded_at=at)
        data = build_report(conn, as_of=t1, known_at=t1, baseline=t0, kind="overview")
        change = data["change"]
        self.assertEqual(change["groups"], {"market_and_valuation": Decimal(-260_000), "net_inflow": Decimal(5_000_000)})
        self.assertEqual((change["from"], change["to"]), (date(2026, 10, 10), date(2026, 10, 17)))
        page = render(data)[1]
        self.assertIn("10월 10일 → 10월 17일", page)
        self.assertIn("시장·평가 변동은 -26만 원", page)
        self.assertIn("입출금·원장 등록은 +500만 원", page)
        self.assertIn("시세로는 <b>26만 원</b>이 줄었습니다", page)          # The loss is not averaged away.
        self.assertIn("10/10 대비 시장·평가 -26만 원 · 입출금 +500만 원", page)  # The net-worth tile says so too.
        payload = narrative.build_payload(data["view"], data, None)
        self.assertEqual(payload["change_since_last_report"]["market_and_valuation"], "-260000")
        self.assertEqual(payload["change_since_last_report"]["from"], "2026-10-10")


class LoanTests(Upgrade):
    def test_a_zero_balance_credit_line_is_liquidity_not_a_repayment_target(self):
        data = self.report("rates")
        self.assertEqual(self.proposal(data, "prepayment").figures["loan_rate"], Decimal("0.0352"))  # Not the 5.49% line.
        debt = data["sectors"]["debt"]
        self.assertEqual(debt["unused_credit"], Decimal(30_000_000))
        page = render(data)[1]
        self.assertIn("유동성 방어에 긍정적", page)
        self.assertIn("3,000만 원", page)
        self.assertNotIn("마이너스통장(본인) 원금을 미리 갚는", page)
        # Nor is its maturity a money date on the timeline (review 2026-10-18: "가장 가까운 큰 자금 일정은 … 0원").
        self.assertNotIn("credit_line", [t.get("category") for t in data["timeline"]])
        metrics = narrative.sector_metrics(data["view"], data)
        self.assertEqual(metrics["debt"]["unused_credit_lines"], [{"limit": "30000000", "drawn": "0"}])

    def test_the_mortgage_is_judged_against_the_market_average_for_refinancing(self):
        data = self.report("rates")
        keep = self.proposal(data, "refinance")
        self.assertEqual((keep.identity, keep.priority), ("refinance:keep", 3))
        self.assertIn("대환 없이 유지", keep.title)
        self.assertEqual(data["advice"]["refinance"]["verdict"], "keep")
        page = render(data)[1]
        self.assertIn("대환 없이 유지 유리", page)
        self.assertIn("3.95%", page)
        # A later month with a much lower market rate: the saving against the fee, and the months to earn it back.
        ledger.store_evidence(self.conn, [("ecos", MARKET, date(2026, 10, 1), "3.20", None)], retrieved_at=AT)
        data = self.report("rates")
        review = self.proposal(data, "refinance")
        self.assertEqual(review.priority, 2)
        self.assertEqual(review.figures["yearly_saving"], Decimal(240_000_000) * (Decimal("0.0352") - Decimal("0.0320")))
        facts = data["advice"]["refinance"]
        self.assertEqual(facts["fee"], Decimal(240_000_000) * proposals.PREPAYMENT_FEE_RATE)
        self.assertEqual(round(facts["break_even_months"]), 45)                              # 2.88M fee / 64,000 a month.
        self.assertIn("대환 검토 권유", render(data)[1])

    def test_the_monthly_series_is_fetched_by_month_and_may_be_absent(self):
        monthly = json.dumps({"StatisticSearch": {"row": [{"TIME": "202609", "DATA_VALUE": "3.95"}]}}).encode()
        daily = json.dumps({"StatisticSearch": {"row": [{"TIME": "20261001", "DATA_VALUE": "2.5"}]}}).encode()
        seen = []

        def get(url):
            seen.append(url)
            if "121Y006" in url:
                return 200, monthly
            return 200, daily
        points = evidence.fetch_provider("ecos", "key", start=date(2026, 1, 1), end=date(2026, 10, 18), get=get)
        self.assertIn(("ecos", MARKET, date(2026, 9, 1), "3.95", None), points)
        self.assertTrue(any("/121Y006/M/202601/202610/BECBLA0302" in url for url in seen))
        empty = json.dumps({"RESULT": {"CODE": "INFO-200", "MESSAGE": "none"}}).encode()
        points = evidence.fetch_provider("ecos", "key", start=date(2026, 1, 1), end=date(2026, 10, 18),
                                         get=lambda url: (200, empty) if "121Y006" in url else (200, daily))
        self.assertFalse(any(p[1] == MARKET for p in points))                     # Optional: absent, not an error.
        self.assertEqual(self.report("rates")["advice"]["refinance"]["verdict"], "keep")


class PensionTests(Upgrade):
    def test_the_four_pipelines_stack_against_the_target_and_the_missing_one_is_asked_for(self):
        data = self.report("tax")
        stack = data["pension_stack"]
        # IRP 50만 + pension savings 30만: the credit covers 75만 a month, so 5만 is kept apart (an ISA or similar).
        self.assertEqual([p["key"] for p in stack["pipelines"]], list(pension.PIPELINES) + ["isa"])
        national, annuity, savings, irp, isa = stack["pipelines"]
        self.assertEqual((irp["monthly_contribution"], isa["monthly_contribution"]), (Decimal(450_000), Decimal(50_000)))
        self.assertEqual((national["basis"], national["net_monthly"], national["start_age"]), ("unknown", None, 65))
        growth = (1 + tax.REAL_RETURN) ** 24                                      # 36 -> 60.
        projected = Decimal(30_000_000) * growth + Decimal(300_000) * 12 * (growth - 1) / tax.REAL_RETURN
        # Paid over the portal's 20 years in equal real instalments (user decision 2026-11-07).
        twenty = tax.REAL_RETURN / (1 - (1 + tax.REAL_RETURN) ** -pension.ACCOUNT_PAYOUT_YEARS)
        self.assertEqual(savings["gross_monthly"], projected * twenty / 12)
        self.assertEqual((savings["start_age"], savings["end_age"]), (60, 80))
        # This year's IRP above the credit (60만) comes back tax-free: the IRP pays tax on a taxable share below one.
        self.assertLess(irp["taxable_share"], 1)
        self.assertEqual(irp["net_monthly"], irp["gross_monthly"] * (1 - pension.PRIVATE_HIGH_TAX * irp["taxable_share"]))
        # Pension savings and IRP together pay out more than the separate-taxation limit a year: the high rate.
        self.assertGreater((savings["gross_monthly"] + irp["gross_monthly"]) * 12, pension.PRIVATE_SEPARATE_LIMIT)
        self.assertTrue(savings["high_private_tax"])
        self.assertEqual(savings["net_monthly"], savings["gross_monthly"] * (1 - pension.PRIVATE_HIGH_TAX))
        self.assertEqual(annuity["gross_monthly"], Decimal(40_000_000) * growth * tax.WITHDRAWAL_RATE / 12)
        self.assertEqual(annuity["net_monthly"], annuity["gross_monthly"])            # Tax-free by assumption.
        self.assertFalse(annuity["premiums_counted"])                                 # 240,000 a month: cover, not savings.
        self.assertEqual(stack["unknown"], ["national"])
        self.assertEqual(stack["total_net"], annuity["net_monthly"] + savings["net_monthly"] + irp["net_monthly"]
                         + isa["net_monthly"])
        # The accounts end at 80; the annuity and the ISA draw go on.
        self.assertEqual((stack["later_age"], stack["later_net"]), (80, annuity["net_monthly"] + isa["net_monthly"]))
        self.assertEqual([t["retire_age"] for t in stack["tiers"]], [50, 55, 60])
        # The national pension is unknown, not zero: what must be held outside the pensions is only a ceiling.
        self.assertTrue(all(t["outside_need"]["upper_bound"] for t in stack["tiers"]))
        self.assertIn("최대 ", render(data)[1])
        self.assertEqual(stack["ratio"], stack["total_net"] / Decimal(5_500_000))
        self.assertIn("pension-pipelines", [r.key for r in data["requests"]])
        page = render(data)[1]
        self.assertIn("연금 종류별 스택", page)
        self.assertIn("입력 필요: 국민연금공단", page)
        # The client states the national pension: it is used as entered and the request disappears.
        self.apply("v2", [flow("np", "pension_income", "1500000", "inflow", pension_source="national", start_date="2055-06-01")])
        data = self.report("tax")
        national = data["pension_stack"]["pipelines"][0]
        self.assertEqual((national["basis"], national["net_monthly"]), ("stated", Decimal(1_500_000) * Decimal("0.97")))
        self.assertNotIn("pension-pipelines", [r.key for r in data["requests"]])
        with self.assertRaises(ImportRejected):
            self.apply("v3", [flow("bad", "salary", "1", "inflow", pension_source="national")])
        # A savings-type premium with a payment term: the annuity is projected to maturity against its floor.
        self.apply("v5", [flow("annuity-premium", "insurance_premium", "1000000", start_date="2024-03-05", end_date="2036-02-29")])
        annuity = self.report("tax")["pension_stack"]["pipelines"][1]
        self.assertTrue(annuity["premiums_counted"] and annuity["guarantee_floor_real"] > 0)
        self.assertEqual(annuity["monthly_premium"], Decimal(1_000_000))
        self.assertGreater(annuity["projected_real"], Decimal(40_000_000) * growth)   # Future premiums add to the fund.
        self.assertEqual(annuity["gross_monthly"], max(annuity["projected_real"], annuity["guarantee_floor_real"]) * tax.WITHDRAWAL_RATE / 12)
        self.assertIn("해지하지 않는다고 보고", annuity["basis_text"])
        # The fund's own after-tax figure is used untouched (amount_basis=net).
        self.apply("v4", [dict(flow("np", "pension_income", "1620000", "inflow", pension_source="national", start_date="2054-03-01",
                                    amount_basis="net"), revision=2, effective_at="2026-10-19T00:00:00+00:00")])
        later = datetime(2026, 10, 20, tzinfo=timezone.utc)
        data = build_report(self.conn, as_of=later, known_at=later, baseline=None, kind="tax", household=HOUSEHOLD)
        national = data["pension_stack"]["pipelines"][0]
        self.assertEqual((national["net_monthly"], national["gross_monthly"], national["basis"]),
                         (Decimal(1_620_000), None, "stated"))
        # An insurer's illustration: the nominal amount of the first payment year, deflated to today's won.
        self.apply("v6", [flow("annuity-payout", "pension_income", "1450000", "inflow", pension_source="insurance_annuity",
                               start_date="2055-03-02", price_basis="nominal")])
        annuity = build_report(self.conn, as_of=later, known_at=later, baseline=None, kind="tax",
                               household=HOUSEHOLD)["pension_stack"]["pipelines"][1]
        years = Decimal((2055 - 2026) * 12 + 3 - 10) / 12
        self.assertEqual((annuity["basis"], annuity["nominal_deflated"]), ("stated", True))
        self.assertEqual(annuity["gross_monthly"], Decimal(1_450_000) / (1 + pension.INFLATION) ** years)
        self.assertEqual(annuity["net_monthly"], annuity["gross_monthly"])   # Tax-free by assumption.
        self.assertEqual(annuity["nominal_monthly"], Decimal(1_450_000))
        data = build_report(self.conn, as_of=later, known_at=later, baseline=None, kind="tax", household=HOUSEHOLD)
        page = render(data)[1]
        self.assertIn("2055년 금액 월 145만 원", page)              # In the letter's calculation notes.
        # The product review (FR-24): forward IRR rises with lifespan, the alternative with its return, and the
        # break-even sits where the alternative's withdrawal equals the annuity.
        review = data["annuity_reviews"][0]
        irrs = [b["irr"] for b in review["by_lifespan"]]
        self.assertEqual([b["to_age"] for b in review["by_lifespan"]], [80, 85, 90, 100])
        self.assertTrue(all(irrs[i] < irrs[i + 1] for i in range(len(irrs) - 1)))
        self.assertEqual(review["remaining_months"], 112)                                   # 2026-11 .. 2036-02.
        self.assertEqual(review["paid_so_far"], Decimal(1_000_000) * 32)                     # 2024-03 .. 2026-10.
        monthly = [a["monthly"] for a in review["alternatives"]]
        self.assertTrue(monthly[0] < monthly[1] < monthly[2])
        be = review["break_even_return"]
        self.assertTrue(Decimal("0.04") < be < Decimal("0.08") or review["verdict"] in ("strong_keep", "surrender_candidate"))
        self.assertIn(review["verdict"], ("strong_keep", "keep_as_floor", "surrender_candidate"))
        self.assertIn("보유 연금보험 평가", page)
        self.assertIn("매몰비용", page)
        metrics = narrative.sector_metrics(data["view"], data)["pension_insurance"]["annuity_reviews"][0]
        self.assertEqual((metrics["verdict"], metrics["policy"]), (review["verdict"], "연금보험 1"))
        with self.assertRaises(ImportRejected):
            self.apply("v7", [flow("bad2", "salary", "1", "inflow", price_basis="nominal")])


    def test_the_partner_pensions_join_the_household_stack_against_one_target(self):
        # User decision 2026-11-08: the spouse's pensions are added to the client's, both retiring at the same age.
        alone = self.report("tax")["pension_stack"]
        self.apply("family", [
            flow("np-partner", "pension_income", "1000000", "inflow", owner="partner", pension_source="national",
                 start_date="2055-03-01"),
            rec("irp-partner", "asset", "partner", category="fund", account_type="irp", currency="KRW", value="10000000",
                valuation_method="statement", liquidity="restricted"),
            flow("salary-partner", "salary", "4000000", "inflow", owner="partner")])
        household = dict(HOUSEHOLD, partner_birth_year=1990)
        data = build_report(self.conn, as_of=AT, known_at=AT, baseline=None, kind="tax", household=household)
        stack = data["pension_stack"]
        self.assertEqual([m["label"] for m in stack["owners"]], ["본인", "배우자"])
        self.assertEqual([m["age"] for m in stack["owners"]], [36, 36])
        # The client's rows keep their ids; the partner's are suffixed, so charts and refs never collide. The partner
        # gets the national pension and only the accounts they hold: no unknown row for a product they do not have.
        self.assertEqual([p["id"] for p in stack["pipelines"]],
                         list(pension.PIPELINES) + ["isa", "national@partner", "irp@partner"])
        rows = {p["id"]: p for p in stack["pipelines"]}
        self.assertEqual((rows["national@partner"]["net_monthly"], rows["national@partner"]["start_age"]),
                         (Decimal(1_000_000) * Decimal("0.97"), 65))
        self.assertEqual(pension.row_label(rows["national@partner"]), "국민연금(배우자)")
        irp = rows["irp@partner"]
        # No transfer into the partner's IRP is recorded: the balance alone grows to the payout age, and it says so.
        self.assertEqual((irp["basis"], irp["monthly_contribution"]), ("estimated", Decimal(0)))
        self.assertIn("지금 잔액만", irp["basis_text"])
        twenty = tax.REAL_RETURN / (1 - (1 + tax.REAL_RETURN) ** -pension.ACCOUNT_PAYOUT_YEARS)
        self.assertEqual(irp["gross_monthly"], Decimal(10_000_000) * (1 + tax.REAL_RETURN) ** 24 * twenty / 12)
        # Tax is each member's own: the partner's IRP alone stays under the separate-taxation limit even though
        # the client's accounts pass it.
        self.assertFalse(irp["high_private_tax"])
        self.assertTrue(rows["pension_savings"]["high_private_tax"])
        self.assertEqual(stack["total_net"], alone["total_net"] + rows["national@partner"]["net_monthly"] + irp["net_monthly"])
        self.assertEqual(stack["unknown"], ["national"])                                 # The client's own statement.
        by_age = {r["age"]: r for r in stack["timeline"]}
        self.assertEqual(by_age[65]["parts"]["national@partner"], Decimal(970_000) * 12)
        self.assertEqual(by_age[64]["parts"]["national@partner"], 0)
        self.assertIn("member-pension-contributions", [r.key for r in data["requests"]])
        page = render(data)[1]
        for text in ("국민연금(배우자)", "두 분 합산", "배우자의 연금도 같은 나이에"):
            self.assertIn(text, page)
        metrics = narrative.sector_metrics(data["view"], data)["pension_insurance"]["pension_stack"]
        self.assertEqual([m["owner"] for m in metrics["members"]], ["self", "household_member_1"])
        self.assertIn("national@household_member_1", [p["ref"] for p in metrics["pipelines"]])
        self.assertNotIn("partner", json.dumps(metrics, ensure_ascii=False))
        # Neither an ended transfer nor one that starts later is a contribution now (Codex review 2026-11-08): the
        # request stays and the IRP is still projected from its balance until a running transfer exists.
        self.apply("ended", [flow("to-irp-partner-old", "internal_transfer", "200000", owner="partner",
                                  target_record_id="irp-partner", end_date="2025-12-31"),
                             flow("to-irp-partner-later", "internal_transfer", "200000", owner="partner",
                                  target_record_id="irp-partner", start_date="2040-01-01")])
        data = build_report(self.conn, as_of=AT, known_at=AT, baseline=None, kind="tax", household=household)
        self.assertIn("member-pension-contributions", [r.key for r in data["requests"]])
        later = next(p for p in data["pension_stack"]["pipelines"] if p["id"] == "irp@partner")
        self.assertEqual(later["monthly_contribution"], Decimal(0))
        # The client's own credit split reads transfers the same way (Codex review 2026-11-08): an IRP transfer that
        # starts in 2040 is not split as if it were paid from today.
        self.apply("later-self", [flow("to-irp-later", "internal_transfer", "3000000", target_record_id="irp",
                                       start_date="2040-01-01")])
        data = build_report(self.conn, as_of=AT, known_at=AT, baseline=None, kind="tax", household=household)
        self.assertEqual((data["tax_plan"]["irp_monthly"], data["pension_stack"]["contributions"]["isa"]),
                         (Decimal(500_000), Decimal(50_000)))
        self.apply("live", [flow("to-irp-partner", "internal_transfer", "200000", owner="partner", target_record_id="irp-partner")])
        data = build_report(self.conn, as_of=AT, known_at=AT, baseline=None, kind="tax", household=household)
        self.assertNotIn("member-pension-contributions", [r.key for r in data["requests"]])
        irp = next(p for p in data["pension_stack"]["pipelines"] if p["id"] == "irp@partner")
        self.assertEqual(irp["monthly_contribution"], Decimal(200_000))

    def test_a_spouse_pension_without_a_birth_year_is_unknown_not_silently_left_out(self):
        # Codex review 2026-11-08: with no partner_birth_year the spouse's stated pension has no age on the household
        # axis; it must make the total a lower bound and ask for the birth year, not vanish from a "complete" total.
        self.apply("family", [flow("np-partner", "pension_income", "1000000", "inflow", owner="partner",
                                   pension_source="national", start_date="2060-03-01")])
        data = self.report("tax")                                         # HOUSEHOLD has no partner_birth_year.
        stack = data["pension_stack"]
        national = next(p for p in stack["pipelines"] if p["id"] == "national@partner")
        self.assertEqual((national["net_monthly"], national["basis"]), (None, "unknown"))
        self.assertIn("partner_birth_year", national["missing"])
        self.assertIn("national@partner", stack["unknown"])
        self.assertFalse(stack["complete"])
        request = next(r for r in data["requests"] if r.key == "pension-pipelines")
        self.assertIn("국민연금(배우자)", request.title)
        # Another member's ledger id is free text and may be a name: it never reaches the model, the spouse keeps
        # alias 1 (the prompts call it the spouse), and that member has no birth-year setting to name (Codex review).
        self.apply("other", [flow("np-other", "pension_income", "500000", "inflow", owner="kim-minsu",
                                  pension_source="national", start_date="2060-01-01")],
                   owners=("self", "partner", "kim-minsu"))
        data = build_report(self.conn, as_of=AT, known_at=AT, baseline=None, kind="tax",
                            household=dict(HOUSEHOLD, partner_birth_year=1990))
        self.assertEqual(data["view"].owner_aliases["partner"], "household_member_1")
        metrics = narrative.sector_metrics(data["view"], data)["pension_insurance"]["pension_stack"]
        other = next(p for p in metrics["pipelines"] if p["owner"] == "household_member_2")
        self.assertEqual(other["missing"], "가구 구성원 출생 연도(로컬 설정)")
        self.assertNotIn("kim-minsu", json.dumps(metrics, ensure_ascii=False))

    def test_without_the_client_birth_year_a_spouse_pension_is_unknown_and_an_unconverted_account_does_not_fail(self):
        # Codex review 2026-11-08: (1) no client birth year means no household axis; the spouse's pension must not be
        # added as if both were the same age. (2) An IRP contribution in a currency without a rate made the other
        # account's tax-free share fail the whole report.
        self.apply("family", [
            flow("np-partner", "pension_income", "1000000", "inflow", owner="partner", pension_source="national",
                 start_date="2060-03-01"),
            rec("ps-partner", "asset", "partner", category="fund", account_type="pension_savings", currency="KRW",
                value="10000000", valuation_method="statement", liquidity="restricted"),
            flow("to-ps-partner", "internal_transfer", "300000", owner="partner", target_record_id="ps-partner"),
            rec("irp-usd-partner", "asset", "partner", category="fund", account_type="irp", currency="USD", value="1000",
                valuation_method="statement", liquidity="restricted"),
            dict(flow("to-irp-usd", "internal_transfer", "100", owner="partner", target_record_id="irp-usd-partner"),
                 currency="USD")])
        no_client_birth = {k: v for k, v in HOUSEHOLD.items() if k != "birth_year"}
        stack = build_report(self.conn, as_of=AT, known_at=AT, baseline=None, kind="tax",
                             household=dict(no_client_birth, partner_birth_year=1995))["pension_stack"]
        national = next(p for p in stack["pipelines"] if p["id"] == "national@partner")
        self.assertEqual((national["net_monthly"], national["basis"]), (None, "unknown"))
        self.assertIn("birth_year", national["missing"])
        self.assertFalse(stack["complete"])
        # With both birth years the report builds: the USD IRP is unknown (no rate), the KRW pension savings is not.
        stack = build_report(self.conn, as_of=AT, known_at=AT, baseline=None, kind="tax",
                             household=dict(HOUSEHOLD, partner_birth_year=1995))["pension_stack"]
        rows = {p["id"]: p for p in stack["pipelines"]}
        self.assertIsNone(rows["irp@partner"]["net_monthly"])
        self.assertIsNotNone(rows["pension_savings@partner"]["net_monthly"])
        self.assertEqual(rows["pension_savings@partner"]["monthly_contribution"], Decimal(300_000))

    def test_a_spouse_born_in_another_year_is_added_in_the_same_calendar_year(self):
        # Codex review 2026-11-08: a spouse five years younger gets their pension at their 65 (2060), which is the
        # client's 70th year; the household chart and totals must add it from 70, not from the client's 65.
        self.apply("family", [
            flow("np-partner", "pension_income", "1000000", "inflow", owner="partner", pension_source="national",
                 start_date="2060-03-01"),
            rec("irp-partner", "asset", "partner", category="fund", account_type="irp", currency="KRW", value="10000000",
                valuation_method="statement", liquidity="restricted")])
        household = dict(HOUSEHOLD, partner_birth_year=1995)
        stack = build_report(self.conn, as_of=AT, known_at=AT, baseline=None, kind="tax", household=household)["pension_stack"]
        rows = {p["id"]: p for p in stack["pipelines"]}
        national, irp = rows["national@partner"], rows["irp@partner"]
        self.assertEqual((national["own_start_age"], national["start_age"], national["age_offset"]), (65, 70, 5))
        # The spouse's IRP pays from their 60 to 80 (the retirement age, 60): the client's 65 to 85.
        self.assertEqual((irp["own_start_age"], irp["own_end_age"], irp["start_age"], irp["end_age"]), (60, 80, 65, 85))
        by_age = {r["age"]: r for r in stack["timeline"]}
        self.assertEqual(by_age[69]["parts"]["national@partner"], 0)
        self.assertEqual(by_age[70]["parts"]["national@partner"], Decimal(970_000) * 12)
        self.assertEqual((by_age[64]["parts"]["irp@partner"], by_age[65]["parts"]["irp@partner"] > 0), (0, True))
        self.assertEqual(stack["tiers"][-1]["all_on_age"], 70)                       # Every pension paying: 2060.
        page = render(build_report(self.conn, as_of=AT, known_at=AT, baseline=None, kind="tax", household=household))[1]
        self.assertIn("2060.03 65세~종신", page)                                      # The table keeps the spouse's own age.
        self.assertIn("배우자 연금은 같은 해에 맞춰", page)

    def test_the_age_chart_stacks_each_pipeline_from_its_start_to_its_end(self):
        data = self.report("tax")
        stack = data["pension_stack"]
        rows = {r["age"]: r for r in stack["timeline"]}
        self.assertEqual((min(rows), max(rows)), (55, 90))
        _, annuity, savings, irp, isa = stack["pipelines"]
        self.assertEqual(rows[59]["total"], 0)                         # Everything known starts at 60.
        self.assertEqual(rows[60]["parts"], {"insurance_annuity": annuity["net_monthly"] * 12,
                                             "pension_savings": savings["net_monthly"] * 12,
                                             "irp": irp["net_monthly"] * 12, "isa": isa["net_monthly"] * 12})
        self.assertAlmostEqual(rows[79]["total"], stack["total_net"] * 12, places=6)
        self.assertAlmostEqual(rows[80]["total"], stack["later_net"] * 12, places=6)  # The 20-year accounts ended.
        self.assertEqual([p["category"] for p in stack["pipelines"]],
                         ["public", "tax_free", "personal", "occupational", "personal"])
        page = render(data)[1]
        self.assertIn("60세 예상 연금 수령액", page)
        self.assertIn("계산 기준", page)                                 # Details moved to the letter's end.
        self.assertNotIn("정부 예시", page)
        self.assertIn("가입 없음(주택연금·농지연금)", page)
        # Two stated annuity records with their own starts: the later one counts only from its age.
        self.apply("v2", [flow("a1", "pension_income", "1000000", "inflow", pension_source="insurance_annuity",
                               start_date="2050-01-01"),
                          flow("a2", "pension_income", "2000000", "inflow", pension_source="insurance_annuity",
                               start_date="2060-01-01")])
        stack = self.report("tax")["pension_stack"]
        rows = {r["age"]: r for r in stack["timeline"]}
        self.assertEqual(rows[69]["parts"]["insurance_annuity"], Decimal(12_000_000))
        self.assertEqual(rows[70]["parts"]["insurance_annuity"], Decimal(36_000_000))
        # An insurer's fixed won amount shrinks in today's won once it is paid.
        self.apply("v3", [dict(flow("a2", "pension_income", "2000000", "inflow", pension_source="insurance_annuity",
                                    start_date="2060-01-01", price_basis="nominal"), revision=2,
                               effective_at="2026-10-19T00:00:00+00:00")])
        later = datetime(2026, 10, 20, tzinfo=timezone.utc)
        stack = build_report(self.conn, as_of=later, known_at=later, baseline=None, kind="tax",
                             household=HOUSEHOLD)["pension_stack"]
        rows = {r["age"]: r for r in stack["timeline"]}
        fixed = [rows[age]["parts"]["insurance_annuity"] - Decimal(12_000_000) for age in (70, 71)]
        self.assertAlmostEqual(fixed[1], fixed[0] / (1 + pension.INFLATION), places=6)

    def test_an_account_follows_its_stated_terms_and_an_employer_db_plan_is_asked_for(self):
        terms = dict(institution="예시증권", opened_on="2016-05-10", contribution_end="2040-03-21",
                     payout_start="2050-03-01", payout_end="2065-03-01", illustrated_annual_pension="9000000")
        changed = "2026-10-19T00:00:00+00:00"
        self.apply("t1", [dict(RECORDS[9], revision=2, effective_at=changed, **terms)])   # The pension-savings account.
        household = dict(HOUSEHOLD, occupational_plan="db", occupational_institution="예시생명")
        later = datetime(2026, 10, 20, tzinfo=timezone.utc)
        data = build_report(self.conn, as_of=later, known_at=later, baseline=None, kind="tax", household=household)
        stack = data["pension_stack"]
        savings = next(p for p in stack["pipelines"] if p["key"] == "pension_savings")
        self.assertEqual((savings["payout_years"], savings["end_age"]), (15, 75))   # 60 + 15.
        self.assertIn("50세(납입 종료일)까지", savings["basis_text"])              # 2040-03 is age 50 for 1990.
        self.assertEqual(savings["products"][0]["illustrated_annual"], Decimal(9_000_000))
        db = next(p for p in stack["pipelines"] if p["key"] == "occupational")
        self.assertEqual((db["category"], db["net_monthly"], db["label"]), ("occupational", None, "퇴직연금(DB형)"))
        self.assertIn("occupational", stack["unknown"])
        page = render(data)[1]
        for text in ("예시증권", "예시생명 DB형 퇴직연금", "입력 필요: 회사 인사팀"):   # Product names only in the table.
            self.assertIn(text, page)
        for text in ("2016.05 가입", "납입 ~2040.03", "정부 예시 연 900만 원"):
            self.assertNotIn(text, page)
        # The hero bar reads the same age as the total (a fixed-term pension may have ended by then).
        _, segments, _ = report_html._hero_segments(data, "tax")
        self.assertAlmostEqual(sum(v for label, v, _ in segments if label != "목표까지 부족"), stack["total_net"], places=6)
        # A contribution end alone is a term; a second account with another period: the longest, said so.
        self.apply("t4", [dict(RECORDS[8], revision=2, effective_at=changed, contribution_end="2030-01-01"),
                          rec("savings-2", "asset", category="fund", account_type="pension_savings", currency="KRW",
                              value="1000000", valuation_method="statement", liquidity="restricted",
                              effective_at=changed, payout_start="2050-03-01", payout_end="2075-03-01")])
        data = build_report(self.conn, as_of=later, known_at=later, baseline=None, kind="tax", household=household)
        pipes = {p["key"]: p for p in data["pension_stack"]["pipelines"]}
        self.assertIn("40세(납입 종료일)까지", pipes["irp"]["basis_text"])
        self.assertEqual(pipes["pension_savings"]["payout_years"], 25)
        self.assertIn("가장 긴 기간", pipes["pension_savings"]["basis_text"])
        # A fixed annuity's erosion counts in the total after the accounts end, as in the chart.
        self.apply("t5", [flow("np2", "pension_income", "1200000", "inflow", pension_source="insurance_annuity",
                               start_date="2055-01-01", price_basis="nominal", effective_at=changed)])
        stack = build_report(self.conn, as_of=later, known_at=later, baseline=None, kind="tax",
                             household=household)["pension_stack"]
        rows = {r["age"]: r for r in stack["timeline"]}
        self.assertAlmostEqual(stack["later_net"] * 12, rows[stack["later_age"]]["total"], places=6)
        with self.assertRaises(ImportRejected):
            self.apply("t2", [dict(RECORDS[0], revision=2, effective_at=changed, institution="예시은행")])   # A bank account has no terms.
        with self.assertRaises(ImportRejected):
            self.apply("t3", [dict(RECORDS[9], revision=3, effective_at=changed, payout_start="2050-03-01",
                                   payout_end="2049-03-01")])

    def test_each_stated_part_keeps_its_own_start_and_end_in_the_totals(self):
        # 1990: 100만 from 60 until the end of 2059 (age 69), 200만 from 70 for life, both after tax.
        self.apply("s1", [flow("p1", "pension_income", "1000000", "inflow", pension_source="other", amount_basis="net",
                               start_date="2050-01-01", end_date="2059-12-31"),
                          flow("p2", "pension_income", "2000000", "inflow", pension_source="other", amount_basis="net",
                               start_date="2060-01-01")])
        tier = next(t for t in self.report("tax")["pension_stack"]["tiers"] if t["retire_age"] == 60)
        other = next(p for p in tier["pipelines"] if p["key"] == "other")
        self.assertEqual(tier["all_on_age"], 70)
        for age, expected in ((60, 1_000_000), (69, 1_000_000), (70, 2_000_000), (90, 2_000_000)):
            self.assertAlmostEqual(pension.monthly_at(other, age), Decimal(expected), places=6)

    def test_a_fixed_annuity_that_falls_behind_the_target_later_still_needs_money_outside(self):
        rows = [{"key": "insurance_annuity", "net_monthly": Decimal(2_000_000), "start_age": 60, "end_age": None,
                 "start_shares": [(60, Decimal(1), 60, None)]}]
        need = pension._outside_need(rows, Decimal(1_500_000), 60)
        self.assertGreater(need["total"], 0)       # 200만 fixed covers 150만 at 60 but not after about 74.

    def test_a_db_plan_pays_a_month_of_average_wage_per_year_of_service_from_an_irp(self):
        household = dict(HOUSEHOLD, occupational_plan="db", occupational_institution="예시생명",
                         occupational_average_wage=10_000_000, occupational_service_start=date(2015, 3, 9))
        data = build_report(self.conn, as_of=AT, known_at=AT, baseline=None, kind="tax", household=household)
        db = next(p for p in data["pension_stack"]["pipelines"] if p["key"] == "occupational")
        self.assertEqual(db["service_months"], 417)                     # 2015-03-09 .. 2050-01-01 (60 for 1990).
        self.assertEqual(db["severance"], Decimal(10_000_000) * 417 / 12)
        twenty = tax.REAL_RETURN / (1 - (1 + tax.REAL_RETURN) ** -pension.ACCOUNT_PAYOUT_YEARS)
        self.assertEqual(db["gross_monthly"], db["severance"] * twenty / 12)
        self.assertEqual((db["start_age"], db["end_age"]), (60, 80))
        # Severance-funded: retirement-income tax, outside the private-pension limit that raises the others' rate.
        self.assertEqual(db["net_monthly"], db["gross_monthly"] * (1 - pension.TAX_FACTORS["occupational"]))
        self.assertEqual(db["products"][0]["now_estimate"], Decimal(10_000_000) * 139 / 12)   # To 2026-10-18.
        early = next(t for t in data["pension_stack"]["tiers"] if t["retire_age"] == 50)
        db50 = next(p for p in early["pipelines"] if p["key"] == "occupational")
        self.assertEqual(db50["start_age"], 55)                          # Waits in the IRP until 55, growing.
        page = render(data)[1]
        self.assertIn("예시생명 DB형 퇴직연금", page)

    def test_the_letter_draws_the_charts_as_pngs_and_the_mail_carries_them_inline(self):
        data = self.report("tax")
        self.assertEqual(len(delivery.canonical_digest(data)), 64)           # The lineage digest takes the stack.
        page = render(data)[1]
        # The donut and the age bars, once for the reader's own pensions and once for the household's (user request
        # 2026-11-08).
        self.assertEqual(page.count('src="data:image/png;base64,'), 4)
        self.assertLess(page.index("고객님 명의, 세후"), page.index("우리 집 노후"))
        html, images = delivery.inline_images(page)
        self.assertEqual(sorted(images), ["chart-1", "chart-2", "chart-3", "chart-4"])
        # A long legend name is cut to its room, never run into the figures (user review 2026-11-08).
        png = pension_chart.donut([("IRP(개인형 퇴직연금)(배우자) " * 3, Decimal(1_000_000), "#0a8f5c"),
                                   ("국민연금", Decimal(2_000_000), "#0052ff")], center="월 25만 원", caption="65세 세후")
        self.assertEqual(png[:4], bytes([0x89]) + b"PNG")
        self.assertNotIn("data:image/png", html)
        self.assertIn('src="cid:chart-1"', html)
        self.assertTrue(all(png[:4] == bytes([0x89]) + b"PNG" for png in images.values()))

    def test_review_round_three_tiny_bars_tax_per_age_and_a_running_annuity(self):
        # A payout of a thousand won beside a large one still draws.
        rows = [{"age": a, "parts": {"national": Decimal(12_000_000), "irp": Decimal(12_000)},
                 "total": Decimal(12_012_000)} for a in range(55, 91)]
        png = pension_chart.age_bars(rows, [("national", "#0052ff"), ("irp", "#0a8f5c")], retire_age=55, focus_age=65)
        self.assertEqual(png[:4], bytes([0x89]) + b"PNG")
        # Two private pensions over the limit together: the high rate only while both pay.
        rows = [{"key": "pension_savings", "gross_monthly": Decimal(1_000_000), "taxable_share": Decimal(1),
                 "start_age": 55, "end_age": 65, "net_monthly": None},
                {"key": "irp", "gross_monthly": Decimal(1_000_000), "taxable_share": Decimal(1),
                 "start_age": 55, "end_age": 75, "net_monthly": None}]
        pension._apply_tax(rows)
        irp = rows[1]
        self.assertEqual(pension.monthly_at(irp, 60), Decimal(1_000_000) * (1 - pension.PRIVATE_HIGH_TAX))
        self.assertEqual(pension.monthly_at(irp, 65), Decimal(1_000_000) * (1 - pension.TAX_FACTORS["irp"]))
        # A fixed won annuity already paying is not deflated for the years before today.
        self.apply("r3", [flow("old", "pension_income", "1000000", "inflow", pension_source="other", amount_basis="net",
                               start_date="2020-01-01", price_basis="nominal")])
        other = next(p for p in self.report("tax")["pension_stack"]["pipelines"] if p["key"] == "other")
        self.assertEqual(pension.monthly_at(other, 36), Decimal(1_000_000))          # 1990 client, 2026.
        self.assertEqual(pension.monthly_at(other, 37), Decimal(1_000_000) / (1 + pension.INFLATION))

    def test_a_birth_month_gives_the_age_in_full_years(self):
        for birth, when, age in [((1991, 7), date(2057, 1, 3), 65), ((1991, None), date(2057, 1, 3), 66),
                                 ((1991, 7), date(2056, 7, 1), 65), ((1991, 7), date(2056, 6, 30), 64)]:
            with self.subTest(birth=birth, when=when):
                self.assertEqual(pension.age_on(birth, when), age)


class FinancialIncomeTests(unittest.TestCase):
    def test_the_threshold_balance_and_the_expected_pay_are_weighed_against_the_financial_income_line(self):
        g = tax.financial_income_guard(Decimal(100_000_000), Decimal("0.04"), Decimal(300_000_000))
        self.assertEqual((g["threshold_balance"], g["interest_now"], g["interest_with_pay"]),
                         (Decimal(500_000_000), Decimal(4_000_000), Decimal(16_000_000)))
        self.assertEqual((g["share_now"], g["share_with_pay"]), (Decimal("0.2"), Decimal("0.8")))
        text = tax._guard_text([("self", g)])
        self.assertTrue(text.startswith("본인 명의로는 3년 안에 받을 성과급"))  # The mail shows the first sentence.
        self.assertIn("예금에 약 5.0억 원을 넘게 두지 말고", text)
        self.assertIn("연 약 1,600만 원(기준의 80%)", text)
        # Unknown is never zero: no rate, no figures; an unknown balance leaves the interest unknown.
        self.assertIsNone(tax.financial_income_guard(Decimal(1), None, None))
        unknown = tax.financial_income_guard(None, Decimal("0.03"), Decimal(1))
        self.assertEqual((unknown["interest_now"], unknown["interest_with_pay"]), (None, None))


class FinancialIncomePerPersonTests(Upgrade):
    def test_each_person_is_weighed_on_their_own_money(self):
        # Review 2026-10-18 (P1): the threshold is per person. The partner's deposit neither raises the client's
        # figure nor, when it cannot be converted, blanks it.
        self.apply("v2", [rec("partner-deposit", "asset", "partner", category="deposit", account_type="bank", currency="KRW",
                               value="900000000", valuation_method="manual", liquidity="days"),
                          rec("partner-usd", "asset", "partner", category="cash", account_type="bank", currency="JPY",
                               value="1000", valuation_method="manual", liquidity="immediate")])
        data = self.report("tax")
        note = next(n for n in data["advice"]["tax_notes"] if n.title.startswith("금융소득종합과세"))
        self.assertTrue(note.body.startswith("본인 명의로"))
        self.assertNotIn("9.3억", note.body)                                   # Never a household total.
        self.assertIn("본인 명의 현금·예금 3,000만 원의 이자", note.body)
        # Each letter carries its reader's own threshold (report design §42): the spouse's figures are in hers.
        self.assertNotIn("배우자 명의", note.body)
        hers = build_report(self.conn, as_of=AT, known_at=AT, baseline=None, kind="tax", household=HOUSEHOLD,
                            audience="partner")
        note = next(n for n in hers["advice"]["tax_notes"] if n.title.startswith("금융소득종합과세"))
        self.assertIn("본인 명의 현금·예금 가운데 원화로 환산하지 못한 금액", note.body)


class FirstReviewTests(unittest.TestCase):
    """The first review of the v3.1 bundle (2026-10-18): each finding reproduced on synthetic input."""

    def test_a_report_that_could_not_judge_never_says_there_is_nothing_to_do(self):
        pending = proposals.Undetermined("집중 종목 정리", "환산하지 못한 금액이 있습니다.")
        advice = {"proposals": [], "undetermined": [pending], "strategy": [], "assumptions": []}
        header = report_html._header({"kind": "markets", "as_of": datetime(2026, 10, 20, 6, tzinfo=timezone.utc)},
                                     action="", pending=1)
        cards = report_html._proposals(advice, True, edition=editions.of("markets"))
        for page in (header, cards):
            self.assertNotIn("새로 실행하실 일은 없습니다", page)
            self.assertNotIn("제안이 없는 것도 판단입니다", page)
        self.assertIn("판단을 보류한 것이 1건", header)
        self.assertIn("판단을 보류한 것이 1건", cards)

    def test_a_lead_is_cut_only_outside_the_markup(self):
        lead = "<b>가. 나. 다</b>입니다. 둘째 문장입니다. 셋째 문장입니다."
        self.assertEqual(report_html._brief(lead), "<b>가. 나. 다</b>입니다. 둘째 문장입니다.")
        self.assertEqual(report_html._brief('<a href="https://x.y/a">a. b</a>. 끝.', 1), '<a href="https://x.y/a">a. b</a>.')

    def test_a_headline_has_a_bound_whatever_the_holding_is_called(self):
        long = "가" * 100 + " 초과분 1차 리밸런싱(분기 822만 원)"
        self.assertLessEqual(len(proposals.fit_headline(long)), proposals.HEADLINE_MAX)
        self.assertTrue(proposals.fit_headline(long).endswith("…"))
        self.assertEqual(proposals.fit_headline("짧은 헤드라인"), "짧은 헤드라인")

    def test_identifiers_and_urls_are_never_shortened_into_another_value(self):
        from smith import headless
        schema = {"type": "object", "additionalProperties": False, "required": ["symbol", "refs", "text"],
                  "properties": {"symbol": {"type": "string", "maxLength": 5},
                                 "refs": {"type": "array", "items": {"type": "string", "maxLength": 4}},
                                 "text": {"type": "string", "maxLength": 12}}}
        pruned = headless.prune({"symbol": "TOOLONG", "refs": ["R1", "R1234567"], "text": "첫 문장입니다. 둘째 문장입니다."}, schema)
        self.assertEqual(pruned["refs"], ["R1"])                            # An over-long ref is dropped, not cut.
        self.assertEqual(pruned["symbol"], "TOOLONG")                       # Left for validation to reject ...
        self.assertEqual(headless.validate(pruned, schema, "output"), ["output.symbol"])
        self.assertEqual(pruned["text"], "첫 문장입니다.")                     # ... while prose is still clipped.

    def test_an_answer_says_when_the_code_or_config_moved_since_the_report(self):
        from smith import qa
        from smith.report_command import lineage
        now = lineage(Path("config/does-not-exist.toml"))
        self.assertRegex(now["code_version"] or "", r"\+src:[0-9a-f]{12}$")  # A working tree is told apart from HEAD.
        sent = {"code_version": "e1cc24f", "config_sha256": "abc"}
        self.assertEqual(qa.drift(sent, {"code_version": "e1cc24f", "config_sha256": "abc"}), [])
        self.assertEqual(qa.drift(sent, {"code_version": "e1cc24f+src:0123456789ab", "config_sha256": "abc"}), ["code"])
        self.assertEqual(qa.drift({"code_version": None, "config_sha256": None}, sent), ["code", "config"])
        at = datetime(2026, 10, 18, tzinfo=timezone.utc)
        self.assertIn("계산 코드와 설정으로 다시 계산했습니다", qa.drift_memo(["code", "config"], at))
        self.assertIn("계산 코드로 다시", qa.drift_memo(["code"], at))
        self.assertIsNone(qa.drift_memo([], at))

    def test_the_precedent_names_its_vintage(self):
        from smith import stress
        self.assertEqual(stress.PRECEDENT["seoul_apartments_vintage"], "2023년 2월 공표치")


class MailRequestTests(Upgrade):
    """The client's e-mail requests of 2026-10-19."""

    def test_a_planned_sale_returns_the_deposit_and_is_the_default(self):
        household = {**HOUSEHOLD, "properties": {**HOUSEHOLD["properties"],
                                                 "rental": {**HOUSEHOLD["properties"]["rental"], "lease_exit": "sell"}}}
        data = build_report(self.conn, as_of=AT, known_at=AT, baseline=None, kind="property", household=household)
        plan = data["lease_plans"][0]
        self.assertEqual((plan["default"], plan["need"]), ("sale", Decimal(0)))       # The sale brings money in.
        self.assertEqual([s.key for s in plan["scenarios"]], ["sale", "relet", "loan"])
        self.assertEqual(plan["stress"]["basis"], "sale")
        proposal = self.proposal(data, "lease-return")
        self.assertIn("매도 대금으로", proposal.title)
        self.assertNotIn("새 세입자 승계가 기본", proposal.title)
        page = render(data)[1]
        self.assertIn("계획하신 대로 만기에 맞춰 집을 팔면", page)
        self.assertIn("만기에 맞춰 매도(계획)", page)
        # The strategy track and the glide plan follow the plan: no own cash is set aside for this deposit.
        track = next(t for t in data["advice"]["strategy"] if t.name == "전세보증금 반환")
        self.assertEqual(track.status, "on_track")
        self.assertLessEqual(len(proposal.headline), proposals.HEADLINE_MAX)
        self.assertFalse(proposal.headline.endswith("…"))
        # Without the setting the re-let path stays the default.
        self.assertEqual(self.report("property")["lease_plans"][0]["default"], "relet")

    def test_an_unpriced_planned_sale_is_unknown_and_the_stress_sale_pays_its_tax(self):
        """Review 2026-10-20: the plan holds when the sale cannot be priced (the need is unknown, never the re-let
        figure), the stress sale subtracts the capital-gains tax at the fallen price, and a shortfall within a year
        is an act-now proposal."""
        from smith import lease
        sale = {"price": Decimal(1_100_000_000), "loans": Decimal(0)}
        acquired = {"acquired_year": 2019, "acquired_price": 600_000_000}
        stressed = lease._stress_sale(Decimal(900_000_000), sale, Decimal("0.3"), acquired, "partner", "leased_out", AT.date())
        self.assertEqual(stressed["basis"], "sale")
        self.assertGreater(stressed["need"], Decimal(900_000_000) - Decimal(770_000_000))     # The tax widens the gap ...
        self.assertEqual(lease._stress_sale(Decimal(900_000_000), sale, Decimal("0.3"), {}, "partner", None, AT.date()),
                         {"need": None, "basis": "unknown", "jeonse_dropped": None})          # ... and without a cost basis, unknown.
        due = date(2027, 8, 1)
        unpriced = {"amount": Decimal(336_000_000), "months": 10, "due": due}
        plan = {"need": None, "sale": {"cash_out": None, "net_inflow": None, "price_basis": "원장 평가액(실거래 3건 미만)",
                                       "basis": "평가액이나 담보대출을 원화로 환산하지 못함"}}
        proposal = proposals._lease_sale(unpriced, plan)
        self.assertIn("남는 금액은 아직 계산하지 못했습니다", proposal.title)
        self.assertEqual((proposal.priority, "need" in proposal.figures), (2, False))
        short = {"need": Decimal(50_000_000), "sale": {"cash_out": Decimal(50_000_000), "net_inflow": None,
                                                        "price_basis": "원장 평가액(실거래 3건 미만)", "basis": "x"}}
        self.assertEqual(proposals._lease_sale(unpriced, short).priority, 1)                 # 10 months, 5천만 원 short.
        self.assertEqual(proposals._lease_sale({**unpriced, "months": 20}, short).priority, 2)

    def test_a_confirmed_residence_makes_the_exemption_the_headline(self):
        """Client e-mail 2026-10-21: the partner's home was bought in 2020 and lived in for two years or more, so
        the one-home exemption (the 12억 excess taxed) is the figure the reports lead with and the general case
        the fallback; without the residence on record the general case still leads."""
        from smith import lease, move_plan
        rental = {**HOUSEHOLD["properties"]["rental"], "lease_exit": "sell", "residence_years": 2}
        household = {**HOUSEHOLD, "properties": {**HOUSEHOLD["properties"], "rental": rental}}
        data = build_report(self.conn, as_of=AT, known_at=AT, baseline=None, kind="property", household=household)
        sale = data["lease_plans"][0]["sale"]
        self.assertEqual((sale["tax_case"], -sale["cash_out"]), ("exempt", sale["net_inflow"]["exempt"]))
        self.assertIn("비과세 기준", sale["basis"])
        proceeds = data["move_plan"]["proceeds"]
        self.assertEqual((proceeds["case"], proceeds["headline"]), ("exempt", proceeds["exempt"]))   # Both homes known.
        self.assertEqual({h["tax_case"] for h in data["move_plan"]["homes"]}, {"exempt"})
        self.assertEqual(data["kpis"]["home_progress"]["after_tax"]["case"], "exempt")
        proposal = self.proposal(data, "lease-return")
        self.assertIn("비과세 기준", proposal.why)
        self.assertIn("비과세 추정", proposal.certainty)
        page = render(data)[1]
        self.assertIn("1세대 1주택 비과세 기준의 양도세를 뺀 값, 요건을 못 갖추면 일반 과세로 약", page)
        self.assertFalse(any(r.key == "residence" for r in data["requests"]))
        # Without the residence on record: the general case leads, the client is asked for the years.
        before = self.report("property")
        self.assertEqual(before["lease_plans"][0]["sale"]["tax_case"], "general")
        self.assertEqual(before["move_plan"]["proceeds"]["case"], "mixed")     # The client's own home alone: mixed.
        self.assertIn("거주 기간이 확인되지 않아 일반 과세", render(before)[1])
        self.assertTrue(any(r.key == "residence" for r in before["requests"]))
        taxes = lease._capital_gains(Decimal(1_100_000_000), {"acquired_year": 2025, "acquired_price": 1_000_000_000,
                                                            "residence_years": 2}, "partner", "leased_out", AT.date())
        self.assertEqual(taxes["headline"], "general")                        # Held under two years: not yet.
        self.assertEqual(move_plan._proceeds([])["case"], None)

    def test_the_lease_exit_setting_is_validated(self):
        from smith.realestate import RealEstateError, validate_properties
        spec = {"lawd_cd": "11110", "apt_name": "A", "exclusive_area_m2": 59.9}
        self.assertTrue(validate_properties({"p": dict(spec, lease_exit="sell")}))
        self.assertTrue(validate_properties({"p": dict(spec, residence_years=2)}))
        with self.assertRaises(RealEstateError):
            validate_properties({"p": dict(spec, lease_exit="keep")})
        with self.assertRaises(RealEstateError):
            validate_properties({"p": dict(spec, residence_years="2")})

    def test_the_changes_are_an_open_band_in_large_type(self):
        story = {"insights": [{"title": "금리 동결", "implication": "변동금리 이자는 그대로입니다.", "links": []}], "watch": []}
        band = report_html._changes_outside(story)
        self.assertNotIn("<details", band)
        self.assertIn("font-size:19px", band)
        self.assertIn("놓치지 마실 변화", band)

    def test_an_answer_never_promises_a_change_smith_cannot_make(self):
        from smith import qa
        for text, promised in (("다음 보고서부터 2028년 8월 기준으로 다시 계산해 드리겠습니다.", True),
                               ("더 크고 눈에 띄게 배치하도록 반영해 두었습니다.", True),
                               ("목표 시점을 2028년 8월로 수정했습니다.", True),
                               ("그렇게 조치하겠습니다.", True),
                               ("말씀하신 내용은 기억해 두었고, Smith를 업데이트하면 계산에 반영됩니다.", False),
                               ("원하시면 매도 순서별 세금을 계산해 드리겠습니다.", False)):
            with self.subTest(text):
                self.assertEqual(qa.promises_change(text), promised)
        # Fail closed (review 2026-10-20): a promise the repair left is replaced by the one true sentence.
        self.assertEqual(qa.neutralize_promises("좋은 점을 짚어 주셨습니다. 다음 보고서부터 그렇게 계산해 드리겠습니다. "
                                                "배치도 반영해 두었습니다. 지금 수치로는 95%입니다."),
                         f"좋은 점을 짚어 주셨습니다. {qa.SAFE_SENTENCE} 지금 수치로는 95%입니다.")
        self.assertIn("warm, kind", qa.SYSTEM_PROMPT)
        self.assertIn("You cannot change Smith", qa.SYSTEM_PROMPT)


class StressWordingTests(unittest.TestCase):
    def test_the_extreme_check_leads_with_its_whole_result_not_the_cash_gap(self):
        # User review 2026-10-18: the cash-only months alone read as unrealistic alarm. Synthetic figures.
        st = {"assumptions": {"rate_shock": Decimal("0.015"), "income_stop_months": 12, "property_drop": Decimal("0.20"),
                              "stock_drop": Decimal("0.30")},
              "monthly": {"burn": Decimal(5_000_000)}, "months_cash": Decimal("2.7"), "covers_scenario": False,
              "after_sales": Decimal(100_000_000), "months_with_sales": Decimal(12), "immediate": Decimal(13_500_000),
              "cash_after_scenario": None, "lease": None, "flags": [], "verdict": "fragile"}
        text = report_html._stress_lead(st, detail=True)
        self.assertIn("드문 극단을 가정해도", text)
        self.assertLess(text.index("30% 내린 값에 팔면 12개월을 버티고"), text.index("현금만으로는"))
        self.assertIn("비상금이 먼저", text)
        self.assertEqual(report_html.STRESS_VERDICT["fragile"][1], "현금만으로 6개월 미만")   # Not "취약합니다".


class MoveTests(Upgrade):
    def test_the_spread_to_each_target_is_tracked_against_both_homes(self):
        data = self.report("property")
        plan = data["move_plan"]
        home, rental = plan["homes"]
        self.assertEqual((home["price"], home["price_basis"]), (Decimal(1_600_000_000), "최근 6개월 실거래 3건 중간값"))
        self.assertEqual(home["before_tax"], Decimal(1_600_000_000) - Decimal(240_000_000))
        self.assertEqual(rental["before_tax"], Decimal(1_100_000_000) - Decimal(336_000_000))
        general = sum((h["before_tax"] - h["tax"]["general"] for h in plan["homes"]), Decimal(0))
        self.assertEqual(plan["proceeds"]["general"], general)
        self.assertTrue(plan["proceeds"]["tax_known"] and plan["proceeds"]["exempt"] > general)
        # The client's own home leads with the exemption (held and lived in), the partner's with the general case
        # until its residence is recorded (client e-mail 2026-10-21): a mixed household headline.
        headline = home["before_tax"] - home["tax"]["exempt"] + rental["before_tax"] - rental["tax"]["general"]
        self.assertEqual((home["tax_case"], rental["tax_case"]), ("exempt", "general"))
        self.assertEqual((plan["proceeds"]["case"], plan["proceeds"]["headline"]), ("mixed", headline))
        alpha, beta = plan["targets"]
        self.assertEqual((alpha["price"], alpha["price_basis"], alpha["trend"]),
                         (Decimal(2_500_000_000), "입력하신 참고 가격(분양가·호가)", None))
        cost = Decimal(2_800_000_000) * (1 + Decimal("0.035"))
        self.assertEqual((beta["cost_with_fees"], beta["spread"]), (cost, cost - headline))
        self.assertEqual(beta["change_6m"], Decimal(2_800_000_000) / Decimal(2_600_000_000) - 1)
        # Six months ago both homes and the target traded lower; the trend compares pre-tax spreads at both ends.
        home_then = Decimal(1_500_000_000) - Decimal(240_000_000)
        rental_then = Decimal(1_000_000_000) - Decimal(336_000_000)
        earlier = Decimal(2_600_000_000) * (1 + Decimal("0.035")) - (home_then + rental_then)
        self.assertEqual(beta["spread_6m_ago"], earlier)
        before_tax_now = cost - plan["proceeds"]["before_tax"]
        self.assertEqual(beta["spread_before_tax"], before_tax_now)
        moved = (before_tax_now - earlier) / cost
        self.assertEqual(beta["trend"], "widening" if moved > Decimal("0.02") else "narrowing" if moved < Decimal("-0.02") else "flat")
        self.assertLess(beta["spread_with_pay"], beta["spread_with_pay_adverse"])   # Confirmed pay only closes less.
        self.assertLess(beta["spread_with_pay"], beta["spread"])                 # Expected pay closes part of it.
        self.assertEqual(plan["horizon"], date(2029, 10, 1))
        page = render(data)[1]
        self.assertLess(page.index("목표 단지 vs 보유 주택 2채 시세 스프레드"), page.index("고객님께 드리는 제안"))
        self.assertIn("가 단지", page)
        self.assertIn("입력하신 참고 가격", page)
        # The trend chart: the target with both ends has two bars; the one without a past reads 자료 부족.
        trend = page.split("필요 추가 자금 추이(세전, 6개월 전 → 지금)")[1]
        self.assertIn(report_html._short(earlier), trend)
        self.assertIn("자료 부족", trend)
        self.assertEqual(data["research_regions"], ["부산광역시 해운대구", "서울특별시 종로구"])
        titles = [t.title for t in research.topics(data["view"], "property", data["research_regions"])]
        self.assertIn("서울특별시 종로구 주요 단지 시세", titles)
        metrics = narrative.sector_metrics(data["view"], data)["real_estate"]["move_plan"]
        self.assertEqual(metrics["targets"][1]["target"], "후보 2")                      # Aliases only for the model ...
        self.assertNotIn("나 단지", json.dumps(metrics, ensure_ascii=False))
        self.assertIn("후보 2 · 나 단지", page)                                       # ... names in the mail.
        self.assertEqual(data["sectors"]["real_estate"]["properties"][1]["valuation_method"], "appraisal")
        self.assertIn("외부 평가(KB 시세 등)", page)


class HouseholdTests(Upgrade):
    def test_the_month_splits_into_fixed_and_discretionary_and_the_balance_sheet_into_kinds(self):
        data = self.report("life")
        split = data["spending_split"]
        self.assertEqual((split["fixed"], split["discretionary"], split["transfers"], split["left"]),
                         (Decimal(1_440_000), Decimal(3_000_000), Decimal(800_000), Decimal(3_760_000)))
        groups = {g["key"]: g for g in data["composition"]["groups"]}
        self.assertEqual(groups["real_estate"]["by_owner"], {"self": Decimal(1_500_000_000), "partner": Decimal(1_100_000_000)})
        self.assertEqual((groups["securities"]["value"], groups["pension"]["value"], groups["cash"]["value"]),
                         (Decimal(150_000_000), Decimal(90_000_000), Decimal(30_000_000)))
        page = render(data)[1]
        self.assertIn("이번 달 수입 900만 원의 쓰임", page)
        self.assertIn("고정비", page)
        self.assertIn("재량비", page)

    def test_experiences_need_sources_and_belong_to_the_household_day(self):
        data = self.report("life")
        brief = {"brief_id": "b", "created_at": AT, "reused": False,
                 "brief": {"items": [{"ref": "R1", "topic": "T1", "tier": "secondary", "kind": "fact", "published_on": "2026-10-10",
                                      "headline": "국립현대미술관 기획전", "detail": "10월 말까지 열립니다.", "publisher": "미술관",
                                      "source_title": "전시 안내", "source_url": "https://example.org/show"}], "gaps": []}}
        payload = narrative.build_payload(data["view"], data, brief)
        empty = {"situation": {"text": "", "refs": []}, "direction": {"text": "", "refs": []},
                 "portfolio": {"text": "", "refs": [], "principles": []}, "order": [], "proposal_notes": [], "holdings": [],
                 "insights": [], "watch": []}
        unsourced = dict(empty, experiences=[{"title": "기획전", "body": "국립현대미술관에서 열립니다.", "when": "10월 말까지", "refs": []}])
        _, dropped = narrative.verify(unsourced, payload)
        self.assertIn("experiences[0]:no-source", dropped)
        sourced = dict(empty, experiences=[{"title": "기획전", "body": "국립현대미술관에서 열립니다.", "when": "10월 말까지", "refs": ["R1"]}])
        verified, dropped = narrative.verify(sourced, payload)
        self.assertEqual(dropped, [])
        self.assertEqual(narrative.edition_trim(dict(verified), "markets")["experiences"], [])
        self.assertEqual(len(narrative.edition_trim(dict(verified), "life")["experiences"]), 1)
        narrative.attach(data, {"output": verified, "memos": {}}, brief, {"outcome": "success", "dropped": []})
        self.assertEqual(data["narrative"]["experiences"][0]["links"][0][1], "https://example.org/show")
        page = render(data)[1]
        self.assertIn(report_html.escape("금주의 라이프스타일 & 경험 소비 큐레이션"), page)
        self.assertIn("기획전", page)
        self.assertIn("https://example.org/show", page)


class ReviewFixTests(Upgrade):
    """The first review of this bundle (2026-10-18): each finding reproduced on synthetic input."""

    def annuity(self, monthly, import_id):
        self.apply(import_id, [flow("annuity-premium", "insurance_premium", "1000000", start_date="2024-03-05", end_date="2036-02-29"),
                               flow("annuity-payout", "pension_income", monthly, "inflow", pension_source="insurance_annuity",
                                    start_date="2055-03-02", price_basis="nominal")])
        return self.report("tax")

    def test_the_verdict_follows_the_break_even_and_keeps_its_direction_outside_the_range(self):
        """A high break-even (the alternative must earn a lot to match) keeps; a low one weighs surrender. The
        policy here: 31M surrender value, 110 premiums of 1M left, the annuity 339 months away."""
        from decimal import localcontext
        from smith import product_review as pr
        cases = [(Decimal(20_000_000), None, "strong_keep"),       # The alternative needs about 15% a year.
                 (Decimal(1_000_000_000), "above", "strong_keep"),  # Not even 30% a year catches it.
                 (Decimal(10_000), None, "surrender_candidate"),     # Safe assets match it.
                 (Decimal(1), "below", "surrender_candidate")]       # Even -20% a year pays more.
        for annuity, bound, verdict in cases:
            with self.subTest(annuity=annuity), localcontext() as context:
                context.prec = 28
                break_even, found = pr._break_even(Decimal(31_000_000), Decimal(1_000_000), 110, 339, annuity)
                self.assertEqual((found, pr.verdict_for(break_even, found)), (bound, verdict))
                if bound is None:
                    self.assertTrue(break_even > pr.EQUITY_RETURN if verdict == "strong_keep" else break_even <= pr.BOND_LIKE_RETURN)
                text = "".join(pr.verdict_text({"break_even_return": break_even, "break_even_bound": found, "verdict": verdict}))
                self.assertNotIn("연 %", text)
                self.assertTrue(bound != "above" or "연 30%" in text)

    def test_unrelated_policies_are_never_added_together(self):
        self.apply("p1", [rec("savings-policy", "asset", category="insurance_surrender_value", account_type="insurance",
                              currency="KRW", value="90000000", valuation_method="statement", liquidity="restricted"),
                          flow("annuity-premium", "insurance_premium", "1000000", start_date="2024-03-05", end_date="2036-02-29"),
                          flow("annuity-payout", "pension_income", "1450000", "inflow", pension_source="insurance_annuity",
                               start_date="2055-03-02", price_basis="nominal")])
        data = self.report("tax")
        self.assertEqual(data["annuity_reviews"], [])                        # Two policies, no links: held ...
        self.assertIn("policy-links", [r.key for r in data["requests"]])     # ... and asked about.
        later = datetime(2026, 10, 19, tzinfo=timezone.utc)
        linked = [dict(flow("annuity-premium", "insurance_premium", "1000000", start_date="2024-03-05", end_date="2036-02-29",
                            policy_record_id="annuity"), revision=2, effective_at=later.isoformat()),
                  dict(flow("annuity-payout", "pension_income", "1450000", "inflow", pension_source="insurance_annuity",
                            start_date="2055-03-02", price_basis="nominal", policy_record_id="annuity"),
                       revision=2, effective_at=later.isoformat())]
        self.apply("p2", linked)
        data = build_report(self.conn, as_of=later, known_at=later, baseline=None, kind="tax", household=HOUSEHOLD)
        review = data["annuity_reviews"][0]
        self.assertEqual((review["policy_record_id"], review["surrender_now"]), ("annuity", Decimal(40_000_000)))
        self.assertNotIn("policy-links", [r.key for r in data["requests"]])
        with self.assertRaises(ImportRejected):
            self.apply("p3", [flow("bad", "living_expense", "1", policy_record_id="annuity")])

    def test_a_failed_narrative_is_at_least_watch_with_one_line_at_the_bottom(self):
        data = self.report("life")
        data["narrative"] = {"status": {"outcome": "failure", "error_code": "timeout", "dropped": []}, "situation": "",
                             "direction": "", "portfolio": "", "holdings": {}, "insights": [], "watch": [], "experiences": []}
        status, reasons = report_html._status(data)
        self.assertNotEqual(status, "stable")
        self.assertTrue(any("AI 해석을 만들지 못해" in r for r in reasons))
        page = render(data)[1]
        footer = page.rsplit("Smith는 조회 전용 자문입니다", 1)[0][-700:]
        self.assertIn("▲ 확인 필요: 이번에는 AI 해석을 만들지 못해 계산 결과만으로 작성했습니다", footer)

    def test_a_stale_market_mortgage_rate_settles_nothing(self):
        stale = self.report("rates")
        self.assertEqual(stale["advice"]["refinance"]["verdict"], "keep")    # 09-01 is fresh on 10-18.
        later = datetime(2027, 1, 20, tzinfo=timezone.utc)                    # 141 days after 2026-09-01.
        data = build_report(self.conn, as_of=later, known_at=later, baseline=None, kind="rates", household=HOUSEHOLD)
        self.assertEqual((data["advice"]["refinance"]["verdict"], data["advice"]["refinance"]["stale"]), ("unknown", True))
        pending = [u for u in data["advice"]["undetermined"] if u.key == "refinance"]
        self.assertIn("오래되었습니다", pending[0].missing)

    def test_an_early_retirement_age_is_projected_to_the_earliest_payout_age(self):
        early = dict(HOUSEHOLD, retirement_age=50)
        stack = build_report(self.conn, as_of=AT, known_at=AT, baseline=None, kind="tax", household=early)["pension_stack"]
        savings = next(p for p in stack["pipelines"] if p["key"] == "pension_savings")
        # Contributions stop at retirement (50, 14 years from 36); the balance waits until 55 to pay out.
        paid = (1 + tax.REAL_RETURN) ** 14
        projected = (Decimal(30_000_000) * paid + Decimal(300_000) * 12 * (paid - 1) / tax.REAL_RETURN) * (1 + tax.REAL_RETURN) ** 5
        self.assertEqual((stack["payout_age"], savings["start_age"], stack["retirement_age"]), (55, 55, 50))
        twenty = tax.REAL_RETURN / (1 - (1 + tax.REAL_RETURN) ** -pension.ACCOUNT_PAYOUT_YEARS)
        self.assertEqual(savings["gross_monthly"], projected * twenty / 12)

    def test_each_retirement_age_scales_the_national_pension_and_counts_the_years_without_a_pension(self):
        # The service's estimate assumes 420 months until age 60 (2050-08 for a client born in 1990).
        self.apply("np", [flow("np", "pension_income", "1800000", "inflow", pension_source="national", start_date="2055-09-01",
                               amount_basis="net", contribution_months="420", contribution_end="2050-08-31")])
        undecided = {k: v for k, v in HOUSEHOLD.items() if k != "retirement_age"}
        data = build_report(self.conn, as_of=AT, known_at=AT, baseline=None, kind="tax", household=undecided)
        stack = data["pension_stack"]
        self.assertEqual((stack["retirement_age"], stack["retirement_age_set"]), (55, False))   # Default (2026-11-07).
        tiers = {t["retire_age"]: t for t in stack["tiers"]}
        national = {age: t["pipelines"][0]["net_monthly"] for age, t in tiers.items()}
        self.assertEqual(national, {60: Decimal(1_800_000), 55: Decimal(1_800_000) * 360 / 420,
                                    50: Decimal(1_800_000) * 300 / 420})
        # Retiring at 50: nothing until the pension accounts at 55 (the ISA row is savings, not a pension).
        self.assertEqual((tiers[50]["first_income_age"], tiers[50]["years_without_pension"]), (55, 5))
        self.assertEqual(tiers[60]["years_without_pension"], 0)
        # Earlier retirement: less paid in, less national pension, more to hold outside the pensions.
        self.assertLess(tiers[50]["total_net"], tiers[55]["total_net"])
        self.assertLess(tiers[55]["total_net"], tiers[60]["total_net"])
        self.assertGreater(tiers[50]["outside_need"]["total"], tiers[55]["outside_need"]["total"])
        # The first five years after a retirement at 50 are paid entirely from outside the pensions.
        first_years = sum(Decimal(5_500_000) * 12 / (1 + tax.REAL_RETURN) ** y for y in range(5))
        self.assertGreater(tiers[50]["outside_need"]["bridge"], first_years)
        # The e-mail answer flagged the readiness ratio it quoted as unverified (2026-10-23): every tier's ratio,
        # written as a percent, is now grounded in the inputs.
        from smith import narrative
        payload = narrative.build_payload(data["view"], data, None)
        for tier in tiers.values():
            text = f"준비율 {tier['ratio'] * 100:.0f}%"
            with self.subTest(text=text):
                self.assertIsNone(narrative.check_block([text], [], payload))
        self.assertIsNotNone(narrative.check_block(["준비율 99%"], [], payload))
        page = render(data)[1]
        for text in ("50·55·60세 세 경우로 계산", "연금 없는 기간", "5년(55세까지)", "연금 밖에서 준비할 돈", "55세 은퇴 기준"):
            self.assertIn(text, page)
        # Both fields or neither, whole months, pension_income only.
        for bad in ({"contribution_months": "420"}, {"contribution_months": "420.5", "contribution_end": "2050-08-31"},):
            with self.subTest(bad=bad), self.assertRaises(ImportRejected):
                self.apply("bad-np", [flow("np2", "pension_income", "1", "inflow", pension_source="national", **bad)])
        with self.assertRaises(ImportRejected):
            self.apply("bad-np2", [flow("x", "salary", "1", "inflow", contribution_months="1", contribution_end="2050-01-01")])

    def test_the_target_trend_is_flat_when_nothing_moved(self):
        from smith import move_plan
        proceeds = {"before_tax": Decimal(1_000), "general": Decimal(800), "headline": Decimal(800), "case": "general",
                    "exempt": Decimal(900), "six_months_ago": Decimal(1_000),
                    "tax_known": True, "hold": []}
        row = move_plan._target("t", {"label": "X"}, {"estimate": Decimal(3_000), "estimate_basis": 3, "change_6m": Decimal(0)},
                                proceeds, None)
        self.assertEqual((row["trend"], row["trend_hold"]), ("flat", False))   # The tax is not a widening gap.
        # One deal against five is no trend (review 2026-10-18): withheld, with the counts.
        thin = move_plan._target("t", {"label": "X"}, {"estimate": Decimal(3_000), "estimate_basis": 1, "previous_basis": 5,
                                                       "change_6m": None}, proceeds, None)
        self.assertEqual((thin["trend"], thin["trend_hold"], thin["change_hold"]), (None, True, {"recent": 1, "older": 5}))
        # A thin home withholds every target's trend; a complex priced by reference simply has no past.
        homes_thin = dict(proceeds, six_months_ago=None, hold=[{"label": "집", "recent": 1, "older": 2}])
        self.assertTrue(move_plan._target("t", {"label": "X"}, {"estimate": Decimal(3_000), "estimate_basis": 3,
                                                                 "change_6m": Decimal(0)}, homes_thin, None)["trend_hold"])
        listed = move_plan._target("t", {"label": "X", "reference_price": 3_000}, None, homes_thin, None)
        self.assertEqual((listed["trend"], listed["trend_hold"]), (None, False))

    def test_targets_alone_are_a_household_setting(self):
        from smith.report_command import profile
        config = {"advice": {}, "targets": {"t": {"label": "X"}}}
        self.assertEqual(profile(config)["targets"], {"t": {"label": "X"}})


class PayloadTests(Upgrade):
    def test_source_check_records_never_reach_the_model_or_the_outbound_check(self):
        """BUG-2026-003: a body hash whose digit run looks like a phone number rejected the whole narrative."""
        from smith.payload import check_outbound
        item = {"ref": "R1", "topic": "T1", "tier": "official", "kind": "policy", "published_on": "2026-10-10",
                "headline": "주택담보대출 규제 안내", "detail": "스트레스 DSR 3단계 시행.", "publisher": "금융위원회",
                "source_title": "보도자료", "source_url": "https://www.fsc.go.kr/board/01012345678",
                "check": {"status": "matched", "final_url": "https://www.fsc.go.kr/x", "http_status": 200,
                          "sha256": "ab01012345678cd" + "0" * 49, "checked_at": "2026-10-18T04:00:00"}}
        brief = {"brief_id": "b", "created_at": AT, "reused": False, "brief": {"items": [item], "gaps": []}}
        data = self.report("rates")
        payload = narrative.build_payload(data["view"], data, brief)
        check_outbound(json.dumps(payload, ensure_ascii=False), [])          # Passes: no check record is sent.
        self.assertNotIn("check", payload["brief"]["items_untrusted"][0])
        self.assertNotIn("source_url", payload["brief"]["items_untrusted"][0])  # URLs stay out (review 2026-10-18).
        self.assertTrue(all("link" not in a for a in payload["household"]["announcements"]))
        self.assertEqual(payload["brief"]["items_untrusted"][0]["tier"], "official")


class ConfigTests(unittest.TestCase):
    def test_targets_and_the_retirement_age_are_validated(self):
        base = Path("config/smith.example.toml").read_text(encoding="utf-8")
        good = base + ('\n[household]\nretirement_age = 60\n[targets.alpha]\nlabel = "가 단지"\nlawd_cd = "11110"\n'
                       'apt_name = "A"\nexclusive_area_m2 = 84.9\nregion = "서울특별시 종로구"\nreference_price = 2500000000\n')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "smith.toml"
            path.write_text(good, encoding="utf-8")
            config = load_config(path)
            self.assertEqual(config["targets"]["alpha"]["reference_price"], 2_500_000_000)
            for bad, message in ((good.replace('lawd_cd = "11110"', 'lawd_cd = "411"'), "targets.alpha.lawd_cd"),
                                 (good + "foo = 1\n", "targets.alpha.foo"),
                                 (good.replace("retirement_age = 60", "retirement_age = 30"), "retirement_age")):
                path.write_text(bad, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, message):
                    load_config(path)


if __name__ == "__main__":
    unittest.main()
