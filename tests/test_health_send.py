"""Sending the weekly health letter (health design §9): due once a week on the configured day, late in the same week,
sent once per ISO week, the snapshot reviewed by the next letter, uncertain outcomes never resent."""
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from smith import gmail, health, health_send, ledger
try:
    import fast_ledger
    from test_health_letter import RAW
except ImportError:   # Run as `python -m unittest tests.test_health_send`.
    from tests import fast_ledger
    from tests.test_health_letter import RAW

SEOUL = ZoneInfo("Asia/Seoul")
CONFIG = {"app": {"timezone": "Asia/Seoul"}, "mail": {"recipient": "client@example.com"},
          "health": {"enabled": True, "weekday": "sunday", "time": "07:00", "goals": ["posture", "body_fat", "ldl"]}}
CREDS = ("cid", "secret", "refresh")
SUNDAY = date(2026, 11, 1)


def setUpModule():
    fast_ledger.install()   # File ledgers without an fsync per commit (test budget); results are unchanged.


def tearDownModule():
    fast_ledger.uninstall()


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=SEOUL).astimezone(timezone.utc)


class SendTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Path(folder.name) / "smith.db"
        health.import_batch(health.path(self.db), RAW)
        self.sent = []

    def sender(self, credentials, *, recipient, subject, html, text, **kwargs):
        self.sent.append((subject, html))
        return f"m{len(self.sent)}"

    def test_due_on_the_letter_day_then_late_in_the_week_and_once_per_week(self):
        with closing(ledger.connect(self.db)) as conn:
            self.assertIsNone(health_send.due(conn, CONFIG, at(date(2026, 10, 31), 9, 0)))     # Saturday: not yet.
            self.assertIsNone(health_send.due(conn, CONFIG, at(SUNDAY, 6, 50)))                # Before the slot.
            self.assertEqual(health_send.due(conn, CONFIG, at(SUNDAY, 7, 5)), SUNDAY)
            self.assertEqual(health_send.due(conn, CONFIG, at(date(2026, 11, 3), 12, 0)), SUNDAY)   # Missed: still this week.
        # An interrupted run of either product is closed by this letter's own run-due (review 2026-10-25).
        with closing(ledger.connect(self.db)) as conn:
            ledger.claim_daily_run(conn, run_key="2026-10-20", day=date(2026, 10, 20), run_id="r1", trigger="scheduled",
                                   now=at(date(2026, 10, 20), 6, 0), lease=health_send.LEASE, max_attempts=3)
        lines = health_send.run_due(self.db, CONFIG, now=at(date(2026, 11, 3), 12, 0), recipient="client@example.com",
                                    credentials=CREDS, sender=self.sender)
        self.assertEqual(lines, ["2026-10-20 interrupted while building: closed", "health health:2026-W44: sent"])
        subject, html = self.sent[0]
        self.assertTrue(subject.startswith("[Smith][건강 · 지연 발송] 11/01 (일)"), subject)
        self.assertIn("목표 계기판", html)
        with closing(ledger.connect(self.db)) as conn:
            self.assertIsNone(health_send.due(conn, CONFIG, at(date(2026, 11, 4), 12, 0)))
            run = ledger.daily_run(conn, "health:2026-W44")
            self.assertEqual((run["status"], run["product"], run["day"], run["message_id"]), ("sent", "health", SUNDAY, "m1"))
            self.assertEqual(ledger.daily_runs_for_day(conn, SUNDAY), [])                        # Not a daily-mail run.
            self.assertEqual([r["run_key"] for r in ledger.sent_daily_runs(conn, since=at(SUNDAY, 0), product="health")],
                             ["health:2026-W44"])
            stored = ledger.daily_snapshot(conn, "health:2026-W44")
            self.assertEqual((stored["product"], stored["week_key"], stored["actions_from"], len(stored["actions"])),
                             ("health", "2026-W44", "guideline", 2))
            self.assertNotIn("source", str(stored["payload"]["series"]))
        again = health_send.publish(self.db, CONFIG, now=at(date(2026, 11, 4), 12, 0), day=SUNDAY, trigger="scheduled",
                                    recipient="client@example.com", credentials=CREDS, sender=self.sender)
        self.assertEqual((again["status"], again["error_code"]), ("skipped", "already-sent"))
        # Next week's letter reviews this week's actions.
        following = date(2026, 11, 8)
        self.assertEqual(health_send.previous_snapshot(self.db, following)["week_key"], "2026-W44")
        self.assertIsNone(health_send.previous_snapshot(self.db, SUNDAY))
        health_send.publish(self.db, CONFIG, now=at(following, 7, 5), day=following, trigger="scheduled",
                            recipient="client@example.com", credentials=CREDS, sender=self.sender)
        self.assertIn("지난주 행동 점검", self.sent[1][1])
        self.assertTrue(self.sent[1][0].startswith("[Smith][건강] 11/08 (일)"))

    def test_uncertain_delivery_is_never_resent_and_a_clean_failure_retries(self):
        def flaky(credentials, **kwargs):
            raise gmail.MailError("timeout", uncertain=True)

        result = health_send.publish(self.db, CONFIG, now=at(SUNDAY, 7, 5), day=SUNDAY, trigger="scheduled",
                                     recipient="client@example.com", credentials=CREDS, sender=flaky)
        self.assertEqual(result["status"], "unknown")
        with closing(ledger.connect(self.db)) as conn:
            self.assertIsNone(health_send.due(conn, CONFIG, at(SUNDAY, 8, 0)))
        monday = date(2026, 11, 9)
        failing = lambda credentials, **kwargs: (_ for _ in ()).throw(gmail.MailError("http-500", uncertain=False))  # noqa: E731
        first = health_send.publish(self.db, CONFIG, now=at(monday, 8, 0), day=date(2026, 11, 8), trigger="scheduled",
                                    recipient="client@example.com", credentials=CREDS, sender=failing)
        self.assertEqual(first["status"], "failed")
        with closing(ledger.connect(self.db)) as conn:
            self.assertEqual(health_send.due(conn, CONFIG, at(monday, 8, 30)), date(2026, 11, 8))   # Retryable.
        no_setup = health_send.publish(self.db, CONFIG, now=at(monday, 9, 0), day=date(2026, 11, 8), trigger="manual",
                                       recipient=None, credentials=None, sender=self.sender)
        self.assertEqual((no_setup["status"], no_setup["error_code"]), ("failed", "no-mail-setup"))
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
