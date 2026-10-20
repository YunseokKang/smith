"""The product review team (smith.picks, report design §26): four model roles directed and verified by code.
Every model call is a stub keyed on its role; every cited page is a stub fetch. Synthetic household (the
v3 fixture: one stock above the single-company limit)."""
import json
import subprocess
import tempfile
from contextlib import closing
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from smith import ledger, picks, report_html
from smith.importer import parse_import
from smith.payload import load_view
from smith.report_data import build_report
from smith.report_html import render
from test_v3_upgrade import AT, BASE, CD, DEALS, HOUSEHOLD, MARKET, RECORDS, Upgrade

CLAUDE = Path("claude.exe")


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


class ReviewTests(Upgrade):
    def view(self):
        return load_view(self.conn, as_of=AT, known_at=AT)

    def test_the_team_finds_verifies_critiques_and_selects_under_code_control(self):
        result = picks.review(self.view(), picks.CORE_ETF, HOUSEHOLD, today=AT.date(), secrets=[], executable=CLAUDE,
                              runner=runner, verifier=verifier)
        statuses = {c["symbol"]: c["status"] for c in result["candidates"]}
        self.assertEqual(statuses, {"360750": "verified", "VOO": "verified", "SPY": "mismatch", "IVV": "verified"})
        self.assertEqual(result["dropped"]["trimmed-stock"], 1)                 # 005930 never reaches the verifier.
        self.assertEqual((result["dropped"]["duplicate"], result["dropped"]["source-dead-url"]), (1, 1))
        self.assertEqual([(p["rank"], p["symbol"]) for p in result["picks"]], [(1, "360750"), (2, "VOO")])
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

    def test_a_mismatch_or_an_unreachable_page_is_no_verification(self):
        c = candidate("VOO", "US", "$500.00")
        self.assertEqual(picks.judge(c, checks([("identity", "match"), ("expense_ratio", "match"), ("price", "match")]),
                                     verifier)["status"], "verified")
        self.assertEqual(picks.judge(c, checks([("identity", "match"), ("expense_ratio", "match"), ("price", "unverified")]),
                                     verifier)["status"], "unverified")
        dead = checks([("identity", "match"), ("expense_ratio", "match"), ("price", "match")], url="https://issuer.example/dead")
        self.assertEqual(picks.judge(c, dead, verifier)["status"], "unverified")  # A match on a page code cannot reach.
        # The price alone may be confirmed by code on the scout's page (issuer pages that render by script).
        by_code = dict(c, source_price_matched=True)
        judged = picks.judge(by_code, checks([("identity", "match"), ("expense_ratio", "match"), ("price", "unverified")]), verifier)
        self.assertEqual(judged["status"], "verified")
        self.assertTrue(any(ch["field"] == "price" and "코드" in ch["found"] for ch in judged["checks"]))
        self.assertEqual(picks.judge(by_code, checks([("identity", "match"), ("expense_ratio", "match"), ("price", "mismatch")]),
                                     verifier)["status"], "mismatch")              # Never over a mismatch.

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
        result = picks.review(self.view(), picks.CORE_ETF, HOUSEHOLD, today=AT.date(), secrets=[], executable=CLAUDE,
                              runner=recording, verifier=verifier, prior=prior)
        self.assertEqual(seen["must_consider"], ["SPY", "VOO", "ZZZ"])
        self.assertEqual(seen["previous_picks"], [{"rank": 1, "symbol": "SPY", "reason": "r"}])   # Continuity input.
        self.assertEqual([c["symbol"] for c in result["candidates"]][:2], ["VOO", "SPY"])

    def test_the_review_is_stored_reused_sized_and_explained(self):
        db = self.file_ledger()
        with closing(ledger.connect_read_only(db)) as conn:
            view = load_view(conn, as_of=AT, known_at=AT)
            self.assertEqual([s.key for s in picks.due(view, "markets", conn, AT)], ["core-etf"])
            self.assertEqual(picks.due(view, "tax", conn, AT), [])            # The tax day carries no trim proposal.
        outcome = picks.review_and_store(db, view, picks.CORE_ETF, HOUSEHOLD, now=AT, today=AT.date(), secrets=[],
                                         executable=CLAUDE, runner=runner, verifier=verifier)
        self.assertEqual((outcome["outcome"], outcome["picks"], outcome["verified"]), ("success", 2, 3))
        with closing(ledger.connect_read_only(db)) as conn:
            self.assertEqual(picks.due(view, "markets", conn, AT + timedelta(days=7)), [])   # Fresh: reused.
            self.assertEqual([s.key for s in picks.due(view, "markets", conn, AT + timedelta(days=9))], ["core-etf"])
            runs = conn.execute("SELECT use_case, count(*) FROM advice_runs GROUP BY use_case").fetchall()
            self.assertEqual(dict(runs), {"product-scout": 1, "product-verify": 4, "product-critique": 1, "product-select": 1,
                                          "product-select-repair": 1})
            data = build_report(conn, as_of=AT, known_at=AT, baseline=None, kind="markets", household=HOUSEHOLD)
        trim = next(p for p in data["advice"]["proposals"] if p.key == "concentration-trim")
        self.assertEqual(trim.table[0], ("순위", "종목", "선정 이유", "분기 매수(가격)"))
        shares = int(trim.figures["per_quarter"] * Decimal("0.6") / Decimal(21_450))
        self.assertIn(f"{shares}주 × 21,450원, 2026-10-17 기준", trim.table[1][3])   # 60% of one step at the verified price.
        self.assertIn("VOO 펀드(VOO, 미국 상장) · 보수 0.03%", trim.table[2][1])
        self.assertEqual(trim.table[2][3], "약 525만 원어치(환율 미상)")        # The fixture ledger has no USD rate.
        with closing(ledger.connect_read_only(db)) as conn:
            stored = ledger.latest_product_review(conn, "core-etf", since=AT - timedelta(days=1))
        priced = picks.pick_table(trim, stored["review"], {"KRW": Decimal(1), "USD": Decimal(1300)})
        self.assertEqual(priced.table[2][3], "8주 × $500.00(약 650,000원), 2026-10-17 기준")   # 40% of one step.
        page = render(data)[1]
        self.assertIn("옮길 곳 1~2순위", page)
        self.assertIn("https://issuer.example/360750", page)
        notes = report_html.system_notes(data)
        self.assertTrue(any(n.startswith("상품 검토 · 코어 지수 ETF") for n in notes))
        self.assertTrue(any("SPY" in n and "mismatch" in n for n in notes))
        self.assertTrue(any("미선정 IVV" in n for n in notes))

    def test_a_failed_scout_is_recorded_and_the_report_goes_out_without_a_table(self):
        db = self.file_ledger()

        def failing(args, **kwargs):
            return subprocess.CompletedProcess(args, 1, "", "boom")
        outcome = picks.review_and_store(db, self.view(), picks.CORE_ETF, HOUSEHOLD, now=AT, today=AT.date(), secrets=[],
                                         executable=CLAUDE, runner=failing, verifier=verifier)
        self.assertEqual(outcome["outcome"], "failure")
        with closing(ledger.connect_read_only(db)) as conn:
            self.assertIsNone(ledger.latest_product_review(conn, "core-etf", since=AT - timedelta(days=1)))
            # Not retried for a while (the prepare step runs every 15 minutes), then due again.
            self.assertEqual(picks.due(self.view(), "markets", conn, AT + timedelta(hours=1)), [])
            self.assertEqual([s.key for s in picks.due(self.view(), "markets", conn, AT + timedelta(hours=7))], ["core-etf"])
            data = build_report(conn, as_of=AT, known_at=AT, baseline=None, kind="markets", household=HOUSEHOLD)
        self.assertEqual(next(p for p in data["advice"]["proposals"] if p.key == "concentration-trim").table, ())

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


def setUpModule():  # noqa: N802 - unittest hook
    import fast_ledger
    fast_ledger.install()


def tearDownModule():  # noqa: N802 - unittest hook
    import fast_ledger
    fast_ledger.uninstall()
