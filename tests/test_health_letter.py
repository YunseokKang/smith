"""The weekly health letter's assembly and rendering (FR-38, user review 2026-10-25): the topic rotation, the trend and
level words code computes, the goal dashboard with do/avoid lines, the activity estimate against the guideline, the
model payload's privacy, line charts, and a letter that stands without prose. Synthetic ledger."""
import unittest
from datetime import date
from decimal import Decimal

from smith import health, health_chart, health_html, health_letter

CONFIG = {"app": {"timezone": "Asia/Seoul"},
          "health": {"topics": ["vascular", "uric-weight", "nutrition-gut", "balance", "stomach-habits"], "goals": ["posture", "body_fat", "ldl"]}}
RAW = {
    "observations": [
        {"code": "ldl", "value": "118", "ref": "0~129", "on": "2021-09-06", "flag": "normal", "method": "direct", "source": "s p.1"},
        {"code": "ldl", "value": "152", "ref": "0~129", "on": "2026-07-07", "flag": "high", "method": "direct", "source": "s p.2"},
        {"code": "ldl", "value": "146", "ref": "0~129", "on": "2026-07-07", "flag": "high", "method": "calculated", "source": "s p.2"},
        {"code": "hdl", "value": "58", "ref": "60~", "on": "2026-07-07", "flag": "low", "source": "s p.2", "uncertain": True},
        {"code": "tg", "value": "140", "ref": "0~149", "on": "2025-06-27", "flag": "normal", "source": "s p.2"},
        {"code": "tg", "value": "120", "ref": "0~149", "on": "2026-07-07", "flag": "normal", "source": "s p.2"},
        {"code": "body_fat_pct", "value": "27.5", "ref": "10~20", "on": "2025-06-27", "flag": "high", "source": "s p.3"},
        {"code": "body_fat_pct", "value": "29.2", "ref": "10~20", "on": "2026-07-07", "flag": "high", "source": "s p.18"},
        {"code": "uric_acid", "value": "8.0", "ref": "3.0~8.2", "on": "2025-06-27", "flag": "attention", "source": "s p.2"},
        {"code": "uric_acid", "value": "7.9", "ref": "3.0~8.2", "on": "2026-07-07", "flag": "attention", "source": "s p.2"},
        {"code": "weight", "value": "59.3", "ref": "52.5~71.1", "on": "2025-06-27", "flag": "normal", "source": "s p.3"},
        {"code": "weight", "value": "61.6", "ref": "52.5~71.1", "on": "2026-07-07", "flag": "normal", "source": "s p.18"},
        {"code": "hbs_ab", "value": "음성", "on": "2026-07-07", "source": "s p.2"},
    ],
    "findings": [
        {"id": "carotid-2025", "area": "혈관", "summary": "우경동맥 두께 증가", "on": "2025-06-27", "track": "carotid-us",
         "follow_up_months": 30, "severity": "follow_up", "source": "s p.9", "recommendation": "2~3년 뒤 추적"},
        {"id": "lipids-2026", "area": "지질", "summary": "이상지질혈증 관리", "on": "2026-07-07", "track": "lipids", "severity": "follow_up",
         "source": "s p.4"},
        {"id": "egd-2025", "area": "위", "summary": "만성표재성위염", "on": "2025-06-27", "track": "egd", "severity": "note", "source": "s p.8"},
        {"id": "ecg-2026", "area": "심장", "summary": "심전도 정상", "on": "2026-07-07", "track": "ecg", "severity": "normal", "source": "s p.4"},
    ],
    "posture": [
        {"code": "forward_head_deg", "value": "30", "on": "2023-05-23", "device": "exbody 리포트 2023", "source": "r p.1"},
        {"code": "forward_head_deg", "value": "5", "on": "2024-06-30", "device": "exbody 화면", "source": "photo"},
        {"code": "forward_head_deg", "value": "7", "on": "2025-06-30", "device": "exbody 화면", "source": "photo"},
        {"code": "pelvic_tilt_deg", "value": "8", "on": "2025-06-30", "device": "exbody 화면", "source": "photo"},
        {"code": "overall_grade", "value": "6단계", "on": "2023-05-23", "device": "exbody 리포트 2023", "source": "r p.1"},
    ],
    "genetics": {"on": "2024-12-13", "types": ["혈중 지질 관리형"], "genetics": [
        {"trait": "중성지방 농도", "area": "건강관리", "grade": "높음", "risk_alleles": 18, "alleles_total": 20, "korean_mean": 14.7, "source": "g p.9"},
        {"trait": "요산치", "area": "건강관리", "grade": "주의", "source": "g p.11"},
        {"trait": "비타민 D 농도", "area": "영양소", "grade": "보통", "source": "g p.30"}]},
    "regimen": [{"name": "탈모약", "ingredient": "dutasteride", "frequency": "weekly", "since": "2018-06"}],
    "routine": [{"activity": "테니스", "per_week": 2, "minutes": "30~40"}, {"activity": "회사 헬스장(스트레칭·요가 위주)", "per_week": "1~2", "minutes": "30~40"},
                {"activity": "산책·조깅", "per_week": "1~2", "minutes": "30~40"}],
}
SUNDAY = date(2026, 11, 1)   # ISO week 44.


class LetterTests(unittest.TestCase):
    def setUp(self):
        self.ledger = health.parse(RAW)

    def test_rotation_brings_a_goal_topic_back_every_other_week(self):
        weeks = [health_letter.topic_for(CONFIG, SUNDAY.replace(day=d)) for d in (1, 8, 15, 22, 29)]
        goal_topics = {"vascular", "uric-weight", "balance"}
        kinds = [t in goal_topics for t in weeks]
        self.assertEqual(kinds, [kinds[0], not kinds[0], kinds[0], not kinds[0], kinds[0]])   # Alternating, deterministic.
        self.assertEqual(health_letter.topic_for(CONFIG, SUNDAY), health_letter.topic_for(CONFIG, SUNDAY))
        no_goals = {"health": {"topics": ["balance", "vascular"]}}
        self.assertIn(health_letter.topic_for(no_goals, SUNDAY), ("balance", "vascular"))
        self.assertEqual(health_letter.period(SUNDAY).week_key, "2026-W44")
        self.assertEqual(health_letter.period(SUNDAY).week_start, date(2026, 10, 26))

    def test_trend_and_level_words_come_from_code(self):
        status = {i["code"]: i for i in health_letter.assemble(self.ledger, CONFIG, day=SUNDAY, today=SUNDAY, topic="vascular")["items"]}
        self.assertEqual((status["ldl"]["trend"], status["ldl"]["level"], status["ldl"]["latest"]["text"]), ("worsening", "out", "152"))
        self.assertEqual((status["tg"]["trend"], status["tg"]["level"]), ("improving", "normal"))     # Down is better.
        self.assertEqual((status["hdl"]["trend"], status["hdl"]["level"]), ("single", "out"))
        weight = health_letter.item_status(self.ledger, "weight")
        self.assertEqual((weight["trend"], weight["level"]), ("flat", "normal"))                       # Inside before and after.
        uric = health_letter.item_status(self.ledger, "uric_acid")
        self.assertEqual((uric["trend"], uric["level"], uric["level_word"]), ("flat", "edge", "경계"))   # 0.1 on a 5.2 span.
        fat = health_letter.item_status(self.ledger, "body_fat_pct")
        self.assertEqual((fat["trend"], fat["delta"]), ("worsening", Decimal("1.7")))
        self.assertEqual(health_letter.plain("ggt"), "간 수치(감마GT, 술 영향)")

    def test_one_method_per_trend_and_the_past_judged_by_its_own_range(self):
        # Two devices for body fat: only the latest one's series makes the trend (review 2026-10-25).
        raw = {**RAW, "observations": RAW["observations"] + [
            {"code": "body_fat_pct", "value": "20", "ref": "10~20", "on": "2024-07-18", "flag": "normal", "method": "device-a", "source": "s"},
            {"code": "body_fat_pct", "value": "30", "ref": "10~20", "on": "2025-01-10", "flag": "high", "method": "device-b", "source": "s"}]}
        fat = health_letter.item_status(health.parse(raw), "body_fat_pct")
        self.assertEqual((fat["latest"]["text"], fat["count"]), ("29.2", 2))          # The latest method (None) only.
        single = {"observations": [
            {"code": "body_fat_pct", "value": "20", "ref": "10~20", "on": "2024-07-18", "flag": "normal", "method": "device-a", "source": "s"},
            {"code": "body_fat_pct", "value": "30", "ref": "10~20", "on": "2025-01-10", "flag": "high", "method": "device-b", "source": "s"}]}
        self.assertEqual(health_letter.item_status(health.parse(single), "body_fat_pct")["trend"], "single")
        # Weight inside its own range at both check-ups, though the ranges moved: flat, not improving.
        moved = {"observations": [
            {"code": "weight", "value": "55", "ref": "50~70", "on": "2024-07-18", "flag": "normal", "source": "s"},
            {"code": "weight", "value": "65", "ref": "60~80", "on": "2026-07-07", "flag": "normal", "source": "s"}]}
        self.assertEqual(health_letter.item_status(health.parse(moved), "weight")["trend"], "flat")

    def test_dashboard_findings_and_estimate(self):
        data = health_letter.assemble(self.ledger, CONFIG, day=SUNDAY, today=SUNDAY, topic="vascular")
        goals = {g["key"]: g for g in data["goals"]}
        self.assertEqual(goals["ldl"]["status"]["latest"]["text"], "152")                  # Direct method only.
        self.assertEqual(goals["ldl"]["status"]["previous"]["text"], "118")
        self.assertEqual([(p["code"], p["trend"]) for p in goals["posture"]["posture"]],
                         [("forward_head_deg", "worsening"), ("pelvic_tilt_deg", "single")])   # Latest device only, 5 -> 7.
        self.assertEqual(goals["body_fat"]["status"]["level"], "out")
        self.assertTrue(goals["body_fat"]["do"] and goals["body_fat"]["avoid"])
        self.assertEqual(set(data["series"]), {"ldl", "hdl", "tg"})
        self.assertEqual(data["topic"]["charts"], ["ldl", "hdl", "tg", "tc"])
        self.assertEqual([f["id"] for f in data["findings"]], ["lipids-2026", "ecg-2026", "carotid-2025"])   # Topic tracks, newest first.
        self.assertEqual([f["label"] for f in data["follow_ups"]], ["경동맥 초음파"])
        self.assertEqual([t["trait"] for t in data["genetics"]], ["중성지방 농도"])
        self.assertEqual(data["regimen_notes"], [])                             # PSA is not a vascular code.
        act = data["activity"]
        self.assertEqual((act["source"], act["aerobic_minutes"], act["strength_days"]), ("estimate", [105, 200], 0))
        self.assertEqual(data["uncertain"], ["좋은 콜레스테롤(HDL)"])
        balance = health_letter.assemble(self.ledger, CONFIG, day=SUNDAY, today=SUNDAY, topic="balance")
        self.assertEqual(len(balance["posture"]), 5)

    def test_payload_carries_status_words_but_no_sources_or_identifiers(self):
        data = health_letter.assemble(self.ledger, CONFIG, day=SUNDAY, today=SUNDAY, topic="uric-weight",
                                      previous={"week_key": "2026-W43", "actions": [{"what": "물 2L", "amount": "매일"}]})
        payload = health_letter.payload(data)
        text = str(payload)
        for banned in ("s p.1", "s p.2", "photo", "'id'", "resolved_note", "exbody 리포트"):   # Page refs, ids, other devices.
            self.assertNotIn(banned, text, banned)
        # Free text is masked and a resolved finding is not sent at all (review 2026-10-25).
        raw = {**RAW, "findings": RAW["findings"] + [
            {"id": "note-2026", "area": "위", "summary": "홍길동 환자 등록번호 123456 소견, 생년월일 1989.02.22", "on": "2026-07-07", "track": "egd",
             "severity": "note", "source": "s p.9"},
            {"id": "eye-2025", "area": "눈", "summary": "우측 안압 높음", "on": "2025-06-27", "track": "eye", "severity": "see_doctor", "source": "s",
             "resolved_on": "2026-10-25", "resolved_note": "재측정 정상"}],
               "regimen": [{"name": "홍길동님 약", "frequency": "daily"}]}
        masked = str(health_letter.payload(health_letter.assemble(health.parse(raw), CONFIG, day=SUNDAY, today=SUNDAY, topic="stomach-habits")))
        for banned in ("홍길동", "123456", "1989", "안압"):
            self.assertNotIn(banned, masked, banned)
        self.assertIn("[식별정보 제거]", masked)
        self.assertEqual(payload["previous_actions"], [{"what": "물 2L", "amount": "매일"}])
        uric = next(i for i in payload["items"] if i["code"] == "uric_acid")
        self.assertEqual((uric["latest"]["value"], uric["trend_word"], uric["level_word"], uric["measurements"]), ("7.9", "그대로", "경계", 2))
        self.assertEqual(payload["genetics_reference_only"][0]["trait"], "요산치")

    def test_charts_and_reference_ranges(self):
        self.assertEqual(health_chart.parse_ref("0~129"), (Decimal(0), Decimal(129)))
        self.assertEqual(health_chart.parse_ref("60~"), (Decimal(60), None))
        self.assertEqual(health_chart.parse_ref("10 미만"), (None, Decimal(10)))
        self.assertEqual(health_chart.parse_ref("정상"), (None, None))
        data = health_letter.assemble(self.ledger, CONFIG, day=SUNDAY, today=SUNDAY, topic="vascular")
        images = health_html.charts(data)
        self.assertEqual(set(images), {"chart-ldl", "chart-hdl", "chart-tg"})          # tc has no values: no chart.
        self.assertTrue(all(png.startswith(b"\x89PNG") for png in images.values()))
        one = health_chart.line_chart([health_chart.Point(date(2026, 7, 7), Decimal("58"), "low", "58")], title="HDL", unit="mg/dL", ref="60~")
        self.assertTrue(one.startswith(b"\x89PNG"))

    def test_render_stands_without_prose_and_takes_verified_prose(self):
        data = health_letter.assemble(self.ledger, CONFIG, day=SUNDAY, today=SUNDAY, topic="vascular")
        subject, html, text = health_html.render(data, inline=True)
        self.assertTrue(subject.startswith("[Smith][건강] 11/01 (일) · 혈관·콜레스테롤"))
        for word in ("목표 계기판", "하기", "피하기", "나쁜 콜레스테롤(LDL)", "152", "▲ 나빠지는 중", "범위 밖", "data:image/png;base64",
                     "지난주 몸 상태", "추정", "이번 주 할 일", "공식 지침의 일반 권고", "경동맥 초음파", "생활 코치", "읽기가 불확실한 값"):
            self.assertIn(word, html, word)
        self.assertNotIn("<svg", html)
        self.assertIn("cid:chart-ldl", health_html.render(data)[1])
        self.assertIn("1. 붉은 고기", text)
        data["prose"] = {"headline": "LDL이 다시 오르고 있습니다, 식단부터", "verdict": "나쁜 콜레스테롤이 범위 밖으로 올라왔습니다. 걱정할 수준은 아니지만 지금 잡을 구간입니다.",
                         "items": {"ldl": "범위 밖으로 올라와 식단을 손볼 때입니다."}, "goal_lines": {"ldl": {"do": "귀리 아침", "avoid": "튀김"}},
                         "actions": [{"category": "food", "what": "귀리 아침", "amount": "주 5일", "why": "식이섬유", "check": "다음 검진 LDL"}]}
        subject, html, text = health_html.render(data, inline=True)
        self.assertIn("LDL이 다시 오르고 있습니다, 식단부터", subject)
        for word in ("지금 잡을 구간", "식단을 손볼 때", "귀리 아침", "튀김"):
            self.assertIn(word, html, word)
        self.assertNotIn("붉은 고기", html)
        self.assertNotIn("공식 지침의 일반 권고", html)
        self.assertIn("1. 귀리 아침", text)


if __name__ == "__main__":
    unittest.main()
