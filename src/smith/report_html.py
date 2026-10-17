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
from smith import editions, principles
from smith.holdings import STANCES
from smith.proposals import Track
from smith.realestate import MIN_BASIS
from smith.report_data import CATEGORY_LABELS, CERTAINTY_LABELS, COMPONENT_LABELS, PAY_LABELS

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
    "스트레스 테스트": "금리 급등, 소득 중단, 집값·주식 하락 같은 나쁜 일이 한꺼번에 온다고 가정하고 우리 집 돈이 버티는지 미리 계산해 보는 것입니다.",
    "안심 소비 한도": "비상금과 날짜가 정해진 목표에 매월 필요한 적립을 빼고도 남아, 이번 달에 써도 계획이 늦어지지 않는 돈입니다.",
}
STRESS_LEASE_FLAG = "lease_deposit_uncovered"  # stress.LEASE_UNCOVERED
STRESS_VERDICT = {"robust": ("●", "버팁니다", "#05b169"), "watch": ("▲", "빠듯합니다", "#f4b000"),
                  "fragile": ("■", "취약합니다", "#ff5c66"), "unknown": ("?", "판단 불가", "#7c828a")}


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
    bands = [_band(DARK, _header(data, status, reasons) + _judgement(story) + _overview(data) + _callout(advice)),
             _band(CANVAS, _proposals(advice, full), border=True)]
    if full and advice["strategy"]:
        bands.append(_band(DARK, _strategy(advice["strategy"], story.get("direction", ""), story.get("direction_memo", ""))))
    sections = [_changes_outside(story), _follow_up(advice), _tax(advice, data, full), _change(data), _timeline(data)]
    if full:
        sections.append(_future_income(data))
        sectors = data["sectors"]
        sections += [_allocation(data), _cash(sectors["cash"], data), _spending(advice.get("spending_room")),
                     _stress(data["stress"]), _debt(sectors["debt"]),
                     _policy(data, story, full), _holdings(data, story, full), _real_estate(sectors["real_estate"]),
                     _pension(sectors["pension_insurance"]), _macro(sectors["macro"])]
    else:
        sections += [_policy(data, story, full), _holdings(data, story, full)]
    sections += [_requests(data.get("requests") or [], full), _glossary()]
    bands.append(_band(CANVAS, "".join(sections), border=True))
    bands.append(_band(SOFT, _footer(data, advice["assumptions"], story.get("status"))))
    return _subject(data, status), _page(bands)


def _page(bands: list[str]) -> str:
    spacer = '<tr><td style="height:12px;line-height:12px;font-size:0">&nbsp;</td></tr>'
    return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>Smith 보고서</title></head>'
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
    hero = (_header(data, status, reasons, lead=edition.lead) + _judgement(story) + _tile_grid(_tiles(data, edition))
            + _callout(advice, edition))
    bands = [_band(DARK, hero)]
    if advice["proposals"] or overview:
        bands.append(_band(CANVAS, _proposals(advice, not overview, edition=edition), border=True))
    if edition.strategy and advice["strategy"]:
        bands.append(_band(DARK, _strategy(advice["strategy"], story.get("direction", ""), story.get("direction_memo", ""))))
    sectors = data["sectors"]
    renderers = {
        "follow_up": lambda: _follow_up(advice), "change": lambda: _change(data),
        "allocation": lambda: _allocation(data), "timeline": lambda: _timeline(data),
        "future_income": lambda: _future_income(data),
        "requests": lambda: _requests(data.get("requests") or [], True), "glossary": lambda: _glossary(),
        "holdings": lambda: _holdings(data, story, True), "policy": lambda: _policy(data, story, True),
        "macro": lambda: _macro(sectors["macro"]), "real_estate": lambda: _real_estate(sectors["real_estate"]),
        "debt": lambda: _debt(sectors["debt"]), "tax": lambda: _tax(advice, data, True),
        "pension": lambda: _pension(sectors["pension_insurance"]), "cash": lambda: _cash(sectors["cash"], data),
        "spending": lambda: _spending(advice.get("spending_room")), "stress": lambda: _stress(data["stress"]),
        "stress_summary": lambda: _stress_summary(data["stress"]),
    }
    title = "이번 주 눈여겨볼 변화" if overview else f"{edition.label} — 눈여겨볼 변화"
    sections = [_changes_outside(story, title)] + [renderers[name]() for name in edition.sections]
    if edition.terms:
        sections.append(_glossary(edition.terms))
    if any(sections):
        bands.append(_band(CANVAS, "".join(sections), border=True))
    bands.append(_band(SOFT, _footer(data, advice["assumptions"], story.get("status"))))
    return _subject(data, status), _page(bands)


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
        return [("부동산 평가액", amount(f["property_value"]), f"자산의 {_pct(estate['share_of_assets'], 0)}"),
                ("부동산 관련 부채", amount(f["property_debt"]), "담보대출·보증금 반환"),
                ("돌려드릴 보증금", "-" if lease is None else amount(lease["amount"]),
                 "" if lease is None else f"{lease['date']:%Y.%m} 만기"),
                ("최근 6개월 실거래", f"{f['property_trades_6m']}건", "같은 단지·같은 면적")]
    if edition.tiles == "rates":
        debt = s["debt"]
        one = next((x for x in debt["shocks"] if x["shock"] == Decimal("0.01")), None)
        base = next((r for r in s["macro"]["evidence"] if r["spec"].series_id == "722Y001/0101000" and r["value"] is not None),
                    None)
        return [("변동금리 대출", "미상" if debt["variable_total"] is None else _short(debt["variable_total"]), "잔액"),
                ("월 이자(추정)", "미상" if debt["monthly_interest_estimate"] is None
                 else _short(debt["monthly_interest_estimate"]), "현재 금리 기준"),
                ("금리 1%p 오르면", "-" if one is None or one["monthly_increase"] is None
                 else "+" + _short(one["monthly_increase"]), "월 이자 증가"),
                ("한국은행 기준금리", "미상" if base is None else f"{base['value']}%",
                 "" if base is None else f"{base['observed_on']:%m.%d} 기준")]
    if edition.tiles == "tax":
        plan = data["tax_plan"]
        room = (("세액공제 남은 한도", "한도 채움", "올해는 더 넣어도 공제가 늘지 않습니다") if not plan["irp_room"] else
                ("세액공제 남은 한도", _short(plan["irp_room"]), f"채우면 약 {_short(plan['refund_gain'])} 더 환급(추정)"))
        lower = f" · 환산 못 한 {plan['pension_unconverted']}건 제외(최소치)" if plan["pension_unconverted"] else ""
        return [("올해 연금 납입(예상)", _short(plan["savings_paid"] + plan["irp_paid"]), "연금저축 + IRP" + lower),
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
    failed_sync = any(s["last_failure"] and (not s["last_success"] or s["last_failure"] > s["last_success"])
                      for s in data["sync"])
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
    if not data["completeness"]["complete"] or failed_sync or data["warnings"]:
        reasons.append("일부 데이터가 불완전하거나 오래되었거나 동기화에 실패했습니다(맨 아래 '데이터 상태').")
    if must:
        reasons.append(f"판단에 꼭 필요한 자료 {len(must)}건이 비어 있습니다"
                       + ("('다음에 확인할 것')." if requests_shown else "(종합·생활 보고의 '다음에 확인할 것')."))
    stress = data.get("stress") or {}
    if stress.get("liquidity_verdict") == "fragile":
        reasons.append(f"나쁜 일이 겹치는 가정에서 현금만으로 {stress['months_cash']:.1f}개월밖에 버티지 못합니다"
                       "(스트레스 테스트).")
    if STRESS_LEASE_FLAG in stress.get("flags", ()):
        reasons.append(f"나쁜 일이 겹치는 가정에서 {stress['lease']['due']:%Y년 %m월} 보증금 반환이 부족합니다"
                       "(스트레스 테스트).")
    if here or elsewhere:
        return "act", reasons
    if reasons:
        return "watch", reasons
    return "stable", ["데이터가 최신이고, 판단에 꼭 필요한 자료가 갖춰져 있습니다."]


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

def _header(data: dict[str, Any], status: str, reasons: list[str], *, lead: str = "") -> str:
    edition = editions.title(data["kind"], data["as_of"].date())
    icon, label, color = STATUS[status]
    reason = escape(" ".join(reasons))
    note = ""
    if lead:
        note += f'<div style="font-size:14px;line-height:1.6;color:{ON_DARK_SOFT};margin-top:10px">{escape(lead)}</div>'
    if data.get("delivery_note"):
        note += (f'<div style="font-size:13px;line-height:1.6;color:{ON_DARK};background:{DARK_RAISED};border-radius:16px;'
                f'padding:12px 14px;margin-top:14px">▲ {escape(data["delivery_note"])}</div>')
    return (f'<tr><td>{_eyebrow(f"SMITH · {edition}", color=ON_DARK, background=DARK_RAISED)}'
            f'<div style="font-family:{SANS};font-weight:400;font-size:30px;line-height:1.15;letter-spacing:-0.8px;'
            f'color:{ON_DARK};margin:4px 0 12px">{data["as_of"]:%Y년 %m월 %d일} 보고</div>'
            f'<div style="font-size:14px;color:{ON_DARK_SOFT}"><span style="color:{color};font-weight:600">{icon} {label}</span>'
            f' — {reason}</div>{note}</td></tr>')


def _overview(data: dict[str, Any]) -> str:
    return _tile_grid(_overview_tiles(data))


def _overview_tiles(data: dict[str, Any]) -> list[tuple[str, str, str]]:
    k = data["kpis"]
    months = "미상" if k["immediate_months"] is None else f'월 지출 {k["immediate_months"]:.1f}개월분'
    home = k["home_progress"]
    return [("순자산", _short(k["net_worth"]), "기준선" if k["net_worth_change"] is None else _signed_short(k["net_worth_change"])),
            ("월 여유 현금흐름", _short(k["monthly_net"]), "수입 − 지출"),
            ("즉시 쓸 수 있는 돈", _short(k["immediate"]), months),
            ("주택 목표 준비율(본인)", "미상" if home is None else _pct(home["ratio_self"], 0),
             "" if home is None else f'{home["target_date"]:%Y.%m} 기준 시나리오')]


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


def _callout(advice: dict[str, Any], edition: editions.Edition | None = None) -> str:
    """The single most important message, once per report, closing the dark hero. "Nothing to do" is
    said only when every rule could judge; a rule stopped by unknown inputs is named instead. A themed day
    speaks only for its own area."""
    proposals, undetermined = advice.get("proposals") or [], advice.get("undetermined") or []
    certainty = ""
    themed = edition is not None and edition.key != "overview"
    eyebrow = "오늘의 한 가지" if themed else "이번 주 가장 중요한 한 가지"
    if proposals:
        top = proposals[0]
        title, why, certainty = top.title, _first_sentence(top.why), top.certainty
    elif undetermined:
        # Never "nothing to do" while a rule of this report could not judge (its own day lists it).
        title = (f"오늘 다룬 {edition.label} 분야에 새 제안은 없지만, 자료가 부족해 판단하지 못한 부분이 있습니다." if themed
                 else "이번 주 새 제안은 없지만, 자료가 부족해 판단하지 못한 부분이 있습니다.")
        why = "판단하지 못한 것: " + " · ".join(f"{u.topic}({u.missing})" for u in undetermined)
    elif themed:
        title = f"오늘 다룬 {edition.label} 분야에서는 새로 실행하실 일이 없습니다."
        why = "다른 분야의 제안은 그 분야를 다루는 보고와 주간 종합 보고에서 말씀드립니다."
    else:
        title = "이번 주에는 새로 실행하실 일이 없습니다. 지금의 전략을 유지하시는 것이 합리적입니다."
        why = "현금 여유, 대출 부담, 목표 일정 모두 기준 안에 있어 바꿀 이유가 확인되지 않았습니다."
    label = (f'<div style="font-size:12px;color:{ON_DARK_SOFT};margin-top:8px">근거의 성격: {escape(certainty)}</div>'
             if certainty else "")
    return (f'<tr><td style="padding-top:10px"><div style="background:{DARK_RAISED};border-radius:24px;padding:22px 20px">'
            f'{_eyebrow(eyebrow, color=ON_DARK, background=BLUE)}'
            f'<div style="font-family:{SANS};font-weight:400;font-size:22px;line-height:1.35;letter-spacing:-0.4px;'
            f'color:{ON_DARK}">{escape(title)}</div>'
            f'<div style="font-size:14px;line-height:1.6;color:{ON_DARK_SOFT};margin-top:10px">{escape(why)}</div>'
            f'{label}</div></td></tr>')


def _proposals(advice: dict[str, Any], full: bool, edition: editions.Edition | None = None) -> str:
    """The proposal cards: why now, effect, risks, timing, what would change the judgement, certainty.
    Thursday cards are short but keep the certainty label. Judgements stopped by unknown inputs follow."""
    proposals = advice.get("proposals") or []
    themed = edition is not None and edition.key != "overview"
    head = f'<tr><td>{_eyebrow("오늘의 제안" if themed else "이번 주 제안")}{_heading("고객님께 드리는 제안", size=28)}</td></tr>'
    pending = _undetermined(advice)  # A daily edition carries only its own rules' pending judgements.
    if not proposals:
        return head + (f'<tr><td style="font-size:15px;line-height:1.6;color:{BODY}">새 제안이 없습니다. 제안이 없는 것도 '
                       '판단입니다. 다음 보고에서 변화가 있으면 다시 말씀드리겠습니다.</td></tr>') + pending
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
            home = editions.home(p.key) if edition is not None else None
            detail = (f'<div style="font-size:14px;line-height:1.6;color:{BODY}">{escape(_first_sentence(p.why))}</div>'
                      f'<div style="font-size:12px;color:{MUTED};margin-top:6px">근거의 성격: {escape(p.certainty)}'
                      + (f" · 자세히: {escape(home.label)} 보고" if home else "") + '</div>')
        cards.append(f'<tr><td style="padding-top:12px"><div style="background:{CANVAS};border:1px solid {HAIRLINE};'
                     f'border-radius:24px;padding:20px 20px">{badge}{title}{detail}</div></td></tr>')
    rest = proposals[3:]
    more = ""
    if rest:
        items = "".join(f'<div style="font-size:13px;line-height:1.6;color:{BODY}">· {escape(p.title)}</div>' for p in rest)
        more = (f'<tr><td style="padding-top:16px"><div style="font-size:12px;font-weight:600;color:{MUTED};'
                f'margin-bottom:4px">함께 검토한 다른 제안</div>{items}</td></tr>')
    if not full:
        note = ("종합 보고는 요약입니다. 제안마다 근거와 위험은 그 분야를 다루는 요일 보고에서 자세히 말씀드립니다."
                if edition is not None else "목요 보고는 요약입니다. 근거와 위험은 월요 보고에서 자세히 말씀드립니다.")
        more += f'<tr><td style="padding-top:12px;font-size:12px;color:{MUTED}">{note}</td></tr>'
    return head + "".join(cards) + more + pending


def _undetermined(advice: dict[str, Any]) -> str:
    """Rules that could not judge because an input is unknown: said plainly, never read as all clear."""
    items = advice.get("undetermined") or []
    if not items:
        return ""
    rows = "".join(f'<div style="font-size:13px;line-height:1.6;color:{BODY}">· <b style="font-weight:600;color:{INK}">'
                   f'{escape(u.topic)}</b> — {escape(u.missing)}</div>' for u in items)
    return (f'<tr><td style="padding-top:16px"><div style="font-size:12px;font-weight:600;color:{MUTED};'
            f'margin-bottom:4px">자료가 부족해 이번에 판단하지 못한 것</div>{rows}</td></tr>')


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


def _changes_outside(story: dict[str, Any], title: str = "이번 주 눈여겨볼 변화") -> str:
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
    when = "이번 주" if title == "이번 주 눈여겨볼 변화" else "오늘 아침"
    lead = f"{when} 조사한 정책·시장 변화 가운데 고객님 자산과 직접 연결되는 것만 골랐습니다. 출처를 누르면 원문을 보실 수 있습니다."
    return _section(title, lead, "".join(blocks))


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
    balanced = change.get("balanced", True)
    parts = [(COMPONENT_LABELS[key], value) for key, value in change["parts"].items() if value] if balanced else []
    parts.sort(key=lambda item: -abs(item[1]))
    direction = "증가" if change["total"] > 0 else "감소" if change["total"] < 0 else "변동 없음"
    lead = (f"지난 보고 이후 <b>순자산</b>(가진 것 − 갚을 것)이 <b>{_short(abs(change['total']))} {direction}</b>했습니다."
            if change["total"] else "지난 보고 이후 순자산 변화가 없습니다.")
    if parts:
        lead += f" 가장 큰 요인은 <b>{escape(parts[0][0])}</b>입니다."
        lead += " 며칠 사이의 시세 등락은 흔한 일이며, 장기 목표 대비 흐름으로 판단하시기 바랍니다."
    if not balanced:
        lead += " 원인별 분해의 합계가 전체 변화와 맞지 않아 이번에는 분해를 싣지 않았습니다(Smith 계산 점검이 필요합니다)."
    if not change["complete"]:
        lead += " 환율이 없는 통화가 있어 분해가 일부 불완전합니다."
    correction = change.get("correction") or Decimal(0)
    if correction:
        lead += (f" 과거 입력을 바로잡아 지난 보고의 출발점이 {_signed_short(correction)} 달라졌습니다. 이는 실제 변동이 "
                 "아니어서 위 변화에 넣지 않았습니다.")
    rows = [[label, _won(value)] for label, value in parts] + [["합계", _won(change["total"])]]
    if correction:
        rows.append(["(참고) 과거 입력 정정 · 합계 제외", _won(correction)])
    return _section("지난 보고 이후 변화", lead, (_diverging(parts) if parts else "") + _table(["요인", "금액"], rows))


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
        rows.append(f'<tr><td style="width:100px;font-family:{MONO};font-size:12px;font-weight:500;color:{INK};padding:7px 8px 7px 0">'
                    f'{item["date"]:%Y.%m.%d}</td>'
                    f'<td style="font-size:13px;color:{BODY};padding:7px 0 7px 12px;border-left:3px '
                    f'{"dashed " + MUTED_SOFT if inflow else "solid " + ACCENT}">'
                    f'{escape(item["label"])} · {amount} <span style="color:{MUTED_SOFT}">({months}개월 후)</span></td></tr>')
    needs = [item for item in items if item["kind"] != "inflow"]
    first = needs[0] if needs else None
    lead = ("향후 5년 안에 예정된 큰 지출 일정은 없습니다." if first is None else
            f"가장 가까운 큰 자금 일정은 <b>{first['date']:%Y년 %m월}</b>의 {escape(first['label'])}"
            f"(<b>{_short(first['amount'])}</b>)입니다. 이 일정들이 현금 준비의 기준이 됩니다.")
    if len(needs) < len(items):
        lead += (" 점선 표시는 앞으로 들어올 예정 소득으로, 받기 전까지는 확정된 돈이 아닙니다. 세후 금액은 그해 최종 세금 기준이라 "
                 "지급일에 실제로 들어오는 돈과 다를 수 있습니다.")
    home = data["kpis"]["home_progress"]
    inner = f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{"".join(rows)}</table>'
    if home:
        inner += _home_plan(home)
        gap = home["required"] - home["available_self"]
        lead += (f" 주택 목표의 기준 시나리오에서 본인 자원으로 준비 가능한 금액은 필요액의 <b>{_pct(home['ratio_self'], 0)}</b>"
                 f"이며, 부족분은 약 <b>{_short(gap)}</b>입니다(가정: 집값 연 2%·투자 수익 연 4%).")
        if home.get("pay_at_target"):
            lead += (f" 목표일 전에 받을 예정 소득(확정·예상분, 세후 추정)은 그때 가치로 약 <b>{_short(home['pay_at_target'])}</b>"
                     f"이며, 이를 더하면 필요액의 <b>{_pct(home['ratio_with_pay'], 0)}</b>입니다. 다만 받기 전까지는 확정이 "
                     "아니어서, 준비 상태 판단과 주식 비중 조절 계획은 예정 소득을 뺀 숫자를 기준으로 합니다.")
        elif home.get("pay_at_target") is None:  # Unknown (no FX rate for some pay), never shown as none.
            lead += " 예정 소득 가운데 원화로 환산하지 못한 항목이 있어, 예정 소득을 더한 값은 계산하지 않았습니다(미상)."
    return _section("목표와 일정", lead, inner)


def _home_plan(home: dict[str, Any]) -> str:
    """Bars and the scenario table of the home goal, with expected pay as its own column when there is any."""
    bars = [("필요 자금(비용 포함)", home["required"], False), ("본인 준비 가능액", home["available_self"], True)]
    if home.get("pay_at_target"):
        bars.append(("예정 소득 포함 시(기준)", home["available_with_pay"], False))
    if home["available_with_partner"] is not None:
        bars.append(("배우자 부동산 포함 시", home["available_with_partner"], False))
    with_pay = any(s.get("expected_pay_at_target") not in (None, "0") for s in home["scenarios"])
    headers = ["시나리오", "필요 자금", "본인 준비 가능액", "부족분(본인)"] + (["예정 소득 반영 부족분"] if with_pay else [])
    rows = [[SCENARIO_LABELS.get(s["name"], s["name"]), _short(_dec(s["required_with_costs"])),
             _short(_dec(s["available_self"])), _short(_dec(s["gap_self"]))]
            + ([_short(_dec(s["gap_self_with_expected_pay"]))] if with_pay else []) for s in home["scenarios"]]
    return '<div style="height:12px"></div>' + _bars(bars) + _table(headers, rows)


TAX_BASIS_TEXT = {"net_input": "세후 입력", "estimated": "연봉 기준 추정", "assumed": "가정 세율", "as_entered": "입력값",
                  "unknown": "환율 미상", "prior_unknown": "앞선 보상 금액 미상"}


def _future_income(data: dict[str, Any]) -> str:
    """Expected pay (bonuses, share grants): what is coming, how sure it is, after an estimated tax, and the
    concentration a grant adds. Nothing here is in net worth (report design §15)."""
    found = data.get("future_income") or {}
    items = found.get("items") or []
    if not items:
        return ""
    sure = found["by_certainty"]
    lead = (f"앞으로 받을 성과급·주식 보상은 최종 세후 추정으로 확정 <b>{_short(sure['confirmed'])}</b>, 예상 "
            f"<b>{_short(sure['expected'])}</b>, 가능성 {_short(sure['possible'])}입니다. 아직 받지 않은 돈이라 순자산과 월 현금흐름에는 "
            "넣지 않았고, 계획에는 확실한 정도에 따라 나눠 반영합니다(불리: 확정분만, 기준: 확정+예상, 유리: 전부). 세후는 그해 "
            "최종 세금 기준이며, 지급일에 실제로 들어오는 돈은 원천징수 방식에 따라 다르고 연말정산·급여 공제로 나중에 맞춰집니다.")
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
    inner = _table(["받는 달", "내용", "확실성", "세전", "최종 세후(추정)", "비고"], rows)
    for stock in found.get("same_stock") or []:
        # A known 0% now (no securities yet) still warns; only unknown shares are skipped.
        if stock["share_now"] is None or stock["share_with_grants"] is None or not stock["granted"]:
            continue
        inner += (f'<div style="font-size:14px;line-height:1.6;color:{BODY};margin-top:10px">회사 주식 집중: 지금 보유한 '
                  f'{escape(stock["symbol"])}은 본인 증권 자산의 <b>{_pct(stock["share_now"], 0)}</b>이고, 예정된 주식 보상과 자사주로 '
                  f'받는 성과급(기준, 세후)을 받으면 <b>{_pct(stock["share_with_grants"], 0)}</b>가 됩니다. 월급도 같은 회사에서 나오므로, 받는 대로 일부를 '
                  "분산하는 원칙을 정해 두시면 회사 실적 하나에 소득과 자산이 함께 흔들리는 위험을 줄일 수 있습니다.</div>")
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
    if cash.get("hermes"):
        lead += (" Hermes가 운용하는 자산은 Smith가 매각·변경을 권하지 않으므로 쓸 수 있는 돈에 넣지 않고 "
                 "따로 표시했습니다.")
    lead += _toss_cash_note(cash.get("toss_cash") or {})
    ladder = cash["ladder"]
    bars = _bars([(label, value, label == "즉시") for label, value in ladder])
    return _section("현금·유동성", lead, bars + _table(["현금화 속도", "금액"], [[l, _won(v)] for l, v in ladder]))


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


def _months_text(value: Decimal) -> str:
    return f"{value:.1f}개월"


def _stress_lead(st: dict[str, Any], *, detail: bool) -> str:
    """The stress scenario in words: assumptions, the monthly shortfall, how far cash and then stock sales
    carry the household through the scenario's months, an uncovered lease deposit, and the verdict."""
    a, m = st["assumptions"], st["monthly"]
    horizon = a["income_stop_months"]
    icon, label, color = STRESS_VERDICT[st["verdict"]]
    badge = f'<b style="color:{color}">{icon} {escape(label)}</b>'
    text = (f"변동금리 대출 금리 +{a['rate_shock'] * 100:.1f}%p, 가장 큰 근로·사업 소득 {horizon}개월 중단, "
            f"집값 {a['property_drop'] * 100:.0f}% 하락, 주식·펀드 {a['stock_drop'] * 100:.0f}% 하락이 한꺼번에 온다고 "
            "가정(모두 가정값)했습니다.")
    if m["burn"] is None:
        text += " 월 현금흐름이나 대출 가운데 원화로 환산하지 못한 금액이 있어 버티는 기간은 계산하지 않았습니다."
    elif m["burn"] <= 0:
        text += f" 그래도 매월 약 <b>{_short(-m['burn'])}</b>이 남아 현금이 줄지 않습니다."
    else:
        text += f" 이 경우 매월 약 <b>{_short(m['burn'])}</b>이 모자랍니다."
        if st["months_cash"] is None:
            text += " 즉시 쓸 수 있는 돈에 환산하지 못한 금액이 있어 버티는 개월은 미상입니다."
        elif st["covers_scenario"]:
            text += (f" 즉시 쓸 수 있는 돈 {_short(st['immediate'])}으로 {horizon}개월을 모두 버티고 약 "
                     f"<b>{_short(st['cash_after_scenario'])}</b>이 남습니다.")
        else:
            text += (f" 즉시 쓸 수 있는 돈 {_short(st['immediate'])}만으로는 {horizon}개월 중 "
                     f"<b>{_months_text(st['months_cash'])}</b>을 버팁니다.")
            if st["after_sales"] is not None and st["after_sales"] >= 0:
                text += f" 주식·펀드를 낮아진 값에 팔면 {horizon}개월을 모두 버티고 약 {_short(st['after_sales'])}이 남습니다."
            elif st["months_with_sales"] is not None:
                text += f" 주식·펀드를 낮아진 값에 팔아도 {_months_text(st['months_with_sales'])}까지입니다."
    lease = st["lease"]
    if STRESS_LEASE_FLAG in st["flags"]:
        text += (f" 다만 {lease['due']:%Y년 %m월} 돌려드릴 보증금 {_short(lease['amount'])}은 이 가정에서 약 "
                 f"{_short(lease['available'])}밖에 마련하지 못합니다.")
    text += f" 판정: {badge}."
    if detail:
        text += (f" 기준: {horizon}개월을 모두 버티면 안정, {a['watch_months']:.0f}개월 이상이면 빠듯, 그 미만이면 취약이며, "
                 "가까운 보증금 반환이 부족하면 최소 빠듯입니다.")
        if st["pausable"]:
            text += f" 멈출 수 있다고 표시하신 지출 {_short(st['pausable'])}을 멈추면 버티는 기간이 늘어납니다."
    return text


def _stress(st: dict[str, Any]) -> str:
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
        table_rows.append([f"보증금 반환({lease['due']:%Y.%m})", _won(lease["amount"]), covered])
    return _section("스트레스 테스트: 나쁜 일이 겹치면", _stress_lead(st, detail=True),
                    bars + _table(["항목", "평소", "위기 가정"], table_rows))


def _stress_summary(st: dict[str, Any]) -> str:
    """The overview's one-paragraph version: verdict and months, no table (the household day has it)."""
    return _section("스트레스 테스트: 나쁜 일이 겹치면",
                    _stress_lead(st, detail=False) + " 자세한 표는 생활·목표 보고에 있습니다.", "")


def _spending(room: dict[str, Any] | None) -> str:
    """Guilt-free spending room (report design §16.3): the surplus, what the reserve and contractual payments
    take every month, the room on that basis, and the smaller room when the planned goals are kept too.
    Nothing when an input is unknown (the proposals say so)."""
    if room is None:
        return ""
    safe, with_goals = max(room["room"], Decimal(0)), max(room["room_with_goals"], Decimal(0))
    rows = ([("남는 돈", room["surplus"], False)] + [(label, amount, False) for label, amount in room["committed"]]
            + [("안심 소비 한도(약정 기준)", safe, True)])
    if room["planned"]:
        rows += [(label, amount, False) for label, amount in room["planned"]] + [("계획한 목표까지 지킬 때", with_goals, False)]
    if room["room"] >= 0:
        lead = (f"수입에서 지출과 본인 계좌 이체를 뺀 남는 돈 {_short(room['surplus'])} 가운데 비상금과 약정(보증금 반환)이 매월 "
                f"가져가는 적립은 {_short(room['committed_total'])}입니다. 나머지 <b>{_short(room['room'])}</b>은 이번 달에 쓰셔도 "
                "비상금과 약정이 늦어지지 않는 <b>안심 소비 한도</b>입니다.")
        if room["planned"]:
            lead += (f" 계획한 목표(매월 {_short(room['planned_total'])})까지 지금 속도로 지키려면 약 "
                     f"<b>{_short(room['room_with_goals'])}</b>입니다." if room["room_with_goals"] >= 0 else
                     f" 다만 계획한 목표(매월 {_short(room['planned_total'])})까지 지금 속도로 지키려면 매월 "
                     f"{_short(-room['room_with_goals'])}이 모자라, 목표의 시점·금액을 손보지 않는 한 그 목표는 늦어집니다.")
        lead += " 쌓아 두는 것이 목적이 아니라면, 시간이 지나도 남는 경험에 먼저 배정하시길 권합니다."
    else:
        lead = (f"남는 돈 {_short(room['surplus'])}보다 비상금과 약정에 매월 필요한 적립 {_short(room['committed_total'])}이 "
                f"<b>{_short(-room['room'])}</b> 더 큽니다. 지금은 마음 편히 쓸 여유가 없고, 약정과 지출을 먼저 손봐야 합니다.")
    table = _table(["항목", "매월"], [[label, _won(value)] for label, value, _ in rows])
    return _section("마음 편히 써도 되는 돈", lead, _bars(rows) + table)

STANCE_STYLE = {"add": (UP, "▲"), "hold": (MUTED, "●"), "trim": (BLUE, "▼"), "exit": (BLUE, "■")}


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
        return _section("보유 종목 의견", "매수·정리 의견이 있는 종목만 짧게 말씀드립니다. 근거는 월요 보고에서 자세히 드립니다.", rows)
    if not positions:
        return _section("보유 종목 점검", "종목 단위 정보가 있는 증권이 없습니다.", "")
    inner = _bars([(str(p["name"])[:14], p["value"], p["concentrated"]) for p in positions[:10]])
    inner += _table(["종목", "평가액", "비중", "수익률", "Smith 의견"],
                    [[_named(p), _short(p["value"]), _pct(p["share"]), _return(p["return_on_cost"]),
                      "Hermes 운용" if p["hermes"] else STANCES.get(_of(opinions, p).get("stance"), "의견 없음")]
                     for p in positions])
    inner += "".join(_opinion_card(p, _of(opinions, p)) for p in active)
    held = [p for p in positions if _of(opinions, p).get("stance") == "hold"]
    if held:
        rows = "".join(f'<div style="font-size:13px;line-height:1.6;color:{BODY};margin:3px 0">· <b style="font-weight:600;'
                       f'color:{INK}">{escape(p["name"])}</b> — {escape(_first_sentence(_of(opinions, p)["reason"]))}'
                       f'{_memo(_of(opinions, p).get("memo"))}</div>' for p in held)
        inner += f'<div style="font-size:12px;font-weight:600;color:{MUTED};margin:16px 0 4px">유지 의견</div>{rows}'
    return _section("보유 종목 점검", _holdings_lead(review, sec, bool(opinions)), inner)


def _of(opinions: dict[str, Any], position: dict[str, Any]) -> dict[str, Any]:
    """The opinion on a position; none for a Hermes-managed one, even when it shares the symbol."""
    return {} if position["hermes"] else opinions.get(position["key"], {})


def _holdings_lead(review: dict[str, Any], sec: dict[str, Any], opined: bool) -> str:
    positions = [p for p in review["positions"] if not p["hermes"]]
    lead = ""
    if positions:
        top = positions[0]
        lead = (f"보유 종목 {len(positions)}개, {_short(review['total'])}입니다. 가장 큰 종목({escape(str(top['name']))})이 "
                f"<b>{_pct(review['top1_share'])}</b>, 상위 5개가 <b>{_pct(review['top5_share'])}</b>를 차지하고, 해외 상장 종목 "
                f"비중은 {_pct(review['overseas_share'])}입니다.")
    if review["concentrated"]:
        names = ", ".join(escape(str(p["name"])) for p in positions if p["concentrated"])
        lead += (f" 한 종목이 주식의 {_pct(review['concentration_limit'], 0)}를 넘으면 그 회사 한 곳의 소식에 자산이 크게 "
                 f"흔들리는 <b>집중</b> 상태로 봅니다(가정). 지금 여기에 해당하는 종목: {names}.")
    if sec["unrealized_gain"] is not None:
        lead += (f" 종목별 <b>미실현 손익</b>(팔지 않아 확정되지 않은 손익) 합계는 {_signed_short(sec['unrealized_gain'])}"
                 + (f"이며, 매입 원가를 아는 {sec['unrealized_gain_counted']}개 종목만의 부분 합계입니다."
                    if sec["unrealized_gain_missing"] else "입니다."))
    if review["unlisted"]:
        lead += (f" 종목 코드가 없는 증권 {_short(review['unlisted'])}은 비중 계산에는 넣었지만, 무엇인지 몰라 종목 의견에서는 "
                 "빠졌습니다.")
    if sec.get("hermes"):
        lead += (f" Hermes가 운용하는 자산 {_short(sec['hermes'])}에는 의견을 드리지 않습니다(필요하면 Hermes와 상의하십시오).")
    if not opined:
        lead += " 이번에는 종목별 의견을 만들지 못해 숫자만 보여 드립니다(맨 아래 '데이터 상태')."
    return lead


def _named(position: dict[str, Any]) -> str:
    kind = principles.kind(position)
    return str(position["name"]) + ("" if kind == "stock" else f" ({principles.KIND_LABELS[kind]})")


CHECK_STATUS = {"ok": ("●", "충족", "#05b169"), "breach": ("■", "어긋남", "#cf202f"), "plan": ("▲", "계획 있음", "#b27b00"),
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
        return _section("투자 기준 점검", "우리 집 투자 기준에서 벗어난 항목만 보여 드립니다. 원칙과 근거는 월요 보고에 있습니다.", rows)
    table = _table(["기준", "우리 집 기준", "지금", "판정"],
                   [[c.title, c.standard, c.current, CHECK_STATUS[c.status][1]] for c in checks])
    view = ""
    if story.get("portfolio"):
        view = (f'<div style="background:{SOFT};border-radius:24px;padding:18px 20px;margin-top:14px">'
                f'<div style="font-size:12px;font-weight:600;color:{MUTED};margin-bottom:6px">Smith의 투자 전략 · 주식 전체</div>'
                f'<div style="font-size:15px;line-height:1.65;color:{INK}">{escape(story["portfolio"])}</div>'
                f'{_principle_chips(story.get("portfolio_principles") or [])}'
                f'{_memo(story.get("portfolio_memo"))}{_sources(story.get("portfolio_links") or [])}</div>')
    used = principles.cited([i for c in checks for i in c.principles]
                            + [p.id for p in story.get("portfolio_principles") or []]
                            + [p.id for o in (story.get("holdings") or {}).values() for p in o.get("principles", [])])
    sources = "".join(f'<div style="font-size:12px;line-height:1.6;color:{MUTED};margin:2px 0">· <b style="font-weight:600">'
                      f'{escape(p.investor)} · {escape(p.title)}</b> — {escape(p.principle)} '
                      f'({" · ".join(_cell(source) for source in p.sources)})</div>' for p in used)
    lead = ("전설적인 투자자들이 직접 공개한 글(주주서한, 메모, 강연)에서 공통된 원칙을 골라 우리 집 투자 기준으로 바꿨습니다. "
            "기준의 숫자(한 종목 한도, 지수펀드 비중, 비상금 개월 수)는 Smith의 가정이며 고객님 성향에 맞게 조정할 수 있습니다. "
            "어긋난 항목부터 고치는 것이 순서입니다.")
    return _section("투자 원칙과 우리 집 투자 기준", lead,
                    table + view + f'<div style="font-size:12px;font-weight:600;color:{MUTED};margin:16px 0 4px">'
                                   f'이번 보고에 쓴 원칙과 출처</div>{sources}')


def _check_status(status: str) -> str:
    icon, label, color = CHECK_STATUS[status]
    return f'<span style="color:{color};font-weight:600">{icon} {label}</span>'


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
    color, icon = STANCE_STYLE[stance]
    return f'<span style="color:{color};font-weight:600">{icon} {STANCES[stance]}</span>'


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
    return (f'<div style="border:1px solid {HAIRLINE};border-radius:24px;padding:18px 20px;margin-top:12px">'
            f'<div style="font-size:16px;font-weight:600;color:{INK}">{escape(position["name"])} {_stance(opinion["stance"])}</div>'
            f'<div style="font-family:{MONO};font-size:12px;color:{MUTED};margin:4px 0 6px">{escape(figures)}</div>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{detail}</table>'
            f'{_memo(opinion.get("memo"))}</div>')


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


REQUEST_LABELS = {"must": "꼭 필요", "should": "있으면 더 정확", "nice": "참고"}


def _requests(requests: list[Any], full: bool) -> str:
    """What the client could provide next (checklist.requests). Thursday lists only the essential ones."""
    shown = requests if full else [r for r in requests if r.priority == "must"]
    if not shown:
        return ""
    rows = []
    for r in shown:
        strong = r.priority == "must"
        badge = (f'<span style="display:inline-block;background:{BLUE if strong else STRONG};'
                 f'color:{"#ffffff" if strong else INK};font-size:11px;font-weight:600;border-radius:100px;'
                 f'padding:2px 10px;margin-right:6px">{REQUEST_LABELS[r.priority]}</span>')
        rows.append(f'<div style="margin:6px 0 14px"><div style="font-size:15px;font-weight:600;color:{INK};'
                    f'line-height:1.5">{badge}{escape(r.title)}</div>'
                    f'<div style="font-size:14px;line-height:1.6;color:{BODY};margin-top:4px">{escape(r.why)}</div>'
                    f'<div style="font-size:12px;color:{MUTED};margin-top:4px">입력: {escape(r.how)}</div></div>')
    rest = len(requests) - len(shown)
    lead = ("알려 주시면 보고서의 판단이 더 정확해지는 자료입니다. '꼭 필요'는 지금 판단이 비어 있거나 틀릴 수 있는 "
            "항목입니다.") + (f" 나머지 {rest}건은 월요 보고에서 안내합니다." if rest else "")
    return _section("다음에 확인할 것", lead, "".join(rows))


def _glossary(terms: tuple[str, ...] | None = None) -> str:
    """Every term, or only the ones a themed day uses."""
    chosen = TERMS.items() if terms is None else [(t, TERMS[t]) for t in terms if t in TERMS]
    inner = "".join(f'<div style="font-size:13px;line-height:1.6;color:{BODY};margin:3px 0"><b style="font-weight:600;'
                    f'color:{INK}">{escape(term)}</b> — {escape(text)}</div>' for term, text in chosen)
    return _section("용어 풀이", "", inner)


def _footer(data: dict[str, Any], assumptions: list[str], ai: dict[str, Any] | None = None) -> str:
    """Soft footer: where the numbers came from, their limits, and what Smith never does."""
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
    items = "".join(f'<div style="font-size:12px;line-height:1.6;color:{MUTED}">· {escape(line)}</div>' for line in lines)
    return (f'<tr><td>{_eyebrow("데이터 상태")}'
            f'<div style="font-size:13px;color:{BODY};margin-bottom:8px">이 보고서가 사용한 데이터의 기준 시점과 한계입니다.</div>'
            f'{items}<div style="font-size:12px;line-height:1.6;color:{MUTED};margin-top:14px;padding-top:12px;'
            f'border-top:1px solid {HAIRLINE}">Smith는 조회 전용 자문입니다. 매매·이체·대출 신청을 실행하지 않으며, '
            f'세금과 법령은 실행 전 현행 기준을 확인하셔야 합니다.</div></td></tr>')


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
