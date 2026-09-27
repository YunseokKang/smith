import tempfile
import unittest
from pathlib import Path
from smith.config import load_config

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "smith.example.toml"


class ConfigTests(unittest.TestCase):
    def test_schedule_and_disabled_delivery(self):
        config = load_config(EXAMPLE)
        self.assertEqual(config["app"]["timezone"], "Asia/Seoul")
        self.assertEqual(config["reports"]["weekdays"], ["monday", "thursday"])
        self.assertEqual(config["reports"]["time"], "06:00")
        self.assertFalse(config["reports"]["enabled"])

    def test_invalid_or_unsafe_settings_are_rejected(self):
        mutations = [
            ('financial_access = "read_only"', 'financial_access = "trade"'),
            ('remove_personal_identifiers = true', 'remove_personal_identifiers = false'),
            ('require_macro_context = true', 'require_macro_context = false'),
            ('include_alternatives = true', 'include_alternatives = false'),
            ('time = "06:00"', 'time = "25:00"'),
            ('timezone = "Asia/Seoul"', 'timezone = "Invalid/Zone"'),
            ('["monday", "thursday"]', '["monday", "monday"]'),
            ('["monday", "thursday"]', '[1]'),
            ('enabled = false', 'enabled = "false"'),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.toml"
            for before, after in mutations:
                with self.subTest(after=after):
                    path.write_text(EXAMPLE.read_text(encoding="utf-8").replace(before, after), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_config(path)

    def test_schedule_can_change_without_code(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "custom.toml"
            text = EXAMPLE.read_text(encoding="utf-8")
            path.write_text(text.replace('"monday", "thursday"', '"tuesday"').replace('06:00', '08:30'), encoding="utf-8")
            config = load_config(path)
            self.assertEqual(config["reports"]["weekdays"], ["tuesday"])
            self.assertEqual(config["reports"]["time"], "08:30")


if __name__ == "__main__":
    unittest.main()
