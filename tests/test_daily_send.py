import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from smith import daily, daily_send, gmail, ledger, people

SEOUL = ZoneInfo("Asia/Seoul")
CONFIG = {"app": {"timezone": "Asia/Seoul"}, "daily": {"enabled": True, "time": "06:00", "airways_sensitive": True},
          "mail": {"recipient": "client@example.com"}}
CREDS = ("cid", "secret", "refresh")
BOOK = people.parse({"people": [{"id": "nephew", "label": "조카", "relation": "nephew",
                                 "birth": {"calendar": "solar", "year": 2025, "month": 11, "day": 27}}]})


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=SEOUL).astimezone(timezone.utc)


def collector(db, config, *, now, day):
    return daily.Inputs(None, [], [], BOOK, None, [], {})


class SendTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Path(folder.name) / "smith.db"
        self.sent = []

    def sender(self, credentials, *, recipient, subject, html, text, **kwargs):
        self.sent.append(subject)
        return f"m{len(self.sent)}"

    def test_due_once_a_day_and_sent_once(self):
        day = date(2026, 11, 20)
        with closing(ledger.connect(self.db)) as conn:
            self.assertIsNone(daily_send.due(conn, CONFIG, at(day, 5, 50)))            # Before the slot.
            self.assertEqual(daily_send.due(conn, CONFIG, at(day, 6, 5)), day)
        lines = daily_send.run_due(self.db, CONFIG, now=at(day, 6, 5), recipient="client@example.com", credentials=CREDS,
                                   sender=self.sender, collector=collector)
        self.assertEqual(lines, ["daily 2026-11-20: sent"])
        self.assertTrue(self.sent[0].startswith("[Smith][하루 일과] 11/20 (금)"))
        self.assertIn("조카 생일 D-7", self.sent[0])
        with closing(ledger.connect(self.db)) as conn:
            self.assertIsNone(daily_send.due(conn, CONFIG, at(day, 6, 20)))            # Sent: not due again.
            run = ledger.daily_run(conn, "2026-11-20")
            self.assertEqual((run["status"], run["message_id"], run["attempts"]), ("sent", "m1", 1))
            self.assertEqual(ledger.shown_reminder_keys(conn, ["birthday:nephew:2026:D-7"]), {"birthday:nephew:2026:D-7"})
        again = daily_send.publish(self.db, CONFIG, now=at(day, 6, 30), day=day, trigger="scheduled",
                                   recipient="client@example.com", credentials=CREDS, sender=self.sender, collector=collector)
        self.assertEqual((again["status"], again["error_code"]), ("skipped", "already-sent"))
        self.assertEqual(len(self.sent), 1)

    def test_late_marker_and_manual_runs(self):
        day = date(2026, 11, 20)
        result = daily_send.publish(self.db, CONFIG, now=at(day, 9, 0), day=day, trigger="scheduled",
                                    recipient="client@example.com", credentials=CREDS, sender=self.sender, collector=collector)
        self.assertEqual(result["status"], "sent")
        self.assertTrue(self.sent[0].startswith("[Smith][하루 일과 · 지연 발송] 11/20"))
        manual = daily_send.publish(self.db, CONFIG, now=at(day, 9, 30), day=day, trigger="manual",
                                    recipient="client@example.com", credentials=CREDS, sender=self.sender, collector=collector)
        self.assertEqual(manual["status"], "sent")                                        # A manual send has its own key.
        self.assertTrue(self.sent[1].startswith("[Smith][하루 일과] 11/20"))                 # Never marked late.
        other = date(2026, 11, 21)
        daily_send.publish(self.db, CONFIG, now=at(other, 7, 0), day=other, trigger="manual",
                           recipient="client@example.com", credentials=CREDS, sender=self.sender, collector=collector)
        with closing(ledger.connect(self.db)) as conn:
            self.assertIsNone(daily_send.due(conn, CONFIG, at(other, 7, 30)))             # A manual mail satisfies the day.

    def test_uncertain_outcome_is_never_resent_and_clean_failures_are_bounded(self):
        day = date(2026, 11, 20)

        def uncertain(credentials, **kwargs):
            raise gmail.MailError("unknown-outcome", uncertain=True)
        result = daily_send.publish(self.db, CONFIG, now=at(day, 6, 1), day=day, trigger="scheduled",
                                    recipient="client@example.com", credentials=CREDS, sender=uncertain, collector=collector)
        self.assertEqual(result["status"], "unknown")
        with closing(ledger.connect(self.db)) as conn:
            self.assertIsNone(daily_send.due(conn, CONFIG, at(day, 6, 30)))
            self.assertEqual(ledger.shown_reminder_keys(conn, ["birthday:nephew:2026:D-7"]), {"birthday:nephew:2026:D-7"})

        def failing(credentials, **kwargs):
            raise gmail.MailError("http-400")
        later = date(2026, 11, 21)
        outcomes = []
        for minute in (1, 20, 40, 55):
            with closing(ledger.connect(self.db)) as conn:
                due = daily_send.due(conn, CONFIG, at(later, 6, minute))
            if due is None:
                outcomes.append("not-due")
                continue
            outcomes.append(daily_send.publish(self.db, CONFIG, now=at(later, 6, minute), day=later, trigger="scheduled",
                                               recipient="client@example.com", credentials=CREDS, sender=failing,
                                               collector=collector)["status"])
        self.assertEqual(outcomes, ["failed", "failed", "failed", "not-due"])

    def test_missed_morning_reminder_carries_over_once(self):
        missed, next_day, after = date(2026, 11, 20), date(2026, 11, 21), date(2026, 11, 22)
        carried = daily_send.carry_over(self.db, BOOK, next_day)
        self.assertEqual([(c["key"], c["missed_on"]) for c in carried], [("birthday:nephew:2026:D-7", missed)])
        result = daily_send.publish(self.db, CONFIG, now=at(next_day, 6, 1), day=next_day, trigger="scheduled",
                                    recipient="client@example.com", credentials=CREDS, sender=self.sender, collector=collector)
        self.assertEqual(result["status"], "sent")
        self.assertEqual(daily_send.carry_over(self.db, BOOK, after), [])                 # Said once.

    def test_missing_mail_setup_is_a_clean_failure(self):
        day = date(2026, 11, 20)
        result = daily_send.publish(self.db, CONFIG, now=at(day, 6, 1), day=day, trigger="scheduled", recipient=None,
                                    credentials=None, sender=self.sender, collector=collector)
        self.assertEqual((result["status"], result["error_code"]), ("failed", "no-mail-setup"))
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
