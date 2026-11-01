"""Approval-gated trading (docs/trading-design.md): order shape, plans, approval codes and the execution state machine.

Everything runs against an in-memory fake broker and fake mail: no network, no real order, no real mail.
"""
import re
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from smith import totp, trade_executor, trade_mail, trade_plan, trade_store
from smith.config import _check_trading
from smith.toss import ForbiddenRequest, TossClient, TossError
from smith.toss_orders import OrderClient, check_order

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 11, 3, 0, 30, tzinfo=KST)          # Inside the US regular session below.
SECRET = totp.new_secret()
SETTINGS = {"enabled": True, "max_order_krw": 5_000_000, "max_daily_krw": 10_000_000, "plan_ttl_minutes": 60,
            "limit_band_pct": 1.0, "drift_halt_pct": 3.0}
VALID = {"clientOrderId": "smith-abc-1", "symbol": "CLS", "side": "SELL", "orderType": "LIMIT", "timeInForce": "DAY",
         "quantity": "10", "price": "285.20", "confirmHighValueOrder": False}


class FakeBroker:
    """The read calls trade_plan uses and the two writes, with scripted outcomes for `place`."""

    def __init__(self, clock: "Clock") -> None:
        self.clock = clock
        self.price = Decimal("288.12")
        self.quantity = "10"
        self.placed: list[dict] = []
        self.outcomes: list = []            # per place(): "fill", "open", or an exception to raise
        self.orders_by_id: dict[str, dict] = {}
        self.power = "100000"
        self.listed: list[dict] = []
        self.warnings_list: list[dict] = []

    def stocks(self, symbols):
        return [{"symbol": "CLS", "name": "셀레스티카", "currency": "USD", "status": "ACTIVE", "securityType": "FOREIGN_STOCK"}]

    def holdings(self, account_seq):
        return {"items": [{"symbol": "CLS", "quantity": self.quantity, "averagePurchasePrice": "93.6"}]}

    def sellable_quantity(self, account_seq, symbol):
        return {"sellableQuantity": self.quantity}

    def prices(self, symbols):
        return [{"symbol": "CLS", "lastPrice": str(self.price), "currency": "USD"}]

    def exchange_rate(self, base, quote):
        return {"midRate": "1240"}

    def commissions(self, account_seq):
        return [{"marketCountry": "US", "commissionRate": "0.001"}]

    def warnings(self, symbol):
        return self.warnings_list

    def buying_power(self, account_seq, currency):
        return {"cashBuyingPower": self.power}

    def market_calendar(self, market):
        day = lambda start, end: {"regularMarket": {"startTime": start.isoformat(), "endTime": end.isoformat()}}  # noqa: E731
        return {"previousBusinessDay": day(NOW - timedelta(days=1, hours=1), NOW - timedelta(hours=18, minutes=30)),
                "today": day(NOW - timedelta(hours=1), NOW + timedelta(hours=5, minutes=30)),
                "nextBusinessDay": day(NOW + timedelta(hours=23), NOW + timedelta(hours=29, minutes=30))}

    def orders(self, account_seq, *, status, symbol=None, day=None):
        if status == "OPEN":
            return {"orders": [o for o in self.orders_by_id.values() if o["status"] == "PENDING"], "hasNext": False}
        return {"orders": self.listed + [o for o in self.orders_by_id.values() if o["status"] != "PENDING"],
                "hasNext": False}

    def order(self, account_seq, order_id):
        return self.orders_by_id[order_id]

    def place(self, account_seq, body):
        check_order(body)
        self.placed.append(dict(body))
        outcome = self.outcomes.pop(0) if self.outcomes else "fill"
        if isinstance(outcome, Exception):
            raise outcome
        order_id = f"o{len(self.placed)}"
        self.orders_by_id[order_id] = {
            "orderId": order_id, "status": "FILLED" if outcome == "fill" else "PENDING", "side": body["side"],
            "quantity": body["quantity"], "price": body["price"], "orderedAt": self.clock().isoformat(),
            "execution": {"filledQuantity": body["quantity"] if outcome == "fill" else "0",
                          "averageFilledPrice": body["price"] if outcome == "fill" else None,
                          "commission": "2.85", "tax": "0", "settlementDate": "2026-11-04"}}
        return {"orderId": order_id, "clientOrderId": body["clientOrderId"]}


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class TradingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = trade_store.connect(":memory:")
        self.clock = Clock(NOW)
        self.broker = FakeBroker(self.clock)
        self.mails: list[tuple[str, str]] = []
        self.inbox: list[tuple[str, trade_mail.Reply]] = []     # (thread, reply); a thread is its confirmation id
        self.deps = trade_executor.Deps(
            client=self.broker, account_seq=7, account="toss-0123abcd", mail=self._mail, clock=self.clock, sleep=lambda _: None,
            settings=dict(SETTINGS), tz="Asia/Seoul", totp_secret=SECRET, thread_of=lambda message_id: message_id,
            replies=lambda thread, after, seen: [r for t, r in self.inbox
                                                 if t == thread and r.message_id not in seen and r.sent_at > after],
            sent_with=lambda text, after, before: [r.message_id for _, r in self.inbox if r.code == text
                                                   and after <= r.sent_at.timestamp() < before])

    def tearDown(self) -> None:
        self.conn.close()

    def _mail(self, subject: str, text: str, html: str) -> str:
        self.mails.append((subject, text))
        return f"m{len(self.mails)}"

    def _propose(self, request: trade_plan.Request | None = None) -> dict:
        request = request or trade_plan.Request("SELL", "CLS", None, rebuy=True, said="셀레스티카 전부 팔고 다시 사 줘")
        return trade_executor.propose(self.conn, self.deps, request, account="toss-0123abcd")

    def _reply(self, code: str | None, *, at: datetime | None = None, message_id: str = "r1", thread: str = "m1",
               decision: str = "approve") -> None:
        at = at or self.clock.now + timedelta(seconds=40)
        code = code if code is not None or decision != "approve" else totp.code_at(SECRET, totp.step_of(at))
        self.inbox.append((thread, trade_mail.Reply(message_id, at, decision, code)))
        self.clock.now = at + timedelta(seconds=20)

    # --- order shape -------------------------------------------------------------------------------------------

    def test_only_whole_share_limit_day_orders_pass_the_shape_check(self):
        check_order(VALID)
        for change in ({"orderType": "MARKET"}, {"timeInForce": "CLS"}, {"quantity": "0.5"}, {"quantity": "0"},
                       {"confirmHighValueOrder": True}, {"orderAmount": "100"}, {"clientOrderId": ""},
                       {"price": "-1"}, {"side": "SHORT"}):
            with self.subTest(change=change), self.assertRaises(ForbiddenRequest):
                check_order({**VALID, **change})

    def test_order_client_allows_place_and_cancel_only_and_read_client_allows_neither(self):
        sent = []
        client = OrderClient("id", "secret", transport=lambda *a: sent.append(a) or (500, b"{}"))
        for method, path, allowed in (("POST", "/oauth2/token", True), ("POST", "/api/v1/orders", True),
                                      ("POST", "/api/v1/orders/abc/cancel", True),
                                      ("POST", "/api/v1/orders/abc/modify", False),
                                      ("POST", "/api/v1/conditional-orders", False), ("GET", "/api/v1/orders/abc", True)):
            with self.subTest(path=path):
                self.assertEqual(client._allows(method, path), allowed)
                self.assertFalse(TossClient("id", "secret")._allows(method, path) and path.startswith("/api/v1/orders")
                                 and method == "POST")
        self.assertEqual(sent, [])

    def test_model_facing_modules_never_mention_the_order_path(self):
        source = Path(__file__).resolve().parents[1] / "src" / "smith"
        order_path = re.compile(r"(toss_orders|trade_executor|trade_command|OrderClient)")
        for name in ("adviser", "narrative", "qa", "daily_qa", "health_qa", "research", "picks", "events",
                     "disclosure_insight", "headless", "qa_lookup", "talk"):
            with self.subTest(module=name):
                self.assertIsNone(order_path.search((source / f"{name}.py").read_text(encoding="utf-8")))

    # --- plans -------------------------------------------------------------------------------------------------

    def test_sell_and_rebuy_plan_bounds_prices_and_estimates_the_gain(self):
        plan = trade_plan.build(self.broker, account_seq=7, account="toss-0123abcd", settings=SETTINGS, now=NOW,
                                spent_today_krw=0, request=trade_plan.Request("SELL", "CLS", None, rebuy=True))
        self.assertEqual([leg["side"] for leg in plan["legs"]], ["SELL", "BUY"])
        self.assertEqual(plan["quantity"], "10")
        self.assertIsNone(plan["legs"][1]["quantity"])           # buys back what the sell filled
        self.assertEqual(plan["legs"][0]["floor"], "276.68")      # 288.12 x 0.97 x 0.99, rounded down to the cent
        self.assertEqual(plan["gain_estimate_krw"], 2_412_048)    # (288.12 - 93.6) x 10 x 1240
        self.assertTrue(plan["legs"][0]["client_order_id"] != plan["legs"][1]["client_order_id"])

    def test_plans_outside_the_limits_or_holdings_are_rejected(self):
        cases = (({"max_order_krw": 1_000_000}, trade_plan.Request("SELL", "CLS", None), "한도"),
                 ({}, trade_plan.Request("SELL", "CLS", 11), "보유 수량"),
                 ({}, trade_plan.Request("BUY", "CLS", 100), "한도"))
        for change, request, word in cases:
            with self.subTest(request=request), self.assertRaises(trade_plan.PlanRejected) as caught:
                trade_plan.build(self.broker, account_seq=7, account="a", settings={**SETTINGS, **change}, now=NOW,
                                 spent_today_krw=0, request=request)
            self.assertIn(word, caught.exception.reason)
        self.broker.warnings_list = [{"warningType": "INVESTMENT_WARNING", "startDate": "2026-11-01", "endDate": None}]
        with self.assertRaises(trade_plan.PlanRejected):
            trade_plan.build(self.broker, account_seq=7, account="a", settings=SETTINGS, now=NOW, spent_today_krw=0,
                             request=trade_plan.Request("BUY", "CLS", 1))

    def test_krx_tick_sizes(self):
        for price, kind, tick in ((1999, "STOCK", 1), (4999, "STOCK", 5), (19990, "STOCK", 10), (70000, "STOCK", 100),
                                  (600000, "STOCK", 1000), (9000, "ETF", 5), (1500, "ETF", 1)):
            with self.subTest(price=price):
                self.assertEqual(trade_plan.tick_size("KR", kind, Decimal(price)), Decimal(tick))

    # --- approval ----------------------------------------------------------------------------------------------

    def test_reply_parsing_needs_the_exact_first_line(self):
        for text, expected in (("승인 123456", ("approve", "123456")), ("OK 654321\n감사합니다", ("approve", "654321")),
                               ("취소합니다", ("decline", None)), ("승인해 주세요", None), ("123456", None),
                               ("좋아요 승인 123456", None), ("> 승인 123456", None)):
            with self.subTest(text=text):
                self.assertEqual(trade_mail.decide(text), expected)

    def test_totp_matches_the_rfc_vector_and_its_neighbour_steps_only(self):
        rfc = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"     # RFC 6238 SHA-1 key "12345678901234567890"
        self.assertEqual(totp.code_at(rfc, 59 // 30), "287082")
        at = datetime.fromtimestamp(59, tz=timezone.utc)
        self.assertEqual(totp.matching_step(rfc, "287082", at + timedelta(seconds=30)), 1)
        self.assertIsNone(totp.matching_step(rfc, "287082", at + timedelta(seconds=95)))

    # --- execution ---------------------------------------------------------------------------------------------

    def test_approved_sell_then_rebuy_runs_in_order_and_reports(self):
        self.assertEqual(self._propose()["status"], "confirm_sent")
        self.assertEqual(trade_executor.run(self.conn, self.deps), [])     # no reply yet: nothing happens
        self.assertEqual(self.broker.placed, [])
        self._reply(None)
        lines = trade_executor.run(self.conn, self.deps)
        self.assertEqual([o["side"] for o in self.broker.placed], ["SELL", "BUY"])
        self.assertEqual([o["quantity"] for o in self.broker.placed], ["10", "10"])
        self.assertEqual(self.broker.placed[0]["price"], "285.23")          # 288.12 x 0.99 rounded down
        self.assertEqual(self.broker.placed[1]["price"], "291.01")          # 288.12 x 1.01 rounded up
        self.assertIn("done", lines[-1])
        self.assertIn("거래 결과", self.mails[-1][0])
        self.assertEqual(trade_store.plans(self.conn)[0]["status"], "done")

    def test_wrong_reused_or_early_codes_do_not_approve(self):
        self._propose()
        sent = self.clock.now
        self._reply("000000", message_id="bad")
        trade_executor.run(self.conn, self.deps)
        self.assertEqual(trade_store.plans(self.conn)[0]["status"], "confirm_sent")
        self.assertIn("코드가 맞지 않습니다", self.mails[-1][0])
        early = sent - timedelta(seconds=90)                    # a code from before the confirmation went out
        self.inbox.append(("m1", trade_mail.Reply("early", sent + timedelta(seconds=5), "approve",
                                                  totp.code_at(SECRET, totp.step_of(early)))))
        trade_executor.run(self.conn, self.deps)
        self.assertEqual(trade_store.plans(self.conn)[0]["status"], "confirm_sent")
        self._reply(None, message_id="good")
        trade_executor.run(self.conn, self.deps)
        self.assertEqual(trade_store.plans(self.conn)[0]["status"], "done")
        self.assertFalse(trade_store.use_totp_step(self.conn, totp.step_of(self.inbox[-1][1].sent_at)))   # no reuse

    def test_decline_and_expiry_place_nothing(self):
        self._propose()
        self._reply(None, message_id="no", decision="decline")
        trade_executor.run(self.conn, self.deps)
        self.assertEqual(trade_store.plans(self.conn)[0]["status"], "declined")
        self.clock.now += timedelta(minutes=1)
        self._propose()
        self.clock.now += timedelta(hours=2)
        trade_executor.run(self.conn, self.deps)
        self.assertEqual(trade_store.plans(self.conn)[0]["status"], "expired")
        self.assertEqual(self.broker.placed, [])

    def test_price_drift_after_approval_halts_before_any_order(self):
        self._propose()
        self.broker.price = Decimal("300")                     # +4.1%
        self._reply(None)
        trade_executor.run(self.conn, self.deps)
        self.assertEqual(self.broker.placed, [])
        self.assertEqual(trade_store.plans(self.conn)[0]["status"], "halted")
        self.assertIn("움직였습니다", self.mails[-1][1])

    def test_halt_switch_stops_the_order(self):
        self._propose()
        trade_store.set_state(self.conn, "halted", "1")
        self._reply(None)
        trade_executor.run(self.conn, self.deps)
        self.assertEqual(self.broker.placed, [])
        self.assertEqual(trade_store.plans(self.conn)[0]["status"], "halted")

    def test_lost_answer_is_resent_with_the_same_key_inside_the_window(self):
        self._propose()
        self.broker.outcomes = [TossError(0, "network-error"), TossError(0, "network-error"), "fill", "fill"]
        self._reply(None)
        trade_executor.run(self.conn, self.deps)
        self.assertEqual(trade_store.plans(self.conn)[0]["status"], "unknown")
        self.assertIn("확인하지 못했습니다", self.mails[-1][1])
        self.clock.now += timedelta(minutes=5)
        trade_executor.run(self.conn, self.deps)
        keys = [o["clientOrderId"] for o in self.broker.placed]
        self.assertEqual(keys[:3], [keys[0]] * 3)              # the sell, never under a new key
        self.assertEqual(trade_store.plans(self.conn)[0]["status"], "done")

    def test_lost_answer_after_the_window_is_matched_not_resent(self):
        self._propose()
        self.broker.outcomes = [TossError(0, "network-error"), TossError(0, "network-error")]
        self._reply(None)
        trade_executor.run(self.conn, self.deps)
        self.clock.now += timedelta(minutes=12)
        trade_executor.run(self.conn, self.deps)               # nothing listed: the order never reached Toss
        self.assertEqual(len(self.broker.placed), 2)
        self.assertEqual(trade_store.plans(self.conn)[0]["status"], "halted")

    def test_rejected_sell_stops_the_rebuy(self):
        self._propose()
        self.broker.outcomes = [TossError(422, "stock-restricted")]
        self._reply(None)
        trade_executor.run(self.conn, self.deps)
        self.assertEqual([o["side"] for o in self.broker.placed], ["SELL"])
        self.assertEqual(trade_store.plans(self.conn)[0]["status"], "halted")

    def test_unfilled_sell_waits_and_the_rebuy_waits_with_it(self):
        self._propose()
        self.broker.outcomes = ["open"]
        self._reply(None)
        lines = trade_executor.run(self.conn, self.deps)
        self.assertIn("waiting for a fill", lines[-1])
        self.assertEqual(len(self.broker.placed), 1)

    def test_buy_back_that_cannot_go_in_the_same_session_halts_with_the_cash_kept(self):
        self._propose()
        self._reply(None)
        place = self.broker.place
        def sell_then_vi(account_seq, body):
            self.broker.warnings_list = [{"warningType": "VI_DYNAMIC", "startDate": None, "endDate": None}]
            return place(account_seq, body)
        self.broker.place = sell_then_vi
        self.assertIn("waiting", trade_executor.run(self.conn, self.deps)[-1])
        self.clock.now += timedelta(hours=7)
        trade_executor.run(self.conn, self.deps)
        self.assertEqual([o["side"] for o in self.broker.placed], ["SELL"])
        self.assertEqual(trade_store.plans(self.conn)[0]["status"], "halted")
        self.assertIn("현금으로 남아", self.mails[-1][1])

    # --- review findings (Codex, 2026-10-30) -----------------------------------------------------------------------

    def test_a_refused_resend_never_turns_into_a_fresh_send_after_the_window(self):
        self._propose()
        self.broker.outcomes = [TossError(0, "network-error"), TossError(429, "rate-limit-exceeded")]
        self._reply(None)
        trade_executor.run(self.conn, self.deps)
        first = trade_store.legs(self.conn, trade_store.plans(self.conn)[0]["plan_id"])[0]["first_submit_at"]
        self.clock.now += timedelta(minutes=15)
        trade_executor.run(self.conn, self.deps)
        self.assertEqual(len(self.broker.placed), 2)            # the send and one resend under the same key, no third
        legs = trade_store.legs(self.conn, trade_store.plans(self.conn)[0]["plan_id"])
        self.assertEqual((legs[0]["status"], legs[0]["first_submit_at"]), ("failed", first))

    def test_daily_limit_holds_across_plans_at_execution(self):
        self.deps.settings["max_daily_krw"] = 5_000_000
        buy = trade_plan.Request("BUY", "CLS", 10)
        self._propose(buy)
        self.broker.warnings_list = [{"warningType": "VI_DYNAMIC", "startDate": None, "endDate": None}]
        self._reply(None, message_id="a", thread="m1")
        trade_executor.run(self.conn, self.deps)                # approved, waiting: nothing spent yet
        self._propose(buy)                                     # so this one fits at planning time too
        self._reply(None, message_id="b", thread="m2")
        self.broker.warnings_list = []
        trade_executor.run(self.conn, self.deps)
        self.assertEqual(len(self.broker.placed), 1)
        self.assertEqual(sorted(p["status"] for p in trade_store.plans(self.conn)), ["done", "halted"])

    def test_an_earlier_manual_order_is_never_taken_for_smiths_lost_order(self):
        self.broker.listed = [{"orderId": "manual", "side": "SELL", "quantity": "10", "price": "285.23", "status": "FILLED",
                               "orderedAt": (NOW - timedelta(seconds=30)).isoformat(), "execution": {"filledQuantity": "10"}}]
        self._propose()
        self.broker.outcomes = [TossError(0, "network-error"), TossError(0, "network-error")]
        self._reply(None)
        trade_executor.run(self.conn, self.deps)
        self.clock.now += timedelta(minutes=12)
        trade_executor.run(self.conn, self.deps)
        self.assertEqual([o["side"] for o in self.broker.placed], ["SELL", "SELL"])     # no buy-back
        self.assertEqual(trade_store.plans(self.conn)[0]["status"], "halted")

    def test_no_resend_once_trading_is_switched_off(self):
        self._propose()
        self.broker.outcomes = [TossError(0, "network-error"), TossError(0, "network-error")]
        self._reply(None)
        trade_executor.run(self.conn, self.deps)
        self.deps.settings["enabled"] = False
        self.clock.now += timedelta(minutes=3)
        trade_executor.run(self.conn, self.deps)
        self.assertEqual(len(self.broker.placed), 2)

    def test_a_plan_stays_on_its_own_account(self):
        self._propose()
        self.deps.account = "toss-ffffffff"
        self._reply(None)
        trade_executor.run(self.conn, self.deps)
        self.assertEqual(self.broker.placed, [])
        self.assertEqual(trade_store.plans(self.conn)[0]["status"], "halted")

    def test_buy_back_never_goes_into_the_next_session(self):
        self._propose()
        self.broker.outcomes = ["open"]
        self._reply(None)
        trade_executor.run(self.conn, self.deps)
        order = self.broker.orders_by_id["o1"]
        order.update(status="FILLED", execution={**order["execution"], "filledQuantity": "10", "averageFilledPrice": "285.23"})
        self.clock.now = NOW + timedelta(hours=24)              # the next regular session
        trade_executor.run(self.conn, self.deps)
        self.assertEqual([o["side"] for o in self.broker.placed], ["SELL"])
        self.assertEqual(trade_store.plans(self.conn)[0]["status"], "halted")

    def test_an_unreadable_success_answer_is_unknown_not_rejected(self):
        self._propose()
        self.broker.outcomes = [TossError(200, "invalid-response"), TossError(200, "invalid-response")]
        self._reply(None)
        trade_executor.run(self.conn, self.deps)
        self.assertEqual(trade_store.plans(self.conn)[0]["status"], "unknown")

    def test_only_a_brokerage_account_can_trade(self):
        from smith.toss_sync import account_label
        from smith.trade_command import resolve_account

        class Accounts:
            def accounts(self):
                return [{"accountSeq": 1, "accountType": "BROKERAGE"}, {"accountSeq": 2, "accountType": "ISA"}]
        self.assertEqual(resolve_account(Accounts(), {})[0], 1)
        with self.assertRaises(SystemExit):
            resolve_account(Accounts(), {"account": account_label(2)})

    def test_a_new_plan_is_refused_while_one_awaits_approval(self):
        self._propose(trade_plan.Request("BUY", "CLS", 10))
        self.clock.now += timedelta(seconds=40)
        result = self._propose(trade_plan.Request("BUY", "CLS", 1))
        self.assertEqual(result["status"], "rejected")
        self.assertIn("승인을 기다리는 계획", result["reason"])
        self.assertEqual(len(trade_store.plans(self.conn)), 1)

    def test_a_send_whose_leg_changed_meanwhile_is_not_made(self):
        self._propose()
        plan = trade_store.plans(self.conn)[0]
        leg = plan["legs"][0] | {"quantity": "10", "limit_price": "285.23", "first_submit_at": NOW.isoformat()}
        self.assertIsNotNone(trade_executor._place(self.conn, self.deps, plan, leg, resend=True))   # still pending
        self.assertEqual(self.broker.placed, [])

    def test_one_trade_pass_at_a_time_across_processes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            lock = Path(folder) / "trades.lock"
            with trade_store.exclusive(lock) as first, trade_store.exclusive(lock) as second:
                self.assertEqual((first, second), (True, False))
            with trade_store.exclusive(lock) as again:
                self.assertTrue(again)

    def test_halt_during_the_checks_stops_the_order(self):
        self._propose()
        self._reply(None)
        prices = self.broker.prices
        def halt_then_price(symbols):
            trade_store.set_state(self.conn, "halted", "1")
            return prices(symbols)
        self.broker.prices = halt_then_price
        trade_executor.run(self.conn, self.deps)
        self.assertEqual(self.broker.placed, [])
        self.assertEqual(trade_store.plans(self.conn)[0]["status"], "halted")

    def test_partly_filled_sell_stops_the_buy_back(self):
        self._propose()
        self.broker.outcomes = ["open"]
        self._reply(None)
        trade_executor.run(self.conn, self.deps)
        order = self.broker.orders_by_id["o1"]
        order.update(status="CANCELED", execution={**order["execution"], "filledQuantity": "3", "averageFilledPrice": "285.23"})
        self.clock.now += timedelta(minutes=15)
        trade_executor.run(self.conn, self.deps)
        self.assertEqual([o["side"] for o in self.broker.placed], ["SELL"])
        self.assertEqual(trade_store.plans(self.conn)[0]["status"], "halted")
        self.assertIn("3주만", self.mails[-1][1])

    def test_no_resend_across_the_kst_date(self):
        self.clock.now = NOW.replace(hour=23, minute=55) - timedelta(days=1)    # 23:55 KST, inside the session
        self._propose()
        self.broker.outcomes = [TossError(0, "network-error"), TossError(0, "network-error")]
        self._reply(None)
        trade_executor.run(self.conn, self.deps)
        self.clock.now += timedelta(minutes=6)                  # 00:02 the next KST day, still inside 9 minutes
        trade_executor.run(self.conn, self.deps)
        self.assertEqual(len(self.broker.placed), 2)

    def test_an_approval_does_not_run_a_plan_retired_meanwhile(self):
        self._propose()
        self._reply(None)
        replies = self.deps.replies
        def retire_then_read(thread, after, seen):
            self.conn.execute("UPDATE trade_plans SET status = 'expired', note = 'superseded'")
            return replies(thread, after, seen)
        self.deps.replies = retire_then_read
        trade_executor.run(self.conn, self.deps)
        self.assertEqual(self.broker.placed, [])

    def test_a_resend_is_checked_at_its_stored_price(self):
        buy = trade_plan.Request("BUY", "CLS", 10)
        self._propose(buy)
        self.broker.outcomes = [TossError(0, "network-error"), TossError(0, "network-error")]
        self._reply(None)
        trade_executor.run(self.conn, self.deps)
        self.broker.price = Decimal("282.36")                  # a fresh price would fit a lower limit ...
        self.deps.settings["max_order_krw"] = 3_600_000         # ... the stored $291.01 x 10 x 1240 does not
        self.clock.now += timedelta(minutes=3)
        trade_executor.run(self.conn, self.deps)
        self.assertEqual(len(self.broker.placed), 2)

    def test_no_resend_when_the_checks_run_past_midnight(self):
        self.clock.now = NOW.replace(hour=23, minute=58) - timedelta(days=1)
        self._propose()
        self.broker.outcomes = [TossError(0, "network-error"), TossError(0, "network-error")]
        self._reply(None)
        trade_executor.run(self.conn, self.deps)
        self.clock.now = NOW.replace(hour=23, minute=59, second=50) - timedelta(days=1)
        prices = self.broker.prices
        def slow_prices(symbols):
            self.clock.now += timedelta(seconds=15)             # 00:00:05 when the checks end
            return prices(symbols)
        self.broker.prices = slow_prices
        trade_executor.run(self.conn, self.deps)
        self.assertEqual(len(self.broker.placed), 2)

    def test_a_plan_that_expires_during_the_checks_is_not_sent(self):
        self._propose()
        expires = datetime.fromisoformat(trade_store.plans(self.conn)[0]["expires_at"])
        self._reply(None, at=expires - timedelta(seconds=40))
        self.clock.now = expires - timedelta(seconds=1)
        prices = self.broker.prices
        def slow_prices(symbols):
            self.clock.now += timedelta(seconds=5)
            return prices(symbols)
        self.broker.prices = slow_prices
        trade_executor.run(self.conn, self.deps)
        self.assertEqual(self.broker.placed, [])
        self.assertEqual(trade_store.plans(self.conn)[0]["status"], "expired")

    def test_a_resend_respects_a_lowered_daily_limit(self):
        self._propose(trade_plan.Request("BUY", "CLS", 10))
        self.broker.outcomes = [TossError(0, "network-error"), TossError(0, "network-error")]
        self._reply(None)
        trade_executor.run(self.conn, self.deps)
        self.deps.settings["max_daily_krw"] = 3_000_000
        self.clock.now += timedelta(minutes=3)
        trade_executor.run(self.conn, self.deps)
        self.assertEqual(len(self.broker.placed), 2)

    def test_a_code_from_any_other_sent_mail_is_refused(self):
        self._propose(trade_plan.Request("BUY", "CLS", 1))
        self.clock.now += timedelta(hours=2)                    # A expires on its own
        trade_executor.run(self.conn, self.deps)
        self._propose(trade_plan.Request("BUY", "CLS", 10))
        at = self.clock.now + timedelta(seconds=30)
        code = totp.code_at(SECRET, totp.step_of(at))
        self.inbox.append(("m1", trade_mail.Reply("late-for-a", at, "approve", code)))     # sent to the old thread
        self.inbox.append(("m2", trade_mail.Reply("copy", at + timedelta(seconds=4), "approve", code)))
        self.clock.now = at + timedelta(seconds=20)
        trade_executor.run(self.conn, self.deps)
        self.assertEqual(self.broker.placed, [])
        self.assertIn("복사 방지", self.mails[-1][1])

    def test_a_rate_limited_first_send_does_not_outlive_the_plan(self):
        self._propose()
        expires = datetime.fromisoformat(trade_store.plans(self.conn)[0]["expires_at"])
        self.broker.outcomes = [TossError(429, "rate-limit-exceeded")]
        self._reply(None, at=expires - timedelta(minutes=2))
        trade_executor.run(self.conn, self.deps)
        self.clock.now = expires + timedelta(minutes=14)
        trade_executor.run(self.conn, self.deps)
        self.assertEqual(len(self.broker.placed), 1)
        self.assertEqual(trade_store.plans(self.conn)[0]["status"], "expired")

    def test_an_approval_not_yet_searchable_waits_instead_of_failing(self):
        self._propose()
        self._reply(None)
        search = self.deps.sent_with
        self.deps.sent_with = lambda text, after, before: []      # Gmail has not indexed the reply yet
        self.assertIn("not searchable", trade_executor.run(self.conn, self.deps)[-1])
        self.deps.sent_with = search
        trade_executor.run(self.conn, self.deps)
        self.assertEqual(trade_store.plans(self.conn)[0]["status"], "done")

    def test_sent_mail_search_asks_for_ids_of_one_number_only(self):
        from smith import gmail
        urls = []
        ids = gmail.sent_containing("t", "123456", after=10, before=20,
                                    getter=lambda url, headers: urls.append(url) or (200, {"messages": [{"id": "a1"}]}))
        self.assertEqual(ids, ["a1"])
        self.assertIn("messages?q=in%3Aanywhere%20from%3Ame%20after%3A10%20before%3A20%20%22123456%22", urls[0])
        with self.assertRaises(gmail.MailError):
            gmail.sent_containing("t", "12 OR x", after=1, before=2, getter=lambda *a: (200, {}))

    def test_trading_settings_are_validated(self):
        _check_trading({"enabled": True, "account": "toss-0123abcd"})
        for bad in ({"enabled": "yes"}, {"account": "123-45"}, {"max_order_krw": 200_000_000}, {"drift_halt_pct": 0},
                    {"max_order_krw": 5_000_000, "max_daily_krw": 1_000_000}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                _check_trading(bad)


if __name__ == "__main__":
    unittest.main()
