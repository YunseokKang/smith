"""Holding tax on homes (requirement FR-53): the yearly 재산세 and 종합부동산세, computed in code.

Both taxes fall on whoever owns a home on 1 June (the assessment day) and are levied on its official price
(공시가격): 재산세 by the city in July and September, 종합부동산세 by the National Tax Service in December, per
person on the sum of that person's homes. Every rule below is the law in force for RULES_YEAR, an explicit constant
like the rest of the tax view (smith.tax); amounts are computed here and the narrative may explain them but never
produces new ones.

Left out, and named in `assumptions()` so the reader knows the figure is before them: the tax-base cap
(과세표준 상한제) and the burden caps (세부담상한), which need last year's base and can only lower the bill; the
building part of the regional resource tax (지역자원시설세), a few thousand won on a flat; local ordinance changes
to the urban-area rate.

A government reform of 종합부동산세 for 2027 onward (announced 2026-08-03, revised by the cabinet in September)
was still before the National Assembly on 2026-11-10 and its figures kept moving, so it is named, not computed
(PENDING); the law watch (FR-54) is what tells the report when it passes.

Unknown inputs stay None (never zero).
"""
import statistics
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from smith.fmt import percent, short_won

RULES_YEAR = 2026
RULES_CHECKED = "2026-11-15"
SOURCES = {
    "property": ("지방세법 제110조·제111조·제111조의2·제112조, 시행령 제109조(재산세)", "https://www.law.go.kr/법령/지방세법"),
    "education": ("지방세법 제151조(지방교육세)", "https://www.law.go.kr/법령/지방세법/제151조"),
    "comprehensive": ("종합부동산세법 제8조·제9조, 시행령 제2조의4·제4조의3", "https://www.law.go.kr/법령/종합부동산세법"),
    "rural": ("농어촌특별세법 제5조(종합부동산세액의 20%)", "https://www.law.go.kr/법령/농어촌특별세법/제5조"),
}

# --- 재산세 (local property tax on a home) ---------------------------------------------------------------
# The fair-market-value ratio turns the official price into the tax base. For a one-home household it is set per
# year by decree (2026: 43% up to 300M, 44% up to 600M, 45% above, whatever the price); otherwise 60%.
ONE_HOME_RATIO = ((Decimal(300_000_000), Decimal("0.43")), (Decimal(600_000_000), Decimal("0.44")),
                  (None, Decimal("0.45")))
GENERAL_RATIO = Decimal("0.60")
# (base upper bound, marginal rate, tax at the bracket's lower bound). Standard rates (제111조 ①3나), and the
# one-home special rates for an official price up to SPECIAL_PRICE_CAP (제111조의2), 0.05%p lower in every band.
STANDARD_RATES = ((Decimal(60_000_000), Decimal("0.001"), Decimal(0)),
                  (Decimal(150_000_000), Decimal("0.0015"), Decimal(60_000)),
                  (Decimal(300_000_000), Decimal("0.0025"), Decimal(195_000)),
                  (None, Decimal("0.004"), Decimal(570_000)))
SPECIAL_RATES = ((Decimal(60_000_000), Decimal("0.0005"), Decimal(0)),
                 (Decimal(150_000_000), Decimal("0.001"), Decimal(30_000)),
                 (Decimal(300_000_000), Decimal("0.002"), Decimal(120_000)),
                 (None, Decimal("0.0035"), Decimal(420_000)))
SPECIAL_PRICE_CAP = Decimal(900_000_000)
URBAN_RATE = Decimal("0.0014")      # 도시지역분 (제112조), on the same base.
EDUCATION_RATE = Decimal("0.20")    # 지방교육세 (제151조), on the property tax proper.
SINGLE_BILL_LIMIT = Decimal(200_000)  # At or below this the city may bill the whole year in July (제115조 ①3).

# --- 종합부동산세 (national tax on the sum of a person's homes) -----------------------------------------
ONE_HOME_DEDUCTION = Decimal(1_200_000_000)   # 1세대1주택자 (제8조 ①).
GENERAL_DEDUCTION = Decimal(900_000_000)
COMPREHENSIVE_RATIO = Decimal("0.60")         # 시행령 제2조의4, from 2023.
# (base upper bound, marginal rate, tax at the bracket's lower bound) for two homes or fewer, and three or more
# (heavier only above a 1.2 billion base).
RATES_UP_TO_TWO = ((Decimal(300_000_000), Decimal("0.005"), Decimal(0)),
                   (Decimal(600_000_000), Decimal("0.007"), Decimal(1_500_000)),
                   (Decimal(1_200_000_000), Decimal("0.010"), Decimal(3_600_000)),
                   (Decimal(2_500_000_000), Decimal("0.013"), Decimal(9_600_000)),
                   (Decimal(5_000_000_000), Decimal("0.015"), Decimal(26_500_000)),
                   (Decimal(9_400_000_000), Decimal("0.020"), Decimal(64_000_000)),
                   (None, Decimal("0.027"), Decimal(152_000_000)))
RATES_THREE_OR_MORE = ((Decimal(300_000_000), Decimal("0.005"), Decimal(0)),
                       (Decimal(600_000_000), Decimal("0.007"), Decimal(1_500_000)),
                       (Decimal(1_200_000_000), Decimal("0.010"), Decimal(3_600_000)),
                       (Decimal(2_500_000_000), Decimal("0.020"), Decimal(9_600_000)),
                       (Decimal(5_000_000_000), Decimal("0.030"), Decimal(35_600_000)),
                       (Decimal(9_400_000_000), Decimal("0.040"), Decimal(110_600_000)),
                       (None, Decimal("0.050"), Decimal(286_600_000)))
# The property tax already paid on the part of the value 종합부동산세 also taxes is credited (시행령 제4조의3): that
# part is the top slice of the property-tax base, taxed at the top standard rate. Checked against the National
# Assembly Budget Office's example (official price 2.07 billion, one home): 2,114,000 won, reproduced exactly.
OVERLAP_RATE = Decimal("0.004")
# One-home owners' credits on the tax after that credit (제9조 ⑥~⑧): age on 1 June and years held, 80% together.
AGE_CREDIT = ((70, Decimal("0.40")), (65, Decimal("0.30")), (60, Decimal("0.20")))
HOLDING_CREDIT = ((15, Decimal("0.50")), (10, Decimal("0.40")), (5, Decimal("0.20")))
CREDIT_CAP = Decimal("0.80")
RURAL_RATE = Decimal("0.20")        # 농어촌특별세 on the comprehensive tax.

# When the official price could not be read, it is estimated from the market price: the 2026 apartment prices were
# set at 69% of market value (the fourth year at that level, MOLIT announcement 2026-03-17).
REALIZATION_RATE = Decimal("0.69")

PENDING = ("종합부동산세 개편안(2027년부터 적용 예정)은 국회 심의 중입니다. 정부안은 실거주 1주택 공제를 14억 원으로 올리고 "
           "공정시장가액비율을 70%로 높이는 내용이며, 통과되면 이 추정이 달라집니다.")


def floor10(amount: Decimal) -> Decimal:
    """Tax bills drop amounts under 10 won."""
    return (amount / 10).to_integral_value(rounding=ROUND_FLOOR) * 10


def _progressive(base: Decimal, table: tuple[tuple[Decimal | None, Decimal, Decimal], ...]) -> Decimal:
    if base <= 0:
        return Decimal(0)
    lower = Decimal(0)
    for upper, rate, at_lower in table:
        if upper is None or base <= upper:
            return at_lower + (base - lower) * rate
        lower = upper
    raise AssertionError("unreachable: the last bracket is open")


def one_home_ratio(price: Decimal) -> Decimal:
    return next(rate for upper, rate in ONE_HOME_RATIO if upper is None or price <= upper)


def property_tax(price: Decimal, *, one_home: bool) -> dict[str, Any]:
    """재산세 on one home at `price` (official price), the whole home whoever owns it.

    `main` is the property tax proper (what the comprehensive tax credits), `total` what the July and September
    bills add up to: the tax proper, the urban-area part and the local education tax."""
    ratio = one_home_ratio(price) if one_home else GENERAL_RATIO
    base = price * ratio
    special = one_home and price <= SPECIAL_PRICE_CAP
    main = floor10(_progressive(base, SPECIAL_RATES if special else STANDARD_RATES))
    standard = _progressive(base, STANDARD_RATES)
    urban = floor10(base * URBAN_RATE)
    education = floor10(main * EDUCATION_RATE)
    return {"price": price, "ratio": ratio, "base": base, "special": special, "main": main,
            "standard": standard, "urban": urban, "education": education, "total": main + urban + education}


def comprehensive_tax(homes: list[tuple[Decimal, dict[str, Any]]], *, one_home: bool, age: int | None = None,
                      held_years: int | None = None) -> dict[str, Any]:
    """종합부동산세 of one person: `homes` are (the person's share of the official price, the property tax on the
    person's share). `one_home` is the 1세대1주택자 status (12억 deduction and the age and holding credits).

    Age or years held unknown (None) give no credit: the figure can then only be too high, never too low."""
    total_price = sum((price for price, _ in homes), Decimal(0))
    deduction = ONE_HOME_DEDUCTION if one_home else GENERAL_DEDUCTION
    base = max(Decimal(0), (total_price - deduction) * COMPREHENSIVE_RATIO)
    table = RATES_THREE_OR_MORE if len(homes) >= 3 else RATES_UP_TO_TWO
    computed = _progressive(base, table)
    overlap = Decimal(0)
    if base > 0:
        levied = sum((tax["main"] for _, tax in homes), Decimal(0))
        standard = sum((tax["standard"] for _, tax in homes), Decimal(0))
        ratios = {tax["ratio"] for _, tax in homes}
        # One ratio for the person's homes in practice (one home, or several taxed at the general ratio).
        ratio = max(ratios) if ratios else GENERAL_RATIO
        slice_tax = base * ratio * OVERLAP_RATE
        overlap = min(computed, levied * slice_tax / standard if standard else Decimal(0))
    after_overlap = computed - overlap
    credit_rate = Decimal(0)
    if one_home and after_overlap > 0:
        age_rate = next((r for at, r in AGE_CREDIT if age is not None and age >= at), Decimal(0))
        held_rate = next((r for at, r in HOLDING_CREDIT if held_years is not None and held_years >= at), Decimal(0))
        credit_rate = min(CREDIT_CAP, age_rate + held_rate)
    tax = floor10(after_overlap * (1 - credit_rate))
    rural = floor10(tax * RURAL_RATE)
    return {"price": total_price, "deduction": deduction, "base": base, "computed": computed, "overlap": overlap,
            "credit_rate": credit_rate, "tax": tax, "rural": rural, "total": tax + rural}


def schedule(main: Decimal, total: Decimal) -> list[tuple[int, Decimal]]:
    """(month, amount) of the property-tax bills: half in July and half in September, or the whole year in July
    when the tax proper is small enough for the city to bill it at once."""
    if total <= 0:
        return []
    if main <= SINGLE_BILL_LIMIT:
        return [(7, total)]
    first = floor10(total / 2)
    return [(7, first), (9, total - first)]


@dataclass(frozen=True)
class Home:
    """A home as the holding tax sees it. `price` is the official price, or the market price × REALIZATION_RATE when
    `estimated`; None when neither is known."""
    ref: str
    label: str
    owner: str
    price: Decimal | None
    estimated: bool
    basis: str
    acquired_year: int | None = None
    shares: dict[str, Decimal] = field(default_factory=dict)   # owner -> share; empty means `owner` holds it all.


def assessment_day(year: int) -> date:
    """The day whose owner pays the year's 재산세 and 종합부동산세 (과세기준일, 1 June)."""
    return date(year, 6, 1)


MARRIAGE_YEARS = 10   # 종부세법 시행령 제1조의2 ④: each spouse is their own household for ten years after the wedding.


def one_home_status(homes: list[Home], household: dict[str, Any] | None, *, year: int = RULES_YEAR
                    ) -> dict[str, dict[str, Any]]:
    """Per owner: whether the property tax and the comprehensive tax treat the owner as a one-home household on the
    year's assessment day, and what was left unchecked.

    Unmarried partners are separate households even at one address (a 동거인 is not family: 지방세법 시행령
    제110조의2, 종부세법 시행령 제1조의2), so each owner counts only their own homes. After registration the two form
    one household. For the comprehensive tax each spouse stays their own household for ten years from the wedding,
    read from `[household] marriage_date`; without that date the rule is not applied (the figure can then be too high,
    never too low) and `marriage_date_missing` says so. The property tax's marriage rule was not checked against the
    decree, so a married household with two homes pays the general rate there and `unchecked` says so. A jointly held
    only home is one home for the property tax, while each spouse pays the comprehensive tax on a share with the
    general deduction (the one-home choice for joint owners is the couple's to make)."""
    shares_of = {h.ref: h.shares or {h.owner: Decimal(1)} for h in homes}
    owners = sorted({o for shares in shares_of.values() for o in shares})
    count = {o: sum(1 for shares in shares_of.values() if o in shares) for o in owners}
    config = household or {}
    married = bool(config.get("marriage_registered"))
    wedding = config.get("marriage_date")
    if isinstance(wedding, str):
        try:
            wedding = date.fromisoformat(wedding)
        except ValueError:
            wedding = None
    wedding = wedding if isinstance(wedding, date) else None
    day = assessment_day(year)
    within = wedding is not None and wedding <= day < _years_after(wedding, MARRIAGE_YEARS)
    found = {}
    for owner in owners:
        if not married:
            found[owner] = {"property": count[owner] == 1, "comprehensive": count[owner] == 1, "unchecked": False,
                            "marriage_date_missing": False}
            continue
        only = len(homes) == 1
        joint = only and len(shares_of[homes[0].ref]) > 1
        if only:
            found[owner] = {"property": True, "comprehensive": not joint, "unchecked": False, "marriage_date_missing": False}
        else:
            found[owner] = {"property": False, "comprehensive": count[owner] == 1 and within, "unchecked": True,
                            "marriage_date_missing": count[owner] == 1 and wedding is None}
    return found


def _years_after(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year + years)
    except ValueError:   # 29 February
        return day.replace(year=day.year + years, day=28)


def _age(household: dict[str, Any] | None, owner: str, year: int) -> int | None:
    """Age on 1 June of `year`; None when the birth year or month is not configured."""
    config = household or {}
    prefix = "" if owner == "self" else "partner_"
    born_year, born_month = config.get(f"{prefix}birth_year"), config.get(f"{prefix}birth_month")
    if not isinstance(born_year, int) or not isinstance(born_month, int):
        return None
    return year - born_year - (1 if born_month > 6 else 0)


def household(homes: list[Home], household_config: dict[str, Any] | None, *, year: int = RULES_YEAR,
              projected: bool = False) -> dict[str, Any]:
    """The year's holding tax per home and per owner, and the household total. A home without a price makes its
    owner's figures, and the total, unknown (None) rather than partial. `projected`: the homes are today's, before
    the year's assessment day, so the figure assumes they are still held on 1 June."""
    status = one_home_status(homes, household_config, year=year)
    rows, per_owner_homes = [], {}
    for home in homes:
        shares = home.shares or {home.owner: Decimal(1)}
        one_home = all(status[o]["property"] for o in shares)
        tax = None if home.price is None else property_tax(home.price, one_home=one_home)
        rows.append({"ref": home.ref, "label": home.label, "owner": home.owner, "price": home.price,
                     "estimated": home.estimated, "basis": home.basis, "one_home": one_home, "property": tax,
                     "bills": [] if tax is None else schedule(tax["main"], tax["total"])})
        for owner, share in shares.items():
            per_owner_homes.setdefault(owner, []).append((home, share, tax))
    owners = {}
    for owner, held in per_owner_homes.items():
        known = all(tax is not None for _, _, tax in held)
        prop = sum((tax["total"] * share for _, share, tax in held), Decimal(0)) if known else None
        comp = None
        if known:
            parts = [(home.price * share, _share(tax, share)) for home, share, tax in held]
            years = [year - home.acquired_year - 1 for home, _, _ in held if home.acquired_year]
            comp = comprehensive_tax(parts, one_home=status[owner]["comprehensive"], age=_age(household_config, owner, year),
                                     held_years=min(years) if len(years) == len(held) else None)
        owners[owner] = {"property": None if prop is None else floor10(prop), "comprehensive": comp,
                         "total": None if comp is None else floor10(prop) + comp["total"],
                         "one_home": status[owner], "estimated": any(home.estimated for home, _, _ in held)}
    totals = [o["total"] for o in owners.values()]
    # A later year is computed on the rules checked for RULES_YEAR and says so: the one-home ratio is set per year.
    return {"year": year, "rules_year": RULES_YEAR, "projected": projected, "homes": rows, "owners": owners,
            "total": None if not totals or None in totals else sum(totals, Decimal(0)),
            "estimated": any(h.estimated for h in homes), "pending": PENDING}


def _share(tax: dict[str, Any], share: Decimal) -> dict[str, Any]:
    return {**tax, "main": tax["main"] * share, "standard": tax["standard"] * share}


def target(price: Decimal, *, estimated: bool = True) -> dict[str, Any]:
    """The yearly holding tax of a move-up target bought as the household's only home: one owner, or the couple
    half and half after registering the marriage (each then taxed on half with the general 9억 deduction, unless
    they choose the one-home treatment, which this does not pick for them). No credits: the household is under 60
    and the target newly bought."""
    prop = property_tax(price, one_home=True)
    single = comprehensive_tax([(price, prop)], one_home=True)
    half = _share(prop, Decimal("0.5"))
    joint_each = comprehensive_tax([(price / 2, half)], one_home=False)
    return {"price": price, "estimated": estimated, "property": prop["total"],
            "single": prop["total"] + single["total"], "single_comprehensive": single["total"],
            "joint": prop["total"] + 2 * joint_each["total"], "joint_comprehensive": 2 * joint_each["total"]}


def estimated_price(market_price: Decimal | None) -> Decimal | None:
    return None if market_price is None else floor10(market_price * REALIZATION_RATE)


def homes(properties: list[dict[str, Any]], household_config: dict[str, Any] | None, store: dict[str, Any], *,
          year: int = RULES_YEAR) -> list[Home]:
    """The holding tax's view of the real-estate sector's properties (record_id, label, owner, value, market): the
    official price stored for `year` when there is one, else an estimate from the recent trade median (enough deals)
    or the ledger value, labelled as such."""
    from smith import official_price
    from smith.realestate import MIN_BASIS

    config = (household_config or {}).get("properties") or {}
    found = []
    for prop in properties:
        ref = prop["record_id"]
        official, units = official_price.price_of(store, ref, year)
        market = prop.get("market") or {}
        monthly = market.get("monthly") or []
        near, near_count = _median_of(monthly, {f"{year - 1}-11", f"{year - 1}-12", f"{year}-01", f"{year}-02"})
        whole, whole_count = _median_of(monthly, None)
        if official is not None:
            price, estimated, basis = official, False, f"{year}년 공시가격(같은 면적 {units}세대 중간값)"
        elif near is not None and near_count >= MIN_BASIS:
            # The official price is set on the market of 1 January; trades around it beat today's market.
            price, estimated = estimated_price(near), True
            basis = f"1월 1일 앞뒤 실거래 {near_count}건 중간값의 {percent(REALIZATION_RATE, 0)}(공시가격 추정)"
        elif whole is not None and whole_count >= MIN_BASIS:
            price, estimated = estimated_price(whole), True
            basis = f"최근 1년 실거래 {whole_count}건 중간값의 {percent(REALIZATION_RATE, 0)}(공시가격 추정)"
        elif prop.get("value") is not None:
            price, estimated = estimated_price(prop["value"]), True
            basis = f"원장 평가액의 {percent(REALIZATION_RATE, 0)}(공시가격 추정)"
        else:
            price, estimated, basis = None, True, "가격 미상"
        acquired = (config.get(ref) or {}).get("acquired_year")
        if prop.get("held_assumed"):
            basis += " · 6월 1일 보유 여부를 몰라 보유한 것으로 계산"
        found.append(Home(ref=ref, label=prop["label"], owner=prop.get("owner") or "self", price=price,
                          estimated=estimated, basis=basis, acquired_year=acquired if isinstance(acquired, int) else None))
    return found


def _median_of(monthly: list[tuple[str, Decimal, int]], months: set[str] | None) -> tuple[Decimal | None, int]:
    """(median, count) of the trades in `months` (all when None) from the monthly medians, each month's median standing
    for its deals: around 1 January (November before to February) is the market the year's official price was set on."""
    prices = [median for month, median, count in monthly if months is None or month in months for _ in range(count)]
    return (Decimal(statistics.median(prices)), len(prices)) if prices else (None, 0)


def due_on(result: dict[str, Any]) -> dict[int, Decimal]:
    """Month -> the household's bills of the result's year (July and September property tax, December comprehensive
    tax), known amounts only."""
    months: dict[int, Decimal] = {}
    for row in result["homes"]:
        for month, amount in row["bills"]:
            months[month] = months.get(month, Decimal(0)) + amount
    december = [o["comprehensive"]["total"] for o in result["owners"].values() if o["comprehensive"]]
    if december and sum(december, Decimal(0)) > 0:
        months[12] = sum(december, Decimal(0))
    return months


def sources() -> list[tuple[str, str]]:
    return list(SOURCES.values())


def assumptions() -> list[str]:
    return [f"보유세 기준 {RULES_YEAR}년 현행법({RULES_CHECKED} 확인), 과세표준·세부담 상한 적용 전 금액",
            f"공시가격을 조회하지 못한 집은 실거래 중간값(1월 1일 앞뒤 넉 달, 3건 미만이면 최근 1년, 그것도 없으면 원장 평가액)의 "
            f"{percent(REALIZATION_RATE, 0)}(2026년 공시가격 시세 반영률)로 추정",
            "지역자원시설세(건축물분)는 빠져 있음(아파트 한 채에 몇천 원 수준)",
            "보유 연수는 취득 연도만으로 셈해 장기보유 공제를 적게 잡음(세액이 실제보다 클 수 있음)"]


def describe(result: dict[str, Any]) -> str:
    """One plain line for logs and the CLI."""
    total = result["total"]
    flag = "(추정)" if result["estimated"] else ""
    return f"{result['year']}년 보유세 {short_won(total)}{flag}"
