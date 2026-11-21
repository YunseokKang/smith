"""Places a restaurant question or a calendar entry names (FR-55): only public place names count, and only they would
leave for Naver; a question without one gets a guess back, never a search somewhere else (client 2026-11-16).
Every place here is a synthetic example."""
import unittest

from smith import contacts, dining, dining_view, places


class FindTest(unittest.TestCase):
    def test_public_place_names_only(self) -> None:
        for text, area in (
                # BUG-2026-007's shape: a station no hand list held, joined to 근처 inside brackets.
                ("내일 회사(연신내역)근처 외식하려는데 어디가 좋을까", "연신내역"), ("연신내역 맛집 알려줘", "연신내역"),
                # A bare stem counts with a place word or a word of going right after it; joined particles come off,
                # up to three ("에서는").
                ("연신내 맛집", "연신내"), ("연신내에서 저녁", "연신내"), ("연신내에서는 고기집 없어?", "연신내"),
                ("구파발역에서는 어때", "구파발역"), ("불광 쪽 브런치", "불광"), ("녹번 가서 고기", "녹번"),
                ("구파발역근처에서 만나", "구파발역"), ("중동 근처 맛집", "중동"),
                # Full names anywhere; the most specific place wins, a full name over any bare one, then the earliest.
                ("갈현동 파스타", "갈현동"), ("은평구 맛집", "은평구"), ("서울 은평구 불광동 123", "불광동"),
                ("부산 수영구 남천동 맛집", "남천동"), ("수원 행궁동 맛집", "행궁동"), ("부천 송내역 맛집", "송내역"),
                ("성수동 브런치", "성수동"), ("성수에서 출발해서 구파발역에서 저녁", "구파발역"), ("블루스퀘어 보고 한남동", "블루스퀘어"),
                ("강남역 미팅", "강남역"), ("DDP에서 전시", "DDP"), ("광화문 보라쇼", "광화문"),
                # A person's name is no place (Codex review 2026-11-16, P0): not across a sentence, not with a food or
                # a word of coming, not with a particle that follows people.
                ("친구 이름은 서현. 파스타 좋아한다는데 연신내역 맛집 추천해 줘", "연신내역"), ("서현 파스타 좋아한대", None),
                ("서현 와서 같이 먹을 곳", None), ("서현으로부터 연락 와서 저녁", None), ("서현에 대해 얘기하며 저녁", None),
                ("서현이랑 저녁", None), ("미사 참석", None),
                # Everyday words and plans.
                ("이동 중에 먹을 곳", None), ("운동 끝나고 먹을 곳", None), ("예산 2만원으로 저녁", None), ("고양이 카페", None),
                ("결제내역 확인하고 저녁", None), ("신사업 회의", None), ("가나다 부부상담 라마센터", None),
                ("오늘 저녁 뭐 먹지?", None)):
            with self.subTest(text=text):
                self.assertEqual(places.find(text), area)

    def test_the_address_book_is_never_a_place(self) -> None:
        """Codex review 2026-11-16, rounds 1-5, P0: a given name (after a one- or two-letter surname, spaced or not,
        with a title or a note beside it) or a whole name that is also a place never leaves, not even through the hand
        list (성수동 -> 성수); a different, full place name that only starts with it (서현역) is still the station."""
        def book(*names: str) -> frozenset[str]:
            return frozenset(w for n in names for w in contacts.blank_words(contacts.Contact("c", n, (), None, None, (), "", "")))
        for name in ("김서현", "남궁서현", "김 서현", "남궁 서현", "김서현(동창)", "김서현 차장"):
            with self.subTest(name=name):
                self.assertIsNone(places.find("서현 근처 맛집", people=book(name)))
                self.assertEqual(places.find("서현역 근처 맛집", people=book(name)), "서현역")
                self.assertEqual(places.suggest("서현 다녀와서", people=book(name)), [])
        self.assertIsNone(places.find("조원동이랑 저녁 뭐 먹을까?", people=book("조원동")))
        self.assertIsNone(places.find("성수동은 파스타 좋아하는데 저녁 뭐 먹을까?", people=book("성수동")))
        self.assertEqual((places.find("조원동 맛집"), places.suggest("서현 다녀와서")), ("조원동", ["서현역"]))

    def test_a_place_mentioned_but_not_read(self) -> None:
        """A follow-up that names a place Smith cannot read asked about a place of its own, whatever particle follows it
        (Codex review, rounds 4-5)."""
        for text, named in (("이번엔 김천구미역 근처 맛집은?", True), ("이번엔 김천구미역은 어때?", True), ("구파밸역 근처는?", True),
                            ("이번엔 구파밸역은 어때?", True), ("연신내역은?", True), ("거기 고기집은 없어?", False),
                            ("아내도 좋아할까?", False)):
            with self.subTest(text=text):
                self.assertEqual(places.mentions_place(text), named)
        self.assertEqual(places.suggest("이번엔 구파밸역은 어때?"), ["구파발역"])

    def test_guesses_for_a_question_without_a_place(self) -> None:
        for text, hints in (("구파발 다녀와서 뭐 먹지", ["구파발역"]), ("구파밸역 근처 저녁", ["구파발역"]), ("서현이랑 저녁", []),
                            ("이동 중에 먹을 곳", []), ("오늘 저녁 뭐 먹지?", [])):
            with self.subTest(text=text):
                self.assertEqual(places.suggest(text), hints)
        self.assertEqual(dining_view.no_place_text(["구파발역"]),
                         "말씀하신 동네를 찾지 못해 맛집을 검색하지 못했습니다. 혹시 찾으시는 동네가 구파발역인가요? "
                         "동네나 역 이름을 넣어 다시 물어봐 주세요(예: 구파발역 맛집).")
        self.assertIn("성수동 파스타", dining_view.no_place_text([]))

    def test_the_query_holds_the_place_and_food_words_only(self) -> None:
        for text, query in (("가나다랑 성수에서 파스타 먹을 곳?", "성수 파스타"), ("오리역 맛집", "오리역 맛집"),
                            ("오리역 근처 오리고기", "오리역 고기 오리")):
            with self.subTest(text=text):
                self.assertEqual(dining.query_words(text, places.find(text)), query)


if __name__ == "__main__":
    unittest.main()
