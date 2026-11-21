"""Health-insurance premium of a retired household (report design §50, user decision 2026-11-21).

The retirement target is what is left to spend after every tax and the health-insurance premium, so the pension stack
needs the premium a household pays once nobody in it is employed: its members become regional subscribers (지역가입자)
and one premium is charged on the household's income and property (국민건강보험법 제69조 ⑤, 시행령 별표 4).

- Income: a twelfth of the year's income, a public pension counted at half of the whole amount received (no
  pension-income deduction), times the rate. An income month of 28만 원 or less pays the minimum instead.
- Property: the property-tax base of the household's homes, less one deduction for the household, graded into
  points worth a fixed amount each a month. Nothing is charged at or under the deduction.
- Long-term care insurance: a share of the health premium (income and property parts together), on top.

What is left out, and said in the letter: private pensions (pension savings, IRP) are within the decree's wording but
not charged today, which is the corporation's practice and an assumption here; interest and dividends (charged in
full once they pass 1,000만 원 a year) because the stack does not model the money held outside the pensions; a
tenant's deposit; the 36 months a retiree may keep the employee premium (임의계속가입). Amounts are today's won under
the rules of RULES_YEAR, read on the official texts on RULES_CHECKED.
"""
from decimal import Decimal
from typing import Any

RULES_YEAR = 2026
RULES_CHECKED = "2026-11-21"
SOURCES = {
    "premium": ("국민건강보험법 시행령 제44조(보험료율·부과점수당 금액)·별표 4(재산등급별 점수)",
                "https://www.law.go.kr/법령/국민건강보험법시행령"),
    "income": ("국민건강보험법 시행규칙 제44조(소득월액 산정)", "https://www.law.go.kr/법령/국민건강보험법시행규칙"),
    "limits": ("월별 건강보험료액의 상한과 하한에 관한 고시(보건복지부고시 제2025-222호)",
               "https://www.law.go.kr/행정규칙/월별건강보험료액의상한과하한에관한고시"),
    "care": ("노인장기요양보험법 시행령 제4조(장기요양보험료율)", "https://www.law.go.kr/법령/노인장기요양보험법시행령"),
}
RATE = Decimal("0.0719")                  # 시행령 제44조 ①, from January 2026.
CARE_RATIO = Decimal("0.1314")            # 장기요양보험료율 0.9448% over the health rate, on the whole premium.
PENSION_SHARE = Decimal("0.5")            # 시행규칙 제44조: pay and pensions count at half.
# 시행규칙 제44조 ③ 단서: an income month at or under this pays MINIMUM as its income part; property comes on top.
MINIMUM_INCOME_MONTH = Decimal(280_000)
MINIMUM, MAXIMUM = Decimal(20_160), Decimal(4_591_740)   # The household's monthly health premium (고시 제2025-222호).
POINT_VALUE = Decimal("211.5")            # 시행령 제44조 ②: won a month per property point.
PROPERTY_DEDUCTION = Decimal(100_000_000)  # 별표 4 제1호: one for the household, from February 2024.
# 별표 4 (개정 2024.5.7.): (the grade's upper bound in 만 원, inclusive; None for the last grade, its points).
PROPERTY_POINTS = (
    (450, 22), (900, 44), (1350, 66), (1800, 97), (2250, 122), (2700, 146), (3150, 171), (3600, 195), (4050, 219),
    (4500, 244), (5020, 268), (5590, 294), (6220, 320), (6930, 344), (7710, 365), (8590, 386), (9570, 412),
    (10700, 439), (11900, 465), (13300, 490), (14800, 516), (16400, 535), (18300, 559), (20400, 586), (22700, 611),
    (25300, 637), (28100, 659), (31300, 681), (34900, 706), (38800, 731), (43200, 757), (48100, 785), (53600, 812),
    (59700, 841), (66500, 881), (74000, 921), (82400, 961), (91800, 1001), (103000, 1041), (114000, 1091),
    (127000, 1141), (142000, 1191), (158000, 1241), (176000, 1291), (196000, 1341), (218000, 1391), (242000, 1451),
    (270000, 1511), (300000, 1571), (330000, 1641), (363000, 1711), (399300, 1781), (439230, 1851), (483153, 1921),
    (531468, 1991), (584615, 2061), (643077, 2131), (707385, 2201), (778124, 2271), (None, 2341))


def property_points(base: Decimal) -> int:
    """The points of a household's property-tax base (won, every home added): none at or under the deduction."""
    amount = (base - PROPERTY_DEDUCTION) / 10_000
    if amount <= 0:
        return 0
    return next(points for upper, points in PROPERTY_POINTS if upper is None or amount <= upper)


def regional_premium(public_pensions: Decimal, property_base: Decimal | None) -> dict[str, Any]:
    """A regional-subscriber household's monthly premium from the year's public pensions before tax (every member's
    added) and the property-tax base of its homes (None: unknown).

    Keys: income, property (None while the base is unknown: the premium is then a lower bound), health (the two,
    within the yearly limits), care (long-term care insurance on it) and total (health and care)."""
    month = public_pensions * PENSION_SHARE / 12
    income = MINIMUM if month <= MINIMUM_INCOME_MONTH else month * RATE
    held = None if property_base is None else property_points(property_base) * POINT_VALUE
    health = min(MAXIMUM, income + (held or Decimal(0)))
    care = health * CARE_RATIO
    return {"income": income, "property": held, "health": health, "care": care, "total": health + care}


def sources() -> list[tuple[str, str]]:
    """(title, URL) of the official texts the rules were read on."""
    return list(SOURCES.values())


def assumptions() -> list[str]:
    return [f"은퇴 뒤 건강보험료: {RULES_YEAR}년 기준({RULES_CHECKED} 확인). 두 분이 한 세대의 지역가입자라고 보고, 국민연금 수령액의 "
            f"{PENSION_SHARE * 100:.0f}%에 보험료율 {RATE * 100:.2f}%, 지금 보유한 주택의 재산세 과세표준에서 "
            f"{PROPERTY_DEDUCTION / 100_000_000:.0f}억 원을 뺀 금액의 재산 점수(점수당 {POINT_VALUE}원), 그 합의 "
            f"{CARE_RATIO * 100:.2f}%인 장기요양보험료를 더함",
            "건강보험료에 넣지 않은 것: 연금저축·IRP 수령액(지금은 부과하지 않는 운영 방식을 유지한다고 가정), 연금 밖 자산의 "
            "이자·배당(연 1,000만 원을 넘으면 전액 부과), 전·월세 보증금, 퇴직 뒤 36개월간 직장 보험료를 유지하는 임의계속가입"]
