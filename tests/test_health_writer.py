"""The health writer's verification (health design §7, v2 + review 2026-10-25): no number in the headline, numbers
only from the input, no diagnosis, inference or dosing wording (figures or Korean counts), item lines and the verdict
never against code's trend read through which way is better, do/avoid per goal, one to three complete actions with
known targets, no resolved finding, no genetics-only ground, prose cut at sentences."""
import unittest
from datetime import date

from smith import health, health_letter, health_writer

try:
    from test_health_letter import CONFIG, RAW, SUNDAY
except ImportError:   # Run as `python -m unittest tests.test_health_writer`.
    from tests.test_health_letter import CONFIG, RAW, SUNDAY


def _data(topic: str = "vascular") -> dict:
    raw = {**RAW, "findings": RAW["findings"] + [
        {"id": "iop-2025", "area": "눈", "summary": "우측 안압 높음 (23 mmHg)", "on": "2025-06-27", "track": "eye", "severity": "see_doctor",
         "source": "s p.3", "resolved_on": "2026-10-25", "resolved_note": "재측정 정상"}]}
    return health_letter.assemble(health.parse(raw), CONFIG, day=SUNDAY, today=SUNDAY, topic=topic,
                                  previous={"week_key": "2026-W43", "actions": [{"what": "걷기", "amount": "30분"}]})


def _action(**over) -> dict:
    base = {"category": "food", "what": "귀리 아침", "amount": "주 5일 한 컵", "why": "식이섬유가 LDL을 내립니다",
            "check": "다음 검진의 LDL", "targets": ["ldl"]}
    return {**base, **over}


def _output(**over) -> dict:
    base = {"headline": "", "verdict": "", "items": [], "goal_lines": [], "actions": [_action()], "review_line": "", "question": ""}
    return {**base, **over}


class VerifyTests(unittest.TestCase):
    def test_good_output_passes_whole(self):
        data = _data()
        output = _output(headline="나쁜 콜레스테롤이 다시 오르고 있습니다", verdict="범위 밖으로 올라왔습니다. 걱정할 수준은 아니지만 지금 식단을 잡을 구간입니다.",
                         items=[{"code": "ldl", "line": "2021년 118에서 올라 범위 밖입니다."}, {"code": "tg", "line": "정상 범위 안에서 내려오는 중입니다."}],
                         goal_lines=[{"key": "ldl", "do": "귀리 아침", "avoid": "튀김"}, {"key": "posture", "do": "턱 당기기", "avoid": "폰 숙여 보기"}],
                         actions=[_action(), _action(category="exercise", what="빠르게 걷기", amount="30분 × 주 5일", why="HDL을 올립니다",
                                                     check="주 150분", targets=["hdl"])],
                         review_line="지난주 걷기 30분은 추정 활동 105~200분 안에 들어 있습니다.", question="테니스는 어느 요일에 하십니까?")
        prose, problems = health_writer.verify(output, data, health_letter.payload(data))
        self.assertEqual(problems, [])
        self.assertEqual(set(prose["items"]), {"ldl", "tg"})
        self.assertEqual(set(prose["goal_lines"]), {"ldl", "posture"})
        self.assertEqual(len(prose["actions"]), 2)
        self.assertIn("review_line", prose)

    def test_rules_drop_the_field_and_name_the_reason(self):
        data = _data()
        payload = health_letter.payload(data)
        cases = [
            ({"headline": "요산 7.6, 열쇠는 체지방 28.8%입니다"}, "number in the headline"),
            ({"headline": "나쁜 콜레스테롤이 좋아지고 있습니다"}, "headline: contradicts the trend of ldl"),   # Code: worsening.
            ({"verdict": "LDL이 187까지 오르면 위험합니다."}, "verdict: number not in input"),
            ({"verdict": "이 수치는 고지혈증 질환입니다."}, "forbidden wording"),
            ({"verdict": "이 정도면 고혈압일 수 있습니다."}, "forbidden wording"),                          # Inference.
            ({"verdict": "통풍이 의심됩니다."}, "forbidden wording"),
            ({"verdict": "오메가3를 1000mg씩 드세요."}, "forbidden wording"),
            ({"verdict": "비타민 D를 하루 두 알 드십시오."}, "forbidden wording"),                           # Korean count.
            ({"verdict": "약을 끊으셔도 됩니다."}, "forbidden wording"),
            ({"verdict": "자세한 내용은 https://example.com 참고."}, "link or contact"),
            ({"verdict": "우측 안압 높음 소견은 이제 괜찮습니다."}, "mentions a resolved finding"),
            ({"verdict": "나쁜 콜레스테롤은 내려가고 있어 안심입니다. 체지방률은 그대로입니다."}, "verdict: contradicts the trend of ldl"),
            ({"verdict": "중성지방은 내려오고 있어 좋아졌습니다. 나쁜 콜레스테롤은 올라 범위 밖입니다."}, None),   # Both true.
            ({"verdict": "체중은 정상 범위에서 안정되어 있습니다."}, None),                                  # Flat said as steady.
            ({"items": [{"code": "psa", "line": "정상입니다."}]}, "items: unknown code"),
            ({"items": [{"code": "ldl", "line": "좋아지고 있어 안심입니다."}]}, "contradicts the trend"),   # Code says worsening.
            ({"items": [{"code": "ldl", "line": "올라서 범위 밖이라 식단을 손볼 때입니다."}]}, None),         # Up is the truth.
            ({"items": [{"code": "tg", "line": "내려와 좋아지는 중입니다."}]}, None),                       # Down is better for TG.
            ({"items": [{"code": "tg", "line": "올라가고 있어 걱정입니다."}]}, "contradicts the trend"),     # TG fell.
            ({"goal_lines": [{"key": "sleep", "do": "x", "avoid": "y"}]}, "goal_lines: unknown key"),
            ({"actions": [_action(category="pill")]}, "actions: incomplete"),
            ({"actions": [_action(targets=["psa"])]}, "no known target"),
            ({"actions": [_action(why="유전자 검사에서 중성지방 주의라서", targets=["ldl"])]}, None),      # Grounded in a measured value.
            ({"actions": [_action(why="유전자 검사에서 주의라서", targets=["apob"])]}, "genetics as the only ground"),   # Known, unmeasured.
            ({"actions": [_action(), _action(), _action(), _action()]}, "more than three"),
        ]
        for patch, expected in cases:
            with self.subTest(expected=expected or "ok", patch=str(patch)[:50]):
                prose, problems = health_writer.verify(_output(**patch), data, payload)
                if expected is None:
                    self.assertEqual(problems, [])
                else:
                    self.assertTrue(any(expected in p for p in problems), problems)
        prose, problems = health_writer.verify(_output(actions=[_action(targets=["psa"])]), data, payload)
        self.assertNotIn("actions", prose)                                 # Fallback actions will show instead.
        self.assertIn("actions: none usable, guideline fallback shown", problems)

    def test_rising_is_good_news_for_an_item_whose_better_is_up(self):
        raw = {**RAW, "observations": RAW["observations"] + [
            {"code": "hdl", "value": "50", "ref": "60~", "on": "2025-06-27", "flag": "low", "source": "s p.2"}]}
        data = health_letter.assemble(health.parse(raw), CONFIG, day=SUNDAY, today=SUNDAY, topic="vascular")
        hdl = next(i for i in data["items"] if i["code"] == "hdl")
        self.assertEqual(hdl["trend"], "improving")                      # 50 -> 58, up is better.
        payload = health_letter.payload(data)
        prose, problems = health_writer.verify(_output(verdict="좋은 콜레스테롤(HDL)이 올라 좋아지고 있습니다.",
                                                       items=[{"code": "hdl", "line": "올라서 좋아지는 중입니다."}]), data, payload)
        self.assertEqual(problems, [])
        prose, problems = health_writer.verify(_output(items=[{"code": "hdl", "line": "내려가고 있어 걱정입니다."}]), data, payload)
        self.assertTrue(any("contradicts" in p for p in problems), problems)

    def test_an_action_naming_a_ruled_out_food_is_dropped(self):
        raw = {**RAW, "constraints": [{"text": "생선 알레르기", "since": None, "source": "메일"}]}
        data = health_letter.assemble(health.parse(raw), CONFIG, day=SUNDAY, today=SUNDAY, topic="vascular")
        output = _output(actions=[_action(what="고등어를 주 2회", amount="한 토막"), _action()],
                         goal_lines=[{"key": "ldl", "do": "연어와 귀리", "avoid": "튀김"}])
        prose, problems = health_writer.verify(output, data, health_letter.payload(data))
        self.assertEqual([a["what"] for a in prose["actions"]], ["귀리 아침"])
        self.assertNotIn("ldl", prose["goal_lines"])
        self.assertEqual(len([p for p in problems if "ruled out" in p]), 2)

    def test_review_line_only_when_there_was_a_previous_letter(self):
        data = health_letter.assemble(health.parse(RAW), CONFIG, day=SUNDAY, today=SUNDAY, topic="vascular")
        prose, _ = health_writer.verify(_output(review_line="지난주 잘하셨습니다."), data, health_letter.payload(data))
        self.assertNotIn("review_line", prose)

    def test_long_text_is_cut_at_a_sentence_end_and_paragraphs_survive(self):
        sentence = "요산은 퓨린이 남긴 찌꺼기입니다. "
        long = sentence * 80 + "마지막 문장은 잘려도 됩니다"
        cut = health_writer._clean(long, 400)
        self.assertTrue(cut.endswith("찌꺼기입니다."), cut[-20:])
        self.assertLessEqual(len(cut), 400)
        self.assertEqual(health_writer._clean("첫 단락입니다.\n\n둘째  단락입니다.", 100), "첫 단락입니다.\n둘째 단락입니다.")
        self.assertTrue(health_writer._clean("가" * 50, 20).endswith("…"))

    def test_tokens_cover_values_dates_and_coach_sizes(self):
        found = health_writer.tokens({"a": "152", "b": "2026-07-07", "c": [7.5, {"d": "0~129"}]})
        for token in ("152", "2026", "07", "7.5", "129", "30"):
            self.assertIn(token, found, token)
        self.assertEqual(date(2026, 7, 7).isoformat(), "2026-07-07")


if __name__ == "__main__":
    unittest.main()
