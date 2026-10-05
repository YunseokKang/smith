import json
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from smith import delivery, gmail, ledger
from smith.importer import parse_import

KST = ZoneInfo("Asia/Seoul")
CONFIG = {"app": {"timezone": "Asia/Seoul"},
          "reports": {"enabled": True, "weekdays": ["monday", "thursday"], "time": "06:00"}}
MON = datetime(2026, 10, 5, 6, 0, tzinfo=KST)   # Monday
THU = datetime(2026, 10, 8, 6, 0, tzinfo=KST)   # Thursday
NEXT_MON = datetime(2026, 10, 12, 6, 0, tzinfo=KST)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "smith.db"
        doc = {"schema_version": 1, "import_id": "m1", "source": "manual", "mode": "patch",
               "as_of": "2026-10-01T00:00:00+00:00", "owners": [{"id": "self"}],
               "records": [{"id": "cash", "kind": "asset", "owner_id": "self",
                            "effective_at": "2026-10-01T00:00:00+00:00", "revision": 1, "status": "active",
                            "category": "cash", "account_type": "bank", "currency": "KRW", "value": "1000000",
                            "valuation_method": "manual", "liquidity": "immediate"}]}
        with closing(ledger.connect(self.db)) as conn:
            ledger.apply_import(conn, parse_import(json.dumps(doc)), recorded_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.sent, self.notes = [], []

    def sender(self, outcome="sent"):
        def send(credentials, *, recipient, subject, html, text):
            self.sent.append(subject)
            self.notes.append(html)
            if outcome == "unknown":
                raise gmail.MailError("unknown-outcome", uncertain=True)
            if outcome == "failed":
                raise gmail.MailError("http-400")
            return "msg-id"
        return send

    def due(self, now):
        with closing(ledger.connect(self.db)) as conn:
            return delivery.due(conn, CONFIG, now)

    def publish(self, now, slot, missed=(), outcome="sent"):
        return delivery.publish(self.db, recipient="me@example.com", credentials=("a", "b", "c"), now=now,
                                slot=slot, missed=list(missed), kind="monday", sender=self.sender(outcome))

    def test_slots_follow_the_configured_local_schedule(self):
        slots = delivery.slots_between(CONFIG["reports"], "Asia/Seoul", MON - timedelta(minutes=1), NEXT_MON)
        self.assertEqual(slots, [MON, THU, NEXT_MON])

    def test_each_slot_is_sent_once_and_uncertain_sends_are_not_repeated(self):
        self.assertEqual(self.due(MON + timedelta(minutes=5)), (MON, []))  # First run: no backfill.
        self.assertEqual(self.publish(MON + timedelta(minutes=5), MON)["status"], "sent")
        self.assertIsNone(self.due(MON + timedelta(hours=1)))
        self.assertEqual(self.publish(MON + timedelta(hours=1), MON)["status"], "skipped")
        self.assertEqual(self.publish(THU + timedelta(minutes=1), THU, outcome="unknown")["status"], "unknown")
        self.assertIsNone(self.due(THU + timedelta(hours=1)))  # Possibly delivered: never resent automatically.
        self.assertEqual(len(self.sent), 2)

    def test_activation_skips_the_slot_that_already_passed(self):
        with closing(ledger.connect(self.db)) as conn:
            self.assertEqual(delivery.activate(conn, CONFIG, THU - timedelta(hours=1)), MON)
        self.assertIsNone(self.due(THU - timedelta(minutes=30)))       # Monday passed before activation.
        self.assertEqual(self.due(NEXT_MON + timedelta(hours=2)), (NEXT_MON, [THU]))  # Missed after: sent late.

    def test_a_schedule_edit_applies_from_now_without_duplicate_or_phantom_reports(self):
        with closing(ledger.connect(self.db)) as conn:
            self.assertEqual(delivery.reconcile_schedule(conn, CONFIG, MON), (False, None))  # First run adopts it.
        self.publish(MON + timedelta(minutes=5), MON)
        cases = [  # (edited schedule, when the next run sees it, slot it marks, next slot then sent on time)
            ({"time": "08:00"}, datetime(2026, 10, 7, 7, 0, tzinfo=KST), MON.replace(hour=8), THU.replace(hour=8)),
            ({"weekdays": ["monday", "wednesday", "thursday"]}, THU - timedelta(hours=1),
             datetime(2026, 10, 7, 6, 0, tzinfo=KST), THU),
        ]
        for edit, seen, marked, following in cases:
            with self.subTest(edit=edit):
                config = {**CONFIG, "reports": {**CONFIG["reports"], **edit}}
                with closing(ledger.connect(self.db)) as conn:
                    self.assertEqual(delivery.reconcile_schedule(conn, config, seen), (True, marked))
                    self.assertIsNone(delivery.due(conn, config, seen))   # Nothing before the edit goes out.
                    self.assertEqual(delivery.due(conn, config, following + timedelta(minutes=5)), (following, []))
                    self.assertEqual(delivery.reconcile_schedule(conn, config, seen), (False, None))
                    delivery.reconcile_schedule(conn, CONFIG, seen)    # Back to Monday/Thursday 06:00.

    def test_clean_failures_retry_up_to_the_limit(self):
        for attempt in range(ledger.MAX_SEND_ATTEMPTS):
            self.assertEqual(self.due(MON + timedelta(minutes=attempt)), (MON, []))
            self.assertEqual(self.publish(MON + timedelta(minutes=attempt), MON, outcome="failed")["status"], "failed")
        self.assertIsNone(self.due(MON + timedelta(hours=1)))

    def test_missed_slots_are_merged_into_one_late_report(self):
        self.publish(MON + timedelta(minutes=1), MON)
        late = NEXT_MON + timedelta(hours=3)  # PC was off on Thursday and early Monday.
        slot, missed = self.due(late)
        self.assertEqual((slot, missed), (NEXT_MON, [THU]))
        result = self.publish(late, slot, missed)
        self.assertEqual(result["status"], "sent")
        self.assertIn("지연 발송", self.sent[-1])
        self.assertIsNone(self.due(late + timedelta(minutes=15)))
        with closing(ledger.connect(self.db)) as conn:
            statuses = {r["slot"]: r["status"] for r in ledger.report_runs(conn) if r["slot"]}
        self.assertEqual(sorted(statuses.values()), ["merged", "sent", "sent"])

    def test_a_failed_late_send_keeps_the_missed_slots_for_the_retry(self):
        self.publish(MON + timedelta(minutes=1), MON)
        late = NEXT_MON + timedelta(hours=3)
        slot, missed = self.due(late)
        self.assertEqual(self.publish(late, slot, missed, outcome="failed")["status"], "failed")
        self.assertEqual(self.due(late + timedelta(minutes=15)), (NEXT_MON, [THU]))  # Thursday not yet settled.
        self.publish(late + timedelta(minutes=15), NEXT_MON, [THU])
        self.assertIn("10월 08일 06:00", self.notes[-1])

    def test_interrupted_runs_are_retried_only_before_the_mail_request(self):
        with closing(ledger.connect(self.db)) as conn:
            ledger.claim_report(conn, report_id="r1", slot=MON, kind="monday", trigger="scheduled", created_at=MON,
                                as_of=MON, baseline=None, missed_slots=[])
        self.assertIsNone(self.due(MON + timedelta(minutes=10)))         # In progress: leave it alone.
        with closing(ledger.connect(self.db)) as conn:
            self.assertEqual(delivery.recover(conn, MON + timedelta(hours=1))[0]["was"], "building")
        self.assertEqual(self.due(MON + timedelta(hours=1)), (MON, []))   # Never reached Gmail: retry.
        with closing(ledger.connect(self.db)) as conn:
            ledger.claim_report(conn, report_id="r2", slot=THU, kind="thursday", trigger="scheduled", created_at=THU,
                                as_of=THU, baseline=None, missed_slots=[])
            ledger.start_sending(conn, report_id="r2", now=THU, subject="s", html_sha256="d")
            delivery.recover(conn, THU + timedelta(hours=1))
            self.assertEqual(ledger.report_runs(conn)[0]["status"], "unknown")
        self.assertIsNone(self.due(THU + timedelta(hours=1)))            # May have arrived: never resent.
        self.publish(NEXT_MON + timedelta(minutes=1), NEXT_MON)
        self.assertIn("10월 08일 06:00 보고는 발송 결과를 확인하지 못했습니다", self.notes[-1])

    def test_a_run_that_lost_its_claim_does_not_send(self):
        with closing(ledger.connect(self.db)) as conn:
            ledger.claim_report(conn, report_id="old", slot=MON, kind="monday", trigger="scheduled", created_at=MON,
                                as_of=MON, baseline=None, missed_slots=[])
            delivery.recover(conn, MON + timedelta(hours=1))   # Lease expired: the old run is closed.
            ledger.claim_report(conn, report_id="new", slot=MON, kind="monday", trigger="scheduled",
                                created_at=MON + timedelta(hours=1), as_of=MON, baseline=None, missed_slots=[])
            # The old process resumes: it must not move the slot to sending, and so must not send.
            self.assertFalse(ledger.start_sending(conn, report_id="old", now=MON, subject="s", html_sha256="d"))
            self.assertEqual(ledger.report_runs(conn)[0]["status"], "building")
            self.assertTrue(ledger.start_sending(conn, report_id="new", now=MON, subject="s", html_sha256="d"))

    def test_an_earlier_failed_slot_is_reported_with_the_next_one(self):
        self.publish(MON + timedelta(minutes=1), MON)
        self.publish(THU + timedelta(minutes=1), THU, outcome="failed")   # One failure, attempts left.
        late = NEXT_MON + timedelta(minutes=5)
        self.assertEqual(self.due(late), (NEXT_MON, [THU]))               # Not silently dropped.
        self.publish(late, NEXT_MON, [THU])
        with closing(ledger.connect(self.db)) as conn:
            statuses = {r["slot"]: r["status"] for r in ledger.report_runs(conn) if r["slot"]}
        self.assertEqual(statuses[ledger._db_time(THU)], "merged")

    def test_a_sending_row_without_a_start_time_is_recovered(self):
        # Rows migrated from v7 have no send_started_at.
        with closing(ledger.connect(self.db)) as conn:
            ledger.claim_report(conn, report_id="v7", slot=MON, kind="monday", trigger="scheduled", created_at=MON,
                                as_of=MON, baseline=None, missed_slots=[])
            conn.execute("UPDATE report_runs SET status = 'sending', send_started_at = NULL WHERE report_id = 'v7'")
            delivery.recover(conn, MON + timedelta(hours=1))
            self.assertEqual(ledger.report_runs(conn)[0]["status"], "unknown")

    def test_lateness_is_judged_when_the_message_is_sent(self):
        # Built 5 minutes after the slot, but the narrative took 40 more minutes: the report is late.
        with mock.patch("smith.delivery.time.monotonic", side_effect=[0.0, 2400.0]):
            self.publish(MON + timedelta(minutes=5), MON)
        self.assertIn("지연 발송", self.sent[-1])

    def test_missing_setup_is_recorded_for_unattended_runs(self):
        result = delivery.record_failure(self.db, now=MON, slot=MON, missed=[], kind="monday",
                                         code="setup-no-gmail-grant")
        self.assertEqual(result["status"], "failed")
        with closing(ledger.connect(self.db)) as conn:
            self.assertEqual(ledger.report_runs(conn)[0]["error_code"], "setup-no-gmail-grant")


if __name__ == "__main__":
    unittest.main()
