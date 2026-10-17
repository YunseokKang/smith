"""Daily themed editions: one short report a day, each on its own subject (report design §14).

The weekly plan (configurable under [reports.editions]) gives every report day an edition. An edition
decides what goes into that day's report and nothing else is computed differently:

- tiles: the four numbers in the dark hero,
- sections: which report sections are rendered, in order,
- proposals: which proposal rules it carries (every rule belongs to exactly one themed edition; the
  Monday overview carries them all, briefly),
- research: which generalized research topics are searched that morning, and whether the holdings are,
- focus: the theme the narrative writes about, kept short.

The two original kinds stay valid: "monday" is the full report with every section, "thursday" the
short change check. Reports already sent with them are rebuilt exactly as they were (e-mail answers).
"""
from dataclasses import dataclass
from datetime import date
from typing import Any

WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
WEEKDAY_KO = ("월", "화", "수", "목", "금", "토", "일")
LEGACY = ("monday", "thursday")


@dataclass(frozen=True)
class Edition:
    key: str
    label: str                        # "투자·시장"; the subject reads "화요 투자·시장".
    lead: str                         # One line under the date: what today's report is about.
    tiles: str                        # Which tile set the hero shows (report_html._tiles).
    sections: tuple[str, ...]
    proposals: tuple[str, ...] | None  # Proposal keys carried; None = all of them.
    research: tuple[str, ...]          # Research topic categories (research.topics).
    holdings_research: bool
    strategy: bool                     # The dark "on track?" band of long-range goals.
    terms: tuple[str, ...]             # Glossary entries worth repeating on this day.
    focus: str                         # The narrative's theme (English, for the model).


EDITIONS: dict[str, Edition] = {e.key: e for e in (
    Edition("overview", "종합 보고", "한 주의 시작: 우리 집 전체 그림, 이번 주 제안과 할 일입니다. 분야별 자세한 내용은 요일마다 따로 보내 드립니다.",
            "overview", ("follow_up", "change", "allocation", "timeline", "requests", "glossary"), None,
            ("housing_market", "housing_policy", "mortgage", "jeonse", "home_tax", "pension_tax", "rates",
             "us_stocks", "kr_stocks", "household_policy"), False, True, (),
            "the weekly overview across every area: where the household stands now, this week's proposals in "
            "the order to act on them, and whether the long-range direction holds. Each area's detail comes in "
            "its own themed report during the week (markets and holdings, property, rates and loans, tax and "
            "pensions, the household's month and goals), so stay at the level of the whole picture."),
    Edition("markets", "투자·시장", "주식·펀드와 환율: 보유 종목별 의견과 투자 기준 점검입니다.",
            "markets", ("holdings", "policy", "macro"), ("goal-stocks-glide",),
            ("us_stocks", "kr_stocks", "rates"), True, False, ("미실현 손익", "환율 효과", "%p"),
            "markets and the household's listed holdings: how the US and Korean markets and the won/dollar rate "
            "moved, an opinion on every holding, and the portfolio strategy against the investment policy."),
    Edition("property", "부동산", "두 집의 시세와 전세 흐름, 보증금 반환 준비와 주택 정책입니다.",
            "property", ("real_estate", "timeline"), ("lease-return",),
            ("housing_market", "housing_policy", "jeonse", "home_tax"), False, False, ("LTV", "역전세"),
            "the household's homes: official trade and jeonse figures for each property, the lease deposit due "
            "and the reverse-jeonse gap, the regional housing market and housing policy, and home taxes only as "
            "they bear on a sale or a move."),
    Edition("rates", "금리·대출", "기준금리와 대출 금리, 우리 집 이자 부담과 추가 상환 판단입니다.",
            "rates", ("debt", "macro"), ("prepayment", "variable-rate"),
            ("mortgage", "rates", "household_policy"), False, False, ("변동금리", "CD 91일물", "%p"),
            "interest rates and the household's loans: the policy-rate outlook, mortgage rates and rules, what "
            "the variable-rate loan costs now and if rates move, and whether extra repayment beats saving."),
    Edition("tax", "세금·연금", "연금 세액공제, 해외주식 공제, ISA와 연말정산 준비입니다.",
            "tax", ("tax", "pension"), ("pension-credit", "overseas-harvest", "isa-open"),
            ("pension_tax", "home_tax"), False, False, ("세액공제", "미실현 손익"),
            "tax and pensions: this year's pension credit room, overseas capital-gains deduction, ISA, the "
            "year-end settlement checklist, marriage-registration and home-tax timing, and the pension accounts."),
    Edition("life", "생활·목표", "한 달 현금흐름과 비상금, 목표까지의 준비 상황입니다.",
            "life", ("cash", "timeline", "follow_up", "requests"), ("emergency-reserve", "surplus-plan"),
            (), False, True, ("유동성", "순자산"),
            "the household's own month: income, spending, saving and investing transfers, what is left, the "
            "emergency reserve, and progress toward the dated goals (lease deposit, home move, retirement). "
            "No market news is researched for this theme; work from the household's numbers and the client's "
            "statements."),
)}

# The weekly plan when [reports.editions] does not say otherwise.
DEFAULT_PLAN = {"monday": "overview", "tuesday": "markets", "wednesday": "property", "thursday": "rates",
                "friday": "tax", "saturday": "life"}
KINDS = tuple(EDITIONS) + LEGACY


def of(kind: str) -> Edition | None:
    """The edition of a report kind; None for the two original kinds (full Monday, short Thursday)."""
    return EDITIONS.get(kind)


def for_day(config: dict[str, Any] | None, day: date) -> str:
    """The edition for a local calendar day: [reports.editions], then the default plan, then the overview."""
    name = WEEKDAY_NAMES[day.weekday()]
    table = ((config or {}).get("reports") or {}).get("editions") or {}
    return table.get(name) or DEFAULT_PLAN.get(name, "overview")


def same_series(kind: str) -> tuple[str, ...]:
    """Kinds whose last sent report is this report's baseline: last week's report on the same theme (the
    full Monday report counts as an overview)."""
    return ("overview", "monday") if kind in ("overview", "monday") else (kind,)


def home(proposal_key: str) -> Edition | None:
    """The themed day that explains a proposal in full (the overview only summarizes it)."""
    return next((e for e in EDITIONS.values() if e.proposals is not None and proposal_key in e.proposals), None)


def carries(kind: str, proposal_key: str) -> bool:
    edition = of(kind)
    return edition is None or edition.proposals is None or proposal_key in edition.proposals


def research_categories(kind: str | None) -> tuple[str, ...] | None:
    """Topic categories to research for a kind; None means every topic (the original kinds)."""
    edition = None if kind is None else of(kind)
    return None if edition is None else edition.research


def researches_holdings(kind: str | None) -> bool:
    edition = None if kind is None else of(kind)
    return edition is None or edition.holdings_research


def title(kind: str, day: date) -> str:
    """"화요 투자·시장" for an edition; the original titles for the original kinds."""
    edition = of(kind)
    if edition is None:
        return "목요 변화 점검" if kind == "thursday" else "월요 종합 보고"
    return f"{WEEKDAY_KO[day.weekday()]}요 {edition.label}"
