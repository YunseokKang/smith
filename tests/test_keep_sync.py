import io
import json
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from smith import drive, keep

NOW = datetime(2026, 10, 26, 20, 0, tzinfo=timezone.utc)


def takeout_zip(notes: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in notes.items():
            archive.writestr(f"Takeout/Keep/{name}.json", json.dumps({"title": "", "textContent": text, "isTrashed": False,
                                                                     "userEditedTimestampUsec": 1_700_000_000_000_000}))
        archive.writestr("Takeout/archive_browser.html", "<html></html>")
    return buffer.getvalue()


class DriveTests(unittest.TestCase):
    def test_archives_are_parsed_and_grouped_by_export(self):
        def getter(url, headers):
            self.assertIn("Bearer tok", headers["Authorization"])
            self.assertIn("name+contains+%27takeout-%27", url)
            return 200, {"files": [{"id": "fileB2xxxxxxxxxx", "name": "takeout-20261101T060000Z-1-002.zip", "size": "10"},
                                   {"id": "fileB1xxxxxxxxxx", "name": "takeout-20261101T060000Z-1-001.zip", "size": "20"},
                                   {"id": "fileB0xxxxxxxxxx", "name": "takeout-20261101T060000Z-001.zip", "size": "2"},
                                   {"id": "fileA1xxxxxxxxxx", "name": "takeout-20260901T060000Z-3-001.zip", "size": "5"},
                                   {"id": "otherxxxxxxxxxxx", "name": "photo.zip"}]}
        archives = drive.takeout_archives("tok", getter=getter)
        self.assertEqual([a.name for a in archives], ["takeout-20261101T060000Z-001.zip", "takeout-20261101T060000Z-1-001.zip",
                                                      "takeout-20261101T060000Z-1-002.zip", "takeout-20260901T060000Z-3-001.zip"])
        newest = drive.newest_export(archives)
        self.assertEqual([(a.stamp, a.batch, a.part) for a in newest],
                         [("20261101T060000Z", 0, 1), ("20261101T060000Z", 1, 1), ("20261101T060000Z", 1, 2)])
        for status, body, code in ((401, {}, "login-required"), (403, {"error": {"errors": [{"reason": "accessNotConfigured"}]}}, "api-not-enabled"),
                                   (403, {}, "no-scope"), (500, {}, "http-500")):
            with self.subTest(code=code), self.assertRaises(drive.DriveError) as caught:
                drive.takeout_archives("tok", getter=lambda url, headers, s=status, b=body: (s, b))
            self.assertEqual(caught.exception.code, code)


class SyncTests(unittest.TestCase):
    def test_sync_imports_the_newest_export_once_and_searches_it(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Path(folder) / "keep"
            listing = [drive.Archive("idB0xxxxxxxxxxx", "takeout-20261101T060000Z-001.zip", "20261101T060000Z", 1, 2, "", 0),
                       drive.Archive("idB1xxxxxxxxxxx", "takeout-20261101T060000Z-1-001.zip", "20261101T060000Z", 1, 10, "", 1),
                       drive.Archive("idA1xxxxxxxxxxx", "takeout-20260901T060000Z-1-001.zip", "20260901T060000Z", 1, 5, "", 1)]
            downloads = []

            def downloader(token, file_id, target):
                downloads.append(file_id)
                target.parent.mkdir(parents=True, exist_ok=True)
                if file_id == "idB0xxxxxxxxxxx":   # The index archive holds only the browser page.
                    target.write_bytes(takeout_zip({}))
                else:
                    target.write_bytes(takeout_zip({"n1": "와이프는 향수보다 꽃을 좋아한다. 생일엔 작은 꽃다발이 늘 맞았다.",
                                                    "n2": "일은 속도보다 방향이다. 오늘 하나만 제대로 끝내자."}))
                return 10
            first = keep.sync(store, "tok", now=NOW, lister=lambda token: listing, downloader=downloader)
            self.assertEqual((first["outcome"], first["stamp"], first["imported"]), ("imported", "20261101T060000Z", 2))
            self.assertEqual(downloads, ["idB0xxxxxxxxxxx", "idB1xxxxxxxxxxx"])   # Only the newest export's archives.
            self.assertFalse((store / "incoming" / "takeout-20261101T060000Z-1-001.zip").exists())   # Cleaned up.
            second = keep.sync(store, "tok", now=NOW + timedelta(hours=1), lister=lambda token: listing, downloader=downloader)
            self.assertEqual((second["outcome"], len(downloads)), ("unchanged", 2))
            self.assertFalse(keep.sync_due(store, NOW + timedelta(hours=2)))
            self.assertTrue(keep.sync_due(store, NOW + timedelta(hours=30)))
            notes = keep.load(store)
            found = keep.search(notes, "와이프 선물")
            self.assertEqual([n.note_id for n in found], ["n1"])
            self.assertEqual(keep.search(notes, "방향")[0].note_id, "n2")          # A two-character Korean word matches.
            self.assertEqual(keep.search(notes, "zzz"), [])
            # A newer export of other products (no Keep in it) leaves the notes alone and is not tried again.
            photos_only = [drive.Archive("idCxxxxxxxxxxxx", "takeout-20261201T060000Z-001.zip", "20261201T060000Z", 1, 2, "", 0)]

            def index_only(token, file_id, target):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(takeout_zip({}))
                return 2
            skipped = keep.sync(store, "tok", now=NOW + timedelta(days=40), lister=lambda token: photos_only, downloader=index_only)
            self.assertEqual(skipped["outcome"], "no-keep")
            self.assertEqual(len(keep.load(store)), 2)
            self.assertEqual(keep.sync(store, "tok", now=NOW + timedelta(days=41), lister=lambda token: photos_only,
                                       downloader=index_only)["outcome"], "unchanged")
            err = keep.sync(store, "tok", now=NOW, lister=lambda token: (_ for _ in ()).throw(drive.DriveError("no-scope")))
            self.assertEqual((err["outcome"], err["error_code"]), ("error", "no-scope"))
            empty = keep.sync(Path(folder) / "other", "tok", now=NOW, lister=lambda token: [])
            self.assertEqual(empty["outcome"], "none")


if __name__ == "__main__":
    unittest.main()
