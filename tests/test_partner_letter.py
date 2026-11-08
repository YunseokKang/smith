"""The spouse's own letter (report design §42): its own run state, its address, only split editions, and its
proposal history kept apart from the client's. Synthetic household, nothing is sent."""
import json
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from smith import ledger, partner_letter
from smith.importer import parse_import

AT = datetime(2026, 11, 6, 21, 0, tzinfo=timezone.utc)   # Friday 06:00 in Seoul.
CONFIG = {"app": {"timezone": "Asia/Seoul"}, "reports": {}, "mail": {"recipient": "me@example.com",
                                                                     "partner_recipient": "family@example.com"}}


def rec(record_id, kind, owner, **fields):
    return {"id": record_id, "kind": kind, "owner_id": owner, "effective_at": "2026-10-01T00:00:00+00:00",
            "revision": 1, "status": "active", **fields}


class PartnerLetterTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "smith.db"
        records = [
            rec("cash", "asset", "self", category="cash", account_type="bank", currency="KRW", value="1000000",
                valuation_method="manual", liquidity="immediate"),
            rec("irp-partner", "asset", "partner", category="fund", account_type="irp", currency="KRW", value="5000000",
                valuation_method="statement", liquidity="restricted"),
            rec("salary-partner", "cashflow", "partner", direction="inflow", category="salary", currency="KRW",
                amount="4000000", frequency="monthly", start_date="2026-01-01")]
        doc = {"schema_version": 1, "import_id": "p1", "source": "manual", "mode": "patch",
               "as_of": "2026-10-01T00:00:00+00:00", "owners": [{"id": "self"}, {"id": "partner"}], "records": records}
        with closing(ledger.connect(self.db)) as conn:
            ledger.apply_import(conn, parse_import(json.dumps(doc)), recorded_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.sent = []

    def send(self, credentials, *, recipient, subject, html, text, **extra):
        self.sent.append((recipient, subject, html))
        return f"msg-{len(self.sent)}"

    def publish(self, kind="tax", config=CONFIG, trigger="scheduled"):
        return partner_letter.publish(self.db, config, now=AT, kind=kind, trigger=trigger, credentials=("a", "b", "c"),
                                      household={"partner_birth_year": 1990, "birth_year": 1990}, sender=self.send)

    def test_one_letter_per_split_edition_and_day_to_the_spouse_only(self):
        first = self.publish()
        self.assertEqual((first["status"], first["run_key"]), ("sent", "report-partner:tax:2026-11-07"))
        (recipient, _, html), = self.sent
        self.assertEqual(recipient, "family@example.com")
        self.assertIn("나의 세금·연금", html)
        self.assertEqual(self.publish()["error_code"], "already-sent")              # The same slot never twice.
        self.assertEqual(self.publish(trigger="manual")["status"], "sent")           # A manual send is its own run.
        self.assertEqual(self.publish(kind="property")["error_code"], "not-personalized")
        without = {**CONFIG, "mail": {"recipient": "me@example.com"}}
        self.assertEqual(self.publish(config=without)["error_code"], "no-partner-recipient")
        self.assertEqual(len(self.sent), 2)
        with closing(ledger.connect_read_only(self.db)) as conn:
            # Her proposals are kept under her scope; the client's report runs never see her letter.
            identities = set(ledger.active_proposals(conn))
            self.assertTrue(identities and all(i.startswith("partner:") for i in identities))
            self.assertEqual(ledger.report_runs(conn), [])
            self.assertEqual([r["status"] for r in ledger.daily_runs(conn, product=partner_letter.PRODUCT)], ["sent", "sent"])


    def test_a_failed_letter_is_retried_on_its_key_and_a_sent_one_never_again(self):
        def rejected(credentials, **kwargs):
            from smith import gmail
            raise gmail.MailError("http-400")
        failed = partner_letter.publish(self.db, CONFIG, now=AT, kind="tax", trigger="scheduled", credentials=("a", "b", "c"),
                                        household={"partner_birth_year": 1990, "birth_year": 1990}, sender=rejected)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(self.publish()["status"], "sent")                         # A later run retries the same key.
        self.assertEqual(self.publish()["error_code"], "already-sent")


    def test_the_retry_follows_a_scheduled_report_that_went_out_or_may_have(self):
        # Codex review 2026-11-08: a client report left unknown (a lost Gmail response) still lets the spouse's
        # failed letter be retried on its own key, within the window; the client's mail is never resent here.
        from datetime import date, timedelta
        from unittest import mock

        from smith import report_command
        with closing(ledger.connect(self.db)) as conn:
            ledger.claim_report(conn, report_id="r1", slot=AT, kind="tax", trigger="scheduled", created_at=AT, as_of=AT,
                                baseline=None, missed_slots=[])
            ledger.finish_report(conn, report_id="r1", status="unknown", finished_at=AT)
        outcome = {"status": "sent", "run_key": "k", "error_code": None}
        with mock.patch("smith.partner_letter.publish", return_value=outcome) as publish, \
                mock.patch("smith.credentials.load_gmail", return_value=("a", "b", "c")):
            report_command._retry_partner_letter(self.db, CONFIG, AT + timedelta(hours=1))
            report_command._retry_partner_letter(self.db, CONFIG, AT + timedelta(hours=7))   # Past the window.
        self.assertEqual(publish.call_count, 1)
        self.assertEqual((publish.call_args.kwargs["kind"], publish.call_args.kwargs["day"]), ("tax", date(2026, 11, 7)))


    def test_the_letter_narrates_itself_on_the_client_report_s_snapshot_and_a_retry_keeps_it(self):
        # One narrative call per person (user decision 2026-11-08): the letter's narrator gets the spouse's side of the
        # household, built at the ledger time of the client's report (Codex review 2026-11-08), on the retry too.
        from datetime import timedelta

        from smith import gmail, narrative
        later = rec("msft-partner", "asset", "partner", category="stock", account_type="brokerage", currency="KRW",
                    value="9000000", valuation_method="market", liquidity="days", symbol="MSFT", market="US",
                    quantity="10", unit_price="900000", average_cost="500000", instrument_name="Microsoft")
        later["effective_at"] = (AT + timedelta(hours=1)).isoformat()
        doc = {"schema_version": 1, "import_id": "p3", "source": "manual", "mode": "patch", "as_of": later["effective_at"],
               "owners": [{"id": "partner"}], "records": [later]}
        with closing(ledger.connect(self.db)) as conn:
            ledger.apply_import(conn, parse_import(json.dumps(doc)), recorded_at=AT + timedelta(hours=1))
        seen = []

        def narrator(data):
            seen.append((data["audience"], data["as_of"]))
            narrative.attach(data, None, None, {"outcome": "skipped", "run_ids": ["n1"]})
            data["narrative"]["household"] = "배우자 쪽 호출이 쓴 두 분 문단"

        def rejected(credentials, **kwargs):
            raise gmail.MailError("http-400")
        household = {"partner_birth_year": 1990, "birth_year": 1990}
        failed = partner_letter.publish(self.db, CONFIG, now=AT + timedelta(hours=2), kind="markets", trigger="scheduled",
                                        credentials=("a", "b", "c"), household=household, sender=rejected,
                                        narrator=narrator, snapshot_at=AT)
        self.assertEqual(failed["status"], "failed")
        retried = partner_letter.publish(self.db, CONFIG, now=AT + timedelta(hours=3), kind="markets", trigger="scheduled",
                                         credentials=("a", "b", "c"), household=household, sender=self.send,
                                         narrator=narrator)
        self.assertEqual(retried["status"], "sent")
        self.assertEqual(seen, [("partner", AT), ("partner", AT)])     # The spouse's side, the client's snapshot, twice.
        (_, _, html), = self.sent
        self.assertNotIn("Microsoft", html)            # Recorded after the client's report: not in this letter.
        self.assertIn("배우자 쪽 호출이 쓴 두 분 문단", html)
        with closing(ledger.connect_read_only(self.db)) as conn:
            self.assertEqual(ledger.daily_snapshot(conn, retried["run_key"])["narrative_run_ids"], ["n1"])

    def test_the_writer_reads_the_spouse_s_own_last_letters_and_not_the_client_s_memory(self):
        # The spouse's next letter on the theme is told what the last ones said (report design §33, §42), and the
        # client's memory, told from the client's side, stays out of that call.
        from smith import narrative
        verified = {"insights": [{"title": "금리 인하 신호", "body": "", "implication": ""}], "watch": [], "experiences": []}
        with closing(ledger.connect(self.db)) as conn:
            ledger.record_advice_run(conn, run_id="n1", created_at=AT,
                                     use_case=narrative.FINAL_USE_CASE + narrative.PARTNER_USE_CASE,
                                     question="markets", payload="{}", prompt_version="narrative-test", model="test",
                                     cost_usd=None, outcome="success", error_code=None,
                                     advice=json.dumps({"verified": verified}, ensure_ascii=False))

        def narrator(data):
            narrative.attach(data, None, None, {"outcome": "skipped", "run_ids": ["n1"]})
        self.assertEqual(partner_letter.publish(self.db, CONFIG, now=AT, kind="markets", trigger="scheduled",
                                                credentials=("a", "b", "c"), sender=self.send, narrator=narrator,
                                                household={"partner_birth_year": 1990, "birth_year": 1990})["status"], "sent")
        hers = {"kind": "markets", "audience": "partner", "as_of": AT, "advice": {"proposals": [], "strategy": []}}
        told = narrative.previous_report(self.db, hers)
        self.assertEqual((told["date"], [i["title"] for i in told["insights"]]), ("2026-11-07", ["금리 인하 신호"]))
        self.assertIsNone(narrative.previous_report(self.db, dict(hers, audience="self")))   # The client's: none sent.
        self.assertEqual(narrative.report_memory(self.db, hers)[0], [])


class PersonalIncomeTests(unittest.TestCase):
    def test_a_transfer_between_one_s_own_accounts_is_not_income(self):
        # Codex review 2026-11-08: an incoming internal transfer diluted the premium share of income.
        from smith import tax
        from smith.payload import load_view
        transfer = rec("in-transfer", "cashflow", "partner", direction="inflow", category="internal_transfer",
                       currency="KRW", amount="2000000", frequency="monthly", start_date="2026-01-01")
        doc = {"schema_version": 1, "import_id": "p2", "source": "manual", "mode": "patch",
               "as_of": "2026-10-01T00:00:00+00:00", "owners": [{"id": "partner"}], "records": [transfer]}
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "smith.db"
            with closing(ledger.connect(db)) as conn:
                ledger.apply_import(conn, parse_import(json.dumps(doc)), recorded_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
                view = load_view(conn, as_of=AT, known_at=AT)
        self.assertEqual(tax.facts(view, AT.date(), None, owner="partner")["monthly_income"], 0)


class PersonalTaxNotesTests(unittest.TestCase):
    def test_household_notes_leave_the_reader_s_own_tax_card(self):
        # Codex review 2026-11-08: marriage, gifts and the other member's home are the household's, so a per-person
        # letter's own tax card never shows them (nor the other member's figures).
        from smith import report_html, tax
        own, shared = tax.Note("나의 메모", "본문.", "언제", "근거"), tax.Note("두 분 메모", "본문.", "언제", "근거", household=True)
        advice = {"tax_notes": [own, shared], "tax_checklist": []}
        data = {"kind": "tax", "as_of": AT}
        mine = report_html._tax(advice, data, True)
        self.assertIn("나의 메모", mine)
        self.assertNotIn("두 분 메모", mine)
        self.assertIn("두 분 메모", report_html._household_tax(advice))
        self.assertIn("두 분 메모", report_html._tax(advice, {"kind": "overview", "as_of": AT}, True))   # Not split yet.


if __name__ == "__main__":
    unittest.main()
