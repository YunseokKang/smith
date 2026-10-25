"""Replies in a health-letter thread (health design §8): the danger gate answers without a model, the answer is
verified sentence by sentence, the client's own statements go to the health notes and never to the shared memory,
and the rendering carries the boundary."""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from smith import health, health_qa, headless

try:
    from test_health_letter import CONFIG, RAW
except ImportError:
    from tests.test_health_letter import CONFIG, RAW

NOW = datetime(2026, 10, 26, 3, 0, tzinfo=timezone.utc)
SNAPSHOT = {"week_key": "2026-W43", "day": "2026-10-25", "topic": "vascular", "prose": {"headline": "LDL이 오르고 있습니다", "verdict": "식단부터."},
            "actions": [{"what": "귀리 아침", "amount": "주 5일", "why": "식이섬유", "check": "다음 검진 LDL", "category": "food"}]}


class QATests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Path(folder.name) / "smith.db"
        health.import_batch(health.path(self.db), RAW)
        self.ctx = health_qa.context(self.db, CONFIG, SNAPSHOT, now=NOW, tz="Asia/Seoul")

    def test_context_is_the_letter_plus_the_ledger_and_masked(self):
        self.assertEqual(self.ctx["letter"]["headline"], "LDL이 오르고 있습니다")
        self.assertEqual(self.ctx["letter"]["actions"][0]["what"], "귀리 아침")
        self.assertTrue(any(i["code"] == "ldl" for i in self.ctx["items"]))
        self.assertIsNone(self.ctx["wearable"]["last_7_days"])             # No Fitbit days stored.
        self.assertNotIn("s p.1", str(self.ctx))

    def test_danger_symptom_is_answered_by_code_without_a_model(self):
        calls = []
        result = health_qa.answer(self.db, self.ctx, "어제부터 가슴 통증이 있고 숨이 차요. 운동해도 되나요?", now=NOW, received_at=NOW,
                                  history=[], secrets=[], tz="Asia/Seoul", caller=lambda text: calls.append(text) or {})
        self.assertTrue(result["emergency"])
        self.assertIn("119", result["text"])
        self.assertEqual(calls, [])
        html, text = health_qa.render("가슴 통증", result)
        self.assertIn("119", html)
        self.assertIn("생활 코치", text)

    def test_answer_is_verified_sentence_by_sentence_and_statements_are_noted(self):
        def caller(text):
            return {"answer": "나쁜 콜레스테롤은 2021년 118에서 152로 올라 범위 밖입니다. 고지혈증 질환입니다. 오메가3를 1000mg씩 드세요.\n\n"
                              "이번 주는 귀리 아침을 주 5일 이어 가십시오. 자세한 설명은 https://example.com 을 보십시오.",
                    "follow_up": ["귀리 대신 현미도 되나요?"],
                    "remember": ["나는 10월에 안과에서 안압을 재측정해 16으로 정상이었다", "아내는 혈압약을 먹는다"]}
        result = health_qa.answer(self.db, self.ctx, "LDL이 왜 올랐나요?", now=NOW, received_at=NOW, history=[], secrets=[],
                                  tz="Asia/Seoul", caller=caller)
        self.assertEqual(result["text"], "나쁜 콜레스테롤은 2021년 118에서 152로 올라 범위 밖입니다.\n\n이번 주는 귀리 아침을 주 5일 이어 가십시오.")
        self.assertEqual(len(result["problems"]), 3)                        # Diagnosis, dose, link dropped.
        self.assertEqual(result["remember"], [])                             # Never the shared memory...
        self.assertEqual(result["noted"], ["나는 10월에 안과에서 안압을 재측정해 16으로 정상이었다"])   # ...the health notes, first person only.
        notes = (health.path(self.db) / "client-notes.md").read_text(encoding="utf-8")
        self.assertIn("2026-10-26 (메일 답장): 나는 10월에 안과에서", notes)
        self.assertNotIn("아내", notes)
        html, text = health_qa.render("LDL이 왜 올랐나요?", result)
        self.assertIn("귀리 대신 현미", html)
        self.assertIn("건강 기록에 적어 두겠습니다", html)
        with self.assertRaises(headless.HeadlessError):
            health_qa.answer(self.db, self.ctx, "질문", now=NOW, received_at=NOW, history=[], secrets=[], tz="Asia/Seoul",
                             caller=lambda text: {"answer": "오메가3를 1000mg씩 드세요.", "follow_up": [], "remember": []})


if __name__ == "__main__":
    unittest.main()
