"""Retirement pension stack (report design §18.5): the household's pension pipelines side by side.

The retirement track used to count the pension accounts alone and call the result a readiness ratio. A
household with a national pension, an insurer's annuity, a pension-savings account and an IRP has four
pipelines, and the question the client asks is simpler: how much of the monthly retirement target do they
cover together, after tax, and from what age? This module answers it deterministically:

- a pipeline the client has stated (a `pension_income` cash flow with `pension_source`, amount in today's
  won, `start_date` the payout start) is used as entered;
- a pension account (pension_savings, IRP) without a stated payout is projected like the retirement track:
  balance plus contributions at the real return to the payout age, then the withdrawal rate;
- an insurer's annuity without a stated payout is projected from its surrender value plus the premiums still
  to be paid over the policy's payment term (the client keeps the policy to maturity, user statement
  2026-10-18), net of an assumed loading, against the product's guaranteed annuity base;
- the national pension cannot be estimated from the ledger: without a statement it is unknown and asked for.

The retirement age is not one number (user decision 2026-10-23): the stack is computed for each age in
RETIREMENT_TIERS. Contributions stop at retirement, private pension accounts pay from 55 at the earliest, the
national pension shrinks with the contribution months lost (its statement assumes paying until a stated month),
and each tier says how many years pass without any pension and what the household must hold outside the
pensions at retirement to reach the target (`outside_need`). The headline figures are the tier of
[household] retirement_age, or DEFAULT_PAYOUT_AGE when it is not set.

The stack is the household's (user decision 2026-11-08): every member with cash flows or a pension account in
the ledger (`owners`) gets the same pipelines from their own records and birth date ([household]
partner_birth_year/partner_birth_month for `partner`), retires at the same age in every tier, and the rows are
added against the one household target. Tax is applied per member, since each is a taxpayer. Every row carries
`owner`, a unique `id` (the pipeline key, suffixed with the owner for anyone but the client) and, when the stack
holds more than one member, an `owner_label` the letter appends to the pipeline name (`row_label`). Ages are the
client's: a member born in another year has every age in their rows shifted onto the client's age axis (`_shift`,
own ages kept in `own_start_age`/`own_end_age`), so the totals, the age chart and the gap outside the pensions add
what is paid in the same year. A younger member's pay in the years between the client's retirement and their own
is not counted (the stack never counts pay).

Future contributions follow the split that earns the full pension credit (tax.credit_split): the same report
proposes it, so the stack never assumes the opposite (review 2026-10-23); the part no credit covers is its own
row (an ISA or similar account). Contributions no credit covered come back tax-free, so a payout is taxed only on
its taxable share.

Every tax factor is an assumption echoed in the result. An unknown pipeline makes the
total a lower bound, never zero.
"""
from datetime import date
from decimal import Decimal
from typing import Any

from smith.payload import LedgerView, base_amount
from smith.records import Kind
from smith.summary import recurring_active
from smith.tax import PENSION_SAVINGS_LIMIT, PENSION_TOTAL_LIMIT, REAL_RETURN, WITHDRAWAL_RATE

# The headline retirement age when none is set: 55 (user decision 2026-11-07, "그래프는 55세 은퇴 기준"; was 60).
DEFAULT_PAYOUT_AGE = 55
RETIREMENT_TIERS = (50, 55, 60)   # User request 2026-10-23: never assume one retirement age.
PIPELINES = ("national", "insurance_annuity", "pension_savings", "irp")  # Always shown, in this order.
LABELS = {"national": "국민연금", "insurance_annuity": "변액·연금보험", "pension_savings": "연금저축", "irp": "IRP",
          "occupational": "퇴직연금", "other": "기타 연금", "isa": "ISA 등(공제 밖 납입)"}
# The contributions no pension credit covers, kept in an ISA or a similar account: drawn like the pension accounts
# but from the retirement age, gains taxed at the ISA's separate 9.9% (its tax-free allowance ignored: lower bound).
ISA_GAINS_TAX = Decimal("0.099")
NATIONAL_MIN_MONTHS = 120         # Fewer contribution months give no old-age pension (a lump-sum refund instead).
_ACCOUNTS = {"pension_savings": "pension_savings", "irp": "irp"}
# Assumed tax on the payout (share of the gross monthly figure). National pension income is taxed after the
# pension-income deduction and comes out small; private pensions paid as an annuity are taxed at 5.5% (55-69)
# while the year's private payouts stay within the separate-taxation limit, 16.5% above it; an insurer's
# annuity kept ten years within the premium limits is tax-free. A pension paid from severance pay (an employer DB or
# DC plan, moved to an IRP) is taxed as retirement income at 70% of its retirement-income tax (60% after ten years),
# outside the private-pension limit: the employer's own statement puts that tax near 5% (2026-11-07), so 3.5%.
TAX_FACTORS = {"national": Decimal("0.03"), "insurance_annuity": Decimal(0), "pension_savings": Decimal("0.055"),
               "irp": Decimal("0.055"), "occupational": Decimal("0.035"), "other": Decimal("0.055")}
PRIVATE_SEPARATE_LIMIT = Decimal(15_000_000)   # Yearly private pension payouts above this lose the low rate.
# The insurer's annuity: a premium of at least this a month with a fixed payment
# term (end_date) is the policy's premium; ANNUITY_LOADING of each premium is assumed to go to charges and the
# rest to the fund; the product guarantees an annuity base of the premiums paid plus simple interest at
# GUARANTEE_SIMPLE_RATE a year until commencement (secondary sources, 2026-10-18; 약관 확인 필요), which is a
# nominal figure deflated at INFLATION into today's won before it is compared with the fund projection.
ANNUITY_PREMIUM_MIN = Decimal(500_000)
ANNUITY_LOADING = Decimal("0.10")
GUARANTEE_SIMPLE_RATE = Decimal("0.05")
INFLATION = Decimal("0.02")
PRIVATE_HIGH_TAX = Decimal("0.165")
_PRIVATE = ("pension_savings", "irp", "other")
# National pension eligibility age by birth year (국민연금법 부칙): 61 for 1953-56 ... 65 from 1969.
_NATIONAL_AGES = ((1952, 60), (1956, 61), (1960, 62), (1964, 63), (1968, 64))
PRIVATE_EARLIEST_AGE = 55
_MONTHS = {"monthly": 1, "quarterly": 3, "annual": 12}
TIMELINE_AGES = range(55, 91)
# The five pension families the letter groups every pipeline under (user request 2026-11-07), each pipeline in
# exactly one: the IRP is a retirement pension by law (근로자퇴직급여 보장법) even though it shares the pension-account
# tax credit with the pension savings, and an insurer's annuity kept ten years is the tax-free family.
CATEGORIES = (("public", "공적연금"), ("occupational", "퇴직연금"), ("personal", "개인연금"),
              ("tax_free", "비과세 연금(보험)"), ("property", "부동산 연금"))
CATEGORY_OF = {"national": "public", "occupational": "occupational", "irp": "occupational",
               "pension_savings": "personal", "isa": "personal", "other": "personal", "insurance_annuity": "tax_free"}
# Products of each family the household does not hold, named so the table shows the whole map.
CATEGORY_EXAMPLES = {"public": "국민연금·공무원연금 등", "occupational": "DB형·DC형·IRP", "personal": "연금저축·ISA 등",
                     "tax_free": "변액연금보험·톤틴연금 등", "property": "주택연금·농지연금"}
# A pension account with no payout end is paid over this many years: the public pension portal's assumption
# for an account without a set period, used so Smith and the portal read the same (user decision 2026-11-07).
ACCOUNT_PAYOUT_YEARS = 20
# The age a fixed won annuity's erosion is followed to before the shortfall is held level (outside_need).
PLAN_AGE = 100
OCCUPATIONAL_PLANS = {"db": "DB형", "dc": "DC형"}
_TERM_FIELDS = ("institution", "opened_on", "contribution_end", "payout_start", "payout_end", "illustrated_annual_pension")
OWNER_LABELS = {"self": "본인", "partner": "배우자"}
# The local setting holding each member's birth year: only the client and the spouse have one ([household]).
_BIRTH_KEYS = {"self": "birth_year", "partner": "partner_birth_year"}


def owner_label(owner: str) -> str:
    """A member's role word for the letter and the model: never the ledger's owner id, which is free text."""
    return OWNER_LABELS.get(owner, "가구 구성원")
# An owner joins the stack with any cash flow or an account of these types; property alone adds nothing to ask about.
_PENSION_ACCOUNT_TYPES = ("pension_savings", "irp", "dc", "insurance")


def owners(view: LedgerView) -> list[str]:
    """The household members whose pensions the stack adds up: the client first, then every other owner with a
    cash flow or a pension-type account in the ledger (user decision 2026-11-08)."""
    found = {"self"}
    for record in view.records:
        if record.kind is Kind.CASHFLOW or (record.kind is Kind.ASSET
                                            and record.fields.get("account_type") in _PENSION_ACCOUNT_TYPES):
            found.add(record.owner_id)
    return ["self"] + sorted(found - {"self"})


def row_label(row: dict[str, Any]) -> str:
    """The pipeline's name, with the member's role word when the stack holds more than one ("국민연금(배우자)")."""
    who = row.get("owner_label")
    return row["label"] if not who else f"{row['label']}({who})"


def stack(view: LedgerView, household: dict[str, Any] | None, today: date,
          plan: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The pension stack against the monthly retirement target; None without a target (the track asks).

    `plan` is tax.pension_plan: this year's contributions above the credit (tax-free when drawn) and the split
    future contributions follow. Without it the current standing transfers are projected as they are.

    Keys: target, retirement_age (the headline tier), retirement_age_set, payout_age, age (the client's), owners
    (owner, label, age per member), tiers (one per age: retire_age, pipelines, total_net, ratio, unknown (row ids),
    first_income_age, years_without_pension, all_on_age, outside_need), and the headline tier's pipelines (key, id,
    owner, owner_label, label, start_age, gross_monthly, net_monthly, basis stated/estimated/unknown, basis_text,
    missing, taxable_share), total_net (known pipelines only), ratio, unknown, complete; contributions (the client's
    projected split and the current one), current_split (the headline tier's client pension-account rows with the
    current split, when the two differ), assumptions.
    """
    household = household or {}
    target = household.get("retirement_monthly_spend")
    if not target:
        return None
    target = Decimal(target)
    birth = born(household)
    age = None if birth is None else age_on(birth, today)
    chosen = household.get("retirement_age")
    headline = int(chosen) if chosen else DEFAULT_PAYOUT_AGE
    kind = household.get("occupational_plan")
    start = household.get("occupational_service_start")
    occupational = ({"type": kind, "institution": household.get("occupational_institution"),
                     "wage": household.get("occupational_average_wage"),
                     "service_start": date.fromisoformat(start) if isinstance(start, str) else start}
                    if kind in OCCUPATIONAL_PLANS else None)
    # One entry per household member: their own statements, accounts, contributions and birth date. The credit
    # split (`plan`) and the employer plan named in the local settings are the client's.
    people = []
    for owner in owners(view):
        member_birth = born(household, owner)
        people.append({"owner": owner, "label": owner_label(owner), "birth": member_birth,
                       "age": None if member_birth is None else age_on(member_birth, today),
                       "stated": _stated(view, today, owner),
                       "contributions": _contribution_plan(view, today, plan if owner == "self" else None, owner),
                       "occupational": occupational if owner == "self" else None})
    contributions = people[0]["contributions"]
    tiers = [_tier(view, people, retire=retire, target=target, today=today)
             for retire in sorted(set(RETIREMENT_TIERS) | {headline})]
    head = next(t for t in tiers if t["retire_age"] == headline)
    current = None
    if contributions["changed"]:
        as_now = dict(contributions, **contributions["current"], isa=Decimal(0))
        swapped = [dict(p, contributions=as_now) if p["owner"] == "self" else p for p in people]
        row = _tier(view, swapped, retire=headline, target=target, today=today)
        current = {"total_net": row["total_net"], "ratio": row["ratio"],
                   "pipelines": [p for p in row["pipelines"] if p["key"] in _ACCOUNTS and p["owner"] == "self"]}
    return {"target": target, "retirement_age": headline, "retirement_age_set": bool(chosen),
            # Private pensions pay from 55 at the earliest, whatever the retirement age (review 2026-10-18).
            "payout_age": max(headline, PRIVATE_EARLIEST_AGE), "age": age, "tiers": tiers,
            "owners": [{"owner": p["owner"], "label": p["label"], "age": p["age"]} for p in people],
            "pipelines": head["pipelines"], "total_net": head["total_net"], "ratio": head["ratio"],
            "first_end_age": head["first_end_age"], "later_age": head["later_age"], "later_net": head["later_net"],
            "later_ratio": head["later_ratio"],
            "unknown": head["unknown"], "complete": not head["unknown"], "contributions": contributions,
            "current_split": current, "timeline": timeline(head), "assumptions": assumptions(shared=len(people) > 1)}


def timeline(tier: dict[str, Any], ages: range = TIMELINE_AGES) -> list[dict[str, Any]]:
    """The yearly after-tax payout at each age of one retirement tier, in today's won, per pipeline, so the letter
    can draw the public pension portal's age-by-age chart (중앙노후준비지원센터 '내연금'). A pipeline counts from its
    start age to the age before its end (none: for life) at its monthly run rate times twelve (no partial first
    year). A stated pipeline of several records adds each record's share from its own start age; a nominal one
    (an insurer's fixed won amount, no inflation increase in its terms) loses INFLATION a year after its start, as
    the same won buys less (user review 2026-11-07). Unknown pipelines are left out.

    Rows: age, parts ({pipeline row id: yearly amount}, every known pipeline present), total."""
    known = [p for p in tier["pipelines"] if p["net_monthly"] is not None and p["start_age"] is not None]
    rows = []
    for age in ages:
        parts = {p["id"]: monthly_at(p, age) * 12 for p in known}
        rows.append({"age": age, "parts": parts, "total": sum(parts.values(), Decimal(0))})
    return rows


def monthly_at(row: dict[str, Any], age: int) -> Decimal:
    """A known pipeline's after-tax monthly payout at `age` in today's won: nothing before its start or from its
    end age, each stated record from its own start, and a nominal (fixed won) record less INFLATION a year after
    it starts. The one reading the totals, the gap outside the pensions and the age chart share (Codex review
    2026-11-07)."""
    if row["net_monthly"] is None or row["start_age"] is None:
        return Decimal(0)
    total = Decimal(0)
    for start, share, erode_from, end in segments(row):
        if start is not None and start <= age and (end is None or age < end):
            base = _net_at(row, age)
            years = 0 if erode_from is None else max(0, age - erode_from)
            total += base * share / (1 + INFLATION) ** years
    return total


def _net_at(row: dict[str, Any], age: int) -> Decimal:
    """The after-tax monthly figure at `age`: a private pension is taxed at the high rate only in the years the
    household's taxable private payouts pass the separate-taxation limit (Codex review 2026-11-07)."""
    if "net_low" not in row:
        return row["net_monthly"]
    return row["net_high"] if age in row["high_ages"] else row["net_low"]


def segments(row: dict[str, Any]) -> list[tuple[int | None, Decimal, int | None, int | None]]:
    """(start age, share of the monthly figure, the age a nominal part starts losing value from or None for a
    today's-won part, end age or None for life) of each part of a pipeline: one per stated record, or the whole
    pipeline from its start to its end."""
    return row.get("start_shares") or [(row["start_age"], Decimal(1), None, row.get("end_age"))]


def _tier(view: LedgerView, people: list[dict[str, Any]], *, retire: int, target: Decimal, today: date) -> dict[str, Any]:
    """Every pipeline of every household member for one retirement age (all retire together, user decision
    2026-11-08), the after-tax total once all have started and the readiness ratio against the household target,
    the years from retirement to the first pension, and what must be held outside the pensions at retirement."""
    rows = []
    client = people[0]["birth"]
    for person in people:
        mine = _rows(view, person, retire=retire, today=today)
        if client is None and person["owner"] != "self":
            # Without the client's birth year there is no household axis to put a member's ages on: their payouts
            # are unknown, never read as if both were the same age (Codex review 2026-11-08).
            for row in mine:
                _unplaceable(row, f"{owner_label('self')} 출생 연도(로컬 설정 [household] {_BIRTH_KEYS['self']})",
                             "고객님 출생 연도가 없어 가구의 나이 축에 놓을 수 없음")
        _apply_tax(mine)   # Each member is a taxpayer: the separate-taxation limit counts their own payouts.
        # One time axis for the household: a member's ages become the client's age in the same year (Codex review
        # 2026-11-08: a younger spouse's pension must not be added from the client's 65th year).
        offset = 0 if client is None or person["birth"] is None else person["birth"][0] - client[0]
        for row in mine:
            _shift(row, offset)
        rows += mine
    shared = len(people) > 1
    for row in rows:
        row.update(category=CATEGORY_OF.get(row["key"], "personal"),
                   id=row["key"] if row["owner"] == "self" else f"{row['key']}@{row['owner']}",
                   owner_label=owner_label(row["owner"]) if shared else None)
        row.setdefault("products", products(view, row["key"], row["owner"]))
    known = [r for r in rows if r["net_monthly"] is not None]
    # Every part's own start and end counts: a later payout of the same pipeline starts at its own age.
    starts = [start for r in known for start, _, _, _ in segments(r) if start is not None]
    all_on = max(starts, default=None)
    # Once every pension has started, and once the fixed-term ones have ended (what the lifelong ones leave then).
    total = sum((monthly_at(r, all_on) if all_on is not None else r["net_monthly"] for r in known), Decimal(0))
    ends = [end for r in known for _, _, _, end in segments(r) if end is not None]
    later_age = max(ends, default=None)
    later = None if later_age is None else sum((monthly_at(r, later_age) for r in known), Decimal(0))
    # The ISA row is savings drawn from retirement, not a pension: the years without a pension ignore it.
    first = min((r["start_age"] for r in rows if r["key"] != "isa" and r["net_monthly"] is not None
                 and r["start_age"] is not None), default=None)
    return {"retire_age": retire, "pipelines": rows, "total_net": total, "ratio": total / target,
            "first_end_age": min(ends, default=None), "later_age": later_age, "later_net": later,
            "later_ratio": None if later is None else later / target,
            "unknown": [r["id"] for r in rows if r["net_monthly"] is None], "first_income_age": first,
            "years_without_pension": None if first is None else max(0, first - retire),
            "all_on_age": all_on, "outside_need": _outside_need(rows, target, retire)}


def _rows(view: LedgerView, person: dict[str, Any], *, retire: int, today: date) -> list[dict[str, Any]]:
    """One member's pipelines for a retirement age: the four always shown, the ISA row when the credit split
    leaves contributions outside it, any other stated pipeline, and the employer plan named in the settings."""
    stated, contributions, owner = person["stated"], person["contributions"], person["owner"]
    args = dict(age=person["age"], birth=person["birth"], retire=retire, contributions=contributions, today=today,
                owner=owner)
    # The client's four pipelines are always shown (a missing one is asked for); another member gets the national
    # pension (everyone employed has one to state) and only the accounts and policies they actually hold, so a
    # product they do not have is not an unknown that turns the household total into a lower bound.
    keys = [key for key in PIPELINES
            if owner == "self" or key == "national" or stated.get(key) or _held(view, key, owner)]
    rows = [_pipeline(view, key, stated.get(key, []), **args) for key in keys]
    if contributions["isa"]:
        rows.append(_isa(contributions, age=person["age"], retire=retire))
    rows += [_pipeline(view, key, records, **args) for key, records in sorted(stated.items()) if key not in PIPELINES]
    if person["occupational"] and "occupational" not in stated:
        rows.append(_employer_plan(person["occupational"], retire=retire, birth=person["birth"], today=today))
    for row in rows:
        row["owner"] = owner
        # A payout with no age to pay from cannot be added on the household's time axis: it is unknown, not a figure
        # the totals silently leave out (Codex review 2026-11-08: the stack read as complete without it).
        if row["start_age"] is None:
            # The text reaches the model (narrative pension_stack.missing): a role word and a fixed setting key only,
            # never the ledger's owner id (Codex review 2026-11-08).
            key = _BIRTH_KEYS.get(owner)
            _unplaceable(row, f"{owner_label(owner)} 출생 연도(로컬 설정{f' [household] {key}' if key else ''})",
                         "출생 연도가 없어 받는 나이를 정할 수 없음")
    return rows


def _unplaceable(row: dict[str, Any], missing: str, why: str) -> None:
    """Turn a payout that cannot be put on the household's age axis into an unknown one (a lower bound that asks
    for `missing`), so no total silently leaves it out. A row already unknown is left as it is."""
    if row["gross_monthly"] is None and row["net_monthly"] is None:
        return
    row.update(gross_monthly=None, net_monthly=None, nominal_monthly=None, stated_net=False, basis="unknown",
               basis_text=f"{row['basis_text']} · {why}", missing=missing)


def _employer_plan(plan: dict[str, Any], *, retire: int, birth: tuple[int, int | None] | None,
                   today: date) -> dict[str, Any]:
    """The employer's DB or DC plan the client named in the local settings (the public portal shows a DB plan
    with no figure: its provider does not report it). A DB plan pays a month of average wage per year of service
    (근로자퇴직급여 보장법): with the employer's average monthly wage and the service start, the severance at each
    retirement age is that wage, held in today's won, times the service months; it moves to an IRP and is paid
    from max(retirement, 55) over ACCOUNT_PAYOUT_YEARS like the other accounts, growing at the real return while it
    waits. Without those figures (or for a DC plan, whose balance is the client's) it is unknown: a lower bound."""
    kind = OCCUPATIONAL_PLANS[plan["type"]]
    start = plan.get("service_start")
    row = {"key": "occupational", "label": f"퇴직연금({kind})", "start_age": None, "gross_monthly": None,
           "net_monthly": None, "assets_now": None, "monthly_contribution": None, "basis": "unknown",
           "basis_text": "회사가 운용하는 퇴직연금이라 금융사가 금액을 알려 주지 않음(정부 조회 0원)",
           "missing": "회사 인사팀의 퇴직금 추계액, 또는 입사일과 최근 3개월 월평균 임금",
           "taxable_share": Decimal(1), "retire_age": retire,
           "products": [{"institution": plan.get("institution"), "name": f"{kind} 퇴직연금",
                         "opened_on": None if start is None else start.isoformat(), "opened_label": "입사"}]}
    wage = plan.get("wage")
    if plan["type"] != "db" or wage is None or start is None or birth is None:
        return row
    wage = Decimal(wage)
    leave = date(birth[0] + retire, birth[1] or 1, 1)
    months, now_months = _service_months(start, leave), _service_months(start, today)
    severance = wage * months / 12
    payout = max(retire, PRIVATE_EARLIEST_AGE)
    balance = severance * (1 + REAL_RETURN) ** (payout - retire)
    payment = REAL_RETURN / (1 - (1 + REAL_RETURN) ** -ACCOUNT_PAYOUT_YEARS)
    row["products"][0]["now_estimate"] = wage * now_months / 12
    row.update(basis="estimated", missing="", start_age=payout, end_age=payout + ACCOUNT_PAYOUT_YEARS,
               payout_years=ACCOUNT_PAYOUT_YEARS, gross_monthly=balance * payment / 12, severance=severance,
               service_months=months,
               basis_text=f"회사 조회 평균임금 월 {wage / 10_000:,.0f}만 원(오늘 돈 가치로 유지)에 {retire}세 퇴직까지 근속 "
                          f"{months // 12}년 {months % 12}개월을 곱한 퇴직금 약 {severance / 100_000_000:,.1f}억 원을 IRP로 옮겨 "
                          f"{payout}세부터 {ACCOUNT_PAYOUT_YEARS}년간 실질 연 {REAL_RETURN * 100:.0f}%로 나눠 받는다고 본 추정")
    return row


def _shift(row: dict[str, Any], years: int) -> None:
    """Move a member's row onto the client's age axis: the member is `years` younger (positive) or older, so every
    age in the row (start, end, each stated part, the erosion start of a nominal part, the high-tax ages) moves by
    that much. The member's own start and end ages stay in `own_start_age`/`own_end_age` for the table."""
    row["own_start_age"], row["own_end_age"], row["age_offset"] = row["start_age"], row.get("end_age"), years
    if not years:
        return
    for key in ("start_age", "end_age"):
        if row.get(key) is not None:
            row[key] += years
    if row.get("start_shares"):
        row["start_shares"] = [(None if start is None else start + years, share, None if erode is None else erode + years,
                                None if end is None else end + years) for start, share, erode, end in row["start_shares"]]
    if "high_ages" in row:
        row["high_ages"] = frozenset(age + years for age in row["high_ages"])


def _held(view: LedgerView, key: str, owner: str) -> bool:
    """Whether a member holds the product behind a pipeline: an account of its type, or an annuity policy."""
    if key in _ACCOUNTS:
        return _has_account(view, _ACCOUNTS[key], owner)
    if key == "insurance_annuity":
        return any(r.kind is Kind.ASSET and r.owner_id == owner and r.fields["category"] == "insurance_surrender_value"
                   for r in view.records)
    return False


def _service_months(start: date, end: date) -> int:
    """Whole months of service from `start` to `end` (a month counts once its day is reached)."""
    return max(0, (end.year - start.year) * 12 + end.month - start.month - (end.day < start.day))


def _outside_need(rows: list[dict[str, Any]], target: Decimal, retire: int) -> dict[str, Decimal] | None:
    """What the household must hold outside the pensions at retirement, in today's won, to live on the target:
    each year's shortfall until the last pipeline starts or a fixed-term one ends (`bridge`), and the lasting
    shortfall after that drawn at the withdrawal rate (`lasting`), both discounted to the retirement age at the
    real return. Every stated part's own start and end bounds the years walked, and with a fixed won annuity the
    walk goes on to PLAN_AGE, since its value keeps falling (Codex review 2026-11-07). None when a known pipeline
    has no start age (the timeline is unknown). With a pipeline whose payout is unknown the figure counts it as
    nothing and is only an upper bound (`upper_bound`, review 2026-10-23: unknown is not zero)."""
    known = [r for r in rows if r["net_monthly"] is not None]
    if any(r["start_age"] is None for r in known):
        return None
    parts = [part for r in known for part in segments(r)]
    last = max([retire] + [start for start, _, _, _ in parts if start is not None]
               + [end for _, _, _, end in parts if end is not None])
    # A fixed won annuity keeps losing value: follow the years to PLAN_AGE before the income is held level.
    if any(erode is not None for _, _, erode, _ in parts):
        last = max(last, PLAN_AGE)
    bridge = Decimal(0)
    for year in range(retire, last):
        income = sum((monthly_at(r, year) for r in known), Decimal(0))
        bridge += max(Decimal(0), target - income) * 12 / (1 + REAL_RETURN) ** (year - retire)
    gap = max(Decimal(0), target - sum((monthly_at(r, last) for r in known), Decimal(0)))
    lasting = gap * 12 / WITHDRAWAL_RATE / (1 + REAL_RETURN) ** (last - retire)
    return {"bridge": bridge, "lasting": lasting, "total": bridge + lasting,
            "upper_bound": len(known) < len(rows), "unknown": [r["id"] for r in rows if r["net_monthly"] is None]}


def _contribution_plan(view: LedgerView, today: date, plan: dict[str, Any] | None, owner: str = "self") -> dict[str, Any]:
    """Monthly contributions the projection uses: the credit split of `plan` (the report proposes it) or, without a
    plan, the owner's current standing transfers; this year's contributions no credit covers (tax-free when drawn)."""
    current = {"pension_savings": _contributions(view, "pension_savings", today, owner),
               "irp": _contributions(view, "irp", today, owner)}
    found = {**current, "isa": Decimal(0), "current": current, "changed": False,
             "nontaxed_now": {"pension_savings": Decimal(0), "irp": Decimal(0)}}
    if plan is None or None in current.values():
        return found
    found.update(pension_savings=plan["next_savings"], irp=plan["next_irp"], isa=plan["next_rest"],
                 changed=(plan["next_savings"], plan["next_irp"]) != (current["pension_savings"], current["irp"]),
                 nontaxed_now={"pension_savings": plan["savings_excess"], "irp": plan["excess"] - plan["savings_excess"]})
    return found


def _uncredited_yearly(contributions: dict[str, Any]) -> dict[str, Decimal]:
    """The yearly contributions of a standing split that no credit covers, by account (pension savings first). An
    account whose contribution could not be converted (None) leaves what depends on it at nothing uncredited, so
    that payout is taxed in full (a lower bound), instead of failing the whole report (Codex review 2026-11-08)."""
    savings, irp = contributions["pension_savings"], contributions["irp"]
    found = {"pension_savings": Decimal(0) if savings is None else max(Decimal(0), savings * 12 - PENSION_SAVINGS_LIMIT),
             "irp": Decimal(0)}
    if savings is not None and irp is not None:
        room = max(Decimal(0), PENSION_TOTAL_LIMIT - min(savings * 12, PENSION_SAVINGS_LIMIT))
        found["irp"] = max(Decimal(0), irp * 12 - room)
    return found


def _stated(view: LedgerView, today: date, owner: str = "self") -> dict[str, list[Any]]:
    """One member's pension_income flows by pipeline (a future start is a stated payout, an ended one is
    not); flows without a source count as "other"."""
    found: dict[str, list[Any]] = {}
    for record in view.records:
        f = record.fields
        if record.kind is not Kind.CASHFLOW or f["category"] != "pension_income" or record.owner_id != owner:
            continue
        if f.get("end_date") and f["end_date"] < today.isoformat():
            continue
        found.setdefault(f.get("pension_source") or "other", []).append(record)
    return found


def _pipeline(view: LedgerView, key: str, records: list[Any], *, age: int | None, birth: Any, retire: int,
              contributions: dict[str, Any], today: date, owner: str = "self") -> dict[str, Any]:
    label = LABELS.get(key, key)
    payout_age = max(retire, PRIVATE_EARLIEST_AGE)
    row = {"key": key, "label": label, "start_age": None, "gross_monthly": None, "net_monthly": None,
           "assets_now": None, "monthly_contribution": None, "basis": "unknown", "basis_text": "", "missing": "",
           "taxable_share": Decimal(1)}
    if records:
        amounts = [base_amount(view, r) for r in records]
        if any(a is None for a in amounts):
            row.update(basis_text="예상 수령액을 원화로 환산하지 못함", missing="그 통화의 환율")
            return row
        monthly = Decimal(0)
        nominal = False
        nominal_monthly = Decimal(0)   # The statement's own figure before deflation, for the table.
        starts = [date.fromisoformat(r.fields["start_date"]) for r in records]
        parts = []                     # (start date, today's-won monthly part) per record, for the age chart.
        for amount, record, start in zip(amounts, records, starts):
            part = amount / _MONTHS.get(record.fields["frequency"], 1)
            nominal_monthly += part
            if record.fields.get("price_basis") == "nominal":
                # An insurer's illustration is the amount of the first payment year: bring it to today's won so
                # it stacks with the national pension statement and the real-return projections.
                years = Decimal(max(0, (start.year - today.year) * 12 + start.month - today.month)) / 12
                part = part / (1 + INFLATION) ** years
                nominal = True
            monthly += part
            parts.append((start, part, record.fields.get("price_basis") == "nominal"))
        start_year = min(starts).year
        # Each record pays from its own start: the chart adds a later one only from its age (Codex review 2026-11-07).
        # A nominal payout stays the same won amount, so in today's won it shrinks by inflation after it starts.
        ends = [None if birth is None or not r.fields.get("end_date")
                else age_on(birth, date.fromisoformat(r.fields["end_date"])) + 1 for r in records]
        # A nominal part is deflated to today's won up to its start (or today, if it already pays): it erodes from
        # the later of the two (Codex review 2026-11-07: a running annuity is not deflated for past years twice).
        shares = []
        for (start, part, fixed), end in zip(parts, ends):
            start_age = None if birth is None else age_on(birth, start)
            erode = None if not fixed or start_age is None else max(start_age, age if age is not None else start_age)
            shares.append((start_age, part / monthly if monthly else Decimal(0), erode, end))
        end_age = None if None in ends else max(ends)
        row.update(basis="stated", start_age=None if birth is None else age_on(birth, min(starts)), start_year=start_year,
                   start_date=min(starts), nominal_deflated=nominal, nominal_monthly=nominal_monthly if nominal else None,
                   start_shares=shares, end_age=end_age)
        value_note = f"명목 금액을 물가 {INFLATION * 100:.0f}%로 현재 가치 환산" if nominal else "현재 가치"
        if key == "national":
            scale, note = _national_scale(records, retire=retire, birth=birth)
            monthly *= scale
            value_note += note
            row.update(national_scale=scale)
        # An after-tax statement figure (the fund's own estimate) is used as the net payout, untouched.
        if all(r.fields.get("amount_basis") == "net" for r in records):
            row.update(net_monthly=monthly, gross_monthly=None, tax_factor=None, stated_net=True,
                       basis_text=f"알려 주신 예상 수령액(세후, {value_note})")
        else:
            row.update(gross_monthly=monthly, basis_text=f"알려 주신 예상 수령액(세전, {value_note})")
        return row
    if key == "national":
        row.update(start_age=None if birth is None else national_pension_age(birth[0]),
                   basis_text="예상 수령액이 입력되지 않음",
                   missing="국민연금공단 '내 연금 알아보기'의 예상 월 수령액(현재 가치)과 수령 개시 연령")
        return row
    if key in _ACCOUNTS:
        assets = _assets(view, lambda f: f.get("account_type") == _ACCOUNTS[key], owner)
        contribution = contributions[key]
        row.update(assets_now=assets, monthly_contribution=contribution)
        if assets is None or contribution is None:
            row.update(basis_text="잔액이나 납입액을 원화로 환산하지 못함", missing="그 통화의 환율")
            return row
        if not _has_account(view, _ACCOUNTS[key], owner):
            row.update(basis_text="계좌가 없음", missing=f"{label} 계좌가 있다면 잔액과 월 납입액")
            return row
        terms = _terms(products(view, key, owner))
        stop = None if age is None or terms["contribution_end"] is None else age_on(birth, terms["contribution_end"])
        row = _project(row, assets, contribution, age=age, retire=retire, start=payout_age,
                       nontaxed_now=contributions["nontaxed_now"][key],
                       uncredited_yearly=_uncredited_yearly(contributions)[key],
                       years=terms["years"], contribution_stop=stop)
        if terms["mixed"]:
            row["basis_text"] += ". 수령 기간이나 납입 종료일이 다른 계좌가 섞여 가장 긴 기간과 가장 늦은 종료일로 계산"
        return row
    if key == "insurance_annuity":
        assets = _assets(view, lambda f: f["category"] == "insurance_surrender_value", owner)
        if assets is None:
            row.update(basis_text="해약환급금을 원화로 환산하지 못함", missing="그 통화의 환율")
            return row
        if not any(r.kind is Kind.ASSET and r.owner_id == owner and r.fields["category"] == "insurance_surrender_value"
                   for r in view.records):
            row.update(basis_text="연금보험 레코드가 없음", missing="연금보험이 있다면 해약환급금 또는 보험사 예시표의 예상 월 연금액")
            return row
        row.update(assets_now=assets)
        return _annuity(view, row, assets, age=age, payout_age=payout_age, today=today, owner=owner)
    row.update(basis_text="예상 수령액이 입력되지 않음", missing=f"{label}의 예상 월 수령액과 개시 시점")
    return row


def _project(row: dict[str, Any], assets: Decimal, contribution: Decimal, *, age: int | None, retire: int, start: int,
             nontaxed_now: Decimal, uncredited_yearly: Decimal, years: int = ACCOUNT_PAYOUT_YEARS,
             contribution_stop: int | None = None) -> dict[str, Any]:
    """Balance plus contributions until retirement (or the account's contribution end, if earlier), then the
    balance alone until the payout starts, at the real return, paid out in equal real instalments over `years`
    (the account's payout period, the public portal's 20 years when none is set; user decision 2026-11-07). The
    contributions no credit covered (this year's and every later year's `uncredited_yearly`) come back tax-free:
    their value in today's won at the start sets the taxable share."""
    if age is None:
        row.update(basis_text="출생 연도가 없어 수령 개시까지의 기간을 알 수 없음", missing="출생 연도(로컬 설정)")
        return row
    until = retire if contribution_stop is None else min(retire, contribution_stop)
    paying = max(0, until - age)
    projected = _grown(assets, contribution * 12, paying, max(0, start - age))
    # Contributions are constant in today's won; the tax-free principal is the nominal amount paid, so its value in
    # today's won at the payout start is deflated over the years in between.
    years_to_start = max(0, start - age)
    nontaxed = nontaxed_now / (1 + INFLATION) ** years_to_start + sum(
        (uncredited_yearly / (1 + INFLATION) ** (years_to_start - year) for year in range(paying)), Decimal(0))
    share = Decimal(1) if not projected else max(Decimal(0), 1 - nontaxed / projected)
    payment = REAL_RETURN / (1 - (1 + REAL_RETURN) ** -years)   # Equal real instalments that empty the account.
    stop = f"{until}세(납입 종료일)" if until < retire else f"{retire}세 은퇴"
    # No standing transfer into the account (a partner's account, say): the balance alone is projected and said so.
    put = (f"지금 잔액에 월 {contribution / 10_000:,.0f}만 원을 {stop}까지 넣고" if contribution
           else "지금 잔액만(월 납입 기록 없음)")
    row.update(gross_monthly=projected * payment / 12, start_age=start, end_age=start + years, basis="estimated",
               taxable_share=share, payout_years=years,
               basis_text=f"{put} 실질 연 {REAL_RETURN * 100:.0f}%로 불려 {start}세부터 {years}년간 나눠 받는다고 본 추정")
    return row


def products(view: LedgerView, key: str, owner: str = "self") -> list[dict[str, Any]]:
    """One member's products behind a pipeline with their stated terms (report design §18.5): the pension
    accounts of its type, the annuity policies, or the national pension's statement."""
    if key == "national":
        return [{"institution": "국민연금공단", "name": "국민연금", "contribution_end": r.fields.get("contribution_end"),
                 "payout_start": r.fields["start_date"]} for r in view.records
                if r.kind is Kind.CASHFLOW and r.owner_id == owner and r.fields["category"] == "pension_income"
                and r.fields.get("pension_source") == "national"]
    accept = ((lambda f: f.get("account_type") == _ACCOUNTS[key]) if key in _ACCOUNTS
              else (lambda f: f["category"] == "insurance_surrender_value") if key == "insurance_annuity" else None)
    if accept is None:
        return []
    found = []
    for record in view.records:
        f = record.fields
        if record.kind is not Kind.ASSET or record.owner_id != owner or not accept(f):
            continue
        if any(f.get(name) for name in _TERM_FIELDS):
            found.append({"institution": f.get("institution"), "name": f.get("instrument_name"),
                          "opened_on": f.get("opened_on"), "contribution_end": f.get("contribution_end"),
                          "payout_start": f.get("payout_start"), "payout_end": f.get("payout_end"),
                          "illustrated_annual": None if f.get("illustrated_annual_pension") is None
                          else Decimal(f["illustrated_annual_pension"])})
    return found


def _terms(found: list[dict[str, Any]]) -> dict[str, Any]:
    """The payout period (whole years) and contribution end of an account type from its stated terms. The type's
    balances are projected as one, so accounts with different periods are paid over the longest (it pays less a
    year, never past the money), and contributions run to the latest stated end; `mixed` says so."""
    periods = set()
    for p in found:
        if p["payout_start"] and p["payout_end"]:
            start, end = date.fromisoformat(p["payout_start"]), date.fromisoformat(p["payout_end"])
            periods.add(max(1, round(((end.year - start.year) * 12 + end.month - start.month) / 12)))
    ends = {p["contribution_end"] for p in found if p["contribution_end"]}
    return {"years": max(periods, default=ACCOUNT_PAYOUT_YEARS), "mixed": len(periods) > 1 or len(ends) > 1,
            "contribution_end": date.fromisoformat(max(ends)) if ends else None}


def _isa(contributions: dict[str, Any], *, age: int | None, retire: int) -> dict[str, Any]:
    """The contributions no pension credit covers, kept in an ISA or similar account (tax.credit_split): built up
    until retirement and drawn from it like the pension accounts; only the gains are taxed."""
    monthly = contributions["isa"]
    row = {"key": "isa", "label": LABELS["isa"], "start_age": retire, "gross_monthly": None, "net_monthly": None,
           "assets_now": Decimal(0), "monthly_contribution": monthly, "basis": "estimated", "missing": "",
           "taxable_share": Decimal(1),
           "basis_text": (f"연금저축·IRP 공제 한도를 넘는 월 {monthly / 10_000:,.0f}만 원을 {retire}세 은퇴까지 ISA 등에 모아 실질 연 "
                          f"{REAL_RETURN * 100:.0f}%로 불려 연 {WITHDRAWAL_RATE * 100:.0f}%씩 꺼내 쓴다고 본 추정")}
    if age is None:
        row.update(basis="unknown", basis_text="출생 연도가 없어 은퇴까지의 기간을 알 수 없음", missing="출생 연도(로컬 설정)")
        return row
    paying = max(0, retire - age)
    projected = _grown(Decimal(0), monthly * 12, paying, paying)
    principal = sum((monthly * 12 / (1 + INFLATION) ** (paying - year) for year in range(paying)), Decimal(0))
    row.update(gross_monthly=projected * WITHDRAWAL_RATE / 12,
               taxable_share=Decimal(1) if not projected else max(Decimal(0), 1 - principal / projected))
    return row


def _grown(balance: Decimal, yearly: Decimal, paying: int, years: int) -> Decimal:
    """`balance` grown for `years` at the real return, plus `yearly` added for the first `paying` of them."""
    paid = (1 + REAL_RETURN) ** paying
    return (balance * paid + yearly * (paid - 1) / REAL_RETURN) * (1 + REAL_RETURN) ** max(0, years - paying)


def _national_scale(records: list[Any], *, retire: int, birth: Any) -> tuple[Decimal, str]:
    """The share of the stated national pension a retirement at `retire` keeps, and a note for the basis text.
    The service's estimate assumes contributing until `contribution_end` for `contribution_months` months; retiring
    earlier stops contributions then (voluntary contributions are not assumed). The old-age pension grows about in
    proportion to the contribution months (basic pension formula, average income held), and is not paid at all
    below NATIONAL_MIN_MONTHS. Without the two fields the statement is used as it is, which for an early
    retirement is an upper bound and is said so."""
    fields = records[0].fields
    months, end = fields.get("contribution_months"), fields.get("contribution_end")
    if not months or not end or birth is None:
        return Decimal(1), " · 가입 기간 정보가 없어 조기 은퇴 시 감액은 반영하지 않음(상한)"
    months = int(Decimal(months))
    end_age = age_on(birth, date.fromisoformat(end))
    kept = months - max(0, end_age - retire) * 12
    if kept >= months:
        return Decimal(1), ""
    if kept < NATIONAL_MIN_MONTHS:
        return Decimal(0), f" · {retire}세 은퇴면 가입 {kept}개월로 10년 미만이라 연금 없음(반환일시금)"
    return Decimal(kept) / Decimal(months), f" · {retire}세 은퇴면 가입 {kept}/{months}개월로 비례 감액"


def _annuity(view: LedgerView, row: dict[str, Any], assets: Decimal, *, age: int | None, payout_age: int,
             today: date, owner: str = "self") -> dict[str, Any]:
    """The insurer's annuity kept to maturity: the surrender value plus the premiums still due (net of the
    loading) at the real return until the payout age, or the guaranteed base (premiums plus simple interest,
    deflated) when that is higher, drawn at the withdrawal rate. Without a qualifying premium the surrender
    value alone is projected (a lower bound)."""
    if age is None:
        row.update(basis_text="출생 연도가 없어 수령 개시까지의 기간을 알 수 없음", missing="출생 연도(로컬 설정)")
        return row
    years = max(0, payout_age - age)
    payout = date(today.year + years, today.month, 1)
    premiums = [r for r in view.records if r.kind is Kind.CASHFLOW and r.owner_id == owner
                and r.fields["category"] == "insurance_premium" and r.fields.get("end_date")
                and r.fields["frequency"] == "monthly" and r.fields["end_date"] >= today.isoformat()]
    monthly = Decimal(0)
    floor = Decimal(0)   # Nominal guaranteed base at commencement.
    counted = False
    for record in premiums:
        amount = base_amount(view, record)
        if amount is None or amount < ANNUITY_PREMIUM_MIN:
            continue
        counted = True
        start, end = date.fromisoformat(record.fields["start_date"]), date.fromisoformat(record.fields["end_date"])
        month = date(start.year, start.month, 1)
        while month <= end and month < payout:
            held = Decimal((payout.year - month.year) * 12 + payout.month - month.month) / 12
            floor += amount * (1 + GUARANTEE_SIMPLE_RATE * held)
            month = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
        monthly += amount  # The policy's monthly premium (still being paid).
        row["pay_until"] = end
    growth = (1 + REAL_RETURN) ** years
    projected = assets * growth
    if counted and monthly:
        end = max(date.fromisoformat(r.fields["end_date"]) for r in premiums)
        pay_years = min(Decimal(max(0, (end.year - today.year) * 12 + end.month - today.month)) / 12, Decimal(years))
        invested = monthly * (1 - ANNUITY_LOADING) * 12
        projected += invested * ((1 + REAL_RETURN) ** pay_years - 1) / REAL_RETURN * (1 + REAL_RETURN) ** (years - pay_years)
    floor_real = floor / (1 + INFLATION) ** years
    base = max(projected, floor_real)
    row.update(gross_monthly=base * WITHDRAWAL_RATE / 12, start_age=max(payout_age, PRIVATE_EARLIEST_AGE), basis="estimated",
               premiums_counted=counted, projected_real=projected, guarantee_floor_real=floor_real,
               floor_applies=floor_real > projected, monthly_premium=monthly,
               missing="보험사 예시표의 예상 월 연금액과 개시 연령")
    if counted:
        row["basis_text"] = (f"납입 기간까지 월 보험료 {monthly / 10_000:,.0f}만 원을 다 내고 해지하지 않는다고 보고, 사업비 "
                             f"{ANNUITY_LOADING * 100:.0f}%를 뺀 적립금을 {payout_age}세까지 실질 연 {REAL_RETURN * 100:.0f}%로 불린 값과 "
                             f"납입보험료 연 {GUARANTEE_SIMPLE_RATE * 100:.0f}% 단리 최저보증(현재 가치 환산) 중 큰 쪽"
                             f"{'(최저보증 적용)' if floor_real > projected else '(적립금 적용)'}을 연 {WITHDRAWAL_RATE * 100:.0f}%씩 "
                             "연금으로 받는다고 본 추정. 보험사 예시표 연금액을 알려 주시면 그 값으로 바꿉니다")
    else:
        row["basis_text"] = ("지금 해약환급금만 실질 연 3%로 불린 추정(앞으로 낼 보험료는 넣지 않은 하한). 보험사 예시표의 예상 월 "
                             "연금액을 알려 주시면 그 값으로 바꿉니다")
    return row


def _apply_tax(rows: list[dict[str, Any]]) -> None:
    """Net monthly figures by the assumed tax factors on each payout's taxable share (contributions no credit
    covered come back tax-free and do not count toward the limit). A private pension carries both its low-rate and
    high-rate net and the ages at which the household's taxable private payouts pass the separate-taxation limit:
    monthly_at reads the right one per age, and net_monthly is its first payment year's."""
    private = [r for r in rows if r["key"] in _PRIVATE and r["gross_monthly"] is not None and r["start_age"] is not None]
    # The ages whose taxable private payouts (only those paying that year) pass the limit.
    high_ages = frozenset(age for age in range(PRIVATE_EARLIEST_AGE, PLAN_AGE + 1)
                          if sum((r["gross_monthly"] * r["taxable_share"] * share
                                  for r in private for start, share, _, end in segments(r)
                                  if start is not None and start <= age and (end is None or age < end)), Decimal(0)) * 12
                          > PRIVATE_SEPARATE_LIMIT)
    for row in rows:
        gross = row["gross_monthly"]
        if gross is None or row.get("stated_net"):
            continue
        if row["key"] in _PRIVATE:
            low = gross * (1 - TAX_FACTORS.get(row["key"], Decimal("0.055")) * row["taxable_share"])
            high = gross * (1 - PRIVATE_HIGH_TAX * row["taxable_share"])
            starts_high = row["start_age"] in high_ages
            row.update(net_low=low, net_high=high, high_ages=high_ages, high_private_tax=starts_high,
                       tax_factor=PRIVATE_HIGH_TAX if starts_high else TAX_FACTORS.get(row["key"], Decimal("0.055")),
                       net_monthly=high if starts_high else low)   # The table's figure: its first payment year.
            continue
        factor = ISA_GAINS_TAX if row["key"] == "isa" else TAX_FACTORS.get(row["key"], Decimal("0.055"))
        row.update(tax_factor=factor, net_monthly=gross * (1 - factor * row["taxable_share"]), high_private_tax=False)


def born(household: dict[str, Any], owner: str = "self") -> tuple[int, int | None] | None:
    """(birth year, birth month or None) of a member from [household] (`birth_year`/`birth_month` for the client,
    `partner_birth_year`/`partner_birth_month` for the spouse); None without a birth year or for any other member."""
    key = _BIRTH_KEYS.get(owner)
    year = None if key is None else household.get(key)
    return None if year is None else (int(year), household.get(key.replace("_year", "_month")))


def age_on(birth: tuple[int, int | None], when: date) -> int:
    """Age in full years on `when`. With the birth month the birthday month counts as reached (the national
    pension starts the month after it); with the year alone it is the age reached during that calendar year."""
    year, month = birth
    return when.year - year - (month is not None and when.month < month)


def national_pension_age(birth_year: int) -> int:
    for upto, age in _NATIONAL_AGES:
        if birth_year <= upto:
            return age
    return 65


def _assets(view: LedgerView, accept: Any, owner: str = "self") -> Decimal | None:
    total = Decimal(0)
    for record in view.records:
        if record.kind is Kind.ASSET and record.owner_id == owner and accept(record.fields):
            value = base_amount(view, record)
            if value is None:
                return None
            total += value
    return total


def _has_account(view: LedgerView, account_type: str, owner: str = "self") -> bool:
    return any(r.kind is Kind.ASSET and r.owner_id == owner and r.fields.get("account_type") == account_type
               for r in view.records)


def _contributions(view: LedgerView, account_type: str, today: date, owner: str = "self") -> Decimal | None:
    """Monthly transfers into one member's accounts of this type (the same reading as the tax rules)."""
    accounts = {r.record_id for r in view.records if r.kind is Kind.ASSET and r.owner_id == owner
                and r.fields.get("account_type") == account_type}
    total = Decimal(0)
    for record in view.records:
        f = record.fields
        # A transfer counts while it runs today: not one that ended, nor one that starts later (Codex review 2026-11-08).
        if record.kind is not Kind.CASHFLOW or f["category"] != "internal_transfer" or f.get("target_record_id") not in accounts \
                or f["frequency"] not in _MONTHS or not recurring_active(f, today):
            continue
        amount = base_amount(view, record)
        if amount is None:
            return None
        total += amount / _MONTHS[f["frequency"]]
    return total


def assumptions(shared: bool = False) -> list[str]:
    tiers = "·".join(f"{age}세" for age in RETIREMENT_TIERS)
    household = ([f"가구 합산: 배우자의 연금도 같은 나이에 함께 은퇴한다고 보고 본인 연금과 더해 한 목표(세후 월 생활비)에 견줌"
                  f"(사용자 결정 2026-11-08). 연금소득세와 사적연금 연 {PRIVATE_SEPARATE_LIMIT / 10_000:,.0f}만 원 기준은 사람마다 따로 적용"]
                 if shared else [])
    return household + [f"연금 스택: 국민연금 세후 {TAX_FACTORS['national'] * 100:.0f}% 차감, 연금저축·IRP 연금소득세 "
            f"{TAX_FACTORS['irp'] * 100:.1f}%(과세 대상 사적연금이 연 {PRIVATE_SEPARATE_LIMIT / 10_000:,.0f}만 원 초과 시 "
            f"{PRIVATE_HIGH_TAX * 100:.1f}%), 세액공제를 받지 않은 납입 원금은 꺼낼 때 비과세(올해 이전 해의 공제 밖 납입은 "
            "모름: 과세로 본 하한), ISA 등 공제 밖 적립은 이익에만 9.9%, 연금보험 비과세 요건 충족 가정",
            f"은퇴 나이: 정하지 않아 {tiers} 세 경우로 계산. 납입은 은퇴 때 멈추고, 연금 계좌는 {PRIVATE_EARLIEST_AGE}세 전에는 "
            "받지 않으며, 국민연금은 조회값이 전제한 가입 기간 대비 줄어든 개월 수에 비례해 감액(임의 가입 계속 납부는 가정 안 함)",
            f"연금 계좌 수령액: 지금 잔액과 월 납입(세액공제를 빠짐없이 받는 연금저축·IRP 분할, 공제 밖 나머지는 ISA 등)을 "
            f"은퇴 때(계좌의 납입 종료일이 더 이르면 그때)까지 넣고 수령 개시까지 실질 연 {REAL_RETURN * 100:.0f}%로 불려, 계좌에 "
            f"정한 수령 기간(없으면 정부 '내연금'과 같은 {ACCOUNT_PAYOUT_YEARS}년) 동안 같은 실질 금액으로 나눠 받는다고 가정. "
            "납입액은 오늘 돈 가치로 고정(물가만큼 늘려 넣는다는 뜻)",
            f"연금 밖에서 준비할 돈: 은퇴부터 마지막 연금이 시작되거나 기간이 정해진 연금이 끝날 때까지 해마다의 부족분과 그 뒤 계속되는 부족분(연 "
            f"{WITHDRAWAL_RATE * 100:.0f}% 인출 기준)을 실질 연 {REAL_RETURN * 100:.0f}%로 은퇴 시점까지 할인한 오늘 돈 가치",
            f"연금보험: 납입 기간 끝까지 보험료를 내고 해지하지 않으며 사업비 {ANNUITY_LOADING * 100:.0f}%, 납입보험료 연 "
            f"{GUARANTEE_SIMPLE_RATE * 100:.0f}% 단리 최저보증(물가 {INFLATION * 100:.0f}%로 현재 가치 환산, 약관 확인 필요) 가정"]
