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
import base64
import re
from decimal import ROUND_HALF_UP, Decimal
from html import escape
from typing import Any

from smith.fmt import short_won as _short
from smith import editions, korean, pension, pension_chart, picks, principles, proposals, stress, tax
from smith.holdings import STANCES
from smith.proposals import Track
from smith.realestate import MIN_BASIS
from smith.report_data import (CATEGORY_LABELS, CERTAINTY_LABELS, COMPONENT_LABELS, GROUP_LABELS, PAY_LABELS,
                               QUANTITY_CHANGES)

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
    bands.append(_band(SOFT, _footer(_system_warnings(data), corrections=_corrections_line(data))))
    return _subject(data, status), _page(bands)


def render_event(note: dict[str, Any]) -> tuple[str, str, str]:
    """Return (subject, html, text) of an event note (report design §29): the hero names the event and what it
    means for the household in one line, four tiles carry the figures, then what happened and our numbers, the
    researched why with its sources, and Smith's stance. Never a proposal: those belong to the regular reports. A
    disclosure note has its own layout (`_disclosure_note`)."""
    if note["kind"] == "disclosure":
        return _disclosure_note(note)
    subject, title, line, tiles, facts, stance = (_bok_event(note) if note["kind"] == "bok_decision"
                                                  else _move_event(note))
    why = _event_research(note)
    # Smith's stance leads (user review 2026-11-03): the reader wants the verdict first, then the figures, then the why.
    body = (_opened(_section("Smith의 입장", stance, "", brief=None))
            + _opened(_section("무엇이 일어났고 우리 집에는", facts[0], facts[1], brief=None))
            + (_opened(_section("왜 그랬나", why[0], why[1], brief=None)) if why else ""))
    guide = "정기 보고와 별개로, 우리 집에 중요한 사건이 있는 날에만 보내는 안내입니다. 제안은 정기 보고에서만 드립니다."
    html = _page([_band(DARK, _event_hero(note, title, line, tiles)), _band(CANVAS, body, border=True),
                  _band(SOFT, _footer((), week=guide, shared=bool(note.get("family_copy"))))])
    text = (f"{subject}\n\n{title}\n{line}\n\n{_plain(facts[0])}\n\n{_plain(stance)}\n\n{PLAIN_TAIL}")
    return subject, html, text


PLAIN_TAIL = "이 안내는 HTML 형식입니다. HTML을 지원하는 메일 앱에서 확인하시기 바랍니다."


def _event_hero(note: dict[str, Any], title: str, line: str, tiles: list[tuple[Any, ...]]) -> str:
    return (f'<tr><td>{_eyebrow("SMITH · 사건 안내", color=ON_DARK, background=DARK_RAISED)}'
            f'<div style="font-family:{SANS};font-weight:400;font-size:28px;line-height:1.15;letter-spacing:-0.8px;'
            f'color:{ON_DARK};margin:4px 0 12px">{escape(title)}</div>'
            f'<div style="font-size:14px;line-height:1.5;color:{ON_DARK}">{note["as_of"]:%Y년 %m월 %d일 %H:%M} 기준 · '
            f'{escape(line)}</div></td></tr>' + _tile_grid(tiles))


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
    subject = f"[Smith · 사건 안내] {note['day']:%m/%d} 보유 종목 {pct} ({_signed_short(change)})"
    line = f"{since:%m.%d %H:%M} 이후 {_signed_short(change)} · 시장 손익 {_signed_short(note['market'])} · 환율 {_signed_short(note['fx'])}"
    tiles = [("보유 종목 변동", _signed_short(change), f"{since:%m.%d %H:%M} 이후 · 평가액 {_short(note['start'])} 기준"),
             ("변동률", pct, "시장 손익과 환율 효과", ratio),
             ("가장 크게 움직인 종목", str(top["name"])[:12] if top else "-",
              f"{_signed_short(top['change'])} ({top['ratio'] * 100:+.1f}%)" if top else ""),
             ("스트레스 가정 대비", f"{note['stress_share'] * 100:.0f}%", "주식 30% 하락 가정의 비율")]
    # Only the rates of change are coloured, gains green and losses red (user review 2026-11-03).
    by_market_toned = " · ".join(f"{label} {_signed_short(by_market[key])}"
                                 for key, label in (("KR", "한국 상장"), ("US", "미국 상장")) if key in by_market)
    lead = (f"지난 점검({since:%m월 %d일 %H:%M}) 이후 보유 종목 평가액이 {_signed_short(change)}"
            f"({_good_bad(ratio, pct)}) 움직였습니다. 시장 손익이 {_signed_short(note['market'])}, "
            f"환율 효과가 {_signed_short(note['fx'])}입니다."
            + (f" {by_market_toned}." if by_market_toned else ""))
    rows = [[str(m["name"]), _signed_short(m["change"]),
             Raw(_good_bad(m["ratio"], f"{m['ratio'] * 100:+.1f}%"))] for m in movers]
    stance = (f"하루 변동으로 매매하지 않습니다. 이번 변동은 Smith의 스트레스 테스트가 가정하는 주식 30% 하락의 "
              f"{note['stress_share'] * 100:.0f}%입니다. 정기 보고의 제안(분기 정리 일정, 날짜 있는 목표 자금의 이동)은 그대로이고, "
              "종목별 판단은 다음 화요 투자·시장 보고에서 다시 봅니다.")
    return (subject, f"보유 종목 {pct} ({since:%m.%d} 이후)", line, tiles,
            (lead, _table(["종목", "변동", "변동률"], rows) if rows else ""), stance)


_FILING_ORDER = ("dividend", "dilution", "risk", "return", "earnings", "corporate")
_FILING_PILL = {"dividend": ("배당", "good"), "return": ("주주환원", "good"), "dilution": ("희석 주의", "warn"),
                "earnings": ("실적", "neutral"), "risk": ("위험 신호", "bad"), "corporate": ("회사 변화", "neutral")}


# The reading's verdict on the reason to hold (disclosure_insight.VERDICTS): the pill, and the short tile word.
VERDICT_PILL = {"intact": ("보유 이유 그대로", "good"), "watch": ("지켜볼 신호", "warn"), "review": ("다시 볼 이유", "bad")}
VERDICT_TILE = {"intact": "그대로", "watch": "지켜볼 신호", "review": "다시 봄"}
_VERDICT_ORDER = ("review", "watch", "intact", None)
REVIEW_NEXT = "다음 화요 투자·시장 보고에서 이 종목 판단을 다시 드립니다"
_REACTION = re.compile(r"([+\-−])\s*(\d+(?:\.\d+)?)\s*%")


def _disclosure_note(note: dict[str, Any]) -> tuple[str, str, str]:
    """Material filings of the holdings (FR-44, report design §29) read at a glance (user request 2026-11-07: "딱 봤을
    때 아 핵심만 잘 추렸구나, 내가 뭘 하면 되는구나"). The subject and the hero name the main company's takeaway and what
    the client has to do; four tiles carry what to do, the reason to hold, what we hold and the money or the price
    reaction; then Smith's judgement with the one thing to do per company, then one card per company (a results
    release and its statements are one story). What to do is code's (a dividend arrives by itself, a rights offer asks
    for a decision); the verdict and the prose are the reading's, escaped like any untrusted text."""
    companies = _companies(note)
    lead = companies[0]
    more = f" · 외 {len(companies) - 1}곳" if len(companies) > 1 else ""
    todo = [c for c in companies if c["action"][1]]
    todo_text = "할 일 없음" if not todo else f"할 일 {len(todo)}건"
    money = _dividend_tile(companies)
    # The note's dividend total, said once: not again when the lead's headline is already code's dividend line.
    paid = ("" if money is None or money[1] == "-" or lead["headline"].startswith("배당 세후") else
            f" · 배당 세후 약 {money[1]}" + (f"(미계산 {money[3]}건 제외)" if money[3] else ""))
    subject = (f"[Smith · 사건 안내] {note['day']:%m/%d} {lead['name']} {lead['headline'].replace('*', '')}"
               f"{paid}{more} · {todo_text}")
    title = f"{lead['name']} {lead['headline']}"
    count = sum(len(c["filings"]) for c in companies)
    line = f"{' · '.join(c['name'] for c in companies)[:40]} · 공시 {count}건 · {todo_text}"
    tiles = [_todo_tile(companies, todo), _verdict_tile(companies), _held_tile(companies),
             money[:3] if money else _risk_or_reaction_tile(companies)]
    failed = note.get("research_failed") or not any(c["reading"] for c in companies)
    head = ("이번에는 공시 해석을 만들지 못해 사실만 전합니다. 할 일과 우리 집 숫자는 Smith가 공시 원문과 보유 수량으로 "
            "정했습니다." if failed else "")
    body = (_opened(_section("판단과 할 일", escape(head), "".join(_judgement_row(c) for c in companies), brief=None))
            + _opened(_section("무슨 일이 있었나", "", "".join(_company_card(c) for c in companies), brief=None)))
    guide = ("보유 종목에 중요한 공시가 나온 날에만 보내는 안내입니다. 우리 집 숫자(배당·보유액)와 할 일은 Smith가 공시 원문과 "
             "보유 수량으로 정했고" + ("" if failed else ", 해석은 원문과 보도를 읽고 정리했습니다") + (". " if failed else ". ")
             + "매매 제안은 정기 보고에서만 드립니다.")
    html = _page([_band(DARK, _event_hero(note, title, line, tiles)), _band(CANVAS, body, border=True),
                  _band(SOFT, _footer((), week=escape(guide), law=money is not None,
                                      shared=bool(note.get("family_copy"))))])
    rows = "\n".join(f"- {c['name']}: " + (f"{(c['reading'] or {}).get('thesis')} " if (c["reading"] or {}).get("thesis")
                                            else "") + f"할 일: {c['action'][0]}" for c in companies)
    text = f"{subject}\n\n{title}\n{line}\n\n{head + chr(10) if head else ''}{rows}\n\n{PLAIN_TAIL}"
    return subject, html, text


def _companies(note: dict[str, Any]) -> list[dict[str, Any]]:
    """The note's filings by company, the one that matters most first: something to do, then a reason to hold in
    question, then the kind (money and the stake before titles to read), then the larger holding. A filing a
    correction replaced is left out: the correction carries its figures (disclosures._supersede)."""
    from smith.disclosure_insight import company_key   # Same key the reading was written under.
    newest = sorted(note["filings"], key=lambda f: (f["filed_on"], f["rcept_no"]), reverse=True)
    groups: dict[str, list[dict[str, Any]]] = {}
    for f in [f for f in newest if not f.get("superseded")] or newest:
        groups.setdefault(company_key(f), []).append(f)
    found = []
    for filings in groups.values():
        filings.sort(key=lambda f: _FILING_ORDER.index(f["category"]))     # Stable: newest first within a kind.
        reading = next((f["insight"] for f in filings if f.get("insight")), None)
        dividend = _company_dividend(filings, note["day"])
        first = filings[0]
        found.append({"name": first.get("display_name") or first["corp_name"], "filings": filings, "reading": reading,
                      "dividend": dividend, "headline": _headline(filings, reading, dividend),
                      "action": _filing_todo(filings, reading, dividend), "share": first.get("held_share"),
                      "value": first.get("held_value")})
    return sorted(found, key=lambda c: (not c["action"][1], _VERDICT_ORDER.index((c["reading"] or {}).get("verdict")),
                                        _FILING_ORDER.index(c["filings"][0]["category"]),
                                        -Decimal(c["value"] or 0)))


def _company_dividend(filings: list[dict[str, Any]], day: Any) -> dict[str, Any] | None:
    """The company's cash dividends still to come (a corrected dividend of an earlier quarter may already be paid),
    after tax: {"net" (None when none could be computed), "dates", "undated", "uncounted"}; None without one. A cash
    dividend code could not compute is counted apart, never summed as zero (review 2026-10-30)."""
    due = day.isoformat()
    totals = [f["household"]["total"] for f in filings if f["category"] == "dividend"
              and (f.get("household") or {}).get("total") and all(d >= due for d in f["household"]["total"]["pay_dates"])]
    uncounted = sum(1 for f in filings if f["category"] == "dividend" and (f.get("figures") or {}).get("cash")
                    and not (f.get("household") or {}).get("total"))
    if not totals and not uncounted:
        return None
    return {"net": sum((Decimal(t["net"]) for t in totals), Decimal(0)) if totals else None,
            "dates": sorted({d for t in totals for d in t["pay_dates"]}),
            "undated": sum(1 for t in totals if not t["pay_dates"]), "uncounted": uncounted}


def _md(day: str) -> str:
    return f"{day[5:7]}/{day[8:10]}"


def _one_pay_date(dividend: dict[str, Any]) -> str | None:
    """The pay date of the whole amount, or None when the amount is paid on several dates or partly on none yet
    (the whole sum is never tied to the first date, review 2026-11-07)."""
    return dividend["dates"][0] if len(dividend["dates"]) == 1 and not dividend["undated"] else None


def _headline(filings: list[dict[str, Any]], reading: dict[str, Any] | None, dividend: dict[str, Any] | None) -> str:
    """The company's takeaway: the reading's headline, else code's (the dividend we receive, the dilution, the
    filing's kind)."""
    if reading and reading.get("headline"):
        return reading["headline"]
    if dividend and dividend["net"]:
        day = _one_pay_date(dividend)
        return f"배당 세후 약 {_short(dividend['net'])}" + (f" {_md(day)} 입금" if day else "")
    for f in filings:
        g = f.get("figures") or {}
        if g.get("type") == "유상증자" and g.get("dilution"):
            return f"유상증자, 지분 약 {Decimal(g['dilution']) * 100:.1f}% 희석"
    first = filings[0]
    return first["title"].split(" · ", 1)[-1] if first.get("source") == "sec" else f"{_FILING_PILL[first['category']][0]} 공시"


def _filing_todo(filings: list[dict[str, Any]], reading: dict[str, Any] | None,
                 dividend: dict[str, Any] | None) -> tuple[str, bool]:
    """What the client has to do about the company's filings, decided by code, never by the reading: (text, whether
    something is to be done). A rights offer to shareholders asks for a decision; a dividend arrives by itself; a
    risk filing or a reason to hold in question goes to the next Tuesday report, where trade opinions belong."""
    for f in filings:
        g = f.get("figures") or {}
        if g.get("type") == "유상증자" and "주주배정" in (g.get("method") or ""):
            return "유상증자에 청약할지 정하셔야 합니다(청약 일정과 방법은 공시 원문)", True
        # A rights issue whose method code could not read may be offered to us: unknown is not "nothing to do"
        # (review 2026-11-07).
        if f.get("source") != "sec" and "유상증자" in f["title"] and not (g.get("type") == "유상증자" and g.get("method")):
            return "증자 방식을 읽지 못했습니다. 주주배정(청약 대상)인지 공시 원문에서 확인해 주십시오", True
    parts = ["없음"]
    if dividend and dividend["net"]:
        day = _one_pay_date(dividend)
        when = (f"{_md(day)}에" if day else "지급일마다" if dividend["dates"] else "지급일이 정해지면")
        parts.append(f"배당 세후 약 {_short(dividend['net'])}은 {when} 증권 계좌로 들어옵니다")
    if (reading or {}).get("verdict") == "review" or any(f["category"] == "risk" for f in filings):
        parts.append(REVIEW_NEXT)
    return " · ".join(parts), False


def _todo_tile(companies: list[dict[str, Any]], todo: list[dict[str, Any]]) -> tuple[str, str, str]:
    if todo:
        return ("할 일", f"{len(todo)}건", f"{todo[0]['name']} 유상증자 "
                + ("청약 결정" if todo[0]["action"][0].startswith("유상증자에 청약") else "방식 확인"))
    if any((c["reading"] or {}).get("verdict") == "review" or REVIEW_NEXT in c["action"][0] for c in companies):
        return ("할 일", "없음", "화요 보고에서 다시 판단")
    return ("할 일", "없음", "배당은 자동 입금" if any(c["dividend"] for c in companies) else "지켜보기만 하면 됩니다")


def _verdict_tile(companies: list[dict[str, Any]]) -> tuple[str, str, str]:
    read = [c for c in companies if (c["reading"] or {}).get("verdict")]
    if not read:
        return ("보유 이유", "-", "해석을 받지 못함")
    worst = min((c["reading"]["verdict"] for c in read), key=_VERDICT_ORDER.index)
    return ("보유 이유", VERDICT_TILE[worst], " · ".join(c["name"] for c in read if c["reading"]["verdict"] == worst)[:24])


def _held_tile(companies: list[dict[str, Any]]) -> tuple[str, str, str]:
    values, shares = [c["value"] for c in companies], [c["share"] for c in companies]
    if None in values:
        return ("우리 보유", "-", "보유액을 모름")
    weight = "" if None in shares else f"증권 자산의 {sum(Decimal(s) for s in shares) * 100:.1f}%"
    sub = weight if len(companies) == 1 else f"{len(companies)}개 회사" + (f" · {weight}" if weight else "")
    return ("우리 보유", f"약 {_short(sum(Decimal(v) for v in values))}", sub)


def _dividend_tile(companies: list[dict[str, Any]]) -> tuple[str, str, str, int] | None:
    """(title, value, sub, uncounted) of the dividends still to come, or None. The sub names the first pay date and
    says when others differ or are not set yet (a real note of 2026-10-29 put SK하이닉스's undated dividend under
    삼성전자's 11/27)."""
    found = [c["dividend"] for c in companies if c["dividend"]]
    if not found:
        return None
    nets = [d["net"] for d in found if d["net"] is not None]
    dates = sorted({x for d in found for x in d["dates"]})
    undated, uncounted = sum(d["undated"] for d in found), sum(d["uncounted"] for d in found)
    sub = ("세후" + (f" · {_md(dates[0])}" + (" 외" if len(dates) > 1 else "") if dates else "")
           + (f" · 지급일 미정 {undated}건" if undated else "") + (f" · 미계산 {uncounted}건 제외" if uncounted else ""))
    return ("배당 입금 예정", _short(sum(nets, Decimal(0))) if nets else "-", sub, uncounted)


def _risk_or_reaction_tile(companies: list[dict[str, Any]]) -> tuple[Any, ...]:
    flagged = sum(1 for c in companies for f in c["filings"] if f["category"] in ("dilution", "risk"))
    if flagged:
        return ("희석·위험 공시", f"{flagged}건", "유상증자·전환사채·소송 등")
    lead = companies[0]
    reaction = (lead["reading"] or {}).get("reaction") or ""
    if not reaction:
        return ("주가 반응", "-", "보도에서 찾지 못함" if lead["reading"] else "해석을 받지 못함")
    move = _REACTION.search(reaction)
    tone = None if move is None else Decimal(move.group(2)) * (-1 if move.group(1) in "-−" else 1)
    return ("주가 반응", reaction, lead["name"][:20], tone)     # Whole: a cut "-12.5%*" lost its figure and mark.


def _label(title: str, text: str) -> str:
    return (f'<div style="font-size:14px;line-height:1.6;color:{BODY};margin-top:4px">'
            f'<span style="font-weight:700;color:{INK}">{title}</span> {escape(text)}</div>')


def _judgement_row(c: dict[str, Any]) -> str:
    """One company: the verdict pill, the name and the one-sentence thesis; what to do; the next thing to check."""
    r = c["reading"] or {}
    pill = _pill(*VERDICT_PILL[r["verdict"]]) + " " if r.get("verdict") else ""
    thesis = f" {escape(r['thesis'])}" if r.get("thesis") else ""
    return (f'<div style="margin:0 0 16px"><div style="font-size:15px;line-height:1.6;color:{BODY}">{pill}'
            f'<span style="font-weight:700;color:{INK}">{escape(c["name"])}</span>{thesis}</div>'
            + _label("할 일", c["action"][0]) + (_label("다음 확인", r["watch"]) if r.get("watch") else "") + "</div>")


_NO_FACTS = ("미국 SEC에 낸 보고", "자세한 내용은", "이 공시는")


def _company_card(c: dict[str, Any]) -> str:
    """One company: what we hold, the reading (what happened, what it means for us), the code's figures per filing,
    the filings and the pages the reading used. A sentence marked "*" holds a figure no cited page confirmed."""
    r = c["reading"]
    held = ("" if c["value"] is None else f"우리 보유 약 {_short(Decimal(c['value']))}"
            + ("" if c["share"] is None else f" · 증권 자산의 {Decimal(c['share']) * 100:.1f}%"))
    reading = "" if not r else "".join(_label(title, r[key]) for title, key in (
        ("핵심", "what_happened"), ("주가 반응", "reaction"), ("우리에게", "meaning")) if r.get(key))
    facts = "".join(f'<div style="font-size:14px;line-height:1.6;color:{BODY};margin-top:6px">'
                    f'{_pill(*_FILING_PILL[f["category"]])} {escape(text)}</div>'
                    for f in c["filings"] for text in (_filing_text(f),) if not text.startswith(_NO_FACTS))
    if not reading and not facts:
        facts = (f'<div style="font-size:14px;line-height:1.6;color:{BODY};margin-top:6px">'
                 + ("미국 SEC에 낸 보고(영문)입니다. 해석은 다음 화요 투자·시장 보고에서 드립니다." if c["filings"][0].get("source") == "sec"
                    else "공시 원문에 숫자로 읽을 표가 없어 제목과 원문만 전합니다.") + "</div>")
    filed = "".join(f'<div style="font-size:13px;line-height:1.6;color:{MUTED}">원문 · <a href="{escape(f["url"])}" '
                    f'style="color:{MUTED};text-decoration:underline">{escape(f["title"])}</a> '
                    f'({_md(f["filed_on"])} · {"SEC" if f.get("source") == "sec" else "전자공시"})</div>' for f in c["filings"])
    links = " · ".join(f'<a href="{escape(url)}" style="color:{MUTED}">근거 {n}</a>'
                       for n, url in enumerate((r or {}).get("sources") or [], 1))
    notes = ("* 표시한 문장의 수치 일부는 출처 페이지에서 자동 확인하지 못했습니다. " if r and r.get("unchecked_figures") else "")
    tail = ("" if not notes and not links else
            f'<div style="font-size:12px;line-height:1.6;color:{MUTED};margin-top:4px">{escape(notes)}{links}</div>')
    return (f'<div style="margin:12px 0;padding:14px 16px;border:1px solid {HAIRLINE};border-radius:16px">'
            f'<div style="font-size:16px;font-weight:700;line-height:1.4;color:{INK}">{escape(c["name"])}</div>'
            + (f'<div style="font-family:{MONO};font-size:12px;color:{MUTED};margin:2px 0 4px">{escape(held)}</div>' if held else "")
            + f'{reading}{facts}<div style="margin-top:8px">{filed}</div>{tail}</div>')


def _filing_text(f: dict[str, Any]) -> str:
    """What the filing says in figures, and what it means for this household, all from code."""
    if f.get("superseded"):
        day = f["superseded"]
        return f"이 공시는 {day[5:7]}/{day[8:10]} 정정 공시로 바뀌었습니다. 정정 공시의 숫자를 보세요."
    if f.get("figures_failed"):
        return f"세부 숫자는 이번에 읽지 못했습니다({f['figures_failed']}). 원문 링크에서 확인해 주세요."
    if f.get("source") == "sec":
        return "미국 SEC에 낸 보고입니다. 제목은 보고 항목을 우리말로 옮긴 것이고, 원문(영문)은 링크에서 볼 수 있습니다."
    g = f.get("figures")
    if not g:
        return "자세한 내용은 공시 원문을 확인해 주세요."
    if f["category"] == "dividend":
        per = " · ".join(f"{'보통주' if k == 'common' else '우선주'} 1주당 {Decimal(v):,.0f}원" for k, v in g["per_share"].items())
        dates = "".join(f", {label} {day[5:7]}/{day[8:10]}" for label, day in (("기준일", g["record_date"]), ("지급", g["pay_date"])) if day)
        if not g["cash"]:
            return f"{g['kind']} {per}{dates}. 현금배당이 아니어서(현물·혼합) 받을 금액은 계산하지 않았습니다."
        found = f.get("household") or {}
        total = found.get("total")
        rows = found.get("items") or []
        held = " · ".join(f"{'보통주' if r['symbol'].endswith('0') else '우선주'} {Decimal(r['quantity']):,.0f}주"
                          for r in rows if r["owner"] == "self")
        family = any(r["owner"] != "self" for r in rows)
        basis = (" 보고서 기준 수량이라 기준일 보유량과 다르면 달라집니다." if any(r["quantity_basis"] == "report_date" for r in rows)
                 else "")
        ours = ("" if total is None else
                f" 우리 집({held}{', 가족 몫 포함' if family else ''}): 세전 {_won(Decimal(total['gross']))}, "
                f"세금 {_won(Decimal(total['withheld']))}을 떼고 약 {_short(Decimal(total['net']))} 입금 예정.{basis}")
        return f"{g['kind']} {per}{dates}.{ours or ' 우리 집 몫은 계산하지 못했습니다.'}"
    if g["type"] == "유상증자":
        ratio = f" 기존 주주 지분이 약 {Decimal(g['dilution']) * 100:.1f}% 줄어듭니다." if g.get("dilution") else ""
        return f"새 주식 {g['new_shares']}주(기존 {g['shares_before']}주), {g['method']}.{ratio}"
    if g["type"] == "전환사채":
        ratio = (f"(발행 주식의 {Decimal(g['ratio_to_shares']) * 100:.2f}%)" if g.get("ratio_to_shares") else "")
        return (f"사채 {_filed_won(g['amount'])}, 전환가 {g['conversion_price']}원. 모두 주식으로 바뀌면 "
                f"{g['shares_on_conversion']}주{ratio}가 늘어납니다. 전환 청구는 {g['conversion_from']}부터, {g['method']} 발행.")
    return (f"자기 주식 {g['shares']}주를 {_filed_won(g['amount'])}어치 사들입니다"
            f"({g['method']}, {g['from']}~{g['until']}).")


def _filed_won(text: str | None) -> str:
    """A filing's won amount the way a reader takes it in: 20조 77억 원 as 약 20조 원, 122억 원, 3,500만 원."""
    try:
        value = Decimal(str(text or "").replace(",", ""))
    except ArithmeticError:
        return f"{text}원"
    if value >= 10 ** 12:
        return f"약 {value / 10 ** 12:,.1f}조 원".replace(".0조", "조")
    return _short(value).replace(".0억", "억")


EVENT_WHY_LIMIT, EVENT_WHY_CHARS = 3, 110   # Findings and characters per finding in an event's "왜 그랬나".


def _event_research(note: dict[str, Any]) -> tuple[str, str] | None:
    """The researched background of an event: each finding with its source link, dated, marked 공식 or 참고."""
    items = [i for i in note.get("items") or [] if not i.get("decision_source")]
    if not items:
        return (("원인 조사는 이번에 실패했습니다. 숫자는 Smith의 계산이며, 배경은 다음 정기 보고에서 다룹니다.", "")
                if note.get("research_failed") else None)
    # Only what the reader needs (user review 2026-11-03): the few most relevant findings (official first), each a
    # linked headline and its first sentence; the full detail stays in the source.
    items = sorted(items, key=lambda i: i.get("tier") != "official")[:EVENT_WHY_LIMIT]
    rows = []
    for item in items:
        gist = _first_sentence(item["detail"])
        gist = gist if len(gist) <= EVENT_WHY_CHARS else gist[:EVENT_WHY_CHARS].rstrip() + "…"
        rows.append(f'<div style="margin:10px 0"><div style="font-size:14px;font-weight:600;line-height:1.5;color:{INK}">'
                    f'<a href="{escape(item["source_url"])}" style="color:{INK};text-decoration:underline">{escape(item["headline"])}</a></div>'
                    f'<div style="font-size:13px;line-height:1.6;color:{BODY}">{escape(gist)}</div></div>')
    return "", "".join(rows)


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
    # The expand hint of a collapsible card is hidden inline and appears only inside a real <details>, where it
    # follows the card's state. Gmail drops <details> (every card shows open), so there it never appears and
    # never promises a tap that does nothing; the stylesheet overrides the inline style with !important.
    style = ('<style>details>summary{list-style:none}details>summary::-webkit-details-marker{display:none}'
             'details>summary .mk-hint{display:inline!important}details>summary .mk-hint.mk-block{display:block!important}'
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
        "follow_up": lambda: _follow_up(advice, household=False if edition.personal else None),
        "household_follow_up": lambda: _follow_up(advice, household=True),
        "change": lambda: _change(data),
        "allocation": lambda: _allocation(data), "timeline": lambda: _timeline(data),
        "goals": lambda: _goals_brief(data), "future_income": lambda: _future_income(data, overview=True),
        "requests": lambda: (_requests(data.get("requests") or [], False, "생활·목표 보고") if overview
                             else _requests(data.get("requests") or [], True)),
        # The week's holding changes lead the markets day (report design §36); the holdings card then omits them.
        "week": lambda: _holdings_week(data, story),
        "holdings": lambda: _holdings(data, story, True, quantities="week" not in edition.opening),
        "policy": lambda: _policy(data, story, True),
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
        "my_pensions": lambda: _my_pensions(data), "household_tax": lambda: _household_tax(advice),
        # The reader's own holdings and their week (report design §42), and the two of them together. The letter's
        # own narrative call opined on the reader's own holdings (narrative.opined).
        "my_week": lambda: _holdings_week(_own(data), story),
        "my_holdings": lambda: _holdings(_own(data), story, True, quantities=False),
        "household_holdings": lambda: _household_holdings(data),
        # The reader's own loans and the two of them together (report design §42).
        "my_debt": lambda: _my_debt(data, advice, sectors),
        "household_debt": lambda: _household_debt(sectors["debt"]),
        "my_real_estate": lambda: _my_real_estate(data),
        # The reader's own month (§42); the household's month, reserve and room are in "spending" and "cash".
        "my_month": lambda: _spending(None, data.get("own_month")),
        "my_overview": lambda: _my_overview(data),
    }
    if data.get("audience", "self") != "self":
        # The requests ask for what the ledger lacks, answered by the client who keeps it; the spouse's replies are not
        # answered (§42.3), so that letter does not list them.
        renderers["requests"] = lambda: ""
    if edition.personal:
        bands += _split_bands(data, edition, renderers, advice, story)
    else:
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
    shown = edition.opening + edition.sections + edition.household
    notes = pension_notes(data.get("pension_stack")) if "pension_stack" in shown else []
    reader = data.get("audience", "self")
    bands.append(_band(SOFT, _footer(_system_warnings(data), week=_week_line(data), corrections=_corrections_line(data),
                                     notes=notes, reader=reader,
                                     doubtful=_doubtful(data, story) if reader != "self" else ())))
    return _subject(data, status), _page(bands)


def _doubtful(data: dict[str, Any], story: dict[str, Any]) -> list[str]:
    """The parts of a letter put back after failing verification, by the name the reader sees (report design §42.4;
    collected when the narrative is attached, narrative._doubtful_parts). The client's letter keeps these caveats in
    the system notes its e-mail answers read; the spouse's replies are not answered, so that letter names them at its
    end (user decision 2026-11-08)."""
    return list(story.get("doubtful") or [])


def _split_bands(data: dict[str, Any], edition: editions.Edition, renderers: dict[str, Any], advice: dict[str, Any],
                 story: dict[str, Any]) -> list[str]:
    """A per-person letter's body (report design §42): "나의 …" (the reader's own accounts, with the proposals made to
    the reader) and then "우리 집 …" (the two of them together). Each group's first section is open. Both letters
    call the reader 고객님 (user decision 2026-11-08)."""
    def group(names: tuple[str, ...]) -> list[str]:
        parts = [renderers[name]() for name in names]
        return [_opened(part) if i == 0 else part for i, part in enumerate(p for p in parts if p)]

    mine = group(edition.personal)
    # The proposals on the reader's own money, and those on the household's (editions.HOUSEHOLD_PROPOSALS) under the
    # household's group, so the two letters never each read the household's room as their own (sample 2026-11-08).
    own = {**advice, "proposals": [p for p in advice["proposals"] if p.key not in editions.HOUSEHOLD_PROPOSALS]}
    both = {**advice, "undetermined": [],
            "proposals": [p for p in advice["proposals"] if p.key in editions.HOUSEHOLD_PROPOSALS]}
    full = edition.key != "overview"   # The overview carries every proposal briefly, pointing to its themed day.
    proposals = (_proposals(own, full, edition=edition) if own["proposals"] or advice.get("undetermined") else "")
    household_proposals = (_proposals(both, full, edition=edition, heading="두 분께 드리는 제안") if both["proposals"]
                           else "")
    ours = group(edition.household)
    bands = [_band(CANVAS, _group_title(f"나의 {edition.label}", "고객님 명의의 계좌·집·대출·연금·보험만 봅니다.")
                   + "".join(mine[:1]) + proposals + "".join(mine[1:]), border=True)]
    changes = _changes_outside(story, "이번 주 눈여겨볼 변화" if edition.key == "overview" else f"{edition.label} — 눈여겨볼 변화")
    if changes:
        bands.append(_band(CANVAS, changes, border=True))
    # The household paragraph this letter's narrative wrote for its reader (§42), under the household's title.
    shared = story.get("household") or ""
    lead = ("" if not shared else f'<tr><td style="padding:0 0 10px"><div style="font-size:15px;line-height:1.7;'
                                  f'color:{INK}">{escape(shared)}</div></td></tr>')
    bands.append(_band(CANVAS, _group_title(f"우리 집 {edition.household_label or edition.label}", "두 분을 합쳐 봅니다.")
                       + lead + household_proposals + "".join(ours), border=True))
    # The long-range tracks (home move, lease return, retirement) are the household's: under its group.
    if edition.strategy and advice["strategy"]:
        bands.append(_band(DARK, _strategy(advice["strategy"], story.get("direction", ""))))
    return bands


def _own(data: dict[str, Any]) -> dict[str, Any]:
    """The report data with the reader's own holdings, week and securities figures (unrealized gains, Hermes) in
    place of the household's (report design §42; Codex review 2026-11-08: the card's gain total was the household's)."""
    own = data.get("own_holdings") or data["holdings"]
    listed = [p for p in own["positions"] if not p["hermes"]]
    gains = [p["unrealized_gain"] for p in listed]
    known = [g for g in gains if g is not None]
    hermes = [p["value"] for p in own["positions"] if p["hermes"] and p["value"] is not None]
    securities = {**data["sectors"]["securities"], "unrealized_gain": sum(known, Decimal(0)) if known else None,
                  "unrealized_gain_counted": len(known), "unrealized_gain_missing": len(gains) - len(known),
                  "hermes": sum(hermes, Decimal(0)) if hermes else None}
    return dict(data, holdings=own, sectors={**data["sectors"], "securities": securities},
                holding_price_moves=data.get("own_holding_price_moves"),
                holding_quantity_changes=data.get("own_holding_quantity_changes"))


def _household_holdings(data: dict[str, Any]) -> str:
    """The two members' listed holdings together (report design §42): the largest by value with whose they are,
    the shares of the combined portfolio, and the holdings both members hold. Hermes-managed lots stay out."""
    review = data["holdings"]
    listed = [p for p in review["positions"] if not p["hermes"] and p["value"] is not None]
    if not listed:
        return ""

    def whose(p: dict[str, Any]) -> str:
        owners = p.get("owners") or []
        return "두 분" if len(owners) > 1 else "본인" if owners == ["self"] else "배우자"
    top, rest = listed[:4], listed[4:]
    segments = [(str(p["name"])[:12], p["value"], "") for p in top]
    if rest:
        segments.append((f"기타({len(rest)}종)", sum((p["value"] for p in rest), Decimal(0)), ""))
    inner = _stacked(segments) + '<div style="height:8px"></div>'
    inner += _table(["종목", "평가액", "비중", "보유"], [[_named(p), _short(p["value"]), _pct(p["share"]), whose(p)]
                                                     for p in listed[:10]])
    both = [str(p["name"]) for p in listed if len(p.get("owners") or []) > 1]
    lead = (f"두 분이 가진 종목은 {len(listed)}개 {_short(review['total'])}이고, 가장 큰 종목({escape(str(listed[0]['name']))})이 "
            f"<b>{_pct(review['top1_share'])}</b>, 상위 5개가 <b>{_pct(review['top5_share'])}</b>, 해외 상장 비중은 "
            f"{_pct(review['overseas_share'])}입니다." + (f" 두 분이 함께 가진 종목은 {escape(', '.join(both))}입니다." if both else ""))
    return _section("두 분의 보유 종목", lead, inner, brief=None,
                    note=escape(f"{len(listed)}개 종목 · 상위 {min(len(listed), 10)}개"))


def _my_debt(data: dict[str, Any], advice: dict[str, Any], sectors: dict[str, Any]) -> str:
    """The reader's own loans (report design §42): the line-up, the rate sensitivity and the refinancing verdict on
    the reader's own figures, the same card the rates day opens with."""
    own = data.get("own_debt") or sectors["debt"]
    if not own["loans"]:
        return _section("나의 대출", "고객님 명의의 대출은 없습니다.", "")
    return _debt(own, advice.get("refinance"), sectors["macro"].get("policy"))


def _household_debt(debt: dict[str, Any]) -> str:
    """Both members' loans together (report design §42): the total, the variable-rate part and the month's interest,
    and each loan with whose it is. The reader's own sensitivity is in "나의 금리·대출"."""
    loans = [l for l in debt["loans"] if l["amount"] is not None]
    if not debt["loans"]:
        return ""
    total = _total_or_none(l["amount"] for l in debt["loans"])
    # Each label already says whose the loan is ("신용대출(배우자)", report_data._liability_label).
    lead = (f"두 분의 대출과 돌려줄 보증금은 {len(debt['loans'])}건 "
            f"<b>{'미상' if total is None else _short(total)}</b>이고, 이 가운데 변동금리는 "
            f"{'미상' if debt['variable_total'] is None else _short(debt['variable_total'])}, 월 이자는 약 "
            f"{'미상' if debt['monthly_interest_estimate'] is None else _short(debt['monthly_interest_estimate'])}입니다.")
    table = _table(["대출", "잔액", "금리", "방식"],
                   [[l["label"], _short(l["amount"]), f"{l['rate'] * 100:.2f}%",
                     RATE_TYPE_LABELS.get(l["rate_type"], l["rate_type"])] for l in loans])
    return _section("두 분의 대출", lead, table, brief=None, note=escape(f"{len(debt['loans'])}건"))


def _my_overview(data: dict[str, Any]) -> str:
    """The reader's own side of the household at a glance (report design §42): net worth, assets by kind, loans, the
    month and the expected pay, all in the reader's name; the household's whole picture follows under "우리 집"."""
    # Unknown when one of the reader's assets or liabilities has no FX rate (report_data._own_net_worth).
    worth = data.get("own_net_worth")
    groups = [(g["label"], g["by_owner"].get("self")) for g in (data.get("composition") or {}).get("groups") or []]
    groups = [(label, value) for label, value in groups if value]
    loans = (data.get("own_debt") or {}).get("loans") or []
    owed = _total_or_none(l["amount"] for l in loans) if loans else Decimal(0)
    month = data.get("own_month")
    coming = [i for i in (data.get("future_income") or {}).get("items") or []
              if i.owner_id == "self" and not i.overdue and i.certainty in ("confirmed", "expected")]
    pay = _total_or_none(i.net for i in coming) if coming else None
    lead = (f"고객님 명의 순자산은 <b>{_short(worth)}</b>입니다." if worth is not None else
            "고객님 명의 자산·부채 가운데 원화로 환산하지 못한 것이 있어 순자산은 <b>미상</b>입니다.")
    if groups:
        lead += (" 자산은 " if worth is not None else " 환산된 자산만 보면 ") \
            + ", ".join(f"{escape(label)} {_short(value)}" for label, value in groups) + "입니다."
    rows = [["순자산", "미상" if worth is None else _short(worth)],
            ["대출과 돌려줄 보증금", "미상" if owed is None else _short(owed)]]
    if month is not None and month["inflow"]:
        rows += [["월 수입", _short(month["inflow"])], ["월 남는 돈(지출·적립 뒤)", _short(month["left"])]]
    if coming:
        rows.append(["앞으로 받을 성과급·주식 보상(확정·예상, 세후)", "미상" if pay is None else _short(pay)])
    chart = _stacked([(label, value, "") for label, value in groups]) if groups else ""
    return _section("나의 자산 한눈에", lead, chart + _table(["항목", "금액"], rows),
                    note=escape("" if worth is None else f"순자산 {_short(worth)}"))


def _my_real_estate(data: dict[str, Any]) -> str:
    """The reader's own homes (report design §42): the property card on the reader's homes, with their trade figures
    and their share of the reader's own assets."""
    own = data.get("own_real_estate")
    if own is None:
        return ""
    if not own["properties"]:
        return _section("나의 부동산", "고객님 명의의 부동산은 없습니다.", "")
    figures = {k[len("own_"):]: v for k, v in (data.get("figures") or {}).items() if k.startswith("own_property")}
    return _real_estate(own, figures)


def _total_or_none(amounts: Any) -> Decimal | None:
    """A sum that is unknown when any part is unknown (missing is not zero)."""
    total = Decimal(0)
    for amount in amounts:
        if amount is None:
            return None
        total += amount
    return total


def _group_title(title: str, note: str) -> str:
    """The heading of a per-person letter's group (나의 … / 우리 집 …) and the one line saying whose figures follow."""
    return (f'<tr><td style="padding:0 0 4px">{_heading(title, size=22)}'
            f'<div style="font-size:14px;line-height:1.6;color:{BODY};margin:0 0 6px">{escape(note)}</div></td></tr>')


def _my_pensions(data: dict[str, Any]) -> str:
    """The reader's own pensions (report design §42): their pipelines from the household stack, what they pay after
    tax once every pension has started at the headline retirement age, and that share of the household's."""
    stack = data.get("pension_stack")
    if stack is None:
        return ""
    mine = [dict(p, owner_label=None) for p in stack["pipelines"] if p.get("owner", "self") == "self"]
    if not mine:
        return ""
    head = next(t for t in stack["tiers"] if t["retire_age"] == stack["retirement_age"])
    at = head["all_on_age"]
    total = sum((p["net_monthly"] if at is None else pension.monthly_at(p, at) for p in mine
                 if p["net_monthly"] is not None), Decimal(0))
    share = total / stack["total_net"] if stack["total_net"] else None
    lead = (f"고객님 명의 연금은 {len(mine)}개이고, {stack['retirement_age']}세 은퇴 기준 모든 연금이 나온 뒤 세후 월 "
            f"<b>{_short(total)}</b>입니다" + (f"(우리 집 연금의 {_pct(share, 0)})" if share is not None else "") + ".")
    missing = [p for p in mine if p["net_monthly"] is None]
    if missing:
        lead += f" {', '.join(_sub_label(p) for p in missing)}은 아직 수령액을 몰라 뺐습니다."
    # The reader's own charts beside the household's (user request 2026-11-08), each pipeline in the household's colour.
    colors = _pipeline_colors(stack)
    known = [p for p in mine if p["net_monthly"] is not None and p["start_age"] is not None]
    parts = [part for p in known for part in pension.segments(p)]
    charts = _pension_charts(mine, pension.timeline({"pipelines": mine}), colors, retire_age=stack["retirement_age"],
                             focus_age=max((start for start, _, _, _ in parts if start is not None), default=None),
                             drop_age=max((end for _, _, _, end in parts if end is not None), default=None),
                             whose="고객님 명의, ")
    return _section("나의 연금", lead, charts + _family_table(dict(stack, pipelines=mine), colors), brief=None,
                    note=escape(f"세후 월 {_short(total)}"))


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
        # A per-person markets day (§42) shows the reader's own holdings in the hero; the household's follow below.
        own = bool(edition.personal) and data.get("own_holdings") is not None
        review, p, mine = (data["own_holdings"], "own_", "나의 ") if own else (data["holdings"], "", "")
        listed = [x for x in review["positions"] if not x["hermes"]]
        top = listed[0]["name"] if listed else ""
        partial = f" · 원가 아는 {f[p + 'listed_gain_counted']}개만" if f[p + "listed_gain_missing"] else ""
        return [(f"{mine}보유 종목 평가액", amount(f[p + "listed_value"]), f"{f[p + 'listed_count']}개 종목 · Hermes 운용분 제외"),
                ("미실현 손익", "미상" if f[p + "listed_gain"] is None else _signed_short(f[p + "listed_gain"]),
                 "팔지 않아 확정되지 않은 손익" + partial),
                ("해외 상장 비중", _pct(review["overseas_share"], 0), "보유 증권 전체 기준"),
                ("가장 큰 종목 비중", _pct(review["top1_share"], 0), str(top)[:16])]
    if edition.tiles == "property":
        # A per-person property day (§42) shows the reader's own homes in the hero; the household's follow below.
        own = bool(edition.personal) and data.get("own_real_estate") is not None
        estate, mine = (data["own_real_estate"], "나의 ") if own else (s["real_estate"], "")
        if own:
            f = {**f, **{k[len("own_"):]: v for k, v in f.items() if k.startswith("own_property")}}
        lease = next((t for t in data["timeline"] if t.get("category") == "lease_deposit_obligation"), None)
        basis = " · ".join(f.get("property_valuation") or []) or "원장 입력가"
        if f.get("property_estimate") is not None and f["property_value"] is not None:
            basis += f" · 실거래 중간값 합계 {amount(f['property_estimate'])}({_signed_short(f['property_estimate'] - f['property_value'])})"
        elif f.get("property_estimate_reference") is not None and f["property_value"] is not None:
            reference = f["property_estimate_reference"]
            basis += f" · 실거래 중간값 합계 {amount(reference)}({_signed_short(reference - f['property_value'])}, 참고)"
        return [(f"{mine}부동산 평가액", amount(f["property_value"]), f"자산의 {_pct(estate['share_of_assets'], 0)} · 기준: {basis}"),
                (f"{mine}부동산 관련 부채", amount(f["property_debt"]), "담보대출·보증금 반환"),
                ("돌려드릴 보증금", "-" if lease is None else amount(lease["amount"]),
                 "" if lease is None else f"{lease['date']:%Y.%m} 만기"),
                ("최근 6개월 실거래", f"{f['property_trades_6m']}건", _deals_stale(estate) or "같은 단지·같은 면적")]
    if edition.tiles == "rates":
        # A per-person rates day (§42) shows the reader's own loans in the hero; the household's follow below.
        own = bool(edition.personal) and data.get("own_debt") is not None
        debt, mine = (data["own_debt"], "나의 ") if own else (s["debt"], "")
        one = next((x for x in debt["shocks"] if x["shock"] == Decimal("0.01")), None)
        base = next((r for r in s["macro"]["evidence"] if r["spec"].series_id == "722Y001/0101000" and r["value"] is not None),
                    None)
        policy = s["macro"].get("policy") or {}
        meeting = policy.get("next_meeting")
        when = "" if base is None else f"{base['observed_on']:%m.%d} 기준"
        if meeting is not None:  # The rate-setting day matters more than the observation day (§28).
            when += (" · " if when else "") + ("오늘 금통위" if policy.get("meeting_today") else f"다음 금통위 {meeting:%m.%d}")
        return [(f"{mine}변동금리 대출", "미상" if debt["variable_total"] is None else _short(debt["variable_total"]), "잔액"),
                (f"{mine}월 이자(추정)", "미상" if debt["monthly_interest_estimate"] is None
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
                 else ("노후 준비율(연금 세후)", _pct(stack["ratio"], 0),
                       f"{stack['retirement_age']}세 은퇴 기준 · "
                       + " · ".join(f"{t['retire_age']}세 {_pct(t['ratio'], 0)}" for t in others)
                       + (f" · {len(stack['unknown'])}개 미상 제외" if stack["unknown"] else "")))
        return [first,
                room,
                ("공제받지 못하는 납입", _short(plan["excess"]), "한도를 넘어 세액공제가 없는 금액"),
                ("해외주식 미실현 이익", _short(plan["us_gains"]) if plan["us_positions"] else "-",
                 (("올해 실현 미상 · 남은 공제 미상" if plan["us_realized"] is None else
                   f"올해 실현 {_short(plan['us_realized'])}(추정) · 남은 공제 "
                   f"{_short(max(Decimal(0), tax.OVERSEAS_DEDUCTION - plan['us_realized']))}") if plan.get("us_sales")
                  else "연 250만 원까지 공제")
                 + (f" · 원가 모르는 {plan['us_unknown']}종목 제외" if plan["us_unknown"] else ""))]
    if edition.tiles == "life":
        summary = data["view"].summary
        # Without an FX rate for some flow the monthly totals are partial: shown, but never as complete.
        partial = " · 일부 미상" if "cash_flow" in summary.incomplete_areas else ""
        months = "미상" if k["immediate_months"] is None else f'월 지출 {k["immediate_months"]:.1f}개월분'
        month = data.get("own_month")
        if edition.personal and month is not None:
            # The reader's own month (§42): the household's month and reserve are in "우리 집 생활·목표".
            return [("나의 월 수입", _short(month["inflow"]), "반복 수입"),
                    ("나의 월 지출", _short(month["fixed"] + month["discretionary"]), "대출 상환·보험 포함"),
                    ("나의 월 적립·투자", _short(month["transfers"]), "본인 계좌로 옮기는 돈"),
                    ("나의 남는 돈", _short(month["left"]), f"가구가 즉시 쓸 수 있는 돈: {months}")]
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
    # A report whose prose failed says so before it is opened (user decision 2026-11-03): its opinions are missing.
    failed = ((data.get("narrative") or {}).get("status") or {}).get("outcome") == "failure"
    return f"[Smith] {day} {edition} · {STATUS[status][1]} · {lead}" + (" · 해설 없음" if failed else "")


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
             "" if home is None else
             f'{home["target_date"]:%Y.%m} · {proposals.home_words(home)[0]}만 {_pct(home["ratio_self"], 0)}')]


def _composition_bar(data: dict[str, Any], edition: editions.Edition | None) -> str:
    """The micro chart under the judgement (report design §18.1): what the headline amounts of this day are
    made of, as one stacked bar with a legend. Each theme shows its own composition."""
    title, segments, note = _hero_segments(data, None if edition is None else edition.tiles)
    if not segments:
        return ""
    colors = None
    if edition is not None and edition.tiles == "tax":   # Family hues, the shortfall in neutral grey.
        hue = {label: _FAMILY_SHADES[key][0] for key, label in pension.CATEGORIES}
        colors = [hue.get(label, MUTED_BAR) for label, value, _ in segments if value is not None and value > 0]
    bar = _stacked(segments, on_dark=True, colors=colors)
    if not bar:
        return ""
    foot = f'<div style="font-size:12px;color:{ON_DARK};margin-top:6px">{escape(note)}</div>' if note else ""
    return (f'<tr><td style="padding-top:16px"><div style="font-size:12px;font-weight:600;color:{ON_DARK};'
            f'margin-bottom:8px">{escape(title)}</div>{bar}{foot}</td></tr>')


def _hero_segments(data: dict[str, Any], tiles: str | None) -> tuple[str, list[tuple[str, Decimal | None, str]], str]:
    """(title, segments, note) of the hero bar for a tile set: the balance sheet by kind and owner on the
    overview, the holdings on the markets day, the homes, the loans, the pension pipelines, the month."""
    if tiles == "markets":
        own = editions.personalized(data["kind"]) and data.get("own_holdings") is not None
        review = data["own_holdings"] if own else data["holdings"]
        listed = [p for p in review["positions"] if not p["hermes"] and p["value"] is not None]
        top, rest = listed[:4], listed[4:]
        segments = [(str(p["name"])[:12], p["value"], "") for p in top]
        if rest:
            segments.append((f"기타({len(rest)}종)", sum((p["value"] for p in rest), Decimal(0)), ""))
        return ("나의 " if own else "") + "보유 종목 구성(Hermes 운용분 제외)", segments, ""
    if tiles == "property":
        own = editions.personalized(data["kind"]) and data.get("own_real_estate") is not None
        props = (data["own_real_estate"] if own else data["sectors"]["real_estate"])["properties"]
        return ("나의 " if own else "") + "부동산 평가액 구성", [(p["label"], p["value"], "") for p in props], ""
    if tiles == "rates":
        own = editions.personalized(data["kind"]) and data.get("own_debt") is not None
        debt = data["own_debt"] if own else data["sectors"]["debt"]
        drawn = [(l["label"], l["amount"], "") for l in debt["loans"] if l["amount"]]
        unused = debt.get("unused_credit")
        note = f"미사용 한도 {_short(unused)}(마이너스통장)는 부채가 아니라 유사시 바로 쓸 수 있는 여유입니다." if unused else ""
        return ("나의 " if own else "") + "부채 구성", drawn, note
    if tiles == "tax":
        stack = data.get("pension_stack")
        if stack is None:
            return "", [], ""
        # One segment per pension family (공적·퇴직·개인·비과세), as in the stack card (user request 2026-11-07), each
        # at the age every pension has started, the same reading as total_net (a fixed-term one may have ended).
        head = next(t for t in stack["tiers"] if t["retire_age"] == stack["retirement_age"])
        at = head["all_on_age"]
        segments = [(label, sum((p["net_monthly"] if at is None else pension.monthly_at(p, at) for p in members
                                 if p["net_monthly"] is not None), Decimal(0)), "")
                    for _, label, members in _families(stack)]
        segments = [s for s in segments if s[1] > 0]
        gap = stack["target"] - stack["total_net"]
        if gap > 0:
            segments.append(("목표까지 부족", gap, ""))
        note = f"세후 월 수령액 합계 {_short(stack['total_net'])} / 목표 월 {_short(stack['target'])}"
        return (f"노후 월 현금흐름 준비({stack['retirement_age']}세 은퇴 기준)", segments,
                note + (f" · 미상 {len(stack['unknown'])}개 제외" if stack["unknown"] else ""))
    if tiles == "life":
        # A per-person life day (§42) shows the reader's own month in the hero; the household's follows below.
        own = editions.personalized(data["kind"]) and data.get("own_month") is not None
        split = data.get("own_month") if own else data.get("spending_split")
        if split is None or not split["inflow"]:
            return "", [], ""
        segments = [("고정비", split["fixed"], ""), ("재량비", split["discretionary"], ""), ("적립·투자", split["transfers"], "")]
        if split["left"] > 0:
            segments.append(("남는 돈", split["left"], ""))
        return f"{'나의 ' if own else ''}이번 달 수입 {_short(split['inflow'])}의 쓰임", segments, ""
    composition = data.get("composition") or {}
    groups = composition.get("groups") or []
    segments = []
    for g in groups:
        owners = g["by_owner"]
        note = " · ".join(f"{'본인' if o == 'self' else '배우자'} {_short(v)}" for o, v in sorted(owners.items(), key=lambda item: (item[0] != "self", item[0]))) if len(owners) > 1 else ""
        segments.append((g["label"], g["value"], note))
    note = "" if composition.get("complete", True) else "환율이 없어 원화로 바꾸지 못한 자산은 빠져 있습니다."
    return f"자산의 구성 · {_short(composition.get('total'))}", segments, note


def _stacked(segments: list[tuple[str, Decimal | None, str]], *, on_dark: bool = False,
             colors: list[str] | None = None) -> str:
    """One stacked bar with a legend: each segment in its fixed colour, a 2px gap between fills, the share
    and an optional note beside each label. Unknown or zero segments are left out; nothing without a total.
    `colors` (one per segment, all segments positive) replaces the palette, e.g. a family's shades."""
    rows = [(label, value, note) for label, value, note in segments if value is not None and value > 0]
    palette = colors or SEGMENTS
    if colors is None and len(rows) > len(SEGMENTS):  # Hues are never cycled: the tail folds into one segment.
        rows = rows[:len(SEGMENTS) - 1] + [("기타", sum((v for _, v, _ in rows[len(SEGMENTS) - 1:]), Decimal(0)), "")]
    total = sum((value for _, value, _ in rows), Decimal(0))
    if not rows or not total:
        return ""
    surface = DARK if on_dark else CANVAS
    cells = "".join(f'<td style="width:{max(1, int(value / total * 100))}%;height:14px;font-size:0;line-height:0;'
                    f'background:{palette[i % len(palette)]};border-right:2px solid {surface}">&nbsp;</td>'
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
        dot = f'<span style="color:{palette[i % len(palette)]}">●</span> '
        extra = f' <span style="font-family:{MONO}">({escape(note)})</span>' if note else ""
        items.append(f'<span style="display:inline-block;white-space:nowrap;margin:6px 12px 0 0;font-size:12px;color:{ink}">'
                     f'{dot}{escape(label)} <span style="font-family:{MONO}">{_pct(value / total, 0)}</span>{extra}</span>')
    return bar + f'<div style="line-height:1.8">{divider.join(items)}</div>'


def _gauge(label: str, ratio: Decimal | None, text: str) -> str:
    """Progress toward a target: the filled share (capped at 100%), the figure beside it, a line under it."""
    fill = 0 if ratio is None else max(0, min(100, int(ratio * 100)))
    shown = "미상" if ratio is None else _pct(ratio, 0)
    # The label on its own line: "주택 목표 준비율(가구 통합 + 예정 소득(확정·예상, 세후))" in a 160px cell that would not
    # wrap pushed the bar off a phone screen (user review 2026-11-02).
    return (f'<div style="font-size:13px;line-height:1.5;color:{BODY};margin-top:6px">{escape(label)}</div>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:4px 0 4px"><tr>'
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


GOOD_ON_DARK, BAD_ON_DARK = "#4cd98a", "#ff6b78"   # Event notes: good news green, bad news red (user 2026-11-03), light for a dark tile.


def _tile_grid(tiles: list[tuple[Any, ...]]) -> str:
    """Tiles of (title, value, sub); a fourth item, a Decimal, colours the value green when good and red when bad."""
    def tile(title: str, value: str, sub: str, tone: Decimal | None = None) -> str:
        color = ON_DARK if not tone else GOOD_ON_DARK if tone > 0 else BAD_ON_DARK
        return (f'<td style="width:50%;padding:5px;vertical-align:top"><div style="border-radius:24px;padding:16px 18px;'
                f'background:{DARK_RAISED}">'
                f'<div style="font-size:12px;font-weight:600;color:{ON_DARK_SOFT}">{escape(title)}</div>'
                f'<div style="font-family:{MONO};font-weight:500;font-size:22px;color:{color};margin:8px 0 2px">'
                f'{escape(value)}</div>'
                f'<div style="font-size:12px;color:{ON_DARK_SOFT}">{escape(sub)}</div></div></td>')
    # Two by two so the tiles stay readable on a phone.
    grid = "".join(f"<tr>{tile(*a)}{tile(*b)}</tr>" for a, b in (tiles[:2], tiles[2:]))
    return (f'<tr><td style="padding-top:18px"><table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0">{grid}</table></td></tr>')


def _proposals(advice: dict[str, Any], full: bool, edition: editions.Edition | None = None,
               heading: str = "고객님께 드리는 제안") -> str:
    """The proposal cards: why now, effect, risks, timing, what would change the judgement, certainty.
    Thursday cards are short but keep the certainty label. Judgements stopped by unknown inputs follow."""
    proposals = advice.get("proposals") or []
    themed = edition is not None and edition.key != "overview"
    head = f'<tr><td>{_eyebrow("오늘의 제안" if themed else "이번 주 제안")}{_heading(heading, size=28)}</td></tr>'
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
            count = len(p.table) - 1
            # The note names the ranks there are (the 2026-11-02 letter had two picks and spoke of "2·3순위").
            others = {1: "", 2: "2순위는 1순위 대신 고를 때의 대안입니다. "}.get(count, f"2~{count}순위는 1순위 대신 고를 때의 대안입니다. ")
            detail += (f'<div style="font-size:14px;font-weight:700;color:{INK};margin-top:14px">옮길 곳 '
                       + ("1순위" if count == 1 else f"1~{count}순위")
                       + (f" · 분기 {_short(step)}" if step is not None else "") + "</div>"
                       + _pick_cards(p.table[1:]) + _sources(p.table_links)
                       + f'<div style="font-size:12px;line-height:1.6;color:{MUTED};margin-top:6px">수량은 분기 금액을 1순위에 모두 '
                       f"넣을 때입니다. {others}가격은 조사 시점 종가라 주문 전 다시 확인하십시오.</div>")
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


def _pick_cards(rows: tuple[tuple[str, ...], ...]) -> str:
    """The products to move money into (picks.pick_table rows), one card per rank: the product, its market, fee
    and price date, the shares one quarterly step buys (rank 1) or the alternative's price, and the reason at
    reading size. A phone showed the former four-column table at 12px with the reason in a narrow column (user
    review 2026-10-27)."""
    cards = []
    for rank, product, reason, size, meta in rows:
        pill = _pill(f"{rank}순위", "good" if rank == "1" else "neutral")
        cards.append(f'<div style="border:1px solid {HAIRLINE};border-radius:16px;padding:12px 14px;margin-top:8px">'
                     f'<div style="font-size:14px;font-weight:600;line-height:1.5;color:{INK}">{pill} {escape(product)}</div>'
                     f'<div style="font-size:12px;color:{MUTED};margin-top:4px">{escape(meta)}</div>'
                     f'<div style="font-size:14px;font-weight:600;font-family:{MONO};color:{INK};margin-top:8px">{escape(size)}</div>'
                     f'<div style="font-size:13px;line-height:1.65;color:{BODY};margin-top:6px">{escape(reason)}</div></div>')
    return "".join(cards)


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
    """"▼ 펼쳐서 상세 보기" on a closed card, "▲ 접기" on an open one; floated right in a section summary, or its
    own line (`block`) under a card title. Hidden inline and shown only by the stylesheet inside a real <details>:
    Gmail (app and web) drops <details> and shows every card open, so the hint there promised a tap that did
    nothing (client report 2026-11-02)."""
    color = ON_DARK if on_dark else BLUE
    place = "margin-top:6px" if block else "float:right;margin-left:10px"
    kind = "mk-hint mk-block" if block else "mk-hint"
    return (f'<span class="{kind}" style="display:none;{place};font-size:12px;font-weight:600;color:{color};white-space:nowrap">'
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
    for field, label in (("situation_memo", "판단"), ("direction_memo", "전략 방향"), ("portfolio_memo", "투자 전략"),
                         ("household_memo", "우리 집")):
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


def _household_tax(advice: dict[str, Any]) -> str:
    """The tax points about both members (marriage registration, gifts, the other member's home) in a per-person
    letter's household group (report design §42); empty without any."""
    notes = [n for n in advice.get("tax_notes") or [] if n.household]
    if not notes:
        return ""
    inner = "".join(f'<div style="margin:6px 0 14px"><div style="font-size:15px;font-weight:600;color:{INK};'
                    f'margin-bottom:4px">{escape(n.title)}</div><div style="font-size:14px;line-height:1.6;color:{BODY}">'
                    f'{escape(_first_sentence(n.body))}</div></div>' for n in notes[:MAX_TAX_NOTES])
    return _section("두 분의 세금: 혼인신고·주택·증여", "두 분이 함께 정할 세금 문제입니다.", inner, brief=None)


def _tax(advice: dict[str, Any], data: dict[str, Any], full: bool) -> str:
    """After-tax view: strategy notes (Monday) and the year-end settlement checklist (Q4, both editions). In a
    per-person letter (§42) the household's notes go to `_household_tax` instead."""
    notes, rows = advice.get("tax_notes") or [], advice.get("tax_checklist") or []
    edition = editions.of(data["kind"])
    if edition is not None and "household_tax" in edition.household:
        notes = [n for n in notes if not n.household]
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


def _follow_up(advice: dict[str, Any], *, household: bool | None = None) -> str:
    """Proposals the client accepted (in progress) and earlier decisions (§10.6). A per-person letter splits them
    like its proposals (`household`: only those on the household's money, or only the others; Codex review
    2026-11-08: an accepted household spending room came back under "나의 …" in both letters)."""
    in_progress, decided = advice.get("in_progress") or [], advice.get("decided") or []
    if household is not None:
        in_progress = [p for p in in_progress if (p.key in editions.HOUSEHOLD_PROPOSALS) == household]
        decided = [e for e in decided if (e["key"] in editions.HOUSEHOLD_PROPOSALS) == household]
    if not in_progress and not decided:
        return ""
    labels = {"declined": "보류하기로 하셨습니다", "done": "완료하셨습니다"}
    rows = [f'<div style="font-size:14px;line-height:1.6;color:{BODY};margin:4px 0">· <b style="font-weight:600;color:{INK}">'
            f'진행 중</b> — {escape(p.title)}</div>' for p in in_progress]
    rows += [f'<div style="font-size:14px;line-height:1.6;color:{BODY};margin:4px 0">· {escape(e["key"])}: '
             # The ledger keeps a decision's time as `status_at` (Codex review 2026-11-08: the old field name crashed
             # any letter with a decision on record).
             f'{labels[e["status"]]}({(e.get("status_at") or "")[:10]})</div>' for e in decided]
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
    # The client's plan in either letter (§42): the spouse's letter names the planner 배우자분 and the spouse 고객님.
    planner, other = proposals.home_words(home)
    text = (f"주택 목표는 기준 시나리오에서 <b>{escape(home['baseline_label'])}</b> 기준으로 필요액의 "
            f"<b>{_pct(home['ratio_baseline'], 0)}</b>를 준비할 수 있고, "
            + (f"부족분은 약 <b>{_short(gap)}</b>입니다." if gap > 0 else f"필요액을 넘어 약 <b>{_short(-gap)}</b>이 남습니다.")
            + f" 보수적으로 지금 있는 자산만 보면 {planner} 자원 {_pct(home['ratio_self'], 0)}")
    if home.get("ratio_household") is not None:
        text += f", {other} 부동산 순자산을 더해 {_pct(home['ratio_household'], 0)}"
    text += "입니다. 집값 연 2%와 투자 수익 연 4%를 가정했습니다."
    after = home.get("after_tax")
    if after and after.get("case") == "exempt":
        text += (f" {other} 주택은 2년 보유·거주가 확인되어 팔 때 양도세를 1세대 1주택 비과세 기준 약 {_short(after['tax']['exempt'])}"
                 f"(12억 원 초과분만 과세)으로 보며, 요건을 못 갖추면 약 {_short(after['tax']['general'])}입니다. 혼인신고를 하시면 혼인일부터 "
                 "10년 안에 이 집을 먼저 파는 순서가 안전합니다.")
    elif after:
        text += (f" {other} 주택을 팔아 쓰는 경우의 양도세는 1세대 1주택 비과세 요건을 갖추면 약 {_short(after['tax']['exempt'])}, 일반 "
                 f"과세면 약 {_short(after['tax']['general'])}이며, 혼인신고 시점과 2년 거주 여부에 따라 갈립니다.")
    if home.get("pay_at_target") is None and home.get("pay_after_target") is None:
        pass
    elif home.get("pay_at_target") is None:  # Unknown (no FX rate for some pay), never shown as none.
        text += " 예정 소득 가운데 원화로 환산하지 못한 항목이 있어 그 몫은 넣지 않았습니다."
    return text


def _home_plan(home: dict[str, Any]) -> str:
    """Bars and the scenario table of the home goal: the household baseline, the conservative tiers and the
    client-only figures, with expected pay as its own column when there is any (§17.3)."""
    planner, _ = proposals.home_words(home)
    bars = [("필요 자금(비용 포함)", home["required"], False),
            (f"가구 통합 기준({home['baseline_label']})", home["baseline"], True)]
    if home.get("available_household") is not None and home["available_household"] != home["baseline"]:
        bars.append(("가구 통합, 지금 있는 자산만", home["available_household"], False))
    bars.append((f"{planner} 자원만", home["available_self"], False))
    with_pay = any(s.get("expected_pay_at_target") not in (None, "0") for s in home["scenarios"])
    household = any(s.get("available_household") is not None for s in home["scenarios"])
    # One card per scenario, not a six-column table at 12px (user review 2026-11-02: readable on a phone).
    cards = []
    for s in home["scenarios"]:
        parts = [("필요 자금(그때 집값·비용)", s["required_with_costs"]), (f"{planner} 자원", s["available_self"])]
        if household:
            parts.append(("가구 통합", s["available_household"]))
        if with_pay and household:
            parts.append(("예정 소득까지", s["available_household_with_expected_pay"]))
        gap = _dec(s["gap_household_with_expected_pay"] if with_pay and household and s.get("gap_household_with_expected_pay") is not None
                   else s["gap_including_partner_property_equity"] if household else s["gap_self"])
        verdict = ("부족분 미상" if gap is None else f"부족 {_short(gap)}" if gap > 0 else
                   "딱 맞음" if gap == 0 else f"남음 {_short(-gap)}")
        figures = " · ".join(f"{label} {_short(_dec(value))}" for label, value in parts)
        cards.append(f'<div style="border:1px solid {HAIRLINE};border-radius:16px;padding:12px 14px;margin-top:8px">'
                     f'<div style="font-size:14px;font-weight:600;color:{INK}">'
                     f'{escape(SCENARIO_LABELS.get(s["name"], s["name"]))} 시나리오 · {escape(verdict)}</div>'
                     f'<div style="font-size:13px;line-height:1.65;color:{BODY};margin-top:4px">{escape(figures)}</div></div>')
    gauge = _gauge(f"주택 목표 준비율({home['baseline_label']})", home["ratio_baseline"], "")
    return '<div style="height:12px"></div>' + gauge + "".join(cards)


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
                 + (f"2년 보유·거주가 확인되어 1세대 1주택 비과세 기준이며, 요건을 못 갖추면 약 {_short(sale['net_inflow']['general'])}입니다."
                    if sale.get("tax_case") == "exempt" else
                    f"1세대 1주택 비과세 요건을 갖추면 약 {_short(sale['net_inflow']['exempt'])}입니다."))
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
    # A bucket with nothing in it is left out: "불확실 0원" reads as a puzzle, not a fact.
    parts = [f"{label} <b>{_short(sure[key])}</b>" for key, label in
             (("confirmed", "확정"), ("expected", "예상"), ("possible", "불확실(받을 수도 있는 것)")) if sure[key] != 0]
    lead = (f"앞으로 받을 성과급·주식 보상은 세후 추정 {', '.join(parts) or '<b>0원</b>'}입니다. 받기 전까지는 순자산에 넣지 않고, "
            "계획에서는 보수적으로 볼 때 확정분만, 기준 계획에서는 예상분까지"
            + (", 낙관적으로 볼 때 불확실분까지" if sure["possible"] != 0 else "") + " 셉니다.")
    # The spouse's replies are not answered (§42.3): that letter says what is missing without asking for it.
    asks = data.get("audience", "self") == "self"
    if found.get("overdue"):
        lead += (f" 지급 예정일이 지났는데 받았다는 기록이 없는 항목 {len(found['overdue'])}건은 계획에서 뺐습니다."
                 + (" 받으셨는지 알려 주시면 실제 지급일과 과세 금액으로 기록하겠습니다." if asks else ""))
    if found.get("assumed_tax"):
        lead += (" 세전 기본 연봉을 몰라 세금은 가정 세율로 계산한 항목이 있습니다."
                 + (" 연봉을 알려 주시면 더 정확해집니다." if asks else ""))
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
        inner += (f'<div style="font-size:14px;line-height:1.6;color:{BODY};margin-top:10px">회사 주식 집중: '
                  f'{escape(stock.get("name") or stock["symbol"])}의 비중은 지금 주식의 <b>{_pct(stock["share_now"], 0)}</b>이고, 예정된 주식 보상과 자사주로 '
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
    elif not debt["variable_total"]:
        # Sample 2026-11-08: a reader with fixed-rate loans only read "변동금리 대출 0원이 있습니다".
        lead = "<b>변동금리</b> 대출이 없어, 시장 금리가 움직여도 월 이자는 그대로입니다."
    else:
        lead = (f"<b>변동금리</b> 대출 <b>{_short(debt['variable_total'])}</b>"
                + (f"은 금리가 1%p 오르면 월 이자가 약 <b>{_short(one['monthly_change'])}</b> 늘고, 1%p 내리면 그만큼 줄어듭니다."
                   if one else "이 있습니다."))
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
                     l["maturity"][:7].replace("-", ".") if l["maturity"] else "-"]
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



def _holdings(data: dict[str, Any], story: dict[str, Any], full: bool, *, quantities: bool = True) -> str:
    """Holdings review (report design §12.7): the figures code computed, Smith's view of the whole, a table
    with an opinion per holding, and cards for every opinion other than hold. Thursday lists only those.
    `quantities` False leaves the quantity changes to the week's panel at the top (§36)."""
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
        # Selling the last holding is still news: the quantity changes show without a current position.
        return _section("보유 종목 점검", "종목 단위 정보가 있는 증권이 없습니다.", _quantity_changes(data) if quantities else "")
    listed = [p for p in positions if p["value"] is not None]
    segments = [(str(p["name"])[:12], p["value"], "") for p in listed[:4]]
    if len(listed) > 4:
        segments.append((f"기타({len(listed) - 4}종)", sum((p["value"] for p in listed[4:]), Decimal(0)), ""))
    inner = _stacked(segments) + '<div style="height:8px"></div>'
    inner += _table(["종목", "평가액", "비중", "수익률", "Smith 의견"],
                    [[_named(p), _short(p["value"]), _pct(p["share"]), _return_cell(p["return_on_cost"]),
                      Raw(_pill("Hermes 운용", "neutral")) if p["hermes"] else
                      Raw(_stance(_of(opinions, p)["stance"])) if _of(opinions, p).get("stance") else "의견 없음"]
                     for p in positions])
    if quantities:
        inner += _quantity_changes(data)
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


def _holdings_week(data: dict[str, Any], story: dict[str, Any] | None = None) -> str:
    """The week's change in the client's listed holdings, open at the top of the markets day (report design §36; user
    request 2026-11-03: "지난주 대비 변동내역은 중요하니까 더 가독성 높여서 상단에"). Two figures apart (what prices and
    the won/dollar rate did to the shares held, and what bought or sold shares added), the positions that moved most,
    and the quantity changes as cards. Hermes-managed positions are left out, as in the hero. Nothing without a
    comparison report."""
    moves, found, baseline = data.get("holding_price_moves"), data.get("holding_quantity_changes"), data.get("baseline")
    if moves is None or found is None or baseline is None:
        return ""
    since = f"{baseline.month}월 {baseline.day}일"
    mine = [c for c in found["changes"] if not c["hermes"]]
    # An unknown trade amount is not zero, and with a sale among the unknowns the known sum is no bound either: all
    # unknown reads 미상, some unknown reads "금액을 확인한 종목만" (Codex review 2026-11-03).
    known = [c["amount"] for c in mine if c["amount"] is not None]
    traded = sum(known, Decimal(0)) if known else None
    partial = len(known) < len(mine)
    ratio = moves["repriced_effect"] / moves["repriced"] if moves["repriced"] else None
    # A position that could not be split, or whose quantity could not be compared, is not "no change" (Codex review
    # 2026-11-03): with none split the effect is unknown, with some the figures are scoped to the split ones.
    unsplit = [n for n in moves["unknown"] if n not in found["uncompared"]]
    gaps = bool(moves["unknown"] or found["uncompared"])
    effect = moves["total"] if moves["moves"] or not gaps else None
    scope = "나눌 수 있는 종목만" if gaps else ""
    lead = f"{since} 이후 보유 종목은 " + (
        "시세·환율 효과를 매매와 나누지 못했고, " if effect is None else
        f"시세·환율로 <b>{_toned(effect)}</b>" + (f"({scope}), " if scope else ", ")) + (
        "수량을 비교할 수 있는 종목 가운데 사고판 것은 없습니다." if not mine and found["uncompared"] else
        "사고판 종목은 없습니다." if not mine else
        f"사고판 종목은 {len(mine)}개인데 금액은 확인하지 못했습니다." if traded is None else
        f"사고판 것으로 <b>{_toned(traded)}</b> 바뀌었습니다" + ("(금액을 확인한 종목만)." if partial else "."))
    stats = _week_stats([
        ("시세·환율 효과", effect, "수량 그대로일 때의 변화" + ("" if ratio is None else f" · {_delta_pct(ratio)}")
         + (f" · {scope}" if scope and effect is not None else "")),
        ("사고판 몫", traded, ("비교한 종목 가운데 없음" if found["uncompared"] else "없음") if not mine
         else f"수량이 바뀐 종목 {len(mine)}개"
         + (" · 금액 미상" if traded is None else " · 확인된 금액만" if partial else ""))])
    movers = [m for m in moves["moves"] if m["effect"]][:5]
    why = _week_reasons(data, story or {})
    inner = stats + _benchmarks(ratio if effect is not None else None, data.get("index_moves") or [], since)
    if movers:
        rows = "".join(f'<tr><td style="font-size:14px;color:{INK};padding:7px 8px 7px 0;border-bottom:1px solid {STRONG}">'
                       f'{escape(m["name"])}</td><td style="text-align:right;white-space:nowrap;font-size:14px;padding:7px 0;'
                       f'border-bottom:1px solid {STRONG}">{_toned(m["effect"])}'
                       f'<span style="font-size:12px;color:{MUTED}"> {"새로 보유" if m["new"] else "" if m["ratio"] is None else _delta_pct(m["ratio"])}'
                       f'</span></td></tr>' + why.get((m["market"], m["symbol"]), "") for m in movers)
        inner += (f'<tr><td><div style="font-size:13px;font-weight:700;color:{INK};margin:18px 0 4px">시세로 많이 움직인 종목</div>'
                  f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}</table></td></tr>')
    inner += f'<tr><td>{_quantity_cards(mine, since, scoped=bool(found["uncompared"]))}</td></tr>'
    notes = ["사고판 몫은 바뀐 수량을 지금 가격·환율로 환산한 값이고(판 종목은 그때 가격), 주식 보상 입고나 처음 등록한 계좌도 "
             "여기에 잡힙니다. 시세·환율 효과는 평가액 변화에서 사고판 몫을 뺀 나머지라 둘을 더하면 평가액 변화입니다."]
    hermes = len(found["changes"]) - len(mine)
    if hermes:
        notes.append(f"Hermes 운용분은 따로 보지 않습니다(수량이 바뀐 종목 {hermes}개).")
    if found["uncompared"]:
        notes.append(f"수량 기록이 없어 비교하지 못한 종목: {', '.join(str(n) for n in found['uncompared'])}.")
    if moves.get("early_priced"):
        lots = ", ".join(f"{s['name']} {_qty(s['quantity'])}주({s['priced_at']:%m/%d} 가격부터)" for s in moves["early_priced"])
        notes.append(f"지난 보고 때 가격이 오래돼 그 뒤 변화까지 이번 금액에 잡힌 보유분: {lots}. 비율에서는 뺐습니다.")
    if unsplit:
        notes.append(f"가격이나 환율을 몰라 시세와 매매로 나누지 못해 뺀 종목: {', '.join(str(n) for n in unsplit)}.")
    repriced = [r for r in data["view"].records if r.fields.get("statement_price")] if data.get("view") else []
    if repriced:
        notes.append(f"직접 입력한 계좌의 {len(repriced)}개 보유분은 같은 종목의 토스 시세로 다시 매겼습니다(수량은 마지막 입력 기준).")
    foot = "".join(f'<div style="font-size:12px;line-height:1.6;color:{MUTED};margin-top:4px">{escape(n)}</div>' for n in notes)
    if moves["stale"]:
        # A stale price hides that holding's move; said where it is read, not in the grey notes.
        lots = ", ".join(f"{s['name']} {_qty(s['quantity'])}주({s['priced_at']:%m/%d} 가격)" for s in moves["stale"])
        foot = (f'<div style="font-size:13px;line-height:1.6;color:{BODY};background:{PILLS["warn"][0]};border-radius:12px;'
                f'padding:10px 12px;margin-bottom:6px">가격이 지난 보고 뒤로 갱신되지 않아 주가 변화가 빠진 보유분: '
                f'{escape(lots)}. 직접 입력한 계좌라 새 잔고를 가져오면 반영됩니다.</div>') + foot
    return (f'<tr><td>{_eyebrow(f"{baseline:%m/%d} 대비", color="#ffffff", background=BLUE)}'
            f'{_heading("지난주 대비 보유 종목 변화", size=26)}'
            f'<div style="font-size:15px;line-height:1.65;color:{BODY}">{lead}</div></td></tr>'
            f'{inner}<tr><td style="padding-top:12px">{foot}</td></tr>')


INDEX_LABELS = {"802Y001/0001000": "코스피", "SP500": "S&P 500", "NASDAQCOM": "나스닥"}


def _benchmarks(ratio: Decimal | None, indices: list[dict[str, Any]], since: str) -> str:
    """The holdings' week beside the indices' (report design §38): the holdings in won with the rate's effect, each
    index in its own currency over its own last closes, dates shown because they differ by market and lag."""
    if not indices:
        return ""
    def line(label: str, value: Decimal | None, note: str) -> str:
        shown = (f'<span style="font-family:{MONO};color:{MUTED}">미상</span>' if value is None else
                 _toned_text(value, _delta_pct(value)))
        return (f'<tr><td style="font-size:14px;color:{INK};padding:6px 8px 6px 0;border-bottom:1px solid {STRONG}">{escape(label)}'
                f'<div style="font-size:12px;color:{MUTED}">{escape(note)}</div></td>'
                f'<td style="text-align:right;white-space:nowrap;font-size:15px;padding:6px 0;border-bottom:1px solid {STRONG}">'
                f'{shown}</td></tr>')
    rows = line("내 보유 종목", ratio, f"{since} 이후 · 원화, 환율 포함") + "".join(
        line(INDEX_LABELS.get(m["spec"].series_id, m["spec"].label), m["ratio"],
             f'{m["start_on"]:%m/%d} → {m["end_on"]:%m/%d} 종가 · {m["spec"].provider.upper()}') for m in indices)
    return (f'<tr><td><div style="font-size:13px;font-weight:700;color:{INK};margin:18px 0 4px">시장과 비교</div>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}</table></td></tr>')


def _good_bad(value: Decimal, text: str) -> str:
    """An event note's signed figure: a gain is good news in green, a loss bad news in red (user 2026-11-03)."""
    color = MUTED if not value else "#137333" if value > 0 else "#c5221f"
    return f'<span style="font-family:{MONO};font-weight:700;color:{color}">{escape(text)}</span>'


def _toned_text(value: Decimal, text: str) -> str:
    color = MUTED if not value else UP if value > 0 else DOWN
    return f'<span style="font-family:{MONO};font-weight:700;color:{color}">{escape(text)}</span>'


WHY_MOVED = Decimal("0.10")  # A week's move from which the narrative says why (narrative-v33, report design §39).


def _week_reasons(data: dict[str, Any], story: dict[str, Any]) -> dict[tuple[Any, Any], str]:
    """For a holding that moved 10% or more this week, the first sentence of Smith's opinion, which the narrative
    opens with why it moved; shown under the mover's row. Nothing without an opinion written by the model."""
    opinions = story.get("holdings") or {}
    week = {(m["market"], m["symbol"]): m["ratio"] for m in (data.get("holding_price_moves") or {}).get("moves", [])}
    rows = {}
    for p in (data.get("holdings") or {}).get("positions", []):
        ratio, opinion = week.get((p["market"], p["symbol"])), _of(opinions, p)
        if ratio is None or abs(ratio) < WHY_MOVED or not opinion.get("reason") or opinion.get("by_code"):
            continue
        rows[(p["market"], p["symbol"])] = (
            f'<tr><td colspan="2" style="font-size:13px;line-height:1.6;color:{BODY};padding:0 0 8px;'
            f'border-bottom:1px solid {STRONG}">↳ {escape(_first_sentence(opinion["reason"]))}</td></tr>')
    return rows


def _toned(amount: Decimal) -> str:
    """A signed amount in the chart's diverging colours: a gain red, a loss blue, nothing grey (Korean convention)."""
    color = MUTED if not amount else UP if amount > 0 else DOWN
    return f'<span style="font-family:{MONO};font-weight:700;color:{color}">{escape(_signed_short(amount))}</span>'


def _week_stats(stats: list[tuple[str, Decimal | None, str]]) -> str:
    """Two light tiles side by side: the label, the signed amount at 22px in its colour, a short line under it."""
    def tile(label: str, amount: Decimal | None, sub: str) -> str:
        value = (f'<span style="font-family:{MONO};font-size:22px;color:{MUTED}">{"–" if sub.endswith("없음") else "미상"}</span>'
                 if amount is None else
                 _toned(amount).replace("font-weight:700", "font-weight:700;font-size:22px", 1))
        return (f'<td style="width:50%;padding:5px;vertical-align:top"><div style="border-radius:16px;padding:14px 16px;'
                f'background:{SOFT}"><div style="font-size:13px;font-weight:600;color:{MUTED}">{escape(label)}</div>'
                f'<div style="margin:6px 0 2px">{value}</div>'
                f'<div style="font-size:12px;line-height:1.5;color:{MUTED}">{escape(sub)}</div></div></td>')
    return (f'<tr><td style="padding-top:14px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
            f'<tr>{"".join(tile(*s) for s in stats)}</tr></table></td></tr>')


def _quantity_cards(changes: list[dict[str, Any]], since: str, *, scoped: bool) -> str:
    """The client's quantity changes as one short card each (a phone could not read the four-column table, §33).
    `scoped`: some positions could not be compared, so an empty list speaks only of the compared ones (§32)."""
    title = f'<div style="font-size:13px;font-weight:700;color:{INK};margin:18px 0 4px">{since} 이후 수량이 바뀐 종목</div>'
    if not changes:
        scope = "수량을 비교할 수 있는 종목 가운데 " if scoped else ""
        return title + f'<div style="font-size:14px;line-height:1.6;color:{BODY}">{scope}수량이 바뀐 종목은 없습니다.</div>'
    cards = "".join(
        f'<div style="border:1px solid {HAIRLINE};border-radius:14px;padding:10px 14px;margin-top:6px">'
        f'<div style="font-size:14px;font-weight:600;line-height:1.5;color:{INK}">{_pill(QUANTITY_CHANGES[c["change"]])} '
        f'{escape(c["name"])}</div><div style="font-size:14px;line-height:1.6;color:{BODY};margin-top:4px">'
        f'<span style="font-family:{MONO}">{_qty(c["quantity_start"])} → {_qty(c["quantity_end"])}주</span> · '
        f'{"금액 미상" if c["amount"] is None else _toned(c["amount"])}</div></div>' for c in changes)
    return title + cards


def _quantity_changes(data: dict[str, Any]) -> str:
    """The holdings whose quantity changed since the comparison report (report design §32): the client's own
    as a table, Hermes-managed ones as a count, and the holdings that could not be compared (an unknown quantity is
    not "no change"). Nothing without a comparison report."""
    found, baseline = data.get("holding_quantity_changes"), data.get("baseline")
    if found is None or baseline is None:
        return ""
    since = f"{baseline.month}월 {baseline.day}일"
    mine = [c for c in found["changes"] if not c["hermes"]]
    hermes, uncompared = len(found["changes"]) - len(mine), found["uncompared"]
    title = f'<div style="font-size:12px;font-weight:600;color:{MUTED};margin:16px 0 4px">{since} 이후 수량이 바뀐 종목</div>'
    if not mine:
        scope = "수량을 비교할 수 있는 종목 가운데 " if uncompared else ""
        body = f'<div style="font-size:13px;line-height:1.6;color:{BODY}">{scope}{since} 이후 수량이 바뀐 종목은 없습니다.</div>'
    else:
        body = _table(["종목", "수량", "변화", "평가액 변화분"],
                      [[c["name"], f"{_qty(c['quantity_start'])} → {_qty(c['quantity_end'])}",
                        QUANTITY_CHANGES[c["change"]], "미상" if c["amount"] is None else _signed_short(c["amount"])]
                       for c in mine])
        body += (f'<div style="font-size:12px;line-height:1.6;color:{MUTED};margin-top:6px">원장에 기록된 수량 기준입니다. '
                 "직접 사고판 것 외에 주식 보상 입고, 액면분할, 처음 등록한 계좌도 여기에 잡힙니다. 평가액 변화분은 바뀐 수량을 "
                 "지금 가격·환율로 환산한 값입니다(전량 정리는 비교 시점 가격).</div>")
    if hermes:
        body += (f'<div style="font-size:12px;line-height:1.6;color:{MUTED};margin-top:4px">Hermes 운용분은 {hermes}개 종목의 '
                 "수량이 바뀌었습니다(Hermes 몫이라 따로 보지 않습니다).</div>")
    if uncompared:
        body += (f'<div style="font-size:12px;line-height:1.6;color:{MUTED};margin-top:4px">수량 기록이 없어 비교하지 못한 종목 '
                 f"{len(uncompared)}개: {escape(', '.join(str(n) for n in uncompared))}.</div>")
    return title + body


def _qty(quantity: Decimal | None) -> str:
    """A share count without trailing zeros; fractional US shares read to four places (28.582791 -> 28.5828), which
    is enough to see the change in the mail. Unknown reads 미상."""
    if quantity is None:
        return "미상"
    places = Decimal("0.0001")
    if quantity != quantity.quantize(places):
        quantity = quantity.quantize(places, rounding=ROUND_HALF_UP)
    return f"{quantity.normalize():,f}"


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


def _return_cell(ratio: Decimal | None) -> Raw:
    """The return as a table cell in the chart's diverging colours (user request 2026-10-27): a gain in the
    rising colour, a loss in the falling one, flat or unknown in grey."""
    color = MUTED if ratio is None or ratio == 0 else UP if ratio > 0 else DOWN
    weight = "400" if ratio is None else "600"
    return Raw(f'<span style="font-family:{MONO};font-weight:{weight};color:{color}">{escape(_return(ratio))}</span>')


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
                 ".")
    elif pr["exempt"] is not None and pr["tax_known"]:
        lead += f" 각 집에 1세대 1주택 비과세가 적용되면 약 {_short(pr['exempt'])}까지 늘어납니다."
    for h in plan["homes"]:
        if h["tax"] is not None and h["tax_case"] == "exempt":
            lead += f" {h['label']}의 양도세는 비과세 기준 약 {_short(h['tax']['exempt'])}(요건 미충족 시 약 {_short(h['tax']['general'])})입니다."
        elif h["tax"] is not None:
            lead += (f" {h['label']}의 양도세는 일반 과세 약 {_short(h['tax']['general'])}, 비과세 요건 충족 시 약 "
                     f"{_short(h['tax']['exempt'])}입니다.")
    # The plan is the household's, on the client's pay (§42): the spouse's letter says whose pay it is.
    whose = "배우자분 " if plan.get("pay_owner") not in (None, "self") else ""
    if pay["base"] is not None:
        lead += (f" {'배우자분이 ' if whose else ''}그때까지 받을 성과급·주식 보상은 확정·예상분 세후 약 <b>{_short(pay['base'])}</b>(확정분만 "
                 f"{_short(pay['adverse'])})이며, 지급 전까지는 확정이 아닙니다.")
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
        pays = [f"{whose}예정 소득 반영 시 {_short(nearest['spread_with_pay'])}" if nearest["spread_with_pay"] is not None else "",
                f"확정분만 반영 시 {_short(nearest['spread_with_pay_adverse'])}" if nearest["spread_with_pay_adverse"] is not None else ""]
        pays = ", ".join(p for p in pays if p)
        lead = (f"필요 추가 자금이 가장 작은 곳은 <b>{escape(nearest['alias'])} · {escape(nearest['label'])}</b>으로 약 "
                f"<b>{_short(nearest['spread'])}</b>" + (f"({pays})" if pays else "")
                + (", 세전 기준으로 6개월 전보다 차이가 " + trends[nearest["trend"]] + "입니다." if nearest["trend"] else "입니다.")
                + " " + lead)
    lead += " 핵심 변수는 성과급·자사주 유입과 그 주가, 그리고 매도 순서에 따른 양도세입니다."
    table = _table(["후보 단지", "시세(기준)", "6개월 전 대비", "필요 추가 자금", f"{whose}예정 소득 반영 후", "추이(세전)"], rows)
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
    if ratio is None:
        return "-"
    shown = (ratio * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return f"{'+' if shown > 0 else ''}{abs(shown) if not shown else shown}%"


def _pension_stack(data: dict[str, Any]) -> str:
    """The pension stack (report design §18.5): every pipeline's after-tax monthly payout against the monthly
    retirement target, the readiness ratio, and what is still missing. The tax day's opening view. Only the
    point of each part is in the card; how it was computed goes to the letter's end (`pension_notes`, user review
    2026-11-07)."""
    stack = data.get("pension_stack")
    if stack is None:
        return _section("노후 현금흐름 준비율", "은퇴 후 월 생활비 목표(지금 물가 기준)를 알려 주시면 연금을 모두 합쳐 준비율을 보여 드립니다.", "")
    target, head = stack["target"], stack["retirement_age"]
    tiers = _retirement_tiers(stack)
    # The two sentences the card keeps (report design §21): which ages, then each age's after-tax total and ratio.
    shared = len(stack.get("owners") or []) > 1
    lead = ((f"{head}세 은퇴를 기준으로 계산했고 50·55·60세도 함께 보입니다." if stack["retirement_age_set"] else
             f"은퇴 나이를 정하지 않으셔서 {'·'.join(str(t['retire_age']) for t in tiers)}세 세 경우로 계산했고, 아래 그래프와 표는 "
             f"{head}세 기준입니다.")
            # The household stack (user decision 2026-11-08): both members' pensions against one target.
            + (" 배우자의 연금도 같은 나이에 함께 은퇴한다고 보고 더했습니다." if shared else "")
            + f" 은퇴 후 {'가구 ' if shared else ''}월 <b>{_short(target)}</b>(세후, 현재 가치) 목표에 모든 연금이 나온 뒤의 세후 월 합계는 "
            + ", ".join(f"{t['retire_age']}세 은퇴 <b>{_short(t['total_net'])}</b>({_pct(t['ratio'], 0)})" for t in tiers)
            + "입니다.")
    if stack["later_net"] is not None:
        lead += (f" 기간을 정해 받는 연금이 끝나는 {stack['later_age']}세부터는 세후 월 <b>{_short(stack['later_net'])}</b>"
                 f"({_pct(stack['later_ratio'], 0)})로 줄어듭니다.")
    if stack["unknown"]:
        missing = [p for p in stack["pipelines"] if p["net_monthly"] is None]
        lead += (f" {', '.join(pension.row_label(p) for p in missing)}은 아직 수령액을 몰라 뺀 하한이며, 답장으로 알려 주시면 바로 "
                 "반영합니다: " + " ".join(f"{pension.row_label(p)}는 {p['missing']}." for p in missing if p["missing"]))
    head_tier = next((t for t in tiers if t["retire_age"] == head), None)
    inner = (_tier_table(tiers, None if head_tier is None else pension.need_words(head_tier, target)) + _gauge(f"{head}세 은퇴 기준 목표 월 생활비 대비 준비율", stack["ratio"], "")
             + _pension_timeline(stack) + _family_table(stack))
    return _section("노후 현금흐름 준비율: 연금 종류별 스택", lead, inner,
                    note=escape("준비율 " + " · ".join(f"{t['retire_age']}세 은퇴 {_pct(t['ratio'], 0)}" for t in tiers)))


# One hue per pension family and lighter shades for the products in it (user request 2026-11-07), so the table,
# the composition bar and the age chart read the same.
_FAMILY_SHADES = {"public": ("#0052ff", "#6b9bff"), "occupational": ("#0a8f5c", "#5cbf94", "#9ad9bc"),
                  "personal": ("#7c3aed", "#a78bfa", "#c4b5fd"), "tax_free": ("#b86e00", "#e0a64d"),
                  "property": ("#c43d7a", "#e589b3")}
# The card's short notes: larger and in body ink, not grey (user review 2026-11-07: grey was hard to read).
_NOTE = f"font-size:14px;line-height:1.6;color:{BODY};margin:6px 0 14px"


def _families(stack: dict[str, Any]) -> list[tuple[str, str, list[dict[str, Any]]]]:
    """(family key, label, its pipelines) for all five families, held or not, in the fixed order."""
    return [(key, label, [p for p in stack["pipelines"] if p["category"] == key]) for key, label in pension.CATEGORIES]


def _pipeline_colors(stack: dict[str, Any]) -> dict[str, str]:
    """A shade per pipeline row (by its id: a member's row of the same pipeline is its own, lighter shade)."""
    colors = {}
    for key, _, members in _families(stack):
        shades = _FAMILY_SHADES[key]
        for i, p in enumerate(members):
            colors[p["id"]] = shades[min(i, len(shades) - 1)]
    return colors


def _sub_label(p: dict[str, Any]) -> str:
    """The product-level name inside its family (the family is said by the colour and the family row), with the
    member when the stack holds more than one ("IRP(개인형 퇴직연금)(배우자)")."""
    if p["key"] == "occupational":
        name = p["label"].removeprefix("퇴직연금(").removesuffix(")")
    else:
        name = {"irp": "IRP(개인형 퇴직연금)", "insurance_annuity": "변액연금보험"}.get(p["key"], p["label"])
    return pension.row_label(dict(p, label=name))


def _period(p: dict[str, Any]) -> str:
    """The payout period in the member's own ages (a spouse's row is shifted onto the client's axis elsewhere)."""
    start_age, end_age = p.get("own_start_age", p["start_age"]), p.get("own_end_age", p.get("end_age"))
    if start_age is None:
        return "-"
    start = f"{p['start_date']:%Y.%m} {start_age}세" if p.get("start_date") else f"{start_age}세"
    return f"{start}~{end_age - 1}세" if end_age else f"{start}~종신"


def _product_text(p: dict[str, Any]) -> str:
    """The client's products behind a pipeline by name only (provider and product; user review 2026-11-07: the
    dates and the portal's illustration were too much for the table)."""
    names = []
    for item in p.get("products") or []:
        provider, title = item.get("institution"), item.get("name")
        names.append(" ".join(x for x in (None if provider and title and title.startswith(provider) else provider, title)
                              if x))
    if not any(names) and (p["key"] == "isa" or "계좌가 아직 없어" in p.get("basis_text", "")):
        return "아직 없음(Smith 가정)"
    return ", ".join(n for n in names if n) or "-"


def _family_table(stack: dict[str, Any], colors: dict[str, str] | None = None) -> str:
    """Every pipeline under its family (공적·퇴직·개인·비과세·부동산 연금) with the client's product, the payout
    period and the after-tax monthly figure; a family the household does not hold is one line naming its products.
    `colors` keeps the household's shades when the table shows one reader's pipelines."""
    colors = colors or _pipeline_colors(stack)
    rows = []
    for key, label, members in _families(stack):
        if not members:
            rows.append([Raw(f'<span style="color:{_FAMILY_SHADES[key][0]}">●</span> <b>{escape(label)}</b>'),
                         f"가입 없음({pension.CATEGORY_EXAMPLES[key]})", "-", "-", "-"])
            continue
        for i, p in enumerate(members):
            family = f"<b>{escape(label)}</b> › " if i == 0 else "└ "
            basis = (("입력값" if p["basis_text"].startswith("알려 주신") else "추정") if p["net_monthly"] is not None
                     else f"입력 필요: {p['missing']}")
            rows.append([Raw(f'<span style="color:{colors[p["id"]]}">●</span> {family}{escape(_sub_label(p))}'),
                         _product_text(p), _period(p), _short(p["net_monthly"]), basis])
    return _table(["분류", "가입 상품", "수령 기간", "월 세후", "근거"], rows)


def _pension_timeline(stack: dict[str, Any]) -> str:
    """The public pension portal's chart in Smith's figures (user request 2026-11-07) for the whole household: the
    composition at the age every pension has started, then the yearly payout by age; empty without a payout."""
    head = next(t for t in stack["tiers"] if t["retire_age"] == stack["retirement_age"])
    shared = len(stack.get("owners") or []) > 1
    return _pension_charts(stack["pipelines"], stack.get("timeline") or [], _pipeline_colors(stack),
                           retire_age=stack["retirement_age"], focus_age=head["all_on_age"], drop_age=head["later_age"],
                           whose="두 분 합산, " if shared else "")


def _pension_charts(pipelines: list[dict[str, Any]], rows: list[dict[str, Any]], colors: dict[str, str], *,
                    retire_age: int, focus_age: int | None, drop_age: int | None, whose: str) -> str:
    """A donut of the pipelines at `focus_age` (every pension started), one stacked bar per age with each pipeline's
    yearly after-tax payout in its family's shade (smith.pension_chart PNGs), a legend by family and one line on where
    the payouts drop at `drop_age`. Drawn for the household and for the reader's own pensions (user request
    2026-11-08: "개인용, 가구용 2개로"), with the same colour per pipeline in both; empty without a payout."""
    peak = max((r["total"] for r in rows), default=Decimal(0))
    if not peak:
        return ""
    families = _families({"pipelines": pipelines})
    order = [p for _, _, members in families for p in members if p["id"] in rows[0]["parts"]]
    focus = next((r for r in rows if r["age"] == focus_age), max(rows, key=lambda r: r["total"]))
    title = (f'<div style="font-size:15px;font-weight:600;color:{INK};margin:18px 0 8px">'
             f'{focus["age"]}세 예상 연금 수령액({whose}세후, 오늘 돈 가치): 연 {_short(focus["total"])} · 월 '
             f'{_short(focus["total"] / 12)}</div>')
    shown = [p for p in order if focus["parts"][p["id"]] > 0]
    # PNGs drawn with Pillow (mail clients drop SVG): a data URI here, an inline attachment once sent.
    ring = pension_chart.donut([(_chart_label(p), focus["parts"][p["id"]], colors[p["id"]]) for p in shown],
                               center=f"월 {_short(focus['total'] / 12)}", caption=f"{focus['age']}세 세후")
    bars = pension_chart.age_bars(rows, [(p["id"], colors[p["id"]]) for p in order], retire_age=retire_age,
                                  focus_age=focus["age"])
    chart = "".join(f'<div style="margin:10px 0 4px"><img src="data:image/png;base64,{base64.b64encode(png).decode("ascii")}" '
                    f'width="{pension_chart.WIDTH}" alt="{escape(alt)}" style="display:block;width:100%;'
                    f'max-width:{pension_chart.WIDTH}px;height:auto"></div>'
                    for png, alt in ((ring, f"{focus['age']}세 연금 구성"), (bars, "연령별 연간 연금 수령액")))
    legend = " ".join(
        f'<span style="white-space:nowrap;margin-right:12px"><b>{escape(label)}</b> '
        + " ".join(f'<span style="color:{colors[p["id"]]}">●</span> {escape(_chart_label(p))}' for p in members
                   if p["id"] in rows[0]["parts"]) + "</span>"
        for _, label, members in families if any(p["id"] in rows[0]["parts"] for p in members))
    # The one thing the bars show: where the yearly income drops when the fixed-term pensions end.
    by_age = {r["age"]: r for r in rows}
    ending = [_chart_label(p) for p in order if p.get("end_age") == drop_age]
    note = (f"{retire_age}세 은퇴 기준, 세후, 오늘 돈 가치입니다."
            + (f" {drop_age}세에 {'·'.join(ending)}이 끝나 연 {_short(by_age[drop_age - 1]['total'])}에서 "
               f"{_short(by_age[drop_age]['total'])}로 줄어듭니다."
               if drop_age in by_age and drop_age - 1 in by_age and ending else ""))
    return (title + chart + f'<div style="font-size:13px;color:{INK};margin-top:6px;line-height:1.9">{legend}</div>'
            + f'<div style="{_NOTE}">{escape(note)}</div>')


def _chart_label(p: dict[str, Any]) -> str:
    """A pipeline's short name for a chart legend ("IRP(배우자)"): the table carries the full product names."""
    short = {"irp": "IRP", "insurance_annuity": "변액연금보험", "isa": "ISA 등"}.get(p["key"])
    return _sub_label(p) if short is None else pension.row_label(dict(p, label=short))


def _retirement_tiers(stack: dict[str, Any]) -> list[dict[str, Any]]:
    """The retirement ages still ahead of the client (report design §30)."""
    return [t for t in stack["tiers"] if stack["age"] is None or t["retire_age"] > stack["age"]]


def _tier_table(tiers: list[dict[str, Any]], words: str | None = None) -> str:
    """One row per retirement age: the years before the first pension, the after-tax total once every pension
    has started (and after the fixed-term ones end), the ratio, and what must be held outside the pensions at
    retirement (today's won), with a note saying what that last figure is: the headline age spelled out in the
    client's wording (`words`, pension.need_words; mail of 2026-11-09) when given."""
    later = any(t["later_net"] is not None for t in tiers)
    rows = []
    for t in tiers:
        gap = t["years_without_pension"]
        need = t["outside_need"]
        bound = need is not None and need["upper_bound"]
        more = " 이상" if t["unknown"] else ""
        row = [f"{t['retire_age']}세", "-" if gap is None else f"{gap}년({t['first_income_age']}세까지)" if gap else "없음",
               _short(t["total_net"]) + more, _pct(t["ratio"], 0) + more]
        if later:
            row.append("-" if t["later_net"] is None else f"{_short(t['later_net'])}{more}({t['later_age']}세~)")
        rows.append(row + ["미상" if need is None else ("최대 " if bound else "") + _short(need["total"])])
    note = words or "연금 밖에서 준비할 돈은 연금만으로 모자라는 생활비를 메우려고 은퇴할 때 갖고 있어야 할 돈(오늘 돈 가치)입니다."
    if any(t["unknown"] for t in tiers) and not words:
        note += " 수령액을 모르는 연금이 있어 실제보다 크게 잡힌 최대치입니다."
    headers = ["은퇴 나이", "연금 없는 기간", "모든 연금 개시 후 세후 월", "준비율"] + (["기간 연금 끝난 뒤 세후 월"] if later else [])
    return _table(headers + ["연금 밖에서 준비할 돈"], rows) + f'<div style="{_NOTE}">{escape(note)}</div>'


def pension_notes(stack: dict[str, Any] | None) -> list[str]:
    """How the pension card was computed, for the letter's end (user review 2026-11-07: assumptions and fine detail
    leave the card): the meaning of each column, the payout and tax assumptions, the families, the contribution
    split, and why the public portal's chart differs."""
    if stack is None:
        return []
    notes = ["연금 없는 기간은 은퇴부터 첫 연금(연금 계좌는 55세부터, 국민연금은 65세)까지입니다. 연금 밖에서 준비할 돈은 은퇴부터 "
             f"{pension.PLAN_AGE}세가 될 때까지 해마다 연금으로 모자라는 생활비를 은퇴 시점의 오늘 돈 가치로 합한 것이며, "
             f"{pension.PLAN_AGE}세 이후 몫과 부동산은 넣지 않았습니다."]
    if len(stack.get("owners") or []) > 1:
        notes.append("두 분의 연금을 합쳤습니다. 배우자도 같은 나이에 함께 은퇴한다고 보았고, 목표 월 생활비는 가구 기준 세후 금액입니다. "
                     "연금소득세와 사적연금 연 1,500만 원 기준은 사람마다 따로 적용했습니다. 배우자 연금 계좌에 월 납입 기록이 없으면 지금 "
                     "잔액만 불린 값입니다."
                     + (" 표와 그래프의 나이는 고객님 나이이며, 배우자 연금은 같은 해에 맞춰 옮겨 놓았습니다(수령 기간 칸은 배우자 본인 나이)."
                        if any(p.get("age_offset") for p in stack["pipelines"]) else ""))
    if any(p["key"] == "isa" for p in stack["pipelines"]):
        notes.append("ISA 등 줄은 세액공제 한도를 넘는 납입을 주택 자금에 쓰지 않고 노후까지 남긴다는 Smith의 가정입니다.")
    notes.append(f"연금저축·IRP·퇴직금은 계좌에 정한 수령 기간(정하지 않았으면 정부 '내연금'과 같은 {pension.ACCOUNT_PAYOUT_YEARS}년) "
                 "동안 나눠 받는다고 보아 그 뒤에는 그래프에서 빠집니다. 국민연금은 물가만큼 오르므로 오늘 돈 가치로 평평합니다.")
    for p in stack["pipelines"]:
        if p.get("nominal_monthly") is not None:
            notes.append(f"{_sub_label(p)}은 금액이 정해진 연금이라(약관상 연금기준금액 × 고정 지급률) 물가만큼 오르지 않습니다. "
                         f"{p.get('start_year', '')}년 금액 월 {_short(p['nominal_monthly'])}은 오늘 돈 가치로 월 "
                         f"{_short(p['net_monthly'])}이고, 받는 동안 해마다 물가만큼 가치가 줄어듭니다.")
    notes.append("분류: 공적연금(국민연금), 퇴직연금(DB형·DC형·IRP), 개인연금(연금저축, 공제 밖 납입을 모으는 ISA 등), 비과세 "
                 "연금(10년 이상 유지한 연금보험), 부동산 연금(주택연금·농지연금). IRP는 연금저축과 세액공제 한도를 함께 쓰지만 법으로는 "
                 "퇴직연금이라 퇴직연금에만 넣었습니다.")
    c = stack["contributions"]
    if c["changed"] and stack["current_split"] is not None:
        now = c["current"]
        notes.append(f"앞으로의 납입은 세액공제를 빠짐없이 받는 연금저축 월 {_short(c['pension_savings'])}, IRP 월 {_short(c['irp'])}"
                     + (f", 공제 밖 월 {_short(c['isa'])}은 ISA 등" if c["isa"] else "") + "으로 나눈다고 계산했습니다. 지금처럼 "
                     f"연금저축 월 {_short(now['pension_savings'])}, IRP 월 {_short(now['irp'])}을 계속 넣으면 "
                     f"{stack['retirement_age']}세 은퇴 기준 세후 합계는 약 {_short(stack['current_split']['total_net'])}입니다.")
    notes.append("세후는 국민연금 약 3%, 퇴직금으로 받는 연금 약 3.5%(퇴직소득세 30% 감면), 연금저축·IRP 5.5%(한 해 1,500만 원을 넘는 "
                 "해는 16.5%, 세액공제를 받지 않은 원금은 비과세), 연금보험 비과세를 가정했습니다. 중앙노후준비지원센터 '내연금' "
                 "그래프는 세전이고 그해 금액이라 국민연금이 해마다 높아져 보입니다.")
    return notes


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
                     (f"{r['spec'].provider.upper()} {r['observed_on']:%m.%d}", r["spec"].link)] for r in rows
                    if r["spec"].unit != "pt"])  # Stock indices are the markets panel's benchmark (§38), not rates.
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


def _footer(warnings: list[str] | tuple[str, ...] = (), week: str = "", corrections: str = "",
            notes: list[str] | tuple[str, ...] = (), law: bool = True, shared: bool = False, reader: str = "self",
            doubtful: list[str] | tuple[str, ...] = ()) -> str:
    """One line (report design §18.1): what Smith never does, and that the reasons are a reply away. A problem
    with the report itself (stale data, no narrative) adds one warning line above it; a daily edition adds the
    week's plan (`week`, HTML) so a reader of one day knows where the other areas are (§27); `corrections` is the
    Korean check's one line, printed so it can be read against the report itself (§34, user request 2026-10-27);
    `notes` are the day's calculation notes moved out of its cards (user review 2026-11-07); `law` false leaves the tax
    caveat out of a mail that computes no tax (a filing note without a dividend); `shared` says the mail also went to
    the configured family address and that only the client's own replies are answered (FR-51); `doubtful` names the
    parts of the spouse's letter kept with a verification caveat (user decision 2026-11-08: at the letter's end, not
    under each paragraph). The footer reads in body ink at 13px: grey small print was hard to read."""
    warn = ("" if not warnings else
            f'<div style="font-size:13px;line-height:1.6;color:{INK};margin-bottom:8px">▲ 확인 필요: '
            f'{escape(" · ".join(warnings))}. 자세한 사유는 이 메일에 답장으로 물어 주십시오.</div>')
    if doubtful:
        warn += (f'<div style="font-size:13px;line-height:1.6;color:{INK};margin-bottom:8px">▲ 자동 점검: '
                 f'{escape(", ".join(doubtful))}에는 출처나 수치를 다 확인하지 못한 내용이 있습니다. 실행 전에 한 번 더 '
                 "확인하시길 권합니다.</div>")
    small = f"font-size:13px;line-height:1.7;color:{BODY}"
    basis = ("" if not notes else
             f'<div style="{small};margin-bottom:12px"><div style="font-weight:600;margin-bottom:4px">계산 기준</div>'
             + "".join(f'<div style="margin:0 0 4px">· {escape(n)}</div>' for n in notes) + "</div>")
    guide = "" if not week else f'<div style="{small};margin-bottom:6px">{week}</div>'
    checked = "" if not corrections else f'<div style="{small};margin-bottom:6px">{escape(corrections)}</div>'
    caveat = f"세금·법령은 {tax.RULES_YEAR}년 기준으로 계산했으니 실행 전 현행 법령을 확인하십시오. " if law else ""
    family = " 이 메일은 가족 주소로도 함께 보냅니다(답장 질문은 고객님 메일에서 보내신 것에만 답합니다)." if shared else ""
    # The spouse's own letter (§42): replies to it are not answered yet, so it does not invite them.
    ask = ("이 레터의 답장 질문에는 아직 답하지 않습니다." if reader != "self" else "판단 근거는 답장으로 물어 주십시오.")
    return (f'<tr><td>{warn}{basis}{guide}{checked}<div style="{small}">Smith는 조회 전용 자문입니다(매매·이체·대출 '
            f'실행 없음). {caveat}{ask}{family}</div></td></tr>')  # The one law caveat (user decision 2026-11-02).


def _corrections_line(data: dict[str, Any]) -> str:
    """The Korean check's summary for the footer, "" when the report went out without a narrative."""
    return korean.summary(((data.get("narrative") or {}).get("status") or {}).get("korean")) or ""


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
        corrections = korean.summary(ai.get("korean"))  # What the Korean check did to this report (report design §34).
        if corrections:
            lines.append(corrections)
    else:
        lines.append(f"AI 해석: 이번에는 실패해({ai.get('error_code', '원인 미상')}) 계산 결과만으로 작성했습니다.")
    return lines


def _source(source: str) -> str:
    return "수동 입력" if source.startswith("manual") else SOURCE_LABELS.get(source, source)


def _dec(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _delta(value: Decimal | None) -> str:
    return "-" if value is None else f"{value:+}"
