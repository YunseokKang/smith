import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from smith.cli import main

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "portfolio.example.json"


class CliTests(unittest.TestCase):
    def test_import_dry_run_apply_list_and_reject(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.db"
            db = Path(directory) / "data" / "smith.db"
            db.parent.mkdir()
            db.touch()  # An existing empty file must survive read-only commands untouched.
            bad = Path(directory) / "bad.json"
            bad.write_text('{"schema_version": 1}', encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(main(["import", str(EXAMPLE), "--db", str(missing), "--dry-run"]), 0)
                self.assertEqual(main(["import", str(EXAMPLE), "--db", str(db), "--dry-run"]), 0)
                self.assertEqual(main(["records", "--db", str(db)]), 1)
                self.assertEqual(main(["evidence", "show", "--db", str(db)]), 1)
                self.assertEqual((missing.exists(), db.stat().st_size), (False, 0))
                self.assertEqual(main(["import", str(EXAMPLE), "--db", str(db)]), 0)
                self.assertEqual(main(["import", str(EXAMPLE), "--db", str(db)]), 0)
                self.assertEqual(main(["records", "--db", str(db), "--known-at", "2026-01-01T00:00:00+00:00"]), 0)
                self.assertEqual(main(["records", "--db", str(db)]), 0)
                self.assertEqual(main(["import", str(bad), "--db", str(db)]), 2)
        text = out.getvalue()
        self.assertIn("created=4", text)
        self.assertIn("already applied", text)
        self.assertIn("(0)\n", text)  # Nothing was known before the import was recorded.
        self.assertIn("asset-example-cash", text)
        self.assertIn("Import rejected", text)


if __name__ == "__main__":
    unittest.main()
