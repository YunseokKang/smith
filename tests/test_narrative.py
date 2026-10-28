import json
import subprocess
import tempfile
import threading
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from smith import compare, headless, ledger, memory, narrative, report_html, research
from smith.importer import parse_import
from smith.payload import check_outbound
from smith.proposals import Proposal, materially_changed, snapshot
from smith.records import ImportBatch, ImportRejected, Observation
from smith.report_data import build_report
from smith.report_html import render

import fast_ledger

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


def system_prompt(args):
    """The system prompt of a headless call, read from the file it travels in."""
    return Path(args[args.index("--system-prompt-file") + 1]).read_text(encoding="utf-8")


def by_prompt(market, holdings, calls=None):
    """Answer the market research call and the holdings research call (told apart by their prompt)."""
    def run(args, **kwargs):
        prompt = system_prompt(args)
        output = holdings if prompt == research.HOLDINGS_PROMPT else market
        if isinstance(output, Exception):
            return subprocess.CompletedProcess(args, 1, "{}", "")
        return runner_returning(output, calls)(args, **kwargs)
    return run


def block(text, refs=()):
    return {"text": text, "refs": list(refs)}


EMPTY = {"situation": block(""), "direction": block(""), "portfolio": dict(block(""), principles=[]), "order": [],
         "proposal_notes": [], "holdings": [], "insights": [], "watch": []}


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
            self.assertNotIn(amount, text)               # No household amounts, quantities or values.
        # Holdings go out by name, symbol and market only (user decision 2026-10-11), as their own topics.
        self.assertEqual([t.title for t in research.topics(view) if t.ref.startswith("H")], ["VOO"])
        check_outbound(text, secrets=["s3cret-value"])
        calls = []
        bad = [dict(ITEM, source_url="http://insecure.example/x"), dict(ITEM, source_url="https://a.kr/x y"),
               dict(ITEM, source_url="https://169.254.169.254/latest"), dict(ITEM, source_url="https://127.0.0.1/x"),
               dict(ITEM, topic="T99"), dict(ITEM, published_on="2027-01-01"), dict(ITEM, published_on=""),
               dict(ITEM, published_on="2020-01-01"), dict(ITEM, kind="rumor")]
        news = dict(ITEM, source_url="https://news.example.com/1", published_on="2025-10")
        company = dict(ITEM, topic="H1", headline="VOO 보수 0.03%", source_url="https://investor.vanguard.com/x")
        result = research.run_research(view, today=TODAY, secrets=[], executable=Path("claude.exe"),
                                       runner=by_prompt({"items": [ITEM, news, *bad], "gaps": []},
                                                        {"items": [dict(company, detail="가" * 3000)] + [company] * 4,
                                                         "gaps": []}, calls),
                                       verifier=lambda items: (items, {}))   # Page checks: test_sources.
        self.assertEqual(len(calls), 2)                  # Market and holdings researched separately.
        for args, kwargs in calls:
            self.assertEqual(args[args.index("--tools") + 1], "WebSearch,WebFetch")
            self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")
            self.assertEqual(args[args.index("--disallowedTools") + 1], "mcp__*")
            self.assertNotIn("10000000", kwargs["input"])
        items = result["brief"]["items"]
        # A long detail is trimmed, not fatal to the call; a holding keeps its first three findings.
        self.assertEqual([(i["ref"], i["topic"], i["tier"]) for i in items],
                         [("R1", "T1", "official"), ("R2", "T1", "secondary"), ("R3", "H1", "secondary"),
                          ("R4", "H1", "secondary"), ("R5", "H1", "secondary")])
        self.assertEqual(len(items[2]["detail"]), 1200)
        self.assertNotIn("010-1234-5678", items[0]["detail"])
        # One call failing keeps the other's findings and names the gap; both failing is a failed brief.
        partial_result = research.run_research(view, today=TODAY, secrets=[], executable=Path("claude.exe"),
                                               runner=by_prompt({"items": [ITEM], "gaps": []}, RuntimeError()),
                                               verifier=lambda items: (items, {}))
        self.assertEqual(len(partial_result["brief"]["items"]), 1)
        self.assertTrue(partial_result["brief"]["gaps"][0].startswith("보유 종목 조사 실패"))
        with self.assertRaises(headless.HeadlessError):
            research.run_research(view, today=TODAY, secrets=[], executable=Path("claude.exe"),
                                  runner=by_prompt(RuntimeError(), RuntimeError()), verifier=lambda items: (items, {}))
        self.assertEqual(result["brief"]["dropped"], {"bad-url": 4, "unknown-topic": 1, "future-date": 1, "no-date": 1,
                                                      "stale": 1, "unknown-kind": 1, "over-topic-limit": 2})
        with self.assertRaises(headless.HeadlessError):
            headless.run("x", system_prompt="s", schema=research.BRIEF_SCHEMA, executable=Path("c.exe"), model=None,
                         budget_usd="1", timeout_seconds=1, tools=("Bash",), runner=runner_returning({}))

    def test_the_longest_system_prompts_travel_in_a_file_within_the_windows_command_line_limit(self):
        from smith import team
        for prompt in (narrative.SYSTEM_PROMPT, team.EDITOR_PROMPT):   # Inline, these failed to launch (2026-10-28).
            calls = []
            headless.run("x", system_prompt=prompt, schema=narrative.NARRATIVE_SCHEMA, executable=Path("c.exe"),
                         model="fable", budget_usd="5", timeout_seconds=1, runner=lambda args, **kw: (
                             calls.append(system_prompt(args)), runner_returning(EMPTY)(args, **kw))[1])
            self.assertEqual(calls, [prompt])
        with self.assertRaises(headless.HeadlessError) as raised:   # A schema alone that long is refused, not launched.
            headless.run("x", system_prompt="s", schema={"type": "string", "description": "x" * 33000},
                         executable=Path("c.exe"), model=None, budget_usd="1", timeout_seconds=1,
                         runner=runner_returning(""))
        self.assertEqual(raised.exception.code, "command-too-long")

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

    def test_first_reader_rewrites_only_the_marked_blocks_whose_rewrite_passes(self):
        data = self.data()
        watch = [{"item": "기준금리 결정", "why": "변동금리 대출 이자에 영향", "refs": ["R1"]}]
        # The direction reads as translated (를 통해): sent back once, the repair leaves it, so it goes to the reader
        # without a memo, who marks it; the rewrite rewords it and is tidied on the way (report design §34).
        first = dict(EMPTY, situation=block("현금 여유를 먼저 채우실 때입니다."),
                     direction=block("유동성 버퍼 확보를 통해 안전을 지키십시오."), watch=watch)
        rewrite = dict(first, situation=block("부족분은 9.99억 원입니다."),          # Ungrounded: not applied.
                       direction=block("바로 쓸 수 있는 현금을 먼저 늘리는 쪽이 낫다고 보여집니다."),
                       watch=[{"item": "다른 문장", "why": "바뀐 이유", "refs": []}])  # Not marked: not applied.
        outputs, inputs, audits, shown = [first, first, rewrite], [], [], []       # The repair changes nothing.

        def run(args, **kwargs):
            inputs.append(json.loads(kwargs["input"]))
            return runner_returning(outputs[len(inputs) - 1])(args, **kwargs)

        def first_reader(letter):
            self.assertIsNone(letter["edition"])                               # The full Monday report.
            shown.extend(letter["blocks"])
            notes = [{"path": p, "quote": q, "problem": kind, "why": "어렵습니다.", "suggestion": "풀어 쓰기"}
                     for p, q, kind in (("situation", "현금 여유", "jargon"), ("direction", "유동성 버퍼", "unnatural"))]
            return {"notes": notes, "raw_notes": 2, "overall": "대체로 읽힙니다.", "usage": None, "model": "gpt-test"}

        def audit(use_case, payload, outcome, code, cost, content):
            audits.append(use_case)
            return True
        result = narrative.write(data["view"], data, self.brief(), secrets=[], executable=Path("claude.exe"),
                                 runner=run, audit=audit, first_reader=first_reader)
        out = result["output"]
        self.assertEqual(out["direction"]["text"], "바로 쓸 수 있는 현금을 먼저 늘리는 쪽이 낫다고 보입니다.")
        self.assertEqual(out["situation"]["text"], "현금 여유를 먼저 채우실 때입니다.")
        self.assertEqual(out["watch"], watch)
        self.assertEqual(result["reader"]["outcome"], "rewritten")
        self.assertEqual(result["reader"]["applied"], ["direction"])
        # The record of the Korean corrections (report design §34): what was tidied, flagged, left as it goes out
        # (nothing: the reader's rewrite reworded it, Codex review 2026-10-27), and marked by the reader.
        self.assertEqual(result["korean"], {"tidied": [{"path": "direction", "kind": "double-passive", "before": "보여집", "after": "보입"}],
                                            "flagged": [{"path": "direction", "kind": "calque", "words": "를 통해"}],
                                            "remaining": [],
                                            "reader_notes": [{"path": "direction", "quote": "유동성 버퍼"}]})
        self.assertNotIn("unnatural", result["reader"])
        self.assertEqual(result["memos"], {})                                        # Soft: no memo on the direction.
        self.assertIn("direction", [b["path"] for b in shown])                     # ... so the reader saw it.
        self.assertTrue(inputs[1]["rejected"][0]["reason"].startswith("reads as translated"))
        self.assertEqual(len(inputs[2]["reader_notes"]), 2)
        self.assertEqual(inputs[2]["previous_output"]["situation"]["text"], "현금 여유를 먼저 채우실 때입니다.")
        self.assertTrue(all(set(b) == {"path", "section", "text"} for b in shown))  # Prose only: no refs, no data.
        self.assertEqual(audits, ["report-narrative", "report-narrative-repair", "report-reader",
                                  "report-narrative-reader-rewrite"])
        self.assertEqual(result["cost_usd"], "1.5")

    def test_a_dash_before_a_figure_is_checked_as_the_client_will_read_it(self):
        # Codex review 2026-10-27: numbers are read in the tidied form. A dash glued to the word before a figure is a
        # separator (the positive figure passes); a dash standing before it is its sign (a negative figure the
        # evidence does not carry fails).
        data = self.data()
        payload = narrative.build_payload(data["view"], data, self.brief())
        self.assertIsNone(narrative.check_block(["영통 아파트값은 다음과 같습니다—3.4%."], ["R2"], payload, outside=True))
        self.assertEqual(narrative.check_block(["영통 아파트값이 –3.4% 움직였습니다."], ["R2"], payload, outside=True),
                         "ungrounded-number:-3.4%")

    def test_first_reader_failure_or_no_time_leaves_the_verified_narrative(self):
        data = self.data()
        first = dict(EMPTY, situation=block("현금 여유를 먼저 채우실 때입니다."))
        audits = []

        def failing(letter):
            raise headless.HeadlessError("tool-attempt", "command_execution")

        def audit(use_case, payload, outcome, code, cost, content):
            audits.append((use_case, outcome, code))
            return True
        failed = narrative.write(data["view"], data, self.brief(), secrets=[], executable=Path("claude.exe"),
                                 runner=runner_returning(first), audit=audit, first_reader=failing)
        self.assertEqual(failed["output"]["situation"]["text"], "현금 여유를 먼저 채우실 때입니다.")
        self.assertEqual(failed["reader"], {"outcome": "failure", "error_code": "tool-attempt"})
        self.assertEqual(audits[-1], ("report-reader", "failure", "tool-attempt"))
        times = iter([0.0, narrative.STAGE_SECONDS - 60])                       # A slow first write.
        late = narrative.write(data["view"], data, self.brief(), secrets=[], executable=Path("claude.exe"),
                               runner=runner_returning(first), first_reader=failing, clock=lambda: next(times))
        self.assertEqual(late["reader"], {"outcome": "skipped-time"})

    def test_the_readers_rewrite_gets_the_rest_of_the_stage(self):
        data = self.data()
        first = dict(EMPTY, situation=block("현금 여유를 먼저 채우실 때입니다."))
        timeouts = []

        def run(args, **kwargs):
            timeouts.append(kwargs["timeout"])
            return runner_returning(first)(args, **kwargs)

        def marking(letter):
            note = {"path": "situation", "quote": "현금 여유", "problem": "jargon", "why": "어렵습니다.", "suggestion": "풀어 쓰기"}
            return {"notes": [note], "raw_notes": 1, "overall": "", "usage": None, "model": "gpt-test"}
        # (clock at the gate, clock at the rewrite, the rewrite's timeout): the 2026-10-28 property day (a 9-minute
        # write, a 1-minute reading), then a slow reading that leaves less than the floor, which is still given.
        for gate, at, given in ((540, 600, narrative.STAGE_SECONDS - 600),
                                (600, 1000, narrative.READER_REWRITE_TIMEOUT_SECONDS)):
            timeouts.clear()
            times = iter([0.0, gate, at])
            narrative.write(data["view"], data, self.brief(), secrets=[], executable=Path("claude.exe"), runner=run,
                            first_reader=marking, clock=lambda: next(times))
            self.assertEqual(timeouts, [narrative.TIMEOUT_SECONDS, given])

    def test_rewrites_keep_identity_and_stance(self):
        hold = {"symbol": "VOO", "stance": "hold", "reason": "a", "alternative": "b", "reconsider": "c", "refs": [],
                "principles": []}
        insight = {"title": "금리", "body": "본문", "implication": "의미", "refs": []}
        current = dict(EMPTY, holdings=[hold], insights=[insight, dict(insight, title="환율")])
        second = dict(EMPTY, holdings=[dict(hold, stance="trim", reason="쉽게")],
                      insights=[dict(insight, title="금리의 방향", body="쉽게"), dict(insight, title="금리의 방향")])
        out, applied = narrative.apply_rewrites(current, ["holdings[0]", "insights[0]", "insights[1]"], second, [])
        self.assertEqual(out["holdings"], [hold])                               # A new stance is not a rewording.
        self.assertEqual(out["insights"][0]["title"], "금리의 방향")             # Retitled at the same position.
        self.assertEqual(out["insights"][1]["title"], "환율")                    # Would duplicate a title.
        self.assertEqual(applied, ["insights[0]"])
        _, applied = narrative.apply_rewrites(current, ["insights[0]"], second, ["insights[0]:no-source"])
        self.assertEqual(applied, [])

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
        for text in ("Smith의 판단", "이번 주 눈여겨볼 변화", "시장·정책 맥락", 'href="https://www.suwon.go.kr/x"'):
            self.assertIn(text, html)
        self.assertTrue(any("직접 식별자 없이" in n for n in report_html.system_notes(data)))   # A system note (v3.0).
        with closing(ledger.connect_read_only(self.db)) as conn:
            uses = [row[0] for row in conn.execute("SELECT use_case FROM advice_runs")]
            final = ledger.latest_advice(conn, use_case=narrative.FINAL_USE_CASE)["content"]
        self.assertEqual(uses, ["report-narrative", narrative.FINAL_USE_CASE])
        # The Korean corrections travel with the narrative as sent and into the footer (report design §34).
        self.assertEqual(final["korean"], data["narrative"]["status"]["korean"])
        self.assertEqual(set(final["korean"]), {"tidied", "flagged", "remaining", "reader_notes"})
        self.assertTrue(any(n.startswith("한국어 교정: ") for n in report_html.system_notes(data)))
        footer = render(data)[1].rsplit("Smith는 조회 전용 자문입니다", 1)[0][-900:]  # In the mail itself, not only the notes.
        self.assertIn("한국어 교정: 고정 수정 0건, 재작성 요청 0건, 남은 것 0건, 첫 독자 지적 0건", footer)
        # A themed day sends one insight: the record of what was sent (read by mail answers) holds that one only.
        data = self.data()
        data["kind"] = "rates"
        second = dict(output["insights"][0], title="두 번째 통찰")
        narrative.narrate(self.db, data["view"], data, now=AT, secrets=[], brief_id="same", executable=Path("claude.exe"),
                          runner=runner_returning(dict(output, insights=output["insights"] + [second])))
        with closing(ledger.connect_read_only(self.db)) as conn:
            final = ledger.latest_advice(conn, use_case=narrative.FINAL_USE_CASE)["content"]["verified"]
        self.assertEqual([i["title"] for i in final["insights"]], ["영통 토지거래허가구역"])

    def test_holding_opinions_are_checked_and_buying_more_carries_the_code_precondition(self):
        data = self.data()
        brief = self.brief()
        brief["brief"]["items"].append({"ref": "R3", "topic": "H1", "headline": "VOO 총보수 0.03%", "detail": "S&P 500 추종",
                                        "published_on": "2026-09-30", "publisher": "Vanguard", "source_title": "VOO",
                                        "source_url": "https://investor.vanguard.com/voo", "kind": "fact",
                                        "tier": "secondary"})
        payload = narrative.build_payload(data["view"], data, brief)
        self.assertEqual([(h["symbol"], h["research_topic"]) for h in payload["holdings"]], [("VOO", "H1")])
        opinion = {"symbol": "VOO", "stance": "add", "reason": "S&P 500 전체에 나눠 투자하는 펀드라 쏠림이 없습니다.",
                   "alternative": "", "reconsider": "비상금이 줄어들면 다시 판단합니다.", "refs": ["R3"],
                   "principles": ["BUF-INDEX", "NOT-A-PRINCIPLE"]}
        cases = [("invalid-holding", dict(opinion, symbol="AAPL")), ("invalid-holding", dict(opinion, stance="buy")),
                 ("no-holding-source", dict(opinion, reason="실적이 좋아졌습니다.", refs=[]))]
        for reason, item in cases:
            with self.subTest(reason=reason):
                _, dropped = narrative.verify(dict(EMPTY, holdings=[item]), payload)
                self.assertEqual(dropped, [f"holdings[0]:{reason}"])
        _, dropped = narrative.verify(dict(EMPTY, holdings=[opinion, dict(opinion, stance="hold")]), payload)
        self.assertEqual(dropped, ["holdings[1]:duplicate"])
        output = dict(EMPTY, portfolio=dict(block("주식은 넓게 나눠 담은 펀드 하나뿐이라 지금 구성을 유지하셔도 됩니다."),
                                            principles=["BUF-INDEX", "VAN-BALANCE"]), holdings=[opinion])
        with closing(ledger.connect(self.db)) as conn:
            ledger.record_research(conn, brief_id="b", created_at=AT, topics=[], outcome="success", model="fable",
                                   brief=brief["brief"])
        narrative.narrate(self.db, data["view"], data, now=AT, secrets=[], brief_id="b", executable=Path("claude.exe"),
                          runner=runner_returning(output))
        held = data["narrative"]["holdings"]["VOO"]
        # 10,000,000 cash against six months of 2,000,000 spending: the reserve comes first, says code.
        self.assertIn("비상금 부족분 200만 원", held["condition"])
        self.assertEqual(held["links"], [("Vanguard 2026-09-30", "https://investor.vanguard.com/voo")])
        self.assertEqual([p.id for p in held["principles"]], ["BUF-INDEX"])      # Unknown ids are dropped.
        # The policy: the only holding is a broad index fund (core met), but the reserve is short.
        policy = {c.key: c.status for c in data["policy"]}
        self.assertEqual((policy["core"], policy["single"], policy["reserve"]), ("ok", "ok", "breach"))
        _, html = render(data)
        for text in ("보유 종목 점검", "Smith의 투자 전략 · 주식 전체", "추가 매수 고려", "전제 조건",
                     "투자 원칙과 우리 집 투자 기준", "근거 원칙", "VOO (넓은 지수펀드)",
                     'href="https://www.berkshirehathaway.com/letters/1996.html"'):
            self.assertIn(text, html)
        self.assertTrue(any("종목 조사: 보유 종목 1개" in n for n in report_html.system_notes(data)))
        data["kind"] = "thursday"
        _, html = render(data)
        self.assertIn("보유 종목 의견", html)
        self.assertIn("투자 기준 점검", html)                                  # Only the checks that are off.
        self.assertNotIn("Smith의 투자 전략 · 주식 전체", html)

    def test_holding_opinions_cite_their_own_research_and_obey_the_limit(self):
        doc = {"schema_version": 1, "import_id": "aaa", "source": "manual", "mode": "patch", "as_of": AT.isoformat(),
               "owners": [{"id": "self"}], "records": [
                   rec("aaa", "asset", category="stock", account_type="brokerage", currency="KRW", value="50000000",
                       valuation_method="market", liquidity="days", symbol="AAA", market="KR",
                       instrument_name="수동으로 적은 이름")]}
        with closing(ledger.connect(self.db)) as conn:
            ledger.apply_import(conn, parse_import(json.dumps(doc)), recorded_at=AT)
        data = self.data()
        brief = self.brief()
        for ref, topic in (("R3", "H1"), ("R4", "H2")):
            brief["brief"]["items"].append(dict(brief["brief"]["items"][1], ref=ref, topic=topic, headline=f"{topic} 실적 개선"))
        payload = narrative.build_payload(data["view"], data, brief)
        held = {h["symbol"]: h for h in payload["holdings"]}
        self.assertEqual((held["AAA"]["research_topic"], held["VOO"]["research_topic"]), ("H1", "H2"))
        self.assertEqual(held["AAA"]["name"], "AAA")                             # A manual name stays local.
        self.assertTrue(held["AAA"]["above_concentration_limit"])
        base = {"alternative": "", "reconsider": "비중이 바뀌면 다시 봅니다.", "principles": []}
        cases = [("other-holding-source", dict(base, symbol="VOO", stance="hold", reason="실적이 좋습니다.", refs=["R3"])),
                 ("no-holding-source", dict(base, symbol="AAA", stance="hold", reason="실적이 좋습니다.", refs=["R2"])),
                 ("add-over-limit", dict(base, symbol="AAA", stance="add", reason="더 사십시오.", refs=[]))]
        for reason, item in cases:
            with self.subTest(reason=reason):
                _, dropped = narrative.verify(dict(EMPTY, holdings=[item]), payload)
                # AAA is above the limit, so an output with no opinion on it at all is also rejected (§17.4).
                missing = ["holdings[missing]:missing-opinion:AAA"] if item["symbol"] != "AAA" else []
                self.assertEqual(dropped, [f"holdings[0]:{reason}"] + missing)
        good = dict(base, symbol="AAA", stance="trim", reason="실적은 좋지만 한 회사에 너무 몰려 있습니다.", refs=["R3"])
        self.assertEqual(narrative.verify(dict(EMPTY, holdings=[good]), payload)[1], [])

    def test_a_failed_narrative_is_audited_and_leaves_the_deterministic_report(self):
        data = self.data()
        failing = lambda args, **kwargs: subprocess.CompletedProcess(args, 1, "{}", "")  # noqa: E731
        narrative.narrate(self.db, data["view"], data, now=AT, secrets=[], executable=Path("claude.exe"), runner=failing)
        self.assertEqual(data["narrative"]["status"]["outcome"], "failure")
        _, html = render(data)
        self.assertTrue(any("계산 결과만으로 작성했습니다" in n for n in report_html.system_notes(data)))
        self.assertNotIn("Smith의 판단", html)
        with closing(ledger.connect_read_only(self.db)) as conn:
            rows = list(conn.execute("SELECT use_case, outcome FROM advice_runs"))
        self.assertEqual(rows, [("report-narrative", "failure")])

    def test_listed_holdings_carry_gain_and_return_on_cost_only_when_known(self):
        def holding(symbol, currency, quantity, price, cost):
            return rec(symbol.lower(), "asset", category="stock", account_type="brokerage", currency=currency,
                       value=str(Decimal(quantity) * Decimal(price)), valuation_method="market", liquidity="days",
                       symbol=symbol, market="US", quantity=quantity, unit_price=price, average_cost=cost)
        doc = {"schema_version": 1, "import_id": "m2", "source": "manual", "mode": "patch", "as_of": AT.isoformat(),
               "owners": [{"id": "self"}], "records": [
                   holding("AAPL", "USD", "10", "120", "45"), holding("MSFT", "USD", "1", "200", "100"),
                   holding("TSLA", "USD", "1", "32.45", "100"), holding("SONY", "JPY", "1", "1500", "1000")]}
        with closing(ledger.connect(self.db)) as conn:
            ledger.apply_import(conn, parse_import(json.dumps(doc)), recorded_at=AT)
        data = self.data()
        payload = narrative.build_payload(data["view"], data, None)
        held = {a["symbol"]: a for a in payload["household"]["assets"] if a.get("symbol")}
        # 75 USD x 10 x 1,400; the return is in dollars, so the exchange rate does not distort it.
        self.assertEqual((held["AAPL"]["unrealized_gain"], held["AAPL"]["return_on_cost"]), ("1050000", "1.6667"))
        self.assertNotIn("unrealized_gain", held["VOO"])                     # No cost basis: unknown, not zero.
        # Without a JPY rate the gain in won is unknown, but the return needs no rate.
        self.assertEqual(("unrealized_gain" in held["SONY"], held["SONY"]["return_on_cost"]), (False, "0.5000"))
        securities = payload["sector_metrics"]["securities"]
        self.assertEqual((securities["unrealized_gain"], securities["positions_without_cost_basis"]), ("1095430", 2))
        # A "%" is compared only with percentages the inputs state, rounded to the digits written, same sign.
        for text, grounded in (("수익률 166.67%", True), ("수익률 166.7%", True), ("수익률 167%", True),
                               ("수익률 -167%", False), ("수익률 169%", False),
                               ("수익률 100%", True),                                   # 1.0000 keeps its value.
                               ("손실률 -67.6%", True), ("손실률 -0.7%", False),        # Not the ratio itself...
                               ("손실률 -0.6755%", False),                              # ...nor its raw digits.
                               ("CD 금리 2.5%", True), ("CD 금리 250%", False)):        # Already in %: not x100.
            with self.subTest(text=text):
                self.assertEqual(narrative.check_block([text], [], payload) is None, grounded)

    def test_only_inputs_and_the_clients_statements_are_evidence(self):
        data = self.data()
        payload = dict(narrative.build_payload(data["view"], data, None), client_question="수수료가 55.5%라면?",
                       recent_report={"situation": "비중이 77.7%입니다"},
                       conversation=[{"client": "지난번 질문", "smith": "수익률 42.4%였습니다"}],
                       specialist_findings=[{"what": "점유율 88.8%"}])
        for text, grounded in (("수수료 55.5%", True), ("비중 77.7%", False), ("수익률 42.4%", False),
                               ("점유율 88.8%", False)):
            with self.subTest(text=text):    # Model outputs are never evidence, whatever key they arrive under.
                self.assertEqual(narrative.check_block([text], [], payload) is None, grounded)

    def test_team_specialists_see_their_area_and_cannot_vouch_for_figures(self):
        seen, lock = {}, threading.Lock()
        finding = {"headline": "현금 1,000만 원은 비상금입니다", "what": "보유 현금 1,000만 원", "why_it_matters": "버팀목",
                   "so_what": "유지", "severity": "HIGH", "certainty": "fact", "refs": []}
        made_up = dict(finding, headline="투자 여력 9,999만 원", what="여력 9,999만 원", severity="urgent")
        specialist = {"findings": [finding, made_up], "cross_sector": ["보증금 반환과 투자가 상충"], "data_gaps": []}
        editor = dict(EMPTY, situation=block("고객님은 여유 자금 9,999만 원을 투자에 쓰실 수 있습니다."))

        def runner(args, **kwargs):
            prompt = system_prompt(args)
            if "Your area: " not in prompt:
                return runner_returning(editor)(args, **kwargs)
            area = prompt.split("Your area: ")[1].split()[0].rstrip(":,")
            with lock:
                seen[area] = json.loads(kwargs["input"])
            if area == "debt":
                return subprocess.CompletedProcess(args, 1, "{}", "")      # One specialist fails.
            return runner_returning(specialist)(args, **kwargs)
        data = self.data()
        narrative.narrate(self.db, data["view"], data, now=AT, secrets=[], executable=Path("claude.exe"), runner=runner,
                          pipeline="team")
        status = data["narrative"]["status"]
        self.assertEqual((status["pipeline"], status["outcome"]), ("team", "success"))
        # Each specialist reads only its area's records.
        symbols = lambda area: {a.get("symbol") or a["category"] for a in seen[area]["household"]["assets"]}  # noqa: E731
        self.assertEqual(symbols("investments"), {"VOO"})
        self.assertEqual(symbols("liquidity"), {"cash"})
        self.assertEqual(symbols("real"), {"real_estate"})
        self.assertNotIn("tax", seen["liquidity"])
        by_area = {s["area"]: s for s in status["specialists"]}
        self.assertEqual(by_area["debt"]["status"], "failed")
        liquidity = by_area["liquidity"]["findings"]
        self.assertEqual([f["severity"] for f in liquidity], ["high", "medium"])
        self.assertNotIn("check_failed", liquidity[0])                       # 1,000만 원 is the ledger's cash.
        self.assertTrue(liquidity[1]["check_failed"].startswith("ungrounded-number"))
        with closing(ledger.connect_read_only(self.db)) as conn:
            runs = dict(conn.execute("SELECT use_case, payload FROM advice_runs WHERE use_case NOT LIKE '%repair'"))
        self.assertEqual(sorted(runs), ["report-narrative", narrative.FINAL_USE_CASE, "report-specialist-debt", "report-specialist-investment",
                                        "report-specialist-liquidity", "report-specialist-real_estate",
                                        "report-specialist-tax_pension"])
        sent = json.loads(runs["report-narrative"])
        self.assertEqual({s["area"]: s["status"] for s in sent["specialist_findings"]}["debt"], "failed")
        # The editor repeated a specialist's made-up figure: findings are not evidence, so it is flagged.
        self.assertIn("확인되지 않은 수치", data["narrative"]["situation_memo"])

    def test_reports_receive_the_client_memory(self):
        memory.add(memory.directory(self.db), "property", "솔방울은 거주 중인 집을 말한다", on=TODAY, source="메일")
        data = self.data()
        narrative.narrate(self.db, data["view"], data, now=AT, secrets=[], executable=Path("claude.exe"),
                          runner=runner_returning(EMPTY))
        self.assertEqual(data["narrative"]["status"]["memory"], {"stored": 1, "sent": 1})
        with closing(ledger.connect_read_only(self.db)) as conn:
            sent = json.loads(conn.execute("SELECT payload FROM advice_runs WHERE use_case = 'report-narrative'").fetchone()[0])
        self.assertEqual([m["fact"] for m in sent["client_memory"]], ["솔방울은 거주 중인 집을 말한다"])

    def test_the_previous_report_on_the_theme_is_sent_and_its_titles_are_not_repeated(self):
        data = self.data()
        self.assertIsNone(narrative.previous_report(self.db, data))               # A first report: nothing to avoid.
        week_ago = AT - timedelta(days=7)
        final = {"verified": {"situation": {"text": "지난주 상황", "refs": []}, "direction": {"text": "", "refs": []},
                              "portfolio": {"text": "", "refs": []},
                              "insights": [{"title": "영통 규제", "body": "지정", "implication": "영향", "refs": ["R1"]}],
                              "watch": [{"item": "금통위", "why": "다음 달", "refs": []}], "experiences": [],
                              "holdings": [{"symbol": "XYZ", "stance": "hold", "reason": "지난주 이유"}]},
                 "korean": {"tidied": [{"path": "situation", "kind": "dash", "before": "—", "after": ","}], "flagged": [],
                            "remaining": [], "reader_notes": []}}
        with closing(ledger.connect(self.db)) as conn:
            ledger.record_advice_run(conn, run_id="final-1", created_at=week_ago, use_case=narrative.FINAL_USE_CASE,
                                     question="monday", payload="{}", prompt_version="x", model="m", cost_usd=None,
                                     outcome="success", advice=json.dumps(final, ensure_ascii=False))
            ledger.claim_report(conn, report_id="prev", slot=None, kind="monday", trigger="manual", created_at=week_ago,
                                as_of=week_ago, baseline=None, missed_slots=[])
            ledger.record_lineage(conn, report_id="prev", known_at=week_ago, brief_id=None, advice_run_ids=["final-1"],
                                  config_sha256=None, code_version=None, data_sha256="d")
            ledger.finish_report(conn, report_id="prev", status="sent", finished_at=week_ago, shown_proposals=[])
            # The report before it: only its titles travel, so a title does not come back every other week.
            older_at = AT - timedelta(days=14)
            older = {"verified": {**final["verified"],
                                  "insights": [{"title": "두 주 전 통찰", "body": "b", "implication": "i", "refs": ["R1"]}],
                                  "watch": [{"item": "두 주 전 항목", "why": "w", "refs": []}]}}
            ledger.record_advice_run(conn, run_id="final-0", created_at=older_at, use_case=narrative.FINAL_USE_CASE,
                                     question="monday", payload="{}", prompt_version="x", model="m", cost_usd=None,
                                     outcome="success", advice=json.dumps(older, ensure_ascii=False))
            ledger.claim_report(conn, report_id="prev0", slot=None, kind="monday", trigger="manual", created_at=older_at,
                                as_of=older_at, baseline=None, missed_slots=[])
            ledger.record_lineage(conn, report_id="prev0", known_at=older_at, brief_id=None, advice_run_ids=["final-0"],
                                  config_sha256=None, code_version=None, data_sha256="d")
            ledger.finish_report(conn, report_id="prev0", status="sent", finished_at=older_at, shown_proposals=[])
        with closing(ledger.connect_read_only(self.db)) as conn:   # `smith report korean`: newest first, None before v27.
            history = narrative.korean_history(conn, limit=5)
        self.assertEqual([(h["kind"], h["korean"]) for h in history], [("monday", final["korean"]), ("monday", None)])
        previous = narrative.previous_report(self.db, data)
        self.assertEqual(previous["date"], week_ago.astimezone(data["as_of"].tzinfo).date().isoformat())
        self.assertEqual(previous["earlier"], [{"date": older_at.astimezone(data["as_of"].tzinfo).date().isoformat(),
                                               "insights": ["두 주 전 통찰"], "watch": ["두 주 전 항목"], "experiences": []}])
        self.assertEqual((previous["situation"], [i["title"] for i in previous["insights"]], previous["watch"],
                          previous["holdings"]),
                         ("지난주 상황", ["영통 규제"], [{"item": "금통위"}],
                          [{"symbol": "XYZ", "stance": "hold", "reason": "지난주 이유"}]))
        payload = narrative.build_payload(data["view"], data, self.brief(), last_report=previous)
        self.assertNotIn("last_report", narrative._EVIDENCE_KEYS)                # Smith's own prose is not evidence.
        repeated = dict(EMPTY, insights=[{"title": "영통 규제", "body": "다시 지정", "implication": "영향", "refs": ["R1"]},
                                         {"title": "영통 규제 완화 논의", "body": "지정 해제 논의", "implication": "영향",
                                          "refs": ["R1"]},
                                         {"title": "두 주 전 통찰", "body": "다시", "implication": "영향", "refs": ["R1"]}],
                        # A qualifier tag the polish strips does not make last week's title new (Codex review 2026-10-27).
                        watch=[{"item": "금통위", "why": "다음 달", "refs": []}, {"item": "금통위 (가정)", "why": "다음 달", "refs": []}])
        verified, dropped = narrative.verify(repeated, payload)
        self.assertEqual([d for d in dropped if "repeats" in d],
                         ["insights[0]:repeats-last-report", "insights[2]:repeats-last-report",
                          "watch[0]:repeats-last-report", "watch[1]:repeats-last-report"])
        self.assertEqual([i["title"] for i in verified["insights"]], ["영통 규제 완화 논의"])
        # A repeat is never restored from the raw output when the repair repeats it, drops it or fails (Codex
        # review 2026-10-27): the final output simply lacks it.
        for name, second in (("repeated again", repeated), ("left out", dict(repeated, insights=[], watch=[])),
                             ("timed out", subprocess.TimeoutExpired("claude", 1))):
            with self.subTest(repair=name):
                outputs = [repeated, second]
                calls = []

                def run(args, **kwargs):
                    calls.append(1)
                    answer = outputs[len(calls) - 1]
                    if isinstance(answer, Exception):
                        raise answer
                    return runner_returning(answer)(args, **kwargs)
                written = narrative.write(data["view"], data, self.brief(), secrets=[], executable=Path("claude.exe"),
                                          runner=run, last_report=previous)
                self.assertEqual(([i["title"] for i in written["output"]["insights"]], written["output"]["watch"]),
                                 (["영통 규제 완화 논의"], []))
        # The report's writer receives it and the status says which report it was.
        narrative.narrate(self.db, data["view"], data, now=AT, secrets=[], executable=Path("claude.exe"),
                          runner=runner_returning(EMPTY))
        self.assertEqual(data["narrative"]["status"]["last_report"], previous["date"])
        with closing(ledger.connect_read_only(self.db)) as conn:
            sent = json.loads(conn.execute("SELECT payload FROM advice_runs WHERE use_case = 'report-narrative'").fetchone()[0])
        self.assertEqual(sent["last_report"]["watch"], [{"item": "금통위"}])

    def test_compare_builds_both_reports_from_one_snapshot_without_sending(self):
        calls = []

        def narrate(db, view, data, **kwargs):
            calls.append((kwargs["pipeline"], kwargs["now"], kwargs["brief_id"]))
            narrative.attach(data, None, None, {"brief": None, "model": "m", "pipeline": kwargs["pipeline"],
                                                "outcome": "failure", "error_code": "x", "dropped": [], "run_ids": []})
        with tempfile.TemporaryDirectory() as directory:
            page = compare.run(self.db, now=AT, kind="monday", tz="Asia/Seoul", baseline=None, household=None,
                               brief_id=None, secrets=[], out_dir=Path(directory), narrate=narrate)
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ["index.html", "single.html", "team.html"])
            html = page.read_text(encoding="utf-8")
        self.assertEqual(sorted(calls), [("single", AT, None), ("team", AT, None)])   # Same instant and brief.
        self.assertIn('src="team.html"', html)
        self.assertIn("전문가 단계가 실행되지 않았습니다", html)

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

def setUpModule():  # noqa: N802 - unittest hook
    fast_ledger.install()  # File ledgers without an fsync per commit (test budget); results are unchanged.


def tearDownModule():  # noqa: N802 - unittest hook
    fast_ledger.uninstall()


if __name__ == "__main__":
    unittest.main()
