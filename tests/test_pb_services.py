"""Private-banker services added against the top-tier PB benchmark (report design §16): the composite
stress test, the guilt-free spending room, the peer benchmark research topic and the behavioural-shield
prompts. Synthetic household; every figure is checked against hand arithmetic. The second class holds the
first review's counterexamples."""
import json
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

from smith import adviser, editions, ledger, narrative, proposals, qa, report_html, research, stress
from smith.importer import parse_import
from smith.payload import load_view
from smith.report_data import build_report
from smith.report_html import render

AT = datetime(2026, 10, 17, tzinfo=timezone.utc)


def rec(record_id, kind, owner="self", **fields):
    return {"id": record_id, "kind": kind, "owner_id": owner, "effective_at": AT.isoformat(), "revision": 1,
            "status": "active", **fields}


def flow(record_id, category, amount, direction="outflow", owner="self", currency="KRW", **extra):
    return rec(record_id, "cashflow", owner, category=category, direction=direction, currency=currency, amount=amount,
               frequency="monthly", start_date="2026-01-01", **extra)


def goal(record_id, category, target, on, currency="KRW"):
    return rec(record_id, "goal", category=category, currency=currency, target_amount=target, target_date=on, priority="high")


def lease(record_id, principal, due):
    return rec(record_id, "liability", category="lease_deposit_obligation", currency="KRW", outstanding_principal=principal,
               annual_rate="0", rate_type="fixed", repayment_method="bullet", maturity=due)


RECORDS = [
    rec("cash", "asset", category="cash", account_type="bank", currency="KRW", value="30000000",
        valuation_method="manual", liquidity="immediate"),
    rec("etf", "asset", category="fund", account_type="brokerage", currency="KRW", value="100000000",
        valuation_method="statement", liquidity="days", symbol="KODEX200", market="KR"),
    rec("deposit", "asset", category="deposit", account_type="bank", currency="KRW", value="70000000",
        valuation_method="statement", liquidity="days"),
    rec("hermes", "asset", category="unclassified", account_type="brokerage", currency="KRW", value="50000000",
        valuation_method="statement", liquidity="days", managed_by="hermes"),
    rec("home", "asset", category="real_estate", account_type="none", currency="KRW", value="1000000000",
        valuation_method="manual", liquidity="months", occupancy="owner_occupied"),
    rec("mortgage", "liability", category="mortgage", currency="KRW", outstanding_principal="400000000",
        annual_rate="0.04", rate_type="variable", repayment_method="bullet", collateral_record_id="home"),
    {**lease("lease", "200000000", "2027-10-01"), "collateral_record_id": "home"},
    flow("salary", "salary", "6000000", "inflow"),
    flow("partner-pay", "salary", "3000000", "inflow", owner="partner"),
    flow("living", "living_expense", "3000000"),
    flow("interest", "loan_payment", "1500000", liability_record_id="mortgage"),
    flow("gym", "other_expense", "500000", commitment="discretionary"),
]


class Household(unittest.TestCase):
    def setUp(self):
        self.conn = ledger.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.imports = 0
        self.load(RECORDS, owners=("self", "partner"))

    def load(self, records, owners=("self",)):
        self.imports += 1
        doc = {"schema_version": 1, "import_id": f"i{self.imports}", "source": "manual", "mode": "patch",
               "as_of": AT.isoformat(), "owners": [{"id": o} for o in owners], "records": records}
        ledger.apply_import(self.conn, parse_import(json.dumps(doc)), recorded_at=AT)

    def report(self, kind="life"):
        return build_report(self.conn, as_of=AT, known_at=AT, baseline=None, kind=kind)

    def room(self):
        return self.report()["advice"]["spending_room"]

    def proposal(self, data):
        return next(p for p in data["advice"]["proposals"] if p.key == "spending-room")


class StressAndSpendingTests(Household):
    def test_composite_stress_counts_the_scenario_months_from_the_household_figures(self):
        st = self.report()["stress"]
        m = st["monthly"]
        # Income 9.0M, the largest earner (self, 6.0M) stops; outflow 5.0M + 400M x 1.5% / 12 = 0.5M extra interest.
        self.assertEqual((m["inflow"], m["lost_income"], m["lost_income_owner"]), (Decimal(9_000_000), Decimal(6_000_000), "self"))
        self.assertEqual(m["extra_interest"], Decimal(500_000))
        self.assertEqual(m["burn"], Decimal(2_500_000))                      # 5.0M + 0.5M - 3.0M
        self.assertEqual(st["months_cash"], Decimal(12))                      # 30M / 2.5M: the whole scenario, 0 left.
        self.assertEqual((st["covers_scenario"], st["cash_after_scenario"]), (True, Decimal(0)))
        # Sellable within days at stressed prices: ETF 100M x 0.7 + deposit 70M; Hermes money is never counted.
        self.assertEqual(st["sellable_stressed"], Decimal(140_000_000))
        self.assertEqual(st["months_with_sales"], Decimal(12))                # Capped at the scenario length (not 68).
        self.assertEqual(st["after_sales"], Decimal(140_000_000))
        self.assertEqual(st["pausable"], Decimal(500_000))
        # Net worth drop: home 1,000M x 20% + ETF 100M x 30% + Hermes 50M x 30% (still the household's wealth).
        self.assertEqual(st["net_worth_drop"], Decimal(245_000_000))
        self.assertEqual(st["net_worth_drop_ratio"].quantize(Decimal("0.0001")), Decimal("0.3769"))  # / 650M net worth
        # LTV counts the mortgage only; the lease deposit owed on the home is a separate obligation.
        self.assertEqual((st["properties"][0]["ltv"], st["properties"][0]["ltv_stressed"]), (Decimal("0.4"), Decimal("0.5")))
        self.assertEqual(st["properties"][0]["deposits"], Decimal(200_000_000))
        # Lease due in 12 months: cash + stressed sales - 12 months of burn = 170M - 30M = 140M < 200M.
        self.assertEqual((st["lease"]["available"], st["lease"]["covered"]), (Decimal(140_000_000), False))
        # Cash lasts the scenario (robust liquidity) but the deposit cannot be returned: overall at most watch.
        self.assertEqual((st["liquidity_verdict"], st["flags"], st["verdict"]), ("robust", ["lease_deposit_uncovered"], "watch"))
        data = self.report()
        page = render(data)[1]
        # Named in the status reasons, which are system notes (v3.0: not printed in the mail at all).
        self.assertIn("상태 판정: 스트레스 테스트에서는 2027년 10월 보증금 반환에 필요한 돈이 부족합니다.",
                      report_html.system_notes(data))
        self.assertIn("12개월을 모두 버티고", page)

    def test_stress_verdicts_and_unknown_inputs(self):
        self.assertEqual(stress._verdict(Decimal("5.9"), Decimal(1)), "fragile")
        self.assertEqual(stress._verdict(Decimal(6), Decimal(1)), "watch")
        self.assertEqual(stress._verdict(None, Decimal(-1)), "robust")       # Still saving under stress.
        self.assertEqual(stress._verdict(None, Decimal(1)), "unknown")       # Cash unknown: no confident verdict.
        self.assertEqual(stress._verdict(None, None), "unknown")
        # A dollar deposit without an FX rate: sellable total, lease judgement and the net-worth ratio (partial
        # balance sheet) are unknown, not zero; cash alone is still known, so no flag and a robust verdict.
        self.load([rec("usd", "asset", category="deposit", account_type="bank", currency="USD", value="1000",
                       valuation_method="statement", liquidity="days")])
        st = self.report()["stress"]
        self.assertIsNone(st["sellable_stressed"])
        self.assertIsNone(st["months_with_sales"])
        self.assertIsNone(st["lease"]["covered"])
        self.assertIsNone(st["net_worth_drop_ratio"])
        self.assertEqual((st["flags"], st["verdict"]), ([], "robust"))

    def test_spending_room_on_the_committed_basis(self):
        data = self.report()
        room = data["advice"]["spending_room"]
        # Surplus 4.0M (9.0M - 5.0M, no transfers). Reserve target 6 x 5.0M = 30M is met: no reserve need.
        # Lease 200M due in 12 months, liquid (cash 30M + days 170M, Hermes excluded) = 200M: no lease need,
        # but all of that liquid money is spoken for. No planned goals.
        self.assertEqual(room["surplus"], Decimal(4_000_000))
        self.assertEqual(dict(room["committed"]), {"비상금 채우기": Decimal(0), "보증금 반환 준비": Decimal(0)})
        self.assertEqual((room["planned"], room["room"], room["room_with_goals"]), ([], Decimal(4_000_000), Decimal(4_000_000)))
        proposal = self.proposal(data)
        self.assertEqual(proposal.priority, 3)
        self.assertIn("400만 원", proposal.title)
        self.assertIn("경험", proposal.effect)
        page = render(data)[1]
        self.assertIn("마음 편히 써도 되는 돈", page)
        self.assertIn("안심 소비 한도(약정 기준)", page)
        self.assertIn(report_html.STRESS_TITLE, page)
        # Concrete: the four assumptions with their sizes and the 2022 precedent with its sources (review 2026-10-18).
        self.assertIn("변동금리 +1.5%p, 가장 큰 소득 12개월 중단, 집값 20%·주가 30% 하락이 한 해에 모두 오는 드문 극단을 가정해도", page)
        self.assertIn("2022년 기준금리 1.00%→3.25%, 코스피 -24.9%", page)
        self.assertIn("bok.or.kr", page)
        self.assertNotIn("나쁜 일", page)

    def test_a_near_deposit_the_surplus_cannot_reach_is_an_act_now_item(self):
        # An earlier, larger deposit due in 8 months takes (250M - 200M liquid) / 8 months every month.
        self.load([lease("lease2", "250000000", "2027-06-01")])
        data = self.report()
        room = data["advice"]["spending_room"]
        self.assertEqual(dict(room["committed"])["보증금 반환 준비"], Decimal(6_250_000))
        self.assertEqual((room["room"], room["near_commitment_short"]), (Decimal(-2_250_000), True))
        proposal = self.proposal(data)
        self.assertEqual(proposal.priority, 1)
        self.assertIn("여유가 없습니다", proposal.title)
        self.assertIn("더 큽니다", render(data)[1])

    def test_spending_room_is_undetermined_when_a_flow_is_unconverted(self):
        self.load([flow("usd-pay", "other_income", "100", "inflow", currency="USD")])
        data = self.report()
        self.assertIsNone(data["advice"]["spending_room"])
        self.assertIn("안심 소비 한도", [u.topic for u in data["advice"]["undetermined"]])
        self.assertNotIn("마음 편히 써도 되는 돈", render(data)[1])

    def test_the_narrative_sees_both_as_evidence_and_the_days_carry_them(self):
        data = self.report("overview")
        view = load_view(self.conn, as_of=AT, known_at=AT)
        metrics = narrative.sector_metrics(view, data)
        self.assertEqual((metrics["stress"]["verdict"], metrics["stress"]["liquidity_verdict"]), ("watch", "robust"))
        self.assertEqual(metrics["stress"]["months_on_cash"], "12.0")
        self.assertEqual((metrics["spending_room"]["room"], metrics["spending_room"]["room_with_goals"]), ("4000000", "4000000"))
        self.assertIn("sector_metrics", narrative._EVIDENCE_KEYS)
        # The scenario's "1.5%p", "20%", "30%" and the net-worth fall in % are percent candidates for the
        # number check, so a sentence quoting them passes.
        percents = narrative._percents(metrics["stress"])
        self.assertTrue({Decimal("1.5"), Decimal(20), Decimal(30)} <= percents, percents)
        self.assertIn((Decimal(245_000_000) / Decimal(650_000_000) * 100).quantize(Decimal("0.01")), percents)
        self.assertEqual(editions.home("spending-room").key, "life")
        self.assertIn("stress_summary", editions.EDITIONS["overview"].sections)
        self.assertIn("stress", editions.EDITIONS["life"].sections)
        self.assertNotIn("stress", editions.EDITIONS["markets"].sections)
        overview = render(data)[1]
        self.assertIn("자세한 표는 생활·목표 보고에 있습니다", overview)   # One paragraph on the overview ...
        self.assertNotIn("가정한 해", overview)                            # ... without the table.
        self.assertIn("가정한 해", render(self.report("life"))[1])
        self.assertNotIn(report_html.STRESS_TITLE, render(self.report("tax"))[1])


class ReviewCounterexampleTests(Household):
    """The first review's counterexamples (REVIEW-REQUEST-2026-10-17-pb-services.md)."""

    def test_other_dated_goals_shrink_the_room_with_goals_and_the_title_says_so(self):
        # A 1,000M purchase next month: nothing is set aside for it, so it needs its whole target this month.
        self.load([goal("car", "major_purchase", "1000000000", "2026-11-15")])
        data = self.report()
        room = data["advice"]["spending_room"]
        self.assertEqual(dict(room["planned"]), {"큰 지출 목표(2026.11)": Decimal(1_000_000_000)})
        self.assertEqual(room["room"], Decimal(4_000_000))                    # Reserve and the lease are still fine ...
        self.assertLess(room["room_with_goals"], 0)                           # ... but the planned goal is not.
        proposal = self.proposal(data)
        self.assertEqual(proposal.priority, 3)
        self.assertIn("여유가 없습니다", proposal.title)
        self.assertIn("계획한 목표까지 지킬 때", render(data)[1])

    def test_a_home_goal_whose_plan_cannot_be_computed_makes_the_room_undetermined(self):
        self.load([goal("home-usd", "home", "2000000", "2029-01-01", currency="USD")])   # No FX rate.
        data = self.report()
        self.assertIsNone(data["advice"]["spending_room"])
        self.assertIn("안심 소비 한도", [u.topic for u in data["advice"]["undetermined"]])
        self.assertNotIn("쓰실 수 있는 돈", render(data)[1])

    def test_a_home_goal_keeps_the_committed_room_and_shows_the_goal_room_beside_it(self):
        # A 3,000M home in 24 months: the plan already saves every won of the client's net cash flow and
        # still falls far short, so the room with goals is deeply negative while the committed room stays.
        self.load([goal("goal", "home", "3000000000", "2028-10-01")])
        data = self.report()
        room = data["advice"]["spending_room"]
        self.assertGreater(dict(room["planned"])["주택 이전 목표"], room["surplus"])
        self.assertEqual(room["room"], Decimal(4_000_000))
        self.assertLess(room["room_with_goals"], 0)
        self.assertEqual(self.proposal(data).priority, 3)

    def test_a_deposit_due_later_this_month_is_one_month_away_not_dropped(self):
        self.load([lease("lease3", "50000000", "2026-10-31")])
        data = self.report()
        st = data["stress"]
        self.assertEqual((st["lease"]["due"], st["lease"]["months"]), (date(2026, 10, 31), 1))
        # Drained for one month only: 170M - 2.5M = 167.5M covers 50M.
        self.assertEqual((st["lease"]["available"], st["lease"]["covered"]), (Decimal(167_500_000), True))
        self.assertEqual((st["flags"], st["verdict"]), ([], "robust"))
        # The lease-return rule lives on the property day; the full report carries every rule.
        lease_return = next(p for p in self.report("monday")["advice"]["proposals"] if p.key == "lease-return")
        self.assertEqual(lease_return.figures["months"], Decimal(1))
        self.assertEqual(dict(data["advice"]["spending_room"]["committed"])["보증금 반환 준비"], Decimal(0))

    def test_a_home_goal_due_this_month_divides_by_one_month(self):
        self.load([goal("soon", "home", "1200000000", "2026-10-30")])
        room = self.room()
        self.assertIsNotNone(room)
        self.assertGreater(dict(room["planned"])["주택 이전 목표"], 0)


class BenchmarkAndShieldTests(unittest.TestCase):
    def test_the_peer_benchmark_is_asked_only_on_the_overview_and_without_the_household_age(self):
        conn = ledger.connect(":memory:")
        self.addCleanup(conn.close)
        doc = {"schema_version": 1, "import_id": "b1", "source": "manual", "mode": "patch", "as_of": AT.isoformat(),
               "owners": [{"id": "self"}], "records": RECORDS[:1]}
        ledger.apply_import(conn, parse_import(json.dumps(doc)), recorded_at=AT)
        view = load_view(conn, as_of=AT, known_at=AT)
        overview = [t for t in research.topics(view, "overview") if "또래" in t.title]
        self.assertEqual(len(overview), 1)
        self.assertIn("연령대별", overview[0].question)                    # Age bands in general, not the client's.
        self.assertIn("가계금융복지조사", overview[0].question)
        for kind in (None, "monday", "thursday", "life", "markets"):      # Not the original kinds either.
            self.assertEqual([t for t in research.topics(view, kind) if "또래" in t.title], [], kind)

    def test_prompts_name_the_behavioural_shield_and_the_new_inputs(self):
        self.assertIn("Behavioural shield", adviser.SYSTEM_PROMPT)
        self.assertNotIn("one or two", adviser.SYSTEM_PROMPT)
        self.assertIn("Behavioural shield", qa.SYSTEM_PROMPT)
        self.assertIn("sector_metrics.stress", narrative.SYSTEM_PROMPT)
        self.assertIn("room_with_goals", narrative.SYSTEM_PROMPT)
        self.assertEqual((adviser.PROMPT_VERSION, qa.PROMPT_VERSION, narrative.PROMPT_VERSION),
                         ("advice-v10", "mail-answer-v13", "narrative-v25"))
        self.assertIn("spending-room", proposals.RULE_KEYS)


if __name__ == "__main__":
    unittest.main()
