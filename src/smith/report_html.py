"""Email-safe HTML rendering of report data (docs/report-design.md §4, §10.2, §11).

Gmail runs no scripts, so every chart is an inline-styled HTML table: stat tiles, emphasis bars
(one accent, the rest neutral), a zero-centred diverging bar for changes and a timeline. Each chart
has a plain table twin. State is always icon + label + color.

Visual language follows docs/design-reference.md (style only, no brand marks): white canvas, one
blue accent used scarcely, display type at weight 400 with negative tracking, tabular monospace
numbers, 24px card radius, pill badges, and a light/dark band rotation: dark hero (date, key numbers,
the one most important message) -> white proposals -> dark strategy -> white sections -> soft footer.
Chart colors were checked with the dataviz validator on white: increases red and decreases blue
(the Korean market convention), always with an arrow and a sign.
"""
from decimal import Decimal
from html import escape
from typing import Any

from smith.fmt import short_won as _short
from smith.proposals import Proposal, Track
from smith.realestate import MIN_BASIS
from smith.report_data import CATEGORY_LABELS, COMPONENT_LABELS

# Design tokens (docs/design-reference.md).
CANVAS, SOFT, STRONG, DARK, DARK_RAISED = "#ffffff", "#f7f7f7", "#eef0f3", "#0a0b0d", "#16181c"
BLUE, HAIRLINE = "#0052ff", "#dee1e6"
INK, BODY, MUTED, MUTED_SOFT, ON_DARK, ON_DARK_SOFT = "#0a0b0d", "#5b616e", "#7c828a", "#a8acb3", "#ffffff", "#a8acb3"
SANS = "Inter,Pretendard,'Malgun Gothic','Apple SD Gothic Neo',-apple-system,'Segoe UI',Roboto,sans-serif"
# Numbers are tabular; Hangul units fall back to the system font.
MONO = "'JetBrains Mono','Geist Mono',Consolas,'Malgun Gothic','Apple SD Gothic Neo',monospace"
# Chart colors (validated): accent and the neutral rest; diverging poles.
ACCENT, MUTED_BAR, UP, DOWN = BLUE, "#a8acb3", "#cf202f", BLUE
STATUS = {"stable": ("●", "안정", "#05b169"), "watch": ("▲", "주의", "#f4b000"), "act": ("■", "대응 필요", "#ff5c66")}
TRACK_STATUS = {"on_track": ("●", "궤도 위", "#05b169"), "attention": ("▲", "보완 필요", "#f4b000"),
                "unknown": ("○", "판단 불가", ON_DARK_SOFT)}
PRIORITY_LABELS = {1: "지금 실행", 2: "이번 달·분기", 3: "유지·점검"}
SERIES_LABELS = {
    "722Y001/0101000": "한국은행 기준금리", "817Y002/010200000": "국고채 3년", "817Y002/010210000": "국고채 10년",
    "817Y002/010502000": "CD 91일", "731Y001/0000001": "원/달러 환율", "DFEDTARU": "미국 기준금리(상단)",
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
TERMS = {
    "순자산": "가진 것(자산)에서 갚아야 할 것(부채)을 뺀 금액입니다.",
    "유동성": "필요할 때 얼마나 빨리, 손해 없이 현금으로 바꿀 수 있는지를 뜻합니다.",
    "변동금리": "몇 달마다 시장 금리에 맞춰 이자율이 다시 정해지는 대출 방식입니다.",
    "CD 91일물": "은행이 91일 만기로 발행하는 예금증서의 금리로, 단기 시장 금리와 변동금리 대출의 기준으로 자주 쓰입니다.",
    "LTV": "집값 대비 대출 비율입니다. 50%라면 집값의 절반을 빌렸다는 뜻입니다.",
    "역전세": "새로 받을 전세보증금이 돌려줘야 할 기존 보증금보다 적어, 차액을 집주인이 마련해야 하는 상황입니다.",
    "세액공제": "낼 세금에서 일정 금액을 직접 빼 주는 제도입니다. 연말정산 때 그만큼 돌려받습니다.",
    "환율 효과": "외화 자산의 가치가 환율 변화만으로 원화 기준에서 늘거나 줄어든 부분입니다.",
    "미실현 손익": "아직 팔지 않아 확정되지 않은, 장부상의 이익 또는 손실입니다.",
    "%p": "퍼센트포인트. 3%에서 4%로 오르면 1%p 오른 것입니다.",
}


def render(data: dict[str, Any]) -> tuple[str, str]:
    """Return (subject, html). Monday is the full edition; Thursday shows proposals, changes and schedule."""
    status = _status(data)
    full = data["kind"] != "thursday"
    advice = data.get("advice") or {"proposals": [], "strategy": [], "assumptions": []}
    story = data.get("narrative") or {}
    bands = [_band(DARK, _header(data, status) + _judgement(story) + _overview(data) + _callout(advice["proposals"])),
             _band(CANVAS, _proposals(advice["proposals"], full), border=True)]
    if full and advice["strategy"]:
        bands.append(_band(DARK, _strategy(advice["strategy"], story.get("direction", ""), story.get("direction_memo", ""))))
    sections = [_changes_outside(story), _follow_up(advice), _tax(advice, data, full), _change(data), _timeline(data)]
    if full:
        sectors = data["sectors"]
        sections += [_allocation(data), _cash(sectors["cash"], data), _debt(sectors["debt"]),
                     _securities(sectors["securities"]), _real_estate(sectors["real_estate"]),
                     _pension(sectors["pension_insurance"]), _macro(sectors["macro"])]
    sections.append(_glossary())
    bands.append(_band(CANVAS, "".join(sections), border=True))
    bands.append(_band(SOFT, _footer(data, advice["assumptions"], story.get("status"))))
    spacer = '<tr><td style="height:12px;line-height:12px;font-size:0">&nbsp;</td></tr>'
    html = (f'<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>Smith 보고서</title></head>'
            f'<body style="margin:0;padding:0;background:{CANVAS};font-family:{SANS};color:{INK};word-break:keep-all;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{CANVAS}">'
            f'<tr><td align="center" style="padding:12px 4px">'
            f'<table role="presentation" width="640" cellpadding="0" cellspacing="0" style="width:100%;max-width:640px">'
            f'{spacer.join(bands)}</table></td></tr></table></body></html>')
    return _subject(data, status), html


def _status(data: dict[str, Any]) -> str:
    failed_sync = any(s["last_failure"] and (not s["last_success"] or s["last_failure"] > s["last_success"])
                      for s in data["sync"])
    return "watch" if not data["completeness"]["complete"] or failed_sync or data["warnings"] else "stable"


def _subject(data: dict[str, Any], status: str) -> str:
    day = data["as_of"].strftime("%m/%d")  # as_of is already in the household timezone.
    edition = "목요 변화 점검" if data["kind"] == "thursday" else "월요 종합 보고"
    proposals = (data.get("advice") or {}).get("proposals") or []
    change = data["kpis"]["net_worth_change"]
    lead = f"제안 {len(proposals[:3])}건" if proposals else (
        "기준선 보고" if change is None else f"순자산 {_signed_short(change)}")
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


def _section(title: str, lead: str, inner: str) -> str:
    return (f'<tr><td style="padding:22px 0 6px;border-top:1px solid {HAIRLINE}">{_heading(title, size=22)}'
            f'<div style="font-size:15px;line-height:1.6;color:{BODY};margin-bottom:12px">{lead}</div>{inner}</td></tr>')


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
    return not isinstance(cell, tuple) and cell[:1] in set("0123456789+-−▲▼")


def _text_of(cell: str | tuple[str, str]) -> str:
    return cell[0] if isinstance(cell, tuple) else cell


def _cell(cell: str | tuple[str, str]) -> str:
    if not isinstance(cell, tuple):
        return escape(cell)
    text, url = cell
    # Only a whole, plain https URL becomes a link; anything else (spaces, controls, quotes, angle
    # brackets) stays text rather than being trimmed into a different URL.
    if not url.startswith("https://") or any(ch.isspace() or ord(ch) < 32 or ch in "\"'<>\x7f" for ch in url):
        return escape(text)
    return f'<a href="{escape(url, quote=True)}" style="color:{BLUE};text-decoration:none">{escape(text)}</a>'


# --- top bands --------------------------------------------------------------------------------------------

def _header(data: dict[str, Any], status: str) -> str:
    edition = "월요 종합 보고" if data["kind"] != "thursday" else "목요 변화 점검"
    icon, label, color = STATUS[status]
    reason = ("데이터가 모두 최신이며 누락이 없습니다." if status == "stable"
              else "일부 데이터가 불완전하거나 동기화에 실패했습니다. 맨 아래 '데이터 상태'를 확인하시기 바랍니다.")
    note = ""
    if data.get("delivery_note"):
        note = (f'<div style="font-size:13px;line-height:1.6;color:{ON_DARK};background:{DARK_RAISED};border-radius:16px;'
                f'padding:12px 14px;margin-top:14px">▲ {escape(data["delivery_note"])}</div>')
    return (f'<tr><td>{_eyebrow(f"SMITH · {edition}", color=ON_DARK, background=DARK_RAISED)}'
            f'<div style="font-family:{SANS};font-weight:400;font-size:30px;line-height:1.15;letter-spacing:-0.8px;'
            f'color:{ON_DARK};margin:4px 0 12px">{data["as_of"]:%Y년 %m월 %d일} 보고</div>'
            f'<div style="font-size:14px;color:{ON_DARK_SOFT}"><span style="color:{color};font-weight:600">{icon} {label}</span>'
            f' — {reason}</div>{note}</td></tr>')


def _overview(data: dict[str, Any]) -> str:
    k = data["kpis"]
    months = "미상" if k["immediate_months"] is None else f'월 지출 {k["immediate_months"]:.1f}개월분'
    home = k["home_progress"]
    tiles = [("순자산", _short(k["net_worth"]), "기준선" if k["net_worth_change"] is None else _signed_short(k["net_worth_change"])),
             ("월 여유 현금흐름", _short(k["monthly_net"]), "수입 − 지출"),
             ("즉시 쓸 수 있는 돈", _short(k["immediate"]), months),
             ("주택 목표 준비율(본인)", "미상" if home is None else _pct(home["ratio_self"], 0),
              "" if home is None else f'{home["target_date"]:%Y.%m} 기준 시나리오')]

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


def _callout(proposals: list[Proposal]) -> str:
    """The single most important message, once per report, closing the dark hero."""
    if proposals:
        top = proposals[0]
        title, why = top.title, _first_sentence(top.why)
    else:
        title = "이번 주에는 새로 실행하실 일이 없습니다. 지금의 전략을 유지하시는 것이 합리적입니다."
        why = "현금 여유, 대출 부담, 목표 일정 모두 기준 안에 있어 바꿀 이유가 확인되지 않았습니다."
    return (f'<tr><td style="padding-top:10px"><div style="background:{DARK_RAISED};border-radius:24px;padding:22px 20px">'
            f'{_eyebrow("이번 주 가장 중요한 한 가지", color=ON_DARK, background=BLUE)}'
            f'<div style="font-family:{SANS};font-weight:400;font-size:22px;line-height:1.35;letter-spacing:-0.4px;'
            f'color:{ON_DARK}">{escape(title)}</div>'
            f'<div style="font-size:14px;line-height:1.6;color:{ON_DARK_SOFT};margin-top:10px">{escape(why)}</div>'
            f'</div></td></tr>')


def _proposals(proposals: list[Proposal], full: bool) -> str:
    """The proposal cards: why now, effect, risks, timing, what would change the judgement, certainty."""
    head = f'<tr><td>{_eyebrow("이번 주 제안")}{_heading("고객님께 드리는 제안", size=28)}</td></tr>'
    if not proposals:
        return head + (f'<tr><td style="font-size:15px;line-height:1.6;color:{BODY}">새 제안이 없습니다. 제안이 없는 것도 '
                       '판단입니다. 다음 보고에서 변화가 있으면 다시 말씀드리겠습니다.</td></tr>')
    cards = []
    for number, p in enumerate(proposals[:3], 1):
        badge = (f'<span style="display:inline-block;background:{BLUE};color:#ffffff;font-size:12px;font-weight:600;'
                 f'border-radius:100px;padding:4px 11px;margin-right:6px">{number}</span>'
                 f'<span style="display:inline-block;background:{STRONG};color:{INK};font-size:12px;font-weight:600;'
                 f'border-radius:100px;padding:4px 12px">{PRIORITY_LABELS.get(p.priority, "")}</span>')
        if p.times_shown:
            badge += (f'<span style="display:inline-block;background:{STRONG};color:{MUTED};font-size:12px;font-weight:600;'
                      f'border-radius:100px;padding:4px 12px;margin-left:6px">지난 보고에 이어 {p.times_shown + 1}번째</span>')
        title = (f'<div style="font-size:18px;font-weight:600;line-height:1.4;color:{INK};margin:12px 0 8px">'
                 f'{escape(p.title)}</div>')
        if full:
            rows = [("왜 지금", escape(p.why))]
            if p.context:
                rows.append(("시장·정책 맥락", escape(p.context) + _sources(p.links)))
            rows += [("예상 효과", escape(p.effect)), ("위험과 대안", escape(p.risks)), ("시점", escape(p.timing)),
                     ("다시 판단할 조건", escape(p.reconsider)), ("근거의 성격", escape(p.certainty))]
            detail = "".join(f'<tr><td style="width:92px;vertical-align:top;font-size:12px;font-weight:600;color:{MUTED};'
                             f'padding:6px 8px 6px 0">{escape(k)}</td><td style="font-size:14px;line-height:1.6;color:{BODY};'
                             f'padding:6px 0">{v}</td></tr>' for k, v in rows)
            detail = f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{detail}</table>'
        else:
            detail = f'<div style="font-size:14px;line-height:1.6;color:{BODY}">{escape(_first_sentence(p.why))}</div>'
        cards.append(f'<tr><td style="padding-top:12px"><div style="background:{CANVAS};border:1px solid {HAIRLINE};'
                     f'border-radius:24px;padding:20px 20px">{badge}{title}{detail}</div></td></tr>')
    rest = proposals[3:]
    more = ""
    if rest:
        items = "".join(f'<div style="font-size:13px;line-height:1.6;color:{BODY}">· {escape(p.title)}</div>' for p in rest)
        more = (f'<tr><td style="padding-top:16px"><div style="font-size:12px;font-weight:600;color:{MUTED};'
                f'margin-bottom:4px">함께 검토한 다른 제안</div>{items}</td></tr>')
    if not full:
        more += (f'<tr><td style="padding-top:12px;font-size:12px;color:{MUTED}">목요 보고는 요약입니다. 근거와 위험은 월요 보고에서 '
                 '자세히 말씀드립니다.</td></tr>')
    return head + "".join(cards) + more


def _judgement(story: dict[str, Any]) -> str:
    """The narrative's bottom line, under the date and status in the dark hero."""
    if not story.get("situation"):
        return ""
    brief = (story.get("status") or {}).get("brief")
    basis = ""
    if brief:
        basis = f"{brief['created_at']:%m월 %d일 %H시} 조사 자료 기준" + (" (이번 보고에서 새로 조사하지 못해 다시 사용)"
                                                                     if brief.get("reused") else "")
        basis = f'<div style="font-size:12px;color:{ON_DARK_SOFT};margin-top:6px">{escape(basis)}</div>'
    links = "".join(f' · <a href="{escape(url, quote=True)}" style="color:{ON_DARK_SOFT}">{escape(title)}</a>'
                    for url, title in dict((u, t) for t, u in story.get("situation_links", [])).items()
                    if _plain_link(url))
    return (f'<tr><td style="padding-top:16px"><div style="font-size:12px;font-weight:600;color:{ON_DARK_SOFT};'
            f'margin-bottom:6px">Smith의 판단</div><div style="font-size:16px;line-height:1.65;color:{ON_DARK}">'
            f'{escape(story["situation"])}</div>{_memo(story.get("situation_memo"), dark=True)}{basis}'
            + (f'<div style="font-size:12px;color:{ON_DARK_SOFT};margin-top:4px">출처{links}</div>' if links else "")
            + '</td></tr>')


def _plain_link(url: str) -> bool:
    return url.startswith("https://") and not any(ch.isspace() or ord(ch) < 32 or ch in "\"'<>\x7f" for ch in url)


def _memo(memo: str | None, *, dark: bool = False) -> str:
    """A short caveat from code verification, shown under the block it concerns."""
    if not memo:
        return ""
    color = ON_DARK_SOFT if dark else MUTED
    return f'<div style="font-size:12px;color:{color};margin-top:4px">자동 점검 메모: {escape(memo)}</div>'


def _sources(links: Any) -> str:
    """Small source links after a sourced statement."""
    unique = {url: title for title, url in reversed(list(links))}  # One link per page, first title wins.
    items = [_cell((title, url)) for url, title in reversed(list(unique.items()))][:4]
    if not items:
        return ""
    return (f'<div style="font-size:12px;color:{MUTED};margin-top:4px">출처: ' + " · ".join(items) + "</div>")


def _changes_outside(story: dict[str, Any]) -> str:
    """Insights from the research brief that matter for this household, and what to watch next."""
    insights, watch = story.get("insights") or [], story.get("watch") or []
    if not insights and not watch:
        return ""
    blocks = []
    for item in insights:
        blocks.append(f'<div style="margin:6px 0 16px"><div style="font-size:16px;font-weight:600;color:{INK};'
                      f'margin-bottom:4px">{escape(item["title"])}</div>'
                      f'<div style="font-size:14px;line-height:1.6;color:{BODY}">{escape(item["body"])}</div>'
                      f'<div style="font-size:14px;line-height:1.6;color:{INK};margin-top:6px"><b style="font-weight:600">'
                      f'우리 집에 주는 의미</b> — {escape(item["implication"])}</div>{_memo(item.get("memo"))}'
                      f'{_sources(item["links"])}</div>')
    if watch:
        rows = "".join(f'<div style="font-size:14px;line-height:1.6;color:{BODY};margin:4px 0">· <b style="font-weight:600;'
                       f'color:{INK}">{escape(w["item"])}</b> — {escape(w["why"])}{_memo(w.get("memo"))}'
                       f'{_sources(w["links"])}</div>' for w in watch)
        blocks.append(f'<div style="font-size:12px;font-weight:600;color:{MUTED};margin:8px 0 4px">다음 보고까지 지켜볼 것</div>{rows}')
    lead = "이번 주 조사한 정책·시장 변화 가운데 고객님 자산과 직접 연결되는 것만 골랐습니다. 출처를 누르면 원문을 보실 수 있습니다."
    return _section("이번 주 눈여겨볼 변화", lead, "".join(blocks))


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
                         f'{escape(n.body)}</div><div style="font-size:12px;color:{MUTED};margin-top:4px">시점: '
                         f'{escape(n.when)} · {escape(n.certainty)}</div></div>' for n in notes)
    if year_end and rows:
        inner += (f'<div style="font-size:12px;font-weight:600;color:{MUTED};margin:8px 0 0">올해 연말정산 점검표</div>'
                  + _table(["항목", "올해 상황", "할 일"], [list(row) for row in rows]))
    lead = ("같은 돈이라도 어느 계좌에 두고 언제 실현하느냐에 따라 세후 결과가 달라집니다. 고객님 상황에 해당하는 세금 규칙과 "
            "그 의미를 정리했습니다. 세법은 자주 바뀌므로 실행 전 현행 기준을 확인하셔야 합니다.")
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


def _strategy(tracks: list[Track], direction: str = "", direction_memo: str = "") -> str:
    """Monday: whether each long-range goal is on track, as raised cards on the dark band."""
    rows = []
    for track in tracks:
        icon, label, color = TRACK_STATUS[track.status]
        rows.append(f'<tr><td style="padding-top:10px"><div style="background:{DARK_RAISED};border-radius:24px;padding:18px 20px">'
                    f'<div style="font-size:13px;font-weight:600;color:{ON_DARK}">{escape(track.name)} '
                    f'<span style="color:{color}">{icon} {label}</span></div>'
                    f'<div style="font-size:17px;font-weight:400;line-height:1.4;color:{ON_DARK};margin:6px 0">'
                    f'{escape(track.headline)}</div>'
                    f'<div style="font-size:13px;line-height:1.6;color:{ON_DARK_SOFT}">{escape(track.detail)}</div>'
                    f'</div></td></tr>')
    lead = (f'<div style="font-size:15px;line-height:1.65;color:{ON_DARK_SOFT};margin-bottom:6px">{escape(direction)}</div>'
            if direction else "") + _memo(direction_memo, dark=True)
    return (f'<tr><td>{_eyebrow("전략 방향", color=ON_DARK, background=DARK_RAISED)}'
            f'{_heading("목표까지 지금 궤도에 있습니까", color=ON_DARK, size=28)}{lead}'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{"".join(rows)}</table></td></tr>')


def _first_sentence(text: str) -> str:
    end = text.find(". ")  # Decimal points (3.61%) are never followed by a space.
    return text if end < 0 else text[:end + 1]


# --- sections ---------------------------------------------------------------------------------------------

def _change(data: dict[str, Any]) -> str:
    change = data["change"]
    if change is None:
        return _section("지난 보고 이후 변화", "첫 보고서입니다. 이번 수치를 앞으로 변화를 비교할 <b>기준선</b>으로 삼습니다.", "")
    parts = [(COMPONENT_LABELS[key], value) for key, value in change["parts"].items() if value]
    parts.sort(key=lambda item: -abs(item[1]))
    direction = "증가" if change["total"] > 0 else "감소" if change["total"] < 0 else "변동 없음"
    lead = (f"지난 보고 이후 <b>순자산</b>(가진 것 − 갚을 것)이 <b>{_short(abs(change['total']))} {direction}</b>했습니다."
            if change["total"] else "지난 보고 이후 순자산 변화가 없습니다.")
    if parts:
        lead += f" 가장 큰 요인은 <b>{escape(parts[0][0])}</b>입니다."
        lead += " 며칠 사이의 시세 등락은 흔한 일이며, 장기 목표 대비 흐름으로 판단하시기 바랍니다."
    if not change["complete"]:
        lead += " 환율이 없는 통화가 있어 분해가 일부 불완전합니다."
    rows = [[label, _won(value)] for label, value in parts] + [["합계", _won(change["total"])]]
    return _section("지난 보고 이후 변화", lead, (_diverging(parts) if parts else "") + _table(["요인", "금액"], rows))


def _timeline(data: dict[str, Any]) -> str:
    items = data["timeline"]
    if not items:
        return _section("다가오는 큰 일정", "향후 5년 안에 예정된 큰 자금 일정이 없습니다.", "")
    today = data["as_of"].date()
    rows = []
    for item in items:
        months = (item["date"].year - today.year) * 12 + item["date"].month - today.month
        rows.append(f'<tr><td style="width:100px;font-family:{MONO};font-size:12px;font-weight:500;color:{INK};padding:7px 8px 7px 0">'
                    f'{item["date"]:%Y.%m.%d}</td>'
                    f'<td style="font-size:13px;color:{BODY};padding:7px 0 7px 12px;border-left:3px solid {ACCENT}">'
                    f'{escape(item["label"])} · {_short(item["amount"])} <span style="color:{MUTED_SOFT}">({months}개월 후)</span></td></tr>')
    first = items[0]
    lead = (f"가장 가까운 큰 자금 일정은 <b>{first['date']:%Y년 %m월}</b>의 {escape(first['label'])}"
            f"(<b>{_short(first['amount'])}</b>)입니다. 이 일정들이 현금 준비의 기준이 됩니다.")
    home = data["kpis"]["home_progress"]
    inner = f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{"".join(rows)}</table>'
    if home:
        gap = home["required"] - home["available_self"]
        lead += (f" 주택 목표의 기준 시나리오에서 본인 자원으로 준비 가능한 금액은 필요액의 <b>{_pct(home['ratio_self'], 0)}</b>"
                 f"이며, 부족분은 약 <b>{_short(gap)}</b>입니다(가정: 집값 연 2%·투자 수익 연 4%).")
        bars = [("필요 자금(비용 포함)", home["required"], False), ("본인 준비 가능액", home["available_self"], True)]
        if home["available_with_partner"] is not None:
            bars.append(("배우자 부동산 포함 시", home["available_with_partner"], False))
        inner += '<div style="height:12px"></div>' + _bars(bars)
        inner += _table(["시나리오", "필요 자금", "본인 준비 가능액", "부족분(본인)"],
                        [[SCENARIO_LABELS.get(s["name"], s["name"]), _short(_dec(s["required_with_costs"])),
                          _short(_dec(s["available_self"])), _short(_dec(s["gap_self"]))] for s in home["scenarios"]])
    return _section("목표와 일정", lead, inner)


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
    bars = _bars([(CATEGORY_LABELS.get(k, k), v, i == 0) for i, (k, v) in enumerate(rows)])
    table = _table(["자산 유형", "금액", "비중"], [[CATEGORY_LABELS.get(k, k), _won(v), _pct(v / summary.total_assets)]
                                            for k, v in rows])
    return _section("자산 구성", lead, bars + table)


def _cash(cash: dict[str, Any], data: dict[str, Any]) -> str:
    months = data["kpis"]["immediate_months"]
    if cash["reserve_gap"] is None:
        # An amount without an FX rate makes the reserve check unknown rather than met or short.
        lead = ("즉시 활용 가능한 자금이나 월 지출 가운데 환율이 없어 원화로 바꾸지 못한 금액이 있어, "
                f"비상금 기준(월 지출 {cash['emergency_months_assumption']}개월) 충족 여부는 계산하지 않았습니다.")
    else:
        lead = (f"즉시 활용 가능한 자금은 <b>{_short(cash['immediate'])}</b>으로, 월 지출의 "
                f"<b>{'미상' if months is None else f'{months:.1f}개월'}</b>분입니다. "
                f"비상금 기준(월 지출 {cash['emergency_months_assumption']}개월, 가정)인 {_short(cash['reserve_target'])}"
                + (f"까지 <b>{_short(cash['reserve_gap'])}</b>이 부족합니다." if cash["reserve_gap"] else "을 충족합니다."))
    lead += " <b>유동성</b>(얼마나 빨리 현금으로 바꿀 수 있는지)별 분포는 아래와 같습니다."
    ladder = cash["ladder"]
    bars = _bars([(label, value, label == "즉시") for label, value in ladder])
    return _section("현금·유동성", lead, bars + _table(["현금화 속도", "금액"], [[l, _won(v)] for l, v in ladder]))


def _debt(debt: dict[str, Any]) -> str:
    one = next((s for s in debt["shocks"] if s["shock"] == Decimal("0.01")), None)
    if debt["variable_total"] is None:
        lead = ("<b>변동금리</b>(몇 달마다 시장 금리에 맞춰 이자율이 바뀌는 방식) 대출 가운데 환율이 없어 원화로 바꾸지 못한 "
                "대출이 있어, 합계와 금리 상승 시 이자 증가액을 계산하지 않았습니다.")
    else:
        lead = (f"<b>변동금리</b>(몇 달마다 시장 금리에 맞춰 이자율이 바뀌는 방식) 대출은 <b>{_short(debt['variable_total'])}</b>입니다. "
                + (f"금리가 1%p 오르면 월 이자가 약 <b>{_short(one['monthly_increase'])}</b>"
                   f"(연 {_short(one['monthly_increase'] * 12)}) 늘어납니다." if one and debt["variable_total"] else ""))
    bars = _bars([(f"+{s['shock'] * 100:.1f}%p", s["monthly_increase"], s["shock"] == Decimal("0.01"))
                  for s in debt["shocks"]])
    table = _table(["대출", "잔액", "금리", "방식", "만기"],
                   [[l["label"], _short(l["amount"]), f"{l['rate'] * 100:.2f}%", RATE_TYPE_LABELS.get(l["rate_type"], l["rate_type"]),
                     (l["maturity"] or "-")[:7].replace("-", ".")]
                    for l in debt["loans"]])
    return _section("부채·금리", lead, bars + table)


def _securities(sec: dict[str, Any]) -> str:
    positions = sec["positions"]
    if not positions:
        return _section("주식·증권", "종목 단위 정보가 있는 증권이 없습니다.", "")
    top = positions[0]
    lead = (f"주식·증권 {_short(sec['total'])} 가운데 가장 큰 종목({escape(str(top['name']))})이 <b>{_pct(top['share'])}</b>를 "
            f"차지합니다. 구성을 모르는 증권은 {_short(sec['unclassified'])}이며"
            + (f", 종목별 <b>미실현 손익</b>(팔지 않아 확정되지 않은 손익) 합계는 {_signed_short(sec['unrealized_gain'])}입니다."
               if sec["unrealized_gain"] is not None else "입니다."))
    if sec["unrealized_gain"] is not None and sec["unrealized_gain_missing"]:
        lead += (f" 이 손익은 매입 원가를 아는 {sec['unrealized_gain_counted']}개 종목만의 <b>부분 합계</b>이며, "
                 f"원가를 모르는 {sec['unrealized_gain_missing']}개 종목은 빠져 있습니다.")
    bars = _bars([(str(p["name"])[:14], p["value"], i < 3) for i, p in enumerate(positions)])
    table = _table(["종목", "평가액", "증권 내 비중"], [[str(p["name"]), _won(p["value"]), _pct(p["share"])] for p in positions])
    return _section("주식·증권", lead, bars + table)


def _real_estate(estate: dict[str, Any]) -> str:
    props = estate["properties"]
    if not props:
        return ""
    dates = sorted({f"{p['valued_on']:%Y.%m}" for p in props})
    lead = (f"부동산이 자산의 <b>{_pct(estate['share_of_assets'], 0)}</b>를 차지합니다(시세 기준 {', '.join(dates)}). "
            "<b>LTV</b>(집값 대비 대출 비율)가 낮을수록 시세 하락에 견딜 여유가 큽니다.")
    bars = _bars([(p["label"], p["value"], i == 0) for i, p in enumerate(props)])
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
    return _section("부동산·주거", lead, bars + table)


def _delta_pct(ratio: Decimal | None) -> str:
    return "-" if ratio is None else f"{'+' if ratio > 0 else ''}{ratio * 100:.1f}%"


def _pension(pi: dict[str, Any]) -> str:
    lead = (f"인출이 제한된 자산(연금·청약 등)은 <b>{_short(pi['restricted_total'])}</b>입니다. "
            f"월 보험료는 <b>{_short(pi['monthly_premiums'])}</b>로 월 소득의 {_pct(pi['premium_to_income'])}입니다.")
    if not pi["surrender_values_known"]:
        lead += " 보험 해약환급금이 아직 입력되지 않아 보험의 자산 가치는 반영되어 있지 않습니다."
    return _section("연금·보험", lead, "")


def _macro(macro: dict[str, Any]) -> str:
    rows = [r for r in macro["evidence"] if r["value"] is not None]
    rates = [(SERIES_LABELS.get(r["spec"].series_id, r["spec"].label), r["change_12m"]) for r in rows
             if r["spec"].unit == "%" and r["change_12m"] is not None]
    rising = sum(1 for _, change in rates if change > 0)
    lead = (f"지난 1년간 주요 금리 {len(rates)}개 중 <b>{rising}개가 올랐습니다</b>(단위: <b>%p</b>). "
            "금리가 오르면 변동금리 대출 이자는 늘고, 예금 이자와 채권 수익률은 높아지며, 주식·부동산 가격에는 "
            "대체로 부담이 됩니다. 우리 집 자산 가운데 이 변화에 노출된 금액은 아래 표와 같습니다.")
    chart = _diverging([(label, change) for label, change in rates], value_text=_pp) if rates else ""
    # Three-month changes stay in the data for the narrative; the phone-width table keeps the 1-year view.
    table = _table(["지표", "값", "1년 변화", "출처·관측일"],
                   [[SERIES_LABELS.get(r["spec"].series_id, r["spec"].label),
                     f"{r['value']}{'%' if r['spec'].unit == '%' else '원'}", _delta(r["change_12m"]),
                     (f"{r['spec'].provider.upper()} {r['observed_on']:%m.%d}", r["spec"].link)] for r in rows])
    table += _table(["금리·환율 변화에 노출된 자산·부채", "금액"],
                    [[_exposure_label(e.key), _won(e.amount)] for e in macro["exposures"].exposures])
    news = macro["announcements"][:5]
    if news:
        table += _table(["최근 공식 발표(제목을 누르면 원문)", "날짜"],
                        [[(a.title, a.link), f"{a.published_at:%Y.%m.%d}"] for a in news])
    return _section("거시 환경", lead, chart + table)


def _exposure_label(key: str) -> str:
    return EXPOSURE_LABELS.get(key.split(":")[0], key.split(":")[0])


def _pp(value: Decimal) -> str:
    arrow = "▲" if value > 0 else "▼" if value < 0 else "–"
    return f"{arrow} {value:+.2f}%p"


def _glossary() -> str:
    inner = "".join(f'<div style="font-size:13px;line-height:1.6;color:{BODY};margin:3px 0"><b style="font-weight:600;'
                    f'color:{INK}">{escape(term)}</b> — {escape(text)}</div>' for term, text in TERMS.items())
    return _section("용어 풀이", "", inner)


def _footer(data: dict[str, Any], assumptions: list[str], ai: dict[str, Any] | None = None) -> str:
    """Soft footer: where the numbers came from, their limits, and what Smith never does."""
    lines = [f"{_source(f['source'])}: {f['records']}건, 기준일 {f['oldest'].date()}~{f['newest'].date()}"
             for f in data["freshness"]]
    lines += [f"동기화 · {_source(s['source'])}: 최근 성공 {s['last_success'].date() if s['last_success'] else '없음'}"
              for s in data["sync"]]
    lines += [f"경고: {w}" for w in data["warnings"]]
    if not data["completeness"]["complete"]:
        lines.append(f"불완전 영역: {', '.join(data['completeness']['areas'])}")
    lines += [f"가정: {a}" for a in assumptions]
    lines += _ai_lines(ai)
    items = "".join(f'<div style="font-size:12px;line-height:1.6;color:{MUTED}">· {escape(line)}</div>' for line in lines)
    return (f'<tr><td>{_eyebrow("데이터 상태")}'
            f'<div style="font-size:13px;color:{BODY};margin-bottom:8px">이 보고서가 사용한 데이터의 기준 시점과 한계입니다.</div>'
            f'{items}<div style="font-size:12px;line-height:1.6;color:{MUTED};margin-top:14px;padding-top:12px;'
            f'border-top:1px solid {HAIRLINE}">Smith는 조회 전용 자문입니다. 매매·이체·대출 신청을 실행하지 않으며, '
            f'세금과 법령은 실행 전 현행 기준을 확인하셔야 합니다.</div></td></tr>')


def _ai_lines(ai: dict[str, Any] | None) -> list[str]:
    """How the AI stages went: research brief, narrative, and what code verification removed."""
    if not ai:
        return ["AI 해석: 이번 보고서는 계산 결과만으로 작성했습니다."]
    brief = ai.get("brief")
    if brief:
        line = (f"정책·시장 조사: {brief['created_at']:%m월 %d일} 웹 조사 {brief['items']}건(공식 출처 {brief.get('official', 0)}건). "
                "금액·종목·계좌·직접 식별자 없이, 승인된 시·구와 자산·부채 유형만으로 조사했습니다.")
        if brief.get("checked"):
            line += (f" 출처 페이지를 직접 열어 {brief['checked']}건을 확인했고, 그중 {brief['matched']}건은 본문에서 "
                     "같은 수치를 찾았습니다(없어진 페이지의 항목은 뺐습니다).")
        if brief.get("reused"):
            line += " 이번 보고에서 새로 조사하지 못해, 같은 주제로 앞서 조사한 자료를 다시 사용했습니다."
        lines = [line]
    else:
        lines = ["정책·시장 조사: 이번 보고에 쓸 수 있는 조사 결과가 없어 외부 정책·시장 변화는 반영하지 않았습니다."]
    if ai["outcome"] == "success":
        flagged = len(ai.get("dropped") or [])
        lines.append("AI 해석: 계산값과 출처로 자동 점검했습니다"
                     + (f"(확인되지 않은 부분이 있는 {flagged}개 항목에는 '자동 점검 메모'를 붙였습니다)." if flagged else "."))
    else:
        lines.append(f"AI 해석: 이번에는 실패해({ai.get('error_code', '원인 미상')}) 계산 결과만으로 작성했습니다.")
    return lines


def _source(source: str) -> str:
    return "수동 입력" if source.startswith("manual") else SOURCE_LABELS.get(source, source)


def _dec(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _delta(value: Decimal | None) -> str:
    return "-" if value is None else f"{value:+}"
