"""`smith evening`: the evening "내일 준비" mail (daily design §16): preview without sending, the model input, a
manual send, recent runs; and the scheduled step `smith report run-due` calls."""
import argparse
import json
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from smith import evening, evening_html, evening_send, korean, netflix_picks, talk
from smith.config import load_config, local_time

DEFAULT_LOCAL_CONFIG = Path("config/smith.local.toml")
PREVIEW_DIR = Path("reports")   # Ignored by Git, like the other previews.
PREPARE_AHEAD = timedelta(hours=1)


def add_parser(sub: Any, default_db: Path) -> None:
    command = sub.add_parser("evening", help="The evening 내일 준비 mail: preview (never sends), model inputs, send, status")
    command.add_argument("action", choices=("preview", "inputs", "send", "status"),
                         help="preview: build tomorrow's mail and write the HTML to reports/; inputs: the writer's payload "
                              "as JSON (no model call); send: send tonight's mail now (manual run, with the model); status: recent runs")
    command.add_argument("--as-of", type=date.fromisoformat,
                         help="the evening, YYYY-MM-DD (default: today in the household timezone); the mail is about the next day")
    command.add_argument("--model", action="store_true",
                         help="preview/send: also run the talk research and the writer (headless model calls; audited in the ledger)")
    command.add_argument("--force", action="store_true", help="send: send again although tonight's mail already went out")
    command.add_argument("--out", type=Path, help="preview: where to write the HTML (default reports/evening-preview-<day>.html)")
    command.add_argument("--config", type=Path, default=DEFAULT_LOCAL_CONFIG)
    command.add_argument("--db", type=Path, default=default_db)


def run(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except (OSError, ValueError, UnicodeDecodeError):
        print(f"Missing setup: local config missing or invalid: {args.config}")
        return 2
    now = datetime.now(timezone.utc)
    tonight = args.as_of or local_time(now, config["app"]["timezone"]).date()
    day = tonight + timedelta(days=1)
    if args.action == "status":
        from smith import ledger

        with closing(ledger.connect_read_only(args.db)) as conn:
            print("\n".join(evening_send.describe(conn)))
        return 0
    if args.action == "send":
        return _send(args.db, config, now=now, day=day, with_model=args.model, force=args.force)
    data = evening_send.build(args.db, config, now=now, day=day, modeler=_with_model if args.model else None)
    partner = evening_send.settings(config)["partner"]
    if args.action == "inputs":
        from smith import evening_writer

        print(json.dumps(evening_writer.payload(data, partner=partner, partner_memory=[], partner_notes=[], recent_lines=[]),
                         ensure_ascii=False, indent=2, default=str))
        return 0
    subject, html, text = evening_html.render(data, partner=partner, inline=True)
    out = args.out or PREVIEW_DIR / f"evening-preview-{day.isoformat()}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"Subject: {subject}")
    print(f"HTML: {out} ({len(html.encode('utf-8')) // 1024} KB) — a preview; nothing was sent.")
    print(text)
    if data["failures"]:
        print(f"Sources not read: {data['failures']}")
        return 1
    return 0


def run_due(db: Path, config: dict[str, Any], *, now: datetime) -> list[str]:
    """For `smith report run-due`: in the hour before tonight's slot the talk research runs ahead (reused for six
    hours), then the mail goes out when due. Returns log lines."""
    from smith import credentials, headless

    lines: list[str] = []
    tonight = local_time(now, config["app"]["timezone"]).date()
    day = tonight + timedelta(days=1)
    slot = evening_send.slot_for(config, tonight)
    kind = talk.kind_for(day, evening_send.settings(config)["kinds"])
    if slot - PREPARE_AHEAD <= now < slot and evening_send.is_mail_evening(config, tonight):
        try:
            if kind in talk.RESEARCHED:
                found = talk.research_and_store(db, now=now, day=day, kind=kind, avoid=_avoid(db, now),
                                                secrets=credentials.all_secrets(), executable=headless.find_claude())
                if found["outcome"] != "reused":
                    lines.append(f"evening talk prepared for {day.isoformat()} ({kind}): {found['outcome']}, {len(found['items'])} item(s)")
            if evening_send.settings(config)["netflix"]:
                shown = netflix_picks.recent(db, now=now, product=evening.PRODUCT)
                picks = netflix_picks.research_and_store(db, now=now, day=day, avoid=shown["titles"], avoid_ids=shown["ids"],
                                                         secrets=credentials.all_secrets(), executable=headless.find_claude())
                if picks["outcome"] != "reused":
                    lines.append(f"evening netflix picks prepared for {day.isoformat()}: {picks['outcome']}, {len(picks['items'])} item(s)")
        except headless.HeadlessError as error:
            lines.append(f"evening research not prepared ({error})")
    lines += evening_send.run_due(db, config, now=now, recipient=config.get("mail", {}).get("recipient"),
                                  credentials=credentials.load_gmail(), modeler=_with_model)
    return lines


def _avoid(db: Path, now: datetime) -> list[str]:
    return [r["title"] for r in talk.recent(db, now=now, product=evening.PRODUCT) if r["title"]]


def _send(db: Path, config: dict[str, Any], *, now: datetime, day: date, with_model: bool, force: bool) -> int:
    from smith import credentials

    result = evening_send.publish(db, config, now=now, day=day, trigger="manual", recipient=config.get("mail", {}).get("recipient"),
                                  credentials=credentials.load_gmail(), modeler=_with_model if with_model else None, force=force)
    if result["status"] == "skipped" and result.get("error_code") == "already-sent":
        print("Tonight's mail already went out; add --force to send another one.")
    print(f"{result['status']}: {result.get('subject') or ''}" + (f" ({result['error_code']})" if result.get("error_code") else ""))
    return 0 if result["status"] == "sent" else 1


def _with_model(db: Path, config: dict[str, Any], data: dict[str, Any], now: datetime, day: date) -> None:
    """The talk research (current kinds only) and the writer; a failure of either leaves the code's own lines in place
    and is named in the mail's system notes."""
    from smith import credentials, evening_writer, headless

    secrets = credentials.all_secrets()
    try:
        executable = headless.find_claude()
    except headless.HeadlessError as error:
        print(f"Model step skipped ({error}).")
        data["failures"]["writer"] = "no-claude"
        return
    recent = data.get("talk_recent") or talk.recent(db, now=now, product=evening.PRODUCT)
    if data["talk_kind"] in talk.RESEARCHED:
        found = talk.research_and_store(db, now=now, day=day, kind=data["talk_kind"], avoid=[r["title"] for r in recent if r["title"]],
                                        secrets=secrets, executable=executable)
        data["talk_candidates"] = found["items"]
        print(f"Talk ({data['talk_kind']}): {found['outcome']}, {len(found['items'])} item(s)"
              + (f" ({found['error_code']})" if found["error_code"] else ""))
        if not found["items"]:
            # Nothing verified: tonight is a warm evening instead, and the notes say why (never an unverified trend).
            data["failures"]["talk"] = str(found["error_code"] or "no-verified-item")[:40]
            data["talk_kind"] = talk.FALLBACK
    if evening_send.settings(config)["netflix"]:
        shown = netflix_picks.recent(db, now=now, product=evening.PRODUCT)
        picks = netflix_picks.research_and_store(db, now=now, day=day, avoid=shown["titles"], avoid_ids=shown["ids"],
                                                 secrets=secrets, executable=executable)
        data["netflix"] = picks["items"]
        print(f"Netflix picks: {picks['outcome']}, {len(picks['items'])} item(s)" + (f" ({picks['error_code']})" if picks["error_code"] else ""))
        if not picks["items"]:
            data["failures"]["netflix"] = str(picks["error_code"] or "no-verified-pick")[:40]
    written = evening_writer.write_and_record(db, data, now=now, secrets=secrets, partner=evening_send.settings(config)["partner"],
                                              recent_lines=[r["line"] for r in recent if r["line"]], executable=executable)
    data["prose"] = written["prose"]
    print(f"Writer: {'ok' if written['prose'] is not None else 'failed'}"
          + (f" ({written['error_code']})" if written["error_code"] else "")
          + (f"; dropped: {'; '.join(written['problems'])}" if written["problems"] else "")
          + (f"; {korean.summary({'tidied': written['prose'].get('korean_fixes') or []})}" if written["prose"] else ""))
    if written["prose"] is None:
        data["failures"]["writer"] = str(written["error_code"])[:40]
