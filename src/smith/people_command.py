"""`smith people`: the household's dates (FR-32) — check the file, list the coming year, show today's reminders."""
import argparse
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from smith import people
from smith.config import local_time


def add_parser(sub: Any, default_db: Path) -> None:
    command = sub.add_parser("people", help="Family and friends' dates (data/people.json next to the ledger)")
    command.add_argument("action", choices=("check", "list", "today", "path", "add", "date"),
                         help="check: validate the file; list: the coming occurrences; today: what today's mail would "
                              "remind; path: the file; add ID: a person; date ID: an anniversary, occasion or expiry")
    command.add_argument("id", nargs="?", help="add/date: a short id (lowercase, digits, dashes), such as father")
    command.add_argument("--label", help="add/date: how the mail names it, a relation word such as 아버지 (never a full name)")
    command.add_argument("--relation", default="", help="add: father, mother, spouse, sister, nephew, father-in-law, ...")
    command.add_argument("--birth", help="add: solar birthday YYYY-MM-DD or MM-DD")
    command.add_argument("--lunar", help="add: lunar birthday as M-D (the day celebrated); date: the month-day is lunar",
                         nargs="?", const=True)
    command.add_argument("--year", type=int, help="add: birth year when --lunar is given; date: the first year, for 주년")
    command.add_argument("--kind", choices=people.DATE_KINDS, help="date: anniversary, occasion or expiry")
    command.add_argument("--on", help="date: MM-DD for a yearly date, YYYY-MM-DD for an expiry")
    command.add_argument("--remind", help="date: days before, comma-separated, such as 30,7")
    command.add_argument("--person", help="date: the person it belongs to (an id from people)")
    command.add_argument("--days", type=int, default=people.WINDOW_DAYS, help="list: window in days (default 400)")
    command.add_argument("--as-of", type=date.fromisoformat, help="list/today: YYYY-MM-DD (default: today)")
    command.add_argument("--db", type=Path, default=default_db)


def run(args: argparse.Namespace) -> int:
    file = people.path(args.db)
    if args.action == "path":
        print(file)
        return 0
    try:
        if args.action in ("add", "date"):
            return _edit(args, file)
        book = people.load(file)
    except people.PeopleError as error:
        print(f"people.json problem: {error}")
        return 2
    today = args.as_of or local_time(datetime.now(timezone.utc)).date()
    if args.action == "check":
        print(f"{file}: {len(book.people)} people, {len(book.dates)} dates (occasions included). OK.")
        for person in book.people:
            if person.birth is not None and person.birth.year is None:
                print(f"  note: {person.label} ({person.id}) has no birth year, so no age or milestone is computed.")
        for item in book.dates:
            if item.kind == "anniversary" and item.year is None:
                print(f"  note: {item.label} ({item.id}) has no first year, so no 주년 is computed.")
        return 0
    if args.action == "today":
        found = people.reminders(book, today)
        print(f"{today.isoformat()} reminders: {len(found)}")
        for item in found:
            print(f"  D-{item.days_left} · {_line(item.occurrence, today)}")
        return 0
    rows = people.occurrences(book, today, args.days)
    print(f"{len(rows)} occurrences from {today.isoformat()} over {args.days} days")
    for occurrence in rows:
        print(f"  {_line(occurrence, today)}")
    return 0


def _line(occurrence: people.Occurrence, today: date) -> str:
    left = (occurrence.on - today).days
    parts = [f"{occurrence.on.isoformat()} ({occurrence.on.strftime('%a')})", f"D-{left}", occurrence.label]
    if occurrence.milestone:
        parts.append(f"★ {occurrence.milestone}")
    if occurrence.years is not None:
        parts.append(f"{occurrence.years}주년" if occurrence.kind == "anniversary" else f"만 {occurrence.years}세")
    if occurrence.note:
        parts.append(occurrence.note)
    parts.append("알림 " + "·".join(f"D-{d}" for d in occurrence.remind_days))
    return " · ".join(parts)


def _edit(args: argparse.Namespace, file: Path) -> int:
    if not args.id or not args.label:
        print("Give an id and --label, for example: smith people add father --label 아버지 --relation father --lunar 9-4 --year 1960")
        return 2
    raw = people.read_raw(file)
    if args.action == "add":
        entry = _person_entry(args)
        raw.setdefault("people", []).append(entry)
    else:
        entry = _date_entry(args)
        raw.setdefault("dates", []).append(entry)
    book = people.save(file, raw)
    print(f"Saved {args.id} to {file} ({len(book.people)} people, {len(book.dates)} dates).")
    return 0


def _person_entry(args: argparse.Namespace) -> dict[str, Any]:
    entry: dict[str, Any] = {"id": args.id, "label": args.label, "relation": args.relation}
    if args.lunar and args.lunar is not True:
        month, day = _month_day(args.lunar)
        entry["birth"] = {"calendar": "lunar", "year": args.year, "month": month, "day": day}
    elif args.birth:
        parts = args.birth.split("-")
        if len(parts) == 3:
            when = date.fromisoformat(args.birth)
            entry["birth"] = {"calendar": "solar", "year": when.year, "month": when.month, "day": when.day}
        else:
            month, day = _month_day(args.birth)
            entry["birth"] = {"calendar": "solar", "year": args.year, "month": month, "day": day}
    return entry


def _date_entry(args: argparse.Namespace) -> dict[str, Any]:
    if not args.kind or not args.on:
        raise people.PeopleError("date needs --kind and --on (MM-DD, or YYYY-MM-DD for an expiry)")
    entry: dict[str, Any] = {"id": args.id, "label": args.label, "kind": args.kind}
    if args.kind == "expiry":
        entry["date"] = args.on
    else:
        entry["month"], entry["day"] = _month_day(args.on)
        if args.year is not None:
            entry["year"] = args.year
        if args.lunar:
            entry["lunar"] = True
    if args.remind:
        try:
            entry["remind_days"] = [int(d) for d in args.remind.split(",")]
        except ValueError:
            raise people.PeopleError("--remind takes whole days, such as 30,7") from None
    if args.person:
        entry["person_id"] = args.person
    return entry


def _month_day(text: str) -> tuple[int, int]:
    try:
        month, day = (int(p) for p in text.split("-"))
    except ValueError:
        raise people.PeopleError(f"expected M-D, got {text!r}") from None
    return month, day
