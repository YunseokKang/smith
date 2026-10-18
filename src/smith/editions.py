"""Daily themed editions: one short report a day, each on its own subject (report design §14).

The weekly plan (configurable under [reports.editions]) gives every report day an edition. An edition
decides what goes into that day's report and nothing else is computed differently:

- tiles: the four numbers in the dark hero,
- sections: which report sections are rendered, in order,
- proposals: which proposal rules it carries (every rule belongs to exactly one themed edition; the
  Monday overview carries them all, briefly),
- research: which generalized research topics are searched that morning, and whether the holdings are,
- focus: the theme the narrative writes about, kept short.

The full timeline, the asset allocation and the expected-pay table appear only on the overview; a themed
day quotes the figures of its own area (report design §17.6, user review 2026-10-17).

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
    # Sections rendered open right under the hero, before the proposals: the day's single most important
    # view (the loan line-up on the rates day, the pension stack on the tax day, report design §18.1).
    opening: tuple[str, ...] = ()


EDITIONS: dict[str, Edition] = {e.key: e for e in (
    Edition("overview", "종합 보고", "한 주의 시작: 우리 집 전체 그림, 이번 주 제안과 할 일입니다. 분야별 자세한 내용은 요일마다 따로 보내 드립니다.",
            "overview", ("follow_up", "change", "allocation", "timeline", "stress_summary", "future_income", "requests",
                         "glossary"),
            None,
            ("housing_market", "housing_policy", "mortgage", "jeonse", "home_tax", "pension_tax", "rates",
             "us_stocks", "kr_stocks", "household_policy", "benchmark"), False, True, (),
            "the weekly overview across every area: where the household stands now, this week's proposals in "
            "the order to act on them, and whether the long-range direction holds. Each area's detail comes in "
            "its own themed report during the week (markets and holdings, property, rates and loans, tax and "
            "pensions, the household's month and goals), so stay at the level of the whole picture. Say in one "
            "sentence whether the household survives the composite stress scenario (sector_metrics.stress), and, "
            "when the brief has a benchmark item (the official survey of net worth and income by age band), where "
            "this household's net worth sits in its age band, with the survey year: a position, not a goal. When "
            "change_since_last_report is present, brief the week's change in two figures with its dates: market prices, "
            "FX and revaluations applied to what the household held (market_and_valuation, \"시장·평가 변동\") and what "
            "moved in or out (net_inflow); never let a market loss hide behind new entries in a single total."),
    Edition("markets", "투자·시장", "주식·펀드와 환율: 보유 종목별 의견과 투자 기준 점검입니다.",
            "markets", ("holdings", "policy", "macro_brief"), ("goal-stocks-glide", "concentration-trim"),
            ("us_stocks", "kr_stocks", "rates"), True, False, ("미실현 손익", "환율 효과", "%p", "이중 집중"),
            "markets and the household's listed holdings: how the US and Korean markets and the won/dollar rate "
            "moved, an opinion on every holding, and the portfolio strategy against the investment policy. Join "
            "macro liquidity (rates), each company's fundamentals (results, guidance, valuation from the research), "
            "the trend and flows, and its standing in its sector. The holding flagged employer_stock concentrates "
            "the client's human capital and financial assets in one company (double concentration): manage it "
            "first and most carefully, with the code's trim schedule (portfolio_facts.concentration_plan)."),
    Edition("property", "부동산", "두 집의 시세와 전세 흐름, 목표 단지와의 차이, 보증금 반환 준비와 주택 정책입니다.",
            "property", ("move_targets", "real_estate", "lease_plan"), ("lease-return",),
            ("housing_market", "housing_policy", "jeonse", "home_tax", "target_market"), False, False, ("LTV", "역전세"),
            "the household's homes: official trade and jeonse figures for each property, how the lease deposit "
            "due is actually returned (sector_metrics.real_estate.lease_return_plan: a new tenant's deposit "
            "replaces most of it, so the household's own cash is the reverse-jeonse gap plus a buffer; selling "
            "brings cash in; a move-out loan is bounded by the debt-service cap), the regional housing market and "
            "housing policy, and the sequence of marriage registration, selling one home and buying the next as it "
            "changes taxes and loan limits (conditionally, with 현행 법령 확인 필요). When "
            "sector_metrics.real_estate.move_plan is present, it is the day's core: for each target complex the "
            "spread between its cost and the net proceeds of selling both homes, whether that spread widened or "
            "narrowed over six months, and how much of it the expected pay (by certainty) and the employer stock's "
            "price could close; from it, propose the order of selling the two homes (tax cases per home), the loan "
            "room and the tax moves, conditionally.", opening=("move_targets",)),
    Edition("rates", "금리·대출", "대출 현황과 금리 민감도, 대환 실익, 기준금리와 시장 금리입니다.",
            "rates", ("debt", "macro"), ("prepayment", "variable-rate", "refinance"),
            ("mortgage", "rates", "household_policy"), False, False, ("변동금리", "CD 91일물", "%p"),
            "interest rates and the household's loans: the loan line-up first, what the variable-rate loan costs "
            "now and if rates move, whether the mortgage rate is competitive against the market average on new "
            "bank mortgages (sector_metrics.debt.refinance: keep, or review a refinancing with the fee paid back in "
            "the stated months), whether extra repayment beats saving, and the policy-rate outlook. A credit line "
            "with nothing drawn (sector_metrics.debt.unused_credit_lines) costs nothing and is liquidity room, never "
            "a loan to repay.", opening=("debt",)),
    Edition("tax", "세금·연금", "노후 현금흐름 준비율, 연금 세액공제, 해외주식 공제, 금융소득 방어와 보험료 효율입니다.",
            "tax", ("tax", "annuity_review", "pension"), ("pension-credit", "overseas-harvest", "isa-open", "insurance-review"),
            ("pension_tax", "home_tax"), False, False, ("세액공제", "미실현 손익", "감액완납", "금융소득종합과세"),
            "tax and pensions: the pension stack first (sector_metrics.pension_insurance.pension_stack: every "
            "pipeline's after-tax monthly payout and start age against the monthly retirement target, the readiness "
            "ratio, and which pipeline still lacks a figure, asked for in a private banker's words), this year's "
            "pension credit room, overseas capital-gains deduction, ISA, the year-end settlement checklist, "
            "marriage-registration and home-tax timing, and whether the insurance premiums "
            "(sector_metrics.pension_insurance.insurance_review) are an efficient use of the monthly cash flow. For "
            "each entry of sector_metrics.pension_insurance.annuity_reviews, give the client a decisive opinion of the "
            "policy in two or three sentences: what they still pay and get, the forward return by lifespan against "
            "the index-fund alternative, the code's verdict and the one condition that flips it, and the sunk cost "
            "to ignore. "
            "Expected pay and share grants will lift interest and dividends toward the financial-income threshold: "
            "weigh every placement after tax and name the tax-advantaged route (ISA, pension accounts, the "
            "savings-insurance exemption, separately taxed bonds and infrastructure funds, the overseas capital-gains "
            "regime) as a standing rule.", opening=("pension_stack",)),
    Edition("life", "생활·목표", "한 달 현금흐름의 균형, 비상금, 목표까지의 준비, 이번 주의 경험 제안입니다.",
            "life", ("experiences", "cash", "spending", "stress", "goals", "follow_up", "requests"),
            ("emergency-reserve", "surplus-plan", "spending-room"),
            ("experience",), False, True, ("유동성", "순자산", "안심 소비 한도", "스트레스 테스트"),
            "the household's own month: income, spending, saving and investing transfers, what is left, the "
            "emergency reserve, and progress toward the dated goals (lease deposit, home move, retirement) on the "
            "household basis (sector_metrics.home_plan). Two private-banker questions belong here: does the "
            "household survive the composite stress scenario (sector_metrics.stress: how many months, what would "
            "help), and what may the client spend this month without slowing a goal (sector_metrics.spending_room). "
            "A reserve gap is filled by moving money between assets when sector_metrics.cash.reserve_swap says the "
            "sellable assets allow it, never by cutting the monthly surplus to nothing. Where there is room, suggest "
            "spending that keeps its value over time (health, learning, travel, art and performances, time with "
            "family) over goods that lose value, as a suggestion shaped by the preferences in client_memory, never an "
            "instruction and never invented tastes. Diagnose the month's balance of fixed costs and discretionary "
            "spending (sector_metrics.cash.spending_split) against the household's income and net worth before any "
            "figure. Then curate the experience dividend: from the brief's experience findings (exhibitions, books, "
            "places, dining, sport), write one or two `experiences` the client might enjoy this week, timely and of "
            "quality, matched to the preferences in client_memory, offered freely as choices, each with its refs. No "
            "market or policy news is researched for this theme; work from the household's numbers and the client's "
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
