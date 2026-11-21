"""Public household-spending statistics as a yardstick for the card-spending note (FR-60, spending design §5.2).

Source: 가계동향조사 (Household Income and Expenditure Survey) by 국가데이터처 (formerly 통계청), read from KOSIS on
2026-11-21 (tables last updated 2026-08-27): 가구당 월평균 가계수지, 전국, 1인 이상 가구, 명목, 전체가구.
- by household size: table DT_1L9U105, 2025 annual;
- by income fifth: table DT_1L9U103, 2025 annual; the disposable income of each fifth is quarterly only, so the four
  quarters of 2025 are kept and averaged here, for choosing the nearest fifth and nothing else.

The figures are a whole household's monthly average over every means of payment. A card statement is one person's one
card. So the comparison is one-sided: a card that alone reaches the household figure says something; a card below it
says nothing about the household (`compare`). An average is a reference, never a norm.

Refresh: the annual results come out late in February. Until a KOSIS adapter reads them, the table is replaced by
hand from the two tables above and `PERIOD` moves with it; the unit test checks every row against its own total.
"""
from typing import Any

PERIOD = "2025년 연간"
SURVEY = "가계동향조사(국가데이터처, 옛 통계청)"
SOURCE_URL = "https://kosis.kr/statHtml/statHtml.do?orgId=101&tblId=DT_1L9U105"
SOURCE_URLS = (SOURCE_URL, "https://kosis.kr/statHtml/statHtml.do?orgId=101&tblId=DT_1L9U103")

# The twelve consumption items in the survey's order (COICOP 2018 names as KOSIS prints them).
ITEMS = ("food_home", "alcohol_tobacco", "clothing", "housing", "household_goods", "health", "transport", "telecom",
         "leisure", "education", "eating_out_lodging", "other")
ITEM_LABEL = {
    "food_home": "식료품·비주류음료", "alcohol_tobacco": "주류·담배", "clothing": "의류·신발", "housing": "주거·수도·광열",
    "household_goods": "가정용품·가사서비스", "health": "보건", "transport": "교통·운송", "telecom": "정보통신",
    "leisure": "오락·문화", "education": "교육", "eating_out_lodging": "음식·숙박", "other": "기타상품·서비스",
}


def _row(members: float, consumption: int, *items: int) -> dict[str, Any]:
    return {"members": members, "consumption": consumption, **dict(zip(ITEMS, items, strict=True))}


# Won a month, a household. The last size group is "4인 이상" (average 4.20 members).
ALL = _row(2.19, 2_939_091, 449_087, 37_626, 141_084, 360_650, 119_823, 227_576, 336_795, 167_025, 175_330, 208_599,
           465_329, 250_168)
BY_SIZE = {
    1: _row(1.00, 1_757_319, 238_451, 33_696, 80_347, 324_417, 62_079, 128_940, 189_365, 104_516, 106_044, 30_486,
            312_736, 146_243),
    2: _row(2.00, 2_710_796, 478_513, 34_950, 121_200, 331_514, 117_533, 265_863, 336_870, 144_778, 165_455, 62_446,
            396_333, 255_341),
    3: _row(3.00, 3_884_655, 587_918, 43_314, 193_566, 402_143, 180_862, 280_202, 476_401, 221_972, 230_962, 292_008,
            627_027, 348_281),
    4: _row(4.20, 4_861_419, 700_078, 44_515, 250_034, 444_896, 180_622, 315_400, 500_190, 281_279, 281_500, 767_719,
            738_700, 356_485),
}
BY_FIFTH = {
    1: _row(1.34, 1_361_265, 298_001, 24_486, 48_496, 273_093, 48_181, 150_546, 92_302, 78_739, 57_588, 23_320,
            176_422, 90_090),
    2: _row(1.67, 1_979_446, 350_822, 34_753, 86_552, 327_970, 79_407, 179_360, 196_129, 120_693, 88_951, 61_066,
            296_699, 157_044),
    3: _row(2.14, 2_670_797, 423_393, 41_595, 124_773, 350_678, 100_423, 198_578, 299_974, 166_968, 150_036, 142_657,
            454_184, 217_538),
    4: _row(2.69, 3_644_258, 526_014, 42_527, 176_112, 396_714, 156_847, 268_854, 426_024, 209_250, 209_574, 304_980,
            607_519, 319_844),
    5: _row(3.10, 5_037_696, 647_009, 44_757, 269_373, 454_682, 214_154, 340_423, 669_257, 259_376, 370_369, 510_698,
            791_482, 466_116),
}
# 처분가능소득 of each fifth, 2025 1/4 ~ 4/4 (the annual table has expenditure only).
DISPOSABLE_QUARTERS = {
    1: (920_518, 1_017_883, 1_126_120, 1_061_264),
    2: (2_289_970, 2_371_442, 2_567_222, 2_482_777),
    3: (3_539_553, 3_503_249, 3_774_823, 3_633_486),
    4: (5_208_930, 4_966_934, 5_357_733, 5_202_796),
    5: (9_180_077, 8_261_472, 9_065_868, 9_360_789),
}
_FIFTH_NAME = {1: "소득 하위 20% 가구", 2: "소득 2분위 가구", 3: "소득 중간 20% 가구", 4: "소득 4분위 가구", 5: "소득 상위 20% 가구"}

# A card area against the survey items it can honestly be set beside: (key, the card's categories, the survey's
# items, what the survey figure also holds that the card area does not). Areas whose match is loose (shopping,
# subscriptions, learning, travel) are left out rather than compared badly.
AREAS: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...], str], ...] = (
    ("eating", "외식·커피·술", ("cafe", "dining", "drinks"), ("eating_out_lodging",), "숙박비 포함"),
    ("groceries", "장보기·생활용품", ("grocery",), ("food_home", "household_goods"), ""),
    ("transport", "교통", ("transport",), ("transport",), "자동차 구입·연료비 포함"),
    ("leisure", "문화·책", ("culture",), ("leisure",), "단체여행·운동·취미용품 포함"),
    # Gyms and studios are a category of their own (`fitness`): the survey counts them under leisure, not health.
    ("health", "의료·건강", ("health",), ("health",), ""),
)


def fifth_for(income: int) -> int:
    """The income fifth whose average disposable income of the year is nearest a household's monthly take-home."""
    means = {fifth: sum(quarters) / len(quarters) for fifth, quarters in DISPOSABLE_QUARTERS.items()}
    return min(means, key=lambda fifth: abs(means[fifth] - income))


def groups(members: int | None, household_income: int | None) -> list[dict[str, Any]]:
    """The reference groups for one household: its size group and its income fifth when each is known, else the
    average of all households. Each: {"label", "short" (a chart label), "members" (the group's average size), "row"}."""
    found = []
    if members and members > 0:
        size = min(int(members), 4)
        name = f"{size}인 가구" if size < 4 else "4인 이상 가구"
        found.append({"label": name, "short": name, "members": BY_SIZE[size]["members"], "row": BY_SIZE[size]})
    if household_income and household_income > 0:
        fifth = fifth_for(int(household_income))
        row = BY_FIFTH[fifth]
        found.append({"label": f"{_FIFTH_NAME[fifth]}(평균 {row['members']:.1f}명)", "short": _FIFTH_NAME[fifth][:-3],
                      "members": row["members"], "row": row})
    return found or [{"label": f"전체 가구 평균(평균 {ALL['members']:.1f}명)", "short": "전체 가구", "members": ALL["members"],
                      "row": ALL}]


def compare(months: list[dict[str, Any]], *, members: int | None = None,
            household_income: int | None = None) -> dict[str, Any] | None:
    """The card's areas beside the reference groups' monthly household averages; None without a month.

    `months` are `spending.month` results, newest last. The card side is the average over them (one statement is
    noisy against a yearly average) with the newest beside it. `reaches` says whether the card alone matches the
    group's whole-household figure, and `card_average_pct` is the card's average as a percentage of it, rounded down
    (100 or more exactly when `reaches`).
    """
    if not months:
        return None
    chosen, latest, areas = groups(members, household_income), months[-1], []
    for _, label, categories, items, also in AREAS:
        amount = lambda m: sum(m["categories"].get(c, {}).get("amount", 0) for c in categories)   # noqa: E731
        total = sum(amount(m) for m in months)
        if total <= 0:
            continue
        average, peers = round(total / len(months)), []
        for group in chosen:
            figure = sum(group["row"][item] for item in items)
            # Whole won, no rounding: 395,000 against 396,333 has not reached it, and its percentage reads 99.
            peers.append({"group": group["label"], "short": group["short"], "amount": figure,
                          "reaches": total >= figure * len(months),
                          "card_average_pct": total * 100 // (figure * len(months))})
        areas.append({"area": label, "survey_items": " + ".join(ITEM_LABEL[item] for item in items), "survey_also_holds": also,
                      "card_latest": amount(latest), "card_average": average, "card_months": len(months), "peers": peers})
    return {"survey": SURVEY, "period": PERIOD, "unit": "가구당 월평균(원)", "source_url": SOURCE_URL,
            "groups": [{"label": g["label"], "short": g["short"], "members": g["members"]} for g in chosen], "areas": areas}
