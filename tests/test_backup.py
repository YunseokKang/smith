import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from smith import backup_command, ledger, memory


class BackupTests(unittest.TestCase):
    def test_copies_are_verified_readable_and_pruned_to_the_newest(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        db, out = root / "smith.db", root / "backups"
        ledger.connect(db).close()
        memory.add(memory.directory(db), "plan", "은퇴 후 제주에 살고 싶다", on=date(2026, 10, 5), source="대화")
        (out / "keep-me.txt").parent.mkdir()
        (out / "keep-me.txt").write_text("not a backup", encoding="utf-8")   # Other files are never touched.
        start = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)
        results = [backup_command.backup(db, out, now=start + timedelta(minutes=i), keep=2) for i in range(3)]
        self.assertEqual([removed for _, _, removed in results], [0, 0, 1])
        self.assertEqual(results[-1][1], ledger.SCHEMA_VERSION)
        names = sorted(p.name for p in out.iterdir())
        self.assertEqual(names, ["keep-me.txt", "smith-20261005T060100Z.db", "smith-20261005T060100Z.memory",
                                 "smith-20261005T060200Z.db", "smith-20261005T060200Z.memory"])
        self.assertEqual([f.text for f in memory.load(out / "smith-20261005T060200Z.memory")], ["은퇴 후 제주에 살고 싶다"])
        with closing(ledger.connect_read_only(results[-1][0])) as copy:
            self.assertEqual(ledger.report_runs(copy), [])

    def test_a_missing_ledger_is_an_error_not_an_empty_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "out"
            with self.assertRaises(backup_command.BackupError):
                backup_command.backup(Path(directory) / "absent.db", out,
                                      now=datetime(2026, 10, 5, tzinfo=timezone.utc), keep=1)
            self.assertEqual(list(out.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
