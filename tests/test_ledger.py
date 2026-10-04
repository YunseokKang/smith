import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from smith import ledger
from smith.importer import load_import_file, parse_import
from smith.records import Action, ImportRejected, Kind

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "portfolio.example.json"
SEP1, OCT1, NOV1 = "2026-09-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00", "2026-11-01T00:00:00+00:00"
SEP15, OCT15, NOV15 = (datetime(2026, month, 15, tzinfo=timezone.utc) for month in (9, 10, 11))
RECORDED = datetime(2026, 11, 20, tzinfo=timezone.utc)
LATER = datetime(2026, 12, 1, tzinfo=timezone.utc)


def asset(record_id: str = "asset-a", revision: int = 1, value: str = "100", effective_at: str = SEP1,
          **extra: object) -> dict:
    return {"id": record_id, "kind": "asset", "owner_id": "owner-a", "effective_at": effective_at,
            "revision": revision, "status": "active", "category": "cash", "account_type": "bank",
            "currency": "KRW", "value": value, "valuation_method": "manual", "liquidity": "immediate", **extra}


def closing_revision(revision: int, effective_at: str) -> dict:
    return {"id": "asset-a", "kind": "asset", "owner_id": "owner-a", "effective_at": effective_at,
            "revision": revision, "status": "closed"}


def liability(record_id: str = "liability-a", **extra: object) -> dict:
    return {"id": record_id, "kind": "liability", "owner_id": "owner-a", "effective_at": SEP1,
            "revision": 1, "status": "active", "category": "credit_loan", "currency": "KRW",
            "outstanding_principal": "500", "annual_rate": "0.05", "rate_type": "fixed",
            "repayment_method": "bullet", **extra}


def cashflow(**extra: object) -> dict:
    return {"id": "cashflow-a", "kind": "cashflow", "owner_id": "owner-a", "effective_at": SEP1,
            "revision": 1, "status": "active", "direction": "inflow", "category": "salary",
            "currency": "KRW", "amount": "300", "frequency": "monthly", "start_date": "2026-09-01", **extra}


def document(*records: dict, import_id: str = "import-1", **envelope: object) -> dict:
    doc = {"schema_version": 1, "import_id": import_id, "source": "manual", "mode": "patch",
           "as_of": SEP1, "owners": [{"id": "owner-a"}], "records": list(records) or [asset()]}
    doc.update(envelope)
    return doc


class ParseImportTests(unittest.TestCase):
    def test_example_file_with_byte_order_mark_is_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bom.json"
            path.write_bytes(b"\xef\xbb\xbf" + EXAMPLE.read_bytes())
            batch = load_import_file(path)
        self.assertEqual({record.kind for record in batch.records}, set(Kind))

    def test_values_are_canonical_and_hash_ignores_formatting(self):
        doc = document(asset(value="1000.50"))
        compact, indented = parse_import(json.dumps(doc)), parse_import(json.dumps(doc, indent=2))
        self.assertEqual(compact.records[0].fields["value"], "1000.5")
        self.assertEqual(compact.payload_sha256, indented.payload_sha256)

    def test_invalid_documents_are_rejected(self):
        text = json.dumps(document())
        cases = [
            ("float amount", text.replace('"value": "100"', '"value": 100.5'), "decimal strings"),
            ("duplicate key", text.replace('"value": "100"', '"value": "100", "value": "200"'), "duplicate key"),
            ("negative", document(asset(value="-1")), "must not be negative"),
            ("negative zero", document(asset(value="-0.00")), "must not be negative"),
            ("separator", document(asset(value="1,000")), "decimal string"),
            ("unknown field", document(asset(note="x")), "unknown field"),
            ("missing field", document({k: v for k, v in asset().items() if k != "currency"}), "is required"),
            ("unsupported currency", document(asset(currency="ZZZ")), "supported currency"),
            ("loan payment link", document(cashflow(direction="outflow", category="loan_payment")),
             "required for category loan_payment"),
            ("naive time", document(asset(effective_at="2026-09-01T00:00:00")), "UTC offset"),
            ("undeclared owner", document(asset(owner_id="owner-b")), "declared in owners"),
            ("duplicate id", document(asset(), asset()), "duplicate record id"),
            ("schema version", document(schema_version=2), "schema_version"),
            ("snapshot", document(mode="snapshot"), "not supported yet"),
            ("api source", document(source="toss"), "manual"),
            ("percent rate", document(liability(annual_rate="4.5")), "fraction below 1"),
            ("end before start", document(cashflow(end_date="2026-08-01")), "end_date"),
            ("direction", document(cashflow(direction="outflow")), "must be inflow"),
            ("correction reason", document(asset(revision=2, change_type="correction", corrects_revision=1)),
             "required for a correction"),
            ("bool revision", document(asset(revision=True)), "positive integer"),
            ("no records", document(records=[]), "at least one"),
        ]
        for label, doc, expected in cases:
            with self.subTest(label):
                with self.assertRaises(ImportRejected) as caught:
                    parse_import(doc if isinstance(doc, str) else json.dumps(doc))
                self.assertTrue(any(expected in p for p in caught.exception.problems), caught.exception.problems)

    def test_all_problems_are_reported_together(self):
        unknown_kind = {"id": "x-1", "kind": "loan", "owner_id": "owner-a", "effective_at": SEP1,
                        "revision": 0, "status": "active"}
        for record, expected in [(asset(value="-1", currency="krw"), ["currency", "value"]),
                                 (unknown_kind, ["kind", "revision"])]:
            with self.subTest(expected=expected):
                with self.assertRaises(ImportRejected) as caught:
                    parse_import(json.dumps(document(record)))
                problems = caught.exception.problems
                self.assertEqual([p.split(":")[0].rsplit(".", 1)[-1] for p in problems], expected)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.conn = ledger.connect(":memory:")
        self.addCleanup(self.conn.close)

    def apply(self, *records: dict, import_id: str, recorded_at: datetime = RECORDED, dry_run: bool = False,
              **envelope: object):
        batch = parse_import(json.dumps(document(*records, import_id=import_id, **envelope)))
        return ledger.apply_import(self.conn, batch, recorded_at=recorded_at, dry_run=dry_run)

    def state(self, as_of: datetime, known_at: datetime = LATER, record_id: str = "asset-a"):
        states = {s.record.record_id: s.record for s in ledger.record_states(self.conn, as_of=as_of, known_at=known_at)}
        record = states.get(record_id)
        return None if record is None else (record.status, record.fields.get("value"))

    def history(self, record_id: str = "asset-a") -> int:
        return len(ledger.record_history(self.conn, record_id))

    def test_reimport_is_idempotent_and_changed_content_is_rejected(self):
        self.assertEqual(self.apply(asset(), import_id="i1").actions, (("asset-a", 1, Action.CREATED),))
        self.assertEqual(self.apply(asset(), import_id="i1").outcome, "already_imported")
        with self.assertRaises(ImportRejected):
            self.apply(asset(value="999"), import_id="i1")
        self.assertEqual(self.apply(asset(), import_id="i2").actions[0][2], Action.UNCHANGED)
        self.assertEqual(self.history(), 1)

    def test_update_and_close_follow_effective_time(self):
        self.apply(asset(value="100"), import_id="i1")
        updated = self.apply(asset(revision=2, value="200", effective_at=OCT1), import_id="i2")
        closed = self.apply(closing_revision(3, NOV1), import_id="i3")
        self.assertEqual((updated.actions[0][2], closed.actions[0][2]), (Action.UPDATED, Action.CLOSED))
        self.assertEqual(self.state(SEP15), ("active", "100"))
        self.assertEqual(self.state(OCT15), ("active", "200"))
        self.assertEqual(self.state(NOV15), ("closed", None))
        self.assertEqual(self.history(), 3)

    def test_correction_replaces_only_its_period_and_respects_known_time(self):
        self.apply(asset(value="100"), import_id="i1")
        self.apply(asset(revision=2, value="200", effective_at=OCT1), import_id="i2")
        corrected_at = datetime(2026, 11, 25, tzinfo=timezone.utc)
        result = self.apply(asset(revision=3, value="150", change_type="correction", corrects_revision=1,
                                  reason="typo"), import_id="i3", recorded_at=corrected_at)
        self.assertEqual(result.actions[0][2], Action.CORRECTED)
        self.assertEqual(self.state(SEP15), ("active", "150"))
        self.assertEqual(self.state(OCT15), ("active", "200"))
        self.assertEqual(self.state(SEP15, known_at=RECORDED), ("active", "100"))

    def test_conflicting_revision_rejects_the_whole_import(self):
        self.apply(asset(value="100"), import_id="i1")
        self.apply(asset(revision=2, value="110", change_type="correction", corrects_revision=1, reason="typo"),
                   import_id="i2")
        cases = [
            ("gap", asset(revision=4), {}, "expected 3"),
            ("stale", asset(revision=1), {}, "expected 3"),
            ("same revision", asset(revision=2, value="120"), {}, "different content"),
            ("kind change", liability(record_id="asset-a", revision=3), {}, "cannot change"),
            ("source change", asset(revision=3, effective_at=OCT1), {"source": "manual-other"}, "cannot change"),
            ("double correction", asset(revision=3, change_type="correction", corrects_revision=1, reason="x"),
             {}, "already corrected"),
            # Moving a correction to another time would leave the target's period without a state.
            ("correction time", asset(revision=3, effective_at=OCT1, change_type="correction",
                                      corrects_revision=2, reason="x"), {}, "must keep the effective_at"),
            ("backdated update", asset(revision=3, effective_at=SEP1), {}, "must be later than"),
            ("missing reference", cashflow(direction="outflow", category="loan_payment",
                                           liability_record_id="liability-missing"), {}, "existing liability"),
            ("future as_of", asset(revision=3, effective_at=OCT1), {"as_of": "2027-01-01T00:00:00+00:00"}, "future"),
        ]
        for label, record, envelope, expected in cases:
            with self.subTest(label):
                with self.assertRaises(ImportRejected) as caught:
                    self.apply(asset(record_id="asset-b"), record, import_id=label.replace(" ", "-"), **envelope)
                self.assertTrue(any(expected in p for p in caught.exception.problems), caught.exception.problems)
                self.assertEqual((self.history("asset-b"), self.history()), (0, 2))

    def test_closed_record_accepts_only_corrections(self):
        self.apply(asset(value="100"), import_id="i1")
        self.apply(closing_revision(2, OCT1), import_id="i2")
        with self.assertRaises(ImportRejected) as caught:
            self.apply(asset(revision=3, effective_at=NOV1), import_id="i3")
        self.assertIn("is closed", caught.exception.problems[0])
        self.apply(asset(revision=3, effective_at=OCT1, change_type="correction", corrects_revision=2,
                         reason="closed by mistake"), import_id="i4")
        self.assertEqual(self.state(NOV15), ("active", "100"))

    def test_dry_run_writes_nothing(self):
        loan = liability(collateral_record_id="asset-a")  # Resolved against the same batch.
        result = self.apply(asset(), loan, import_id="i1", dry_run=True)
        self.assertEqual((result.outcome, [a for _, _, a in result.actions]), ("dry_run", [Action.CREATED] * 2))
        self.assertEqual(self.history(), 0)
        self.assertEqual(self.apply(asset(), loan, import_id="i1").outcome, "applied")


if __name__ == "__main__":
    unittest.main()
