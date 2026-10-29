import json
import subprocess
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from smith import ledger, qa, qa_lookup
from smith.importer import parse_import

import fast_ledger

BEFORE_RECORD = datetime(2026, 9, 20, 3, 0, tzinfo=timezone.utc)
AFTER_RECORD = datetime(2026, 10, 10, 3, 0, tzinfo=timezone.utc)
REPORT = datetime(2026, 10, 20, 3, 0, tzinfo=timezone.utc)
URL = "https://dart.example.kr/disclosure/1"
NO_LOOKUP = {"needed": "no", "holding": "", "topic": "none", "subject": ""}


def lot(record_id, symbol, quantity, account, revision=1, owner="self", **extra):
    return {"id": record_id, "kind": "asset", "owner_id": owner, "effective_at": "", "revision": revision,
            "status": "active", "category": "stock", "account_type": account, "currency": "KRW",
            "value": str(quantity * 10_000), "valuation_method": "market", "liquidity": "days", "symbol": symbol,
            "market": "KR", "quantity": str(quantity), "unit_price": "10000", **extra}


def checked(items, tier=None):
    return [dict(item, check={"status": "matched", "found": ["361"]}, ref=f"R{n}") for n, item in enumerate(items, 1)], {}


def declared(symbol, currency="KRW"):
    return {"symbol": symbol, "per_share": Decimal("361"), "currency": currency, "kind": "quarterly",
            "record_date": date(2026, 9, 30), "pay_date": date(2026, 11, 27), "ref": "R1", "checked": True}


class LookupTests(unittest.TestCase):
    """Synthetic household. On the record date the client holds 400 + 1 common shares in two general accounts and the
    partner 1; by the report the client has 415 in the first account and 5 preferred shares bought into an ISA after
    the record date. A Hermes-managed lot is never counted."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "smith.db"
        imports = [(BEFORE_RECORD, [lot("lot-a", "123450", 400, "brokerage"), lot("lot-q", "123450", 1, "brokerage"),
                                    lot("lot-p", "123450", 1, "brokerage", owner="partner"),
                                    lot("lot-h", "123450", 10, "brokerage", managed_by="hermes")]),
                   (AFTER_RECORD, [lot("lot-a", "123450", 415, "brokerage", revision=2),
                                   lot("lot-b", "123455", 5, "isa")])]
        with closing(ledger.connect(self.db)) as conn:
            for n, (at, records) in enumerate(imports, 1):
                doc = {"schema_version": 1, "import_id": f"m{n}", "source": "manual", "mode": "patch",
                       "as_of": at.isoformat(), "owners": [{"id": "self"}, {"id": "partner"}],
                       "records": [dict(r, effective_at=at.isoformat()) for r in records]}
                ledger.apply_import(conn, parse_import(json.dumps(doc)), recorded_at=at)
        self.data = qa.rebuild(self.db, None, now=REPORT)

    def test_only_names_from_codes_list_leave_and_code_writes_the_input(self):
        cases = [({"holding": "123450", "topic": "dividend"}, None),
                 ({"holding": "999999", "topic": "dividend"}, "특정하지"),
                 ({"holding": "123450", "topic": "buy"}, "특정하지"),
                 ({"topic": "public", "subject": "kr-rate"}, None),
                 # Free text never leaves, whatever it says (review 2026-10-29).
                 ({"topic": "public", "subject": "연봉 7천만원 맞벌이 양도세"}, "공개 주제"),
                 ({"topic": "public", "subject": ""}, "공개 주제")]
        for asked, refusal in cases:
            with self.subTest(asked=asked):
                request, reason = qa_lookup.gate({"holding": "", "subject": "", **asked}, self.data)
                self.assertEqual(request is None, refusal is not None)
                if refusal:
                    self.assertIn(refusal, reason)
        request, _ = qa_lookup.gate({"holding": "123450", "topic": "dividend"}, self.data)
        sent = qa_lookup.research_input(request, date(2026, 10, 29))
        self.assertIn("123455", sent)                       # The preferred share the client also holds.
        for private in ("400", "415", "isa", "brokerage", "4150000", "partner"):
            self.assertNotIn(private, sent)
        request, _ = qa_lookup.gate({"holding": "", "topic": "public", "subject": "kr-rate"}, self.data)
        self.assertEqual(json.loads(qa_lookup.research_input(request, date(2026, 10, 29)))["question"],
                         qa_lookup.PUBLIC_SUBJECTS["kr-rate"][1])

    def test_a_dividend_counts_only_when_its_source_states_the_amount(self):
        items = [{"ref": "R7", "source_url": URL, "detail": "보통주·우선주 1주당 361원, 기준일 9월 30일",
                  "status": "matched", "page_numbers": ["30", "361"]},
                 {"ref": "R8", "source_url": URL + "x", "detail": "1주당 361원", "status": "mismatch", "page_numbers": []},
                 {"ref": "R9", "source_url": URL + "y", "detail": "우선주 1주당 361원", "status": "reachable",
                  "page_numbers": None},                                     # The page could not be read.
                 # Matched on its dates only: the page says another amount (review 2026-10-29).
                 {"ref": "R10", "source_url": URL + "z", "detail": "1주당 362원, 기준일 9월 30일", "status": "matched",
                  "page_numbers": ["30"]}]
        entry = {"symbol": "123450", "per_share": "361", "currency": "KRW", "kind": "quarterly",
                 "record_date": "2026-09-30", "pay_date": "2026-11-27", "source_url": URL}
        raw = [dict(entry, per_share="362"),                          # Not what the finding says.
               dict(entry, per_share="30"),                           # A date's number, not an amount.
               dict(entry, per_share="362", source_url=URL + "z"),    # Not on the page.
               dict(entry, symbol="777777"),                          # Not asked for.
               dict(entry, symbol="123455", source_url=URL + "x"),    # The page contradicted the finding.
               entry, dict(entry, symbol="123455", source_url=URL + "y")]
        found, unproven = qa_lookup.dividends(raw, items, {"123450", "123455"})
        self.assertEqual([(d["symbol"], d["per_share"], d["ref"], d["checked"]) for d in found],
                         [("123450", Decimal("361"), "R7", True), ("123455", Decimal("361"), "R9", False)])
        self.assertEqual(unproven, [])
        # Only dates the cited finding writes are used: R7 writes the record date ("9월 30일"), R9 neither date.
        self.assertEqual([(d["record_date"], d["pay_date"]) for d in found],
                         [(date(2026, 9, 30), None), (None, None)])
        # One page may carry a finding per share class; each is proved by its own, and a class no finding proves is
        # reported so the total is not shown as complete.
        page = [{"ref": "R1", "source_url": URL, "detail": "보통주 1주당 361원", "status": "matched", "page_numbers": ["361", "362"]},
                {"ref": "R2", "source_url": URL, "detail": "우선주 1주당 362원", "status": "matched", "page_numbers": ["361", "362"]}]
        found, unproven = qa_lookup.dividends([entry, dict(entry, symbol="123455", per_share="362"),
                                               dict(entry, symbol="123457", per_share="999")], page,
                                              {"123450", "123455", "123457"})
        self.assertEqual([(d["symbol"], d["ref"]) for d in found], [("123450", "R1"), ("123455", "R2")])
        self.assertEqual(unproven, ["123457"])

    def test_the_dividend_is_computed_per_account_and_owner_from_the_record_date(self):
        found = qa_lookup.estimates(self.db, self.data, [declared("123450"), declared("123455")], known_at=REPORT)
        rows = {(r["symbol"], r["owner"], r["account"]): r for r in found["items"]}
        mine = rows[("123450", "self", "general")]
        # 401 shares on the record date (not the report's 415+1), withheld per account: 144,400 won withholds 20,210 and
        # 2,020 (each cut to 10 won); 361 won withholds 50 and 0. The partner's share is the partner's row.
        self.assertEqual((mine["quantity"], mine["quantity_basis"], mine["gross"], mine["withheld"], mine["net"],
                          mine["hermes_lots_excluded"]), ("401", "record_date", "144761", "22280", "122481", True))
        self.assertEqual(rows[("123450", "household_member_1", "general")]["net"], "311")
        preferred = rows[("123455", "self", "isa")]          # Bought after the record date: the report's count, said so.
        self.assertEqual((preferred["quantity_basis"], preferred["gross"], preferred["withheld"]), ("report_date", "1805", "0"))
        self.assertEqual(found["total"], {"gross": "146927", "withheld": "22330", "net": "124597", "pay_dates": ["2026-11-27"]})
        notes = qa_lookup.memos({"status": "done", "reason": None}, found)
        self.assertTrue(any("보고서 기준 수량" in n for n in notes) and any("Hermes" in n for n in notes))
        # Lots that cannot be counted (here: another currency than the dividend's) leave the total unknown.
        odd = qa_lookup.estimates(self.db, self.data, [declared("123450", "USD")], known_at=REPORT)
        self.assertEqual((odd["total"], odd["uncounted_lots"]), (None, 3))
        self.assertTrue(any("합계는 내지 않았습니다" in n for n in qa_lookup.memos({"status": "done"}, odd)))

    def test_an_answer_looks_up_a_declared_dividend_without_sending_household_data(self):
        calls = []
        research = {"findings": [{"claim": "가상전자 보통주 1주당 361원 분기배당, 기준일 2026-09-30, 지급일 2026-11-27",
                                  "source_title": "현금·현물배당결정", "source_url": URL, "published_on": "2026-10-29",
                                  "publisher": "DART"}],
                    "dividends": [{"symbol": "123450", "per_share": "361", "currency": "KRW", "kind": "quarterly",
                                   "record_date": "2026-09-30", "pay_date": "2026-11-27", "source_url": URL}],
                    "gaps": []}
        asking = {"answer": {"text": "공시를 찾아보겠습니다.", "refs": []}, "follow_up": [], "remember": [],
                  "lookup": {"needed": "yes", "holding": "123450", "topic": "dividend", "subject": ""}}
        answered = {"answer": {"text": "11월 27일에 세금을 떼고 약 12만 원이 들어올 예정입니다(현행 법령 확인 필요).",
                               "refs": ["R1", "tax"]}, "follow_up": [], "remember": [], "lookup": NO_LOOKUP}

        def runner(args, **kwargs):
            web = args[args.index("--tools") + 1] == "WebSearch,WebFetch"
            calls.append((web, kwargs["input"]))
            return subprocess.CompletedProcess(args, 0, json.dumps({
                "subtype": "success", "is_error": False, "total_cost_usd": 0.1,
                "structured_output": research if web else answered if len(calls) == 3 else asking}), "")
        question = qa.Question("q1", "t1", None, REPORT, "[Smith] 보고", "가상전자 배당 얼마 받아?", "<q1@mail>", "")
        result = qa.answer(self.db, self.data, question, now=REPORT, secrets=[], executable=Path("claude.exe"),
                           runner=runner, verifier=checked)
        self.assertEqual([web for web, _ in calls], [False, True, False])
        self.assertEqual(json.loads(calls[1][1])["company"], "123450")   # A manual name stays local: the symbol goes.
        for private in ("400", "415", "brokerage", "hermes", "partner", "배당 얼마"):
            self.assertNotIn(private, calls[1][1])
        second = json.loads(calls[2][1])
        self.assertEqual(second["dividend_estimates"]["total"]["net"], "122792")   # 122,481 + the partner's 311.
        self.assertEqual(second["lookup"]["refs"], ["R1"])
        self.assertTrue(result["looked_up"])
        self.assertIn("약 12만 원", result["text"])
        self.assertFalse(any("확인되지 않은" in m for m in result["memos"]), result["memos"])   # Grounded in code's figure.
        html, _ = qa.render(question, result, self.data)
        self.assertIn(URL, html)                                                          # The source is linked.


class DartLookupTests(unittest.TestCase):
    """The same household, with an OpenDART key: a dividend question is answered from the filing itself."""

    setUp = LookupTests.setUp

    def test_a_dividend_question_is_answered_from_the_filing_without_a_web_call(self):
        from test_dart import FakeDart
        calls, fake = [], FakeDart()
        asking = {"answer": {"text": "공시를 찾아보겠습니다.", "refs": []}, "follow_up": [], "remember": [],
                  "lookup": {"needed": "yes", "holding": "123450", "topic": "dividend", "subject": ""}}
        answered = {"answer": {"text": "11월 27일에 세금을 떼고 약 12만 원이 들어올 예정입니다(현행 법령 확인 필요).",
                               "refs": ["R1", "tax"]}, "follow_up": [], "remember": [], "lookup": NO_LOOKUP}

        def runner(args, **kwargs):
            calls.append(args[args.index("--tools") + 1])
            return subprocess.CompletedProcess(args, 0, json.dumps({
                "subtype": "success", "is_error": False, "total_cost_usd": 0.1,
                "structured_output": answered if len(calls) == 2 else asking}), "")
        question = qa.Question("q1", "t1", None, REPORT, "[Smith] 보고", "가상전자 배당 얼마 받아?", "<q1@mail>", "")
        result = qa.answer(self.db, self.data, question, now=REPORT, secrets=["k" * 40], executable=Path("claude.exe"),
                           runner=runner, verifier=checked, dart_key="k" * 40, dart_fetch=fake)
        self.assertEqual(calls, ["", ""])                                    # Two answer calls, no web call.
        self.assertEqual([path for path, _ in fake.calls], ["corpCode.xml", "list.json", "document.xml", "document.xml"])
        self.assertTrue(all(params["crtfc_key"] == "k" * 40 for _, params in fake.calls))
        with closing(ledger.connect_read_only(self.db)) as conn:
            second = json.loads(conn.execute("SELECT payload FROM advice_runs WHERE use_case = 'mail-answer-looked-up'")
                                .fetchone()[0])
        item = next(i for i in second["brief"]["items_untrusted"] if i["ref"] == "R1")
        self.assertEqual((item["tier"], item["publisher"]), ("official", "금융감독원 전자공시(DART)"))
        self.assertIn("보통주 1주당 361원", item["detail"])
        rows = {(r["symbol"], r["owner"], r["account"]): r for r in second["dividend_estimates"]["items"]}
        self.assertEqual(rows[("123455", "self", "isa")]["gross"], "1805")      # The preferred share's 종류주식 amount.
        self.assertTrue(all(r["source_numbers_checked"] and r["pay_date"] == "2026-11-27" for r in rows.values()))
        self.assertEqual(result["memos"], ["배당 기준일의 보유 수량이 원장에 없어, 보고서 기준 수량으로 계산했습니다.",
                                           "Hermes가 운용하는 계좌의 같은 종목 배당은 계산에 넣지 않았습니다."])
        html, _ = qa.render(question, result, self.data)
        self.assertIn("https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20261029800101", html)


def setUpModule():  # noqa: N802 - unittest hook
    fast_ledger.install()


def tearDownModule():  # noqa: N802 - unittest hook
    fast_ledger.uninstall()


if __name__ == "__main__":
    unittest.main()
