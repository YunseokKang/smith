import argparse
import io
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest import mock

from smith import credentials, doctor_command, ledger

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "smith.example.toml"


class DoctorTests(unittest.TestCase):
    def run_doctor(self, db, config):
        output = io.StringIO()
        with mock.patch.object(doctor_command, "_scheduler", lambda results, root: None), \
                mock.patch.object(credentials, "load_gmail", return_value=("id", "secret", "token")), \
                mock.patch.object(credentials, "load_toss_client", return_value=None), \
                mock.patch.object(credentials, "load_api_key", return_value="k"), redirect_stdout(output):
            code = doctor_command.run(argparse.Namespace(db=db, config=config))
        return code, output.getvalue()

    def test_reports_problems_without_values_and_never_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "smith.db"
            code, text = self.run_doctor(db, EXAMPLE)
            self.assertEqual(code, 1)                                    # No ledger, no recipient.
            self.assertIn("FAIL  ledger", text)
            self.assertIn("FAIL  mail", text)
            self.assertNotIn("secret", text)                             # Credentials: presence only.
            self.assertFalse(db.exists())                                # Read-only: nothing created.
            with closing(ledger.connect(db)):
                pass
            code, text = self.run_doctor(db, EXAMPLE)
            self.assertIn(f"OK    ledger       schema v{ledger.SCHEMA_VERSION}, integrity ok", text)
            self.assertIn("missing: toss", text)


if __name__ == "__main__":
    unittest.main()
