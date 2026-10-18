"""The world-class PB upgrade (report design §17, user spec 2026-10-17): the reserve filled by an asset swap,
the lease deposit's net outflow by scenario, the household basis for the home goal, the employer stock that
always gets an opinion (and the repair-merge defect that lost it, BUG-2026-002), the insurance review, and
the prose rules (rounded amounts, no internal vocabulary, no alarm banner, system notes in the footer).
Synthetic household; every figure is checked against hand arithmetic."""
import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from smith import ledger, narrative, report_html, stress
from smith.importer import parse_import
from smith.payload import load_view
from smith.report_data import build_report
from smith.report_html import render

AT = datetime(2026, 10, 17, tzinfo=timezone.utc)
HOUSEHOLD = {"annual_gross_salary": 100_000_000, "marriage_registered": False,
             "properties": {"rental": {"acquired_year": 2019, "acquired_price": 600_000_000}}}


def rec(record_id, kind, owner="self", **fields):
    return {"id": record_id, "kind": kind, "owner_id": owner, "effective_at": AT.isoformat(), "revision": 1,
            "status": "active", **fields}


def flow(record_id, category, amount, direction="outflow", owner="self", **extra):
    return rec(record_id, "cashflow", owner, category=category, direction=direction, currency="KRW", amount=amount,
               frequency="monthly", start_date="2024-01-01", **extra)


def stock(record_id, symbol, market, quantity, price, cost, **extra):
    return rec(record_id, "asset", category="stock", account_type="brokerage", currency="KRW", value=str(quantity * price),
               valuation_method="market", liquidity="days", symbol=symbol, market=market, quantity=str(quantity),
               unit_price=str(price), average_cost=str(cost), instrument_name=symbol, **extra)


RECORDS = [
    rec("cash", "asset", category="cash", account_type="bank", currency="KRW", value="10000000",
        valuation_method="manual", liquidity="immediate"),
    stock("samsung", "005930", "KR", 1000, 90_000, 70_000),           # 90M: 52.9% of 170M of securities.
    stock("voo", "VOO", "KR", 600, 100_000, 90_000),                   # 60M (listed here in won for one FX-free fixture).
    stock("smr", "SMR", "KR", 200, 100_000, 120_000),                  # 20M, a loss.
    rec("home", "asset", category="real_estate", account_type="none", currency="KRW", value="1500000000",
        valuation_method="manual", liquidity="months", occupancy="owner_occupied"),
    rec("rental", "asset", "partner", category="real_estate", account_type="none", currency="KRW", value="1100000000",
        valuation_method="manual", liquidity="months", occupancy="leased_out"),
    rec("lease", "liability", "partner", category="lease_deposit_obligation", currency="KRW", outstanding_principal="336000000",
        annual_rate="0", rate_type="fixed", repayment_method="bullet", maturity="2028-08-01", collateral_record_id="rental"),
    rec("goal", "goal", category="home", currency="KRW", target_amount="3000000000", target_date="2029-10-01", priority="high"),
    flow("salary", "salary", "9000000", "inflow"),
    flow("living", "living_expense", "5000000"),
    flow("insurance-savings", "insurance_premium", "1000000"),         # 1.0M a month: possibly savings-type, since 2024.
    flow("insurance-cover", "insurance_premium", "240000"),
    rec("grant", "cashflow", category="stock_compensation", direction="inflow", currency="KRW", amount="30000000",
        frequency="once", start_date="2027-03-01", certainty="expected", amount_basis="gross", symbol="005930",
        market="KR", quantity="300"),
]


def jeonse(day, price="315000000"):
    return {"property_ref": "rental", "kind": "rent", "deal_date": day, "area": "84.9", "price": price,
            "monthly_rent": "0", "floor": "7", "contract_type": None, "cancelled": False}


def trade(day, price="1100000000"):
    return {**jeonse(day, price), "kind": "trade"}


class Upgrade(unittest.TestCase):
    def setUp(self):
        self.conn = ledger.connect(":memory:")
        self.addCleanup(self.conn.close)
        doc = {"schema_version": 1, "import_id": "u1", "source": "manual", "mode": "patch", "as_of": AT.isoformat(),
               "owners": [{"id": "self"}, {"id": "partner"}], "records": RECORDS}
        ledger.apply_import(self.conn, parse_import(json.dumps(doc)), recorded_at=AT)
        deals = [jeonse("2026-08-10"), jeonse("2026-09-05"), jeonse("2026-10-01"),
                 trade("2026-07-15"), trade("2026-08-20"), trade("2026-09-25")]
        ledger.replace_deals(self.conn, deals, months={("rental", k, m) for k in ("rent", "trade")
                                                      for m in ("202607", "202608", "202609", "202610")},
                             properties={"rental"}, fetched_at=AT)

    def report(self, kind="monday"):
        return build_report(self.conn, as_of=AT, known_at=AT, baseline=None, kind=kind, household=HOUSEHOLD)

    def proposal(self, data, key):
        return next(p for p in data["advice"]["proposals"] if p.key == key)


class CalculatorTests(Upgrade):
    def test_the_reserve_gap_is_filled_by_one_asset_swap_not_the_monthly_surplus(self):
        data = self.report("life")
        swap = data["advice"]["reserve_swap"]
        # Target 6 x 5.0M (living only; premiums are outflows too: 6.24M x 6 = 37.44M) minus 10M cash.
        self.assertEqual(swap["gap"], Decimal(37_440_000) - Decimal(10_000_000))
        self.assertEqual(swap["sellable_within_days"], Decimal(170_000_000))        # 90M + 60M + 20M, all within days.
        self.assertTrue(swap["one_step"])                                            # 170M >= 3 x 27.44M.
        self.assertEqual(swap["concentration_excess"], Decimal(90_000_000) - Decimal("0.25") * Decimal(170_000_000))
        reserve = self.proposal(data, "emergency-reserve")
        self.assertIn("한 번에 채우시길", reserve.title)
        self.assertIn("초과분부터 정리하면", reserve.why)                             # 47.5M of excess covers the 27.4M gap.
        self.assertNotIn("매월 여유 자금을 먼저 비상금에", reserve.effect)
        room = data["advice"]["spending_room"]
        self.assertEqual(dict(room["committed"])["비상금 채우기"], Decimal(0))         # Nothing from the surplus.
        self.assertEqual(room["reserve_swap"], swap["gap"])
        page = render(data)[1]
        self.assertIn("일시 충당", page)
        self.assertIn("자산 전체를 보는 방법", page)

    def test_the_lease_deposit_is_a_net_outflow_by_scenario(self):
        data = self.report("property")
        (plan,) = data["lease_plans"]
        relet, sale, loan, st = plan["relet"], plan["sale"], plan["loan"], plan["stress"]
        # Three jeonse contracts at 315M: the household's own cash is the 21M gap plus a 5% buffer (16.8M).
        self.assertEqual((plan["default"], relet["gap"], relet["buffer"]), ("relet", Decimal(21_000_000), Decimal(16_800_000)))
        self.assertEqual(plan["need"], Decimal(37_800_000))
        # Sale: 1,100M median less the 336M deposit, no secured loan; the general-tax case is the headline.
        self.assertEqual((sale["price"], sale["before_tax"]), (Decimal(1_100_000_000), Decimal(764_000_000)))
        self.assertLess(sale["net_inflow"]["general"], sale["net_inflow"]["exempt"])
        self.assertEqual(-sale["cash_out"], sale["net_inflow"]["general"])
        self.assertGreater(-sale["cash_out"], Decimal(500_000_000))
        # The partner has no income on record: the move-out loan is unknown, not zero.
        self.assertIsNone(loan["amount"])
        self.assertIn("소득 기록이 없어", loan["basis"])
        # Stress: the jeonse level falls 20% to 252M, so the deposit needs 84M, not 336M.
        self.assertEqual((st["basis"], st["need"]), ("reverse_jeonse", Decimal(84_000_000)))
        lease = data["stress"]["lease"]
        self.assertEqual((lease["need"], lease["basis"]), (Decimal(84_000_000), "reverse_jeonse"))
        # Twelve months without the only salary burn 6.24M a month: 10M cash + 119M stressed sales - 74.88M
        # leaves 54.12M, short of the 84M gap (but nowhere near the old test against the whole 336M).
        self.assertEqual((lease["available"], lease["covered"]), (Decimal(54_120_000), False))
        self.assertEqual(data["stress"]["flags"], [stress.LEASE_UNCOVERED])
        self.assertIn("메우지 못하는 차액 8,400만 원", render(self.report("life"))[1])
        proposal = self.proposal(data, "lease-return")
        self.assertIn("직접 준비하실 돈은 약 3,780만 원", proposal.title)
        self.assertIn("들어옵니다", proposal.why)                                     # The sale scenario brings cash in.
        self.assertEqual(proposal.figures["need"], Decimal(37_800_000))
        page = render(data)[1]
        self.assertIn("보증금 반환: 실제로 준비할 돈", page)
        self.assertIn("새 세입자 승계(기본)", page)
        self.assertNotIn("목표와 일정", page)                                         # The full timeline is the overview's.

    def test_the_home_goal_leads_with_the_household_baseline(self):
        data = self.report("overview")
        home = data["kpis"]["home_progress"]
        self.assertEqual(home["partner_equity"], Decimal(1_100_000_000) - Decimal(336_000_000))
        self.assertEqual(home["available_household"], home["available_self"] + home["partner_equity"])
        self.assertEqual(home["baseline_label"], "가구 통합 + 예정 소득(확정·예상, 세후)")
        self.assertGreater(home["available_household_with_pay"], home["available_household"])
        self.assertEqual(home["baseline"], home["available_household_with_pay"])
        self.assertGreater(home["ratio_baseline"], home["ratio_self"])
        after = home["after_tax"]
        self.assertLess(after["tax"]["exempt"], after["tax"]["general"])            # One-home exemption taxes less.
        self.assertEqual(after["available"]["general"], home["baseline"] - after["tax"]["general"])
        track = next(t for t in data["advice"]["strategy"] if t.name == "주택 이전")
        self.assertIn("가구 통합 기준", track.headline)
        self.assertIn("본인 자원", track.detail)
        self.assertIn("비과세 요건을 갖추면", track.detail)
        room = data["advice"]["spending_room"]
        # The lease's own cash (37.8M, see the lease test) uses liquid money first; the home gap is the baseline's.
        self.assertEqual(dict(room["planned"])["주택 이전 목표"],
                         home["monthly_net_self"] + (home["gap_baseline"] + Decimal(37_800_000)) / home["months"])
        page = render(data)[1]
        self.assertIn("가구 통합", page)
        self.assertIn("본인 자원", page)                              # The client-only tier stays a table column.

    def test_the_employer_stock_gets_a_trim_schedule_and_never_reads_no_opinion(self):
        data = self.report("markets")
        samsung = next(p for p in data["holdings"]["positions"] if p["symbol"] == "005930")
        self.assertTrue(samsung["employer"] and samsung["must_opine"] and samsung["concentrated"])
        plan = data["advice"]["concentration"]
        self.assertEqual((plan["excess_total"], plan["quarters"], plan["employer_double"]), (Decimal(47_500_000), 4, True))
        self.assertEqual(plan["positions"][0]["per_quarter"], Decimal(11_875_000))
        trim = self.proposal(data, "concentration-trim")
        self.assertEqual(trim.priority, 1)                                           # The employer's stock: act now.
        self.assertIn("4분기에 걸쳐", trim.title)
        self.assertIn("이중 집중", trim.why)
        self.assertIn("받는 즉시 전부 지수펀드로 분산", trim.effect)
        view = load_view(self.conn, as_of=AT, known_at=AT)
        payload = narrative.build_payload(view, data, None)
        held = {h["symbol"]: h for h in payload["holdings"]}
        self.assertTrue(held["005930"]["employer_stock"] and held["005930"]["must_opine"])
        self.assertFalse(held["VOO"]["must_opine"])
        self.assertEqual(payload["portfolio_facts"]["concentration_plan"]["holdings"][0]["symbol"], "005930")
        empty = {"situation": {"text": "", "refs": []}, "direction": {"text": "", "refs": []},
                 "portfolio": {"text": "", "refs": [], "principles": []}, "order": [], "proposal_notes": [],
                 "holdings": [], "insights": [], "watch": []}
        _, dropped = narrative.verify(empty, payload)
        self.assertEqual(dropped, ["holdings[missing]:missing-opinion:005930"])
        self.assertEqual(narrative._rejection_reason(dropped[0])["field"], "holdings")
        # Without a verified opinion the code writes one from the plan: never "의견 없음" on this holding.
        narrative.attach(data, {"output": empty, "dropped": dropped, "memos": {}, "cost_usd": "0"}, None, {"outcome": "success"})
        opinion = data["narrative"]["holdings"]["005930"]
        self.assertEqual((opinion["stance"], opinion["by_code"]), ("trim", True))
        self.assertIn("1,188만 원", opinion["reason"])
        page = render(data)[1]
        self.assertIn("집중도 기준에 따라 계산으로 작성", page)
        self.assertIn("이중 집중", page)
        self.assertEqual(page.count("의견 없음"), 2)                                 # VOO and SMR, which need none.

    def test_a_failed_narrative_still_gives_the_employer_stock_the_code_opinion(self):
        from smith import headless
        data = self.report("markets")
        narrative.attach(data, None, None, {"outcome": "failure", "error_code": "timeout"})
        self.assertEqual(data["narrative"]["holdings"]["005930"]["by_code"], True)
        page = render(data)[1]
        self.assertIn("집중도 기준에 따라 계산으로 작성", page)
        self.assertIn("종목별 의견을 만들지 못해", page)                        # Still honest about the model's failure.
        # Runaway output is clipped to the schema's bounds before validation instead of losing the whole call.
        schema = {"type": "object", "additionalProperties": False, "required": ["refs", "text"],
                  "properties": {"refs": {"type": "array", "maxItems": 2, "items": {"type": "string"}},
                                 "text": {"type": "string", "maxLength": 30}}}
        pruned = headless.prune({"refs": ["R1", "R2", "R3"], "text": "첫 문장입니다. 둘째 문장입니다. 셋째 문장이 길게 이어집니다.",
                                 "extra": 1}, schema)
        self.assertEqual(pruned, {"refs": ["R1", "R2"], "text": "첫 문장입니다. 둘째 문장입니다."})
        self.assertEqual(headless.validate(pruned, schema, "output"), [])
        self.assertEqual(headless.clip_text("가나다라마바사", 4), "가나다라")        # No boundary: a plain cut.
        # One long sentence is cut at a clause break with an ellipsis, never inside a word (2026-10-18).
        self.assertEqual(headless.clip_text("첫째 절이 길게 이어지고, 둘째 절이 이어집니다", 20), "첫째 절이 길게 이어지고…")

    def test_insurance_premiums_above_the_limit_or_past_the_home_date_are_a_proposal(self):
        data = self.report("tax")
        review = data["advice"]["insurance_review"]
        self.assertEqual(review["monthly_premiums"], Decimal(1_240_000))
        self.assertEqual(review["share_of_income"].quantize(Decimal("0.001")), Decimal("0.138"))   # 1.24M / 9M.
        self.assertTrue(review["over_limit"] and review["mismatch"])
        self.assertEqual([s["maturity_year"] for s in review["savings_type"]], [2034])               # 2024 + 10 > 2029.
        proposal = self.proposal(data, "insurance-review")
        self.assertIn("감액완납", proposal.title)
        self.assertIn("기간 불일치", proposal.why)
        self.assertIn("1,200만 원", proposal.effect)                                 # 1.0M x 12 a year back in cash flow.
        self.assertIn("insurance-review", {p.key for p in self.report("monday")["advice"]["proposals"]})
        self.assertNotIn("insurance-review", {p.key for p in self.report("life")["advice"]["proposals"]})
        self.assertIn("감액완납", render(data)[1])


class NarrativeRuleTests(Upgrade):
    def test_the_repair_merge_matches_rejected_items_by_identity(self):
        # The incident (BUG-2026-002): the first call's opinion on the largest holding (index 0) failed, QQQ (2)
        # and INTC (3) failed too; the repair fixed QQQ only. The old merge gave slot 0 to QQQ and slot 2 to
        # another holding, so the largest holding silently lost its opinion and read "의견 없음".
        def opinion(symbol, stance):
            return {"symbol": symbol, "stance": stance, "reason": "r", "alternative": "", "reconsider": "c", "refs": [],
                    "principles": []}
        raw = {"holdings": [opinion("005930", "trim"), opinion("VOO", "add"), opinion("QQQ", "hold"), opinion("INTC", "exit")]}
        first = {"holdings": [opinion("VOO", "add")], "proposal_notes": [], "insights": [], "watch": [],
                 "situation": {"text": "s", "refs": []}, "direction": {"text": "", "refs": []},
                 "portfolio": {"text": "", "refs": [], "principles": []}, "order": []}
        dropped = ["holdings[0]:unknown-ref", "holdings[2]:ungrounded-number:2.5%", "holdings[3]:unknown-ref"]
        repaired = {**first, "holdings": [opinion("VOO", "add"), opinion("QQQ", "hold"), opinion("086900", "exit")]}
        merged, remaining = narrative.merge(first, dropped, repaired, raw)
        self.assertEqual([h["symbol"] for h in merged["holdings"]], ["VOO", "QQQ"])
        self.assertEqual(remaining, ["holdings[0]:unknown-ref", "holdings[3]:unknown-ref"])
        restored, memos = narrative.restore(merged, remaining, raw)                   # The failing ones come back ...
        self.assertEqual([h["symbol"] for h in restored["holdings"]], ["VOO", "QQQ", "005930", "INTC"])
        self.assertEqual(restored["holdings"][2]["memo"], narrative.MEMOS["unknown-ref"])  # ... with a footer note.
        # A missing opinion written in the repair is merged by its symbol.
        merged, remaining = narrative.merge(first, ["holdings[missing]:missing-opinion:005930"],
                                            {**first, "holdings": [opinion("005930", "trim")]}, raw)
        self.assertEqual(([h["symbol"] for h in merged["holdings"]], remaining), (["VOO", "005930"], []))

    def test_rounded_amounts_pass_the_number_check_and_precise_ones_are_polished(self):
        data = self.report("overview")
        payload = narrative.build_payload(data["view"], data, None)
        net_worth = Decimal(payload["household"]["balance_sheet"]["net_worth"])      # 2,780M of assets less the 336M deposit.
        self.assertEqual(net_worth, Decimal(2_444_000_000))
        payload["household"]["balance_sheet"]["net_worth"] = "2725395264"             # The incident's figure.
        for text, ok in (("순자산은 약 27억 2,500만 원입니다.", True), ("순자산은 약 27.3억 원입니다.", True),
                         ("순자산은 27억 2,539만 5,264원입니다.", True), ("순자산은 약 27억 2,600만 원입니다.", False),
                         ("순자산은 약 29억 원입니다.", False)):          # 28억 would match the 27.8억 of assets.
            with self.subTest(text=text):
                self.assertEqual(narrative.check_block([text], [], payload) is None, ok)
        self.assertEqual(narrative.polish("순자산은 27억 2,539만 5,264원이고(전망) 월 660,016원이 남습니다. 325만 7,091원과 1,662만 원."),
                         "순자산은 약 27.3억 원이고 월 약 66만 원이 남습니다. 약 326만 원과 1,662만 원.")
        self.assertEqual(narrative.polish("약 85,000원과 약 3.4억 원은 그대로입니다."), "약 85,000원과 약 3.4억 원은 그대로입니다.")

    def test_internal_vocabulary_is_unusable_and_parentheses_are_limited(self):
        self.assertEqual(narrative.style_problem(["JSON 입력에서 owner_id를 partner로 한 레코드가 필요합니다."]), "internal-term")
        self.assertEqual(narrative.style_problem(["변동금리(설명) 대출과 LTV(비율)를 함께 봅니다."]), "parentheses")
        self.assertIsNone(narrative.style_problem(["변동금리(설명) 대출을 봅니다. LTV(비율)도 봅니다."]))
        # A category instead of the event is rewritten (user review 2026-10-18); the named event passes.
        self.assertEqual(narrative.style_problem(["나쁜 일이 겹치는 한 해를 현금으로 버티지 못합니다."]), "vague-term")
        self.assertIsNone(narrative.style_problem(["금리 1.5%p 상승과 소득 12개월 중단이 함께 오면 2.7개월을 버팁니다."]))
        data = self.report("overview")
        payload = narrative.build_payload(data["view"], data, None)
        output = {"situation": {"text": "JSON 입력을 더 주십시오.", "refs": []}, "direction": {"text": "", "refs": []},
                  "portfolio": {"text": "", "refs": [], "principles": []}, "order": [], "proposal_notes": [],
                  "holdings": [], "insights": [], "watch": []}
        verified, dropped = narrative.verify(output, payload)
        self.assertEqual(dropped, ["situation:internal-term"])
        restored, memos = narrative.restore(verified, dropped, output)
        self.assertEqual((restored["situation"]["text"], memos), ("", {}))            # Never shown to the client.
        self.assertIn("자동 점검 메모", narrative.INTERNAL_TERMS)
        self.assertIn("No internal vocabulary", narrative.SYSTEM_PROMPT)
        self.assertIn("executive headline", narrative.SYSTEM_PROMPT)
        self.assertIn("reserve_swap", narrative.SYSTEM_PROMPT)
        self.assertNotIn("강윤석", narrative.SYSTEM_PROMPT)                            # No identifiers in a prompt.


class RenderingTests(Upgrade):
    def test_the_mail_leads_with_a_headline_and_keeps_system_notes_in_the_footer(self):
        data = self.report("overview")
        data["narrative"] = {"status": {"outcome": "success", "dropped": ["direction:no-source"]},
                             "situation": "주식의 쏠림을 풀면 비상금과 집중이 함께 해결됩니다. 지난주 시장은 조용했습니다. "
                                          "가계의 강점은 두 집의 순자산이고 병목은 한 종목입니다.",
                             "direction": "장기 방향은 그대로입니다.", "direction_memo": "출처가 확인되지 않은 외부 사실이 들어 있습니다.",
                             "situation_memo": "", "portfolio": "", "holdings": {}, "insights": [], "watch": []}
        page = render(data)[1]
        self.assertIn("주식의 쏠림을 풀면 비상금과 집중이 함께 해결됩니다.", page)
        self.assertLess(page.index("주식의 쏠림을 풀면"), page.index("지난주 시장은 조용했습니다"))
        # v3.0 (report design §18.1): caveats, input hints and the badge's reasons are not in the mail at all;
        # they are the report's system notes, kept for the e-mail answers.
        for text in ("자동 점검 메모", "입력: ", "owner_id", "상태 판정: ", "판정 사유는 맨 아래", "데이터 상태 및 시스템 노트",
                     " — 지금 실행을 권하는 제안이"):
            self.assertNotIn(text, page, text)
        notes = report_html.system_notes(data)
        self.assertTrue(any(n.startswith("자동 점검 메모 · 전략 방향: 출처가 확인되지 않은") for n in notes))
        self.assertTrue(any(n.startswith("입력 안내 · ") for n in notes))
        self.assertTrue(any(n.startswith("상태 판정: ") for n in notes))

    def test_themed_days_do_not_repeat_the_overview_tables(self):
        overview, markets, property_, life, rates = (render(self.report(k))[1]
                                                     for k in ("overview", "markets", "property", "life", "rates"))
        for text in ("목표와 일정", "앞으로 들어올 돈", "자산의 구성"):  # The composition is the hero bar.
            self.assertIn(text, overview, text)
        self.assertNotIn("목표와 일정", property_)
        self.assertIn("보증금 반환: 실제로 준비할 돈", property_)
        self.assertNotIn("앞으로 들어올 돈", life)
        self.assertIn("목표까지의 준비", life)
        self.assertIn("가구 통합", life)
        self.assertIn("시장 금리와 환율", markets)                                    # The compact macro view ...
        self.assertNotIn("금리·환율 변화에 노출된", markets)                           # ... without the exposures table,
        self.assertIn("거시 환경", rates)                                             # which stays on the rates day.
        self.assertTrue(all(len(page) < len(render(self.report("monday"))[1]) for page in (markets, property_, life, rates)))


if __name__ == "__main__":
    unittest.main()
