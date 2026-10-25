"""The daily-routine mail (FR-31) rendered in its own warm register (user review 2026-10-25): sand and cream surfaces,
a terracotta accent, a greeting instead of a status badge, and a traffic-light row for weather, dust and pollen. The
finance reports keep their institutional white-and-blue look; the two mails should feel like two rooms of one house.

Every line here comes from code, so the mail stands without a writer's prose; the prose only replaces the weather
sentence and adds gift ideas, news summaries and the sentence of the day. Each light carries a shape and a word beside
its color (dataviz status rule: never color alone).
"""
from datetime import timedelta
from html import escape
from typing import Any

from smith import agenda, people, weather
from smith.report_html import _plain

PREFIX = "[Smith][하루 일과]"
# Warm tokens. Ink pairs were chosen for >= 4.5:1 on the card surface (#fffdf9): INK 13.9, BODY 9.8, MUTED 4.9.
PAGE, CARD, HERO, HERO_DEEP = "#f6efe6", "#fffdf9", "#f3e6d6", "#e8d6c0"
INK, BODY, MUTED, HAIRLINE, ACCENT = "#2a211c", "#3d332d", "#7a6a5f", "#e9ddcd", "#b95b2a"
SANS = "Pretendard,Inter,'Malgun Gothic','Apple SD Gothic Neo',-apple-system,'Segoe UI',Roboto,sans-serif"
# Traffic lights (validated against the card surface 2026-10-25): color, shape, soft tint for chips. The word and the
# shape travel with the color, so 좋음/보통/나쁨 read without it.
LIGHTS = {"good": ("#2e9e44", "●", "#e4f3e7"), "warning": ("#e9a800", "▲", "#fbf0cc"), "serious": ("#e3542a", "■", "#fbe3d8"),
          "critical": ("#a51c1c", "■", "#f6d9d9"), "none": ("#a89a8e", "○", "#efe8df")}
PILLS = {"good": ("#e4f3e7", "#0f5f25"), "neutral": ("#efe8df", "#4f433b"), "warn": ("#fbf0cc", "#6e4f00"),
         "bad": ("#f6d9d9", "#7a1515")}
FOOTER = ("Smith는 조회 전용입니다: 캘린더·할 일을 바꾸지 않고, 메일을 전달하지 않습니다. 일정·기념일·뉴스에 대한 질문이나 조사 요청은 "
          "이 메일에 답장으로 주십시오. 완료 처리는 캘린더 앱에서 직접 하시면 다음 날 메일에서 사라집니다.")
WEEKDAY_SHORT = "월화수목금토일"


def render(data: dict[str, Any], *, inline: bool = False) -> tuple[str, str, str]:
    """(subject, html, text) for one day's mail. `inline` embeds the pictures as data URIs (the preview file); a sent
    mail references them by cid and carries them as attachments (`daily_art.images_for` + `daily_art.payload`)."""
    from smith import daily_art

    prose = data.get("prose") or {}
    images = daily_art.images_for(data)
    pictures = {cid: daily_art.source(cid, images, inline=inline) for cid in images}
    subject = _subject(data)
    cards = [card for card in (_history(data, prose), _calendar(data, prose), _anniversaries(data, prose), _news(data, prose),
                               _sentence(prose, pictures.get("mood")), _tasks(data), _holidays(data), _finance(data)) if card]
    footer_lines = [FOOTER]
    if data["failures"]:
        footer_lines.append("오늘 받지 못한 자료: " + ", ".join(f"{k}({v})" for k, v in sorted(data["failures"].items())))
    footer = ('<tr><td style="padding:4px 6px 12px">'
              + "".join(f'<div style="font-size:12px;line-height:1.6;color:{MUTED};margin-bottom:6px">{escape(line)}</div>'
                        for line in footer_lines) + "</td></tr>")
    html = _page(_hero(data, prose, pictures) + "".join(cards) + footer)
    return subject, html, _text(data, prose, subject)


def _subject(data: dict[str, Any]) -> str:
    day = data["day"]
    parts = [f"{PREFIX} {day:%m/%d} ({data['weekday']})"]
    if data["holiday"]:
        parts.append(data["holiday"])
    if data["weather"] is not None:
        advice = data["weather"]["advice"]
        parts.append(("⚠ " if advice.severe else "") + advice.headline)
    parts.append(f"일정 {len(data['events'])}건")
    if data["tasks"]["due_today"]:
        parts.append(f"할 일 {len(data['tasks']['due_today'])}건")
    top = _top_date(data)
    if top:
        parts.append(top)
    return " · ".join(parts)


def _top_date(data: dict[str, Any]) -> str | None:
    if data["reminders"]:
        item = min(data["reminders"], key=lambda r: r.days_left)
        return f"{item.occurrence.label} {'오늘' if item.days_left == 0 else f'D-{item.days_left}'}"
    if data["upcoming"]:
        item = data["upcoming"][0]
        return f"{item.label} D-{(item.on - data['day']).days}"
    return None


def _page(inner: str) -> str:
    return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>Smith 하루 일과</title></head>'
            f'<body style="margin:0;padding:0;background:{PAGE};font-family:{SANS};color:{INK};word-break:keep-all;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PAGE}">'
            f'<tr><td align="center" style="padding:16px 6px">'
            f'<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:100%;max-width:600px">'
            f'{inner}</table></td></tr></table></body></html>')


def _card(title: str, lead: str, inner: str, *, accent: bool = False) -> str:
    edge = f"border-left:4px solid {ACCENT};" if accent else ""
    head = (f'<div style="font-size:17px;font-weight:700;letter-spacing:-0.3px;color:{INK};margin:0 0 6px">{escape(title)}</div>'
            if title else "")
    lead_html = f'<div style="font-size:15px;line-height:1.6;color:{BODY};margin:0 0 10px">{lead}</div>' if lead else ""
    return (f'<tr><td style="padding:0 0 12px"><div style="background:{CARD};border:1px solid {HAIRLINE};{edge}border-radius:20px;'
            f'padding:18px 20px">{head}{lead_html}{inner}</div></td></tr>')


def _pill(text: str, kind: str = "neutral") -> str:
    background, ink = PILLS[kind]
    return (f'<span style="display:inline-block;background:{background};color:{ink};font-size:12px;font-weight:700;'
            f'border-radius:100px;padding:3px 10px;white-space:nowrap">{escape(text)}</span>')


def _light(signal: weather.Signal) -> str:
    color, shape, tint = LIGHTS[signal.level]
    detail = f'<div style="font-size:12px;color:{MUTED};margin-top:2px">{escape(signal.detail)}</div>' if signal.detail else ""
    return (f'<td style="width:33%;padding:4px;vertical-align:top"><div style="background:{tint};border-radius:16px;padding:12px 14px">'
            f'<div style="font-size:12px;font-weight:700;color:{MUTED}">{escape(signal.name)}</div>'
            f'<div style="font-size:16px;font-weight:700;color:{INK};margin-top:4px"><span style="color:{color};font-size:15px">'
            f'{shape}</span> {escape(signal.label)}</div>{detail}</div></td>')


def _hero(data: dict[str, Any], prose: dict[str, Any], pictures: dict[str, str | None] | None = None) -> str:
    pictures = pictures or {}
    day = data["day"]
    greeting = "편안한 휴일 아침입니다" if data["rest_day"] else "좋은 아침입니다"
    date_line = f"{day:%m월 %d일} {data['weekday']}요일" + (f" · {data['holiday']}" if data["holiday"] else "")
    block = data["weather"]
    if block is not None and block.get("label"):
        date_line += f" · {block['label']} 날씨 기준"
    if block is None:
        headline, line, lights_html, air = "날씨 자료를 오늘 받지 못했습니다", "", "", ""
    else:
        advice, report = block["advice"], block["report"]
        headline = prose.get("headline") or advice.headline
        line = prose.get("weather_line") or _weather_line(advice)
        lights = weather.signals(report, advice, airways_sensitive=bool(data.get("airways_sensitive")))
        rows = [lights[i:i + 3] for i in range(0, len(lights), 3)]
        lights_html = ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:14px">'
                       + "".join("<tr>" + "".join(_light(s) for s in row) + "</tr>" for row in rows) + "</table>")
        air = advice.air_line
        if block.get("label") and report.air is not None:
            air += f" · {block['label']} {report.air.station} 측정소"
    severe = block is not None and block["advice"].severe
    line_style = (f"background:{LIGHTS['critical'][0]};color:#ffffff;border-radius:14px;padding:12px 14px;" if severe
                  else f"color:{BODY};border-left:4px solid {ACCENT};padding:2px 0 2px 12px;")
    # The weather fairy and today's weather picture sit to the right of the greeting (daily_art).
    figures = "".join(f'<img src="{escape(src, quote=True)}" width="{size}" height="{size}" alt="{alt}" '
                      f'style="display:inline-block;width:{size}px;height:{size}px;vertical-align:bottom;margin-left:4px">'
                      for cid, size, alt in (("fairy", 84, "날씨 요정"), ("weather", 72, "오늘의 날씨"))
                      for src in [pictures.get(cid)] if src)
    top = ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
           f'<td style="vertical-align:bottom"><div style="font-size:13px;font-weight:700;color:{ACCENT};letter-spacing:0.2px">SMITH · 하루 일과</div>'
           f'<div style="font-size:26px;line-height:1.25;letter-spacing:-0.6px;color:{INK};margin:8px 0 2px">{escape(greeting)}</div>'
           f'<div style="font-size:14px;color:{MUTED}">{escape(date_line)}</div></td>'
           + (f'<td style="width:170px;text-align:right;vertical-align:bottom">{figures}</td>' if figures else "")
           + "</tr></table>")
    return (f'<tr><td style="padding:0 0 12px"><div style="background:{HERO};border-radius:24px;padding:24px 22px 18px">{top}'
            f'<div style="font-size:20px;font-weight:700;line-height:1.35;color:{INK};margin:16px 0 8px">{escape(headline)}</div>'
            + (f'<div style="font-size:15px;line-height:1.6;{line_style}">{"⚠ " if severe else ""}{escape(line)}</div>' if line else "")
            + lights_html
            + (f'<div style="font-size:12px;line-height:1.5;color:{MUTED};margin-top:12px">{escape(air)}</div>' if air else "")
            + "</div></td></tr>")


def _weather_line(advice: weather.Advice) -> str:
    items = ", ".join(advice.items) if advice.items else "특별한 준비물은 없습니다"
    cautions = " ".join(c + "." for c in advice.cautions)
    return f"준비물: {items}." + (f" {cautions}" if cautions else "")


def _rows(headers: list[str], rows: list[list[str]]) -> str:
    """A small table: escaped cells unless a cell is already HTML (a `Raw` string)."""
    head = "".join(f'<th style="text-align:left;font-size:12px;font-weight:700;color:{MUTED};padding:6px 6px 8px;'
                   f'border-bottom:1px solid {HAIRLINE}">{escape(h)}</th>' for h in headers)
    body = "".join("<tr>" + "".join(f'<td style="font-size:14px;line-height:1.5;padding:8px 6px;color:{BODY};'
                                    f'border-bottom:1px solid {HAIRLINE};vertical-align:top">'
                                    f'{c if isinstance(c, Raw) else escape(c)}</td>' for c in row) + "</tr>" for row in rows)
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>{head}</tr>{body}</table>')


class Raw(str):
    """A cell that is already HTML."""


def _history(data: dict[str, Any], prose: dict[str, Any]) -> str:
    """역사의 오늘: the writer's one-or-two lines on an event of this calendar day, or the code's own pick; just for fun."""
    from smith import history

    aside = prose.get("history") if isinstance(prose.get("history"), dict) else None
    pick = data.get("history_pick")
    if aside:
        year, line = aside["year"], aside["line"]
    elif pick is not None:
        year, line = pick.year, pick.text
    else:
        return ""
    url = history.page_url(data["day"])
    body = (f'<div style="font-size:15px;line-height:1.65;color:{BODY}"><span style="font-weight:700;color:{ACCENT}">{year}년</span> '
            f'{escape(line)}</div>'
            f'<div style="font-size:12px;color:{MUTED};margin-top:6px">재미로 보는 코너 · 출처: '
            f'<a href="{escape(url, quote=True)}" style="color:{MUTED}">위키백과 {data["day"].month}월 {data["day"].day}일</a></div>')
    return _card("역사의 오늘", "", body)


def _calendar(data: dict[str, Any], prose: dict[str, Any]) -> str:
    events: list[agenda.Event] = data["events"]
    due_today: list[agenda.Task] = data["tasks"]["due_today"]
    if not events and not due_today:
        return _card("오늘 일정", "오늘 등록된 일정이 없습니다. 여유 있는 하루 보내세요.", "")
    rows: list[list[str]] = []
    for event in events:
        when = "종일" if event.all_day else f"{event.start:%H:%M}–{event.end:%H:%M}"   # type: ignore[str-format]
        title = escape(event.title) + (f" {_pill('예약', 'neutral')}" if event.event_type == "fromGmail" else "")
        if event.all_day and (event.end - event.start).days > 1:   # type: ignore[operator]
            title += f' <span style="color:{MUTED}">~{(event.end - timedelta(days=1)):%m/%d}</span>'   # type: ignore[operator]
        rows.append([when, Raw(title), event.location or ""])
    for task in due_today:
        rows.append(["할 일", Raw(escape(task.title) + f" {_pill('오늘 기한', 'warn')}"), task.list_title])
    notes = [f"겹침: {a.title} / {b.title}" for a, b in data["overlaps"]]
    notes += [f"{gap}분 간격: {a.title} → {b.title}" for a, b, gap in data["tight_gaps"]]
    lead = prose.get("agenda_line") or (f"일정 {len(events)}건" + (f", 오늘 기한 할 일 {len(due_today)}건" if due_today else "")
                                         + ". 시간이 없는 일정은 등록 순서입니다.")
    inner = _rows(["시간", "일정", "장소"], rows)
    if notes:
        inner += f'<div style="font-size:13px;color:{INK};margin-top:8px">▲ {escape(" · ".join(notes))}</div>'
    title = "오늘 일정" + (f" {len(events)}건" if events else "") + (f" · 할 일 {len(due_today)}건" if due_today else "")
    return _card(title, lead, inner)


def _anniversaries(data: dict[str, Any], prose: dict[str, Any]) -> str:
    reminders: list[people.Reminder] = data["reminders"]
    upcoming: list[people.Occurrence] = data["upcoming"]
    carried: list[dict[str, Any]] = data.get("carried_reminders") or []
    if not reminders and not upcoming and not carried:
        return ""
    ideas = {i.get("key"): i.get("idea") for i in prose.get("anniversaries", []) if isinstance(i, dict)}
    # One table, soonest first (user request 2026-10-25): a reminder day carries a pill, the rest a plain D-N.
    flagged = {r.occurrence.key: r for r in reminders}
    merged = {o.key: o for o in upcoming}
    merged.update({r.occurrence.key: r.occurrence for r in reminders})
    rows: list[list[str]] = []
    for occ in sorted(merged.values(), key=lambda o: (o.on, o.label)):
        left = (occ.on - data["day"]).days
        when = "오늘" if left == 0 else f"D-{left}"
        first = (_pill(when, "bad" if left == 0 else ("warn" if left <= 7 else "neutral")) if occ.key in flagged
                 else f'<span style="color:{MUTED}">{when}</span>')
        star = f" ★ {escape(occ.milestone)}" if occ.milestone else ""
        note = f'<div style="font-size:12px;color:{MUTED}">{escape(occ.note)}</div>' if occ.note else ""
        idea = ideas.get(occ.key)
        idea_html = f'<div style="font-size:13px;line-height:1.5;color:{INK};margin-top:3px">→ {escape(idea)}</div>' if idea else ""
        rows.append([Raw(first), Raw(f"<b>{escape(occ.label)}</b>{star}{note}{idea_html}"),
                     f"{occ.on:%m/%d} ({WEEKDAY_SHORT[occ.on.weekday()]})"])
    for item in carried:   # A reminder from a morning whose mail never went out, said once.
        if any(item["key"].rsplit(":D-", 1)[0] == o.key for o in merged.values()):
            continue
        star = f" ★ {escape(item['milestone'])}" if item.get("milestone") else ""
        rows.insert(0, [Raw(_pill("지난 알림", "neutral")),
                        Raw(f"<b>{escape(item['label'])}</b>{star}<div style=\"font-size:12px;color:{MUTED}\">"
                            f"{item['missed_on']:%m/%d} 메일이 나가지 못해 오늘 알립니다</div>"),
                        f"{item['date']:%m/%d} ({WEEKDAY_SHORT[item['date'].weekday()]})"])
    lead = ("오늘 챙길 날이 있습니다." if any(r.days_left == 0 for r in reminders) else
            "미리 준비하면 좋은 날입니다." if reminders or carried else "2주 안에 있는 날들입니다.")
    return _card("기념일", lead, _rows(["남은 날", "날", "날짜"], rows), accent=bool(reminders))


def _news(data: dict[str, Any], prose: dict[str, Any]) -> str:
    news = data["news"]
    if news is None:
        return ""
    if not news:
        return _card("오늘의 뉴스", "확인된 뉴스를 오늘 받지 못했습니다.", "")
    summaries = {s.get("ref"): s.get("summary") for s in prose.get("news", []) if isinstance(s, dict)}
    blocks = []
    for item in news:
        url = item.get("source_url") or ""
        head = escape(str(item.get("headline", "")))
        link = f'<a href="{escape(url)}" style="color:{INK};text-decoration:underline">{head}</a>' if url.startswith("https://") else head
        meta = " · ".join(p for p in (str(item.get("publisher") or ""), str(item.get("published_on") or "")) if p)
        summary = summaries.get(item.get("ref")) or str(item.get("detail") or "")
        blocks.append(f'<div style="margin:8px 0 12px"><div style="font-size:15px;line-height:1.5;font-weight:700">{link}</div>'
                      f'<div style="font-size:12px;color:{MUTED};margin:2px 0 4px">{escape(meta)}</div>'
                      f'<div style="font-size:14px;line-height:1.6;color:{BODY}">{escape(summary)}</div></div>')
    return _card("오늘의 뉴스", f"간밤의 국내외 소식 {len(news)}건입니다. 링크는 도달을 확인한 것만 싣습니다.", "".join(blocks))


def _sentence(prose: dict[str, Any], picture: str | None = None) -> str:
    sentence = prose.get("sentence")
    if not sentence:
        return ""
    text = f'<div style="font-size:18px;line-height:1.6;color:{INK}">“{escape(sentence)}”</div>'
    if picture:
        text = ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
                f'<td style="width:76px;vertical-align:middle"><img src="{escape(picture, quote=True)}" width="64" height="64" '
                f'alt="오늘의 기분" style="display:block;width:64px;height:64px"></td>'
                f'<td style="vertical-align:middle">{text}</td></tr></table>')
    return _card("오늘의 문장", "", text, accent=True)


def _tasks(data: dict[str, Any]) -> str:
    overdue: list[agenda.Task] = data["tasks"]["overdue"]
    total = data["tasks"]["overdue_total"]
    if "tasks" in data["failures"]:
        return _card("미완료 할 일", "할 일 목록을 오늘 읽지 못했습니다.", "")
    if not overdue:
        return ""
    rows = [[f"D+{(data['day'] - t.due).days}", t.title, t.due.isoformat()] for t in overdue if t.due]   # type: ignore[operator]
    lead = (f"기한이 지난 할 일 {total}건 중 가까운 {len(overdue)}건입니다." if total > len(overdue)
            else f"기한이 지난 할 일 {total}건입니다.")
    return _card("미완료 할 일", lead, _rows(["지남", "할 일", "기한"], rows))


def _holidays(data: dict[str, Any]) -> str:
    ahead = data["holidays_ahead"]
    runs = data["long_weekends"]
    if not ahead and not runs:
        return ""
    lines = [f"{h.day:%m/%d}({WEEKDAY_SHORT[h.day.weekday()]}) {escape(h.name)} · D-{(h.day - data['day']).days}" for h in ahead]
    lines += [f"연휴 {a:%m/%d}~{b:%m/%d} ({(b - a).days + 1}일)" for a, b in runs]
    return _card("공휴일·연휴", "2주 안의 쉬는 날입니다.",
                 "".join(f'<div style="font-size:14px;line-height:1.7;color:{BODY}">{line}</div>' for line in lines))


def _finance(data: dict[str, Any]) -> str:
    dates = data["finance_dates"]
    if not dates:
        return ""
    lines = []
    for item in dates:
        when = "오늘" if item["days"] == 0 else f"D-{item['days']}"
        lines.append(f"{item['date']:%m/%d} {escape(item['label'])} · {when}")
    return _card("재무 날짜", "날짜만 적습니다. 금액과 판단은 재무 메일에서 다룹니다.",
                 "".join(f'<div style="font-size:14px;line-height:1.7;color:{BODY}">{line}</div>' for line in lines)
                 + f'<div style="font-size:12px;color:{MUTED};margin-top:6px">세금 일정은 통상 일정이며 그해 고지서로 확인하십시오.</div>')


def _text(data: dict[str, Any], prose: dict[str, Any], subject: str) -> str:
    lines = [subject, ""]
    if data["weather"] is not None:
        advice = data["weather"]["advice"]
        lines += [advice.headline, _plain(prose.get("weather_line") or _weather_line(advice)), advice.air_line, ""]
    lines.append(f"오늘 일정 {len(data['events'])}건")
    for event in data["events"]:
        when = "종일" if event.all_day else f"{event.start:%H:%M}"   # type: ignore[str-format]
        lines.append(f"- {when} {event.title}")
    for item in sorted(data["reminders"], key=lambda r: r.days_left):
        lines.append(f"- 기념일: {item.occurrence.label} {'오늘' if item.days_left == 0 else f'D-{item.days_left}'}")
    if data["tasks"]["overdue"]:
        lines.append(f"미완료 할 일 {data['tasks']['overdue_total']}건")
    lines += ["", "이 메일은 HTML 형식입니다. HTML을 지원하는 메일 앱에서 확인하시기 바랍니다."]
    return "\n".join(lines)
