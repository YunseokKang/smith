"""The product review team (smith.picks, report design §26): four model roles directed and verified by code.
Every model call is a stub keyed on its role; every cited page is a stub fetch. Synthetic household (the
v3 fixture: one stock above the single-company limit)."""
import json
import subprocess
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from smith import headless, ledger, picks, report_command, report_html
from smith.fmt import short_won
from smith.importer import parse_import
from smith.payload import load_view
from smith.report_data import build_report
from smith.report_html import render
from test_delivery import CONFIG, MON
from test_v3_upgrade import AT, BASE, CD, DEALS, HOUSEHOLD, MARKET, RECORDS, Upgrade

CLAUDE = Path("claude.exe")
SPEC = replace(picks.CORE_ETF, official_hosts=("issuer.example",))   # The stub pages count as official.
OFFICIAL = SPEC.official_hosts


def candidate(symbol, market, price, date="2026-10-17", url=None, expense="0.03%"):
    return {"symbol": symbol, "name": f"{symbol} 펀드", "issuer": "운용사", "market": market, "index": "S&P 500",
            "expense_ratio": expense, "aum": "100억", "inception": "2010-09-07", "return_1y": "15%", "return_3y": "",
            "return_5y": "", "price": price, "price_date": date, "distribution": "분배금 지급", "tax_note": "",
            "source_url": url or f"https://issuer.example/{symbol}", "source_title": "상품 페이지", "publisher": "운용사"}


def envelope(args, output):
    body = {"type": "result", "subtype": "success", "is_error": False, "structured_output": output, "total_cost_usd": 0.25}
    return subprocess.CompletedProcess(args, 0, json.dumps(body), "")


def checks(verdicts, url="https://issuer.example/official"):
    return {"checks": [{"field": field, "claimed": "x", "found": "x", "verdict": verdict, "source_url": url,
                        "source_title": "공식", "publisher": "운용사"} for field, verdict in verdicts], "notes": []}


SCOUT = {"candidates": [candidate("360750", "KR", "21,450원", expense="0.07%"), candidate("VOO", "US", "$500.00"),
                        candidate("SPY", "US", "$600.00", expense="0.09%"), candidate("IVV", "US", "$550.00"),
                        candidate("005930", "KR", "80,000원"),                      # The stock being trimmed.
                        candidate("DEAD", "US", "$10.00", url="https://issuer.example/dead"),  # Its page is gone.
                        candidate("voo", "US", "$500.00")],                       # Duplicate (case).
         "gaps": ["세계 전체 시장 ETF의 총보수를 찾지 못함"]}
VERIFY = {"360750": checks([("identity", "match"), ("expense_ratio", "match"), ("price", "match"), ("aum", "unverified")]),
          "VOO": checks([("identity", "match"), ("expense_ratio", "match"), ("price", "match")]),
          "SPY": checks([("identity", "match"), ("expense_ratio", "mismatch"), ("price", "match")]),
          "IVV": checks([("identity", "match"), ("expense_ratio", "match"), ("price", "match")])}
CRITIQUE = {"reviews": [{"symbol": "360750", "pros": ["국내 상장이라 원화로 바로 산다"], "cons": ["보수가 더 높다"],
                         "risks": ["환헤지 없음"], "score": "8", "score_basis": "비용 대비 접근성"},
                        {"symbol": "VOO", "pros": ["보수 0.03%"], "cons": ["달러 환전 필요"], "risks": [], "score": "9",
                         "score_basis": "최저 비용"},
                        {"symbol": "IVV", "pros": ["보수 0.03%"], "cons": ["VOO와 같은 지수"], "risks": [], "score": "8",
                         "score_basis": "중복"},
                        {"symbol": "SPY", "pros": [], "cons": [], "risks": [], "score": "7", "score_basis": "x"}]}
SELECT = {"picks": [{"symbol": "360750", "rank": "1", "reason": "보수 0.07%로 원화로 바로 삽니다.", "condition": "ISA를 열면 다시 봅니다."},
                    {"symbol": "VOO", "rank": "2", "reason": "보수 0.03%로 가장 쌉니다.", "condition": "환율이 급등하면 늦춥니다."},
                    {"symbol": "SPY", "rank": "3", "reason": "x", "condition": "x"}],                 # Not verified.
          "rejected": [{"symbol": "IVV", "why": "VOO와 같은 지수라 중복"}, {"symbol": "ZZZ", "why": "x"}]}


def runner(args, **kwargs):
    prompt = args[args.index("--system-prompt") + 1]
    payload = json.loads(kwargs["input"])
    if "Role: product scout" in prompt:
        assert "--tools" in args and args[args.index("--tools") + 1] == "WebSearch,WebFetch"
        return envelope(args, SCOUT)
    if "Role: independent fact-checker" in prompt:
        return envelope(args, VERIFY[payload["candidate"]["symbol"]])
    if "Role: product analyst" in prompt:
        assert args[args.index("--tools") + 1] == ""                       # No web for the critic ...
        assert {c["symbol"] for c in payload["candidates"]} == {"360750", "VOO", "IVV"}  # ... verified facts only.
        return envelope(args, CRITIQUE)
    if "Role: the household's private banker" in prompt:
        assert payload["household_context"]["stocks_being_trimmed"] == ["005930"]
        assert "VOO" in payload["household_context"]["holds_broad_index_funds"]
        assert "value" not in json.dumps(payload)                          # Nothing of the household's money.
        return envelope(args, SELECT)
    raise AssertionError(prompt[:60])


def verifier(items):
    kept, dropped = [], {}
    for item in items:
        if "dead" in item["source_url"]:
            dropped["dead-url"] = dropped.get("dead-url", 0) + 1
            continue
        kept.append({**item, "check": {"status": "matched", "final_url": item["source_url"]}, "ref": f"R{len(kept) + 1}"})
    return kept, dropped


def reader(url):
    """The scout's pages state every price with its currency and the quoted date."""
    return "종가 21,450원 · $500.00 · $600.00 · $550.00 · $10.00 · 80,000원 (2026-10-17 기준)"


class ReviewTests(Upgrade):
    def view(self):
        return load_view(self.conn, as_of=AT, known_at=AT)

    def test_the_team_finds_verifies_critiques_and_selects_under_code_control(self):
        result = picks.review(self.view(), SPEC, HOUSEHOLD, today=AT.date(), secrets=[], executable=CLAUDE,
                              runner=runner, verifier=verifier, reader=reader)
        statuses = {c["symbol"]: c["status"] for c in result["candidates"]}
        self.assertEqual(statuses, {"360750": "verified", "VOO": "verified", "SPY": "mismatch", "IVV": "verified"})
        self.assertEqual(result["dropped"]["trimmed-stock"], 1)                 # 005930 never reaches the verifier.
        self.assertEqual((result["dropped"]["duplicate"], result["dropped"]["source-dead-url"]), (1, 1))
        # Code's rank 1 (the held fund at the lowest cost) leads although the selector ranked it second.
        self.assertEqual(result["rank_1"], "VOO")
        self.assertEqual([(p["rank"], p["symbol"]) for p in result["picks"]], [(1, "VOO"), (2, "360750")])
        self.assertEqual(result["dropped"]["pick-unknown"], 1)                  # SPY failed verification ...
        self.assertEqual([r["symbol"] for r in result["rejected"]], ["IVV"])     # ZZZ was never a candidate.
        self.assertEqual(result["candidates"][0]["symbol"], "VOO")              # Held funds are verified first.
        self.assertEqual(result["cost_usd"], "2.00")                            # ... so the selector was asked once more.

    def test_a_reason_written_to_the_input_is_repaired_once_then_withheld(self):
        bad = {"picks": [{"symbol": "VOO", "rank": "1", "reason": "입력 메모가 지적한 과세 문제를 피합니다.", "condition": "x"}],
               "rejected": []}
        self.assertEqual([f["symbol"] for f in picks.style_faults(bad)], ["VOO"])
        passing = [dict(candidate("VOO", "US", "$500.00"), status="verified")]
        self.assertEqual(picks.clean_selection(bad, passing, [])[2], {"pick-style": 1})
        self.assertEqual(picks.selection_faults(bad, passing, [])[0]["reason"][:12], "mentions the")
        self.assertEqual(picks.style_faults({"picks": [{"symbol": "VOO", "rank": "1", "reason": "보수 0.03%로 가장 쌉니다.",
                                                         "condition": "x"}]}), [])
        self.assertEqual(picks._rate_text("연 0.0062% (운용보수 0.0001%); 기타비용 미표시"), "0.0062%")
        self.assertEqual(picks._name_text("Vanguard Total Stock Market ETF (페이지 제목 표기: Vanguard Morningstar …)"),
                         "Vanguard Total Stock Market ETF")

    def test_a_number_the_facts_do_not_carry_drops_the_pick(self):
        passing = [dict(candidate("VOO", "US", "$500.00"), status="verified")]
        picked, _, dropped = picks.clean_selection({"picks": [{"symbol": "VOO", "rank": "1", "reason": "수익률 25%가 기대됩니다.",
                                                                "condition": "x"}], "rejected": []}, passing, [])
        self.assertEqual((picked, dropped), ([], {"pick-ungrounded-number": 1}))

    def test_a_mismatch_an_unreachable_or_an_unofficial_page_is_no_verification(self):
        c = candidate("VOO", "US", "$500.00")
        self.assertEqual(picks.judge(c, checks([("identity", "match"), ("expense_ratio", "match"), ("price", "match")]),
                                     verifier, OFFICIAL)["status"], "verified")
        self.assertEqual(picks.judge(c, checks([("identity", "match"), ("expense_ratio", "match"), ("price", "unverified")]),
                                     verifier, OFFICIAL)["status"], "unverified")
        dead = checks([("identity", "match"), ("expense_ratio", "match"), ("price", "match")], url="https://issuer.example/dead")
        self.assertEqual(picks.judge(c, dead, verifier, OFFICIAL)["status"], "unverified")  # A match on a page code cannot reach.
        blog = checks([("identity", "match"), ("expense_ratio", "match"), ("price", "match")], url="https://blog.example/voo")
        judged = picks.judge(c, blog, verifier, OFFICIAL)
        self.assertEqual(judged["status"], "unverified")                        # Nor on a page outside the official hosts ...
        self.assertTrue(all("공식 출처 아님" in ch["found"] for ch in judged["checks"]))
        self.assertEqual(picks.judge(c, blog, verifier, ("blog.example",))["status"], "verified")  # ... unless the class lists it.
        # The price alone may be confirmed by code on the scout's page (issuer pages that render by script).
        by_code = dict(c, source_price_matched=True)
        judged = picks.judge(by_code, checks([("identity", "match"), ("expense_ratio", "match"), ("price", "unverified")]), verifier,
                             OFFICIAL)
        self.assertEqual(judged["status"], "verified")
        self.assertTrue(any(ch["field"] == "price" and "코드" in ch["found"] for ch in judged["checks"]))
        self.assertEqual(picks.judge(by_code, checks([("identity", "match"), ("expense_ratio", "match"), ("price", "mismatch")]),
                                     verifier, OFFICIAL)["status"], "mismatch")    # Never over a mismatch.
        self.assertEqual(judged["verdicts"], {"identity": "match", "expense_ratio": "match", "price": "match"})

    def test_the_price_on_a_page_needs_its_currency_and_the_quoted_date(self):
        page = "Vanguard S&P 500 ETF (VOO) NAV $500.00 as of Oct 17, 2026. Expense ratio 0.03%. 500000 shares traded."
        self.assertTrue(picks.price_on_page(page, "$500.00", "2026-10-17", "US"))
        self.assertFalse(picks.price_on_page(page, "$500.00", "2026-10-16", "US"))                   # Another day's price.
        self.assertFalse(picks.price_on_page(page.replace("$500.00", "500.00"), "$500.00", "2026-10-17", "US"))  # No currency.
        self.assertFalse(picks.price_on_page("가격 21,450 2026-10-17", "21,450원", "2026-10-17", "KR"))
        self.assertTrue(picks.price_on_page("종가 21,450원 (2026.10.17)", "21,450원", "2026-10-17", "KR"))
        self.assertTrue(picks.price_on_page("종가 21450원 2026년 10월 17일", "21,450원", "2026-10-17", "KR"))
        self.assertFalse(picks.price_on_page("121,450원 2026-10-17", "21,450원", "2026-10-17", "KR"))     # Part of a larger number.
        self.assertFalse(picks.price_on_page(None, "$500.00", "2026-10-17", "US"))

    def test_code_fixes_rank_one_by_cost_tax_route_held_fund_and_continuity(self):
        kr = dict(candidate("360750", "KR", "21,450원", expense="0.07%"), status="verified")
        voo = dict(candidate("VOO", "US", "$500.00"), status="verified")
        ivv = dict(candidate("IVV", "US", "$550.00"), status="verified")
        plain = {"has_isa": False, "has_pension_accounts": False, "holds_broad_index_funds": []}
        self.assertEqual(picks.rank_first([kr, voo, ivv], plain, None), "IVV")               # Equal cost: the symbol order.
        self.assertEqual(picks.rank_first([kr, voo, ivv], {**plain, "holds_broad_index_funds": ["VOO"]}, None), "VOO")
        self.assertEqual(picks.rank_first([kr, voo, ivv], plain, "VOO"), "VOO")              # Continuity within the margin ...
        cheap = dict(candidate("SPYM", "US", "$70.00", expense="0.02%"), status="verified")
        self.assertEqual(picks.rank_first([voo, cheap], plain, "VOO"), "VOO")                # ... 1bp is not a reason to switch,
        self.assertEqual(picks.rank_first([kr, cheap], plain, "360750"), "SPYM")             # 20bp (7 + the tax penalty) is.
        kospi = dict(candidate("069500", "KR", "40,000원", expense="0.15%"), status="verified")
        kospi["index"] = "KOSPI 200"
        self.assertEqual(picks.rank_first([kospi, dict(kr, expense_ratio="0.05%")], plain, None), "069500")  # No penalty at home ...
        self.assertEqual(picks.rank_first([kospi, dict(kr, expense_ratio="0.05%")], {**plain, "has_isa": True}, None), "360750")
        self.assertIsNone(picks.rank_first([dict(voo, expense_ratio="미상")], plain, None))
        # The analysts see confirmed facts only; the rest is named as unverified.
        facts = picks.facts_of(dict(voo, verdicts={"identity": "match", "expense_ratio": "match", "price": "match", "aum": "match"}))
        self.assertEqual((facts["aum"], facts["inception"], facts["unverified"]), ("100억", "", "inception, distribution"))
        self.assertNotIn("return_1y", facts)

    def test_a_selection_without_codes_rank_one_is_no_selection(self):
        passing = [dict(candidate("VOO", "US", "$500.00"), status="verified"), dict(candidate("IVV", "US", "$550.00"), status="verified")]
        chosen = {"picks": [{"symbol": "IVV", "rank": "1", "reason": "보수 0.03%입니다.", "condition": "x"},
                            {"symbol": "VOO", "rank": "2", "reason": "보수 0.03%입니다.", "condition": "x"}], "rejected": []}
        self.assertEqual([f["reason"][:8] for f in picks.selection_faults(chosen, passing, [], "VOO")], ["picks[0]"])
        picked, _, dropped = picks.clean_selection(chosen, passing, [], "VOO")
        self.assertEqual(([p["symbol"] for p in picked], dropped), (["VOO", "IVV"], {}))     # Reordered when present ...
        picked, _, dropped = picks.clean_selection({"picks": chosen["picks"][:1], "rejected": []}, passing, [], "VOO")
        self.assertEqual((picked, dropped), ([], {"pick-rank-1": 1}))                       # ... nothing when absent.

        def ignoring(args, **kwargs):
            if "Role: the household's private banker" in args[args.index("--system-prompt") + 1]:
                return envelope(args, {"picks": [{"symbol": "ZZZ", "rank": "1", "reason": "x", "condition": "x"}], "rejected": []})
            return runner(args, **kwargs)
        with self.assertRaises(headless.HeadlessError) as caught:
            picks.review(self.view(), SPEC, HOUSEHOLD, today=AT.date(), secrets=[], executable=CLAUDE, runner=ignoring,
                         verifier=verifier, reader=reader)
        self.assertEqual(caught.exception.code, "no-picks")                                  # Recorded as a failure, no table.

    def test_last_weeks_candidates_are_considered_again_and_its_picks_verified_first(self):
        seen = {}

        def recording(args, **kwargs):
            prompt = args[args.index("--system-prompt") + 1]
            if "Role: product scout" in prompt:
                seen["must_consider"] = json.loads(kwargs["input"])["must_consider"]
            if "Role: the household's private banker" in prompt:
                seen["previous_picks"] = json.loads(kwargs["input"])["previous_picks"]
            return runner(args, **kwargs)
        prior = {"candidates": [{"symbol": "SPY"}, {"symbol": "ZZZ"}], "picks": [{"symbol": "SPY", "rank": 1, "reason": "r"}]}
        result = picks.review(self.view(), SPEC, HOUSEHOLD, today=AT.date(), secrets=[], executable=CLAUDE,
                              runner=recording, verifier=verifier, reader=reader, prior=prior)
        self.assertEqual(seen["must_consider"], ["SPY", "VOO", "ZZZ"])
        self.assertEqual(seen["previous_picks"], [{"rank": 1, "symbol": "SPY", "reason": "r"}])   # Continuity input.
        self.assertEqual([c["symbol"] for c in result["candidates"]][:2], ["VOO", "SPY"])

    def test_the_review_is_stored_reused_sized_and_explained(self):
        db = self.file_ledger()
        with closing(ledger.connect_read_only(db)) as conn:
            view = load_view(conn, as_of=AT, known_at=AT)
            self.assertEqual([s.key for s in picks.due(view, "markets", conn, AT)], ["core-etf"])
            self.assertEqual(picks.due(view, "tax", conn, AT), [])            # The tax day carries no trim proposal.
        outcome = picks.review_and_store(db, view, SPEC, HOUSEHOLD, now=AT, today=AT.date(), secrets=[],
                                         executable=CLAUDE, runner=runner, verifier=verifier, reader=reader)
        self.assertEqual((outcome["outcome"], outcome["picks"], outcome["verified"]), ("success", 2, 3))
        with closing(ledger.connect_read_only(db)) as conn:
            self.assertEqual(picks.due(view, "markets", conn, AT + timedelta(days=7)), [])   # Fresh: reused.
            self.assertEqual([s.key for s in picks.due(view, "markets", conn, AT + timedelta(days=9))], ["core-etf"])
            runs = conn.execute("SELECT use_case, count(*) FROM advice_runs GROUP BY use_case").fetchall()
            self.assertEqual(dict(runs), {"product-scout": 1, "product-verify": 4, "product-critique": 1, "product-select": 1,
                                          "product-select-repair": 1})
            data = build_report(conn, as_of=AT, known_at=AT, baseline=None, kind="markets", household=HOUSEHOLD)
        trim = next(p for p in data["advice"]["proposals"] if p.key == "concentration-trim")
        step = trim.figures["per_quarter"]
        self.assertEqual(trim.table[0], ("순위", "종목", "선정 이유", "분기 매수 수량(가격)", "시장·보수·가격 기준일"))
        self.assertEqual(trim.table[1][1], "VOO 펀드 (VOO)")
        self.assertEqual(trim.table[1][4], "미국 상장 · 보수 0.03% · 2026-10-17 종가 기준")
        self.assertEqual(trim.table[1][3], f"약 {short_won(step)}어치(환율 미상)")   # Rank 1 takes the step; no USD rate here.
        self.assertEqual(trim.table[2][3], "대안 · 1주 21,450원")   # Rank 2 is the alternative, priced only.
        with closing(ledger.connect_read_only(db)) as conn:
            stored = ledger.latest_product_review(conn, "core-etf", since=AT - timedelta(days=1))
            self.assertEqual(list(picks.load(conn, AT)), ["concentration-trim"])
            self.assertEqual(picks.load(conn, AT - timedelta(hours=1)), {})     # A rebuild as of before the review: none.
        priced = picks.pick_table(trim, stored["review"], {"KRW": Decimal(1), "USD": Decimal(1300)})
        self.assertEqual(priced.table[1][3], f"{int(step / 650_000)}주 × $500.00(약 65만 원)")
        page = render(data)[1]
        self.assertIn("옮길 곳 1~2순위", page)
        self.assertIn("1순위</span> VOO 펀드 (VOO)", page)   # Cards, one piece per line, not a four-column table.
        start = page.index("옮길 곳 1~2순위")
        self.assertNotIn("<th", page[start:page.index("출처:", start)])
        self.assertIn("https://issuer.example/360750", page)
        self.assertEqual(data["advice"]["pending_products"], [])
        notes = report_html.system_notes(data)
        self.assertTrue(any(n.startswith("상품 검토 · 코어 지수 ETF") for n in notes))
        self.assertTrue(any("SPY" in n and "mismatch" in n for n in notes))
        self.assertTrue(any("미선정 IVV" in n for n in notes))

    def test_a_failed_scout_is_recorded_and_the_report_goes_out_without_a_table(self):
        db = self.file_ledger()

        def failing(args, **kwargs):
            return subprocess.CompletedProcess(args, 1, "", "boom")
        outcome = picks.review_and_store(db, self.view(), SPEC, HOUSEHOLD, now=AT, today=AT.date(), secrets=[],
                                         executable=CLAUDE, runner=failing, verifier=verifier, reader=reader)
        self.assertEqual(outcome["outcome"], "failure")
        with closing(ledger.connect_read_only(db)) as conn:
            self.assertIsNone(ledger.latest_product_review(conn, "core-etf", since=AT - timedelta(days=1)))
            # Not retried for a while (the prepare step runs every 15 minutes), then due again.
            self.assertEqual(picks.due(self.view(), "markets", conn, AT + timedelta(hours=1)), [])
            self.assertEqual([s.key for s in picks.due(self.view(), "markets", conn, AT + timedelta(hours=7))], ["core-etf"])
            data = build_report(conn, as_of=AT, known_at=AT, baseline=None, kind="markets", household=HOUSEHOLD)
        self.assertEqual(next(p for p in data["advice"]["proposals"] if p.key == "concentration-trim").table, ())
        self.assertEqual(data["advice"]["pending_products"], ["concentration-trim"])   # The card says the table follows.
        self.assertIn("검토 팀의 확인이 끝나는 대로", render(data)[1])

    def file_ledger(self) -> Path:
        """The fixture household in a file ledger (reviews are stored by path), closed before the directory goes."""
        db = Path(self.enterContext(tempfile.TemporaryDirectory())) / "smith.db"
        with closing(ledger.connect(db)) as conn:
            doc = {"schema_version": 1, "import_id": "v1", "source": "manual", "mode": "patch", "as_of": AT.isoformat(),
                   "owners": [{"id": "self"}, {"id": "partner"}], "records": RECORDS}
            ledger.apply_import(conn, parse_import(json.dumps(doc)), recorded_at=AT)
            months = {(d["property_ref"], d["kind"], d["deal_date"][:7].replace("-", "")) for d in DEALS}
            ledger.replace_deals(conn, DEALS, months=months, properties={"home", "rental", "target:beta", "target:alpha"},
                                 fetched_at=AT)
            ledger.store_evidence(conn, [("ecos", CD, date(2026, 10, 1), "3.00", None), ("ecos", BASE, date(2026, 10, 1), "2.50", None),
                                         ("ecos", MARKET, date(2026, 9, 1), "3.95", None)], retrieved_at=AT)
        return db


class ScheduleTests(unittest.TestCase):
    def test_reviews_run_in_their_own_window_ahead_of_a_slot_never_in_the_prepare_hour(self):
        """Two to four hours before a slot (about 20 minutes of model calls); the last hour belongs to the prepare
        step and the report, and send-now never waits for a review (review 2026-10-20)."""
        slot = report_command.review_slot
        self.assertEqual(slot(CONFIG["reports"], "Asia/Seoul", MON - timedelta(hours=3)), MON)
        self.assertEqual(slot(CONFIG["reports"], "Asia/Seoul", MON - timedelta(hours=2)), MON)
        self.assertIsNone(slot(CONFIG["reports"], "Asia/Seoul", MON - timedelta(hours=1, minutes=59)))
        self.assertIsNone(slot(CONFIG["reports"], "Asia/Seoul", MON - timedelta(minutes=30)))
        self.assertIsNone(slot(CONFIG["reports"], "Asia/Seoul", MON - timedelta(hours=4, minutes=1)))


def setUpModule():  # noqa: N802 - unittest hook
    import fast_ledger
    fast_ledger.install()


def tearDownModule():  # noqa: N802 - unittest hook
    import fast_ledger
    fast_ledger.uninstall()
