"""The weekly health letter rendered (FR-38, user review 2026-10-25): a verdict first, a goal dashboard that says what
to do and what to avoid, one topic card as a short table (plain names, the latest value, code's trend word, one line
each) with line charts over time, then this week's actions, the follow-ups and the coach boundary. Calm green register
apart from the finance reports' blue and the daily mail's sand.

Every line comes from code — the trend and level words, the guideline do/avoid lines, the guideline fallback actions —
so the letter stands without the writer; the writer's prose replaces the headline, the verdict, the per-item lines, the
do/avoid lines and the actions when it passed verification (health_writer). Charts are PNGs (health_chart): a data URI
in the preview file, an inline `cid:` attachment in the sent mail, because mail clients drop SVG.
"""
import base64
from datetime import date
from html import escape
from typing import Any

from smith import health_chart
from smith.report_html import _plain

PREFIX = "[Smith][건강]"
PAGE, CARD, HERO, HERO_DEEP = "#eef3ee", "#fbfdfb", "#dfeadf", "#cfe0cf"
INK, BODY, MUTED, HAIRLINE, ACCENT = "#1d2a22", "#33413a", "#6b7a70", "#dde6dd", "#2f7d4f"
FLAG_COLORS = {"high": "#c9552d", "low": "#c9552d", "attention": "#d79a1e", "normal": "#2f7d4f", "unknown": "#9aa89f"}
FLAG_WORDS = {"high": "높음", "low": "낮음", "attention": "주의", "normal": "정상", "unknown": "판정 없음"}
TREND_STYLE = {"improving": ("#2f7d4f", "▼ 좋아지는 중"), "worsening": ("#c9552d", "▲ 나빠지는 중"), "flat": ("#6b7a70", "= 그대로"),
               "single": ("#9aa89f", "한 번 측정")}
LEVEL_STYLE = {"normal": "#2f7d4f", "edge": "#d79a1e", "out": "#c9552d", "unknown": "#9aa89f"}
# Traffic lights for the wearable week (the daily mail's palette, validated on a card surface 2026-10-25): colour and shape.
LIGHTS = {"good": ("#2e9e44", "●"), "warning": ("#e9a800", "▲"), "serious": ("#e3542a", "■"), "none": ("#a89a8e", "○")}
CATEGORY_WORDS = {"food": "음식", "exercise": "운동", "life": "생활"}
SEVERITY_WORDS = {"normal": "정상", "note": "참고", "follow_up": "추적", "see_doctor": "진료 권고"}
SANS = "Pretendard,Inter,'Malgun Gothic','Apple SD Gothic Neo',-apple-system,'Segoe UI',Roboto,sans-serif"
MONO = "'JetBrains Mono',Consolas,'Courier New',monospace"
WEEKDAY_KO = "월화수목금토일"


def render(data: dict[str, Any], *, inline: bool = False) -> tuple[str, str, str]:
    """(subject, html, text) for one letter. `inline` embeds the charts as data URIs (the preview file); a sent mail
    references them by cid and carries them as attachments (`charts`)."""
    prose = data.get("prose") or {}
    images = charts(data)
    subject = _subject(data, prose)
    cards = [card for card in (_dashboard(data, prose), _topic(data, prose, images, inline), _actions(data, prose),
                               _activity(data), _review(data, prose), _follow_ups(data), _boundary(data)) if card]
    html = _page(_hero(data, prose) + "".join(cards))
    return subject, html, _text(data, prose, subject)


def charts(data: dict[str, Any]) -> dict[str, bytes]:
    """cid -> PNG for the topic's charted series (the sender attaches these inline)."""
    found: dict[str, bytes] = {}
    for code in data["topic"].get("charts", []):
        rows = [r for r in data["series"].get(code, []) if r["value"] is not None]
        if code == "ldl" and any(r["method"] == "direct" for r in rows):
            rows = [r for r in rows if r["method"] == "direct"]
        if not rows:
            continue
        points = [health_chart.Point(r["on"], r["value"], r["flag"], r["text"]) for r in rows]
        found[f"chart-{code}"] = health_chart.line_chart(points, title=rows[-1]["label"], unit=rows[-1]["unit"], ref=rows[-1]["ref"])
    return found


def _subject(data: dict[str, Any], prose: dict[str, Any]) -> str:
    day: date = data["day"]
    parts = [f"{PREFIX} {day:%m/%d} ({WEEKDAY_KO[day.weekday()]})", data["topic"]["label"]]
    headline = prose.get("headline")
    if headline:
        parts.append(headline)
    return " · ".join(parts)


def _page(inner: str) -> str:
    return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>Smith 건강</title></head>'
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


def _pill(text: str, color: str) -> str:
    return (f'<span style="display:inline-block;border:1px solid {color};color:{color};font-size:12px;font-weight:700;'
            f'border-radius:100px;padding:2px 9px;white-space:nowrap">{escape(text)}</span>')


def _trend_pill(status: dict[str, Any]) -> str:
    color, word = TREND_STYLE[status["trend"]]
    return _pill(word, color)


def _level_pill(status: dict[str, Any]) -> str:
    return _pill(status["level_word"], LEVEL_STYLE[status["level"]])


def _hero(data: dict[str, Any], prose: dict[str, Any]) -> str:
    day: date = data["day"]
    headline = prose.get("headline") or f"이번 주 주제는 {data['topic']['label']}입니다"
    verdict = prose.get("verdict") or _code_verdict(data)
    return (f'<tr><td style="padding:0 0 12px"><div style="background:{HERO};border-radius:24px;padding:22px 22px 18px">'
            f'<div style="font-size:13px;font-weight:700;color:{MUTED};letter-spacing:0.2px">Smith 건강 · {day:%Y년 %m월 %d일} '
            f'({WEEKDAY_KO[day.weekday()]}) · {escape(data["topic"]["label"])}</div>'
            f'<div style="font-size:22px;font-weight:800;line-height:1.35;color:{INK};margin-top:8px;letter-spacing:-0.4px">'
            f'{escape(headline)}</div>'
            f'<div style="font-size:15px;line-height:1.65;color:{BODY};margin-top:10px">{escape(verdict)}</div></div></td></tr>')


def _code_verdict(data: dict[str, Any]) -> str:
    """The reading when the writer did not pass: counts of items by trend and level, from code."""
    items = data["items"]
    if not items:
        return "이 주제에 기록된 값이 아직 없습니다."
    worsening = [i["label"] for i in items if i["trend"] == "worsening"]
    improving = [i["label"] for i in items if i["trend"] == "improving"]
    out = [i["label"] for i in items if i["level"] in ("edge", "out")]
    parts = []
    if out:
        parts.append("참고 범위를 벗어나거나 경계에 있는 값: " + ", ".join(out[:4]) + ".")
    if worsening:
        parts.append("지난 검진보다 나빠진 값: " + ", ".join(worsening[:4]) + ".")
    if improving:
        parts.append("좋아진 값: " + ", ".join(improving[:4]) + ".")
    if not parts:
        parts.append("이 주제의 값은 모두 참고 범위 안이고 지난 검진과 비슷합니다.")
    return " ".join(parts) + f" 행동의 축은 {data['topic']['axis']}입니다."


def _dashboard(data: dict[str, Any], prose: dict[str, Any]) -> str:
    if not data["goals"]:
        return ""
    goal_lines = prose.get("goal_lines") or {}
    rows = []
    for goal in data["goals"]:
        status = goal["status"]
        if goal["posture"]:
            latest = goal["posture"]
            value = " · ".join(f"{escape(p['label'])} {escape(p['latest']['text'])}{escape(p['unit'])}" for p in latest)
            on = latest[-1]["latest"]["on"]
            meta = f"{on:%Y.%m} 측정 · {escape(latest[-1]['device'])} 기준"
            pills = " ".join(_trend_pill(p) for p in latest[:1])
        elif status:
            o = status["latest"]
            value = f"{escape(o['text'])} {escape(o['unit'])}"
            if goal["pair"]:
                value += f" / {escape(goal['pair']['latest']['text'])} {escape(goal['pair']['unit'])}"
            on = o["on"]
            before = (f" · 지난 검진 {status['previous']['on']:%Y.%m} {escape(status['previous']['text'])}" if status["previous"] else "")
            meta = f"{on:%Y.%m} 측정 · 참고 범위 {escape(status['ref'] or '보고서에 없음')}{before}"
            pills = f"{_level_pill(status)} {_trend_pill(status)}"
        else:
            value, meta, pills = "기록 없음", "원장에 값이 없습니다", _pill("자료 없음", MUTED)
        lines = goal_lines.get(goal["key"]) or {"do": goal["do"], "avoid": goal["avoid"]}
        rows.append(f'<tr><td style="padding:10px 0;border-bottom:1px solid {HAIRLINE}">'
                    f'<div style="font-size:15px;color:{INK}"><b>{escape(goal["label"])}</b> &nbsp;{value} &nbsp;{pills}</div>'
                    f'<div style="font-size:12px;color:{MUTED};margin-top:2px">{meta}</div>'
                    f'<div style="font-size:14px;color:{BODY};margin-top:6px"><span style="color:{ACCENT};font-weight:700">하기</span> '
                    f'{escape(lines["do"])}</div>'
                    f'<div style="font-size:14px;color:{BODY};margin-top:2px"><span style="color:{FLAG_COLORS["high"]};font-weight:700">피하기</span> '
                    f'{escape(lines["avoid"])}</div></td></tr>')
    return _card("목표 계기판", "", f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{"".join(rows)}</table>',
                 accent=True)


def _topic(data: dict[str, Any], prose: dict[str, Any], images: dict[str, bytes], inline: bool) -> str:
    parts = []
    lines = prose.get("items") or {}
    rows = []
    # Only what deserves a line (user review 2026-10-25: the essentials, short and bold): the charted items, anything
    # outside or at the edge of its range, anything getting worse. The rest is one quiet sentence.
    charted = set(data["topic"].get("charts", []))
    notable = [i for i in data["items"] if i["code"] in charted or i["level"] != "normal" or i["trend"] == "worsening"]
    quiet = [i for i in data["items"] if i not in notable]
    for item in notable:
        o = item["latest"]
        line = lines.get(item["code"]) or _code_line(item)
        rows.append(f'<tr><td style="padding:8px 0;border-bottom:1px solid {HAIRLINE};vertical-align:top">'
                    f'<div style="font-size:14px;color:{INK}"><b>{escape(item["label"])}</b> &nbsp;'
                    f'<span style="font-family:{MONO}">{escape(o["text"])}{"?" if o["uncertain"] else ""} {escape(o["unit"])}</span> '
                    f'&nbsp;{_level_pill(item)} {_trend_pill(item)}</div>'
                    f'<div style="font-size:13px;line-height:1.55;color:{BODY};margin-top:3px">{escape(line)}</div></td></tr>')
    if rows:
        parts.append(f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{"".join(rows)}</table>')
    if quiet:
        parts.append(f'<div style="font-size:13px;color:{MUTED};margin-top:8px">정상 범위 안에서 그대로인 값: '
                     + ", ".join(f'{escape(i["label"])} {escape(i["latest"]["text"])}' for i in quiet) + "</div>")
    for cid, png in images.items():
        src = f"cid:{cid}" if not inline else "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        parts.append(f'<div style="margin:14px 0 4px"><img src="{src}" width="{health_chart.WIDTH}" alt="추이 그래프" '
                     f'style="display:block;width:100%;max-width:{health_chart.WIDTH}px;height:auto;border-radius:12px"></div>')
    if images:
        parts.append(f'<div style="font-size:12px;color:{MUTED}">점 색은 그 보고서의 판정(초록 정상 · 주황 주의 · 붉음 범위 밖), 연한 띠는 참고 범위입니다.</div>')
    if data["posture"]:
        parts.append(_posture_table(data["posture"]))
    findings = [f for f in data["findings"] if f["severity"] != "normal" and f["resolved_on"] is None][:5]
    if findings:
        items = "".join(
            f'<tr><td style="padding:4px 8px 4px 0;font-size:12px;color:{MUTED};white-space:nowrap;vertical-align:top">{f["on"]:%Y.%m}</td>'
            f'<td style="padding:4px 0;font-size:13px;color:{BODY}">{_pill(SEVERITY_WORDS[f["severity"]], FLAG_COLORS["attention"] if f["severity"] != "note" else MUTED)} '
            f'<b>{escape(f["track_label"])}</b> {escape(f["summary"])}'
            + (f' <span style="color:{MUTED}">— {escape(f["recommendation"])}</span>' if f["recommendation"] else "") + "</td></tr>"
            for f in findings)
        parts.append(f'<div style="font-size:13px;font-weight:700;color:{MUTED};margin:14px 0 4px">보고서가 쓴 소견</div>'
                     f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{items}</table>')
    for note in data["regimen_notes"]:
        parts.append(f'<div style="font-size:12px;color:{BODY};background:{HERO};border-radius:12px;padding:8px 12px;margin-top:10px">'
                     f'복용 약과 검사 해석 · {escape(note["text"])} <span style="color:{MUTED}">({escape(note["source"])})</span></div>')
    if data["uncertain"]:
        parts.append(f'<div style="font-size:12px;color:{MUTED};margin-top:8px">읽기가 불확실한 값(?): {escape(", ".join(data["uncertain"]))} '
                     f'(원본 결과지로 확인 전까지 참고만)</div>')
    return _card(f"이번 주 주제 · {data['topic']['label']}", "", "".join(parts))


def _code_line(item: dict[str, Any]) -> str:
    """One line from code for an item the writer did not cover: the level, and the move since the previous reading."""
    text = item["level_word"]
    if item["ref"]:
        text += f"(참고 {item['ref']})"
    if item["previous"] is not None:
        text += f", 지난 검진({item['previous']['on']:%Y.%m}) {item['previous']['text']}에서 {item['trend_word']}"
    return text + "."


POSTURE_KEY_CODES = ("forward_head_deg", "forward_head_mm", "pcmt_kg", "pelvic_tilt_deg", "shoulder_tilt_deg", "q_angle_deg",
                     "overall_index", "overall_grade")


def _posture_table(rows: list[dict[str, Any]]) -> str:
    """The latest posture measurement only, its key items (review 2026-10-25: all 48 rows used to print); earlier
    measurements and other devices are one line, since the methods are not comparable anyway."""
    if not rows:
        return ""
    latest_on = max(r["on"] for r in rows)
    latest = [r for r in rows if r["on"] == latest_on]
    device = latest[0]["device"]
    shown = [r for r in latest if r["code"] in POSTURE_KEY_CODES]
    cells = "".join(f'<tr><td style="padding:3px 8px 3px 0;font-size:13px;color:{BODY}">{escape(r["label"])}'
                    + (f' {r["side"]}' if r["side"] else "") + "</td>"
                    f'<td style="text-align:right;font-family:{MONO};font-size:12px;color:{INK}">{escape(r["text"])}{escape(r["unit"])}</td></tr>'
                    for r in shown)
    others = sorted({(r["device"], r["on"]) for r in rows if r["on"] != latest_on})
    earlier = "; ".join(f"{d} {o:%Y.%m}" for d, o in others)
    return (f'<div style="font-size:13px;font-weight:700;color:{INK};margin:12px 0 2px">자세 측정 · {escape(device)} '
            f'<span style="font-weight:400;color:{MUTED}">{latest_on:%Y.%m}</span></div>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{cells}</table>'
            f'<div style="font-size:12px;color:{MUTED};margin-top:6px">그 밖의 값 {len(latest) - len(shown)}개와 이전 측정({escape(earlier) or "없음"})은 '
            f'원장에 있습니다. 장비·방식이 다른 측정값은 서로 비교하지 않습니다.</div>')


def _actions(data: dict[str, Any], prose: dict[str, Any]) -> str:
    actions = prose.get("actions") or data["fallback_actions"]
    source_note = ("" if prose.get("actions") else
                   f'<div style="font-size:12px;color:{MUTED};margin-top:10px">이번 주는 공식 지침의 일반 권고입니다(개인화된 제안은 작성 모델이 '
                   f'검증을 통과했을 때 실립니다). 출처: {escape("; ".join(data["sources"]))}</div>')
    items = []
    for number, action in enumerate(actions, 1):
        items.append(f'<div style="border:1px solid {HAIRLINE};border-radius:14px;padding:12px 14px;margin:8px 0">'
                     f'<div style="font-size:15px;font-weight:700;color:{INK}">{number}. {escape(action["what"])} '
                     f'{_pill(CATEGORY_WORDS.get(action.get("category", ""), action.get("category", "")), ACCENT)}</div>'
                     f'<div style="font-size:14px;color:{BODY};margin-top:4px"><b>얼마나</b> {escape(action["amount"])}</div>'
                     f'<div style="font-size:13px;color:{BODY};margin-top:2px"><b>왜</b> {escape(action["why"])}</div>'
                     f'<div style="font-size:13px;color:{MUTED};margin-top:2px"><b>확인</b> {escape(action["check"])}</div></div>')
    return _card("이번 주 할 일", "", "".join(items) + source_note, accent=True)


def _activity(data: dict[str, Any]) -> str:
    act = data["activity"]
    guide = act["guideline"]
    if act["source"] == "estimate":
        low, high = act["aerobic_minutes"]
        lead = (f"Fitbit 연결 전이라 알려 주신 평소 활동으로 <b>추정</b>한 값입니다. 유산소 주 {low}~{high}분"
                f"(지침 {guide['aerobic_minutes']}분), 근력 운동 주 {act['strength_days']}일(지침 {guide['strength_days']}일).")
    else:
        start, end = act.get("week", ["", ""])
        lead = (f"Fitbit 기록({escape(str(start)[5:].replace('-', '.'))}~{escape(str(end)[5:].replace('-', '.'))}, {act['days_with_data']}일): "
                f"중강도 환산 활동 주 {act['aerobic_minutes']}분(고강도 1분 = 중강도 2분; 지침 {guide['aerobic_minutes']}분), "
                f"근력 운동 {act['strength_days']}일(지침 {guide['strength_days']}일).")
    if act["source"] == "estimate":
        marks = (f'<div style="font-size:14px;color:{BODY}">'
                 f'{_pill("유산소 " + ("충족" if act["meets_aerobic"] else "미달"), ACCENT if act["meets_aerobic"] else FLAG_COLORS["attention"])} '
                 f'{_pill("근력 " + ("충족" if act["meets_strength"] else "미달"), ACCENT if act["meets_strength"] else FLAG_COLORS["attention"])}'
                 f'<div style="font-size:12px;color:{MUTED};margin-top:8px">기준: {escape(guide["source"])}</div></div>')
        return _card("지난주 몸 상태", lead, marks)
    # Traffic lights (user request 2026-10-25): the client's figure, the guideline figure, and a level code decided —
    # colour, shape and word together, so nothing rests on the colour alone.
    rows = []
    for signal in act.get("signals", []):
        color, shape = LIGHTS[signal["level"]]
        rows.append(f'<tr><td style="padding:6px 8px 6px 0;border-bottom:1px solid {HAIRLINE};font-size:13px;color:{INK};white-space:nowrap">'
                    f'<b>{escape(signal["name"])}</b></td>'
                    f'<td style="padding:6px 8px 6px 0;border-bottom:1px solid {HAIRLINE};font-size:13px;color:{INK};white-space:nowrap">'
                    f'<span style="color:{color};font-size:14px">{shape}</span> {escape(signal["level_word"])}</td>'
                    f'<td style="padding:6px 8px 6px 0;border-bottom:1px solid {HAIRLINE};font-size:13px;color:{BODY}">{escape(signal["mine"])}</td>'
                    f'<td style="padding:6px 0;border-bottom:1px solid {HAIRLINE};font-size:12px;color:{MUTED}">{escape(signal["guide"])}</td></tr>')
    head = (f'<tr><td style="font-size:11px;color:{MUTED};padding-bottom:4px">항목</td><td style="font-size:11px;color:{MUTED}">신호</td>'
            f'<td style="font-size:11px;color:{MUTED}">내 지난주</td><td style="font-size:11px;color:{MUTED}">권고</td></tr>')
    extras = []
    if act.get("deep_avg_minutes") or act.get("rem_avg_minutes"):
        extras.append(f"깊은 잠 평균 {act.get('deep_avg_minutes') or 0}분 · REM {act.get('rem_avg_minutes') or 0}분")
    if act.get("skin_temp_delta_avg") is not None:
        extras.append(f"피부 온도 기준선 대비 {act['skin_temp_delta_avg']:+.2f}℃")
    if act.get("weight_latest_kg") is not None:
        extras.append(f"기기 체중 {act['weight_latest_kg']} kg")
    sources = sorted({s["source"] for s in act.get("signals", [])})
    marks = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{head}{"".join(rows)}</table>'
             + (f'<div style="font-size:12px;color:{BODY};margin-top:8px">{escape(" · ".join(extras))}</div>' if extras else "")
             + (f'<div style="font-size:12px;color:{FLAG_COLORS["attention"]};margin-top:6px">기기 기록을 다 받지 못해 일부는 지난 동기화 값입니다.</div>'
                if act.get("stale") else "")
             + f'<div style="font-size:11px;line-height:1.5;color:{MUTED};margin-top:8px">기준 출처: {escape("; ".join(sources))}. '
               f'● 좋음 · ▲ 보통 · ■ 주의 · ○ 자료 없음. 기준선 비교(심박·회복)는 본인의 지난 4주 평균입니다.</div>')
    return _card("지난주 몸 상태", lead, marks)


def _review(data: dict[str, Any], prose: dict[str, Any]) -> str:
    previous = data["previous_actions"]
    if not previous:
        return ""
    items = "".join(f'<li style="margin:4px 0">{escape(a.get("what", ""))} — {escape(a.get("amount", ""))}</li>' for a in previous)
    lead = prose.get("review_line") or f"{escape(str(data['previous_week']))} 레터의 행동입니다. 어떻게 되었는지 답장으로 알려 주시면 다음 주에 반영합니다."
    return _card("지난주 행동 점검", lead, f'<ul style="margin:0;padding-left:18px;font-size:14px;color:{BODY}">{items}</ul>')


def _follow_ups(data: dict[str, Any]) -> str:
    rows = []
    for item in data["follow_ups"]:
        when = f"D-{item['days_left']}" if item["days_left"] >= 0 else f"{-item['days_left']}일 지남"
        rows.append(f'<tr><td style="padding:5px 8px 5px 0;font-family:{MONO};font-size:12px;color:{BODY};white-space:nowrap">{item["due"]:%Y.%m.%d}</td>'
                    f'<td style="padding:5px 8px 5px 0;font-size:12px;color:{MUTED};white-space:nowrap">{when}</td>'
                    f'<td style="padding:5px 0;font-size:14px;color:{BODY}"><b>{escape(item["label"])}</b> · {escape(item["summary"])}</td></tr>')
    checkup = data["next_checkup"]
    if checkup:
        rows.append(f'<tr><td style="padding:5px 8px 5px 0;font-family:{MONO};font-size:12px;color:{BODY}">{checkup:%Y.%m.%d}</td>'
                    f'<td style="padding:5px 8px 5px 0;font-size:12px;color:{MUTED}">예상</td>'
                    f'<td style="padding:5px 0;font-size:14px;color:{BODY}">건강검진(지난 검진 1년 뒤)</td></tr>')
    if not rows:
        return ""
    return _card("다가오는 검사", "보고서가 적은 추적 간격으로 계산한 날짜입니다. 예약은 고객님이 하시고, 날짜가 다르면 답장으로 알려 주십시오.",
                 f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{"".join(rows)}</table>')


def _boundary(data: dict[str, Any]) -> str:
    from smith.health_letter import BOUNDARY
    lines = [BOUNDARY, "이 메일에 답장하면 건강 기록만으로 답합니다(재무·일정 자료는 들어가지 않습니다). 가슴 통증·호흡 곤란·마비 같은 "
             "증상은 메일이 아니라 119·응급실입니다."]
    if data["failures"]:
        lines.append("이번 주 받지 못한 자료: " + ", ".join(f"{k}({v})" for k, v in sorted(data["failures"].items())))
    return ('<tr><td style="padding:4px 6px 12px">'
            + "".join(f'<div style="font-size:12px;line-height:1.6;color:{MUTED};margin-bottom:6px">{escape(line)}</div>' for line in lines)
            + "</td></tr>")


def _text(data: dict[str, Any], prose: dict[str, Any], subject: str) -> str:
    lines = [subject, "", _plain(prose.get("verdict") or _code_verdict(data)), ""]
    goal_lines = prose.get("goal_lines") or {}
    for goal in data["goals"]:
        status = goal["status"]
        if status:
            o = status["latest"]
            lines.append(f"- {goal['label']}: {o['text']} {o['unit']} ({o['on']:%Y.%m}, {status['level_word']}, {status['trend_word']})")
        elif goal["posture"]:
            lines.append("- 자세: " + ", ".join(f"{p['label']} {p['latest']['text']}{p['unit']}" for p in goal["posture"]))
        guide = goal_lines.get(goal["key"]) or {"do": goal["do"], "avoid": goal["avoid"]}
        lines.append(f"  하기: {guide['do']} / 피하기: {guide['avoid']}")
    lines.append("")
    item_lines = prose.get("items") or {}
    for item in data["items"]:
        lines.append(f"- {item['label']} {item['latest']['text']} {item['unit']} [{item['level_word']}, {item['trend_word']}] "
                     f"{item_lines.get(item['code']) or _code_line(item)}")
    lines.append("")
    for number, action in enumerate(prose.get("actions") or data["fallback_actions"], 1):
        lines.append(f"{number}. {action['what']} — {action['amount']} (왜: {action['why']}; 확인: {action['check']})")
    for item in data["follow_ups"]:
        lines.append(f"- {item['label']} 추적 {item['due']:%Y.%m.%d}")
    from smith.health_letter import BOUNDARY
    lines += ["", BOUNDARY]
    return "\n".join(lines)
