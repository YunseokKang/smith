import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from smith import daily, daily_writer, history, keep, people

WIKITEXT = """{{달력}}
== 사건 ==
* [[1597년]] - [[명량 해전]] 발발.<ref>참고</ref>
* [[1917년]] - 러시아에서 [[10월 혁명]]이 발생하다. ([[율리우스력]] 기준)
* [[2022년]]
** [[한승수]] 전 국무총리가 유엔총회의장협의회 의장으로 선출되었다.
** [[대구]] 매천시장에서 화재가 발생했다.

== 문화 ==
* [[1990년]] - 아무 일.
"""


def note(name, text, *, edited="2026-09-01T00:00:00+00:00", labels=(), trashed=False, archived=False, pinned=False, items=None):
    raw = {"title": "", "textContent": text, "labels": [{"name": l} for l in labels], "isTrashed": trashed,
           "isArchived": archived, "isPinned": pinned,
           "userEditedTimestampUsec": int(datetime.fromisoformat(edited).timestamp() * 1_000_000)}
    if items is not None:
        raw.pop("textContent")
        raw["listContent"] = [{"text": i, "isChecked": False} for i in items]
    return name, raw


class KeepTests(unittest.TestCase):
    def test_import_selection_and_rotation(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "Takeout" / "Keep"
            source.mkdir(parents=True)
            for name, raw in (note("a.json", "느긋한 일요일 아침에는 산책이 생각을 정리해 준다. 걷다 보면 한 주의 방향이 보인다."),
                              note("b.json", "일은 속도보다 방향이다. 오늘 하나만 제대로 끝내자.", labels=("생각",), pinned=True),
                              note("c.json", "장보기", items=["우유", "달걀"]),
                              note("d.json", "버린 메모인데 길이는 충분히 길게 써 둔다 버린 메모", trashed=True),
                              note("e.json", "짧다")):
                (source / name).write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            store = Path(folder) / "data" / "keep"
            result = keep.import_takeout(Path(folder) / "Takeout", store)
            self.assertEqual((result.imported, result.trashed, result.empty), (4, 1, 0))
            notes = keep.load(store)
            self.assertEqual(sorted(n.id for n in notes), ["a", "b", "c", "e"])
            self.assertEqual(next(n for n in notes if n.id == "c").kind, "list")
            day = date(2026, 10, 25)
            chosen = keep.thoughts(notes, day=day, context="10월 25일 일요일 산책 맑음", used={})
            self.assertEqual([n.id for n in chosen], ["a#0", "b#0"])                  # Lists and short notes are not thoughts.
            keep.mark_used(store, "a#0", day)
            self.assertEqual([n.id for n in keep.thoughts(notes, day=date(2026, 10, 26), context="월요일", used=keep.used(store))], ["b#0"])
            self.assertEqual([n.id for n in keep.thoughts(notes, day=date(2027, 1, 10), context="", used=keep.used(store))], ["b#0", "a#0"])
            payload = keep.for_model(chosen)
            self.assertEqual(payload[0]["id"], "a#0")
            self.assertNotIn("isTrashed", json.dumps(payload))


class EntryTests(unittest.TestCase):
    def test_monthly_journal_notes_split_into_dated_entries(self):
        journal = keep.Note("2026-04", "2026-04", "0406 ㆍ스웨덴의 라곰은 적당함을 뜻한다. 개인의 균형이 중요하다.\n"
                            "ㆍ논리와 감정을 써야 할 순간을 구분하는 것이 지혜다. 사실의 엄밀함과 전달의 온기.\n"
                            "0412 ㆍ짧음\n4/20 ㆍ봄에는 걷는 시간을 늘리자. 걷다 보면 생각이 정리된다.", ("센터링",), None, False, False, "note")
        found = keep.entries([journal])
        self.assertEqual([(e.id, e.on, e.text[:12]) for e in found],
                         [("2026-04#0", date(2026, 4, 6), "스웨덴의 라곰은 적당함"), ("2026-04#1", date(2026, 4, 6), "논리와 감정을 써야 할"),
                          ("2026-04#3", date(2026, 4, 20), "봄에는 걷는 시간을 늘")])   # "짧음" is too short to be a thought.
        self.assertEqual([e.id for e in keep.search([journal], "걷기 생각 정리")], ["2026-04#3"])
        whole = keep.Note("x", "", "줄바꿈 없는 한 덩어리 메모도 그대로 하나의 생각이 된다.", (), None, False, False, "note")
        self.assertEqual([e.id for e in keep.entries([whole])], ["x#0"])


class HistoryTests(unittest.TestCase):
    def test_parse_and_pick(self):
        events = history.parse(WIKITEXT)
        self.assertEqual([(e.year, e.text) for e in events],
                         [(1597, "명량 해전 발발."), (1917, "러시아에서 10월 혁명이 발생하다. (율리우스력 기준)."),
                          (2022, "한승수 전 국무총리가 유엔총회의장협의회 의장으로 선출되었다."),
                          (2022, "대구 매천시장에서 화재가 발생했다.")])
        self.assertEqual(history.pick(events, date(2026, 10, 25)).year, 1597)       # A Korean event is preferred.
        self.assertEqual(history.page_url(date(2026, 10, 25)), "https://ko.wikipedia.org/wiki/10%EC%9B%94_25%EC%9D%BC")

    def test_cache_is_used_when_fresh(self):
        calls = []

        def getter(url, headers):
            calls.append(url)
            return 200, {"parse": {"wikitext": WIKITEXT}}
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "history.json"
            now = datetime(2026, 10, 25, tzinfo=timezone.utc)
            first = history.events_for(date(2026, 10, 25), cache=cache, now=now, getter=getter)
            second = history.events_for(date(2027, 10, 25), cache=cache, now=now, getter=getter)
            self.assertEqual((len(first), len(second), len(calls)), (4, 4, 1))


class WriterAsideTests(unittest.TestCase):
    def test_history_and_thought_are_checked_against_the_input(self):
        book = people.parse({})
        notes = [keep.Note("n1", "", "일은 속도보다 방향이다. 오늘 하나만 제대로 끝내자.", (), None, False, False, "note")]
        inputs = daily.Inputs(None, [], [], book, None, [], {}, history.parse(WIKITEXT), notes, {})
        data = daily.assemble(inputs, day=date(2026, 10, 25), tz="Asia/Seoul", now=datetime(2026, 10, 24, 21, tzinfo=timezone.utc),
                              airways_sensitive=False)
        self.assertEqual([t.id for t in data["thoughts"]], ["n1#0"])
        payload_text = str(daily.payload(data))
        output = {"headline": "", "weather_line": "", "agenda_line": "", "anniversaries": [], "news": [], "tasks_line": "",
                  "sentence": "속도보다 방향, 오늘은 하나만 제대로 끝내 보는 하루.", "thought_id": "n1#0",
                  "history": {"year": 1597, "line": "명량에서 열세 척이 물길을 읽어 이긴 날, 준비가 운을 만든다는 말을 떠올리게 해요."}}
        prose, problems = daily_writer.verify(output, data, payload_text, tastes_known={})
        self.assertEqual((prose["thought_id"], prose["history"]["year"]), ("n1#0", 1597))
        self.assertEqual(problems, [])
        output["history"] = {"year": 1066, "line": "헤이스팅스 전투."}
        output["thought_id"] = "nope"
        prose, problems = daily_writer.verify(output, data, payload_text, tastes_known={})
        self.assertNotIn("history", prose)
        self.assertEqual(prose["thought_id"], "")
        self.assertEqual(problems, ["history: year not in the list or a number not in input"])


if __name__ == "__main__":
    unittest.main()
