import tempfile
import unittest
import unittest.mock
from datetime import datetime, timezone
from pathlib import Path

from smith import daily_qa, headless

NOW = datetime(2026, 11, 20, 1, 0, tzinfo=timezone.utc)
CTX = {"today": {"date": "2026-11-20", "agenda": {"events": [{"title": "김민수 교수 미팅", "all_day": True}]}},
       "window": {"events": [], "tasks": []}, "upcoming_45_days": [],
       "news_links": {"R1": ("금통위 앞두고 관망", "https://www.yna.co.kr/view/x")}}


def fake_caller(script):
    """Plays the model: pops one canned output per call and records (use_case, web) in order."""
    calls = []

    def call(text, system_prompt, schema, use_case, timeout, web):
        calls.append((use_case, web, text))
        step = script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step, "0.01"
    return call, calls


def verified(items):
    for i, item in enumerate(items, 1):
        item["ref"] = f"R{i + 1}"
        item["check"] = {"status": "reachable"}
    return items, {}


class DailyAnswerTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Path(folder.name) / "smith.db"

    def test_plain_question_is_answered_from_context_only(self):
        call, calls = fake_caller([{"answer": {"text": "오늘은 교수님 미팅 하나뿐입니다. 자세한 건 https://x.y 참고", "refs": ["R1", "R7"]},
                                    "follow_up": ["내일 일정도 볼까요?"], "remember": [{"topic": "family", "fact": "와이프는 꽃을 좋아한다"}],
                                    "research": {"needed": "no", "subject": "", "kind": "none", "queries": [], "anchors": []}}])
        result = daily_qa.answer(self.db, CTX, "오늘 일정 뭐야?", now=NOW, received_at=NOW, history=[], secrets=[], tz="Asia/Seoul",
                                 executable=Path("claude"), caller=call)
        self.assertEqual([(u, w) for u, w, _ in calls], [("daily-answer", False)])
        self.assertEqual(result["text"], "오늘은 교수님 미팅 하나뿐입니다. 자세한 건 참고")   # URL stripped.
        self.assertEqual(result["refs"], ["R1"])                                              # Unknown ref dropped.
        self.assertEqual(result["remember"], [("family", "와이프는 꽃을 좋아한다")])
        self.assertFalse(result["researched"])
        html, text = daily_qa.render("오늘 일정 뭐야?", result)
        self.assertIn("SMITH · 하루 일과 답변", html)
        self.assertIn("[R1] 금통위 앞두고 관망", html)
        self.assertIn("기억해 두겠습니다", html)

    def test_research_request_runs_one_bounded_web_call_and_cites_findings(self):
        first = {"answer": {"text": "찾아보고 말씀드리겠습니다.", "refs": []}, "follow_up": [], "remember": [],
                 "research": {"needed": "yes", "subject": "김민수 교수", "kind": "person",
                              "queries": ["김민수 교수 서울대 반도체"], "anchors": ["서울대", "반도체"]}}
        findings = {"findings": [
            {"claim": "서울대 전기정보공학부 교수로 반도체 소자를 연구한다", "source_title": "학부 교수진", "source_url": "https://ee.snu.ac.kr/people/1",
             "published_on": "", "publisher": "서울대", "confidence": "confirmed"},
            {"claim": "집 주소는 ...", "source_title": "x", "source_url": "http://insecure.example/x", "published_on": "", "publisher": "", "confidence": "unconfirmed"}],
            "identity": {"matched_anchors": ["서울대", "반도체"], "note": "두 단서가 일치"}, "gaps": []}
        second = {"answer": {"text": "김민수 교수는 서울대 전기정보공학부 교수입니다 [R2]. 수상 경력은 [R9] 참고.", "refs": ["R2"]},
                  "follow_up": [], "remember": [{"topic": "other", "fact": "김민수 교수는 반도체 전문가다"},
                                                {"topic": "plan", "fact": "다음 주 교수님과 두 번째 미팅을 잡을 계획이다"}],
                  "research": {"needed": "no", "subject": "", "kind": "none", "queries": [], "anchors": []}}
        call, calls = fake_caller([first, findings, second])
        with unittest.mock.patch.object(daily_qa.sources, "verify", verified):
            result = daily_qa.answer(self.db, CTX, "오늘 만나는 김민수 교수에 대해 조사해서 알려줘", now=NOW, received_at=NOW, history=[],
                                     secrets=[], tz="Asia/Seoul", executable=Path("claude"), caller=call)
        self.assertEqual([(u, w) for u, w, _ in calls], [("daily-answer", False), ("daily-research", True), ("daily-answer-researched", False)])
        self.assertNotIn("source_url", calls[2][2])                                           # URLs never go back in.
        self.assertTrue(result["researched"])
        self.assertEqual(result["refs"], ["R2"])
        self.assertEqual(result["text"], "김민수 교수는 서울대 전기정보공학부 교수입니다 [R2]. 수상 경력은  참고.")   # R9 unknown: removed.
        self.assertEqual(result["links"]["R2"], ("학부 교수진", "https://ee.snu.ac.kr/people/1"))
        self.assertEqual(result["remember"], [("plan", "다음 주 교수님과 두 번째 미팅을 잡을 계획이다")])    # Third-party fact dropped.
        self.assertEqual(result["memos"], [])

    def test_failed_research_still_answers_with_a_memo(self):
        first = {"answer": {"text": "찾아보겠습니다.", "refs": []}, "follow_up": [], "remember": [],
                 "research": {"needed": "yes", "subject": "홍길동 대표", "kind": "person", "queries": ["홍길동 대표"], "anchors": []}}
        second = {"answer": {"text": "공개 자료에서 확인된 내용이 없습니다.", "refs": []}, "follow_up": [], "remember": [],
                  "research": {"needed": "no", "subject": "", "kind": "none", "queries": [], "anchors": []}}
        call, calls = fake_caller([first, headless.HeadlessError("timeout"), second])
        result = daily_qa.answer(self.db, CTX, "홍길동 대표 조사해줘", now=NOW, received_at=NOW, history=[], secrets=[],
                                 tz="Asia/Seoul", executable=Path("claude"), caller=call)
        self.assertEqual(len(calls), 3)
        self.assertTrue(result["memos"][0].startswith("공개 자료에서 확인된 내용을 찾지 못했습니다"))


if __name__ == "__main__":
    unittest.main()
