"""The evening "내일 준비" mail's data (daily design §16, user request 2026-10-27): what to get ready tonight for
tomorrow, and one small thing to bring to the partner.

The mail is about tomorrow and read tonight. `collect` is the morning mail's reader pointed at tomorrow
(`daily.collect(horizon="tomorrow")`: tomorrow's calendar, tasks, dates and holidays, the weather outlook read now);
`assemble` adds the evening's own parts to the morning's data dict: the checklist for tonight, decided by rules here
(`prep`), and the talk kind of the evening (`talk.kind_for`). A model may later phrase the lines and write the talk
item; the mail stands on the code's lines without it.
"""
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from smith import agenda, daily, talk, weather
from smith.config import local_time

PRODUCT = "evening"
MAX_PREP = 8
# Tomorrow's hours that matter for clothes and an umbrella (user decision 2026-10-27): the commute on a working day,
# the outing hours on a rest day. Inclusive hour spans; `[evening] commute_hours` / `rest_hours` change them.
COMMUTE_HOURS = (("출근길", 7, 9), ("퇴근길", 18, 20))
REST_HOURS = (("오전 외출", 10, 12), ("오후 외출", 14, 18))
# What each rule item means tonight (weather.advise's wording -> the evening action).
ITEM_ACTIONS = {"우산": "우산을 현관에 꺼내 두세요", "외투(아침·저녁 쌀쌀)": "내일 입을 외투를 꺼내 두세요",
                "물·양산": "물병과 양산을 챙겨 두세요", "마스크": "마스크를 가방에 넣어 두세요",
                "마스크(기관지 기준)": "마스크를 가방에 넣어 두세요 (기관지 기준)"}


def target_day(config: dict[str, Any], now: datetime) -> date:
    """Tomorrow in the household zone: the day an evening mail sent at `now` is about."""
    return local_time(now, config["app"]["timezone"]).date() + timedelta(days=1)


def collect(db: Path, config: dict[str, Any], *, now: datetime, day: date) -> daily.Inputs:
    """Tomorrow's sources (`day` is tomorrow), each failing on its own as in the morning mail."""
    return daily.collect(db, config, now=now, day=day, horizon="tomorrow")


def assemble(inputs: daily.Inputs, *, day: date, tz: str, now: datetime, airways_sensitive: bool,
             kinds: tuple[str, ...] = talk.ROTATION, commute_hours: tuple[tuple[str, int, int], ...] = COMMUTE_HOURS,
             rest_hours: tuple[tuple[str, int, int], ...] = REST_HOURS) -> dict[str, Any]:
    """The morning data dict for tomorrow plus the evening's parts: `evening_of` (tonight's date), `prep` (tonight's
    checklist), `talk_kind` and an empty `talk` (filled by the talk research and the writer)."""
    data = daily.assemble(inputs, day=day, tz=tz, now=now, airways_sensitive=airways_sensitive)
    data.update({"product": PRODUCT, "evening_of": day - timedelta(days=1), "talk_kind": talk.kind_for(day, kinds),
                 "talk_candidates": [], "talk": None, "thoughts": [], "history_events": [], "history_pick": None,
                 "netflix": None,    # None: not asked for tonight; a list (maybe empty) once the picks ran.
                 "sleep": None})     # sleep.Plan, added by `add_sleep` when the corner is on.
    data["commute"] = windows(data, rest_hours if data["rest_day"] else commute_hours)
    data["prep"] = prep(data)
    return data


def add_sleep(data: dict[str, Any], *, health_folder: Path, now: datetime) -> None:
    """Tonight's sleep corner (daily design §16.7): the night outlook and the client's recent nights (local Fitbit
    days, never fetched here) through the rules of `sleep.plan`."""
    from smith import fitbit, sleep

    block = data["weather"]
    report = None if block is None else block["report"]
    state = fitbit.sync_state(health_folder)
    # The sleep source's own last success: another source's success does not make old nights fresh (review
    # 2026-10-27). A file from before per-source times falls back to the sync time.
    stamp = (state.get("sources") or {}).get("sleep") or (state.get("synced_at") if "sources" not in state else None)
    try:
        synced = datetime.fromisoformat(str(stamp)) if stamp else None
    except ValueError:
        synced = None
    timed = [e for e in data["events"] if not e.all_day]
    air = None if report is None else report.air
    data["sleep"] = sleep.plan(night=weather.night(report), now_obs=None if report is None else report.now,
                               days=fitbit.load_days(health_folder), synced_at=synced, tonight=data["evening_of"], now=now,
                               first_event=timed[0].start if timed else None, rest_day=data["rest_day"],
                               airways_sensitive=bool(data.get("airways_sensitive")),
                               air_bad=air is not None and max(air.pm10_grade or 0, air.pm25_grade or 0) >= 3,
                               sleep_failed="sleep" in (state.get("failed") or []))


def windows(data: dict[str, Any], spans: tuple[tuple[str, int, int], ...]) -> list[weather.Window]:
    """Tomorrow's forecast over each span (the commute, or the outing hours on a rest day); spans without data drop."""
    block = data["weather"]
    forecast = None if block is None else block["report"].today
    return [w for w in (weather.window(forecast, label, start, end) for label, start, end in spans) if w is not None]


def prep(data: dict[str, Any]) -> list[dict[str, str]]:
    """Tonight's checklist for tomorrow, in the order a reader acts on it: what tomorrow is (a holiday), what to put by
    the door (weather), when the day starts (first timed event, bookings, overlaps), what is due, whose day it is, a
    finance date that falls tomorrow. Each item is {"kind", "text"}; at most MAX_PREP."""
    items: list[dict[str, str]] = []
    if data["holiday"]:
        items.append({"kind": "holiday", "text": f"내일은 {data['holiday']}입니다. 쉬는 날 기준으로 계획하세요"})
    block = data["weather"]
    if block is not None:
        advice, forecast = block["advice"], block["report"].today
        for caution in advice.cautions:
            if advice.severe and "외출 자제" in caution:
                items.append({"kind": "weather", "text": f"{caution.split(':')[0]}: 내일 일정과 이동 수단을 다시 확인하세요"})
        for item in advice.items:
            text = ITEM_ACTIONS.get(item, f"{item} 챙겨 두세요")
            if item.startswith("외투") and forecast is not None and forecast.tmin is not None:
                text += f" (아침 최저 {forecast.tmin:g}℃)"
            wet = next((w for w in data.get("commute") or [] if w.rain_from is not None), None)
            if item == "우산" and wet is not None:
                text += f" ({wet.label} {wet.rain_from:02d}시부터 비)"
            elif item == "우산" and forecast is not None and forecast.pop_max:
                text += f" (강수확률 {forecast.pop_max}%)"
            items.append({"kind": "weather", "text": text})
    items += _agenda_items(data)
    for reminder in sorted(data["reminders"], key=lambda r: r.days_left):
        label = reminder.occurrence.label
        if reminder.days_left == 0:
            items.append({"kind": "date", "text": f"내일 {label}입니다. 축하 메시지와 선물을 오늘 밤 준비해 두세요"})
        else:
            items.append({"kind": "date", "text": f"{label}까지 {reminder.days_left + 1}일 남았습니다. 선물이나 예약을 미리 알아보세요"})
    items += [{"kind": "finance", "text": f"내일 {f['label']}"} for f in data["finance_dates"] if f["days"] == 0]
    return items[:MAX_PREP]


def _agenda_items(data: dict[str, Any]) -> list[dict[str, str]]:
    events: list[agenda.Event] = data["events"]
    items = []
    timed = [e for e in events if not e.all_day]
    if timed:
        first = timed[0]
        place = f" · {first.location}" if first.location else ""
        items.append({"kind": "event", "text": f"첫 일정 {first.start:%H:%M} {first.title}{place}: 알람과 나설 시간을 맞춰 두세요"})   # type: ignore[str-format]
    items += [{"kind": "booking", "text": f"예약 확인: {e.title}"} for e in events if e.event_type == "fromGmail"][:2]
    items += [{"kind": "event", "text": f"겹치는 일정: {a.title} / {b.title}. 오늘 밤 하나를 정리해 두세요"}
              for a, b in data["overlaps"]][:2]
    due = data["tasks"]["due_today"]
    if due:
        names = ", ".join(t.title for t in due[:3]) + (f" 외 {len(due) - 3}건" if len(due) > 3 else "")
        items.append({"kind": "task", "text": f"내일 기한 할 일: {names}"})
    return items
