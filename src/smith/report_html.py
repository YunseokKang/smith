"""Email-safe HTML rendering of report data (docs/report-design.md §4, §10.2, §11).

Gmail runs no scripts, so every chart is an inline-styled HTML table: stat tiles, emphasis bars
(one accent, the rest neutral), a zero-centred diverging bar for changes and a timeline. Each chart
has a plain table twin. State is always icon + label + color, except the mail's top line, which carries only the
action (v3.1, report design §21; the subject names the state).

Visual language follows docs/design-reference.md (style only, no brand marks): white canvas, one
blue accent used scarcely, display type at weight 400 with negative tracking, tabular monospace
numbers, 24px card radius, pill badges, and a light/dark band rotation: dark hero (date, key numbers,
the one most important message) -> white proposals -> dark strategy -> white sections -> soft footer.
Chart colors were checked with the dataviz validator on white: increases red and decreases blue
(the Korean market convention), always with an arrow and a sign.

v3.0 (report design §18, user review 2026-10-18): body text in near-black ink (the grey ink was hard to
read), the status line names today's action, a composition bar under the judgement, every section is a
collapsible <details> card (the hero, the proposals and a theme's opening section stay open), charts come
before their tables, opinions and checks are colour pills, and the mail ends with one line: the status
reasons, data status and sources moved out of the mail into `system_notes` (for the e-mail answers).
"""
import re
from decimal import Decimal
from html import escape
from typing import Any

from smith.fmt import short_won as _short
from smith import editions, picks, principles, stress
from smith.holdings import STANCES
from smith.proposals import Track
from smith.realestate import MIN_BASIS
from smith.report_data import CATEGORY_LABELS, CERTAINTY_LABELS, COMPONENT_LABELS, GROUP_LABELS, PAY_LABELS

# Design tokens (docs/design-reference.md).
CANVAS, SOFT, STRONG, DARK, DARK_RAISED = "#ffffff", "#f7f7f7", "#eef0f3", "#0a0b0d", "#16181c"
BLUE, HAIRLINE = "#0052ff", "#dee1e6"
# Text is near-black on white and white on dark: the grey body ink of the design reference read poorly on
# a phone (user review 2026-10-18). Hierarchy comes from size and weight, not from fading the ink.
INK, BODY, MUTED, MUTED_SOFT, ON_DARK, ON_DARK_SOFT = "#0a0b0d", "#111111", "#3c4043", "#3c4043", "#ffffff", "#ffffff"
SANS = "Inter,Pretendard,'Malgun Gothic','Apple SD Gothic Neo',-apple-system,'Segoe UI',Roboto,sans-serif"
# Numbers are tabular; Hangul units fall back to the system font.
MONO = "'JetBrains Mono','Geist Mono',Consolas,'Malgun Gothic','Apple SD Gothic Neo',monospace"
# Chart colors (validated): accent and the neutral rest; diverging poles.
ACCENT, MUTED_BAR, UP, DOWN = BLUE, "#a8acb3", "#cf202f", BLUE
STATUS = {"stable": ("●", "안정", "#05b169"), "watch": ("▲", "주의", "#f4b000"), "act": ("■", "대응 필요", "#ff5c66")}
# Pill badges (user spec 2026-10-18): green = favourable or met, grey = keep or planned, orange = partial exit
# or check, red = full exit or breached. (background, ink, weight); the ink reads on its background.
PILLS = {"good": ("#e6f4ea", "#137333", 700), "neutral": ("#eef0f3", "#3c4043", 600),
         "warn": ("#fef7e0", "#b06000", 700), "bad": ("#fce8e6", "#c5221f", 700)}
# A collapsible card's summary: a soft, rounded button (user spec 2026-10-18, report design §20).
SUMMARY = ("display:block;list-style:none;cursor:pointer;outline:none;background:#f8f9fa;padding:16px 20px;"
           f"border-radius:16px;font-family:{SANS};font-weight:600;color:#0a0b0d;")
STANCE_PILL = {"add": "good", "hold": "neutral", "trim": "warn", "exit": "bad"}
CHECK_PILL = {"ok": "good", "plan": "neutral", "watch": "warn", "breach": "bad", "unknown": "neutral"}
# Stacked-bar segments in a fixed order (hues follow the entity, never cycled; dataviz validator, light
# surface, 2026-10-18: lightness, chroma, CVD and normal-vision checks pass).
SEGMENTS = ("#0052ff", "#b86e00", "#0a8f5c", "#7c3aed", "#c43d7a")
TRACK_STATUS = {"on_track": ("●", "궤도 위", "#05b169"), "attention": ("▲", "보완 필요", "#f4b000"),
                "unknown": ("○", "판단 불가", ON_DARK_SOFT)}
PRIORITY_LABELS = {1: "지금 실행", 2: "이번 달·분기", 3: "유지·점검"}
MAX_TAX_NOTES = 4  # The tax strategy notes in the mail (user review 2026-10-18: a quarter of the text).
SERIES_LABELS = {
    "722Y001/0101000": "한국은행 기준금리", "817Y002/010200000": "국고채 3년", "817Y002/010210000": "국고채 10년",
    "817Y002/010502000": "CD 91일", "731Y001/0000001": "원/달러 환율", "121Y006/BECBLA0302": "은행 주담대 금리(신규취급 평균)",
    "DFEDTARU": "미국 기준금리(상단)",
    "DFF": "미국 실효 기준금리", "DGS2": "미국 국채 2년", "DGS10": "미국 국채 10년", "MORTGAGE30US": "미국 30년 주택대출",
}
EXPOSURE_LABELS = {
    "variable_rate_debt": "변동금리 대출", "krw_cash_and_deposits": "원화 현금·예금", "usd_assets": "달러 자산",
    "equities_and_unclassified_securities": "주식·구성 미상 증권", "real_estate": "부동산",
    "lease_deposit_due": "전세보증금 반환(만기 있음)",
}
SCENARIO_LABELS = {"base": "기준", "favorable": "유리", "adverse": "불리"}
RATE_TYPE_LABELS = {"fixed": "고정", "variable": "변동", "mixed": "혼합", "unknown": "미확인"}
SOURCE_LABELS = {"toss": "토스증권", "ecos": "한국은행 ECOS", "fred": "미국 FRED", "fed-monetary": "Fed 통화정책 발표",
                 "bok-press-conference": "한국은행 총재 기자간담회", "bok-mpb-minutes": "금통위 의사록"}
STRESS_LEASE_FLAG = "lease_deposit_uncovered"  # stress.LEASE_UNCOVERED
STRESS_TITLE = "스트레스 테스트: 금리 상승·소득 중단·집값·주가 하락이 함께 오면"
# Concrete labels (user review 2026-10-18): what the verdict means, not 취약/빠듯.
STRESS_VERDICT = {"robust": ("●", "현금만으로 버팀", "#05b169"), "watch": ("▲", "현금만으로 6개월 이상", "#f4b000"),
                  "fragile": ("■", "현금만으로 6개월 미만", "#ff5c66"), "unknown": ("?", "판단 불가", "#7c828a")}


def render(data: dict[str, Any]) -> tuple[str, str]:
    """Return (subject, html). A daily edition (smith.editions) renders its own tiles, proposals and
    sections; the original kinds are the full Monday report and the short Thursday check."""
    edition = editions.of(data["kind"])
    if edition is not None:
        return _render_edition(data, edition)
    status, reasons = _status(data)
    full = data["kind"] != "thursday"
    advice = data.get("advice") or {"proposals": [], "strategy": [], "assumptions": []}
    story = data.get("narrative") or {}
    bands = [_band(DARK, _header(data, action=_action(advice, status), pending=len(advice.get("undetermined") or []))
                   + _judgement(story)
                   + _composition_bar(data, None) + _overview(data)),
             _band(CANVAS, _proposals(advice, full), border=True)]
    changes = _changes_outside(story)
    if changes:
        bands.append(_band(CANVAS, changes, border=True))
    if full and advice["strategy"]:
        bands.append(_band(DARK, _strategy(advice["strategy"], story.get("direction", ""))))
    sections = [_follow_up(advice), _tax(advice, data, full), _change(data), _timeline(data)]
    if full:
        sections.append(_future_income(data))
        sectors = data["sectors"]
        sections += [_allocation(data), _cash(sectors["cash"], data),
                     _spending(advice.get("spending_room"), data.get("spending_split")),
                     _stress(data["stress"], sectors["real_estate"]), _debt(sectors["debt"], advice.get("refinance"), sectors["macro"].get("policy")),
                     _policy(data, story, full), _holdings(data, story, full),
                     _real_estate(sectors["real_estate"], data.get("figures") or {}), _move_targets(data),
                     _lease_plan(data), _pension_stack(data), _annuity_review(data), _pension(sectors["pension_insurance"]),
                     _macro(sectors["macro"]), _experiences(story)]
    else:
        sections += [_policy(data, story, full), _holdings(data, story, full)]
    sections.append(_requests(data.get("requests") or [], full))
    bands.append(_band(CANVAS, "".join(sections), border=True))
    bands.append(_band(SOFT, _footer(_system_warnings(data))))
    return _subject(data, status), _page(bands)


def render_event(note: dict[str, Any]) -> tuple[str, str, str]:
    """Return (subject, html, text) of an event note (report design §29): the hero names the event and what it
    means for the household in one line, four tiles carry the figures, then what happened and our numbers, the
    researched why with its sources, and Smith's stance. Never a proposal: those belong to the regular reports."""
    subject, title, line, tiles, facts, stance = (_bok_event(note) if note["kind"] == "bok_decision"
                                                  else _move_event(note))
    hero = (f'<tr><td>{_eyebrow("SMITH · 사건 안내", color=ON_DARK, background=DARK_RAISED)}'
            f'<div style="font-family:{SANS};font-weight:400;font-size:28px;line-height:1.15;letter-spacing:-0.8px;'
            f'color:{ON_DARK};margin:4px 0 12px">{escape(title)}</div>'
            f'<div style="font-size:14px;line-height:1.5;color:{ON_DARK}">{note["as_of"]:%Y년 %m월 %d일 %H:%M} 기준 · '
            f'{escape(line)}</div></td></tr>' + _tile_grid(tiles))
    why = _event_research(note)
    body = (_opened(_section("무엇이 일어났고 우리 집에는", facts[0], facts[1], brief=None))
            + (_opened(_section("왜 그랬나 (조사)", why[0], why[1], brief=None)) if why else "")
            + _opened(_section("Smith의 입장", stance, "", brief=None)))
    guide = "정기 보고와 별개로, 우리 집에 중요한 사건이 있는 날에만 보내는 안내입니다. 제안은 정기 보고에서만 드립니다."
    html = _page([_band(DARK, hero), _band(CANVAS, body, border=True), _band(SOFT, _footer((), week=guide))])
    text = (f"{subject}\n\n{title}\n{line}\n\n{_plain(facts[0])}\n\n{_plain(stance)}\n\n"
            "이 안내는 HTML 형식입니다. HTML을 지원하는 메일 앱에서 확인하시기 바랍니다.")
    return subject, html, text


def _plain(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html)


def _rate_text(value: Decimal) -> str:
    return f"{value.normalize():f}%"


def _bok_event(note: dict[str, Any]) -> tuple[str, str, str, list[tuple[str, str, str]], tuple[str, str], str]:
    d = note["decision"]
    label = {"hold": "동결", "hike": f"{d['change']:+.2f}%p 인상", "cut": f"{d['change']:+.2f}%p 인하"}[d["outcome"]]
    rate, monthly, step = _rate_text(d["rate_after"]), note["monthly_change"], note["step"]
    ours = ("변동금리 대출이 없어 우리 집 월 이자는 바뀌지 않습니다" if not note["variable_total"] else
            "우리 집 월 이자는 바뀌지 않습니다" if not monthly else
            f"변동금리 대출 월 이자가 다음 재산정부터 약 {_signed_short(monthly)} 바뀝니다")
    subject = (f"[Smith · 사건 안내] {note['day']:%m/%d} 금통위 {label} {rate} · "
               + ("월 이자 변화 없음" if not monthly else f"월 이자 {_signed_short(monthly)}"))
    # The vote and the guidance are shown only as the Bank's page states them (review 2026-10-23): otherwise left out.
    vote = {True: "만장일치", False: "소수 의견 있음", None: ""}[d["vote_unanimous"]]
    source = "한국은행 ECOS 통계" if d["source"] == "ecos" else "한국은행 공식 페이지"
    next_meeting = note["next_meeting"]
    tiles = [("기준금리", rate, f"{label} · 이전 {_rate_text(d['rate_before'])}" + (f" · {vote}" if vote else "")),
             ("변동금리 대출", _short(note["variable_total"]) if note["variable_total"] else "없음", "잔액"),
             ("월 이자 변화", "변화 없음" if not monthly else _signed_short(monthly), "현재 잔액 기준 · 재산정 때 반영"),
             ("다음 금통위", "-" if next_meeting is None else f"{next_meeting.month}월 {next_meeting.day}일",
              "" if next_meeting is None else f"({editions.WEEKDAY_KO[next_meeting.weekday()]})"
              + (f" · 0.25%p당 월 {_short(step)}" if step else ""))]
    lead = (f"{note['day']:%m월 %d일} 한국은행 금융통화위원회가 기준금리를 {rate}로 {label}했습니다"
            f"({vote + ', ' if vote else ''}출처: {source}). {ours}."
            + (" 배경과 총재 발언의 조사는 이번에 실패해 결정 숫자만 전합니다." if note.get("research_failed") else ""))
    rows = ([["결정", f"{_rate_text(d['rate_before'])} → {rate} ({label})"]] + ([["표결", vote]] if vote else [])
            + ([["향후 방향(한국은행 원문)", f"“{d['guidance']}”"]] if d.get("guidance") else []) + [
            ["변동금리 대출 잔액", _short(note["variable_total"]) if note["variable_total"] else "없음"],
            ["월 이자 변화(현재 잔액 기준)", "변화 없음" if not monthly else _signed_short(monthly)],
            ["0.25%p 한 단계의 월 이자 차이", _short(step) if step else "-"]])
    stance = {
        "hold": ("이번 결정은 우리 집 월 이자를 바꾸지 않습니다. 미리 갚는 방식과 대환 판단은 정기 보고의 기준 그대로이고, 다음 "
                 "금통위 전에 대출 금리 재산정 통지가 오면 그 숫자로 다시 봅니다. 결정 자체로 매매하거나 상환 계획을 바꾸지 않습니다."),
        "hike": (f"변동금리 대출 월 이자가 다음 재산정부터 약 {_signed_short(monthly) if monthly else '0원'} 바뀝니다. 미리 갚을지는 "
                 "대출 금리와 세후 예금 금리의 차이로 판단하는 정기 보고의 기준 그대로이며, 목요 금리·대출 보고에서 새 금리로 "
                 "시나리오를 다시 계산합니다."),
        "cut": (f"변동금리 대출 월 이자가 다음 재산정부터 약 {_signed_short(monthly) if monthly else '0원'} 바뀝니다. 금리가 내리면 "
                "예금 이자도 함께 낮아지므로 미리 갚을지는 두 금리의 차이로 판단하며, 목요 금리·대출 보고에서 새 금리로 시나리오를 "
                "다시 계산합니다."),
    }[d["outcome"]]
    return subject, f"금통위, 기준금리 {rate} {label}", ours, tiles, (lead, _table(["항목", "내용"], rows)), stance


def _move_event(note: dict[str, Any]) -> tuple[str, str, str, list[tuple[str, str, str]], tuple[str, str], str]:
    ratio, change, since = note["ratio"], note["change"], note["since"]
    pct = f"{ratio * 100:+.1f}%"
    movers = note["movers"]
    top = movers[0] if movers else None
    by_market = note["by_market"]
    markets = " · ".join(f"{label} {_signed_short(by_market[key])}" for key, label in (("KR", "한국 상장"), ("US", "미국 상장"))
                         if key in by_market)
    subject = f"[Smith · 사건 안내] {note['day']:%m/%d} 보유 종목 {pct} ({_signed_short(change)})"
    line = f"{since:%m.%d %H:%M} 이후 {_signed_short(change)} · 시장 손익 {_signed_short(note['market'])} · 환율 {_signed_short(note['fx'])}"
    tiles = [("보유 종목 변동", _signed_short(change), f"{since:%m.%d %H:%M} 이후 · 평가액 {_short(note['start'])} 기준"),
             ("변동률", pct, "시장 손익과 환율 효과"),
             ("가장 크게 움직인 종목", str(top["name"])[:12] if top else "-",
              f"{_signed_short(top['change'])} ({top['ratio'] * 100:+.1f}%)" if top else ""),
             ("스트레스 가정 대비", f"{note['stress_share'] * 100:.0f}%", "주식 30% 하락 가정의 비율")]
    lead = (f"지난 점검({since:%m월 %d일 %H:%M}) 이후 보유 종목 평가액이 {_signed_short(change)}({pct}) 움직였습니다. 시장 손익이 "
            f"{_signed_short(note['market'])}, 환율 효과가 {_signed_short(note['fx'])}입니다." + (f" {markets}." if markets else ""))
    rows = [[str(m["name"]), _signed_short(m["change"]), f"{m['ratio'] * 100:+.1f}%"] for m in movers]
    stance = (f"하루 변동으로 매매하지 않습니다. 이번 변동은 Smith의 스트레스 테스트가 가정하는 주식 30% 하락의 "
              f"{note['stress_share'] * 100:.0f}%입니다. 정기 보고의 제안(분기 정리 일정, 날짜 있는 목표 자금의 이동)은 그대로이고, "
              "종목별 판단은 다음 화요 투자·시장 보고에서 다시 봅니다.")
    return (subject, f"보유 종목 {pct} ({since:%m.%d} 이후)", line, tiles,
            (lead, _table(["종목", "변동", "변동률"], rows) if rows else ""), stance)


def _event_research(note: dict[str, Any]) -> tuple[str, str] | None:
    """The researched background of an event: each finding with its source link, dated, marked 공식 or 참고."""
    items = [i for i in note.get("items") or [] if not i.get("decision_source")]
    if not items:
        return (("원인 조사는 이번에 실패했습니다. 숫자는 Smith의 계산이며, 배경은 다음 정기 보고에서 다룹니다.", "")
                if note.get("research_failed") else None)
    lead = f"조사에서 확인한 배경 {len(items)}건입니다. 공식 출처는 '공식', 언론·기관 자료는 '참고'로 표시했습니다."
    rows = []
    for item in items:
        official = item.get("tier") == "official"
        rows.append(f'<div style="margin:10px 0"><div style="font-size:14px;font-weight:600;line-height:1.5;color:{INK}">'
                    f'<a href="{escape(item["source_url"])}" style="color:{INK};text-decoration:underline">{escape(item["headline"])}</a> '
                    f'{_pill("공식" if official else "참고", "good" if official else "neutral")} '
                    f'<span style="font-family:{MONO};font-size:12px;color:{MUTED}">{escape(item["published_on"])}</span></div>'
                    f'<div style="font-size:13px;line-height:1.6;color:{BODY}">{escape(item["detail"])}</div></div>')
    return lead, "".join(rows)


def system_notes(data: dict[str, Any]) -> list[str]:
    """Everything the mail no longer prints (report design §18.1): the status reasons, verification memos,
    input hints, data coverage and freshness, assumptions and the AI stages. Kept for the e-mail answers
    ("왜 이렇게 판단했어?") and the preview command."""
    edition = editions.of(data["kind"])
    story = data.get("narrative") or {}
    advice = data.get("advice") or {}
    _, reasons = _status(data, requests_shown=edition is None or "requests" in edition.sections)
    return (_system_notes(data, story, reasons) + picks.notes(data.get("product_reviews") or {})
            + _data_lines(data, advice.get("assumptions") or [], story.get("status")))


def _page(bands: list[str]) -> str:
    spacer = '<tr><td style="height:12px;line-height:12px;font-size:0">&nbsp;</td></tr>'
    # The expand hint of a collapsible card follows its state where <style> is honoured. The "open" hint is
    # hidden inline, so a client without <style> or <details> (Gmail) shows the card open with one hint
    # (▼ 펼쳐서 상세 보기), never both; here the stylesheet swaps them with !important over the inline style.
    style = ('<style>details>summary{list-style:none}details>summary::-webkit-details-marker{display:none}'
             'details[open]>summary .mk-open{display:inline!important}'
             'details[open]>summary .mk-closed{display:none!important}</style>')
    return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>Smith 보고서</title>{style}</head>'
            f'<body style="margin:0;padding:0;background:{CANVAS};font-family:{SANS};color:{INK};word-break:keep-all;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{CANVAS}">'
            f'<tr><td align="center" style="padding:12px 4px">'
            f'<table role="presentation" width="640" cellpadding="0" cellspacing="0" style="width:100%;max-width:640px">'
            f'{spacer.join(bands)}</table></td></tr></table></body></html>')


def _render_edition(data: dict[str, Any], edition: editions.Edition) -> tuple[str, str]:
    """One themed day: the hero with that day's numbers and the one message, its proposals, the long-range
    goals on the overview and household days, then only that day's sections. Short by construction."""
    status, reasons = _status(data, requests_shown="requests" in edition.sections)
    advice = data.get("advice") or {"proposals": [], "strategy": [], "assumptions": []}
    story = data.get("narrative") or {}
    overview = edition.key == "overview"
    # No "today's one thing" box under the tiles (user review 2026-10-18): the badge names the action and the
    # first proposal card, open right below, explains it.
    hero = (_header(data, action=_action(advice, status), pending=len(advice.get("undetermined") or []))
            + _judgement(story)
            + _composition_bar(data, edition) + _tile_grid(_tiles(data, edition)))
    bands = [_band(DARK, hero)]
    sectors = data["sectors"]
    renderers = {
        "follow_up": lambda: _follow_up(advice), "change": lambda: _change(data),
        "allocation": lambda: _allocation(data), "timeline": lambda: _timeline(data),
        "goals": lambda: _goals_brief(data), "future_income": lambda: _future_income(data, overview=True),
        "requests": lambda: (_requests(data.get("requests") or [], False, "생활·목표 보고") if overview
                             else _requests(data.get("requests") or [], True)),
        "holdings": lambda: _holdings(data, story, True), "policy": lambda: _policy(data, story, True),
        "macro": lambda: _macro(sectors["macro"]), "macro_brief": lambda: _macro(sectors["macro"], compact=True),
        "real_estate": lambda: _real_estate(sectors["real_estate"], data.get("figures") or {}),
        "lease_plan": lambda: _lease_plan(data), "move_targets": lambda: _move_targets(data),
        "debt": lambda: _debt(sectors["debt"], advice.get("refinance"), sectors["macro"].get("policy")),
        "tax": lambda: _tax(advice, data, True),
        "pension": lambda: _pension(sectors["pension_insurance"], advice.get("insurance_review")),
        "pension_stack": lambda: _pension_stack(data), "annuity_review": lambda: _annuity_review(data),
        "cash": lambda: _cash(sectors["cash"], data),
        "spending": lambda: _spending(advice.get("spending_room"), data.get("spending_split")),
        "stress": lambda: _stress(data["stress"], sectors["real_estate"]),
        "stress_summary": lambda: _stress_summary(data["stress"], sectors["real_estate"]),
        "experiences": lambda: _experiences(story),
    }
    # The theme's opening view (the loan line-up, the pension stack, the move targets) comes right under the
    # hero and open; everything after the proposals is collapsed (report design §18.1).
    opening = "".join(_opened(renderers[name]()) for name in edition.opening)
    if opening:
        bands.append(_band(CANVAS, opening, border=True))
    if advice["proposals"] or advice.get("undetermined") or overview:
        bands.append(_band(CANVAS, _proposals(advice, not overview, edition=edition), border=True))
    title = "이번 주 눈여겨볼 변화" if overview else f"{edition.label} — 눈여겨볼 변화"
    changes = _changes_outside(story, title)
    if changes:
        bands.append(_band(CANVAS, changes, border=True))
    if edition.strategy and advice["strategy"]:
        bands.append(_band(DARK, _strategy(advice["strategy"], story.get("direction", ""))))
    sections = [renderers[name]() for name in edition.sections if name not in edition.opening]
    if any(sections):
        bands.append(_band(CANVAS, "".join(sections), border=True))
    bands.append(_band(SOFT, _footer(_system_warnings(data), week=_week_line(data))))
    return _subject(data, status), _page(bands)


def _opened(section: str) -> str:
    """A section rendered open (the first <details> in it)."""
    return section.replace("<details style", "<details open style", 1)


def _action(advice: dict[str, Any], status: str) -> str:
    """The short badge beside the status (report design §20): the first proposal's headline (its title only when
    a rule has none), or which report explains another area's urgent proposal. Never the system reasons."""
    proposals = advice.get("proposals") or []
    if proposals:
        return proposals[0].headline or proposals[0].title
    elsewhere = [editions.home(k) for k in advice.get("household_urgent", [])]
    if status == "act" and any(elsewhere):
        return " · ".join(e.label for e in elsewhere if e) + " 보고의 제안 확인"
    return ""


def _tiles(data: dict[str, Any], edition: editions.Edition) -> list[tuple[str, str, str]]:
    """The four numbers of the day, all computed in report_data (title, value, note)."""
    k, s, f = data["kpis"], data["sectors"], data["figures"]

    def amount(value: Decimal | None) -> str:
        return "미상" if value is None else _short(value)
    if edition.tiles == "markets":
        review = data["holdings"]
        listed = [p for p in review["positions"] if not p["hermes"]]
        top = listed[0]["name"] if listed else ""
        partial = f" · 원가 아는 {f['listed_gain_counted']}개만" if f["listed_gain_missing"] else ""
        return [("보유 종목 평가액", amount(f["listed_value"]), f"{f['listed_count']}개 종목 · Hermes 운용분 제외"),
                ("미실현 손익", "미상" if f["listed_gain"] is None else _signed_short(f["listed_gain"]),
                 "팔지 않아 확정되지 않은 손익" + partial),
                ("해외 상장 비중", _pct(review["overseas_share"], 0), "보유 증권 전체 기준"),
                ("가장 큰 종목 비중", _pct(review["top1_share"], 0), str(top)[:16])]
    if edition.tiles == "property":
        estate = s["real_estate"]
        lease = next((t for t in data["timeline"] if t.get("category") == "lease_deposit_obligation"), None)
        basis = " · ".join(f.get("property_valuation") or []) or "원장 입력가"
        if f.get("property_estimate") is not None and f["property_value"] is not None:
            basis += f" · 실거래 중간값 합계 {amount(f['property_estimate'])}({_signed_short(f['property_estimate'] - f['property_value'])})"
        elif f.get("property_estimate_reference") is not None and f["property_value"] is not None:
            reference = f["property_estimate_reference"]
            basis += f" · 실거래 중간값 합계 {amount(reference)}({_signed_short(reference - f['property_value'])}, 참고)"
        return [("부동산 평가액", amount(f["property_value"]), f"자산의 {_pct(estate['share_of_assets'], 0)} · 기준: {basis}"),
                ("부동산 관련 부채", amount(f["property_debt"]), "담보대출·보증금 반환"),
                ("돌려드릴 보증금", "-" if lease is None else amount(lease["amount"]),
                 "" if lease is None else f"{lease['date']:%Y.%m} 만기"),
                ("최근 6개월 실거래", f"{f['property_trades_6m']}건", _deals_stale(estate) or "같은 단지·같은 면적")]
    if edition.tiles == "rates":
        debt = s["debt"]
        one = next((x for x in debt["shocks"] if x["shock"] == Decimal("0.01")), None)
        base = next((r for r in s["macro"]["evidence"] if r["spec"].series_id == "722Y001/0101000" and r["value"] is not None),
                    None)
        policy = s["macro"].get("policy") or {}
        meeting = policy.get("next_meeting")
        when = "" if base is None else f"{base['observed_on']:%m.%d} 기준"
        if meeting is not None:  # The rate-setting day matters more than the observation day (§28).
            when += (" · " if when else "") + ("오늘 금통위" if policy.get("meeting_today") else f"다음 금통위 {meeting:%m.%d}")
        return [("변동금리 대출", "미상" if debt["variable_total"] is None else _short(debt["variable_total"]), "잔액"),
                ("월 이자(추정)", "미상" if debt["monthly_interest_estimate"] is None
                 else _short(debt["monthly_interest_estimate"]), "현재 금리 기준"),
                ("금리 1%p 오르면", "-" if one is None or one["monthly_change"] is None
                 else "+" + _short(one["monthly_change"]), "월 이자 증가 · 내리면 같은 폭 감소"),
                ("한국은행 기준금리", "미상" if base is None else f"{base['value']}%", when)]
    if edition.tiles == "tax":
        plan = data["tax_plan"]
        room = (("세액공제 남은 한도", "한도 채움", "올해는 더 넣어도 공제가 늘지 않습니다") if not plan["irp_room"] else
                ("세액공제 남은 한도", _short(plan["irp_room"]), f"채우면 약 {_short(plan['refund_gain'])} 더 환급(추정)"))
        lower = f" · 환산 못 한 {plan['pension_unconverted']}건 제외(최소치)" if plan["pension_unconverted"] else ""
        stack = data.get("pension_stack")
        others = [] if stack is None else [t for t in _retirement_tiers(stack) if t["retire_age"] != stack["retirement_age"]]
        first = (("올해 연금 납입(예상)", _short(plan["savings_paid"] + plan["irp_paid"]), "연금저축 + IRP" + lower) if stack is None
                 else ("노후 준비율(4대 연금 세후)", _pct(stack["ratio"], 0),
                       f"{stack['retirement_age']}세 은퇴 기준 · "
                       + " · ".join(f"{t['retire_age']}세 {_pct(t['ratio'], 0)}" for t in others)
                       + (f" · {len(stack['unknown'])}개 미상 제외" if stack["unknown"] else "")))
        return [first,
                room,
                ("공제받지 못하는 납입", _short(plan["excess"]), "한도를 넘어 세액공제가 없는 금액"),
                ("해외주식 미실현 이익", _short(plan["us_gains"]) if plan["us_positions"] else "-",
                 "연 250만 원까지 공제"
                 + (f" · 원가 모르는 {plan['us_unknown']}종목 제외" if plan["us_unknown"] else ""))]
    if edition.tiles == "life":
        summary = data["view"].summary
        # Without an FX rate for some flow the monthly totals are partial: shown, but never as complete.
        partial = " · 일부 미상" if "cash_flow" in summary.incomplete_areas else ""
        months = "미상" if k["immediate_months"] is None else f'월 지출 {k["immediate_months"]:.1f}개월분'
        return [("월 수입", _short(summary.monthly_inflow), "반복 수입" + partial),
                ("월 지출", _short(summary.monthly_outflow), "대출 상환·보험 포함" + partial),
                ("월 적립·투자", _short(summary.monthly_transfers), "본인 계좌로 옮기는 돈" + partial),
                ("남는 돈", amount(f["monthly_left"]), f"즉시 쓸 수 있는 돈: {months}")]
    return _overview_tiles(data)


def _status(data: dict[str, Any], *, requests_shown: bool = True) -> tuple[str, list[str]]:
    """(status, reasons). 대응 필요: a proposal to act on now. 주의: data that failed, is incomplete or
    stale, or a missing input the judgements need (a "must" request). 안정 only when none of these hold.

    The status is household-wide on every daily edition: an urgent proposal of another area still makes
    it 대응 필요 (and says where it is explained), and a judgement this day could not make makes it at
    least 주의. `requests_shown`: whether this report lists the requests itself."""
    advice = data.get("advice") or {}
    here = [p for p in advice.get("proposals") or [] if p.priority == 1]
    elsewhere = [k for k in advice.get("household_urgent", []) if k not in {p.key for p in here}]
    themed = editions.of(data["kind"]) is not None
    pending = (advice.get("undetermined") or []) if themed else []
    must = [r for r in data.get("requests") or [] if r.priority == "must"]
    reasons = []
    if here:
        reasons.append(f"지금 실행을 권하는 제안이 {len(here)}건 있습니다.")
    if elsewhere:
        reasons.append(f"다른 분야에 지금 실행을 권하는 제안이 {len(elsewhere)}건 있습니다"
                       f"({', '.join(editions.home(k).label for k in elsewhere if editions.home(k))} 보고에서 자세히).")
    if pending:
        reasons.append(f"자료가 부족해 판단하지 못한 항목이 {len(pending)}건 있습니다(아래 제안 끝).")
    reasons += [f"{warning}." for warning in _system_warnings(data)]
    if must:
        reasons.append(f"판단에 꼭 필요한 자료 {len(must)}건이 비어 있습니다"
                       + ("('다음에 확인할 것')." if requests_shown else "(종합·생활 보고의 '다음에 확인할 것')."))
    stress = data.get("stress") or {}
    if stress.get("liquidity_verdict") == "fragile":
        reasons.append(f"스트레스 테스트에서는 현금만으로 {stress['months_cash']:.1f}개월밖에 버티지 못합니다.")
    if STRESS_LEASE_FLAG in stress.get("flags", ()):
        reasons.append(f"스트레스 테스트에서는 {stress['lease']['due']:%Y년 %m월} 보증금 반환에 필요한 돈이 부족합니다.")
    if here or elsewhere:
        return "act", reasons
    if reasons:
        return "watch", reasons
    return "stable", ["데이터가 최신이고, 판단에 꼭 필요한 자료가 갖춰져 있습니다."]


def _system_warnings(data: dict[str, Any]) -> list[str]:
    """Problems with the report itself, not with the household: data that is incomplete, stale or failed to
    sync, and a narrative that could not be written. They raise the status to at least 주의 and are the one
    line the mail still prints at the bottom (review 2026-10-18; the full reasons are system notes)."""
    found = []
    failed_sync = any(s["last_failure"] and (not s["last_success"] or s["last_failure"] > s["last_success"])
                      for s in data["sync"])
    if not data["completeness"]["complete"] or failed_sync or data["warnings"]:
        found.append("일부 데이터가 불완전하거나 오래되었거나 동기화에 실패했습니다")
    if ((data.get("narrative") or {}).get("status") or {}).get("outcome") == "failure":
        found.append("이번에는 AI 해석을 만들지 못해 계산 결과만으로 작성했습니다")
    return found


def _subject(data: dict[str, Any], status: str) -> str:
    day = data["as_of"].strftime("%m/%d")  # as_of is already in the household timezone.
    edition = editions.title(data["kind"], data["as_of"].date())
    proposals = (data.get("advice") or {}).get("proposals") or []
    change = data["kpis"]["net_worth_change"]
    themed = editions.of(data["kind"]) is not None and data["kind"] != "overview"
    lead = f"제안 {len(proposals[:3])}건" if proposals else (
        "새 제안 없음" if themed else "기준선 보고" if change is None else f"순자산 {_signed_short(change)}")
    return f"[Smith] {day} {edition} · {STATUS[status][1]} · {lead}"


# --- formatting -------------------------------------------------------------------------------------------

def _signed_short(amount: Decimal) -> str:
    return ("+" if amount > 0 else "") + _short(amount) if amount else "변동 없음"


def _won(amount: Decimal | None) -> str:
    return "미상" if amount is None else f"{amount:,.0f}원"


def _pct(ratio: Decimal | None, places: int = 1) -> str:
    return "미상" if ratio is None else f"{ratio * 100:.{places}f}%"


def _band(background: str, inner: str, *, border: bool = False) -> str:
    edge = f"border:1px solid {HAIRLINE};" if border else ""
    return (f'<tr><td style="background:{background};{edge}border-radius:24px;padding:24px 18px 20px">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{inner}</table></td></tr>')


def _heading(text: str, *, color: str = INK, size: int = 24) -> str:
    return (f'<div style="font-family:{SANS};font-weight:400;font-size:{size}px;line-height:1.2;'
            f'letter-spacing:-0.5px;color:{color};margin:0 0 8px">{escape(text)}</div>')


def _eyebrow(text: str, *, color: str = INK, background: str = STRONG) -> str:
    """Pill label above a heading."""
    return (f'<div style="margin-bottom:10px"><span style="display:inline-block;background:{background};color:{color};'
            f'font-size:12px;font-weight:600;border-radius:100px;padding:4px 12px">{escape(text)}</span></div>')


def _section(title: str, lead: str, inner: str, *, note: str = "", brief: int | None = 2) -> str:
    """A collapsible card, closed by default (report design §18.1, §20): the summary is a soft button with the
    title, a one-line `note` of HTML (a pill or a figure) under it and "▼ 펼쳐서 상세 보기" on the right; the lead
    and the body open on tap. A client without <details> (Gmail) shows it open, so nothing is lost. The lead keeps
    its first `brief` sentences (None: the caller already wrote only what matters)."""
    hint = f'<div style="font-size:13px;font-weight:400;color:{MUTED};margin-top:6px">{note}</div>' if note else ""
    lead = lead if brief is None else _brief(lead, brief)
    return (f'<tr><td style="padding:8px 0"><details style="margin:0">'
            f'<summary style="{SUMMARY}">{_expand_hint()}<span style="font-size:17px;line-height:1.4;letter-spacing:-0.3px">'
            f'{escape(title)}</span>{hint}</summary>'
            f'<div style="font-size:15px;line-height:1.6;color:{BODY};margin:12px 0 12px">{lead}</div>{inner}</details></td></tr>')


def _bars(rows: list[tuple[str, Decimal | None, bool]], *, value_text: Any = _short) -> str:
    """Emphasis bar chart: accent for highlighted rows, neutral for the rest, sorted by caller. Unknown
    values have no bar (the table twin shows them as 미상)."""
    rows = [row for row in rows if row[1] is not None]
    peak = max((abs(v) for _, v, _ in rows), default=Decimal(0)) or Decimal(1)
    cells = []
    for label, value, strong in rows:
        width = max(1, int(abs(value) / peak * 100))
        color = ACCENT if strong else MUTED_BAR
        cells.append(f'<tr><td style="width:160px;font-size:13px;color:{BODY};padding:4px 8px 4px 0;white-space:nowrap">{escape(label)}</td>'
                     f'<td><div style="background:{color};height:12px;width:{width}%;border-radius:0 100px 100px 0"></div></td>'
                     f'<td style="width:100px;text-align:right;font-family:{MONO};font-size:12px;color:{INK};padding-left:8px;'
                     f'white-space:nowrap">{value_text(value)}</td></tr>')
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{"".join(cells)}</table>'


def _paired_bars(rows: list[tuple[str, Decimal | None, Decimal | None]]) -> str:
    """Before and now on one scale: per row a neutral bar (before) above an accent bar (now), each with its
    figure; a missing end has no bar and reads "자료 부족". Empty when no row has both ends."""
    if not any(before is not None and now is not None for _, before, now in rows):
        return ""
    peak = max((abs(v) for _, a, b in rows for v in (a, b) if v is not None), default=Decimal(0)) or Decimal(1)

    def bar(value: Decimal | None, color: str) -> str:
        if value is None:
            return (f'<tr><td style="font-size:11px;color:{MUTED};padding:1px 0">자료 부족</td>'
                    f'<td style="width:90px"></td></tr>')
        width = max(1, int(abs(value) / peak * 100))
        return (f'<tr><td style="padding:1px 0"><div style="background:{color};height:8px;width:{width}%;'
                f'border-radius:0 100px 100px 0"></div></td><td style="width:90px;text-align:right;font-family:{MONO};'
                f'font-size:11px;color:{INK};padding-left:8px;white-space:nowrap">{_short(value)}</td></tr>')
    cells = "".join(f'<tr><td style="width:150px;font-size:12px;color:{BODY};padding:5px 8px 5px 0;vertical-align:middle">'
                    f'{escape(label)}</td><td><table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
                    f'{bar(before, MUTED_BAR)}{bar(now, ACCENT)}</table></td></tr>' for label, before, now in rows)
    legend = (f'<div style="font-size:12px;color:{INK};margin-top:4px"><span style="color:{MUTED_BAR}">●</span> 6개월 전 '
              f'<span style="margin:0 12px">|</span><span style="color:{ACCENT}">●</span> 지금</div>')
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{cells}</table>{legend}'


def _diverging(rows: list[tuple[str, Decimal]], *, value_text: Any = None) -> str:
    """Zero-centred bars: increases to the right in red with ▲ and +, decreases to the left in blue."""
    peak = max((abs(v) for _, v in rows), default=Decimal(0)) or Decimal(1)
    cells = []
    for label, value in rows:
        width = int(abs(value) / peak * 100) if value else 0
        left = (f'<div style="background:{DOWN};height:12px;width:{width}%;margin-left:auto;'
                f'border-radius:100px 0 0 100px"></div>' if value < 0 else "")
        right = (f'<div style="background:{UP};height:12px;width:{width}%;border-radius:0 100px 100px 0"></div>'
                 if value > 0 else "")
        arrow = "▲" if value > 0 else "▼" if value < 0 else "–"
        cells.append(f'<tr><td style="width:160px;font-size:13px;color:{BODY};padding:4px 8px 4px 0;white-space:nowrap">{escape(label)}</td>'
                     f'<td style="width:33%;border-right:1px solid {HAIRLINE}">{left}</td><td style="width:33%">{right}</td>'
                     f'<td style="width:100px;text-align:right;font-family:{MONO};font-size:12px;color:{INK};padding-left:8px;'
                     f'white-space:nowrap">{value_text(value) if value_text else f"{arrow} {_signed_short(value)}"}</td></tr>')
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{"".join(cells)}</table>'


def _table(headers: list[str], rows: list[list[str | tuple[str, str]]]) -> str:
    """The table twin every chart carries; values are exact. A (text, url) cell becomes a source link."""
    head = "".join(f'<th style="text-align:left;font-size:12px;font-weight:600;color:{MUTED};padding:6px;'
                   f'border-bottom:1px solid {HAIRLINE}">{escape(h)}</th>' for h in headers)
    body = "".join("<tr>" + "".join(f'<td style="font-size:12px;padding:6px;color:{BODY};border-bottom:1px solid {STRONG};'
                                    f'{"font-family:" + MONO + ";" if _numeric(c) else ""}'
                                    f'{"white-space:nowrap" if len(_text_of(c)) <= 10 else ""}">{_cell(c)}</td>'
                                    for c in row) + "</tr>" for row in rows)
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:10px">'
            f'<tr>{head}</tr>{body}</table>')


def _numeric(cell: str | tuple[str, str]) -> bool:
    return not isinstance(cell, (tuple, Raw)) and cell[:1] in set("0123456789+-−▲▼")


def _text_of(cell: str | tuple[str, str]) -> str:
    return cell[0] if isinstance(cell, tuple) else cell


def _cell(cell: str | tuple[str, str]) -> str:
    if isinstance(cell, Raw):
        return str(cell)
    if not isinstance(cell, tuple):
        return escape(cell)
    text, url = cell
    # Only a whole, plain https URL becomes a link; anything else (spaces, controls, quotes, angle
    # brackets) stays text rather than being trimmed into a different URL.
    if not url.startswith("https://") or any(ch.isspace() or ord(ch) < 32 or ch in "\"'<>\x7f" for ch in url):
        return escape(text)
    return f'<a href="{escape(url, quote=True)}" style="color:{BLUE};text-decoration:none">{escape(text)}</a>'


# --- top bands --------------------------------------------------------------------------------------------

def _header(data: dict[str, Any], *, action: str = "", pending: int = 0) -> str:
    """Date and the short action badge (report design §18.1, §20). The status label is left out (user review
    2026-10-18: the ⚡ line alone reads better); the status stays in the subject, and its reasons are
    `system_notes` for the e-mail answers. Without an action, judgements stopped by missing data (`pending`) are
    named instead of "nothing to do" (review 2026-10-18: a report that could not judge read as reassurance)."""
    edition = editions.title(data["kind"], data["as_of"].date())
    line = (f'<span style="display:inline-block;background:{DARK_RAISED};color:{ON_DARK};font-weight:600;'
            f'border-radius:100px;padding:4px 12px;margin-top:4px">⚡ 금주 핵심 액션: {escape(action)}</span>'
            if action else f"확정한 실행안은 없고, 자료가 부족해 판단을 보류한 것이 {pending}건 있습니다" if pending
            else "오늘 새로 실행하실 일은 없습니다")
    note = ""
    if data.get("delivery_note"):
        note += (f'<div style="font-size:13px;line-height:1.6;color:{ON_DARK};background:{DARK_RAISED};border-radius:16px;'
                f'padding:12px 14px;margin-top:14px">▲ {escape(data["delivery_note"])}</div>')
    return (f'<tr><td>{_eyebrow(f"SMITH · {edition}", color=ON_DARK, background=DARK_RAISED)}'
            f'<div style="font-family:{SANS};font-weight:400;font-size:30px;line-height:1.15;letter-spacing:-0.8px;'
            f'color:{ON_DARK};margin:4px 0 12px">{data["as_of"]:%Y년 %m월 %d일} 보고</div>'
            f'<div style="font-size:14px;line-height:1.5;color:{ON_DARK}">{line}</div>'
            f'{note}</td></tr>')


def _overview(data: dict[str, Any]) -> str:
    return _tile_grid(_overview_tiles(data))


def _overview_tiles(data: dict[str, Any]) -> list[tuple[str, str, str]]:
    k = data["kpis"]
    months = "미상" if k["immediate_months"] is None else f'월 지출 {k["immediate_months"]:.1f}개월분'
    home = k["home_progress"]
    # The household basis leads (report design §17.3); the client-only figure is in the goals section.
    change = data.get("change") or {}
    groups = change.get("groups")
    # The comparison date leads the note (user review 2026-10-18: "MM/DD 대비" is required).
    since = f"{change['from']:%m/%d} 대비 " if change.get("from") else ""
    change_note = ("기준선" if k["net_worth_change"] is None else
                   since + f"시장·평가 {_signed_short(groups['market_and_valuation'])} · 입출금 {_signed_short(groups['net_inflow'])}"
                   if groups else since + _signed_short(k["net_worth_change"]))
    return [("순자산", _short(k["net_worth"]), change_note),
            ("월 여유 현금흐름", _short(k["monthly_net"]), "수입 − 지출"),
            ("즉시 쓸 수 있는 돈", _short(k["immediate"]), months),
            ("주택 목표 준비율(가구 통합)", "미상" if home is None else _pct(home["ratio_baseline"], 0),
             "" if home is None else f'{home["target_date"]:%Y.%m} · 본인만 {_pct(home["ratio_self"], 0)}')]


def _composition_bar(data: dict[str, Any], edition: editions.Edition | None) -> str:
    """The micro chart under the judgement (report design §18.1): what the headline amounts of this day are
    made of, as one stacked bar with a legend. Each theme shows its own composition."""
    title, segments, note = _hero_segments(data, None if edition is None else edition.tiles)
    if not segments:
        return ""
    bar = _stacked(segments, on_dark=True)
    if not bar:
        return ""
    foot = f'<div style="font-size:12px;color:{ON_DARK};margin-top:6px">{escape(note)}</div>' if note else ""
    return (f'<tr><td style="padding-top:16px"><div style="font-size:12px;font-weight:600;color:{ON_DARK};'
            f'margin-bottom:8px">{escape(title)}</div>{bar}{foot}</td></tr>')


def _hero_segments(data: dict[str, Any], tiles: str | None) -> tuple[str, list[tuple[str, Decimal | None, str]], str]:
    """(title, segments, note) of the hero bar for a tile set: the balance sheet by kind and owner on the
    overview, the holdings on the markets day, the homes, the loans, the pension pipelines, the month."""
    if tiles == "markets":
        listed = [p for p in data["holdings"]["positions"] if not p["hermes"] and p["value"] is not None]
        top, rest = listed[:4], listed[4:]
        segments = [(str(p["name"])[:12], p["value"], "") for p in top]
        if rest:
            segments.append((f"기타({len(rest)}종)", sum((p["value"] for p in rest), Decimal(0)), ""))
        return "보유 종목 구성(Hermes 운용분 제외)", segments, ""
    if tiles == "property":
        props = data["sectors"]["real_estate"]["properties"]
        return "부동산 평가액 구성", [(p["label"], p["value"], "") for p in props], ""
    if tiles == "rates":
        debt = data["sectors"]["debt"]
        drawn = [(l["label"], l["amount"], "") for l in debt["loans"] if l["amount"]]
        unused = debt.get("unused_credit")
        note = f"미사용 한도 {_short(unused)}(마이너스통장)는 부채가 아니라 유사시 바로 쓸 수 있는 여유입니다." if unused else ""
        return "부채 구성", drawn, note
    if tiles == "tax":
        stack = data.get("pension_stack")
        if stack is None:
            return "", [], ""
        known = [p for p in stack["pipelines"] if p["net_monthly"] is not None]
        segments = [(p["label"], p["net_monthly"], "") for p in known]
        gap = stack["target"] - stack["total_net"]
        if len(segments) + (gap > 0) > len(SEGMENTS):
            # Five hues at most, and the fold-in "기타" would hide the shortfall: the two pension accounts share one.
            accounts = [p for p in known if p["key"] in ("pension_savings", "irp")]
            segments = [(p["label"], p["net_monthly"], "") for p in known if p not in accounts]
            segments.insert(2, ("연금저축·IRP", sum((p["net_monthly"] for p in accounts), Decimal(0)), ""))
        if gap > 0:
            segments.append(("목표까지 부족", gap, ""))
        note = f"세후 월 수령액 합계 {_short(stack['total_net'])} / 목표 월 {_short(stack['target'])}"
        return (f"노후 월 현금흐름 준비({stack['retirement_age']}세 은퇴 기준)", segments,
                note + (f" · 미상 {len(stack['unknown'])}개 제외" if stack["unknown"] else ""))
    if tiles == "life":
        split = data.get("spending_split")
        if split is None:
            return "", [], ""
        segments = [("고정비", split["fixed"], ""), ("재량비", split["discretionary"], ""), ("적립·투자", split["transfers"], "")]
        if split["left"] > 0:
            segments.append(("남는 돈", split["left"], ""))
        return f"이번 달 수입 {_short(split['inflow'])}의 쓰임", segments, ""
    composition = data.get("composition") or {}
    groups = composition.get("groups") or []
    segments = []
    for g in groups:
        owners = g["by_owner"]
        note = " · ".join(f"{'본인' if o == 'self' else '배우자'} {_short(v)}" for o, v in sorted(owners.items(), key=lambda item: (item[0] != "self", item[0]))) if len(owners) > 1 else ""
        segments.append((g["label"], g["value"], note))
    note = "" if composition.get("complete", True) else "환율이 없어 원화로 바꾸지 못한 자산은 빠져 있습니다."
    return f"자산의 구성 · {_short(composition.get('total'))}", segments, note


def _stacked(segments: list[tuple[str, Decimal | None, str]], *, on_dark: bool = False) -> str:
    """One stacked bar with a legend: each segment in its fixed colour, a 2px gap between fills, the share
    and an optional note beside each label. Unknown or zero segments are left out; nothing without a total."""
    rows = [(label, value, note) for label, value, note in segments if value is not None and value > 0]
    if len(rows) > len(SEGMENTS):  # Hues are never cycled: the tail folds into one segment.
        rows = rows[:len(SEGMENTS) - 1] + [("기타", sum((v for _, v, _ in rows[len(SEGMENTS) - 1:]), Decimal(0)), "")]
    total = sum((value for _, value, _ in rows), Decimal(0))
    if not rows or not total:
        return ""
    surface = DARK if on_dark else CANVAS
    cells = "".join(f'<td style="width:{max(1, int(value / total * 100))}%;height:14px;font-size:0;line-height:0;'
                    f'background:{SEGMENTS[i % len(SEGMENTS)]};border-right:2px solid {surface}">&nbsp;</td>'
                    for i, (_, value, _) in enumerate(rows))
    bar = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-radius:100px;'
           f'overflow:hidden"><tr>{cells}</tr></table>')
    ink = ON_DARK if on_dark else INK
    # The legend sits on its own line under the bar: a coloured dot, the label and the share, 12px apart and
    # divided by a bar character, so the items never run together even where styles are stripped or the text
    # is copied (user review 2026-10-18: "삼성전자 39%VOO 24%").
    divider = f' <span style="color:{ink};margin-right:12px">|</span> '
    items = []
    for i, (label, value, note) in enumerate(rows):
        dot = f'<span style="color:{SEGMENTS[i % len(SEGMENTS)]}">●</span> '
        extra = f' <span style="font-family:{MONO}">({escape(note)})</span>' if note else ""
        items.append(f'<span style="display:inline-block;white-space:nowrap;margin:6px 12px 0 0;font-size:12px;color:{ink}">'
                     f'{dot}{escape(label)} <span style="font-family:{MONO}">{_pct(value / total, 0)}</span>{extra}</span>')
    return bar + f'<div style="line-height:1.8">{divider.join(items)}</div>'


def _gauge(label: str, ratio: Decimal | None, text: str) -> str:
    """Progress toward a target: the filled share (capped at 100%), the figure beside it, a line under it."""
    fill = 0 if ratio is None else max(0, min(100, int(ratio * 100)))
    shown = "미상" if ratio is None else _pct(ratio, 0)
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:6px 0 4px"><tr>'
            f'<td style="width:160px;font-size:13px;color:{BODY};padding:4px 8px 4px 0;white-space:nowrap">{escape(label)}</td>'
            f'<td><div style="background:{STRONG};height:14px;border-radius:100px"><div style="background:{ACCENT};height:14px;'
            f'width:{fill}%;border-radius:100px"></div></div></td>'
            f'<td style="width:64px;text-align:right;font-family:{MONO};font-size:13px;color:{INK};padding-left:8px">{shown}</td>'
            f'</tr></table>' + (f'<div style="font-size:13px;line-height:1.6;color:{BODY}">{text}</div>' if text else ""))


def _pill(text: str, kind: str = "neutral") -> str:
    background, color, weight = PILLS[kind]
    return (f'<span style="display:inline-block;background:{background};color:{color};font-size:12px;font-weight:{weight};'
            f'border-radius:100px;padding:3px 10px;white-space:nowrap">{escape(text)}</span>')


class Raw(str):
    """A table cell that is already HTML (a pill); every other cell is escaped."""


def _tile_grid(tiles: list[tuple[str, str, str]]) -> str:
    def tile(title: str, value: str, sub: str) -> str:
        return (f'<td style="width:50%;padding:5px;vertical-align:top"><div style="border-radius:24px;padding:16px 18px;'
                f'background:{DARK_RAISED}">'
                f'<div style="font-size:12px;font-weight:600;color:{ON_DARK_SOFT}">{escape(title)}</div>'
                f'<div style="font-family:{MONO};font-weight:500;font-size:22px;color:{ON_DARK};margin:8px 0 2px">'
                f'{escape(value)}</div>'
                f'<div style="font-size:12px;color:{ON_DARK_SOFT}">{escape(sub)}</div></div></td>')
    # Two by two so the tiles stay readable on a phone.
    grid = "".join(f"<tr>{tile(*a)}{tile(*b)}</tr>" for a, b in (tiles[:2], tiles[2:]))
    return (f'<tr><td style="padding-top:18px"><table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0">{grid}</table></td></tr>')


def _proposals(advice: dict[str, Any], full: bool, edition: editions.Edition | None = None) -> str:
    """The proposal cards: why now, effect, risks, timing, what would change the judgement, certainty.
    Thursday cards are short but keep the certainty label. Judgements stopped by unknown inputs follow."""
    proposals = advice.get("proposals") or []
    themed = edition is not None and edition.key != "overview"
    head = f'<tr><td>{_eyebrow("오늘의 제안" if themed else "이번 주 제안")}{_heading("고객님께 드리는 제안", size=28)}</td></tr>'
    pending = _undetermined(advice)  # A daily edition carries only its own rules' pending judgements.
    if not proposals:
        # Never "no proposal is a judgement too" while a rule could not judge (review 2026-10-18).
        text = (f"확정한 새 제안은 없습니다. 다만 자료가 부족해 판단을 보류한 것이 {len(advice.get('undetermined') or [])}건 있어, "
                "무엇이 필요한지 아래에 적었습니다." if advice.get("undetermined") else
                "새 제안이 없습니다. 제안이 없는 것도 판단입니다. 다음 보고에서 변화가 있으면 다시 말씀드리겠습니다.")
        return head + f'<tr><td style="font-size:15px;line-height:1.6;color:{BODY}">{text}</td></tr>' + pending
    cards = []
    for number, p in enumerate(proposals[:3], 1):
        badge = (f'<span style="display:inline-block;background:{BLUE};color:#ffffff;font-size:12px;font-weight:600;'
                 f'border-radius:100px;padding:4px 11px;margin-right:6px">{number}</span>'
                 f'<span style="display:inline-block;background:{STRONG};color:{INK};font-size:12px;font-weight:600;'
                 f'border-radius:100px;padding:4px 12px">{PRIORITY_LABELS.get(p.priority, "")}</span>')
        if p.times_shown:
            badge += (f'<span style="display:inline-block;background:{STRONG};color:{MUTED};font-size:12px;font-weight:600;'
                      f'border-radius:100px;padding:4px 12px;margin-left:6px">이 제안을 안내드린 {p.times_shown + 1}번째 보고</span>')
        title = (f'<div style="font-size:18px;font-weight:600;line-height:1.4;color:{INK};margin:12px 0 8px">'
                 f'{escape(p.title)}</div>')
        if full:
            # One sentence per row (user review 2026-10-18: a quarter of the text); the full reasoning is kept for
            # the e-mail answers.
            rows = [("왜 지금", escape(_first_sentence(p.why)))]
            if p.context:  # A verification caveat on the context is a footer system note, not prose.
                rows.append(("시장·정책 맥락", escape(_first_sentence(p.context)) + _sources(p.links)))
            if number == 1:
                rows += [("예상 효과", escape(_first_sentence(p.effect))), ("위험과 대안", escape(_first_sentence(p.risks))),
                         ("시점", escape(p.timing)), ("다시 판단할 조건", escape(_first_sentence(p.reconsider))),
                         ("근거의 성격", escape(p.certainty))]
            else:  # The later cards: why and when; the rest is a reply away.
                rows.append(("시점", escape(p.timing)))
            detail = "".join(f'<tr><td style="width:92px;vertical-align:top;font-size:12px;font-weight:600;color:{MUTED};'
                             f'padding:6px 8px 6px 0">{escape(k)}</td><td style="font-size:14px;line-height:1.6;color:{BODY};'
                             f'padding:6px 0">{v}</td></tr>' for k, v in rows)
            detail = f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{detail}</table>'
        else:
            home = editions.home(p.key) if edition is not None else None
            # The overview points to the themed day for the certainty and the rest; the original short Thursday
            # report has no such day, so it keeps the certainty label.
            pointer = f"자세히: {escape(home.label)} 보고" if home else f"근거의 성격: {escape(p.certainty)}"
            detail = (f'<div style="font-size:14px;line-height:1.6;color:{BODY}">{escape(_first_sentence(p.why))}</div>'
                      f'<div style="font-size:12px;color:{MUTED};margin-top:6px">{pointer}</div>')
        if p.table:
            # The researched products the action moves money into (client request 2026-10-20).
            step = p.figures.get("per_quarter")
            detail += (f'<div style="font-size:13px;font-weight:700;color:{INK};margin-top:12px">옮길 곳 1~{len(p.table) - 1}순위'
                       + (f" · 분기 {_short(step)}" if step is not None else "") + "</div>"
                       + _table(list(p.table[0]), [list(row) for row in p.table[1:]]) + _sources(p.table_links)
                       + f'<div style="font-size:12px;color:{MUTED};margin-top:4px">수량은 분기 금액을 1순위에 모두 넣을 때이고 '
                       "2·3순위는 1순위 대신 고를 때의 대안입니다. 가격은 조사 시점 종가라 주문 전 다시 확인하십시오.</div>")
        elif p.key in (advice.get("pending_products") or ()):
            detail += (f'<div style="font-size:12px;color:{MUTED};margin-top:10px">옮길 상품 1~3순위는 검토 팀의 확인이 끝나는 대로 '
                       "다음 보고서에 싣습니다.</div>")
        # One card open at a time (user review 2026-10-18): the first proposal shows its detail, the others
        # show the badges and the title until tapped.
        state = " open" if number == 1 else ""
        cards.append(f'<tr><td style="padding-top:12px"><div style="background:{CANVAS};border:1px solid {HAIRLINE};'
                     f'border-radius:24px;padding:18px 20px"><details{state} style="margin:0">'
                     f'<summary style="cursor:pointer;outline:none">{badge}{title}{_expand_hint(block=True)}</summary>'
                     f'<div style="margin-top:8px">{detail}</div></details></div></td></tr>')
    rest = proposals[3:]
    more = ""
    if rest:
        # The rest by their short headlines (user review 2026-10-18: nine full sentences read long).
        items = "".join(f'<div style="font-size:13px;line-height:1.6;color:{BODY};margin:4px 0">· {escape(p.headline or p.title)}</div>'
                        for p in rest)
        more = (f'<tr><td style="padding-top:16px"><details style="margin:0"><summary style="{SUMMARY}font-size:14px">'
                f'{_expand_hint()}함께 검토한 다른 제안 {len(rest)}건</summary>'
                f'<div style="margin-top:6px">{items}</div></details></td></tr>')
    if not full and edition is None:  # The overview cards point to their themed day themselves.
        more += (f'<tr><td style="padding-top:12px;font-size:12px;color:{MUTED}">목요 보고는 요약입니다. 근거와 위험은 월요 '
                 '보고에서 자세히 말씀드립니다.</td></tr>')
    return head + "".join(cards) + more + pending


def _undetermined(advice: dict[str, Any]) -> str:
    """Rules that could not judge because an input is unknown: said plainly, never read as all clear."""
    items = advice.get("undetermined") or []
    if not items:
        return ""
    rows = "".join(f'<div style="font-size:13px;line-height:1.6;color:{BODY};margin:4px 0">· <b style="font-weight:600;color:{INK}">'
                   f'{escape(u.topic)}</b> — {escape(u.missing)}</div>' for u in items)
    return (f'<tr><td style="padding-top:12px"><details style="margin:0"><summary style="{SUMMARY}font-size:14px">'
            f'{_expand_hint()}자료가 부족해 이번에 판단하지 못한 것 {len(items)}건</summary>'
            f'<div style="margin-top:6px">{rows}</div></details></td></tr>')


def _expand_hint(*, on_dark: bool = False, block: bool = False) -> str:
    """"▼ 펼쳐서 상세 보기" on a closed card, "▲ 접기" on an open one where <style> is honoured; floated right in a
    section summary, or its own line (`block`) under a card title."""
    color = ON_DARK if on_dark else BLUE
    place = "display:block;margin-top:6px" if block else "float:right;margin-left:10px"
    return (f'<span style="{place};font-size:12px;font-weight:600;color:{color};white-space:nowrap">'
            f'<span class="mk-closed">▼ 펼쳐서 상세 보기</span><span class="mk-open" style="display:none">▲ 접기</span></span>')


def _judgement(story: dict[str, Any]) -> str:
    """The narrative's bottom line at the top of the dark hero: its first sentence set large as the executive
    headline, the rest of the situation beneath it (report design §17.6)."""
    if not story.get("situation"):
        return ""
    headline, rest = _split_headline(story["situation"])
    brief = (story.get("status") or {}).get("brief")
    basis = ""
    if brief:
        basis = f"{brief['created_at']:%m월 %d일 %H시} 조사 자료 기준" + (" (이번 보고에서 새로 조사하지 못해 다시 사용)"
                                                                     if brief.get("reused") else "")
        basis = f'<div style="font-size:12px;color:{ON_DARK_SOFT};margin-top:6px">{escape(basis)}</div>'
    links = "".join(f' · <a href="{escape(url, quote=True)}" style="color:{ON_DARK_SOFT}">{escape(title)}</a>'
                    for url, title in dict((u, t) for t, u in story.get("situation_links", [])).items()
                    if _plain_link(url))
    body = (f'<div style="font-size:15px;line-height:1.65;color:{ON_DARK_SOFT};margin-top:10px">{escape(rest)}</div>'
            if rest else "")
    return (f'<tr><td style="padding-top:18px"><div style="font-size:12px;font-weight:600;color:{ON_DARK_SOFT};'
            f'margin-bottom:8px">Smith의 판단</div>'
            f'<div style="font-family:{SANS};font-weight:400;font-size:22px;line-height:1.35;letter-spacing:-0.4px;'
            f'color:{ON_DARK}">{escape(headline)}</div>{body}{basis}'
            + (f'<div style="font-size:12px;color:{ON_DARK_SOFT};margin-top:4px">출처{links}</div>' if links else "")
            + '</td></tr>')


def _split_headline(text: str) -> tuple[str, str]:
    """(first sentence, the rest). The model writes the executive headline as the first sentence."""
    end = text.find(". ")
    if end < 0:
        return text.strip(), ""
    return text[:end + 1].strip(), text[end + 1:].strip()


def _plain_link(url: str) -> bool:
    return url.startswith("https://") and not any(ch.isspace() or ord(ch) < 32 or ch in "\"'<>\x7f" for ch in url)


def _system_notes(data: dict[str, Any], story: dict[str, Any], reasons: list[str]) -> list[str]:
    """What used to interrupt the client's prose and now lives in the footer (report design §17.6): the
    reasons behind the status badge, the verification caveats on narrative blocks, and where each requested
    piece of information goes (the JSON field or config key)."""
    notes = [f"상태 판정: {reason}" for reason in reasons]
    memos = []
    for field, label in (("situation_memo", "판단"), ("direction_memo", "전략 방향"), ("portfolio_memo", "투자 전략")):
        if story.get(field):
            memos.append(f"{label}: {story[field]}")
    memos += [f"변화 '{i['title']}': {i['memo']}" for i in story.get("insights") or [] if i.get("memo")]
    memos += [f"지켜볼 것 '{w['item']}': {w['memo']}" for w in story.get("watch") or [] if w.get("memo")]
    names = {p["key"]: str(p["name"]) for p in (data.get("holdings") or {}).get("positions", [])}
    memos += [f"종목 의견 {names.get(key, key)}: {o['memo']}" for key, o in (story.get("holdings") or {}).items() if o.get("memo")]
    memos += [f"제안 맥락 '{_first_sentence(p.title)}': {p.context_memo}"
              for p in (data.get("advice") or {}).get("proposals") or [] if getattr(p, "context_memo", "")]
    notes += [f"자동 점검 메모 · {memo}" for memo in memos]
    notes += [f"입력 안내 · {r.title}: {r.how}" for r in data.get("requests") or []]
    stale = ((data.get("sectors") or {}).get("real_estate") or {}).get("deals_stale")
    if stale:  # Named in the deal sections; the footer keeps the failure and its code for the e-mail answers.
        notes.append(f"데이터 · 아파트 실거래가: {stale['last_failure_on']:%m월 %d일} 갱신 실패({stale['error'] or '-'}), "
                     + (f"{stale['last_success_on']:%m월 %d일}에 받은 값을 썼습니다." if stale["last_success_on"]
                        else "받은 값이 없습니다."))
    toss = _toss_cash_note(((data.get("sectors") or {}).get("cash") or {}).get("toss_cash") or {})
    if toss:  # A standing data caveat, not news: kept for the e-mail answers (user review 2026-10-18).
        notes.append("데이터 · " + re.sub(r"<[^>]+>", "", toss).strip())
    return notes


def _sources(links: Any) -> str:
    """Small source links after a sourced statement."""
    unique = {url: title for title, url in reversed(list(links))}  # One link per page, first title wins.
    items = [_cell((title, url)) for url, title in reversed(list(unique.items()))][:4]
    if not items:
        return ""
    return (f'<div style="font-size:12px;color:{MUTED};margin-top:4px">출처: ' + " · ".join(items) + "</div>")


def _changes_outside(story: dict[str, Any], title: str = "이번 주 눈여겨볼 변화") -> str:
    """Insights from the research brief that matter for this household, and what to watch next: a band of its
    own right after the proposals, open and in larger type (client e-mail 2026-10-19: "중요한데 더 가시성 높여줘,
    크기도 키우고"). The length stays a quarter of the old one: the title and what it means here."""
    insights, watch = story.get("insights") or [], story.get("watch") or []
    if not insights and not watch:
        return ""
    cards = "".join(f'<tr><td style="padding-top:12px"><div style="border-left:4px solid {BLUE};background:{SOFT};'
                    f'border-radius:16px;padding:16px 18px"><div style="font-size:19px;font-weight:700;line-height:1.4;'
                    f'color:{INK};margin-bottom:6px">{escape(item["title"])}</div>'
                    f'<div style="font-size:16px;line-height:1.65;color:{BODY}">{escape(item["implication"])}</div>'
                    f'{_sources(item["links"])}</div></td></tr>' for item in insights)
    if watch:
        rows = "".join(f'<div style="font-size:15px;line-height:1.65;color:{BODY};margin:6px 0">· <b style="font-weight:700;'
                       f'color:{INK}">{escape(w["item"])}</b> — {escape(w["why"])}'
                       f'{_sources(w["links"])}</div>' for w in watch)
        cards += (f'<tr><td style="padding-top:14px"><div style="font-size:15px;font-weight:700;color:{INK};margin-bottom:4px">'
                  f'다음 보고까지 지켜볼 것</div>{rows}</td></tr>')
    return f'<tr><td>{_eyebrow("놓치지 마실 변화", color="#ffffff", background=BLUE)}{_heading(title, size=26)}</td></tr>' + cards


def _tax(advice: dict[str, Any], data: dict[str, Any], full: bool) -> str:
    """After-tax view: strategy notes (Monday) and the year-end settlement checklist (Q4, both editions)."""
    notes, rows = advice.get("tax_notes") or [], advice.get("tax_checklist") or []
    year_end = data["as_of"].month >= 10
    if not (full and notes) and not (year_end and rows):
        return ""
    inner = ""
    if full:
        inner += "".join(f'<div style="margin:6px 0 14px"><div style="font-size:15px;font-weight:600;color:{INK};'
                         f'margin-bottom:4px">{escape(n.title)}</div><div style="font-size:14px;line-height:1.6;color:{BODY}">'
                         f'{escape(_first_sentence(n.body))}</div></div>' for n in notes[:MAX_TAX_NOTES])
    if year_end and rows:
        inner += (f'<div style="font-size:12px;font-weight:600;color:{MUTED};margin:8px 0 0">올해 연말정산 점검표</div>'
                  + _table(["항목", "올해 상황", "할 일"], [list(row) for row in rows]))
    lead = "같은 돈도 어느 계좌에 두고 언제 실현하느냐에 따라 세후 결과가 달라집니다."
    from smith import tax as tax_rules
    inner += (f'<div style="font-size:12px;color:{MUTED};margin-top:8px">근거(세법 기준 {tax_rules.RULES_YEAR}년, '
              f'{tax_rules.RULES_CHECKED} 확인): ' + " · ".join(_cell(item) for item in tax_rules.sources()) + "</div>")
    return _section("세금 관점 전략", lead, inner)


def _follow_up(advice: dict[str, Any]) -> str:
    """Proposals the client accepted (in progress) and earlier decisions (§10.6)."""
    in_progress, decided = advice.get("in_progress") or [], advice.get("decided") or []
    if not in_progress and not decided:
        return ""
    labels = {"declined": "보류하기로 하셨습니다", "done": "완료하셨습니다"}
    rows = [f'<div style="font-size:14px;line-height:1.6;color:{BODY};margin:4px 0">· <b style="font-weight:600;color:{INK}">'
            f'진행 중</b> — {escape(p.title)}</div>' for p in in_progress]
    rows += [f'<div style="font-size:14px;line-height:1.6;color:{BODY};margin:4px 0">· {escape(e["key"])}: '
             f'{labels[e["status"]]}({(e["decided_at"] or "")[:10]})</div>' for e in decided]
    lead = "지난 보고에서 드린 제안의 진행 상황입니다. 수치가 크게 바뀌면 다시 말씀드립니다."
    return _section("지난 제안 점검", lead, "".join(rows))


def _strategy(tracks: list[Track], direction: str = "") -> str:
    """Monday: whether each long-range goal is on track, as raised cards on the dark band."""
    rows = []
    for track in tracks:
        icon, label, color = TRACK_STATUS[track.status]
        # The name, the state and the headline only (user review 2026-10-18: a quarter of the text); the reasoning is
        # a system note away (the e-mail answers).
        rows.append(f'<tr><td style="padding-top:10px"><div style="background:{DARK_RAISED};border-radius:24px;padding:18px 20px">'
                    f'<div style="font-size:13px;font-weight:600;color:{ON_DARK}">{escape(track.name)} '
                    f'<span style="color:{color}">{icon} {label}</span></div>'
                    f'<div style="font-size:17px;font-weight:400;line-height:1.4;color:{ON_DARK};margin:6px 0 0">'
                    f'{escape(track.headline)}</div></div></td></tr>')
    lead = (f'<div style="font-size:15px;line-height:1.65;color:{ON_DARK_SOFT};margin-bottom:6px">{escape(direction)}</div>'
            if direction else "")
    return (f'<tr><td>{_eyebrow("전략 방향", color=ON_DARK, background=DARK_RAISED)}'
            f'{_heading("목표까지 지금 궤도에 있습니까", color=ON_DARK, size=28)}{lead}'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{"".join(rows)}</table></td></tr>')


def _first_sentence(text: str) -> str:
    end = text.find(". ")  # Decimal points (3.61%) are never followed by a space.
    return text if end < 0 else text[:end + 1]


_LEAD_TOKENS = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)[^>]*>|\. ")
_VOID = frozenset({"br", "img", "hr", "wbr"})


def _brief(text: str, sentences: int = 2) -> str:
    """The first `sentences` sentences of a section lead (user review 2026-10-18: the mail keeps a quarter of the
    text, so every lead puts its takeaway first). The lead is HTML: a sentence ends only at ". " outside every
    element, so the markup stays whole (review 2026-10-18: a complex name "가. 나. 다" in <b> lost its </b>).
    Escaped text cannot contain "<", so every tag found is real markup."""
    depth, count = 0, 0
    for token in _LEAD_TOKENS.finditer(text):
        if token.group(2) is None:
            if depth == 0:
                count += 1
                if count == sentences:
                    return text[:token.start() + 1]
        elif token.group(2).lower() not in _VOID and not token.group(0).endswith("/>"):
            depth = max(0, depth + (-1 if token.group(1) else 1))
    return text


# --- sections ---------------------------------------------------------------------------------------------

def _change(data: dict[str, Any]) -> str:
    change = data["change"]
    if change is None:
        return _section("지난 보고 이후 변화", "첫 보고서입니다. 이번 수치를 앞으로 변화를 비교할 <b>기준선</b>으로 삼습니다.", "")
    balanced = change.get("balanced", True)
    parts = [(COMPONENT_LABELS[key], value) for key, value in change["parts"].items() if value] if balanced else []
    parts.sort(key=lambda item: -abs(item[1]))
    direction = "증가" if change["total"] > 0 else "감소" if change["total"] < 0 else "변동 없음"
    span = (f"<b>{change['from']:%m월 %d일} → {change['to']:%m월 %d일}</b>" if change.get("from") and change.get("to")
            else "지난 보고 이후")
    lead = (f"{span} <b>순자산</b>이 <b>{_short(abs(change['total']))} {direction}</b>했습니다."
            if change["total"] else f"{span} 순자산 변화가 없습니다.")
    groups = change.get("groups")
    if groups:
        # The two figures apart (report design §18.2): a market loss is never averaged away by new entries.
        market, inflow = groups["market_and_valuation"], groups["net_inflow"]
        lead += (f" 보유 자산의 <b>시장·평가 변동은 {_signed_short(market)}</b>, 입출금·원장 등록은 "
                 f"{_signed_short(inflow)}입니다.")
        if market < 0 and change["total"] > market:
            lead += f" 새로 들어온 돈과 등록이 합계를 가렸을 뿐, 시세로는 <b>{_short(-market)}</b>이 줄었습니다."
        elif market > 0 and change["total"] < market:
            lead += f" 시세로는 {_short(market)}이 늘었지만 빠져나간 돈이 합계를 낮췄습니다."
    elif parts:
        lead += f" 가장 큰 요인은 <b>{escape(parts[0][0])}</b>입니다."
    if not balanced:
        lead += " 원인별 분해의 합계가 전체 변화와 맞지 않아 이번에는 분해를 싣지 않았습니다(Smith 계산 점검이 필요합니다)."
    if not change["complete"]:
        lead += " 환율이 없는 통화가 있어 분해가 일부 불완전합니다."
    correction = change.get("correction") or Decimal(0)
    if correction:
        lead += (f" 과거 입력을 바로잡아 지난 보고의 출발점이 {_signed_short(correction)} 달라졌습니다. 이는 실제 변동이 "
                 "아니어서 위 변화에 넣지 않았습니다.")
    rows = [[label, _signed_short(value)] for label, value in parts] + [["합계", _signed_short(change["total"])]]
    if correction:
        rows.append(["(참고) 과거 입력 정정 · 합계 제외", _won(correction)])
    chart = _diverging([(GROUP_LABELS[k], v) for k, v in groups.items()]) if groups else (_diverging(parts) if parts else "")
    since = f"{change['from']:%m/%d} 대비 " if change.get("from") else ""
    note = since + _signed_short(change["total"]) + (
        f" · 시장·평가 {_signed_short(groups['market_and_valuation'])} · 입출금 {_signed_short(groups['net_inflow'])}" if groups else "")
    table = "" if groups else _table(["요인", "금액"], rows)
    return _section("지난 보고 이후 변화", lead, chart + table, note=escape(note), brief=None)


def _timeline(data: dict[str, Any]) -> str:
    items = data["timeline"]
    if not items:
        return _section("다가오는 큰 일정", "향후 5년 안에 예정된 큰 자금 일정이 없습니다.", "")
    today = data["as_of"].date()
    rows = []
    for item in items:
        months = (item["date"].year - today.year) * 12 + item["date"].month - today.month
        inflow = item["kind"] == "inflow"
        # Money coming in is told apart from money going out by its sign and a softer marker. The after-tax
        # figure is the year's final tax estimate, not the cash of that day, so it is named as such.
        amount = _short(item["amount"])
        if inflow:
            amount = ("+" + _short(item["amount"]) + "(세후 입력)" if item.get("gross") is None else
                      f"+{_short(item['gross'])}(세전) · 최종 세후 약 {_short(item['amount'])}")
            if item.get("count", 1) > 1:
                usable = sorted({f"{d:%Y.%m}" for d in item.get("usable") or []})
                amount += f" · {item['count']}회 분할" + (f", 일부는 {' · '.join(usable)}부터 사용 가능" if usable else "")
        rows.append(f'<tr><td style="width:100px;font-family:{MONO};font-size:12px;font-weight:500;color:{INK};padding:7px 8px 7px 0">'
                    f'{item["date"]:%Y.%m.%d}</td>'
                    f'<td style="font-size:13px;color:{BODY};padding:7px 0 7px 12px;border-left:3px '
                    f'{"dashed " + MUTED_SOFT if inflow else "solid " + ACCENT}">'
                    f'{escape(item["label"])} · {amount} <span style="color:{MUTED_SOFT}">({months}개월 후)</span></td></tr>')
    needs = [item for item in items if item["kind"] != "inflow"]
    first = needs[0] if needs else None
    lead = ("향후 5년 안에 예정된 큰 지출 일정은 없습니다." if first is None else
            f"가장 가까운 큰 자금 일정은 <b>{first['date']:%Y년 %m월}</b>의 {escape(first['label'])}"
            f"(<b>{_short(first['amount'])}</b>)입니다.")
    home = data["kpis"]["home_progress"]
    inner = f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{"".join(rows)}</table>'
    if home:
        inner += _home_plan(home)
        lead += " " + _first_sentence(_home_lead(home))
    note = "" if home is None else f"주택 준비율 {_pct(home['ratio_baseline'], 0)}"
    return _section("목표와 일정", lead, inner, note=escape(note))


def _home_lead(home: dict[str, Any]) -> str:
    """The home goal in words: the household baseline first, the conservative tiers beside it (§17.3)."""
    gap = home["gap_baseline"]
    text = (f"주택 목표는 기준 시나리오에서 <b>{escape(home['baseline_label'])}</b> 기준으로 필요액의 "
            f"<b>{_pct(home['ratio_baseline'], 0)}</b>를 준비할 수 있고, "
            + (f"부족분은 약 <b>{_short(gap)}</b>입니다." if gap > 0 else f"필요액을 넘어 약 <b>{_short(-gap)}</b>이 남습니다.")
            + f" 보수적으로 지금 있는 자산만 보면 본인 자원 {_pct(home['ratio_self'], 0)}")
    if home.get("ratio_household") is not None:
        text += f", 배우자 부동산 순자산을 더해 {_pct(home['ratio_household'], 0)}"
    text += "입니다. 집값 연 2%와 투자 수익 연 4%를 가정했습니다."
    after = home.get("after_tax")
    if after and after.get("case") == "exempt":
        text += (f" 배우자 주택은 2년 보유·거주를 확인해 주셔서 팔 때 양도세를 1세대 1주택 비과세 기준 약 {_short(after['tax']['exempt'])}"
                 f"(12억 원 초과분만 과세)으로 보며, 요건을 못 갖추면 약 {_short(after['tax']['general'])}입니다. 혼인신고를 하시면 혼인일부터 "
                 "10년 안에 이 집을 먼저 파는 순서가 안전합니다. 현행 법령 확인이 필요합니다.")
    elif after:
        text += (f" 배우자 주택을 팔아 쓰는 경우의 양도세는 1세대 1주택 비과세 요건을 갖추면 약 {_short(after['tax']['exempt'])}, 일반 "
                 f"과세면 약 {_short(after['tax']['general'])}이며, 혼인신고 시점과 2년 거주 여부에 따라 갈립니다. 현행 법령 확인이 필요합니다.")
    if home.get("pay_at_target") is None and home.get("pay_after_target") is None:
        pass
    elif home.get("pay_at_target") is None:  # Unknown (no FX rate for some pay), never shown as none.
        text += " 예정 소득 가운데 원화로 환산하지 못한 항목이 있어 그 몫은 넣지 않았습니다."
    return text


def _home_plan(home: dict[str, Any]) -> str:
    """Bars and the scenario table of the home goal: the household baseline, the conservative tiers and the
    client-only figures, with expected pay as its own column when there is any (§17.3)."""
    bars = [("필요 자금(비용 포함)", home["required"], False),
            (f"가구 통합 기준({home['baseline_label']})", home["baseline"], True)]
    if home.get("available_household") is not None and home["available_household"] != home["baseline"]:
        bars.append(("가구 통합, 지금 있는 자산만", home["available_household"], False))
    bars.append(("본인 자원만", home["available_self"], False))
    with_pay = any(s.get("expected_pay_at_target") not in (None, "0") for s in home["scenarios"])
    household = any(s.get("available_household") is not None for s in home["scenarios"])
    headers = ["시나리오", "필요 자금", "본인 자원"] + (["가구 통합"] if household else []) \
        + (["가구 통합 + 예정 소득"] if with_pay and household else []) + ["부족분(기준)"]
    rows = []
    for s in home["scenarios"]:
        row = [SCENARIO_LABELS.get(s["name"], s["name"]), _short(_dec(s["required_with_costs"])), _short(_dec(s["available_self"]))]
        if household:
            row.append(_short(_dec(s["available_household"])))
        if with_pay and household:
            row.append(_short(_dec(s["available_household_with_expected_pay"])))
        gap = (s["gap_household_with_expected_pay"] if with_pay and household and s.get("gap_household_with_expected_pay") is not None
               else s["gap_including_partner_property_equity"] if household else s["gap_self"])
        row.append(_short(_dec(gap)))
        rows.append(row)
    gauge = _gauge(f"주택 목표 준비율({home['baseline_label']})", home["ratio_baseline"], "")
    return '<div style="height:12px"></div>' + gauge + _table(headers, rows)


def _goals_brief(data: dict[str, Any]) -> str:
    """The household day's goals in brief (§17.6): the dated needs, the home goal on the household basis and
    the retirement line, without the inflow rows and the scenario table (those are on the overview)."""
    today = data["as_of"].date()
    needs = [item for item in data["timeline"] if item["kind"] != "inflow"]
    home = data["kpis"]["home_progress"]
    if not needs and home is None:
        return _section("목표까지의 준비", "향후 5년 안에 예정된 큰 자금 일정이 없습니다.", "")
    rows = [[f"{item['date']:%Y.%m}", item["label"],
             f"{(item['date'].year - today.year) * 12 + item['date'].month - today.month}개월 후", _short(item["amount"])]
            for item in needs]
    lead = _home_lead(home) if home else "향후 5년 안의 큰 자금 일정은 아래와 같습니다."
    lead += " 전체 일정표와 시나리오별 표, 앞으로 들어올 성과급·주식 보상은 종합 보고에 있습니다."
    gauge = "" if home is None else _gauge(f"주택 목표 준비율({home['baseline_label']})", home["ratio_baseline"], "")
    return _section("목표까지의 준비", lead, gauge + (_table(["시점", "일정", "남은 기간", "금액"], rows) if rows else ""),
                    note="" if home is None else escape(f"주택 준비율 {_pct(home['ratio_baseline'], 0)}"))


def _lease_plan(data: dict[str, Any]) -> str:
    """How the nearest lease deposit is actually returned (§17.2): the household's own cash by scenario, the
    sale's net cash in, the loan the debt-service cap allows, and the stress need."""
    plans = data.get("lease_plans") or []
    if not plans:
        return ""
    plan = plans[0]
    relet, sale, loan, st = plan["relet"], plan["sale"], plan["loan"], plan["stress"]
    amount = plan["amount"]
    if plan["default"] == "sale":
        # The client's plan: the home is sold when the lease ends (e-mail 2026-10-19).
        left = -sale["cash_out"]
        lead = (f"{plan['due']:%Y년 %m월} 돌려드릴 보증금 <b>{_short(amount)}</b>은 계획하신 대로 만기에 맞춰 집을 팔면 매도 대금으로 "
                + (f"돌려드리고 약 <b>{_short(left)}</b>이 남습니다." if left >= 0 else
                   f"돌려드리기에 약 <b>{_short(plan['need'])}</b>이 모자랍니다.")
                + " 팔리지 않을 때를 대비한 예비 방법은 아래 표와 같습니다.")
    elif plan["default"] == "relet":
        lead = (f"{plan['due']:%Y년 %m월} 돌려드릴 보증금 <b>{_short(amount)}</b>은 전액이 고객님 주머니에서 나가는 돈이 아닙니다. 새 세입자를 "
                f"받으면 그 보증금이 기존 보증금을 대신하므로, 최근 6개월 같은 단지·면적 전세 중간값 {_short(plan['jeonse'])}을 기준으로 직접 "
                f"준비하실 돈은 차액 {_short(relet['gap'])}에 안전 버퍼 {_short(relet['buffer'])}을 더한 약 <b>{_short(relet['need'])}</b>입니다.")
    else:
        lead = (f"{plan['due']:%Y년 %m월} 돌려드릴 보증금 <b>{_short(amount)}</b>은 같은 단지·면적 전세 계약이 적어 시세를 확정하기 어려워, "
                "보증금 전액을 보수적으로 준비 기준으로 두었습니다. 거래가 쌓이면 새 세입자 승계 기준으로 다시 계산합니다.")
    rows = [[s.label, "미상" if s.cash_out is None else ("들어오는 돈 " + _short(-s.cash_out) if s.cash_out < 0 else _short(s.cash_out)), s.basis]
            for s in plan["scenarios"]]
    if plan["default"] == "sale":
        pass
    elif sale["net_inflow"] is not None:
        lead += (f" 전세를 끼고 팔거나 만기에 맞춰 파는 경우에는 돈이 나가지 않고 약 {_short(-sale['cash_out'])}이 들어옵니다. "
                 + (f"2년 보유·거주를 확인해 주셔서 1세대 1주택 비과세 기준이며, 요건을 못 갖추면 약 {_short(sale['net_inflow']['general'])}입니다."
                    if sale.get("tax_case") == "exempt" else
                    f"1세대 1주택 비과세 요건을 갖추면 약 {_short(sale['net_inflow']['exempt'])}이며, 현행 법령 확인이 필요합니다."))
    elif sale["cash_out"] is not None:
        lead += f" 팔아서 돌려드리는 경우에는 약 {_short(-sale['cash_out'])}이 들어옵니다. 취득가를 알려 주시면 양도세까지 계산해 드립니다."
    if loan["amount"] is not None:
        lead += f" 퇴거자금대출은 소득 대비 상환 비율 기준으로 약 {_short(loan['amount'])}까지 가능할 수 있으나 확정 승인이 아닌 가정입니다."
    if st["basis"] == "reverse_jeonse":
        lead += (f" 스트레스 테스트에서는 전세 시세가 20% 내려 새 세입자 보증금이 {_short(st['jeonse_dropped'])}이 될 때의 차액 "
                 f"<b>{_short(st['need'])}</b>을 감당할 수 있는지로 판정합니다.")
    # The jeonse level and the sale price come from the official deals.
    return _section("보증금 반환: 실제로 준비할 돈", lead, _table(["반환 방법", "직접 준비할 돈", "근거"], rows),
                    note=_stale_note("", data["sectors"]["real_estate"]))


TAX_BASIS_TEXT = {"net_input": "세후 입력", "estimated": "연봉 기준 추정", "assumed": "가정 세율", "as_entered": "입력값",
                  "unknown": "환율 미상", "prior_unknown": "앞선 보상 금액 미상"}


def _future_income(data: dict[str, Any], *, overview: bool = False) -> str:
    """Expected pay (bonuses, share grants): what is coming, how sure it is, after an estimated tax, and the
    concentration a grant adds. Nothing here is in net worth (report design §15)."""
    found = data.get("future_income") or {}
    items = found.get("items") or []
    if not items:
        return ""
    sure = found["by_certainty"]
    lead = (f"앞으로 받을 성과급·주식 보상은 세후 추정 확정 <b>{_short(sure['confirmed'])}</b>, 예상 "
            f"<b>{_short(sure['expected'])}</b>, 가능성 {_short(sure['possible'])}이며, 받기 전까지는 순자산에 넣지 않고 계획에만 "
            "확실성별로 반영합니다.")
    if found.get("overdue"):
        lead += (f" 지급 예정일이 지났는데 받았다는 기록이 없는 항목 {len(found['overdue'])}건은 계획에서 뺐습니다. 받으셨는지 "
                 "알려 주시면 실제 지급일과 과세 금액으로 기록하겠습니다.")
    if found.get("assumed_tax"):
        lead += " 세전 기본 연봉을 몰라 세금은 가정 세율로 계산한 항목이 있습니다. 연봉을 알려 주시면 더 정확해집니다."
    if found.get("prior_unknown"):
        lead += (" 같은 해에 먼저 받는 보상의 세전 금액이나 환율을 몰라, 뒤에 받는 보상의 세율 구간을 정할 수 없어 세후를 "
                 "계산하지 않은 항목이 있습니다(미상).")
    rows = []
    for i in items:
        what = PAY_LABELS.get(i.category, "예정 수입")
        if i.shares:
            what += f" {i.quantity:,f}주" + ("" if i.price is None else f"(주가 {i.price:,f} 기준)")
        elif i.in_stock:
            what += "(자사주로 지급)"
        rows.append([f"{i.paid_on:%Y.%m}", what, "지급 확인 필요" if i.overdue else CERTAINTY_LABELS[i.certainty],
                     _short(i.gross), _short(i.net),
                     TAX_BASIS_TEXT[i.withholding_basis] + ("" if i.usable_from == i.paid_on else f", {i.usable_from:%Y.%m}부터 사용")])
    # The overview's timeline lists every payment already; the table stays for the original full report only.
    inner = "" if overview else _table(["받는 달", "내용", "확실성", "세전", "최종 세후(추정)", "비고"], rows)
    for stock in found.get("same_stock") or []:
        # A known 0% now (no securities yet) still warns; only unknown shares are skipped.
        if stock["share_now"] is None or stock["share_with_grants"] is None or not stock["granted"]:
            continue
        inner += (f'<div style="font-size:14px;line-height:1.6;color:{BODY};margin-top:10px">회사 주식 집중: 지금 보유한 '
                  f'{escape(stock["symbol"])}은 본인 증권 자산의 <b>{_pct(stock["share_now"], 0)}</b>이고, 예정된 주식 보상과 자사주로 '
                  f'받는 성과급(기준, 세후)을 받으면 <b>{_pct(stock["share_with_grants"], 0)}</b>가 됩니다. 받는 대로 일부를 분산하는 '
                  "원칙을 정해 두시길 권합니다.</div>")
    return _section("앞으로 들어올 돈", lead, inner)


def _allocation(data: dict[str, Any]) -> str:
    summary = data["view"].summary
    rows = sorted(summary.by_category.items(), key=lambda item: -item[1])
    if not rows or not summary.total_assets:
        return ""
    top_key, top_value = rows[0]
    immediate = data["kpis"]["immediate"]
    title_share = _pct(top_value / summary.total_assets, 0)
    lead = f"자산의 <b>{title_share}</b>가 {CATEGORY_LABELS.get(top_key, top_key)}에 집중되어 있습니다. "
    if immediate is not None:
        lead += f"즉시 활용 가능한 자금은 전체의 <b>{_pct(immediate / summary.total_assets)}</b>입니다."
    if "balance_sheet" in summary.incomplete_areas:
        lead += " 환율이 없어 원화로 바꾸지 못한 자산은 이 비중 계산에서 빠져 있습니다."
    bars = _stacked([(CATEGORY_LABELS.get(k, k), v, "") for k, v in rows[:4]]
                    + ([("기타", sum((v for _, v in rows[4:]), Decimal(0)), "")] if len(rows) > 4 else []))
    table = _table(["자산 유형", "금액", "비중"], [[CATEGORY_LABELS.get(k, k), _won(v), _pct(v / summary.total_assets)]
                                            for k, v in rows])
    return _section("자산 구성", lead, bars + '<div style="height:8px"></div>' + table,
                    note=escape(f"{CATEGORY_LABELS.get(top_key, top_key)} {title_share}"))


def _cash(cash: dict[str, Any], data: dict[str, Any]) -> str:
    months = data["kpis"]["immediate_months"]
    if cash["reserve_gap"] is None:
        # An amount without an FX rate makes the reserve check unknown rather than met or short.
        lead = ("즉시 활용 가능한 자금이나 월 지출 가운데 환율이 없어 원화로 바꾸지 못한 금액이 있어, "
                f"비상금 기준(월 지출 {cash['emergency_months_assumption']}개월) 충족 여부는 계산하지 않았습니다.")
    else:
        lead = (f"즉시 쓸 수 있는 돈은 <b>{_short(cash['immediate'])}</b>(월 지출 "
                f"{'미상' if months is None else f'{months:.1f}개월'}분)으로, 비상금 기준 {_short(cash['reserve_target'])}"
                + (f"까지 <b>{_short(cash['reserve_gap'])}</b>이 부족합니다." if cash["reserve_gap"] else "을 충족합니다."))
        swap = (data.get("advice") or {}).get("reserve_swap")
        if swap and swap["one_step"]:
            lead += (f" 다만 며칠 안에 팔 수 있는 주식·펀드가 {_short(swap['sellable_within_days'])}으로 부족분의 {swap['multiple']:.0f}배가 "
                     "넘으니, 월급을 쪼개 모으기보다 비중이 과한 종목이나 정리할 종목을 한 번 정리해 채우는 것이 자산 전체를 보는 방법입니다.")
    ladder = cash["ladder"]
    covered = (None if cash["reserve_target"] in (None, 0) or cash["immediate"] is None
               else cash["immediate"] / cash["reserve_target"])
    gauge = "" if covered is None else _gauge("비상금 기준 대비 즉시 가용 자금", covered, "")
    bars = _bars([(label, value, label == "즉시") for label, value in ladder])
    note = "" if covered is None else f"비상금 {_pct(covered, 0)} 확보"
    table = _table(["현금화 속도", "금액", "어떤 자산에서"],
                   [[label, _won(value), _ladder_sources(label, cash)] for label, value in ladder])
    return _section("현금·유동성", lead, gauge + bars + table, note=escape(note))


def _ladder_sources(label: str, cash: dict[str, Any]) -> str:
    """The kinds of asset on one rung, largest first (user review 2026-10-24: which assets can become cash)."""
    from smith.report_data import HERMES_LABEL, LIQUIDITY_LABELS
    if label == HERMES_LABEL:
        return "Hermes가 운용하는 계좌(Smith는 매도를 제안하지 않음)"
    key = next((k for k, v in LIQUIDITY_LABELS.items() if v == label), None)
    kinds = (cash.get("sources") or {}).get(key) or []
    parts = []
    for kind in kinds:
        count = f" {kind['count']}{kind['unit']}" if kind["count"] > 1 or kind["unit"] == "종목" else ""
        parts.append(f"{kind['label']}{count} {'미상' if kind['amount'] is None else _short(kind['amount'])}")
    return " · ".join(parts) or "-"


def _toss_cash_note(toss: dict[str, Any]) -> str:
    """The Toss API reports buying power, not the cash balance: say so instead of showing a silent gap."""
    if not toss.get("synced"):
        return ""
    note = " 토스증권 예수금(현금)은 API로 제공되지 않아 자동으로 수집하지 않습니다(<b>미수집</b>)."
    if toss["manual_records"]:
        note += f" 증권 계좌 현금은 직접 입력하신 값(기준일 {toss['manual_as_of']:%Y.%m.%d})을 썼습니다."
    else:
        note += " 직접 입력한 증권 계좌 현금도 없어, 즉시 쓸 수 있는 돈에서 증권 계좌 현금은 빠져 있습니다."
    power = toss["buying_power"]
    krw = [p["value"] for p in power if p["currency"] == "KRW"]
    usd = [p["value"] for p in power if p["currency"] == "USD"]
    if krw or usd:
        latest = max(p["observed_at"] for p in power)
        amounts = ([_won(sum(krw, Decimal(0)))] if krw else []) + ([f"{sum(usd, Decimal(0)):,.2f}달러"] if usd else [])
        note += (f" 참고로 토스의 매수 가능 금액은 {' · '.join(amounts)}({latest:%m.%d} 기준)이며, 예수금이나 "
                 "출금 가능액과 다를 수 있습니다.")
    return note


def _debt(debt: dict[str, Any], refinance: dict[str, Any] | None = None, policy: dict[str, Any] | None = None) -> str:
    """The loan line-up (the rates day's opening view): the variable-rate total and what each rate move, up or
    down, does to the month's interest (expert review 2026-10-22: the scenarios ran one way only), the next rate
    meeting and what one step would cost (§28), the credit lines with nothing drawn as liquidity room, and the
    refinancing verdict on the mortgage (§18.4)."""
    one = next((s for s in debt["shocks"] if s["shock"] == Decimal("0.01")), None)
    if debt["variable_total"] is None:
        lead = ("<b>변동금리</b>(몇 달마다 시장 금리에 맞춰 이자율이 바뀌는 방식) 대출 가운데 환율이 없어 원화로 바꾸지 못한 "
                "대출이 있어, 합계와 금리 변동 시 이자 변화액을 계산하지 않았습니다.")
    else:
        lead = (f"<b>변동금리</b> 대출 <b>{_short(debt['variable_total'])}</b>"
                + (f"은 금리가 1%p 오르면 월 이자가 약 <b>{_short(one['monthly_change'])}</b> 늘고, 1%p 내리면 그만큼 줄어듭니다."
                   if one and debt["variable_total"] else "이 있습니다."))
    unused = debt.get("unused_credit")
    for line in debt.get("credit_lines", []):
        if line["drawn"] == 0:
            lead += (f" {line['label']}은 잔액 0원이라 이자가 없고, <b>{_short(line['limit'])}</b> 한도는 유사시 바로 쓸 수 있어 "
                     "유동성 방어에 긍정적입니다(상환 대상 아님).")
        elif line["drawn"] is not None and line["limit"] is not None:
            lead += f" {line['label']}은 한도 {_short(line['limit'])} 중 {_short(line['drawn'])}을 쓰고 있습니다."
    verdict = ""
    if refinance is not None:
        verdict = _refinance_block(refinance)
    # Zero-centred: cuts to the left in blue (less interest), rises to the right in red, on today's balance.
    moves = [(f"{s['shock'] * 100:+.1f}%p", s["monthly_change"]) for s in debt["shocks"] if s["monthly_change"] is not None]
    bars = ""
    if any(value for _, value in moves):
        bars = (_diverging(moves)
                + f'<div style="font-size:12px;line-height:1.6;color:{MUTED};margin:4px 0 10px">{_meeting_note(debt, policy)}'
                  '지금 잔액에 금리 변화만 적용한 월 이자의 변화이며, 금리 전망이 아닙니다. 금리가 내리면 예금 이자도 함께 낮아지므로, '
                  '미리 갚을지는 대출 금리와 세후 예금 금리의 차이로 판단합니다.</div>')
    table = _table(["대출", "잔액", "금리", "방식", "만기"],
                   [[l["label"], _short(l["amount"]) + (f"(한도 {_short(l['credit_limit'])} 미사용)" if l.get("credit_limit") is not None
                                                        and l["amount"] == 0 else ""),
                     f"{l['rate'] * 100:.2f}%", RATE_TYPE_LABELS.get(l["rate_type"], l["rate_type"]),
                     (l["maturity"] or "-")[:7].replace("-", ".")]
                    for l in debt["loans"]])
    note = "" if one is None or not one["monthly_change"] else f"금리 ±1%p에 월 이자 ±{_short(one['monthly_change'])}"
    if refinance is not None and refinance["verdict"] != "unknown":
        note += (" · " if note else "") + ("대환 없이 유지 유리" if refinance["verdict"] == "keep" else "대환 검토")
    if unused:
        note += (" · " if note else "") + f"미사용 한도 {_short(unused)}"
    return _section("부채·금리 현황 및 금리 변동 민감도", lead, verdict + bars + table, note=escape(note))


def _meeting_note(debt: dict[str, Any], policy: dict[str, Any] | None) -> str:
    """The next Bank of Korea rate meeting and what one 0.25%p step would do to the month's interest (§28): the
    decision is never predicted. Empty without a calendar date or a variable-rate total."""
    meeting = (policy or {}).get("next_meeting")
    if meeting is None or not debt["variable_total"]:
        return ""
    step = debt["variable_total"] * Decimal("0.0025") / 12
    when = ("오늘 금통위가 기준금리를 정합니다(결과는 발표 뒤 다음 보고에 반영)." if policy.get("meeting_today")
            else f"다음 금통위는 {meeting.month}월 {meeting.day}일({editions.WEEKDAY_KO[meeting.weekday()]})입니다.")
    return f"{when} 0.25%p 움직이면 월 이자가 약 {_short(step)} 달라집니다. "


def _refinance_block(r: dict[str, Any]) -> str:
    """The mortgage against the market average on new bank mortgages, with a verdict pill (§18.4)."""
    rate = f"{r['loan_rate'] * 100:.2f}%"
    if r["verdict"] == "unknown":
        body = (f"{r['label']} {rate}를 견줄 시중 주담대 평균 금리(한국은행 신규취급액 기준) 자료가 아직 없어 대환 실익은 "
                "판정하지 않았습니다.")
        pill = _pill("판정 보류", "neutral")
    elif r["verdict"] == "keep":
        body = (f"{r['label']} {rate}는 한국은행이 집계한 은행 주담대 신규취급 평균 {r['market_rate'] * 100:.2f}%"
                f"({r['observed_on']:%Y년 %m월})와 견줘 {abs(r['gap']) * 100:.2f}%p {'낮아' if r['gap'] <= 0 else '높을 뿐이어서'}, "
                "현재 금리가 시중 최저 수준이므로 대환 없이 유지하시는 것이 유리합니다.")
        pill = _pill("대환 없이 유지 유리", "good")
    else:
        body = (f"{r['label']} {rate}는 시중 평균 {r['market_rate'] * 100:.2f}%({r['observed_on']:%Y년 %m월})보다 "
                f"{r['gap'] * 100:.2f}%p 높아 한 해 약 {_short(r['yearly_saving'])}을 더 내는 셈입니다. 중도상환수수료를 잔액의 "
                f"{r['fee_rate'] * 100:.1f}%로 보면 약 {_short(r['fee'])}이며 {r['break_even_months']:.0f}개월이면 회수합니다. "
                "은행 두 곳 이상에서 대환 견적을 받아 보시길 권합니다.")
        pill = _pill("대환 검토 권유", "warn")
    return (f'<div style="background:{SOFT};border-radius:16px;padding:14px 16px;margin:4px 0 12px">'
            f'<div style="font-size:12px;font-weight:600;color:{MUTED};margin-bottom:6px">대환대출 금리 경쟁력 {pill}</div>'
            f'<div style="font-size:14px;line-height:1.6;color:{BODY}">{escape(body)}</div></div>')


def _months_text(value: Decimal) -> str:
    return f"{value:.1f}개월"


def _stress_lead(st: dict[str, Any], *, detail: bool) -> str:
    """The stress scenario in words: the four assumptions with their sizes, then how far cash and stock sales
    carry the household, an uncovered lease deposit, and that rates, stocks and home prices did move together in
    2022 (user review 2026-10-18: "나쁜 일이 겹치는 한 해" left the client asking what and whether it can happen)."""
    a, m = st["assumptions"], st["monthly"]
    horizon = a["income_stop_months"]
    # An extreme check, said as one (user review 2026-10-18: "비현실적"): the whole result before the cash-only gap.
    text = (f"변동금리 +{a['rate_shock'] * 100:.1f}%p, 가장 큰 소득 {horizon}개월 중단, 집값 {a['property_drop'] * 100:.0f}%·"
            f"주가 {a['stock_drop'] * 100:.0f}% 하락이 한 해에 모두 오는 드문 극단을 가정해도 ")
    if m["burn"] is None:
        text += "원화로 환산하지 못한 금액이 있어 버티는 기간은 계산하지 못했습니다."
    elif m["burn"] <= 0:
        text += f"매월 약 <b>{_short(-m['burn'])}</b>이 남아 현금이 줄지 않습니다."
    elif st["months_cash"] is None:
        text += f"매월 약 {_short(m['burn'])}이 모자라며, 즉시 쓸 수 있는 돈에 환산하지 못한 금액이 있어 버티는 개월은 미상입니다."
    elif st["covers_scenario"]:
        text += (f"즉시 쓸 수 있는 돈 {_short(st['immediate'])}으로 {horizon}개월을 모두 버티고 약 "
                 f"<b>{_short(st['cash_after_scenario'])}</b>이 남습니다.")
    elif st["after_sales"] is not None and st["after_sales"] >= 0:
        text += (f"주식·펀드를 {a['stock_drop'] * 100:.0f}% 내린 값에 팔면 {horizon}개월을 버티고 약 {_short(st['after_sales'])}이 "
                 f"남습니다. 다만 현금만으로는 <b>{_months_text(st['months_cash'])}</b>이라, 주식을 싼값에 팔지 않으려면 비상금이 "
                 "먼저입니다.")
    else:
        text += (f"버티지 못합니다: 현금만으로는 <b>{_months_text(st['months_cash'])}</b>"
                 + (f", 주식·펀드를 팔아도 {_months_text(st['months_with_sales'])}" if st["months_with_sales"] is not None else "")
                 + f"까지만 버팁니다({horizon}개월 중).")
    lease = st["lease"]
    if STRESS_LEASE_FLAG in st["flags"]:
        need = ("보증금 " + _short(lease["amount"]) if lease.get("basis") != "reverse_jeonse" else
                f"보증금 {_short(lease['amount'])} 가운데 새 세입자 보증금으로 메우지 못하는 차액 {_short(lease['need'])}")
        text += f" 다만 {lease['due']:%Y년 %m월} 돌려드릴 {need}은 이 가정에서 약 {_short(lease['available'])}밖에 마련하지 못합니다."
    elif lease is not None and lease.get("basis") == "reverse_jeonse" and lease.get("covered"):
        text += f" {lease['due']:%Y년 %m월} 보증금 반환도 차액 {_short(lease['need'])}만 준비하면 되어 감당할 수 있습니다."
    elif lease is not None and lease.get("basis") == "sale" and lease.get("covered"):
        text += (f" {lease['due']:%Y년 %m월} 보증금은 계획대로 만기에 팔면 집값이 {a['property_drop'] * 100:.0f}% 내려도 매도 대금으로 "
                 "돌려드릴 수 있습니다.")
    p = stress.PRECEDENT
    text += (f" 금리·주가·집값이 함께 움직인 해는 실제로 있었습니다: {p['year']}년 기준금리 {p['base_rate'][0]}→{p['base_rate'][1]}, "
             f"코스피 {p['kospi']}, 서울 아파트 실거래가지수 {p['seoul_apartments']}({p['seoul_apartments_vintage']}). "
             # Client 2026-10-24: a gap in pay is not a near-term risk; the stress year stays as a tail test.
             "소득 중단은 가능성이 낮은 극단 가정(휴직·질병 등)으로, 그런 일이 겹쳐도 버티는지 보려는 것입니다.")
    return text


def _stress_note(st: dict[str, Any], estate: dict[str, Any] | None) -> str:
    """The verdict pill, with the stale-deals pill when the lease result rests on the official deals (the
    reverse-jeonse gap or the sale price): a derived figure that changes the judgement (review 2026-10-24)."""
    icon, label, _ = STRESS_VERDICT[st["verdict"]]
    note = escape(f"{icon} {label}")
    return _stale_note(note, estate) if stress.uses_deals(st) else note


def _stress(st: dict[str, Any], estate: dict[str, Any] | None = None) -> str:
    """The composite stress scenario in full (report design §16.2): the lead, the months bars (capped at the
    scenario length) and the before/after table. All figures come from stress.test; unknown shows as 미상."""
    m = st["monthly"]
    rows = [("현금만으로", st["months_cash"], True), ("주식·펀드 매도 포함", st["months_with_sales"], False)]
    bars = _bars(rows, value_text=_months_text) if m["burn"] is not None and m["burn"] > 0 else ""
    usual_net = None if m["inflow"] is None or m["outflow"] is None else m["inflow"] - m["outflow"]
    table_rows = [["월 수입", _won(m["inflow"]), _won(None if m["inflow"] is None or m["lost_income"] is None
                                                    else m["inflow"] - m["lost_income"])],
                  ["월 지출(이자 포함)", _won(m["outflow"]), _won(None if m["outflow"] is None or m["extra_interest"] is None
                                                            else m["outflow"] + m["extra_interest"])],
                  ["월 남는 돈", _won(usual_net), _won(None if m["burn"] is None else -m["burn"])],
                  ["순자산 변화", "-", "미상" if st["net_worth_drop"] is None else
                   f"-{_short(st['net_worth_drop'])}" + ("" if st["net_worth_drop_ratio"] is None
                                                       else f" ({_pct(st['net_worth_drop_ratio'], 0)})")]]
    for prop in st["properties"]:
        if prop["ltv"] is not None:
            table_rows.append([f"{prop['label']} LTV(담보대출 기준)", _pct(prop["ltv"], 0), _pct(prop["ltv_stressed"], 0)])
    lease = st["lease"]
    if lease is not None:
        covered = ("미상" if lease["covered"] is None else
                   f"{'가능' if lease['covered'] else '부족'}(가용 {_short(lease['available'])})")
        label = "보증금 반환" if lease.get("basis") != "reverse_jeonse" else "보증금 반환, 역전세 차액"
        table_rows.append([f"{label}({lease['due']:%Y.%m})", _won(lease["need"] if lease.get("need") is not None else lease["amount"]),
                           covered])
    horizon = Decimal(st["assumptions"]["income_stop_months"])
    gauge = ("" if st["months_cash"] is None else
             _gauge(f"현금만으로 버티는 기간({horizon:.0f}개월 중)", st["months_cash"] / horizon, ""))
    return _section(STRESS_TITLE, _stress_lead(st, detail=True) + _sources(stress.PRECEDENT_SOURCES),
                    gauge + bars + _table(["항목", "평소", "가정한 해"], table_rows), note=_stress_note(st, estate),
                    brief=None)


def _stress_summary(st: dict[str, Any], estate: dict[str, Any] | None = None) -> str:
    """The overview's one-paragraph version: verdict and months, no table (the household day has it)."""
    return _section(STRESS_TITLE, _stress_lead(st, detail=False) + " 자세한 표는 생활·목표 보고에 있습니다."
                    + _sources(stress.PRECEDENT_SOURCES), "", note=_stress_note(st, estate), brief=None)


def _spending(room: dict[str, Any] | None, split: dict[str, Any] | None = None) -> str:
    """Guilt-free spending room (report design §16.3): the surplus, what the reserve and contractual payments
    take every month, the room on that basis, and the smaller room when the planned goals are kept too.
    First the month's balance of fixed costs and discretionary spending (§18.6). Nothing when an input is
    unknown (the proposals say so)."""
    balance = ""
    if split is not None and split["inflow"]:
        segments = [("고정비", split["fixed"], ""), ("재량비", split["discretionary"], ""), ("적립·투자", split["transfers"], "")]
        if split["left"] > 0:
            segments.append(("남는 돈", split["left"], ""))
        balance = (f"수입 {_short(split['inflow'])} 가운데 고정비(대출 상환·보험·주거·세금과 그렇게 표시하신 지출)가 "
                   f"<b>{_pct(split['fixed_share'], 0)}</b>, 재량비가 <b>{_pct(split['discretionary_share'], 0)}</b>, 본인 계좌 적립·투자가 "
                   f"{_pct(split['transfers'] / split['inflow'], 0)}입니다. ")
        balance_chart = _stacked(segments)
    if room is None:
        return "" if not balance else _section("한 달 소비의 균형", balance, balance_chart)
    safe, with_goals = max(room["room"], Decimal(0)), max(room["room_with_goals"], Decimal(0))
    committed = [("비상금 채우기(주식 일부 정리로 일시 충당)" if label == "비상금 채우기" and room.get("reserve_swap") else label, amount)
                 for label, amount in room["committed"]]
    rows = ([("남는 돈", room["surplus"], False)] + [(label, amount, False) for label, amount in committed]
            + [("안심 소비 한도(약정 기준)", safe, True)])
    if room["planned"]:
        rows += [(label, amount, False) for label, amount in room["planned"]] + [("계획한 목표까지 지킬 때", with_goals, False)]
    if room["room"] >= 0:
        lead = (f"이번 달에 쓰셔도 비상금과 약정이 늦어지지 않는 <b>안심 소비 한도</b>는 <b>{_short(room['room'])}</b>입니다(남는 돈 "
                f"{_short(room['surplus'])} − 비상금·약정 적립 {_short(room['committed_total'])}).")
        if room["planned"]:
            lead += (f" 계획한 목표(매월 {_short(room['planned_total'])})까지 지금 속도로 지키려면 약 "
                     f"<b>{_short(room['room_with_goals'])}</b>입니다." if room["room_with_goals"] >= 0 else
                     f" 다만 계획한 목표(매월 {_short(room['planned_total'])})까지 지금 속도로 지키려면 매월 "
                     f"{_short(-room['room_with_goals'])}이 모자라, 목표의 시점·금액을 손보지 않는 한 그 목표는 늦어집니다.")
        if room.get("reserve_swap"):
            lead += (f" 비상금 부족분 {_short(room['reserve_swap'])}은 보유 주식 일부를 정리해 한 번에 채우는 방안을 제안했으므로 "
                     "매월 적립에서 뺐습니다.")
        lead += " 쌓아 두는 것이 목적이 아니라면, 시간이 지나도 남는 경험에 먼저 배정하시길 권합니다."
    else:
        lead = (f"남는 돈 {_short(room['surplus'])}보다 비상금과 약정에 매월 필요한 적립 {_short(room['committed_total'])}이 "
                f"<b>{_short(-room['room'])}</b> 더 큽니다. 지금은 마음 편히 쓸 여유가 없고, 약정과 지출을 먼저 손봐야 합니다.")
    table = _table(["항목", "매월"], [[label, _won(value)] for label, value, _ in rows])
    chart = (balance_chart + '<div style="height:12px"></div>') if balance else ""
    # The room and the goals' pace lead; the month's split follows (the household day's hero bar shows it too).
    return _section("마음 편히 써도 되는 돈", lead + (" " + balance if balance else ""), chart + _bars(rows) + table,
                    note=escape(f"안심 소비 한도 {_short(safe)}"))



def _holdings(data: dict[str, Any], story: dict[str, Any], full: bool) -> str:
    """Holdings review (report design §12.7): the figures code computed, Smith's view of the whole, a table
    with an opinion per holding, and cards for every opinion other than hold. Thursday lists only those."""
    review, sec = data["holdings"], data["sectors"]["securities"]
    positions = review["positions"]
    opinions = story.get("holdings") or {}
    active = [p for p in positions if _of(opinions, p).get("stance") not in (None, "hold")]
    if not full:
        if not active:
            return ""
        rows = "".join(f'<div style="font-size:14px;line-height:1.6;color:{BODY};margin:4px 0">· <b style="font-weight:600;'
                       f'color:{INK}">{escape(p["name"])}</b> {_stance(_of(opinions, p)["stance"])} — '
                       f'{escape(_first_sentence(_of(opinions, p)["reason"]))}</div>' for p in active)
        return _section("보유 종목 의견", "매수·정리 의견이 있는 종목만 짧게 말씀드립니다. 근거는 월요 보고에서 자세히 드립니다.", rows,
                        note=escape(f"{len(active)}개 종목"))
    if not positions:
        return _section("보유 종목 점검", "종목 단위 정보가 있는 증권이 없습니다.", "")
    listed = [p for p in positions if p["value"] is not None]
    segments = [(str(p["name"])[:12], p["value"], "") for p in listed[:4]]
    if len(listed) > 4:
        segments.append((f"기타({len(listed) - 4}종)", sum((p["value"] for p in listed[4:]), Decimal(0)), ""))
    inner = _stacked(segments) + '<div style="height:8px"></div>'
    inner += _table(["종목", "평가액", "비중", "수익률", "Smith 의견"],
                    [[_named(p), _short(p["value"]), _pct(p["share"]), _return(p["return_on_cost"]),
                      Raw(_pill("Hermes 운용", "neutral")) if p["hermes"] else
                      Raw(_stance(_of(opinions, p)["stance"])) if _of(opinions, p).get("stance") else "의견 없음"]
                     for p in positions])
    # The employer's stock and any holding above the limit come first: the double concentration is the
    # first investment matter (report design §17.4).
    active.sort(key=lambda p: (not p.get("must_opine"), -(p["value"] or 0)))
    inner += "".join(_opinion_card(p, _of(opinions, p)) for p in active)
    held = [p for p in positions if _of(opinions, p).get("stance") == "hold"]
    if held:
        rows = "".join(f'<div style="font-size:13px;line-height:1.6;color:{BODY};margin:3px 0">· <b style="font-weight:600;'
                       f'color:{INK}">{escape(p["name"])}</b> — {escape(_first_sentence(_of(opinions, p)["reason"]))}</div>'
                       for p in held)
        inner += f'<div style="font-size:12px;font-weight:600;color:{MUTED};margin:16px 0 4px">유지 의견</div>{rows}'
    # Opinions written by the model; the code's fallback opinions alone do not make the holdings "opined".
    opined = any(not o.get("by_code") for o in opinions.values())
    counts = {}
    for p in positions:
        stance = _of(opinions, p).get("stance")
        if stance:
            counts[stance] = counts.get(stance, 0) + 1
    note = f"{len(positions)}개 종목 상세 점검" + "".join(f" · {STANCES[s]} {n}" for s, n in counts.items() if s != "hold")
    return _section("보유 종목 점검", _holdings_lead(review, sec, opined), inner, note=escape(note))


def _of(opinions: dict[str, Any], position: dict[str, Any]) -> dict[str, Any]:
    """The opinion on a position; none for a Hermes-managed one, even when it shares the symbol."""
    return {} if position["hermes"] else opinions.get(position["key"], {})


def _holdings_lead(review: dict[str, Any], sec: dict[str, Any], opined: bool) -> str:
    positions = [p for p in review["positions"] if not p["hermes"]]
    lead = ""
    if positions:
        top = positions[0]
        stocks, tiny = principles.stock_count(positions)
        # How many individual companies, shown not judged (review 2026-10-20 replaced the "follow" check); in the
        # first sentence, so the two-sentence brief keeps the opinion note below.
        count = (f", 개별 종목은 {stocks}개" + (f"(1% 미만 {tiny}개)" if tiny else "")) if stocks else ""
        lead = (f"보유 종목 {len(positions)}개 {_short(review['total'])} 가운데 가장 큰 종목({escape(str(top['name']))})이 "
                f"<b>{_pct(review['top1_share'])}</b>, 상위 5개가 <b>{_pct(review['top5_share'])}</b>이고 해외 상장 비중은 "
                f"{_pct(review['overseas_share'])}{count}입니다.")
    if not opined:  # Second, so the two-sentence lead still explains an empty opinion column.
        lead += " 이번에는 종목별 의견을 만들지 못해 숫자만 보여 드립니다."
    employer = [p for p in positions if p.get("employer")]
    if employer:
        lead += (f" {', '.join(escape(str(p['name'])) for p in employer)}은 월급과 주식 보상이 나오는 회사의 주식이라, 앞으로 벌 소득과 "
                 "지금 가진 자산이 한 회사에 함께 묶인 <b>이중 집중</b> 상태입니다. 가장 먼저 관리할 종목입니다.")
    if review["concentrated"]:
        names = ", ".join(escape(str(p["name"])) for p in positions if p["concentrated"])
        lead += (f" 한 종목이 주식의 {_pct(review['concentration_limit'], 0)}를 넘으면 그 회사 한 곳의 소식에 자산이 크게 "
                 f"흔들리는 <b>집중</b> 상태로 봅니다. 이 기준은 가정이며, 지금 여기에 해당하는 종목은 {names}입니다.")
    if sec["unrealized_gain"] is not None:
        lead += (f" 종목별 <b>미실현 손익</b>(팔지 않아 확정되지 않은 손익) 합계는 {_signed_short(sec['unrealized_gain'])}"
                 + (f"이며, 매입 원가를 아는 {sec['unrealized_gain_counted']}개 종목만의 부분 합계입니다."
                    if sec["unrealized_gain_missing"] else "입니다."))
    if review["unlisted"]:
        lead += (f" 종목 코드가 없는 증권 {_short(review['unlisted'])}은 비중 계산에는 넣었지만, 무엇인지 몰라 종목 의견에서는 "
                 "빠졌습니다.")
    if sec.get("hermes"):
        lead += (f" Hermes가 운용하는 자산 {_short(sec['hermes'])}에는 의견을 드리지 않습니다(필요하면 Hermes와 상의하십시오).")
    return lead


def _named(position: dict[str, Any]) -> str:
    kind = principles.kind(position)
    return str(position["name"]) + ("" if kind == "stock" else f" ({principles.KIND_LABELS[kind]})")


# A standard is an assumption to review, not a rule the client broke (user feedback 2026-10-20): "조정 권장", not 어긋남.
CHECK_STATUS = {"ok": ("●", "충족", "#05b169"), "breach": ("■", "조정 권장", "#cf202f"), "plan": ("▲", "계획 있음", "#b27b00"),
                "watch": ("▲", "확인 필요", "#b27b00"), "unknown": ("○", "판단 불가", MUTED)}


def _policy(data: dict[str, Any], story: dict[str, Any], full: bool) -> str:
    """Investment principles and the household's policy (report design §13): the checks code computed, Smith's
    strategy grounded in the principles, and the sources of every principle the report applied."""
    checks = data.get("policy") or []
    if not checks:
        return ""
    if not full:
        off = [c for c in checks if c.status in ("breach", "plan", "watch")]
        if not off:
            return ""
        rows = "".join(f'<div style="font-size:14px;line-height:1.6;color:{BODY};margin:4px 0">· {_check_status(c.status)} '
                       f'<b style="font-weight:600;color:{INK}">{escape(c.title)}</b> — {escape(c.current)}</div>' for c in off)
        return _section("투자 기준 점검", "지금 상황에서 조정을 검토할 항목만 보여 드립니다. 원칙과 근거는 월요 보고에 있습니다.", rows)
    table = _table(["기준", "우리 집 기준", "지금", "판정"],
                   [[c.title, c.standard, c.current, Raw(_check_status(c.status))] for c in checks])
    view = ""
    if story.get("portfolio"):
        view = (f'<div style="background:{SOFT};border-radius:24px;padding:18px 20px;margin-top:14px">'
                f'<div style="font-size:12px;font-weight:600;color:{MUTED};margin-bottom:6px">Smith의 투자 전략 · 주식 전체</div>'
                f'<div style="font-size:15px;line-height:1.65;color:{INK}">{escape(story["portfolio"])}</div>'
                f'{_principle_chips(story.get("portfolio_principles") or [])}'
                f'{_sources(story.get("portfolio_links") or [])}</div>')
    # The principles' quotations and sources no longer print in the mail (report design §18.1): they stay in
    # the report's data for the e-mail answers ("왜 이렇게 판단했어?"), and the opinion cards keep short links.
    lead = ("경험 많은 투자자들이 공개한 투자 원칙을 우리 집 기준으로 옮겼습니다. 법이 아니라 출처가 있는 관점이자 판단의 출발점이며, "
            "기준의 숫자는 Smith의 가정이라 고객님 사정에 맞게 조정할 수 있습니다. 조정 권장 항목부터 보시는 것이 순서입니다.")
    off = sum(1 for c in checks if c.status in ("breach", "plan", "watch"))
    return _section("투자 원칙과 우리 집 투자 기준", lead, table + view,
                    note=escape(f"{len(checks)}개 기준 · 검토 {off}개" if off else f"{len(checks)}개 기준 모두 충족"))


def _check_status(status: str) -> str:
    """A policy check as a colour pill: green met, grey planned, orange to confirm, red breached."""
    return _pill(CHECK_STATUS[status][1], CHECK_PILL[status])


def _principle_chips(cited: list[Any]) -> str:
    """The principles an opinion applies, linked to their sources."""
    if not cited:
        return ""
    items = " · ".join(_cell((f"{p.investor} · {p.title}", p.url)) for p in cited)
    return f'<div style="font-size:12px;color:{MUTED};margin-top:6px">근거 원칙: {items}</div>'


def _return(ratio: Decimal | None) -> str:
    """Return on cost; unknown without an average cost (never 0%)."""
    return "미상" if ratio is None else _delta_pct(ratio)


def _stance(stance: str) -> str:
    """The opinion as a colour pill (user spec 2026-10-18): green add, grey hold, orange trim, red exit."""
    return _pill(STANCES[stance], STANCE_PILL[stance])


def _opinion_card(position: dict[str, Any], opinion: dict[str, Any]) -> str:
    """An opinion other than hold: why, where the money goes, Smith's precondition, when to reconsider."""
    rows = [("이유", escape(opinion["reason"]) + _sources(opinion["links"]))]
    if opinion["alternative"]:
        rows.append(("대안·순서", escape(opinion["alternative"])))
    if opinion["condition"]:
        rows.append(("전제 조건", escape(opinion["condition"])))
    rows.append(("다시 판단할 조건", escape(opinion["reconsider"])))
    if opinion.get("principles"):
        rows.append(("근거 원칙", " · ".join(_cell((f"{p.investor} · {p.title}", p.url)) for p in opinion["principles"])))
    detail = "".join(f'<tr><td style="width:92px;vertical-align:top;font-size:12px;font-weight:600;color:{MUTED};'
                     f'padding:6px 8px 6px 0">{escape(k)}</td><td style="font-size:14px;line-height:1.6;color:{BODY};'
                     f'padding:6px 0">{v}</td></tr>' for k, v in rows)
    figures = (f'{_short(position["value"])} · 비중 {_pct(position["share"])} · 수익률 {_return(position["return_on_cost"])}')
    badge = ""
    if position.get("employer"):
        badge += (f' <span style="display:inline-block;background:{STRONG};color:{INK};font-size:11px;font-weight:600;'
                  f'border-radius:100px;padding:2px 10px;margin-left:6px">이중 집중</span>')
    source = ("" if not opinion.get("by_code") else
              f'<div style="font-size:12px;color:{MUTED};margin-top:8px">이 의견은 Smith의 집중도 기준에 따라 계산으로 작성했습니다.</div>')
    return (f'<div style="border:1px solid {HAIRLINE};border-radius:24px;padding:18px 20px;margin-top:12px">'
            f'<div style="font-size:16px;font-weight:600;color:{INK}">{escape(position["name"])} {_stance(opinion["stance"])}{badge}</div>'
            f'<div style="font-family:{MONO};font-size:12px;color:{MUTED};margin:4px 0 6px">{escape(figures)}</div>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{detail}</table>{source}</div>')


def _deals_stale(estate: dict[str, Any] | None) -> str:
    """The short text beside apartment-deal figures when the last refresh failed (user review 2026-10-24: say it
    where the deals are shown, not at the top of the letter). Empty when the deals are fresh."""
    stale = (estate or {}).get("deals_stale")
    if not stale:
        return ""
    when = stale["last_success_on"]
    return f"실거래가 새로 받지 못함 · {when:%m/%d}에 받은 값" if when else "실거래가 새로 받지 못함"


def _stale_note(note: str, estate: dict[str, Any] | None) -> str:
    """A section's escaped one-line note with the stale-deals pill after it."""
    stale = _deals_stale(estate)
    return note + (" " if note else "") + _pill(stale, "warn") if stale else note


def _real_estate(estate: dict[str, Any], figures: dict[str, Any] | None = None) -> str:
    props = estate["properties"]
    if not props:
        return ""
    figures = figures or {}
    dates = sorted({f"{p['valued_on']:%Y.%m}" for p in props})
    from smith.report_data import VALUATION_LABELS
    basis = ", ".join(sorted({VALUATION_LABELS.get(p.get("valuation_method"), "원장 입력가") for p in props}))
    lead = (f"부동산이 자산의 <b>{_pct(estate['share_of_assets'], 0)}</b>를 차지합니다. 평가액의 기준은 <b>{escape(basis)}</b>"
            f"(기준일 {', '.join(dates)})입니다.")
    estimate, value = figures.get("property_estimate"), figures.get("property_value")
    reference = figures.get("property_estimate_reference")
    if estimate is not None and value is not None:
        lead += (f" 같은 단지·면적의 최근 6개월 실거래 중간값 합계는 <b>{_short(estimate)}</b>으로 평가액과 "
                 f"{_signed_short(estimate - value)} 차이입니다.")
    elif reference is not None and value is not None:
        lead += (f" 같은 단지·면적의 최근 6개월 실거래 중간값 합계는 <b>{_short(reference)}</b>으로 평가액과 "
                 f"{_signed_short(reference - value)} 차이입니다. 다만 {escape(', '.join(figures['property_estimate_thin']))}은 "
                 f"거래가 {MIN_BASIS}건 미만이라 이 합계는 참고용입니다.")
    lead += " <b>LTV</b>(집값 대비 대출 비율)가 낮을수록 시세 하락에 견딜 여유가 큽니다."
    bars = _stacked([(p["label"], p["value"], "") for p in props]) + '<div style="height:8px"></div>'
    table = _table(["부동산", "평가액", "관련 부채", "LTV"],
                   [[p["label"], _short(p["value"]), _short(p["debt"]), _pct(p["ltv"])] for p in props])
    market = [p for p in props if p.get("market")]
    if market:
        lines = []
        for p in market:
            m = p["market"]
            if m["estimate"] is not None and p["value"]:
                diff = p["value"] / m["estimate"] - 1
                lines.append(f"{p['label']}: 최근 6개월 같은 단지·같은 면적 실거래 {m['estimate_basis']}건의 중간값은 "
                             f"<b>{_short(m['estimate'])}</b>으로, 원장 평가액보다 {_pct(abs(diff), 0)} "
                             f"{'낮습니다' if diff > 0 else '높습니다'}"
                             + (" (거래가 적어 참고용)" if m["estimate_basis"] < MIN_BASIS else "") + ".")
            if m["jeonse"] is not None:
                lines.append(f"{p['label']}: 최근 6개월 전세 중간값 <b>{_short(m['jeonse'])}</b>"
                             f"(계약 {m['jeonse_basis']}건, {_short(m['jeonse_range'][0])}~{_short(m['jeonse_range'][1])}"
                             + (", 계약이 적어 참고용" if m["jeonse_basis"] < MIN_BASIS else "") + ").")
        lead += " " + " ".join(lines) + " 평가액을 실거래 기준으로 바꿀지는 고객님 판단으로 정해 주십시오."
        table += _table(["부동산", "실거래 추정(6개월 중간값)", "6개월 전 대비", "12개월 거래"],
                        [[p["label"], _short(p["market"]["estimate"]), _delta_pct(p["market"]["change_6m"]),
                          f"{p['market']['trades_12m']}건"] for p in market])
    note = escape(f"{len(props)}채 · 기준: {basis}")
    return _section("부동산·주거", lead, bars + table, note=_stale_note(note, estate) if market else note)


def _move_targets(data: dict[str, Any]) -> str:
    """The move-up spread (report design §18.3): the two homes' net proceeds against each target's cost, now
    and six months ago, and after the expected pay. The property day's opening view."""
    plan = data.get("move_plan")
    if not plan:
        return ""
    pr, pay = plan["proceeds"], plan["pay"]
    case = pr["case"] if pr["tax_known"] else None
    exempt_homes = [h["label"] for h in plan["homes"] if h["tax_case"] == "exempt"]
    general_homes = [h["label"] for h in plan["homes"] if h["tax_case"] == "general"]
    # The headline sentence carries its own fallback (the brief keeps two sentences and the cheapest target comes
    # first): the case the figure leads with and what the other case would give (client e-mail 2026-10-21: the
    # exemption leads once the residence is known).
    lead = (f"{plan['horizon']:%Y년 %m월} 무렵 두 집을 모두 팔면 손에 남는 돈은 약 <b>{_short(pr['headline'])}</b>"
            + (f"입니다(실거래·원장 기준 매도가에서 담보대출·보증금과 1세대 1주택 비과세 기준의 양도세를 뺀 값, 요건을 못 갖추면 일반 "
               f"과세로 약 {_short(pr['general'])})." if case == "exempt" else
               f"입니다(실거래·원장 기준 매도가에서 담보대출·보증금·양도세를 뺀 값: {', '.join(exempt_homes)}은 비과세 기준, "
               f"{', '.join(general_homes)}은 거주 기간이 확인되지 않아 일반 과세)." if case == "mixed" else
               "입니다(실거래·원장 기준 매도가에서 담보대출·보증금·양도세 일반 과세분을 뺀 값)." if pr["tax_known"]
               else "입니다(매도가에서 담보대출·보증금을 뺀 값, 취득가를 몰라 양도세는 빼지 않음)."))
    if case == "exempt":
        lead += (" 두 집 모두 2년 보유·거주가 확인되어 12억 원 초과분만 과세로 보았고, 두 집을 판 뒤 새 집을 사는 전제입니다"
                 "(현행 법령 확인 필요).")
    elif pr["exempt"] is not None and pr["tax_known"]:
        lead += f" 각 집에 1세대 1주택 비과세가 적용되면 약 {_short(pr['exempt'])}까지 늘어나며, 현행 법령 확인이 필요합니다."
    for h in plan["homes"]:
        if h["tax"] is not None and h["tax_case"] == "exempt":
            lead += f" {h['label']}의 양도세는 비과세 기준 약 {_short(h['tax']['exempt'])}(요건 미충족 시 약 {_short(h['tax']['general'])})입니다."
        elif h["tax"] is not None:
            lead += (f" {h['label']}의 양도세는 일반 과세 약 {_short(h['tax']['general'])}, 비과세 요건 충족 시 약 "
                     f"{_short(h['tax']['exempt'])}입니다.")
    if pay["base"] is not None:
        lead += (f" 그때까지 받을 성과급·주식 보상은 확정·예상분 세후 약 <b>{_short(pay['base'])}</b>(확정분만 {_short(pay['adverse'])})이며, "
                 "지급 전까지는 확정이 아닙니다.")
    trends = {"widening": "벌어지는 중", "narrowing": "좁혀지는 중", "flat": "큰 변화 없음"}
    rows, bars = [], [("두 집 매도 순수익", pr["headline"], True)]
    for t in plan["targets"]:
        # The narrative knows each target by its alias ("후보 1"); the mail pairs the alias with the name.
        named = f"{t['alias']} · {t['label']}"
        bars.append((named, t["cost_with_fees"], False))
        hold = t["change_hold"]
        rows.append([named, f"{_short(t['price'])} · {t['price_basis']}",
                     f"판단 보류(최근 {hold['recent']}·이전 {hold['older']}건)" if hold else _delta_pct(t["change_6m"]),
                     _short(t["spread"]), _short(t["spread_with_pay"]),
                     "판단 보류" if t["trend_hold"] else trends.get(t["trend"], "-")])
    nearest = min((t for t in plan["targets"] if t["spread"] is not None), key=lambda t: t["spread"], default=None)
    if nearest is not None:
        # First (pre-review check 2026-10-18): the gap to the nearest target and what the expected pay closes of it
        # is the day's answer; the two-sentence lead used to stop before it.
        pays = [f"예정 소득 반영 시 {_short(nearest['spread_with_pay'])}" if nearest["spread_with_pay"] is not None else "",
                f"확정분만 반영 시 {_short(nearest['spread_with_pay_adverse'])}" if nearest["spread_with_pay_adverse"] is not None else ""]
        pays = ", ".join(p for p in pays if p)
        lead = (f"필요 추가 자금이 가장 작은 곳은 <b>{escape(nearest['alias'])} · {escape(nearest['label'])}</b>으로 약 "
                f"<b>{_short(nearest['spread'])}</b>" + (f"({pays})" if pays else "")
                + (", 세전 기준으로 6개월 전보다 차이가 " + trends[nearest["trend"]] + "입니다." if nearest["trend"] else "입니다.")
                + " " + lead)
    lead += " 핵심 변수는 성과급·자사주 유입과 그 주가, 그리고 매도 순서에 따른 양도세입니다."
    table = _table(["후보 단지", "시세(기준)", "6개월 전 대비", "필요 추가 자금", "예정 소득 반영 후", "추이(세전)"], rows)
    # The spread trend (user review 2026-10-18): each target's gap six months ago against now, both before tax.
    trend = _paired_bars([(f"{t['alias']} · {t['label']}", t["spread_6m_ago"], t["spread_before_tax"])
                          for t in plan["targets"] if not t["trend_hold"]])
    # A trend needs MIN_BASIS deals in both half-years on both sides; the counts say why it is withheld.
    homes_hold = "; ".join(f"{h['label']}의 실거래가 최근 6개월 {h['recent']}건·이전 6개월 {h['older']}건" for h in pr["hold"])
    withheld = (f'<div style="font-size:12px;color:{MUTED};margin-top:4px">6개월 추이는 판단 보류: '
                + (f"{escape(homes_hold)}으로" if homes_hold else "일부 단지의 실거래가")
                + f" 비교에 필요한 {MIN_BASIS}건에 못 미칩니다.</div>" if any(t["trend_hold"] for t in plan["targets"]) else "")
    trend = (f'<div style="font-size:13px;font-weight:600;color:{INK};margin:14px 0 6px">필요 추가 자금 추이(세전, 6개월 전 → 지금)'
             f'</div>{trend}{withheld}' if trend else withheld)
    note = "" if nearest is None else f"최소 추가 자금 {_short(nearest['spread'])}"
    return _section("목표 단지 vs 보유 주택 2채 시세 스프레드", lead, _bars(bars) + trend + table,
                    note=_stale_note(escape(note), data["sectors"]["real_estate"]))


def _delta_pct(ratio: Decimal | None) -> str:
    return "-" if ratio is None else f"{'+' if ratio > 0 else ''}{ratio * 100:.1f}%"


def _pension_stack(data: dict[str, Any]) -> str:
    """The pension stack (report design §18.5): every pipeline's after-tax monthly payout against the monthly
    retirement target, the readiness ratio, and what is still missing. The tax day's opening view."""
    stack = data.get("pension_stack")
    if stack is None:
        return _section("노후 현금흐름 준비율", "은퇴 후 월 생활비 목표(지금 물가 기준)를 알려 주시면 4대 연금을 합쳐 준비율을 보여 드립니다.", "")
    target, head = stack["target"], stack["retirement_age"]
    tiers = _retirement_tiers(stack)
    # The two sentences the card keeps (report design §21): which ages, then each age's after-tax total and ratio.
    lead = ((f"{head}세 은퇴를 기준으로 계산했고 50·55·60세도 함께 보입니다." if stack["retirement_age_set"] else
             f"은퇴 나이를 정하지 않으셔서 {'·'.join(str(t['retire_age']) for t in tiers)}세 세 경우로 계산했고, 아래 연금 표는 "
             f"{head}세 기준입니다.")
            + f" 은퇴 후 월 <b>{_short(target)}</b>(현재 가치) 목표에 모든 연금이 나온 뒤의 세후 월 합계는 "
            + ", ".join(f"{t['retire_age']}세 은퇴 <b>{_short(t['total_net'])}</b>({_pct(t['ratio'], 0)})" for t in tiers) + "입니다.")
    if stack["unknown"]:
        missing = [p for p in stack["pipelines"] if p["net_monthly"] is None]
        lead += (f" {', '.join(p['label'] for p in missing)}은 아직 수령액을 몰라 뺀 하한이며, 답장으로 알려 주시면 바로 반영합니다: "
                 + " ".join(f"{p['label']}는 {p['missing']}." for p in missing if p["missing"]))
    rows = []
    for p in stack["pipelines"]:
        # A stated payout starts on a date; with only the birth year its age is approximate (review 2026-10-18).
        start = ("-" if p["start_age"] is None else
                 f"{p['start_date']:%Y.%m}(약 {p['start_age']}세)" if p.get("start_date") else f"{p['start_age']}세")
        gross = _short(p["gross_monthly"])
        if p.get("nominal_monthly") is not None:  # The insurer's own figure is a future amount: show both.
            gross = f"{_short(p['gross_monthly'])}(현재 가치) · {p.get('start_year', '')}년 금액 {_short(p['nominal_monthly'])}"
        basis = (("입력값" if p["basis_text"].startswith("알려 주신") else "추정") if p["net_monthly"] is not None
                 else f"입력 필요: {p['missing']}")
        rows.append([p["label"], start, gross, _short(p["net_monthly"]), basis])
    inner = (_tier_table(tiers) + _gauge(f"{head}세 은퇴 기준 목표 월 생활비 대비 준비율", stack["ratio"], "")
             + _table(["연금", "개시", "월 예상(세전)", "세후", "근거"], rows) + _stack_note(stack))
    return _section("노후 현금흐름 준비율: 4대 연금 스택", lead, inner,
                    note=escape("준비율 " + " · ".join(f"{t['retire_age']}세 은퇴 {_pct(t['ratio'], 0)}" for t in tiers)))


def _retirement_tiers(stack: dict[str, Any]) -> list[dict[str, Any]]:
    """The retirement ages still ahead of the client (report design §30)."""
    return [t for t in stack["tiers"] if stack["age"] is None or t["retire_age"] > stack["age"]]


def _tier_table(tiers: list[dict[str, Any]]) -> str:
    """One row per retirement age: the years before the first pension, the after-tax total once every pension
    has started, the ratio, and what must be held outside the pensions at retirement (today's won)."""
    rows = []
    for t in tiers:
        gap = t["years_without_pension"]
        need = t["outside_need"]
        bound = need is not None and need["upper_bound"]
        rows.append([f"{t['retire_age']}세", "-" if gap is None else f"{gap}년({t['first_income_age']}세까지)" if gap else "없음",
                     _short(t["total_net"]) + (" 이상" if t["unknown"] else ""), _pct(t["ratio"], 0) + (" 이상" if t["unknown"] else ""),
                     "미상" if need is None else ("최대 " if bound else "") + _short(need["total"])])
    note = ("연금 없는 기간은 은퇴부터 첫 연금(55세부터 받는 연금 계좌, 65세 국민연금)까지입니다. 연금 밖에서 준비할 돈은 그 기간의 생활비와 "
            "모든 연금이 나온 뒤에도 남는 부족분을 은퇴 시점의 오늘 돈 가치로 합한 것이며, 퇴직금·부동산은 넣지 않았습니다.")
    if any(t["unknown"] for t in tiers):
        note += " 수령액을 모르는 연금은 0원으로 볼 수 없어, 합계와 준비율은 그 이상, 연금 밖에서 준비할 돈은 그 이하일 수 있는 최대치입니다."
    if any(p["key"] == "isa" for t in tiers for p in t["pipelines"]):
        note += " ISA 등 줄은 공제 밖 납입을 주택 이전에 쓰지 않고 노후까지 남긴다는 가정입니다."
    return (_table(["은퇴 나이", "연금 없는 기간", "모든 연금 개시 후 세후 월", "준비율", "연금 밖에서 준비할 돈"], rows)
            + f'<div style="font-size:12px;line-height:1.6;color:{MUTED};margin:4px 0 12px">{escape(note)}</div>')


def _stack_note(stack: dict[str, Any]) -> str:
    """How the contributions were projected (report design §30): the credit split the tax proposal makes, and the
    total with the current split beside it, so the two never read as a contradiction."""
    c = stack["contributions"]
    if not c["changed"] or stack["current_split"] is None:
        return ""
    now = c["current"]
    text = (f"납입은 세액공제를 빠짐없이 받는 연금저축 월 {_short(c['pension_savings'])}, IRP 월 {_short(c['irp'])}으로 나누고"
            + (f" 공제 밖 월 {_short(c['isa'])}은 ISA 등에 모은다고" if c["isa"] else "") + " 계산했습니다. "
            f"지금처럼 연금저축 월 {_short(now['pension_savings'])}, IRP 월 {_short(now['irp'])}을 계속 넣으면 "
            f"{stack['retirement_age']}세 은퇴 기준 세후 합계는 약 {_short(stack['current_split']['total_net'])}입니다. "
            "공제받지 않은 납입 원금은 꺼낼 때 세금이 없어 그만큼 세금을 빼지 않았습니다(현행 법령 확인 필요).")
    return f'<div style="font-size:12px;line-height:1.6;color:{MUTED};margin:8px 0 4px">{escape(text)}</div>'


def _experiences(story: dict[str, Any]) -> str:
    """The experience dividend (report design §18.6): one or two curated things to see, read or do, with their
    sources. Rendered open on the household day."""
    items = story.get("experiences") or []
    if not items:
        return ""
    cards = "".join(f'<div style="border:1px solid {HAIRLINE};border-radius:16px;padding:16px 18px;margin-top:10px">'
                    f'<div style="font-size:16px;font-weight:600;color:{INK}">{escape(e["title"])}</div>'
                    f'<div style="font-size:14px;line-height:1.6;color:{BODY};margin-top:6px">{escape(e["body"])}</div>'
                    f'<div style="font-size:12px;color:{MUTED};margin-top:6px">{escape(e["when"])}</div>'
                    f'{_sources(e.get("links") or [])}</div>' for e in items)
    lead = ("숫자와 저축 너머, 고객님의 삶과 지평을 넓혀 줄 경험(전시, 인문 독서, 여행, 미식, 테니스 같은 웰니스)을 이번 주에 "
            "한두 가지 골랐습니다. 선택은 고객님의 몫입니다.")
    return _opened(_section("금주의 라이프스타일 & 경험 소비 큐레이션", lead, cards, note=escape(f"{len(items)}건")))


def _annuity_review(data: dict[str, Any]) -> str:
    """The insurer's annuities reviewed on their own illustrations (report design §19), one card each."""
    reviews = data.get("annuity_reviews") or []
    return "".join(_annuity_card(r, i, len(reviews)) for i, r in enumerate(reviews, 1))


def _annuity_card(r: dict[str, Any], number: int, count: int) -> str:
    """What is still paid and what comes back, the forward return by lifespan, the index-fund alternative, the
    break-even and the verdict with the condition that flips it."""
    from smith.product_review import SEARCH_HIGH, SEARCH_LOW, verdict_text
    sentence, flip = verdict_text(r)
    pill = _pill({"strong_keep": "유지 유리", "keep_as_floor": "유지(채권 자리)", "surrender_candidate": "해지 검토"}[r["verdict"]],
                 {"strong_keep": "good", "keep_as_floor": "neutral", "surrender_candidate": "warn"}[r["verdict"]])
    be = (f"연 {r['break_even_return'] * 100:.1f}%" if r["break_even_return"] is not None else
          f"연 {SEARCH_HIGH * 100:.0f}% 이상" if r["break_even_bound"] == "above" else f"연 {SEARCH_LOW * 100:.0f}% 미만")
    # Verdict first, then the two numbers behind it, the flip condition and the sunk-cost trap (user review
    # 2026-10-18: a quarter of the text; the tables below carry the rest).
    lead = (f"{sentence} 앞으로 낼 약 <b>{_short(r['remaining_premiums'])}</b>({r['remaining_months']}회)으로 "
            f"{r['annuity_start']:%Y년 %m월}부터 평생 월 <b>{_short(r['annuity_monthly_nominal'])}</b>(지금 가치 "
            f"{_short(r['annuity_real_now'])})을 받으며, 지수펀드로 같은 연금을 만들려면 <b>{be}</b>의 수익률이 필요합니다. "
            f"{flip} 이미 낸 돈(매몰비용)은 판단에서 빼십시오.")
    by_life = _table(["몇 세까지 받나", "연평균 수익률(세후)", "총수령액"],
                     [[f"{b['to_age']}세", _pct(b["irr"], 1) if b["irr"] is not None else "미상", _short(b["received"])]
                      for b in r["by_lifespan"]])
    alt = _table(["지수펀드 연 수익률 가정", f"{r['annuity_start']:%Y년} 월 수령(연 4% 인출)", "지금 가치"],
                 [[_pct(a["rate"], 0), _short(a["monthly"]) + (" (연금보다 적음)" if a["monthly"] < r["annuity_monthly_nominal"] else ""),
                   _short(a["monthly_real"])] for a in r["alternatives"]])
    gauge = _gauge("납입 진행", Decimal(r["paid_months"]) / Decimal(r["total_months"]), "")
    inner = (gauge
             + f'<div style="font-size:12px;font-weight:600;color:{MUTED};margin-top:10px">수명별 연평균 수익률</div>' + by_life
             + f'<div style="font-size:12px;font-weight:600;color:{MUTED};margin-top:12px">같은 돈을 지수펀드에 넣으면(세금 전)</div>' + alt)
    name = r.get("policy_name") or "연금보험"
    title = "보유 연금보험 평가" + (f" ({number}/{count})" if count > 1 else "")
    return _section(title, f"<b>{escape(name)}</b> — " + lead, inner, note=pill, brief=None)




def _pension(pi: dict[str, Any], review: dict[str, Any] | None = None) -> str:
    lead = (f"인출이 제한된 자산(연금·청약 등)은 <b>{_short(pi['restricted_total'])}</b>입니다. "
            f"월 보험료는 <b>{_short(pi['monthly_premiums'])}</b>로 월 소득의 {_pct(pi['premium_to_income'])}입니다.")
    if review:
        if review["over_limit"]:
            lead += (f" 보험료 기준인 소득의 {_pct(review['limit'], 0)}를 넘어, 저축성 보험의 감액완납·중도인출·추가 납입 중단이 "
                     "현금흐름 개선의 첫 후보입니다.")
        for s in review["savings_type"]:
            if s["mismatch"]:
                lead += (f" 월 {_short(s['monthly'])}을 내는 보험이 저축성이라면 비과세 만기 {s['maturity_year']}년이 주택 이전 "
                         f"{review['home_year']}년보다 늦어, 그 돈은 가장 필요한 때 쓸 수 없습니다.")
    if not pi["surrender_values_known"]:
        lead += " 보험 해약환급금이 아직 입력되지 않아 보험의 자산 가치는 반영되어 있지 않습니다."
    return _section("연금·보험", lead, "")


def _macro(macro: dict[str, Any], *, compact: bool = False) -> str:
    """Rates and FX with the household's exposures and the latest official announcements. `compact` (the
    markets day) keeps the chart and the series table; the exposures and announcements stay on the rates day."""
    rows = [r for r in macro["evidence"] if r["value"] is not None]
    rates = [(SERIES_LABELS.get(r["spec"].series_id, r["spec"].label), r["change_12m"]) for r in rows
             if r["spec"].unit == "%" and r["change_12m"] is not None]
    rising = sum(1 for _, change in rates if change > 0)
    # The policy rates' path first (§28: a bank's note leads with the decision and how it was reached), then the
    # count of market rates that rose; the section's summary keeps these two sentences.
    path = _policy_path(macro.get("policy"))
    lead = ((path + " ") if path else "") + (
            f"지난 1년간 주요 금리 {len(rates)}개 중 <b>{rising}개가 올랐습니다</b>(단위 <b>%p</b>). "
            "금리가 오르면 변동금리 대출 이자는 늘고, 예금 이자와 채권 수익률은 높아지며, 주식·부동산 가격에는 "
            "대체로 부담이 됩니다.")
    lead += (" 우리 집 대출·예금에 미치는 영향은 금리·대출 보고에서 다룹니다." if compact else
             " 우리 집 자산 가운데 이 변화에 노출된 금액은 아래 표와 같습니다.")
    chart = _diverging([(label, change) for label, change in rates], value_text=_pp) if rates else ""
    # Three-month changes stay in the data for the narrative; the phone-width table keeps the 1-year view.
    table = _table(["지표", "값", "1년 변화", "출처·관측일"],
                   [[SERIES_LABELS.get(r["spec"].series_id, r["spec"].label),
                     f"{r['value']}{'%' if r['spec'].unit == '%' else '원'}", _delta(r["change_12m"]),
                     (f"{r['spec'].provider.upper()} {r['observed_on']:%m.%d}", r["spec"].link)] for r in rows])
    if compact:
        return _section("시장 금리와 환율", lead, chart + table + _fx_block(macro.get("fx")))
    table += _table(["금리·환율 변화에 노출된 자산·부채", "금액"],
                    [[_exposure_label(e.key), _won(e.amount)] for e in macro["exposures"].exposures])
    table += _fx_block(macro.get("fx"))
    news = macro["announcements"][:5]
    if news:
        table += _table(["최근 공식 발표(제목을 누르면 원문)", "날짜"],
                        [[(a.title, a.link), f"{a.published_at:%Y.%m.%d}"] for a in news])
    return _section("거시 환경", lead, chart + table)


def _policy_path(policy: dict[str, Any] | None) -> str:
    """One sentence on where the two policy rates stand and how they got there over the past year, from the stored
    series (§28): each step's date and size, "뒤 동결" when the last step is behind the latest observation."""
    parts = [_rate_path(label, (policy or {}).get(key)) for label, key in (("한국은행 기준금리", "bok"), ("미국 기준금리(상단)", "fed"))]
    parts = [p for p in parts if p]
    return "" if not parts else "이고, ".join(parts) + "입니다."


def _rate_path(label: str, row: dict[str, Any] | None) -> str:
    if row is None or row["value"] is None:
        return ""
    moves = row.get("moves") or []
    when = f"({row['observed_on']:%m.%d} 기준)"
    if not moves:
        return f"{label} {row['value']}%는 지난 1년 변동이 없었음{when}"
    # Dates carry the year once the year's steps straddle a year end ("10.30, 12.11, 09.17" reads out of order).
    form = "%m.%d" if all(day.year == row["observed_on"].year for day, _ in moves) else "%y.%m.%d"
    steps = ", ".join(f"{day:{form}} {delta:+.2f}%p" for day, delta in moves[-4:])
    held = " 뒤 동결" if moves[-1][0] < row["observed_on"] else ""
    return f"{label} {row['value']}%는 {steps}{held}{when}"


def _fx_block(fx: dict[str, Any] | None) -> str:
    """The won/dollar rate against the dollar assets (expert review 2026-10-22: the exposure table named the
    dollar assets without saying what a move does to them): what 100 won of move does to today's dollars, and
    what the past year's move did to them. Nothing without dollar assets; no second sentence without the series."""
    if fx is None:
        return ""
    body = (f"달러 자산 {_short(fx['usd_assets'])}(약 {fx['usd_amount']:,.0f}달러)은 원/달러 환율이 100원 움직일 때 "
            f"약 {_short(fx['per_100_won'])} 오르내립니다.")
    change = fx["change_12m"]
    if change:
        then = fx["series_rate"] - change
        body += (f" 지난 1년 환율이 {abs(change):,.1f}원 {'내려' if change < 0 else '올라'}({then:,.0f}원→{fx['series_rate']:,.0f}원, "
                 f"한국은행 기준 환율 {fx['series_observed_on']:%m.%d}) 지금 보유한 달러의 원화 가치는 약 "
                 f"{_short(abs(fx['year_effect']))} {'줄었습니다' if change < 0 else '늘었습니다'}.")
    return (f'<div style="background:{SOFT};border-radius:16px;padding:14px 16px;margin:12px 0 4px">'
            f'<div style="font-size:12px;font-weight:600;color:{MUTED};margin-bottom:6px">환율 민감도</div>'
            f'<div style="font-size:14px;line-height:1.6;color:{BODY}">{escape(body)}</div></div>')


def _exposure_label(key: str) -> str:
    return EXPOSURE_LABELS.get(key.split(":")[0], key.split(":")[0])


def _pp(value: Decimal) -> str:
    arrow = "▲" if value > 0 else "▼" if value < 0 else "–"
    return f"{arrow} {value:+.2f}%p"


REQUEST_LABELS = {"must": "꼭 필요", "should": "있으면 더 정확", "nice": "참고"}


def _requests(requests: list[Any], full: bool, rest_where: str = "월요 보고") -> str:
    """What the client could provide next (checklist.requests). Thursday and the overview list only the essential
    ones; `rest_where` names the report that lists the rest."""
    shown = requests if full else [r for r in requests if r.priority == "must"]
    if not shown:
        return ""
    rows = []
    for r in shown:
        strong = r.priority == "must"
        badge = (f'<span style="display:inline-block;background:{BLUE if strong else STRONG};'
                 f'color:{"#ffffff" if strong else INK};font-size:11px;font-weight:600;border-radius:100px;'
                 f'padding:2px 10px;margin-right:6px">{REQUEST_LABELS[r.priority]}</span>')
        # Where the figure goes (the JSON field or config key) is developer detail: it is listed in the
        # footer's system notes, not beside the request the client reads (report design §17.6).
        rows.append(f'<div style="margin:6px 0 10px;font-size:14px;font-weight:600;color:{INK};line-height:1.5">'
                    f'{badge}{escape(r.title)}</div>')
    rest = len(requests) - len(shown)
    lead = "답장으로 알려 주시면 판단이 더 정확해지는 자료입니다." + (f" 나머지 {rest}건은 {rest_where}에서 안내합니다." if rest else "")
    return _section("다음에 확인할 것", lead, "".join(rows))


def _footer(warnings: list[str] | tuple[str, ...] = (), week: str = "") -> str:
    """One line (report design §18.1): what Smith never does, and that the reasons are a reply away. A problem
    with the report itself (stale data, no narrative) adds one warning line above it; a daily edition adds the
    week's plan (`week`, HTML) so a reader of one day knows where the other areas are (§27)."""
    warn = ("" if not warnings else
            f'<div style="font-size:13px;line-height:1.6;color:{INK};margin-bottom:8px">▲ 확인 필요: '
            f'{escape(" · ".join(warnings))}. 자세한 사유는 이 메일에 답장으로 물어 주십시오.</div>')
    guide = "" if not week else f'<div style="font-size:12px;line-height:1.6;color:{MUTED};margin-bottom:6px">{week}</div>'
    return (f'<tr><td>{warn}{guide}<div style="font-size:12px;line-height:1.6;color:{MUTED}">Smith는 조회 전용 자문입니다(매매·이체·대출 '
            '실행 없음). 세금·법령은 실행 전 확인하시고, 판단 근거는 답장으로 물어 주십시오.</div></td></tr>')


def _week_line(data: dict[str, Any]) -> str:
    """The week's reports with today's in bold, for the daily editions only (report design §27): one report a
    day covers one area, and this line says so where the top of the mail no longer does (§21)."""
    if editions.of(data["kind"]) is None:
        return ""
    week = editions.week_plan(data.get("schedule"))
    if not week:
        return ""
    items = " · ".join(f"<b>{day} {escape(label)}</b>" if kind == data["kind"] else f"{day} {escape(label)}"
                       for day, kind, label in week)
    return f"Smith는 하루에 한 주제만 보고합니다: {items}. 오늘 다루지 않은 분야는 그 요일의 보고에 있습니다."


def _data_lines(data: dict[str, Any], assumptions: list[str], ai: dict[str, Any] | None = None) -> list[str]:
    """Where the numbers came from and their limits: data freshness, sync state, warnings, assumptions and the
    AI stages. Not printed in the mail any more; part of `system_notes`."""
    lines = [f"{_source(f['source'])}: {f['records']}건, 기준일 {f['oldest'].date()}~{f['newest'].date()}"
             for f in data["freshness"]]
    lines += [f"동기화 · {_source(s['source'])}: 최근 성공 {s['last_success'].date() if s['last_success'] else '없음'}"
              for s in data["sync"]]
    lines += [f"경고: {w}" for w in data["warnings"]]
    toss = data["sectors"]["cash"].get("toss_cash") or {}
    if toss.get("synced"):
        lines.append("토스증권 예수금: API가 제공하지 않아 미수집 — " + ("증권 계좌 현금은 직접 입력하신 값 사용"
                                                          if toss["manual_records"] else "현금 잔액이 반영되지 않음"))
    if not data["completeness"]["complete"]:
        lines.append(f"불완전 영역: {', '.join(data['completeness']['areas'])}")
    lines += [f"가정: {a}" for a in assumptions]
    edition = editions.of(data["kind"])
    lines += _ai_lines(ai, researched=edition is None or bool(edition.research or edition.holdings_research))
    return lines


def _ai_lines(ai: dict[str, Any] | None, *, researched: bool = True) -> list[str]:
    """How the AI stages went: research brief, narrative, and what code verification removed.
    `researched` is False for a day that researches nothing by design (the household edition)."""
    if not ai:
        return ["AI 해석: 이번 보고서는 계산 결과만으로 작성했습니다."]
    brief = ai.get("brief")
    if brief:
        line = (f"정책·시장 조사: {brief['created_at']:%m월 %d일} 웹 조사 {brief['items']}건(공식 출처 {brief.get('official', 0)}건). "
                "금액·계좌·직접 식별자 없이, 승인된 시·구와 자산·부채 유형으로 조사했습니다.")
        if brief.get("checked"):
            line += (f" 출처 페이지를 직접 열어 {brief['checked']}건을 확인했고, 그중 {brief['matched']}건은 본문에서 "
                     "같은 수치를 찾았습니다(없어진 페이지의 항목은 뺐습니다).")
        if brief.get("reused"):
            line += " 이번 보고에서 새로 조사하지 못해, 같은 주제로 앞서 조사한 자료를 다시 사용했습니다."
        lines = [line]
        if brief.get("holdings_topics"):
            lines.append(f"종목 조사: 보유 종목 {brief['holdings_topics']}개를 이름·심볼·시장만으로 조사해 "
                         f"{brief.get('holdings_covered', 0)}개 종목의 실적·전망 자료를 찾았습니다(수량·금액은 보내지 않음). "
                         "자료가 없는 종목의 의견은 우리 집 숫자만으로 판단한 것입니다.")
    elif not researched:
        lines = ["정책·시장 조사: 이 요일 보고는 우리 집 숫자와 고객님 말씀만으로 쓰며, 웹 조사를 하지 않습니다."]
    else:
        lines = ["정책·시장 조사: 이번 보고에 쓸 수 있는 조사 결과가 없어 외부 정책·시장 변화는 반영하지 않았습니다."]
    if ai["outcome"] == "success":
        flagged = len(ai.get("dropped") or [])
        lines.append("AI 해석: 계산값과 출처로 자동 점검했습니다"
                     + (f". 확인되지 않은 부분이 있는 {flagged}개 항목의 메모는 위 '자동 점검 메모' 줄에 있습니다." if flagged else "."))
    else:
        lines.append(f"AI 해석: 이번에는 실패해({ai.get('error_code', '원인 미상')}) 계산 결과만으로 작성했습니다.")
    return lines


def _source(source: str) -> str:
    return "수동 입력" if source.startswith("manual") else SOURCE_LABELS.get(source, source)


def _dec(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _delta(value: Decimal | None) -> str:
    return "-" if value is None else f"{value:+}"
