"""The evening "내일 준비" mail (daily design §16.1) rendered as the morning mail's evening sibling: the same cream cards
and traffic lights, a night-blue head card instead of the sand one, and lists instead of tables (it is read on a phone
on the sofa). Every line comes from code; the writer's prose only replaces the weather and agenda sentences and adds
gift ideas and the talk item.
"""
from html import escape
from pathlib import Path
from typing import Any

from smith import daily_art, daily_html, korean, netflix_picks, talk, weather
from smith.daily_html import ACCENT, BODY, INK, LIGHTS, MUTED, _card, _page, _pill
from smith.report_html import _plain

PREFIX = "[Smith][내일 준비]"
# Night tokens for the head card, chosen for >= 4.5:1 on NIGHT (#2c3350): TEXT 12.1, SOFT 7.4, GLOW 7.0.
NIGHT, NIGHT_TEXT, NIGHT_SOFT, NIGHT_GLOW = "#2c3350", "#fbf6ee", "#cfc8dc", "#f2b27a"
FOOTER = ("Smith는 조회 전용입니다: 캘린더·할 일을 바꾸지 않고, 메일을 전달하지 않습니다. 내일 일정이나 이야깃거리에 대한 질문은 "
          "이 메일에 답장으로 주십시오. 내일 아침 하루 일과 메일이 최신 날씨로 다시 알려 드립니다.")
WEEKDAY_SHORT = daily_html.WEEKDAY_SHORT
# The warm lines code says when no writer is available (the mail never waits for a model).
FALLBACK_LINES = ("오늘도 고생 많았어. 내일도 당신 편이야.", "오늘 하루 어땠어? 당신 얘기 듣고 싶어.",
                  "요즘 애쓰는 거 다 보여. 고마워.", "내일은 오늘보다 조금 더 가벼운 하루였으면 좋겠다.",
                  "당신 덕분에 오늘도 웃었어.", "푹 자고 내일 아침에 맛있는 거 먹자.")


def render(data: dict[str, Any], *, partner: str = "아내", inline: bool = False) -> tuple[str, str, str]:
    """(subject, html, text) for one evening's mail. `inline` embeds the pictures as data URIs (the preview file)."""
    prose = data.get("prose") or {}
    images = images_for(data)
    pictures = {cid: daily_art.source(cid, images, inline=inline) for cid in images}
    subject = _subject(data)
    item = talk_item(data)
    fixes = prose.setdefault("korean_fixes", []) if prose else []
    cards = [c for c in (_prep(data), _talk(item, partner), _netflix(data, fixes), _sleep(data), _agenda(data, prose),
                         _dates(data, prose)) if c]
    footer_lines = [FOOTER]
    if data["failures"]:
        footer_lines.append("받지 못한 자료: " + ", ".join(f"{k}({v})" for k, v in sorted(data["failures"].items())))
    if prose.get("korean_fixes"):
        footer_lines.append(korean.summary({"tidied": prose["korean_fixes"]}))
    footer = ('<tr><td style="padding:4px 6px 12px">'
              + "".join(f'<div style="font-size:12px;line-height:1.6;color:{MUTED};margin-bottom:6px">{escape(line)}</div>'
                        for line in footer_lines) + "</td></tr>")
    html = _page(_hero(data, prose, pictures) + "".join(cards) + footer, "Smith 내일 준비")
    return subject, html, _text(data, prose, item, subject, partner)


def images_for(data: dict[str, Any]) -> dict[str, Path]:
    """cid -> file: the fairy, tomorrow's weather picture and the moon beside the talk item (files that exist)."""
    block = data.get("weather")
    wanted = {daily_art.FAIRY: daily_art.FAIRY,
              "weather": daily_art.weather_key(None if block is None else block["report"], None if block is None else block["advice"]),
              "mood": "moon"}
    return {cid: path for cid, key in wanted.items() for path in [daily_art.ASSETS / f"{key}.png"] if path.exists()}


def talk_item(data: dict[str, Any]) -> dict[str, str]:
    """The talk item shown: the writer's when it passed its checks, else the code's own (the first verified candidate
    of a current kind, or a warm line rotating by date). Kept in the snapshot so later evenings do not repeat it."""
    prose = data.get("prose") or {}
    if isinstance(prose.get("talk"), dict):
        item = dict(prose["talk"])
    else:
        candidates = data.get("talk_candidates") or []
        if data["talk_kind"] in talk.RESEARCHED and candidates:
            first = candidates[0]
            item = {"kind": data["talk_kind"], "ref": first["ref"], "title": first["headline"],
                    "line": f"요즘 '{first['headline']}' 이야기 많이 하던데, 들어 봤어?", "note": first["detail"]}
        else:
            kind = data["talk_kind"] if data["talk_kind"] not in talk.RESEARCHED else talk.FALLBACK
            item = {"kind": kind, "ref": "", "title": "", "line": fallback_line(data), "note": ""}
    source = next((c for c in data.get("talk_candidates") or [] if c["ref"] == item.get("ref")), None)
    item["label"] = talk.KINDS[item["kind"]]
    if source is not None:
        item.update({"url": source["source_url"], "publisher": source.get("publisher") or "",
                     "published_on": source.get("published_on") or ""})
    return item


def fallback_line(data: dict[str, Any]) -> str:
    """The code's warm line: the first not sent within the last month (`talk_recent`, newest first), else the one
    sent longest ago, so a run of evenings without a writer does not repeat itself every week."""
    used = [r.get("line", "") for r in data.get("talk_recent") or []]
    fresh = [line for line in FALLBACK_LINES if line not in used]
    if fresh:
        return fresh[data["day"].toordinal() % len(fresh)]
    return max(FALLBACK_LINES, key=lambda line: used.index(line))


def _subject(data: dict[str, Any]) -> str:
    day = data["day"]
    parts = [f"{PREFIX} {day:%m/%d} ({data['weekday']})"]
    if data["holiday"]:
        parts.append(data["holiday"])
    if data["weather"] is not None:
        advice = data["weather"]["advice"]
        parts.append(("⚠ " if advice.severe else "") + advice.headline)
    timed = [e for e in data["events"] if not e.all_day]
    parts.append(f"일정 {len(data['events'])}건" + (f" · 첫 일정 {timed[0].start:%H:%M}" if timed else ""))   # type: ignore[str-format]
    if data["reminders"]:
        first = min(data["reminders"], key=lambda r: r.days_left)
        parts.append(f"{first.occurrence.label} {_when(first.days_left)}")
    return " · ".join(parts)


def _when(days_left: int) -> str:
    """A reminder's distance as seen tonight: days_left counts from tomorrow."""
    return "내일" if days_left == 0 else f"D-{days_left + 1}"


def _hero(data: dict[str, Any], prose: dict[str, Any], pictures: dict[str, str | None]) -> str:
    day = data["day"]
    date_line = f"내일 {day:%m월 %d일} {data['weekday']}요일" + (f" · {data['holiday']}" if data["holiday"] else "")
    block = data["weather"]
    if block is not None and block.get("label"):
        date_line += f" · {block['label']} 날씨 기준"
    if block is None:
        headline, line, lights_html, air = "내일 날씨 자료를 받지 못했습니다", "", "", ""
    else:
        advice, report = block["advice"], block["report"]
        headline = prose.get("headline") or advice.headline
        line = prose.get("weather_line") or ("내일 " + daily_html._weather_line(advice))
        lights = weather.signals(report, advice, airways_sensitive=bool(data.get("airways_sensitive")))
        # Two lights a row: with keep-all a word such as 한파주의보 does not fit a third of a phone screen and would widen
        # the whole mail past the screen (checked at 420 px, 2026-10-27).
        lights_html = daily_html.lights_table(lights, 2) + _windows(data)
        air = advice.air_line
    severe = block is not None and block["advice"].severe
    line_style = (f"background:{LIGHTS['critical'][0]};color:#ffffff;border-radius:14px;padding:12px 14px;" if severe
                  else f"color:{NIGHT_TEXT};border-left:4px solid {NIGHT_GLOW};padding:2px 0 2px 12px;")
    figures = "".join(f'<img src="{escape(src, quote=True)}" width="{size}" height="{size}" alt="{alt}" '
                      f'style="display:inline-block;width:{size}px;height:{size}px;vertical-align:bottom;margin-left:4px">'
                      for cid, size, alt in (("fairy", 72, "날씨 요정"), ("weather", 64, "내일의 날씨"))
                      for src in [pictures.get(cid)] if src)
    top = ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
           f'<td style="vertical-align:bottom"><div style="font-size:13px;font-weight:700;color:{NIGHT_GLOW};letter-spacing:0.2px">SMITH · 내일 준비</div>'
           f'<div style="font-size:24px;line-height:1.3;letter-spacing:-0.6px;color:{NIGHT_TEXT};margin:8px 0 2px">오늘 하루 수고 많으셨습니다</div>'
           f'<div style="font-size:14px;color:{NIGHT_SOFT}">{escape(date_line)}</div></td>'
           + (f'<td style="width:150px;text-align:right;vertical-align:bottom">{figures}</td>' if figures else "")
           + "</tr></table>")
    return (f'<tr><td style="padding:0 0 12px"><div style="background:{NIGHT};border-radius:24px;padding:24px 22px 18px">{top}'
            f'<div style="font-size:20px;font-weight:700;line-height:1.35;color:{NIGHT_TEXT};margin:16px 0 8px">{escape(headline)}</div>'
            + (f'<div style="font-size:15px;line-height:1.6;{line_style}">{"⚠ " if severe else ""}{escape(line)}</div>' if line else "")
            + lights_html
            + (f'<div style="font-size:12px;line-height:1.5;color:{NIGHT_SOFT};margin-top:12px">{escape(air)}</div>' if air else "")
            + "</div></td></tr>")


def _windows(data: dict[str, Any]) -> str:
    """The commute (or outing) hours: one line each, figures from code."""
    rows = "".join(f'<div style="font-size:14px;line-height:1.6;color:{NIGHT_TEXT}"><b style="color:{NIGHT_GLOW}">'
                   f'{escape(w.label)}</b> {escape(weather.window_line(w)[len(w.label) + 1:])}</div>' for w in data.get("commute") or [])
    return f'<div style="margin:12px 6px 0">{rows}</div>' if rows else ""


def _prep(data: dict[str, Any]) -> str:
    items = data["prep"]
    if not items:
        return _card("오늘 밤 챙길 것", "내일 따로 챙길 것은 없습니다. 푹 쉬세요.", "", accent=True)
    rows = "".join(f'<div style="font-size:15px;line-height:1.55;color:{BODY};padding:7px 0;border-bottom:1px solid #f0e6da">'
                   f'<span style="color:{ACCENT};font-weight:700">☐</span> {escape(item["text"])}</div>' for item in items)
    return _card("오늘 밤 챙길 것", "", rows, accent=True)


def _talk(item: dict[str, str], partner: str) -> str:
    head = _pill(item["label"], "warn" if item["kind"] in talk.RESEARCHED else "good")
    if item.get("title"):
        head += f' <b style="font-size:15px;color:{INK}">{escape(item["title"])}</b>'
    body = (f'<div style="margin-bottom:8px">{head}</div>'
            f'<div style="font-size:17px;line-height:1.6;color:{INK}">“{escape(item["line"])}”</div>')
    if item.get("note"):
        body += f'<div style="font-size:14px;line-height:1.6;color:{BODY};margin-top:8px">{escape(item["note"])}</div>'
    if str(item.get("url", "")).startswith("https://"):
        meta = " · ".join(p for p in (item.get("publisher") or "", item.get("published_on") or "") if p) or "출처"
        body += (f'<div style="font-size:12px;color:{MUTED};margin-top:6px">출처: <a href="{escape(item["url"], quote=True)}" '
                 f'style="color:{MUTED}">{escape(meta)}</a> · 도달을 확인한 링크만 싣습니다</div>')
    return _card(f"오늘 밤 {partner}에게", "", body)


def _netflix(data: dict[str, Any], fixes: list[dict[str, str]]) -> str:
    """오늘 밤 넷플릭스: three picks, each linked to its Netflix Korea page, with the acclaim's source."""
    picks = data.get("netflix")
    if picks is None:
        return ""
    if not picks:
        return _card("오늘 밤 넷플릭스", "오늘은 확인된 추천을 받지 못했습니다.", "")
    rows = []
    for pick in netflix_picks.for_mail(picks, fixes):
        meta = " · ".join(p for p in (pick.get("year"), pick.get("kind"), pick.get("genre")) if p)
        source = (f' · 평가 출처: <a href="{escape(pick["source_url"], quote=True)}" style="color:{MUTED}">'
                  f'{escape(pick.get("publisher") or "출처")}</a>' if str(pick.get("source_url", "")).startswith("https://") else "")
        rows.append(f'<div style="padding:9px 0;border-bottom:1px solid #f0e6da">'
                    f'<a href="{escape(pick["netflix_url"], quote=True)}" style="font-size:16px;font-weight:700;color:{INK};'
                    f'text-decoration:underline">{escape(pick["title"])}</a>'
                    f'<div style="font-size:12px;color:{MUTED};margin:2px 0 4px">{escape(meta)}</div>'
                    f'<div style="font-size:14px;line-height:1.6;color:{BODY}">{escape(pick["why"])}</div>'
                    f'<div style="font-size:12px;color:{MUTED};margin-top:3px">넷플릭스에서 보기{source}</div></div>')
    note = (f'<div style="font-size:12px;color:{MUTED};margin-top:8px">오늘 한국 넷플릭스 페이지에서 시청 가능한 것만 '
            f'골랐습니다. 평가는 출처 페이지의 도달을 확인한 것만 싣습니다.</div>')
    return _card("오늘 밤 넷플릭스", "", "".join(rows) + note)


def _sleep(data: dict[str, Any]) -> str:
    """오늘 밤 숙면: a bedtime from the usual wake time, the bedroom actions the night calls for, what the recent nights
    suggest, and the figures and guidance behind them."""
    plan = data.get("sleep")
    if plan is None:
        return ""
    head = ""
    if plan.bedtime:
        bed, wake = (int(t[:2]) * 60 + int(t[3:]) for t in (plan.bedtime, plan.wake))
        gap = (wake - bed) % (24 * 60)
        head = (f'<div style="font-size:20px;font-weight:700;color:{INK}">권장 취침 {plan.bedtime}</div>'
                f'<div style="font-size:13px;color:{MUTED};margin:2px 0 8px">{escape(plan.wake_reason)} {plan.wake} 기준 '
                f'{gap // 60}시간{f" {gap % 60}분" if gap % 60 else ""} 전{" (최근 수면이 짧아 30분 당김)" if gap > 450 else ""}</div>')
    lines = [*plan.environment, *plan.body]
    rows = "".join(f'<div style="font-size:15px;line-height:1.55;color:{BODY};padding:7px 0;border-bottom:1px solid #f0e6da">'
                   f'<span style="color:{ACCENT};font-weight:700">☐</span> {escape(line)}</div>' for line in lines)
    if not rows and not head:
        rows = f'<div style="font-size:15px;color:{BODY}">오늘 밤은 따로 챙길 것이 없습니다. 평소처럼 편히 주무세요.</div>'
    small = [" · ".join(plan.facts)] if plan.facts else []
    small += [plan.data_note] if plan.data_note else []
    small += ["근거: " + " / ".join(plan.basis)] if plan.basis else []
    small.append("수면 자료: Fitbit(Google Health, 조회 전용). 건강 상태를 판단하지 않는 생활 조언입니다.")
    inner = head + rows + "".join(f'<div style="font-size:12px;line-height:1.6;color:{MUTED};margin-top:6px">{escape(t)}</div>'
                                  for t in small)
    return _card("오늘 밤 숙면", "", inner)


def _agenda(data: dict[str, Any], prose: dict[str, Any]) -> str:
    events = data["events"]
    due = data["tasks"]["due_today"]
    if "calendar" in data["failures"]:
        return _card("내일 일정", "내일 일정을 읽지 못했습니다.", "")
    if not events and not due:
        return _card("내일 일정", "내일 등록된 일정이 없습니다.", "")
    rows = []
    for event in events:
        when = "종일" if event.all_day else f"{event.start:%H:%M}–{event.end:%H:%M}"   # type: ignore[str-format]
        place = f'<div style="font-size:12px;color:{MUTED}">{escape(event.location)}</div>' if event.location else ""
        rows.append(f'<div style="padding:7px 0;border-bottom:1px solid #f0e6da"><span style="font-size:13px;font-weight:700;'
                    f'color:{ACCENT}">{when}</span> <span style="font-size:15px;color:{INK}">{escape(event.title)}</span>{place}</div>')
    rows += [f'<div style="padding:7px 0;border-bottom:1px solid #f0e6da">{_pill("내일 기한", "warn")} '
             f'<span style="font-size:15px;color:{INK}">{escape(t.title)}</span></div>' for t in due]
    notes = [f"겹침: {a.title} / {b.title}" for a, b in data["overlaps"]]
    notes += [f"{gap}분 간격: {a.title} → {b.title}" for a, b, gap in data["tight_gaps"]]
    inner = "".join(rows) + (f'<div style="font-size:13px;color:{INK};margin-top:8px">▲ {escape(" · ".join(notes))}</div>' if notes else "")
    lead = prose.get("agenda_line") or ""
    return _card(f"내일 일정 {len(events)}건" + (f" · 할 일 {len(due)}건" if due else ""), escape(lead), inner)


def _dates(data: dict[str, Any], prose: dict[str, Any]) -> str:
    reminders = sorted(data["reminders"], key=lambda r: r.days_left)
    if not reminders:
        return ""
    ideas = {i.get("key"): i.get("idea") for i in prose.get("anniversaries", []) if isinstance(i, dict)}
    rows = []
    for reminder in reminders:
        occ = reminder.occurrence
        star = f" ★ {escape(occ.milestone)}" if occ.milestone else ""
        idea = ideas.get(occ.key)
        rows.append(f'<div style="padding:7px 0;border-bottom:1px solid #f0e6da">'
                    f'{_pill(_when(reminder.days_left), "bad" if reminder.days_left == 0 else "warn")} '
                    f'<b style="font-size:15px;color:{INK}">{escape(occ.label)}</b>{star}'
                    f'<span style="font-size:12px;color:{MUTED}"> {occ.on:%m/%d} ({WEEKDAY_SHORT[occ.on.weekday()]})</span>'
                    + (f'<div style="font-size:13px;line-height:1.5;color:{INK};margin-top:3px">→ {escape(idea)}</div>' if idea else "")
                    + "</div>")
    return _card("챙길 날", "", "".join(rows))


def _text(data: dict[str, Any], prose: dict[str, Any], item: dict[str, str], subject: str, partner: str) -> str:
    lines = [subject, ""]
    if data["weather"] is not None:
        advice = data["weather"]["advice"]
        lines += [advice.headline, _plain(prose.get("weather_line") or ("내일 " + daily_html._weather_line(advice))),
                  *[weather.window_line(w) for w in data.get("commute") or []], advice.air_line, ""]
    lines.append("오늘 밤 챙길 것")
    lines += [f"- {p['text']}" for p in data["prep"]] or ["- 따로 챙길 것은 없습니다."]
    lines += ["", f"오늘 밤 {partner}에게 ({item['label']})" + (f": {item['title']}" if item.get("title") else ""), f"“{item['line']}”"]
    if item.get("note"):
        lines.append(item["note"])
    plan = data.get("sleep")
    if plan is not None:
        lines += ["", "오늘 밤 숙면" + (f": 권장 취침 {plan.bedtime} ({plan.wake_reason} {plan.wake} 기준)" if plan.bedtime else "")]
        lines += [f"- {line}" for line in (*plan.environment, *plan.body)]
    if data.get("netflix"):
        lines += ["", "오늘 밤 넷플릭스"] + [f"- {p['title']} ({p.get('year') or ''} {p.get('kind') or ''}) {p['netflix_url']}"
                                          for p in data["netflix"]]
    lines += ["", f"내일 일정 {len(data['events'])}건"]
    for event in data["events"]:
        lines.append(f"- {'종일' if event.all_day else format(event.start, '%H:%M')} {event.title}")
    lines += ["", "이 메일은 HTML 형식입니다. HTML을 지원하는 메일 앱에서 확인하시기 바랍니다."]
    return "\n".join(lines)
