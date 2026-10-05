import json
import subprocess
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from smith import headless, ledger, narrative, research
from smith.importer import parse_import
from smith.payload import check_outbound
from smith.proposals import Proposal, materially_changed, snapshot
from smith.records import ImportBatch, ImportRejected, Observation
from smith.report_data import build_report
from smith.report_html import render

AT = datetime(2026, 10, 2, tzinfo=timezone.utc)
TODAY = date(2026, 10, 5)


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
ITEM = {"topic": "T1", "headline": "영통 아파트값 3.4% 상승", "detail": "문의 010-1234-5678", "published_on": "2026-10-01",
        "publisher": "한국부동산원", "source_title": "주간동향", "source_url": "https://www.reb.or.kr/x", "kind": "fact"}


def runner_returning(output, calls=None):
    def run(args, **kwargs):
        if calls is not None:
            calls.append((args, kwargs))
        envelope = {"type": "result", "subtype": "success", "is_error": False, "structured_output": output,
                    "total_cost_usd": 0.5}
        return subprocess.CompletedProcess(args, 0, json.dumps(envelope), "")
    return run


def block(text, refs=()):
    return {"text": text, "refs": list(refs)}


EMPTY = {"situation": block(""), "direction": block(""), "order": [], "proposal_notes": [], "insights": [], "watch": []}


class NarrativeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "smith.db"
        doc = {"schema_version": 1, "import_id": "m1", "source": "manual", "mode": "patch", "as_of": AT.isoformat(),
               "owners": [{"id": "self"}], "records": RECORDS}
        with closing(ledger.connect(self.db)) as conn:
            ledger.apply_import(conn, parse_import(json.dumps(doc)), recorded_at=AT)
            ledger.apply_import(conn, ImportBatch("fx", "toss", "snapshot", AT, ("self",), (), "x",
                                                  (Observation("USD", "fx_mid_rate", "KRW", "1400", AT),)), recorded_at=AT)
            ledger.store_evidence(conn, [("ecos", "817Y002/010502000", date(2026, 10, 1), "2.50", None)],
                                  retrieved_at=AT)

    def data(self):
        with closing(ledger.connect_read_only(self.db)) as conn:
            return build_report(conn, as_of=AT, known_at=AT, baseline=None, kind="monday")

    def brief(self):
        items = [{"ref": "R1", "topic": "T2", "headline": "영통 토지거래허가구역 지정", "detail": "2026-09-30부터 1년간 지정",
                  "published_on": "2026-09-30", "publisher": "수원시", "source_title": "보도자료",
                  "source_url": "https://www.suwon.go.kr/x", "kind": "policy", "tier": "official"},
                 {"ref": "R2", "topic": "T1", "headline": "영통 아파트값 3.4% 상승", "detail": "주간 기준",
                  "published_on": "2026-10-01", "publisher": "민간 플랫폼", "source_title": "기사",
                  "source_url": "https://news.example.com/77777", "kind": "fact", "tier": "secondary"}]
        return {"created_at": AT, "brief": {"gaps": [], "items": items}}

    def test_region_must_be_an_administrative_area(self):
        for region, ok in (("경기도 수원시 영통구", True), ("서울특별시 강남구", True), ("매탄동", False), ("래미안", False),
                           ("서울특별시 강남구 대치동", False), ("수원시 영통구 매탄동 123", False)):
            doc = {"schema_version": 1, "import_id": "m2", "source": "manual", "mode": "patch", "as_of": AT.isoformat(),
                   "owners": [{"id": "self"}], "records": [dict(RECORDS[1], id="home2", region=region)]}
            with self.subTest(region):
                if ok:
                    parse_import(json.dumps(doc))
                else:
                    with self.assertRaises(ImportRejected):
                        parse_import(json.dumps(doc))

    def test_research_topics_are_generalized_and_sources_are_checked(self):
        view = self.data()["view"]
        text = json.dumps([t.__dict__ for t in research.topics(view)], ensure_ascii=False)
        self.assertIn("경기도 수원시 영통구", text)
        for amount in ("10000000", "1500000000", "200000000", "5000000", "2000000", "1000"):
            self.assertNotIn(amount, text)               # No household amounts.
        self.assertNotIn("VOO", text)                    # Holdings stay out of web searches.
        check_outbound(text, secrets=["s3cret-value"])
        calls = []
        bad = [dict(ITEM, source_url="http://insecure.example/x"), dict(ITEM, source_url="https://a.kr/x y"),
               dict(ITEM, source_url="https://169.254.169.254/latest"), dict(ITEM, source_url="https://127.0.0.1/x"),
               dict(ITEM, topic="T99"), dict(ITEM, published_on="2027-01-01"), dict(ITEM, published_on=""),
               dict(ITEM, published_on="2020-01-01"), dict(ITEM, kind="rumor")]
        news = dict(ITEM, source_url="https://news.example.com/1", published_on="2025-10")
        result = research.run_research(view, today=TODAY, secrets=[], executable=Path("claude.exe"),
                                       runner=runner_returning({"items": [ITEM, news, *bad], "gaps": []}, calls),
                                       verifier=lambda items: (items, {}))   # Page checks: test_sources.
        args = calls[0][0]
        self.assertEqual(args[args.index("--tools") + 1], "WebSearch,WebFetch")
        self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(args[args.index("--disallowedTools") + 1], "mcp__*")
        self.assertNotIn("10000000", calls[0][1]["input"])
        items = result["brief"]["items"]
        self.assertEqual([(i["ref"], i["tier"]) for i in items], [("R1", "official"), ("R2", "secondary")])
        self.assertNotIn("010-1234-5678", items[0]["detail"])
        self.assertEqual(result["brief"]["dropped"], {"bad-url": 4, "unknown-topic": 1, "future-date": 1, "no-date": 1,
                                                      "stale": 1, "unknown-kind": 1})
        with self.assertRaises(headless.HeadlessError):
            headless.run("x", system_prompt="s", schema=research.BRIEF_SCHEMA, executable=Path("c.exe"), model=None,
                         budget_usd="1", timeout_seconds=1, tools=("Bash",), runner=runner_returning({}))

    def test_verification_rules(self):
        data = self.data()
        payload = narrative.build_payload(data["view"], data, self.brief())
        keys = [p["key"] for p in payload["proposals"]]
        cases = [
            ("situation", dict(EMPTY, situation=block("정부가 취득세를 폐지했습니다.")), "no-source"),
            ("situation", dict(EMPTY, situation=block("영통 집값은 반드시 오릅니다.", ["R2"])), "certainty"),
            ("situation", dict(EMPTY, situation=block("대출 승인은 보장됩니다.")), "certainty"),
            ("situation", dict(EMPTY, situation=block("고객님은 다주택자입니다.")), "home-count-asserted"),
            ("situation", dict(EMPTY, situation=block("영통이 규제지역이 되었습니다. 현행 법령 확인 필요.", ["R2"])),
             "unofficial-law-source"),
            ("situation", dict(EMPTY, situation=block("영통이 토지거래허가구역입니다.", ["R1"])), "law-unqualified"),
            ("situation", dict(EMPTY, situation=block("영통 아파트값이 -3.4% 하락했습니다.", ["R2"])), "ungrounded-number"),
            ("situation", dict(EMPTY, situation=block("영통 아파트값이 3.4% 올랐습니다.", ["R1"])), "ungrounded-number"),
            ("situation", dict(EMPTY, situation=block("기사 번호 77777을 보십시오.", ["R2"])), "ungrounded-number"),
            ("situation", dict(EMPTY, situation=block("R1에 따르면 지정되었습니다.", ["R1"])), "ref-in-prose"),
            ("proposal_notes[1]", dict(EMPTY, proposal_notes=[
                {"key": keys[0], "context": "영통 아파트값이 3.4% 올랐습니다.", "refs": ["R2"]},
                {"key": keys[0], "context": "다시 씁니다.", "refs": ["R2"]}]), "duplicate"),
            ("insights[0]", dict(EMPTY, insights=[{"title": "변화", "body": "지정", "implication": "영향", "refs": []}]),
             "no-source"),
        ]
        for path, output, reason in cases:
            with self.subTest(reason=reason, path=path):
                _, dropped = narrative.verify(output, payload)
                self.assertIn(reason, [d.split(":")[1] for d in dropped if d.startswith(path)])
        good = dict(EMPTY, situation=block("영통 아파트값이 3.4% 올랐습니다(전망 아님, 지난주 기준).", ["R2"]),
                    direction=block("영통이 토지거래허가구역으로 지정되어 매도 일정이 길어질 수 있습니다(현행 법령 확인 필요).",
                                    ["R1"]))
        verified, dropped = narrative.verify(good, payload)
        self.assertEqual(dropped, [])
        self.assertTrue(verified["situation"]["text"] and verified["direction"]["text"])
        self.assertEqual(sorted(verified["order"]), sorted(keys))

    def test_repair_keeps_passed_blocks_and_records_every_call(self):
        data = self.data()
        first = dict(EMPTY, situation=block("부족분은 9.99억 원입니다."),
                     watch=[{"item": "기준금리 결정", "why": "변동금리 대출 이자에 영향", "refs": ["R1"]}])
        second = dict(EMPTY, situation=block("현금 여유를 먼저 채우실 때입니다."),
                      watch=[{"item": "다른 문장", "why": "바뀐 이유", "refs": []}])
        outputs, inputs, audits = [first, second], [], []

        def run(args, **kwargs):
            inputs.append(json.loads(kwargs["input"]))
            return runner_returning(outputs[len(inputs) - 1])(args, **kwargs)

        def audit(use_case, payload, outcome, code, cost, content):
            audits.append((use_case, outcome, content))
            return True
        result = narrative.write(data["view"], data, self.brief(), secrets=[], executable=Path("claude.exe"),
                                 runner=run, audit=audit)
        self.assertEqual(result["output"]["situation"]["text"], "현금 여유를 먼저 채우실 때입니다.")
        self.assertEqual(result["output"]["watch"], first["watch"])            # Passed blocks are kept exactly.
        self.assertEqual(result["dropped"], [])
        self.assertIn("9.99", inputs[1]["rejected"][0]["reason"])
        self.assertIn("ownership_notes", inputs[0])
        self.assertEqual([a[0] for a in audits], ["report-narrative", "report-narrative-repair"])
        self.assertEqual(audits[0][2]["raw"], first)                           # Raw output is audited too.

    def test_a_block_failing_twice_is_restored_from_the_output_its_path_indexes(self):
        # The repair returns fewer insights than the first output; the still-failing second insight must come
        # back from the first output with its memo, not vanish (or be swapped for another block).
        data = self.data()
        good = {"title": "금리 흐름", "body": "시장 금리가 움직이고 있습니다.",
                "implication": "변동금리 대출 이자에 영향을 줄 수 있습니다.", "refs": ["R1"]}
        unsourced = {"title": "정책 변화", "body": "정부 정책이 바뀌었습니다.", "implication": "주의가 필요합니다.",
                     "refs": []}
        outputs = [dict(EMPTY, insights=[good, unsourced]), dict(EMPTY, insights=[dict(unsourced, title="다른 제목")])]
        calls = []

        def run(args, **kwargs):
            calls.append(1)
            return runner_returning(outputs[len(calls) - 1])(args, **kwargs)
        result = narrative.write(data["view"], data, self.brief(), secrets=[], executable=Path("claude.exe"), runner=run)
        self.assertEqual(result["dropped"], ["insights[1]:no-source"])
        titles = [(item["title"], item.get("memo")) for item in result["output"]["insights"]]
        self.assertEqual(titles, [("금리 흐름", None), ("정책 변화", narrative.MEMOS["no-source"])])

    def test_narrate_uses_the_pinned_brief_and_falls_back_only_for_the_same_topics(self):
        view = self.data()["view"]
        topics = [{"ref": t.ref, "title": t.title, "question": t.question} for t in research.topics(view)]
        with closing(ledger.connect(self.db)) as conn:
            ledger.record_research(conn, brief_id="same", created_at=AT, topics=topics, outcome="success",
                                   model="fable", brief=self.brief()["brief"])
            ledger.record_research(conn, brief_id="other", created_at=AT + timedelta(hours=1),
                                   topics=topics[:1], outcome="success", model="fable", brief=self.brief()["brief"])
        brief, reused = narrative.pinned_brief(self.db, view, now=AT + timedelta(hours=2), brief_id="other")
        self.assertEqual((brief["brief_id"], reused), ("other", False))         # Pinned wins.
        brief, reused = narrative.pinned_brief(self.db, view, now=AT + timedelta(hours=2), brief_id=None)
        self.assertEqual((brief["brief_id"], reused), ("same", True))          # Fallback: same topics only.
        data = self.data()
        key = data["advice"]["proposals"][0].key
        output = dict(EMPTY, situation=block("지금은 현금 여유를 먼저 채우실 때입니다."),
                      proposal_notes=[{"key": key, "context": "영통이 토지거래허가구역으로 지정되었습니다(현행 법령 확인 필요).",
                                       "refs": ["R1"]}],
                      insights=[{"title": "영통 토지거래허가구역", "body": "지정되었습니다.",
                                 "implication": "매도·매수 일정이 길어질 수 있습니다(현행 법령 확인 필요).", "refs": ["R1"]}])
        narrative.narrate(self.db, data["view"], data, now=AT, secrets=[], brief_id="same",
                          executable=Path("claude.exe"), runner=runner_returning(output))
        self.assertEqual(data["narrative"]["status"]["outcome"], "success")
        self.assertEqual(data["advice"]["proposals"][0].links[0][1], "https://www.suwon.go.kr/x")
        _, html = render(data)
        for text in ("Smith의 판단", "이번 주 눈여겨볼 변화", "시장·정책 맥락", 'href="https://www.suwon.go.kr/x"',
                     "직접 식별자 없이"):
            self.assertIn(text, html)
        with closing(ledger.connect_read_only(self.db)) as conn:
            uses = [row[0] for row in conn.execute("SELECT use_case FROM advice_runs")]
        self.assertEqual(uses, ["report-narrative"])

    def test_a_failed_narrative_is_audited_and_leaves_the_deterministic_report(self):
        data = self.data()
        failing = lambda args, **kwargs: subprocess.CompletedProcess(args, 1, "{}", "")  # noqa: E731
        narrative.narrate(self.db, data["view"], data, now=AT, secrets=[], executable=Path("claude.exe"), runner=failing)
        self.assertEqual(data["narrative"]["status"]["outcome"], "failure")
        _, html = render(data)
        self.assertIn("계산 결과만으로 작성했습니다", html)
        self.assertNotIn("Smith의 판단", html)
        with closing(ledger.connect_read_only(self.db)) as conn:
            rows = list(conn.execute("SELECT use_case, outcome FROM advice_runs"))
        self.assertEqual(rows, [("report-narrative", "failure")])

    def deliver(self, report_id, proposals, at=AT):
        with closing(ledger.connect(self.db)) as conn:
            ledger.claim_report(conn, report_id=report_id, slot=None, kind="monday", trigger="manual", created_at=at,
                                as_of=at, baseline=None, missed_slots=[])
            ledger.finish_report(conn, report_id=report_id, status="sent", finished_at=at,
                                 shown_proposals=[snapshot(p) for p in proposals])

    def test_proposal_history_is_append_only_with_rule_lifecycles(self):
        first = self.data()["advice"]["proposals"]
        self.deliver("r1", first)
        again = {p.key: p for p in self.data()["advice"]["proposals"]}
        key = first[0].key
        self.assertEqual((again[key].times_shown, again[key].renewed), (1, False))
        with closing(ledger.connect(self.db)) as conn:
            self.assertTrue(ledger.decide_proposal(conn, key=key, status="declined", decided_at=AT, note="later"))
            self.assertFalse(ledger.decide_proposal(conn, key="never-shown", status="done", decided_at=AT))
        advice = self.data()["advice"]
        self.assertNotIn(key, [p.key for p in advice["proposals"]])
        self.assertEqual([e["key"] for e in advice["decided"]], [key])
        with closing(ledger.connect(self.db)) as conn:
            ledger.decide_proposal(conn, key=key, status="accepted", decided_at=AT)
            events = [(e["key"], e["event"], e["note"]) for e in ledger.proposal_events(conn) if e["key"] == key]
        self.assertEqual([p.key for p in self.data()["advice"]["in_progress"]], [key])
        # Nothing is overwritten: every decision stays in the event log.
        self.assertEqual(events, [(key, "accepted", None), (key, "declined", "later"), (key, "shown", None)])

    def test_material_change_renews_a_proposal(self):
        base = Proposal("lease-return", 2, "t", "w", "e", "r", "t", "r", "c",
                        figures={"amount": Decimal(300_000_000), "accumulated": Decimal(250_000_000),
                                 "months": Decimal(20)},
                        identity="lease-return:2028-06-30", watch=("amount", "accumulated"))
        anchor = snapshot(base)["figures"]

        def moved(**figures):
            return materially_changed(replace(base, figures={**base.figures, **figures}), anchor)
        self.assertFalse(moved(months=Decimal(19)))                    # A month passing is not a new proposal.
        self.assertFalse(moved(accumulated=Decimal(270_000_000)))      # +8%: within the 20% default.
        self.assertTrue(moved(accumulated=Decimal(320_000_000)))       # +28%.

if __name__ == "__main__":
    unittest.main()
