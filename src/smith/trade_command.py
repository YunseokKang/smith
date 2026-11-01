"""`smith trade`: propose a trade, run the approval and execution pass, show status, halt or resume (trading design).

`propose` only builds a plan and mails the confirmation; nothing is ordered until the client replies with an
authenticator code and a run (`smith trade run` or the scheduled `report run-due`) finds it. `enroll` creates the
authenticator secret: the client runs it in their own terminal, because whoever sees the secret can approve trades.
"""
import argparse
import getpass
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smith import credentials, gmail, totp, trade_executor, trade_mail, trade_plan, trade_store
from smith.config import load_config, local_time
from smith.toss import TossError
from smith.toss_orders import OrderClient
from smith.toss_sync import account_label


def add_parser(sub: Any, default_db: Path) -> None:
    parser = sub.add_parser("trade", help="Approval-gated Toss orders: propose, run, status, halt, resume, enroll")
    parser.add_argument("action", choices=("propose", "run", "status", "halt", "resume", "enroll"))
    parser.add_argument("--sell", metavar="SYMBOL")
    parser.add_argument("--buy", metavar="SYMBOL")
    parser.add_argument("--qty", default=None, help="whole shares, or 'all' for a sell (default for a sell: all)")
    parser.add_argument("--rebuy", action="store_true", help="with --sell: buy the sold quantity back after the fill")
    parser.add_argument("--said", default="", help="the client's own words, quoted in the confirmation mail")
    parser.add_argument("--dry-run", action="store_true", help="propose: show the plan; store and mail nothing")
    parser.add_argument("--plan", help="status: one plan id or code")
    parser.add_argument("--db", type=Path, default=default_db, help="the financial ledger; trades live beside it")
    parser.add_argument("--config", type=Path, default=Path("config/smith.local.toml"))


def store_path(db: Path) -> Path:
    return db.parent / "trades.db"


def run(args: argparse.Namespace) -> int:
    if args.action == "enroll":
        return _enroll()
    config = load_config(args.config)
    settings = config.get("trading") or {}
    if args.action == "propose" and args.dry_run:
        return _propose(None, args, config)      # reads only: no trade ledger, no mail
    with closing(trade_store.connect(store_path(args.db))) as conn:
        if args.action == "status":
            return _status(conn, args.plan, config["app"]["timezone"])
        if args.action in ("halt", "resume"):
            return _halt(conn, args.action == "halt", settings)
        if args.action == "propose":
            return _propose(conn, args, config)
        for line in run_pass(conn, config, store_path(args.db)):
            print(line)
    return 0


def _propose(conn: Any, args: argparse.Namespace, config: dict[str, Any]) -> int:
    request = _request(args)
    if request is None:
        print("Give --sell SYMBOL [--qty N|all] [--rebuy] or --buy SYMBOL --qty N.")
        return 2
    settings = config.get("trading") or {}
    if not settings.get("enabled") and not args.dry_run:
        print("Trading is disabled ([trading] enabled = false). Use --dry-run to see a plan.")
        return 2
    client = _client()
    account_seq, label = resolve_account(client, settings)
    if args.dry_run:
        try:
            plan = trade_plan.build(client, account_seq=account_seq, account=label, request=request, settings=settings,
                                    now=datetime.now(timezone.utc), spent_today_krw=0)
        except trade_plan.PlanRejected as rejected:
            print(f"Rejected: {rejected.reason}")
            return 1
        print(trade_mail.subject(plan))
        print(trade_mail.confirmation(plan, config["app"]["timezone"])[0])
        return 0
    deps = _deps(client, account_seq, config, with_replies=False)
    result = trade_executor.propose(conn, deps, request, account=label)
    print(f"{result['status']}: {result.get('code', '')} {result.get('reason', '')}".rstrip())
    return 0 if result["status"] == "confirm_sent" else 1


def run_pass(conn: Any, config: dict[str, Any], store: Path) -> list[str]:
    """One approval-and-execution pass (also called by `report run-due`). Never sends an order for an unapproved plan."""
    if not trade_store.plans(conn, statuses=trade_store.OPEN_PLAN, limit=1):
        return []
    with trade_store.exclusive(store.with_suffix(".lock")) as held:
        if not held:
            return ["Trade run skipped: another run is in progress."]
        client = _client()
        account_seq, _ = resolve_account(client, config.get("trading") or {})
        deps = _deps(client, account_seq, config, with_replies=True)
        return trade_executor.run(conn, deps)


def _deps(client: OrderClient, account_seq: int, config: dict[str, Any], *, with_replies: bool) -> trade_executor.Deps:
    import time

    recipient = (config.get("mail") or {}).get("recipient")
    stored = credentials.load_gmail()
    if not recipient or stored is None:
        raise SystemExit("Trading needs [mail] recipient and `smith mail login --read`.")

    def mail(subject: str, text: str, html: str) -> str:
        return gmail.send(stored, recipient=recipient, subject=subject, html=html, text=text)

    deps = trade_executor.Deps(client=client, account_seq=account_seq, account=account_label(account_seq), mail=mail, clock=lambda: datetime.now(timezone.utc),
                 sleep=time.sleep, settings=config.get("trading") or {}, tz=config["app"]["timezone"],
                 totp_secret=credentials.load_trade_totp())
    if with_replies:
        token, scopes = gmail.access_token(stored)
        if gmail.READ_SCOPE in scopes:
            deps.replies = lambda thread, after, seen: trade_mail.replies(token, thread, recipient=recipient,
                                                                          after=after, seen=seen)
            deps.thread_of = lambda message_id: gmail.thread_of(token, message_id)
            deps.sent_with = lambda text, after, before: gmail.sent_containing(token, text, after=after, before=before)
    return deps


def resolve_account(client: OrderClient, settings: dict[str, Any]) -> tuple[int, str]:
    """The one allowed account: a brokerage account (never pension or ISA), the one `[trading] account` names (an
    opaque key from `smith toss sync`) or else the only one."""
    accounts = [a for a in client.accounts() or [] if isinstance(a, dict) and type(a.get("accountSeq")) is int
                and a.get("accountType") == "BROKERAGE"]
    wanted = settings.get("account")
    found = [a for a in accounts if not wanted or account_label(a["accountSeq"]) == wanted]
    if len(found) != 1:
        raise SystemExit("The trading account is not unique; set [trading] account to its key (toss-xxxxxxxx).")
    return found[0]["accountSeq"], account_label(found[0]["accountSeq"])


def _client() -> OrderClient:
    stored = credentials.load_toss_client()
    if stored is None:
        raise SystemExit("No Toss credentials; run `smith toss login`.")
    return OrderClient(*stored)


def _request(args: argparse.Namespace) -> trade_plan.Request | None:
    if bool(args.sell) == bool(args.buy):
        return None
    symbol = (args.sell or args.buy).strip().upper()
    if args.sell:
        quantity = None if args.qty in (None, "all") else _whole(args.qty)
        return trade_plan.Request("SELL", symbol, quantity, rebuy=args.rebuy, said=args.said)
    quantity = _whole(args.qty) if args.qty not in (None, "all") else None
    if quantity is None or args.rebuy:
        return None
    return trade_plan.Request("BUY", symbol, quantity, said=args.said)


def _whole(value: str) -> int:
    if not value.isdigit() or int(value) < 1:
        raise SystemExit("--qty must be a whole number of shares (or 'all' for a sell).")
    return int(value)


def _status(conn: Any, plan_id: str | None, tz: str) -> int:
    found = [trade_store.plan(conn, plan_id)] if plan_id else trade_store.plans(conn, limit=10)
    if trade_store.halted(conn):
        print("HALTED: no order will be sent until `smith trade resume`.")
    for plan in filter(None, found):
        made = local_time(datetime.fromisoformat(plan["created_at"]), tz)
        print(f"{plan['code']} {plan['status']:<15} {made:%m-%d %H:%M} {plan['plan']['symbol']} "
              f"{plan['plan']['side']}{' +rebuy' if plan['plan']['rebuy'] else ''} {plan['plan']['quantity']}"
              + (f"  ({plan['note']})" if plan["note"] else ""))
        for leg in plan["legs"]:
            print(f"    leg {leg['leg']} {leg['side']:<4} {leg['status']:<10} qty {leg['quantity'] or '-'} "
                  f"limit {leg['limit_price'] or '-'} filled {leg['filled_quantity'] or '-'} @ {leg['average_price'] or '-'}"
                  + (f" [{leg['error_code']}]" if leg["error_code"] else ""))
    return 0


def _halt(conn: Any, halt: bool, settings: dict[str, Any]) -> int:
    trade_store.set_state(conn, "halted", "1" if halt else "0")
    trade_store.event(conn, datetime.now(timezone.utc), None, "halt" if halt else "resume")
    if not halt:
        print("Resumed. Plans already halted stay halted; propose again if needed.")
        return 0
    canceled = 0
    open_legs = [(p, l) for p in trade_store.plans(conn, statuses=trade_store.OPEN_PLAN, limit=50)
                 for l in p["legs"] if l["status"] == "submitted" and l["order_id"]]
    if open_legs:
        client = _client()
        account_seq, _ = resolve_account(client, settings)
        for _, leg in open_legs:
            try:
                client.cancel(account_seq, leg["order_id"])
                canceled += 1
            except TossError as error:
                print(f"Cancel failed for {leg['client_order_id']}: {error.code}")
    print(f"Halted. Canceled {canceled} open Smith order(s); the next run records their final state and cancels any "
          "order whose send was still unconfirmed.")
    return 0


def _enroll() -> int:
    if credentials.load_trade_totp() and input("An authenticator is already enrolled. Replace it? (yes/no) ") != "yes":
        return 1
    secret = totp.new_secret()
    print("Add this key to your authenticator app (Google Authenticator: + > 설정 키 입력, 시간 기준).")
    print("Do this in your own terminal only; anyone who sees this key can approve trades.")
    print(f"  key: {' '.join(secret[i:i + 4] for i in range(0, len(secret), 4))}")
    print(f"  uri: {totp.provisioning_uri(secret)}")
    code = getpass.getpass("Enter the 6 digits the app shows now (hidden): ").strip()
    if totp.matching_step(secret, code, datetime.now(timezone.utc)) is None:
        print("The code did not match; nothing was saved. Check the phone's clock and try again.")
        return 1
    credentials.save_trade_totp(secret)
    print("Saved to the OS credential store.")
    return 0
