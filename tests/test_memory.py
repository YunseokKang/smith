import tempfile
import unittest
from datetime import date
from pathlib import Path

from smith import memory

ON = date(2026, 10, 5)


class MemoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name) / "memory"

    def test_facts_are_cleaned_dated_and_kept_once(self):
        fact = memory.add(self.folder, "property", "솔방울은  푸른마을아파트 303동이고 연락처는 010-1234-5678", on=ON, source="메일")
        self.assertEqual(fact.text, "솔방울은 푸른마을아파트 (동·호 생략)이고 연락처는 [식별정보 제거]")
        self.assertEqual(memory.add(self.folder, "property", "솔방울은 푸른마을아파트 303동이고, 연락처는 010-1234-5678.",
                                    on=ON, source="대화"), None)                      # Same fact: not repeated.
        self.assertIsNone(memory.add(self.folder, "property", "  ", on=ON, source="대화"))
        self.assertEqual((self.folder / "property.md").read_text(encoding="utf-8"),
                         f"# 부동산·주거\n\n- 2026-10-05 · 메일 · {fact.text}\n")

    def test_hand_written_files_are_read_as_facts(self):
        self.folder.mkdir()
        (self.folder / "plan.md").write_text("# 계획\n\n- 2027-12 입주 예정인 청약에 당첨되면 이사를 검토한다\n"
                                             "  - 들여 쓴 줄은 메모라 읽지 않는다\n본문 문장도 읽지 않는다\n", encoding="utf-8")
        (self.folder / "notes.md").write_text("- 2026-10-01 · 대화 · 은퇴 후 제주에서 살고 싶다\n", encoding="utf-8")
        facts = {f.text: f for f in memory.load(self.folder)}
        self.assertEqual(sorted(facts), ["2027-12 입주 예정인 청약에 당첨되면 이사를 검토한다", "은퇴 후 제주에서 살고 싶다"])
        self.assertEqual((facts["은퇴 후 제주에서 살고 싶다"].topic, facts["은퇴 후 제주에서 살고 싶다"].recorded_on),
                         ("other", date(2026, 10, 1)))
        self.assertEqual(facts["2027-12 입주 예정인 청약에 당첨되면 이사를 검토한다"].source, "직접 작성")

    def test_retrieval_keeps_pinned_and_relevant_facts_within_the_budget(self):
        for topic, text in (("preference", "설명은 짧게, 결론부터 듣고 싶다"), ("property", "솔방울은 푸른마을아파트를 말한다"),
                            ("property", "은행나무는 배우자 명의 집이다"), ("plan", "내년 봄에 자동차를 바꿀 생각이다")):
            memory.add(self.folder, topic, text, on=ON, source="메일")
        facts = memory.load(self.folder)
        self.assertEqual(len(memory.retrieve(facts, "아무 질문")), 4)                   # Everything fits: all of it.
        budget = len("설명은 짧게, 결론부터 듣고 싶다") + len("솔방울은 푸른마을아파트를 말한다")
        chosen = [f.text for f in memory.retrieve(facts, "푸른마을을 팔까요?", budget=budget)]
        # The preference always goes; "푸른마을을" still finds "푸른마을아파트" despite the particle.
        self.assertEqual(chosen, ["설명은 짧게, 결론부터 듣고 싶다", "솔방울은 푸른마을아파트를 말한다"])


if __name__ == "__main__":
    unittest.main()
