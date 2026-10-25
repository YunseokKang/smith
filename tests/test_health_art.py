"""Pictures beside the health letter's actions: the word rules, the category defaults, the bundled files, and the
letter carrying them (data URI in the preview, cid attachment in the mail)."""
import unittest

from smith import health, health_art, health_html, health_letter

try:
    from test_health_letter import CONFIG, RAW, SUNDAY
except ImportError:
    from tests.test_health_letter import CONFIG, RAW, SUNDAY


class ArtTests(unittest.TestCase):
    def test_words_pick_the_picture_and_categories_fall_back(self):
        cases = [
            ({"category": "exercise", "what": "회사 헬스장에서 스쿼트·런지 근력 운동", "amount": "주 2회"}, "weights"),
            ({"category": "exercise", "what": "탄천 산책을 조깅으로", "amount": "주 2회 40분"}, "running"),
            ({"category": "exercise", "what": "턱 당기기와 가슴 열기", "amount": "하루 2회"}, "lotus"),
            ({"category": "exercise", "what": "테니스 뒤 스트레칭", "amount": "10분"}, "tennis"),
            ({"category": "food", "what": "고기 대신 생선·두부", "amount": "주 2회"}, "fish"),
            ({"category": "food", "what": "끼니마다 달걀 하나", "amount": "매일"}, "egg"),
            ({"category": "food", "what": "단 음료를 물로", "amount": "하루 0잔"}, "water"),
            ({"category": "life", "what": "술자리는 주 1회까지", "amount": "소주 반 병"}, "beer"),
            ({"category": "life", "what": "한낮 햇빛 쬐기", "amount": "15분"}, "sun"),
            ({"category": "life", "what": "주 1회 아침 공복에 체중 재기", "amount": "같은 요일"}, "scale"),
            ({"category": "life", "what": "달력에 표시", "amount": "매일"}, "memo"),
            ({"category": "food", "what": "뭔가 먹기", "amount": "x"}, "plate"),
            ({"category": "life", "what": "뭔가 하기", "amount": "x"}, "sparkles"),
        ]
        for action, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(health_art.picture_for(action), expected)
        for key in {k for _, k in health_art.RULES} | set(health_art.DEFAULTS.values()):
            self.assertTrue((health_art.ASSETS / f"{key}.png").exists(), key)
        self.assertTrue((health_art.ASSETS / "LICENSE-fluentui-emoji.txt").exists())

    def test_letter_carries_a_picture_per_action(self):
        data = health_letter.assemble(health.parse(RAW), CONFIG, day=SUNDAY, today=SUNDAY, topic="vascular")
        images = health_art.images_for(data)
        self.assertEqual(list(images), ["action-1", "action-2"])              # Two guideline fallback actions.
        self.assertEqual([p.name for p in images.values()], ["fish.png", "running.png"])   # "걷기 또는 가벼운 달리기".
        subject, html, text = health_html.render(data, inline=True)
        self.assertEqual(html.count("data:image/png;base64"), 3 + 2)          # Three charts and two pictures.
        self.assertIn("cid:action-1", health_html.render(data)[1])
        attached = health_html.attachments(data)
        self.assertEqual(set(attached), {"chart-ldl", "chart-hdl", "chart-tg", "action-1", "action-2"})
        self.assertTrue(all(png.startswith(b"\x89PNG") for png in attached.values()))


if __name__ == "__main__":
    unittest.main()
