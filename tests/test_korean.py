"""Natural Korean in the client's prose (report design §34): what code fixes in place, what it sends back for one
rewrite, and how the writers, the product review and the first reader carry it."""
import unittest

from smith import korean, narrative, picks, reader


class TidyTest(unittest.TestCase):
    def test_double_passives_take_their_single_form_whatever_the_ending(self):
        for double, single in (("이 방식이 가장 낫다고 판단되어집니다.", "이 방식이 가장 낫다고 판단됩니다."),
                               ("그렇게 보여집니다.", "그렇게 보입니다."),
                               ("금리가 내릴 것으로 보여진다.", "금리가 내릴 것으로 보인다."),
                               ("미뤄지는 것으로 보여져서 기다렸습니다.", "미뤄지는 것으로 보여서 기다렸습니다."),
                               ("한도가 조정되어졌습니다.", "한도가 조정되었습니다."),
                               ("자금이 셋으로 나뉘어집니다.", "자금이 셋으로 나뉩니다."),
                               ("잊혀진 계절", "잊힌 계절")):
            with self.subTest(text=double):
                self.assertEqual(korean.tidy(double), single)

    def test_single_passives_and_other_verbs_are_left_alone(self):
        text = "종목을 보여 드립니다. 보여줍니다. 만들어집니다. 이루어진 계획은 되어야 합니다. 여겨집니다."
        self.assertEqual(korean.tidy(text), text)

    def test_dashes_become_range_marks_signs_or_commas(self):
        for before, after in (("금리 3–5% 구간", "금리 3~5% 구간"), ("2024—2026년", "2024~2026년"),
                              ("핵심은 하나 — 현금입니다.", "핵심은 하나, 현금입니다."), ("A—B", "A, B"),
                              ("— 요약", "요약"), ("끝 —", "끝"), ("하이픈-은 그대로", "하이픈-은 그대로"),
                              # A sign is kept as one (Codex review 2026-10-27): the number check reads "-3.4%".
                              ("최저 –3도입니다.", "최저 -3도입니다."), ("수익률은 –3.4%입니다.", "수익률은 -3.4%입니다."),
                              ("체감 –3~–1도", "체감 -3~-1도"), ("(–3%)", "(-3%)"),
                              # Glued to the word before it, a dash before a figure is a separator, never a sign.
                              ("금리는 다음과 같습니다—3.4%.", "금리는 다음과 같습니다, 3.4%."), ("A—3", "A, 3")):
            with self.subTest(text=before):
                self.assertEqual(korean.tidy(before), after)


class ProblemTest(unittest.TestCase):
    def test_patterns_that_need_rewording_are_named_with_their_words(self):
        for text, found in (("데이터 분석을 통해 통찰을 얻습니다.", ("calque", "을 통해")),
                            ("AI 규제에 대해 논의해야 합니다.", ("calque", "에 대해")),
                            ("금리에 대한 민감도가 큽니다.", ("calque", "에 대한")),
                            ("이 문제에 있어서 중요한 것은 속도입니다.", ("calque", "에 있어서")),
                            ("강한 경쟁력을 가지고 있습니다.", ("calque", "을 가지고 있")),
                            ("참석하기 위해서는 신청이 필요합니다.", ("calque", "기 위해서는")),
                            ("긴장으로부터의 해방입니다.", ("calque", "으로부터의")),
                            ("결론적으로 도구를 하나 줄였습니다.", ("cliche", "결론적으로")),
                            ("크게 세 가지로 나눌 수 있습니다.", ("cliche", "크게 세 가지로")),
                            ("주목할 만한 변화입니다.", ("cliche", "주목할 만한")),
                            ("문제가 생길 가능성이 있을 수 있습니다.", ("hedge", "가능성이 있을 수 있")),
                            ("값을 코드에 박아 둡니다.", ("slang", "박아 둡")),
                            ("의견을 뒤집을 조건입니다.", ("slang", "뒤집"))):
            with self.subTest(text=text):
                self.assertEqual(korean.problem(text), found)

    def test_plain_korean_passes(self):
        for text in ("성과의 절반은 세금입니다.", "계좌에 있는 현금을 봅니다.", "노후를 위해 저축합니다.", "그렇게 보입니다.",
                     "대책을 세웁니다.", "경쟁력이 강합니다.", "금리 1.5%p 상승과 소득 12개월 중단이 함께 오면 2.7개월을 버팁니다."):
            with self.subTest(text=text):
                self.assertIsNone(korean.problem(text))

    def test_every_passage_is_listed_in_text_order(self):
        self.assertEqual(korean.problems("결론적으로 분산을 통해 위험을 줄이고, 그 점에 대해 설명합니다."),
                         [("cliche", "결론적으로"), ("calque", "을 통해"), ("calque", "에 대해")])
        self.assertEqual(korean.problems("분산해서 위험을 줄입니다."), [])

    def test_advice_names_the_replacement_once_per_kind(self):
        self.assertIn("~하기 위해서는 -> ~하려면", korean.advice("calque"))
        self.assertEqual(korean.advice("hedge").count(";"), 0)


class CarriedByWritersTest(unittest.TestCase):
    def test_every_writer_prompt_carries_the_rule_and_the_report_checks_it(self):
        from smith import daily_qa, daily_writer, health_qa, health_writer, qa, team
        for module in (narrative, qa, team, picks, daily_writer, daily_qa, health_writer, health_qa):
            with self.subTest(module=module.__name__):
                prompt = getattr(module, "EDITOR_PROMPT", None) or getattr(module, "SELECT_PROMPT", None) or module.SYSTEM_PROMPT
                self.assertIn(korean.RULES, prompt)
        self.assertEqual(narrative.unnatural(["보수가 낮다는 점에서 좋고, 분산을 통해 위험을 줄입니다."]), "unnatural-korean:을 통해")
        self.assertIsNone(narrative.unnatural(["보수가 낮아서 좋고, 분산해서 위험을 줄입니다."]))
        self.assertIn("unnatural-korean", narrative.SOFT)

    def test_a_pick_written_in_translated_korean_is_sent_back_with_the_reason(self):
        faults = picks.style_faults({"picks": [{"symbol": "VOO", "rank": "1", "reason": "낮은 보수를 통해 비용을 아낍니다.",
                                                "condition": "x"}]})
        self.assertEqual([f["symbol"] for f in faults], ["VOO"])
        self.assertTrue(faults[0]["reason"].startswith("reads as translated"))
        self.assertTrue(faults[0]["reason"].endswith(": 를 통해"))

    def test_the_corrections_are_recorded_and_summed_up(self):
        found: list[dict[str, str]] = []
        self.assertEqual(korean.tidy("요산이 안정되어진 것으로 보여집니다 — 좋습니다.", found), "요산이 안정된 것으로 보입니다, 좋습니다.")
        self.assertEqual(found, [{"kind": "double-passive", "before": "되어진", "after": "된"},
                                 {"kind": "double-passive", "before": "보여집", "after": "보입"},
                                 {"kind": "dash", "before": "—", "after": ","}])
        self.assertEqual(korean.fixes("그대로입니다."), [])
        stats = {"tidied": found, "flagged": [{"path": "situation", "kind": "calque", "words": "을 통해"},
                                              {"path": "watch[0]", "kind": "cliche", "words": "결론적으로"}],
                 "remaining": [{"path": "situation", "kind": "calque", "words": "을 통해"}],
                 "reader_notes": [{"path": "direction", "quote": "것입니다"}]}
        self.assertEqual(korean.summary(stats), "한국어 교정: 고정 수정 3건(이중 피동 2, 대시 1), 재작성 요청 2건(번역 칼크 1, 상투구 1: "
                                                "을 통해, 결론적으로), 남은 것 1건(번역 칼크 1: 을 통해), 첫 독자 지적 1건")
        self.assertEqual(korean.summary({"tidied": []}), "한국어 교정: 고정 수정 0건")   # The daily mail and the health letter.
        self.assertIsNone(korean.summary(None))
        self.assertEqual(narrative.korean_flags(["situation:unnatural-korean:에 대해", "watch[0]:no-source"]),
                         [{"path": "situation", "kind": "calque", "words": "에 대해"}])
        output = {"situation": {"text": "그렇게 보여집니다.", "refs": []}, "direction": {"text": "", "refs": []},
                  "portfolio": {"text": "", "refs": []}, "watch": [{"item": "금리 3–5%", "why": "w", "refs": []}]}
        self.assertEqual(narrative.korean_fixes(output), [{"path": "situation", "kind": "double-passive", "before": "보여집", "after": "보입"},
                                                          {"path": "watch[0]", "kind": "range-dash", "before": "–", "after": "~"}])
        output["watch"][0]["why"] = "분산을 통해 위험을 줄입니다."
        self.assertEqual(narrative.korean_left(output), [{"path": "watch[0]", "kind": "calque", "words": "을 통해"}])

    def test_the_first_reader_may_mark_unnatural_korean(self):
        shown = [{"path": "situation", "section": "이번 주 판단", "text": "데이터 분석을 통해 봅니다."}]
        raw = [{"path": "situation", "quote": "분석을 통해", "problem": "unnatural", "why": "번역투입니다.", "suggestion": "분석해"}]
        self.assertEqual([n["problem"] for n in reader.clean_notes(raw, shown)], ["unnatural"])


if __name__ == "__main__":
    unittest.main()
