import base64
import json
import re
import tempfile
import unittest
import unittest.mock
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from smith import contacts, counsel, gmail, health_qa, keep, ledger, memory, qa
from smith.config import load_config
from smith.importer import parse_import

import fast_ledger

NOW = datetime(2026, 11, 1, 3, 0, tzinfo=timezone.utc)
ME = "me@example.com"
CONFIG = {"app": {"timezone": "Asia/Seoul"}, "counsel": {"enabled": True, "prefix": "[상담]", "max_notes": 8}}
EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "smith.example.toml"
ROUTE = {"summary": "팀장 제안을 받을지 고민한다", "domains": ["finance"], "search_terms": ["팀장", "리더십", "승진"],
         "web": {"needed": "no", "subject": "", "queries": []}, "asks_action": "no"}
QUESTION = "팀장 제안을 받았어. 사람 관리가 걱정인데 받는 게 맞을까?"


def setUpModule():
    fast_ledger.install()


def tearDownModule():
    fast_ledger.uninstall()


def fake_caller(script):
    """Plays the model: pops one canned output per call and records (use_case, web, text) in order."""
    calls = []

    def call(text, system_prompt, schema, use_case, timeout, web):
        calls.append((use_case, web, text))
        step = script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step, "0.01"
    return call, calls


def note(note_id, title, text, labels, on):
    return keep.Note(note_id, title, text, tuple(labels), datetime(on.year, on.month, on.day, 12, tzinfo=timezone.utc),
                     False, False, "note")


NOTES = [note("n1", "2026-10", "1006 ㆍ 팀장 제안을 받으면 사람 관리에 시간을 쓰게 되는 게 걱정이다. 그래도 리더십을 배울 기회라는 "
                              "생각도 든다.", ("생각",), date(2026, 10, 6)),
         note("evernote:abc", "리더십 책 정리", "좋은 리더는 팀장 역할에서 위임과 피드백을 먼저 배운다고 한다. 리더십 책의 핵심을 "
                                          "정리해 둔다.", ("My Notebook", "evernote", "notebook"), date(2026, 9, 1)),
         note("n2", "여행", "제주 여행에서 먹은 흑돼지가 맛있었다. 다음에는 서귀포 쪽 숙소를 잡아야겠다.", ("여행",), date(2026, 8, 1))]


def verified(items):
    """Plays sources.verify: https items are kept with a check whose status a [marker] in the title may set."""
    kept = []
    for item in items:
        if item["source_url"].startswith("https://"):
            status = next((s for s in ("unchecked", "mismatch", "matched") if f"[{s}]" in item["source_title"]), "reachable")
            kept.append({**item, "ref": f"R{len(kept) + 1}", "check": {"status": status}})
    return kept, {}


class CounselPipelineTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Path(folder.name) / "smith.db"
        doc = {"schema_version": 1, "import_id": "m1", "source": "manual", "mode": "patch", "as_of": NOW.isoformat(),
               "owners": [{"id": "self"}], "records": [
                   {"id": "cash", "kind": "asset", "owner_id": "self", "effective_at": NOW.isoformat(), "revision": 1,
                    "status": "active", "category": "cash", "account_type": "bank", "currency": "KRW", "value": "10000000",
                    "valuation_method": "manual", "liquidity": "immediate"}]}
        with closing(ledger.connect(self.db)) as conn:
            ledger.apply_import(conn, parse_import(json.dumps(doc)), recorded_at=NOW)
        memory.add(memory.directory(self.db), "preference", "결정할 때 숫자보다 사람을 먼저 본다", on=date(2026, 10, 1), source="대화")
        memory.add(memory.directory(self.db), "counsel", "지난 상담에서 팀장 제안을 고민했다", on=date(2026, 10, 20), source="메일")
        patches = [unittest.mock.patch.object(counsel.keep, "load_all", return_value=NOTES),
                   unittest.mock.patch.object(counsel.contacts, "load", return_value=[]),
                   unittest.mock.patch.object(counsel.daily_qa.credentials, "load_gmail", return_value=None)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def answer(self, script, question=QUESTION, **kwargs):
        call, calls = fake_caller(script)
        result = counsel.answer(self.db, CONFIG, question, now=NOW, received_at=NOW, history=[], secrets=[], tz="Asia/Seoul",
                                caller=call, verifier=verified, **kwargs)
        return result, calls

    def test_plan_normalises_router_output(self):
        plan = counsel.plan({"domains": ["Finance", "sports", "health", "finance"], "search_terms": ["팀장", " 팀장 ", "", "리더십"],
                             "web": {"needed": "YES", "subject": "x", "queries": ["a", "a", " "]}, "asks_action": "no"})
        self.assertEqual((plan["domains"], plan["search_terms"]), (["finance", "health"], ["팀장", "리더십"]))
        self.assertEqual((plan["web"]["needed"], plan["web"]["queries"], plan["asks_action"]), (True, ["a"], False))
        self.assertEqual(counsel.plan(None)["domains"], [])

    def test_web_gate_lets_only_general_queries_leave(self):
        book = [contacts.Contact("c1", "김민수", (), None, None, (), "", "")]
        plan = counsel.plan({"web": {"needed": "yes", "queries": ["연봉 7000만원 양도세", "2026년 연말정산 개정 사항", "리더십 피드백 방법",
                                                              "45세 이직", "한 번 더"]}})
        self.assertEqual(len(plan["web"]["queries"]), counsel.MAX_QUERIES)
        kept, subject, refused = counsel.web_gate(plan, book)
        self.assertEqual((kept, subject, refused), (["2026년 연말정산 개정 사항", "리더십 피드백 방법", "45세 이직"], "2026년 연말정산 개정 사항", None))
        private = counsel.plan({"web": {"needed": "yes", "queries": ["김민수 교수 평판", "010-1234-5678 요금제", "연봉 7000만원"]}})
        kept, subject, refused = counsel.web_gate(private, book)
        self.assertEqual((kept, subject), ([], ""))
        self.assertIn("웹 조사는 하지 않았어요", refused)
        # The subject is screened like a query (review 2026-11-01): a private one is replaced by the first query kept.
        for subject, expected in (("김민수의 연봉 7000만원 이직 상담", "이직 결정 기준"), ("이직 결정에서 흔한 기준", "이직 결정에서 흔한 기준")):
            with self.subTest(subject=subject):
                plan = counsel.plan({"web": {"needed": "yes", "subject": subject, "queries": ["이직 결정 기준"]}})
                self.assertEqual(counsel.web_gate(plan, book)[1], expected)

    def test_walls_answer_without_a_model(self):
        def never(*args):
            raise AssertionError("no model call expected")
        for question, text in (("요즘 가슴 통증이 자주 있어. 운동을 더 해야 할까?", health_qa.EMERGENCY),
                               ("자살 생각이 자꾸 들어. 회사 문제가 커.", counsel.CRISIS_TEXT)):
            with self.subTest(question=question[:6]):
                result = counsel.answer(self.db, CONFIG, question, now=NOW, received_at=NOW, history=[], secrets=[], tz="Asia/Seoul",
                                        caller=never)
                self.assertEqual((result["text"], result["emergency"]), (text, True))
                html, _ = counsel.render(question, result)
                self.assertNotIn("읽은 자료", html)

    def test_answer_reads_the_routed_stores_only_and_checks_the_letter(self):
        letter = ("팀장 제안을 두고 마음이 오가는 게 느껴져요.\n\n10월 6일에 쓰신 메모처럼 사람 관리가 걱정이지만 배울 기회이기도 해요. "
                  "3년쯤 지나면 선택지가 달라질 거예요. LDL 콜레스테롤이 160이라 걱정이에요. 복용을 시작하세요. "
                  "자세한 건 https://x.y 참고하세요 [R3]. 받아들이는 쪽의 반대급부는 시간이에요. 지금 현금은 1,000만 원이에요. "
                  "순자산은 99억 원이고 매달 777만 원이 남아요.")
        output = {"answer": letter, "follow_up": ["팀장이 되면 보상은 어떻게 달라질까요?"],
                  "remember": [{"topic": "plan", "fact": "팀장 제안을 받았다"}, {"topic": "nonsense", "fact": "커피를 좋아한다"},
                               {"topic": "plan", "fact": "팀장 제안을 받을 생각이다"}],
                  "health_statements": ["저는 요즘 잠을 6시간쯤 자요", "아내는 혈압이 높다"]}
        result, calls = self.answer([dict(ROUTE), output])
        self.assertEqual([(u, w) for u, w, _ in calls], [("counsel-route", False), ("counsel-answer", False)])
        routed = json.loads(calls[0][2])
        self.assertEqual(sorted(routed), ["earlier_questions", "question", "stores", "today"])   # No personal data.
        payload = json.loads(calls[1][2])
        self.assertIn("balance_sheet", payload["finance"])
        self.assertNotIn("health", payload)
        self.assertNotIn("life", payload)
        self.assertNotIn("_health_meta", payload)
        self.assertEqual([(n["source"], n["reference_only"]) for n in payload["client_notes"]], [("keep", False), ("evernote", True)])
        # The shared memory and the counsel topic both reach the counsel call; other threads never see the counsel topic.
        self.assertEqual({f["fact"] for f in payload["client_memory"]}, {"결정할 때 숫자보다 사람을 먼저 본다", "지난 상담에서 팀장 제안을 고민했다"})
        self.assertEqual([f.topic for f in memory.load(memory.directory(self.db))], ["preference"])
        self.assertEqual(payload["asks_action"], "no")
        self.assertEqual(result["data_status"]["finance"], "included")
        self.assertEqual((result["data_status"]["health"], result["data_status"]["life"]), ("not asked", "not asked"))
        # The ledger's 10,000,000 won may be written rounded; an amount of the model's own may not (second review).
        self.assertEqual(result["text"], "팀장 제안을 두고 마음이 오가는 게 느껴져요.\n\n10월 6일에 쓰신 메모처럼 사람 관리가 걱정이지만 "
                                         "배울 기회이기도 해요. 3년쯤 지나면 선택지가 달라질 거예요. 자세한 건 참고하세요. "
                                         "받아들이는 쪽의 반대급부는 시간이에요. 지금 현금은 1,000만 원이에요.")
        self.assertEqual(len(result["problems"]), 4)   # A health value not in the input, dosing wording, a stray ref, an invented amount.
        # Only the client's own words are kept as theirs: the coffee, the sleep and the paraphrase came from the model.
        self.assertEqual(result["remember"], [("counsel", "팀장 제안을 받았다")])   # Under the counsel topic, whatever the model said.
        self.assertEqual(result["noted"], [])
        html, text = counsel.render(QUESTION, result)
        self.assertIn("SMITH · 상담", html)
        self.assertIn("읽은 자료: 메모 2개(에버노트 1개 참고), 기억 2개, 재무 자료", text)
        self.assertIn("기억해 둘게요: 팀장 제안을 받았다", text)
        self.assertNotIn("검색한 말", text)

    def test_research_runs_once_with_gated_queries_and_the_letter_cites_it(self):
        route = {**ROUTE, "domains": [], "web": {"needed": "yes", "subject": "첫 관리자 역할에서 흔한 어려움",
                                                 "queries": ["리더십 피드백 방법", "연봉 7000만원 팀장", "김민수 교수 평판"]}}
        findings = {"findings": [
            {"claim": "첫 관리자에게는 위임과 1:1 면담을 먼저 익히라고 조언한다", "source_title": "First-time manager", "source_url": "https://hbr.org/x",
             "published_on": "2024-05-01", "publisher": "HBR", "confidence": "single-source"},
            {"claim": "공제 한도는 9999만 원이다", "source_title": "Tax guide [mismatch]", "source_url": "https://tax.example/x",
             "published_on": "", "publisher": "", "confidence": "confirmed"},
            {"claim": "관리자 교육은 보통 첫 해에 한다", "source_title": "Training note [unchecked]", "source_url": "https://edu.example/x",
             "published_on": "", "publisher": "", "confidence": "confirmed"},
            {"claim": "주소는", "source_title": "x", "source_url": "http://insecure.example/x", "published_on": "", "publisher": "", "confidence": "unconfirmed"}],
            "gaps": ["한국 기업 사례는 적어요"]}
        output = {"answer": "첫 관리자에게는 위임이 먼저라고 해요 [R1]. 교육은 첫 해라는 글도 있어요 [R2]. 다른 글은 [R5] 참고. 다음 보고서부터 반영하겠습니다.",
                  "follow_up": [], "remember": [{"topic": "other", "fact": "김민수 교수는 반도체 전문가다"},
                                                {"topic": "plan", "fact": "저는 팀장 제안을 받았다"}], "health_statements": []}
        with unittest.mock.patch.object(counsel.contacts, "load", return_value=[contacts.Contact("c1", "김민수", (), None, None, (), "", "")]):
            result, calls = self.answer([route, findings, output])
        self.assertEqual([(u, w) for u, w, _ in calls], [("counsel-route", False), ("counsel-research", True), ("counsel-answer", False)])
        research = json.loads(calls[1][2])
        self.assertEqual(sorted(research), ["queries", "subject", "today"])
        self.assertEqual(research["queries"], ["리더십 피드백 방법"])
        sent = json.loads(calls[2][2])["research_findings"]
        self.assertEqual(sent["gaps"], ["한국 기업 사례는 적어요"])
        # The page whose numbers contradicted the claim is gone; the unread page is unconfirmed whatever the model said.
        self.assertEqual([(f["ref"], f["verification"], f["confidence"]) for f in sent["items"]],
                         [("R1", "reachable", "single-source"), ("R2", "unchecked", "unconfirmed")])
        self.assertEqual((result["refs"], result["searched"], result["researched"]), (["R1", "R2"], ["리더십 피드백 방법"], True))
        self.assertEqual(result["links"]["R1"], ("First-time manager", "https://hbr.org/x"))
        self.assertEqual(result["unchecked"], ["R2"])
        self.assertNotIn("R5", result["text"])
        self.assertNotIn("다음 보고서부터", result["text"])   # A promise of a change Smith cannot make is replaced.
        self.assertIn(counsel.ACTION_MEMO, result["memos"])
        self.assertEqual(result["remember"], [("counsel", "저는 팀장 제안을 받았다")])   # The professor is not the client's fact.
        html, text = counsel.render(QUESTION, result)
        self.assertIn("검색한 말: 리더십 피드백 방법", text)
        self.assertIn("[R1] First-time manager", html)
        self.assertIn("[R2] Training note [unchecked]</a> (페이지 미확인)", html)
        self.assertIn("읽은 자료: 메모 2개(에버노트 1개 참고), 기억 2개 · 검색한 말", text)

    def test_health_meta_guards_the_letter_and_a_failed_store_is_reported(self):
        route = {**ROUTE, "domains": ["health", "life"]}
        health_ctx = {"items": [], "goals": [], "resolved_findings": ["위염"], "banned_words": ["생선"]}
        output = {"answer": "위염은 이제 괜찮아졌죠. 생선을 매일 드세요. 생선을 드셔 보세요. 아침 산책 20분은 어때요?",
                  "follow_up": ["생선을 매일 드시는 건 어때요?", "생선을 먹어 보세요.", "위염은 다 나았죠?", "산책 시간을 늘려 볼까요?"],
                  "remember": [{"topic": "plan", "fact": "아침 산책을 하기로 했다"}], "health_statements": []}
        with unittest.mock.patch.object(counsel, "_health", return_value=health_ctx), \
                unittest.mock.patch.object(counsel, "_life", side_effect=OSError("boom")):
            result, calls = self.answer([route, output], question="아침 산책을 하기로 했어. 몸이 무거운데 뭘 바꿔야 할까?")
        payload = json.loads(calls[1][2])
        self.assertEqual(payload["health"], {"items": [], "goals": []})
        self.assertEqual((result["data_status"]["health"], result["data_status"]["life"]), ("included", "unavailable: OSError"))
        self.assertEqual(result["text"], "아침 산책 20분은 어때요?")
        self.assertIn("읽지 못함: 일정", counsel.read_summary(result))
        # A question about the body sends nothing to the shared memory, even a plan (second review 2026-11-01).
        self.assertEqual((result["remember"], result["noted"]), ([], ["아침 산책을 하기로 했다"]))
        self.assertEqual(result["follow_up"], ["산책 시간을 늘려 볼까요?"])   # The ruled-out food and the resolved finding fail here too.

    def test_address_book_names_travel_as_aliases_in_both_calls(self):
        book = [contacts.Contact("c1", "김민수", (), None, None, (), "같은 팀 선배", "")]
        # The full name, or the given name alone before a relation word (second review 2026-11-01), names the contact.
        for question in ("김민수 팀장이 나한테 팀장 제안을 했어. 민수 형 밑에서 일하는 게 맞을까?",
                         "민수 형이 팀장을 제안했어. 민수씨 밑에서 일하는 게 맞을까?",
                         "김민수한테서 팀장 제안을 받았어. 김민수처럼 되고 싶기도 해.",   # Compound particles (third review).
                         "팀장 제안을 받았어.\n김민수한테서 온 제안이야.\t김민수 말이야.",   # After a line break or tab (fourth review).
                         "제친구김민수한테서 팀장 제안을 받았어."):                       # Glued to the word before (sixth review).
            seen = []

            def call(text, system_prompt, schema, use_case, timeout, web):
                seen.append((use_case, text))
                alias = sorted(set(re.findall(r"P\d+(?![0-9])", text)))   # "P1한테서", "\\nP1", "제친구P1": no word boundary there.
                if use_case == "counsel-route":
                    return {**ROUTE, "domains": []}, "0.01"
                return {"answer": f"{alias[0]} 팀장의 제안은 고마운 일이에요. 그래도 {alias[0]} 아래서 배울 것과 잃을 것을 같이 보세요.",
                        "follow_up": [], "remember": [], "health_statements": []}, "0.01"
            with self.subTest(question=question[:6]), unittest.mock.patch.object(counsel.contacts, "load", return_value=book):
                result = counsel.answer(self.db, CONFIG, question, now=NOW, received_at=NOW, history=[], secrets=[], tz="Asia/Seoul", caller=call)
                self.assertEqual([u for u, _ in seen], ["counsel-route", "counsel-answer"])
                for _, text in seen:
                    self.assertNotIn("민수", text)
                self.assertEqual(result["text"], "김민수 팀장의 제안은 고마운 일이에요. 그래도 김민수 아래서 배울 것과 잃을 것을 같이 보세요.")
        self.assertFalse(counsel.given_named("하나만 물어볼게", contacts.Contact("c2", "이하나", (), None, None, (), "", "")))

    def test_health_facts_go_to_the_health_notes_and_are_checked(self):
        route = {**ROUTE, "domains": []}
        question = ("요즘 잠을 6시간쯤 자고 약을 먹는 습관이 있어. LDL 콜레스테롤이 160이야. 괜찮을까? 팀장 제안은 받기로 했어. "
                    "팀장 승진은 수락하지 않았어. 그가 담배를 피운다. 나는 안 피워.")
        output = {"answer": "혈압이 177이라 걱정이에요. LDL이 177이면 높아요. ldl이 177로 올랐어요. 당뇨병으로 보여요. 고혈압 같아요. "
                            "약을 줄여 보세요. 아스피린을 시작해 보세요. 비타민 D를 두 알 챙겨 보세요. 아스피린을 드셔 보세요. "
                            "타이레놀을 시작해 보세요. 혈압은 100이에요. LDL 콜레스테롤이 160이면 눈여겨볼 값이에요. 3년 뒤를 보세요.",
                  "follow_up": ["비타민 D를 매일 두 알 드세요", "비타민 D를 두 알 드셔 보세요", "LDL이 177로 올랐는데 어떻게 낮출까요?",
                                "순자산이 99억 원인데 퇴사해도 될까요?", "다음 검진은 언제가 좋을까요?"],
                  "remember": [{"topic": "profile", "fact": "저는 LDL 콜레스테롤이 160이에요"}, {"topic": "preference", "fact": "약을 먹는 습관이 있다"},
                               {"topic": "profile", "fact": "저는 당뇨가 있어요"}, {"topic": "plan", "fact": "저는 팀장 승진을 수락했다"},
                               {"topic": "plan", "fact": "팀장 제안은 받기로 했다"}],
                  "health_statements": ["비타민 D를 매일 두 알 드세요", "저는 요즘 잠을 6시간쯤 자요", "아내는 혈압이 높다",
                                        "저는 요즘 잠을 6시간쯤 자요", "저는 LDL이 177이에요", "저는 담배를 피운다"]}
        result, _ = self.answer([route, output], question=question)
        # Short item names count as health items in any case; the informal register of a diagnosis, a dose or a
        # named medicine is caught too; a health value must come from the record or the client's words (160 did,
        # 100 and 177 did not, whatever other numbers the input holds).
        self.assertEqual(result["text"], "LDL 콜레스테롤이 160이면 눈여겨볼 값이에요. 3년 뒤를 보세요.")
        # Body facts never; the diabetes was never said; the flipped 않 is not the client's statement; what is kept
        # goes under the counsel topic only.
        self.assertEqual(result["remember"], [("counsel", "팀장 제안은 받기로 했다")])
        # Kept: the client's own words only; dropped: a dose, someone else's, a figure the client never wrote, and a
        # sentence about "그" with 저는 put in front (fifth review).
        self.assertEqual(result["noted"], ["저는 LDL 콜레스테롤이 160이에요", "약을 먹는 습관이 있다", "저는 요즘 잠을 6시간쯤 자요"])
        self.assertEqual(result["follow_up"], ["다음 검진은 언제가 좋을까요?"])   # A follow-up is checked like the letter.
        _, text = counsel.render(question, result)
        self.assertNotIn("두 알", text)

    def test_only_a_whole_statement_of_the_client_is_kept_as_theirs(self):
        cases = [("팀장 제안을 받기로 했다", "팀장 제안을 받기로 했어. 괜찮을까?", True),
                 ("저는 요즘 잠을 6시간쯤 자요", "요즘 잠을 6시간쯤 자고 약을 먹어.", True),
                 ("팀장 제안을 받았다", "그래서 팀장 제안을 받았어.", True),                                   # A connective may lead.
                 ("저는 담배를 피운다", "그가 담배를 피운다. 나는 안 피워.", False),                           # Someone else's (fifth review).
                 ("담배를 피운다", "그가 담배를 피운다.", False),
                 ("담배를 피운다", "그가 밥을 먹고 담배를 피운다.", False),                                    # Still his, a clause later.
                 ("저는 담배를 피워요", "철수가 밥을 먹고 담배를 피워. 나는 안 피워.", False),                  # A named subject, a clause earlier.
                 ("담배를 피운다", "많이 먹고 담배를 피운다.", True),                                           # An adverb is not a subject.
                 ("약을 먹는 습관이 있다", "요즘 잠을 6시간쯤 자고 약을 먹는 습관이 있어.", True),              # The client's own second clause.
                 ("팀장 제안을 받기로 했다", "팀장 제안을 받기로 했다면 어떨까? 아직 결정하지 않았어.", False),   # A condition.
                 ("팀장 제안을 받기로 했다", "팀장 제안을 받기로 했어?", False),                               # A question.
                 ("나는 담배를 피운다", "나는 담배를 피운다는 건 사실이 아니야.", False),                      # A denied claim.
                 ("저는 팀장 승진을 수락했다", "팀장 승진은 수락하지 않았어.", False),                          # A flipped 않.
                 ("팀장 제안을 받았다", "팀장 제안을 받았어, 후회는 없어.", False),                             # A denial in the sentence.
                 ("커피를 좋아한다", "팀장 제안을 받았어.", False)]                                            # Never said.
        for statement, said, expected in cases:
            with self.subTest(statement=statement, said=said):
                self.assertEqual(counsel.said_by_client(statement, said), expected)

    def test_health_values_never_borrow_a_finance_figure(self):
        # A goal 180 months away is not an LDL of 180 (sixth review 2026-11-01).
        money = counsel.ledger_money(None)
        text, problems = counsel.verify("LDL은 180이에요. 목표까지 180개월 남았어요.", allowed={"180"} | counsel._COUNTS, resolved=[], banned=set(),
                                        money=money, health_allowed=counsel._COUNTS)
        self.assertEqual((text, len(problems)), ("목표까지 180개월 남았어요.", 1))
        # A health item's percentage is a health value, checked against the health record, not a ledger rate (seventh review).
        text, problems = counsel.verify("체지방률은 25.3%예요. 주식 비중은 25.3%예요.", allowed=set(), resolved=[], banned=set(), money=money,
                                        health_allowed={"25.3"} | counsel._COUNTS)
        self.assertEqual((text, len(problems)), ("체지방률은 25.3%예요.", 1))

    def test_unread_findings_lend_no_health_numbers(self):
        route = {**ROUTE, "domains": [], "web": {"needed": "yes", "subject": "LDL 콜레스테롤 기준", "queries": ["LDL 콜레스테롤 기준치"]}}
        findings = {"findings": [
            {"claim": "LDL 기준은 177이다", "source_title": "Guide [unchecked]", "source_url": "https://a.example/x", "published_on": "", "publisher": "", "confidence": "confirmed"},
            {"claim": "LDL 목표는 130 미만이다", "source_title": "Guide [matched]", "source_url": "https://b.example/x", "published_on": "", "publisher": "", "confidence": "confirmed"}],
            "gaps": []}
        output = {"answer": "LDL 기준은 177이에요 [R1]. 흔한 목표는 130 미만이에요 [R2].", "follow_up": [], "remember": [], "health_statements": []}
        result, _ = self.answer([route, findings, output])
        self.assertEqual(result["text"], "흔한 목표는 130 미만이에요 [R2].")   # Only a page whose numbers matched lends a value.

    def test_amount_sentences_round_from_ledger_figures_only(self):
        money = counsel.ledger_money({"net_worth": "450000000", "loss": "-120000000", "share": "0.3521", "rate": "3.25", "all": "1.0",
                                      "annual_rate": "0.035", "completeness": {"unconverted": {"USD": "1234.5"}},
                                      "cash": {"currency": "USD", "value": "10000000"}})
        cases = {"순자산은 4.5억 원이에요.": None, "순자산은 4억 5천만 원쯤이에요.": None, "순자산은 45억 원이에요.": "amount not in ledger (45억 원)",
                 "주식 비중은 35%예요.": None, "주식 비중은 36%예요.": "amount not in ledger (36%)", "현금은 1,000억 원이에요.": "amount not in ledger (1,000억 원)",
                 "달러 예금은 1,234.5달러예요.": None, "다 합치면 100%예요.": None, "3년 뒤 12명과 일하게 돼요.": None, "12억 원이에요.": "amount not in ledger (12억 원)",
                 "금리는 3.25%예요.": None,
                 # Sign and currency count (fourth review): a loss is not a gain, and a won figure is not a dollar figure.
                 "손실은 -1.2억 원이에요.": None, "손실은 마이너스 1억 2천만 원이에요.": None, "수익은 1.2억 원이에요.": "amount not in ledger (1.2억 원)",
                 "예금은 10000000달러예요.": "amount not in ledger (10000000달러)", "예금은 1,000만 원이에요.": None,
                 # The currency word after a scale word counts, and a rate is a percentage exactly once (fifth review).
                 "현금은 1,000만 달러예요.": "amount not in ledger (1,000만 달러)", "4.5억 달러예요.": "amount not in ledger (4.5억 달러)",
                 "대출 금리는 0.035%예요.": "amount not in ledger (0.035%)", "대출 금리는 3.5%예요.": None}
        for sentence, expected in cases.items():
            with self.subTest(sentence=sentence):
                self.assertEqual(counsel._amount_problem(sentence, money), expected)
        # A field the payload leaves in the row's own currency (a dollar loan's credit limit) is a dollar figure (seventh review).
        money = counsel.ledger_money({"liabilities": [{"currency": "USD", "outstanding": "130000000", "credit_limit": "100000"},
                                                      {"currency": "KRW", "outstanding": "50000000", "credit_limit": "80000000"}]})
        cases = {"대출 한도는 10만 달러예요.": None, "대출 한도는 10만 원이에요.": "amount not in ledger (10만 원)", "한도는 8,000만 원이에요.": None}
        for sentence, expected in cases.items():
            with self.subTest(sentence=sentence):
                self.assertEqual(counsel._amount_problem(sentence, money), expected)
        self.assertEqual([(str(v), str(p), k, w) for v, p, k, w in counsel.amounts("4억 5천만 원과 35%, 85,000원, -2억 3천만 원")],
                         [("450000000", "10000000", "won", "4억 5천만 원"), ("35", "1", "pct", "35%"), ("85000", "1", "won", "85,000원"),
                          ("-230000000", "10000000", "won", "-2억 3천만 원")])
        # A figure the client wrote may be repeated, with a memo saying it is theirs; a figure of the model's own may not.
        output = {"answer": "말씀하신 월급 500만 원이면 괜찮아요. 순자산은 12억 원이에요.", "follow_up": [], "remember": [], "health_statements": []}
        result, _ = self.answer([{**ROUTE, "domains": ["finance"]}, output], question="월급 500만 원인데 팀장 제안을 받을까?")
        self.assertEqual(result["text"], "말씀하신 월급 500만 원이면 괜찮아요.")
        self.assertIn("말씀하신 금액(500만 원)은 원장에서 확인되지 않은 값이라 그 가정대로 답했어요.", result["memos"])

    def test_counsel_facts_stay_in_the_counsel_topic(self):
        # A fact no word list would call a health fact (fifth review 2026-11-01) is still kept away from the threads
        # that read the shared memory: everything a counsel keeps lives under the counsel topic.
        output = {"answer": "이직은 급하지 않아요.", "follow_up": [], "remember": [{"topic": "family", "fact": "아내는 수유 중이다"}],
                  "health_statements": []}
        result, _ = self.answer([{**ROUTE, "domains": ["finance"]}, output], question="아내는 수유 중이야. 이직을 해도 될까?")
        self.assertEqual(result["remember"], [("counsel", "아내는 수유 중이다")])
        folder = memory.directory(self.db)
        for topic, fact in result["remember"]:
            memory.add(folder, topic, fact, on=NOW.date(), source="메일")
        self.assertEqual([f.topic for f in memory.load(folder)], ["preference"])
        self.assertIn("아내는 수유 중이다", [f.text for f in memory.load(folder, private=True)])

    def test_domains_override_skips_the_router(self):
        output = {"answer": "메모만 보고 말씀드려요.", "follow_up": [], "remember": [], "health_statements": []}
        result, calls = self.answer([output], domains_override=[])
        self.assertEqual([(u, w) for u, w, _ in calls], [("counsel-answer", False)])
        self.assertEqual(result["domains"], [])
        self.assertEqual(result["data_status"]["finance"], "not asked")


def b64(text):
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def message(message_id, thread_id, subject, *, sender=ME, to=ME, labels=("SENT", "INBOX"), smith=False,
            text="팀장 제안을 받을까?", at="1793502000000"):
    headers = [{"name": "From", "value": f"Me <{sender}>"}, {"name": "To", "value": to}, {"name": "Subject", "value": subject},
               {"name": "Message-ID", "value": f"<{message_id}@mail>"}] + ([{"name": gmail.SMITH_HEADER, "value": "1"}] if smith else [])
    return {"id": message_id, "threadId": thread_id, "labelIds": list(labels), "internalDate": at,
            "payload": {"mimeType": "multipart/alternative", "headers": headers,
                        "parts": [{"mimeType": "text/plain", "body": {"data": b64(text)}}]}}


class CounselMailTests(unittest.TestCase):
    """The client's own `[상담]` mails are found by one sent-mail search, checked by header, and their threads read again."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Path(folder.name) / "smith.db"
        with closing(ledger.connect(self.db)):
            pass
        self.messages = [message("counselmsg01", "thread0002", "[상담] 팀장 제안"),
                         message("bookingmsg01", "thread0003", "상담 예약 확인"),
                         message("toothers01", "thread0004", "[상담] 같이 볼래?", to="friend@example.com")]
        self.reads, self.sent = [], []

    def poster(self, url, body, headers):
        return 200, {"access_token": "tok", "scope": f"{gmail.SCOPE} {gmail.READ_SCOPE}"}

    def reader(self, url, headers):
        self.reads.append(url)
        if "messages?q=" in url:   # Ids starting with "old" play mails the search has not indexed yet.
            return 200, {"messages": [{"id": m["id"]} for m in self.messages if "SENT" in m["labelIds"] and not any(
                h["name"] == gmail.SMITH_HEADER for h in m["payload"]["headers"]) and not m["id"].startswith("old")]}
        if "/messages/" in url:
            wanted = url.split("/messages/")[1].split("?")[0]
            found = next(m for m in self.messages if m["id"] == wanted)
            return 200, (found if "format=full" in url else {"id": wanted, "threadId": found["threadId"]})
        thread_id = url.split("/threads/")[1].split("?")[0]
        return 200, {"id": thread_id, "messages": [m for m in self.messages if m["threadId"] == thread_id]}

    def find(self, prefix="[상담]"):
        return qa.find(self.db, ("a", "b", "c"), ME, now=NOW, reader=self.reader, poster=self.poster, counsel_prefix=prefix)

    def test_prefixed_self_mail_is_a_question_and_the_search_is_a_coarse_filter(self):
        questions, skipped = self.find()
        self.assertEqual([(q.message_id, q.product, q.subject, q.text) for q in questions],
                         [("counselmsg01", "counsel", "[상담] 팀장 제안", "팀장 제안을 받을까?")])
        self.assertIn(("bookingmsg01", "thread0003", "no-counsel-prefix"), skipped)
        self.assertIn(("toothers01", "thread0004", "not-to-self"), skipped)   # Written to someone else: not a counsel.
        self.assertTrue(any("messages?q=" in url and "subject%3A%EC%83%81%EB%8B%B4" in url for url in self.reads))
        self.reads.clear()
        questions, _ = self.find(prefix=None)
        self.assertEqual(questions, [])
        self.assertFalse(any("messages?q=" in url for url in self.reads))

    def test_replies_continue_the_counsel_thread_and_the_answer_stays_in_it(self):
        def counsel_answerer(question, history):
            return "<p>답</p>", f"답({len(history)})", {"memos": [], "remember": [], "researched": False}

        def send(credentials, **kwargs):
            self.sent.append(kwargs)
            return f"answer-{len(self.sent)}"
        results = qa.poll(self.db, recipient=ME, credentials=("a", "b", "c"), now=NOW, build=lambda report: {}, secrets=[],
                          sender=send, reader=self.reader, poster=self.poster, counsel=counsel_answerer, counsel_prefix="[상담]")
        self.assertEqual([r["status"] for r in results], ["answered"])
        self.assertEqual((self.sent[0]["subject"], self.sent[0]["thread_id"], self.sent[0]["in_reply_to"]),
                         ("Re: [상담] 팀장 제안", "thread0002", "<counselmsg01@mail>"))
        with closing(ledger.connect_read_only(self.db)) as conn:
            self.assertEqual(ledger.counsel_threads(conn, since=NOW), {"thread0002"})
        # Smith's reply and the client's follow-up land in the thread: only the follow-up is a question, with history.
        self.messages += [message("smithreply01", "thread0002", "Re: [상담] 팀장 제안", smith=True, at="1793502600000"),
                          message("followup01", "thread0002", "Re: [상담] 팀장 제안", text="보상은 어떻게 돼?", at="1793503200000")]
        self.reads.clear()
        questions, skipped = self.find()
        self.assertEqual([(q.message_id, q.product) for q in questions], [("followup01", "counsel")])
        self.assertIn(("smithreply01", "thread0002", "smith-message"), skipped)
        self.assertFalse(any("/messages/counselmsg01?" in url for url in self.reads))   # Known ids are not looked up again.
        results = qa.poll(self.db, recipient=ME, credentials=("a", "b", "c"), now=NOW, build=lambda report: {}, secrets=[],
                          sender=send, reader=self.reader, poster=self.poster, counsel=counsel_answerer, counsel_prefix="[상담]")
        self.assertEqual([r["status"] for r in results], ["answered"])
        self.assertEqual(self.sent[1]["text"], "답(1)")

    def test_the_frequent_routine_reads_counsel_threads_only(self):
        # A report thread exists too; the full routine reads it, the counsel-only routine never touches it and
        # re-reads only the counsel threads of the last COUNSEL_RECENT days (a reply the search has not indexed yet in
        # an older thread waits for the full routine; one the search finds is picked up at once).
        with closing(ledger.connect(self.db)) as conn:
            ledger.claim_report(conn, report_id="r1", slot=None, kind="monday", trigger="manual", created_at=NOW, as_of=NOW,
                                baseline=None, missed_slots=[])
            ledger.finish_report(conn, report_id="r1", status="sent", finished_at=NOW, subject="[Smith][재무] 보고", message_id="reportmsg01")
            ledger.claim_question(conn, message_id="oldcounsel01", thread_id="thread0009", report_id=None,
                                  received_at=NOW - timedelta(days=10), now=NOW, product="counsel", question_text="옛 질문")
            ledger.finish_question(conn, message_id="oldcounsel01", status="answered", now=NOW, answer_message_id="oldanswer01")
        self.messages += [message("reportmsg01", "thread0001", "[Smith][재무] 보고", smith=True),
                          message("oldcounsel01", "thread0009", "[상담] 옛 질문", at="1792638000000"),
                          message("oldfollow01", "thread0009", "Re: [상담] 옛 질문", text="그때 그 얘기 말인데", at="1793503800000")]
        questions, _ = qa.find(self.db, ("a", "b", "c"), ME, now=NOW, reader=self.reader, poster=self.poster, counsel_prefix="[상담]",
                               counsel_only=True)
        self.assertEqual([q.message_id for q in questions], ["counselmsg01"])
        self.assertFalse(any("/messages/reportmsg01?" in url or "/threads/thread0001?" in url or "/threads/thread0009?" in url
                             for url in self.reads))
        self.reads.clear()
        questions, _ = self.find()   # The full routine: the report thread and the older counsel thread are read.
        self.assertEqual(sorted(q.message_id for q in questions), ["counselmsg01", "oldfollow01"])
        self.assertTrue(any("/threads/thread0001?" in url for url in self.reads))

    def test_mail_answer_flags_parse(self):
        import argparse

        from smith import mail_command
        parser = argparse.ArgumentParser()
        mail_command.add_parser(parser.add_subparsers(dest="command"))
        args = parser.parse_args(["mail", "answer", "--counsel-only", "--log"])
        self.assertEqual((args.action, args.counsel_only, args.log), ("answer", True, True))
        self.assertFalse(parser.parse_args(["mail", "answer"]).counsel_only)

    def test_sent_mail_search_follows_later_pages_within_a_bound(self):
        pages = {"": {"messages": [{"id": "a1"}], "nextPageToken": "p2"}, "p2": {"messages": [{"id": "b2"}], "nextPageToken": "p3"},
                 "p3": {"messages": [{"id": "c3"}]}}
        asked = []

        def getter(url, headers):
            asked.append(url)
            token = url.split("pageToken=")[1] if "pageToken=" in url else ""
            return 200, pages[token]
        self.assertEqual(gmail.sent_with_subject("tok", "상담", after=1, getter=getter), ["a1", "b2", "c3"])
        self.assertEqual(len(asked), 3)
        pages["p3"]["nextPageToken"] = "p4"
        pages["p4"] = {"messages": [{"id": "d4"}], "nextPageToken": "p5"}
        pages["p5"] = {"messages": [{"id": "e5"}]}
        self.assertEqual(gmail.sent_with_subject("tok", "상담", after=1, getter=getter), ["a1", "b2", "c3", "d4"])   # Bounded.
        with self.assertRaises(gmail.MailError):
            gmail.sent_with_subject("tok", "[상담]", after=1, getter=getter)

    def test_counsel_section_is_validated(self):
        config = load_config(EXAMPLE)
        self.assertEqual(config["counsel"], {"enabled": False, "prefix": "[상담]", "max_notes": 8})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.toml"
            for before, after in (('prefix = "[상담]"', 'prefix = "[Smith][상담]"'), ('max_notes = 8', 'max_notes = 0'),
                                  ('prefix = "[상담]"', 'prefix = "상담"'), ('max_notes = 8', 'max_notes = true')):
                with self.subTest(after=after):
                    path.write_text(EXAMPLE.read_text(encoding="utf-8").replace(before, after), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_config(path)


if __name__ == "__main__":
    unittest.main()
