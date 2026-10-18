import json
import subprocess
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path

from smith import editions, ledger, narrative, research
from smith.importer import parse_import
from smith.records import ImportBatch, Observation
from smith.report_data import build_report
from smith.report_html import render

import fast_ledger

AT = datetime(2026, 10, 2, tzinfo=timezone.utc)


def rec(record_id, kind, **fields):
    return {"id": record_id, "kind": kind, "owner_id": "self", "effective_at": AT.isoformat(), "revision": 1,
            "status": "active", **fields}


RECORDS = [
    rec("cash", "asset", category="cash", account_type="bank", currency="KRW", value="10000000",
        valuation_method="manual", liquidity="immediate"),
    rec("home", "asset", category="real_estate", account_type="none", currency="KRW", value="1500000000",
        valuation_method="manual", liquidity="months", occupancy="owner_occupied", region="경기도 수원시 영통구"),
    rec("voo", "asset", category="stock", account_type="brokerage", currency="USD", value="1000",
        valuation_method="market", liquidity="days", symbol="VOO", market="US"),
    rec("mortgage", "liability", category="mortgage", currency="KRW", outstanding_principal="200000000",
        annual_rate="0.04", rate_type="variable", repayment_method="equal_principal", collateral_record_id="home"),
    rec("pay", "cashflow", category="salary", direction="inflow", currency="KRW", amount="5000000",
        frequency="monthly", start_date="2026-01-01"),
    rec("living", "cashflow", category="living_expense", direction="outflow", currency="KRW", amount="2000000",
        frequency="monthly", start_date="2026-01-01"),
]
# Every proposal rule (proposals.py). Each belongs to exactly one themed day; the overview carries all.
PROPOSAL_KEYS = {"lease-return", "emergency-reserve", "goal-stocks-glide", "concentration-trim", "surplus-plan",
                 "pension-credit", "overseas-harvest", "isa-open", "insurance-review", "prepayment", "variable-rate",
                 "refinance", "spending-room"}
EXTRA = [
    rec("lease", "liability", category="lease_deposit_obligation", currency="KRW", outstanding_principal="300000000",
        annual_rate="0", rate_type="fixed", repayment_method="bullet", maturity="2028-08-07", collateral_record_id="home"),
    rec("pension", "asset", category="fund", account_type="pension_savings", currency="KRW", value="20000000",
        valuation_method="statement", liquidity="restricted"),
]


class EditionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "smith.db"
        doc = {"schema_version": 1, "import_id": "e1", "source": "manual", "mode": "patch", "as_of": AT.isoformat(),
               "owners": [{"id": "self"}], "records": RECORDS + EXTRA}
        with closing(ledger.connect(self.db)) as conn:
            ledger.apply_import(conn, parse_import(json.dumps(doc)), recorded_at=AT)
            ledger.apply_import(conn, ImportBatch("fx", "toss", "snapshot", AT, ("self",), (), "x",
                                                  (Observation("USD", "fx_mid_rate", "KRW", "1400", AT),)), recorded_at=AT)

    def data(self, kind):
        with closing(ledger.connect_read_only(self.db)) as conn:
            return build_report(conn, as_of=AT, known_at=AT, baseline=None, kind=kind)

    def test_every_proposal_rule_has_one_home_day(self):
        from smith import proposals
        self.assertEqual(set(proposals.RULE_KEYS), PROPOSAL_KEYS)
        homes = [key for e in editions.EDITIONS.values() if e.proposals is not None for key in e.proposals]
        self.assertEqual(sorted(homes), sorted(PROPOSAL_KEYS))          # Exactly once each, none forgotten.
        full = {p.key for p in self.data("monday")["advice"]["proposals"]}
        self.assertEqual({p.key for p in self.data("overview")["advice"]["proposals"]}, full)
        for key, edition in editions.EDITIONS.items():
            if edition.proposals is not None:
                carried = {p.key for p in self.data(key)["advice"]["proposals"]}
                self.assertEqual(carried, full & set(edition.proposals), key)

    def test_the_weekly_plan_and_the_baseline_series(self):
        monday = date(2026, 10, 19)
        self.assertEqual([editions.for_day(None, date(2026, 10, 19 + i)) for i in range(7)],
                         ["overview", "markets", "property", "rates", "tax", "life", "overview"])
        custom = {"reports": {"editions": {"tuesday": "monday"}}}       # The original full report on Tuesdays.
        self.assertEqual(editions.for_day(custom, date(2026, 10, 20)), "monday")
        self.assertEqual(editions.for_day(custom, monday), "overview")
        self.assertEqual(editions.same_series("overview"), ("overview", "monday"))
        self.assertEqual(editions.title("tax", date(2026, 10, 23)), "금요 세금·연금")
        self.assertEqual(editions.title("thursday", date(2026, 10, 22)), "목요 변화 점검")
        with closing(ledger.connect(self.db)) as conn:
            for report_id, kind, day in (("a", "overview", 12), ("b", "markets", 13), ("c", "property", 14)):
                at = datetime(2026, 10, day, tzinfo=timezone.utc)
                ledger.claim_report(conn, report_id=report_id, slot=None, kind=kind, trigger="manual", created_at=at,
                                    as_of=at, baseline=None, missed_slots=[])
                ledger.finish_report(conn, report_id=report_id, status="sent", finished_at=at, message_id=report_id)
            self.assertEqual(ledger.last_sent_report(conn, kinds=editions.same_series("markets"))["report_id"], "b")
            self.assertEqual(ledger.last_sent_report(conn)["report_id"], "c")

    def test_each_day_researches_only_its_topics(self):
        view = self.data("monday")["view"]
        titles = lambda kind: [t.title for t in research.topics(view, kind)]  # noqa: E731
        self.assertEqual(len(research.topics(view)), len(titles("monday")))   # Original kinds: everything.
        self.assertIn("VOO", " ".join(titles("markets")))                    # Holdings only on the markets day.
        self.assertTrue(all(not t.ref.startswith("H") for t in research.topics(view, "property")))
        self.assertTrue(all("주택" in t or "부동산" in t or "전세" in t for t in titles("property")))
        # The household day researches only the experience dividend (v3.0): no market or policy topic.
        self.assertEqual(titles("life"), ["금주의 라이프스타일·경험 큐레이션"])
        self.assertFalse(any("주택" in t or "금리" in t for t in titles("life")))
        calls = []

        def runner(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 1, "{}", "")
        result = research.research_and_store(self.db, view, now=AT, today=AT.date(), secrets=[], kind="life",
                                             executable=Path("claude.exe"), runner=runner)
        self.assertEqual(result["outcome"], "failure")                          # Recorded, never raised.
        self.assertEqual(len(calls), 1)

    def test_each_day_renders_only_its_sections_and_stays_short(self):
        full_subject, full_html = render(self.data("monday"))
        lengths = {}
        for key in editions.EDITIONS:
            subject, page = render(self.data(key))
            lengths[key] = len(page)
            self.assertIn(editions.title(key, AT.date()), subject)
        self.assertTrue(all(length < len(full_html) for length in lengths.values()), lengths)
        markets, property_, overview = (render(self.data(k))[1] for k in ("markets", "property", "overview"))
        self.assertIn("보유 종목 점검", markets)
        self.assertNotIn("부동산·주거", markets)
        self.assertIn("부동산·주거", property_)
        self.assertNotIn("보유 종목 점검", property_)
        self.assertIn("자세히:", overview)                                  # Summary cards point to their day.
        self.assertIn("목요 변화 점검", render(self.data("thursday"))[0])   # The original kinds are unchanged.

    def test_a_question_on_a_themed_report_sees_every_area_at_that_time(self):
        from smith import qa
        with closing(ledger.connect(self.db)) as conn:
            ledger.claim_report(conn, report_id="w", slot=None, kind="property", trigger="manual", created_at=AT,
                                as_of=AT, baseline=None, missed_slots=[])
            ledger.finish_report(conn, report_id="w", status="sent", finished_at=AT, message_id="w")
            report = ledger.get_report(conn, "w")
        data = qa.rebuild(self.db, report, now=AT)
        self.assertEqual({p.key for p in data["advice"]["proposals"]},
                         {p.key for p in self.data("monday")["advice"]["proposals"]})
        self.assertTrue(narrative.build_payload(data["view"], data, None)["holdings"])

    def test_review_fixes_missed_themes_pending_judgements_and_household_status(self):
        from smith import delivery, report_html
        tuesday = datetime(2026, 10, 20, 6, tzinfo=timezone.utc)
        note = delivery._delivery_note(None, [tuesday], tuesday, [], "UTC", [], kind="property")
        self.assertIn("화요 투자·시장", note)
        self.assertIn("다음 주 같은 주제 보고에서 다룹니다", note)
        self.assertNotIn("합쳐", note)                                      # Nothing of Tuesday is in Wednesday's report.
        self.assertIn("합쳐", delivery._delivery_note(None, [tuesday], tuesday, [], "UTC", [], kind="monday"))
        # No CD rate evidence: the rates rules cannot judge. Their day says so instead of "nothing to do".
        rates = self.data("rates")
        self.assertEqual({u.key for u in rates["advice"]["undetermined"]} - {"prepayment", "variable-rate", "refinance"}, set())
        self.assertTrue(rates["advice"]["undetermined"])
        page = render(rates)[1]
        self.assertIn("판단하지 못한", page)
        self.assertNotIn("새로 실행하실 일이 없습니다", page)
        self.assertNotEqual(report_html._status(rates)[0], "stable")
        self.assertFalse(self.data("property")["advice"]["undetermined"])  # Not another day's pending rules.
        # The badge is household-wide: an urgent proposal of another area is named, not hidden.
        markets = self.data("markets")
        markets["advice"]["household_urgent"] = ["lease-return"]
        status, reasons = report_html._status(markets)
        self.assertEqual(status, "act")
        self.assertIn("부동산 보고", " ".join(reasons))

    def test_review_fixes_tax_source_unknown_sums_and_edition_limits(self):
        from smith import report_data
        claim = (["세법상 공제가 적용됩니다. 현행 법령 확인 필요."], ["tax"])
        rates = narrative.build_payload(self.data("rates")["view"], self.data("rates"), None)
        self.assertIsNotNone(narrative.check_block(*claim, rates))        # No tax inputs: "tax" is no source.
        with_tax = dict(rates, tax={"notes": [{"title": "t", "body": "b", "when": "w"}], "year_end_checklist": []})
        self.assertIsNone(narrative.check_block(*claim, with_tax))
        # The markets day carries the holdings, each with Smith's overseas-tax flag, and a trim or exit opinion
        # must give the tax order of selling: "tax" is a source there (review 2026-10-17).
        markets = narrative.build_payload(self.data("markets")["view"], self.data("markets"), None)
        self.assertIsNone(narrative.check_block(*claim, markets))
        data = self.data("property")
        data["sectors"]["real_estate"]["properties"][0]["value"] = None
        self.assertIsNone(report_data._edition_figures(data)["property_value"])  # Unknown is never summed as zero.
        output = {"situation": {"text": "", "refs": []}, "direction": {"text": "", "refs": []},
                  "portfolio": {"text": "", "refs": [], "principles": []}, "order": [], "proposal_notes": [],
                  "holdings": [], "watch": [{"item": f"점검 {i}", "why": "현금 흐름을 지켜봅니다", "refs": []} for i in "abcde"],
                  "insights": [{"title": f"관찰 {i}", "body": "현금 흐름이 안정적입니다", "implication": "유지합니다",
                                "refs": []} for i in "abcd"]}

        def runner(args, **kwargs):
            envelope = {"type": "result", "subtype": "success", "is_error": False, "structured_output": output,
                        "total_cost_usd": 0.1}
            return subprocess.CompletedProcess(args, 0, json.dumps(envelope), "")
        for kind, insights in (("life", 1), ("overview", 2)):
            data = self.data(kind)
            narrative.narrate(self.db, data["view"], data, now=AT, secrets=[], executable=Path("claude.exe"),
                              runner=runner)
            self.assertEqual((len(data["narrative"]["insights"]), len(data["narrative"]["watch"])), (insights, 2), kind)

    def test_the_narrative_sees_only_its_day(self):
        markets, property_ = (narrative.build_payload(self.data(k)["view"], self.data(k), None)
                              for k in ("markets", "property"))
        self.assertTrue(markets["holdings"] and markets["principles"])
        self.assertEqual((property_["holdings"], property_["principles"]), ([], []))
        self.assertTrue(property_["property_market"] is not None and property_["edition_focus"])
        self.assertEqual(markets["property_market"], [])
        self.assertEqual(markets["household"], property_["household"])     # Every amount stays checkable.
        self.assertNotIn("edition_focus", narrative.build_payload(self.data("monday")["view"], self.data("monday"), None))


def setUpModule():  # noqa: N802 - unittest hook
    fast_ledger.install()  # File ledgers without an fsync per commit (test budget); results are unchanged.


def tearDownModule():  # noqa: N802 - unittest hook
    fast_ledger.uninstall()


if __name__ == "__main__":
    unittest.main()
