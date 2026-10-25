"""The weekly health letter's inputs assembled (FR-38): one dict for the renderer, the writer model and the reply
answers. `assemble` is pure (ledger + config + dates in, data out), so tests cover it without a file or a model.

What the letter is made of (health design §3, user review 2026-10-25): a verdict first (is it getting better or
worse, is it a worry, the one thing to do), a goal dashboard with what to do and what to avoid for each goal, one topic
card as a short table of items — plain names, the latest value, the trend computed by code — with line charts over
time, the follow-ups the reports asked for, last week's actions to review, and the weekly activity (from Fitbit once
connected, until then estimated from what the client said they do). Every figure and every trend word comes from the
ledger through code; the model writes what it means in one line per item and what to do this week (health_writer).
"""
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from smith import health
from smith.health_chart import parse_ref
from smith.payload import mask_identifiers

PRODUCT = "health"
# The weekly rotation (health design §3): what each topic reads from the ledger, which series it charts, and the axis
# its actions move on.
TOPICS: dict[str, dict[str, Any]] = {
    "vascular": {"label": "혈관·콜레스테롤", "codes": ("tc", "ldl", "hdl", "tg", "apob", "lpa", "hs_crp", "homocysteine", "cv_risk_10y_pct"),
                 "charts": ("ldl", "hdl", "tg", "tc"),
                 "tracks": ("carotid-us", "echo", "ecg", "lipids", "cv-risk", "tcd", "brain-ct"),
                 "genetics": ("중성지방", "콜레스테롤"), "microbiome": (), "axis": "포화지방·식이섬유·유산소", "goals": ("ldl", "blood_pressure")},
    "uric-weight": {"label": "요산·체중", "codes": ("uric_acid", "weight", "bmi", "waist", "body_fat_pct", "skeletal_muscle_mass", "muscle_mass",
                                                 "visceral_fat_level", "ast", "alt", "ggt"),
                    "charts": ("uric_acid", "body_fat_pct", "weight", "waist"),
                    "tracks": ("uric-acid", "body-composition", "liver-us", "metabolic"),
                    "genetics": ("요산", "비만", "제지방량", "체중감량"), "microbiome": ("well_obesity",),
                    "axis": "퓨린·과당 음료·술, 체지방", "goals": ("body_fat", "uric_acid", "weight")},
    "nutrition-gut": {"label": "영양·장", "codes": ("vitamin_d", "hb", "ferritin", "calcium", "magnesium", "glucose_fasting", "hba1c"),
                      "charts": ("vitamin_d", "glucose_fasting", "hba1c"),
                      "tracks": ("vitamin-d", "hepatitis-ab", "fobt"), "genetics": ("비타민", "지방산", "아미노산"),
                      "microbiome": ("overall", "richness_pct", "balance_pct", "well_fatigue", "well_immunity", "gut_gas"),
                      "axis": "햇빛·음식, 발효식품·식이섬유", "goals": ("vitamin_d", "glucose")},
    "balance": {"label": "몸의 균형", "codes": ("skeletal_muscle_mass", "muscle_mass", "body_fat_pct", "bmr"),
                "charts": ("skeletal_muscle_mass", "body_fat_pct"), "tracks": ("body-composition", "lifestyle"),
                "genetics": ("근육", "부상", "관절염", "유산소 운동"), "microbiome": (), "axis": "스트레칭 루틴, 근력운동 주 2회",
                "goals": ("posture",), "posture": True},
    "stomach-habits": {"label": "위·생활습관", "codes": ("ggt", "stress_index", "hrv", "pulse"), "charts": ("ggt", "stress_index"),
                       "tracks": ("egd", "lifestyle", "mental", "dental"),
                       "genetics": ("알코올", "니코틴", "카페인", "불면증", "수면"), "microbiome": ("well_sleep", "gut_nervous"),
                       "axis": "술·짠 음식·수면 시간", "goals": ()},
}
# The client's priority goals (`[health] goals`): what the dashboard follows, which way is better, and what to do and
# avoid when the writer has not said it for this week (public guidance, never personalised).
GOALS: dict[str, dict[str, Any]] = {
    "posture": {"label": "자세", "posture": ("forward_head_deg", "pelvic_tilt_deg", "pcmt_kg"),
                "do": "앉은 1시간마다 일어나 턱 당기기 10회와 가슴 열기, 모니터는 눈높이로", "avoid": "고개 숙여 폰 보기, 소파에 기대어 오래 앉기"},
    "body_fat": {"label": "체지방률", "code": "body_fat_pct",
                 "do": "근력 운동 주 2일과 끼니마다 단백질 반찬 하나", "avoid": "단 음료, 야식, 술자리 안주"},
    "ldl": {"label": "LDL 콜레스테롤", "code": "ldl", "method": "direct",
            "do": "생선·콩·귀리·채소를 늘리고 빠르게 걷기 주 150분", "avoid": "붉은 고기 기름, 버터, 튀김, 가공육"},
    "uric_acid": {"label": "요산", "code": "uric_acid", "do": "물 하루 1.5~2L, 저지방 유제품", "avoid": "맥주, 과당 음료, 내장류, 과식"},
    "vitamin_d": {"label": "비타민 D", "code": "vitamin_d", "do": "한낮 햇빛 15~20분, 등푸른생선·달걀", "avoid": "하루 종일 실내에만 있기"},
    "weight": {"label": "체중", "code": "weight", "do": "주 1회 같은 조건에서 재기, 걷기 늘리기", "avoid": "늦은 저녁 과식"},
    "blood_pressure": {"label": "혈압", "code": "sbp", "pair": "dbp", "do": "싱겁게 먹기, 매일 걷기", "avoid": "짠 국물, 가공식품, 과음"},
    "glucose": {"label": "공복혈당", "code": "glucose_fasting", "pair": "hba1c", "do": "식후 10분 걷기, 통곡물", "avoid": "단 음료, 흰 빵·과자"},
}
# Plain names for the reader who is new to health data (user review 2026-10-25): the meaning first, the term in brackets.
PLAIN: dict[str, str] = {
    "tc": "총콜레스테롤", "ldl": "나쁜 콜레스테롤(LDL)", "hdl": "좋은 콜레스테롤(HDL)", "tg": "중성지방(혈액 속 기름)",
    "apob": "콜레스테롤 입자 수(ApoB)", "lpa": "유전형 콜레스테롤(Lp(a))", "hs_crp": "혈관 염증 지표(hs-CRP)", "homocysteine": "혈관 손상 지표(호모시스테인)",
    "cv_risk_10y_pct": "10년 심뇌혈관 위험", "uric_acid": "요산(통풍 지표)", "weight": "체중", "bmi": "체질량지수(BMI)", "waist": "허리둘레",
    "body_fat_pct": "체지방률", "body_fat_kg": "체지방량", "muscle_mass": "근육량", "skeletal_muscle_mass": "골격근량(힘 쓰는 근육)",
    "fat_free_mass": "제지방량", "visceral_fat_level": "내장지방 레벨", "whr": "복부지방률", "bmr": "기초대사량(가만히 있어도 쓰는 열량)",
    "ast": "간 수치(AST)", "alt": "간 수치(ALT)", "ggt": "간 수치(감마GT, 술 영향)", "vitamin_d": "비타민 D", "hb": "혈색소(빈혈 지표)",
    "ferritin": "저장 철(페리틴)", "calcium": "칼슘", "magnesium": "마그네슘", "glucose_fasting": "공복혈당", "hba1c": "3개월 평균 혈당(당화혈색소)",
    "stress_index": "스트레스 저항도", "hrv": "자율신경 활성도", "pulse": "맥박", "sbp": "수축기 혈압(위)", "dbp": "이완기 혈압(아래)",
    "forward_head_deg": "거북목 각도", "pelvic_tilt_deg": "골반 경사", "pcmt_kg": "목 뒤 근육 긴장",
}
# Which way is better for a value: "down", "up", or "range" (inside the reference range is what matters).
BETTER: dict[str, str] = {
    "tc": "down", "ldl": "down", "tg": "down", "apob": "down", "lpa": "down", "hs_crp": "down", "homocysteine": "down",
    "cv_risk_10y_pct": "down", "uric_acid": "down", "waist": "down", "body_fat_pct": "down", "body_fat_kg": "down",
    "visceral_fat_level": "down", "whr": "down", "ast": "down", "alt": "down", "ggt": "down", "glucose_fasting": "down", "hba1c": "down",
    "sbp": "down", "dbp": "down", "hdl": "up", "vitamin_d": "up", "muscle_mass": "up", "skeletal_muscle_mass": "up",
    "fat_free_mass": "up", "bmr": "up", "hb": "range", "weight": "range", "bmi": "range", "stress_index": "up", "hrv": "up",
    "pulse": "range", "forward_head_deg": "down", "pelvic_tilt_deg": "down", "pcmt_kg": "down",
}
TREND_WORDS = {"improving": "좋아지는 중", "worsening": "나빠지는 중", "flat": "그대로", "single": "한 번 측정"}
LEVEL_WORDS = {"normal": "정상 범위 안", "edge": "경계", "out": "범위 밖", "unknown": "기준 없음"}
# A move is a trend only when it is at least this share of the reference span (or of the value, with no span), measured
# from the earliest of the last three readings, so a one-point wobble inside the range is "그대로" and a slow creep over
# two check-ups still counts (first model preview 2026-10-25: a 1-point AST move read as "나빠지는 중").
TREND_SHARE_OF_SPAN = Decimal("0.10")
TREND_SHARE_OF_VALUE = Decimal("0.05")
TREND_WINDOW = 3
# Korean adult physical-activity guideline (보건복지부·한국건강증진개발원, 한국인을 위한 신체활동 지침서 2023 개정):
# 150~300 minutes of moderate aerobic activity a week and muscle-strengthening on two or more days.
AEROBIC_MINUTES_WEEK = 150
STRENGTH_DAYS_WEEK = 2
GUIDELINE_SOURCE = "보건복지부·한국건강증진개발원, 한국인을 위한 신체활동 지침서(2023 개정)"
STRENGTH_WORDS = ("근력", "웨이트", "헬스", "스쿼트", "덤벨", "바벨", "밴드")
ESTIMATE_DISCOUNT = Decimal("0.5")   # 헬스장 "스트레칭·요가 위주" counts half as moderate aerobic time.
# What the mail says when the writer did not: two actions per topic from public guidance, never personalised.
FALLBACK_ACTIONS: dict[str, tuple[dict[str, str], ...]] = {
    "vascular": ({"category": "food", "what": "붉은 고기·튀김·가공육 대신 생선·두부·콩", "amount": "고기·튀김 식사 주 3회 이하, 생선 주 2회",
                  "why": "포화지방을 줄이고 불포화지방을 늘리면 LDL이 내려가는 쪽으로 움직입니다", "check": "다음 검진의 LDL·총콜레스테롤"},
                 {"category": "exercise", "what": "빠르게 걷기 또는 가벼운 달리기", "amount": "30분 × 주 5일(= 주 150분)",
                  "why": "유산소 운동은 HDL을 올리고 중성지방을 내리는 쪽으로 움직입니다", "check": "주간 활동 시간 150분"}),
    "uric-weight": ({"category": "food", "what": "술·과당 음료·내장류를 줄이고 물을 늘리기", "amount": "술은 주 2회 이하, 당 음료 0, 물 하루 1.5~2L",
                     "why": "요산은 술·과당·퓨린이 많은 음식에서 오르고 물이 부족하면 잘 빠지지 않습니다", "check": "다음 검진의 요산"},
                    {"category": "exercise", "what": "근력 운동 추가", "amount": "주 2일 × 30분(등·엉덩이·다리)",
                     "why": "근육이 늘면 쉴 때 쓰는 에너지가 늘어 체지방이 빠지기 쉽습니다", "check": "주간 근력 운동 일수 2일"}),
    "nutrition-gut": ({"category": "life", "what": "한낮 햇빛 쬐기", "amount": "하루 15~20분, 팔·얼굴 노출",
                       "why": "비타민 D는 음식보다 햇빛에서 더 많이 만들어집니다", "check": "다음 검진의 비타민 D"},
                      {"category": "food", "what": "발효식품과 식이섬유", "amount": "김치·요거트 매일 1회, 채소 매 끼 한 접시",
                       "why": "장내 유익균의 먹이가 늘어 균형 지표가 좋아지는 쪽으로 움직입니다", "check": "다음 장내미생물 검사"}),
    "balance": ({"category": "exercise", "what": "턱 당기기와 가슴 열기 스트레칭", "amount": "하루 2회 × 각 10회, 앉아 있는 1시간마다 일어나기",
                 "why": "거북목은 목 뒤 근육의 긴장을 늘려 통증과 두통으로 이어집니다", "check": "다음 자세 측정의 거북목 각도"},
                {"category": "exercise", "what": "근력 운동(등·엉덩이·몸통)", "amount": "주 2일 × 30분",
                 "why": "자세를 받치는 근육이 늘어야 교정이 유지됩니다", "check": "주간 근력 운동 일수 2일"}),
    "stomach-habits": ({"category": "life", "what": "술 줄이기", "amount": "주 2회 이하, 한 번에 소주 반 병 이하, 한 잔마다 물 한 잔",
                        "why": "술은 위 점막을 자극하고 요산·중성지방도 올립니다", "check": "다음 검진의 음주 문항·감마GT"},
                       {"category": "life", "what": "잠 7시간 확보", "amount": "취침·기상 시각 고정, 자기 전 1시간 화면 끄기",
                        "why": "수면 부족은 스트레스 호르몬과 식욕을 올립니다", "check": "주간 평균 수면 시간"}),
}
GUIDANCE_SOURCES = ("대한심장학회·한국지질동맥경화학회 이상지질혈증 진료지침(2022, 5판)", "대한류마티스학회 통풍 진료지침(2020)",
                    "질병관리청 국가건강정보포털 '비타민 D', '건강한 수면'", GUIDELINE_SOURCE)
# Foods a constraint can rule out (client mail 2026-10-25: a fish allergy): the words in a constraint's text on the left,
# the words that then may not appear in any suggestion on the right.
FOOD_GROUPS: dict[str, tuple[str, ...]] = {
    # Whole words only: a short syllable ("회", "게", "면") would match "주 3회", "함께", "라면" (seen 2026-10-25).
    "생선": ("생선", "고등어", "연어", "참치", "등푸른", "어류", "생선회", "멸치", "꽁치", "조기", "삼치", "갈치"),
    "갑각류": ("새우", "게살", "꽃게", "대게", "랍스터", "갑각류"), "조개": ("조개", "생굴", "굴을", "홍합", "전복", "패류"),
    "우유": ("우유", "요거트", "요구르트", "치즈", "유제품"), "달걀": ("달걀", "계란"), "견과": ("견과", "호두", "아몬드", "땅콩", "잣"),
    "밀": ("밀가루", "빵", "면류", "국수", "파스타"), "콩": ("콩류", "두부", "두유", "된장", "낫토", "콩밥", "콩 반찬", "콩을"), "복숭아": ("복숭아",),
    "메밀": ("메밀", "막국수"), "돼지고기": ("돼지고기", "삼겹살", "돈육"), "소고기": ("소고기", "쇠고기", "소 고기"), "닭고기": ("닭", "닭고기"),
    "술": ("술", "맥주", "소주", "와인", "반주"), "커피": ("커피", "카페인"),
}
# What stands in when a constraint removes a suggestion (per topic, food side only; exercise and life lines are not
# about foods).
FOOD_ALTERNATIVES: dict[str, dict[str, str]] = {
    "vascular": {"category": "food", "what": "붉은 고기·튀김·가공육 대신 두부·콩·달걀·채소", "amount": "고기·튀김 식사 주 3회 이하, 콩·두부 반찬 매일",
                 "why": "포화지방을 줄이고 식이섬유를 늘리면 LDL이 내려가는 쪽으로 움직입니다", "check": "다음 검진의 LDL·총콜레스테롤"},
    "nutrition-gut": {"category": "food", "what": "달걀 노른자·버섯·비타민 D 강화 우유(또는 두유)", "amount": "달걀 하루 1개, 버섯 반찬 주 3회",
                      "why": "음식으로 보탤 수 있는 비타민 D는 적어 햇빛과 함께 가야 합니다", "check": "다음 검진의 비타민 D"},
}
BOUNDARY = "Smith는 생활 코치입니다. 검진 보고서가 쓴 소견을 풀어 설명하고 생활 습관을 돕되, 진단·처방은 의료진의 몫입니다."


@dataclass(frozen=True)
class Period:
    day: date            # The letter's day (Sunday for the plan, Wednesday for the midweek check).
    week_key: str        # ISO week, "2026-W44": the run key and the snapshot key.
    week_start: date     # Monday.
    week_end: date       # Sunday.
    kind: str            # "plan" | "midweek"


def period(day: date, kind: str = "plan") -> Period:
    iso = day.isocalendar()
    monday = day - timedelta(days=day.weekday())
    return Period(day, f"{iso.year}-W{iso.week:02d}", monday, monday + timedelta(days=6), kind)


def topic_for(config: dict[str, Any], day: date) -> str:
    """The week's topic: goal topics every other week, the rest in between (health design §3). Deterministic in the
    ISO week number, so a resend of the same week keeps its topic and no topic depends on what was sent before."""
    section = config.get("health", {})
    order = [t for t in section.get("topics", list(TOPICS)) if t in TOPICS] or list(TOPICS)
    goals = set(section.get("goals", []))
    priority = [t for t in order if goals & set(TOPICS[t]["goals"])]
    others = [t for t in order if t not in priority]
    if not priority or not others:
        return order[day.isocalendar().week % len(order)]
    pattern = []
    for index, topic in enumerate(priority):
        pattern += [topic, others[index % len(others)]]
    return pattern[day.isocalendar().week % len(pattern)]


def assemble(ledger: health.Ledger, config: dict[str, Any], *, day: date, today: date, topic: str | None = None,
             previous: dict[str, Any] | None = None, fitbit: dict[str, Any] | None = None) -> dict[str, Any]:
    """The letter's data for `day`. `previous` is the last letter's snapshot (its actions are reviewed this week);
    `fitbit` is the weekly activity summary once connected (None: estimated from the client's routine)."""
    when = period(day)
    key = topic or topic_for(config, day)
    spec = TOPICS[key]
    goals = [g for g in config.get("health", {}).get("goals", []) if g in GOALS]
    items = [item for item in (item_status(ledger, code) for code in spec["codes"]) if item is not None]
    constraints = [c.text for c in ledger.constraints]
    banned = banned_words(constraints)
    return {
        "product": PRODUCT, "day": day, "today": today, "week_key": when.week_key, "week_start": when.week_start,
        "week_end": when.week_end, "kind": when.kind,
        "topic": {"key": key, "label": spec["label"], "axis": spec["axis"], "codes": list(spec["codes"]), "charts": list(spec["charts"])},
        "goals": [_respecting(goal_row(ledger, g), banned) for g in goals],
        "items": items,
        "constraints": constraints, "banned_words": sorted(banned),
        "series": {code: series_rows(ledger, code) for code in spec["codes"] if health.series(ledger, code)},
        "posture": posture_rows(ledger) if spec.get("posture") else [],
        "findings": topic_findings(ledger, spec["tracks"]),
        "resolved_findings": [f.summary for f in ledger.findings if f.resolved_on is not None],
        "follow_ups": [{"track": f.track, "label": f.label, "summary": f.finding.summary, "on": f.finding.on, "due": f.due,
                        "days_left": f.days_left} for f in health.follow_ups(ledger, today)],
        "next_checkup": health.next_checkup(ledger),
        "genetics": trait_rows(ledger, spec["genetics"]),
        "microbiome": score_rows(ledger, spec["microbiome"]),
        "regimen": [{"name": r.name, "frequency": r.frequency, "since": r.since} for r in ledger.regimen],
        "regimen_notes": [n for n in health.interpretation_notes(ledger) if set(n["codes"]) & set(spec["codes"])],
        "routine": [{"activity": r.activity, "per_week": [r.per_week_min, r.per_week_max], "minutes": [r.minutes_min, r.minutes_max]}
                    for r in ledger.routine],
        "activity": fitbit if fitbit is not None else activity_estimate(ledger),
        "previous_actions": list((previous or {}).get("actions") or []),
        "previous_week": (previous or {}).get("week_key"),
        "fallback_actions": fallback_actions(key, banned),
        "sources": list(GUIDANCE_SOURCES),
        "uncertain": sorted({plain(o.code) for o in ledger.observations if o.uncertain and o.code in spec["codes"]}),
        "prose": None, "failures": {},
    }


def plain(code: str) -> str:
    return PLAIN.get(code, health.label(code))


def banned_words(constraints: list[str]) -> set[str]:
    """The food words no suggestion may carry, from the client's constraints ("생선 알레르기" rules out 생선·고등어·…)."""
    found: set[str] = set()
    for text in constraints:
        for group, words in FOOD_GROUPS.items():
            if group in text or any(w.strip() in text for w in words):
                found.update(w for w in words)
    return found


def violates(text: str, banned: set[str]) -> bool:
    return any(w in text for w in banned)


def fallback_actions(topic: str, banned: set[str]) -> list[dict[str, str]]:
    """The topic's guideline actions with any that names a ruled-out food replaced by the topic's alternative."""
    actions = []
    for action in FALLBACK_ACTIONS[topic]:
        if violates(f"{action['what']} {action['amount']} {action['why']}", banned):
            alternative = FOOD_ALTERNATIVES.get(topic)
            if alternative and not violates(f"{alternative['what']} {alternative['amount']}", banned) and alternative not in actions:
                actions.append(dict(alternative))
            continue
        actions.append(dict(action))
    return actions


def _respecting(goal: dict[str, Any], banned: set[str]) -> dict[str, Any]:
    """A goal row whose public do/avoid lines never name a ruled-out food."""
    for key in ("do", "avoid"):
        if violates(goal[key], banned):
            words = [w.strip() for w in re.split(r"[·,]", goal[key])]
            kept = [w for w in words if w and not violates(w, banned)]
            goal[key] = "·".join(kept) if kept else ("가능한 음식 안에서 단백질·채소를 늘리기" if key == "do" else goal[key])
    return goal


def item_status(ledger: health.Ledger, code: str, method: str | None = None) -> dict[str, Any] | None:
    """One item's state computed by code: the latest value with the report's range and flag, the previous value, the
    trend (which way it moved and whether that is better), and the level (inside the range, at its edge, outside).
    LDL follows the direct measurement when there is one (a calculated value is another series)."""
    rows = [o for o in health.series(ledger, code, method) if o.value is not None]
    if not rows:
        return None
    if method is None:
        # One way of measuring per trend (review 2026-10-25): the latest reading's method, LDL preferring the direct one.
        chosen = "direct" if code == "ldl" and any(o.method == "direct" for o in rows) else rows[-1].method
        rows = [o for o in rows if o.method == chosen]
    window = rows[-TREND_WINDOW:]
    return status_of(code, _obs(rows[-1]), _obs(rows[-2]) if len(rows) > 1 else None, len(rows),
                     baseline=_obs(window[0]) if len(window) > 1 else None)


def status_of(code: str, latest: dict[str, Any], previous: dict[str, Any] | None, count: int,
              baseline: dict[str, Any] | None = None) -> dict[str, Any]:
    """The trend and level words for one item from its latest, previous and baseline readings (dicts as `_obs` makes
    them). `baseline` is the earliest of the last TREND_WINDOW readings (default: the previous one)."""
    baseline = baseline or previous
    low, high = parse_ref(latest["ref"])
    value = latest["value"]
    inside = None
    if low is not None or high is not None:
        inside = (low is None or value >= low) and (high is None or value <= high)
    if latest["flag"] in ("high", "low") or inside is False and latest["flag"] != "attention":
        level = "out"
    elif latest["flag"] == "attention":
        level = "edge"
    elif inside or latest["flag"] == "normal":
        level = "normal"
    else:
        level = "unknown"
    better = BETTER.get(code, "range")
    trend, delta = "single", None
    if previous is not None and previous["value"] is not None:
        delta = value - previous["value"]
    if baseline is not None and baseline["value"] is not None:
        move = value - baseline["value"]
        if low is not None and high is not None:
            threshold = (high - low) * TREND_SHARE_OF_SPAN
        elif high is not None and high > 0:        # "40 이하": the span is the whole allowed band from zero.
            threshold = high * TREND_SHARE_OF_SPAN
        else:
            threshold = (abs(baseline["value"]) or Decimal(1)) * TREND_SHARE_OF_VALUE
        if abs(move) < threshold:
            trend = "flat"
        elif better == "down":
            trend = "improving" if move < 0 else "worsening"
        elif better == "up":
            trend = "improving" if move > 0 else "worsening"
        else:
            # The earlier reading is judged by its own report's range and flag, not today's (review 2026-10-25).
            was_inside = _inside(baseline)
            trend = "flat" if inside == was_inside else ("improving" if inside else "worsening")
    return {"code": code, "label": plain(code), "unit": latest["unit"], "latest": latest, "previous": previous, "count": count,
            "delta": delta, "trend": trend, "trend_word": TREND_WORDS[trend], "level": level, "level_word": LEVEL_WORDS[level],
            "better": better, "ref": latest["ref"]}


def _inside(reading: dict[str, Any]) -> bool | None:
    low, high = parse_ref(reading.get("ref"))
    if low is None and high is None:
        return True if reading.get("flag") == "normal" else (False if reading.get("flag") in ("high", "low") else None)
    return (low is None or reading["value"] >= low) and (high is None or reading["value"] <= high)


def goal_row(ledger: health.Ledger, goal: str) -> dict[str, Any]:
    """One dashboard line: the item's computed status (or the posture values on the latest device), plus the public
    do/avoid guidance the writer may replace."""
    spec = GOALS[goal]
    row: dict[str, Any] = {"key": goal, "label": spec["label"], "status": None, "pair": None, "posture": [],
                           "do": spec["do"], "avoid": spec["avoid"]}
    if "code" in spec:
        row["status"] = item_status(ledger, spec["code"], spec.get("method"))
        if spec.get("pair"):
            row["pair"] = item_status(ledger, spec["pair"])
    else:
        rows = posture_rows(ledger)
        if rows:
            device = rows[-1]["device"]   # The most recent measurement's device: only its own values are comparable.
            for code in spec["posture"]:
                same = [r for r in rows if r["device"] == device and r["code"] == code and r["value"] is not None]
                if same:
                    window = same[-TREND_WINDOW:]
                    status = status_of(code, same[-1], same[-2] if len(same) > 1 else None, len(same),
                                       baseline=window[0] if len(window) > 1 else None)
                    status["device"] = device
                    row["posture"].append(status)
    return row


def series_rows(ledger: health.Ledger, code: str) -> list[dict[str, Any]]:
    return [_obs(o) for o in health.series(ledger, code)]


def _obs(o: Any) -> dict[str, Any]:
    return {"code": o.code, "label": plain(o.code), "value": o.value, "text": o.text, "unit": o.unit, "ref": o.ref,
            "on": o.on, "flag": o.flag, "method": o.method, "uncertain": o.uncertain}


def posture_rows(ledger: health.Ledger) -> list[dict[str, Any]]:
    rows = [{"code": p.code, "label": plain(p.code), "value": p.value, "text": p.text, "unit": p.unit, "ref": None, "on": p.on,
             "flag": "unknown", "method": None, "uncertain": False, "device": p.device, "side": p.side, "note": p.note}
            for p in ledger.posture]
    return sorted(rows, key=lambda r: (r["on"], r["code"]))


def topic_findings(ledger: health.Ledger, tracks: tuple[str, ...]) -> list[dict[str, Any]]:
    """The topic's findings, newest first, with each track's history behind its latest reading."""
    rows = []
    for finding in sorted(ledger.findings, key=lambda f: f.on, reverse=True):
        if finding.track in tracks:
            rows.append({"id": finding.id, "track": finding.track, "track_label": health.track_label(finding.track or "", finding),
                         "area": finding.area, "summary": finding.summary, "detail": finding.detail, "on": finding.on,
                         "severity": finding.severity, "follow_up_months": finding.follow_up_months,
                         "recommendation": finding.recommendation, "resolved_on": finding.resolved_on,
                         "resolved_note": finding.resolved_note})
    return rows


def trait_rows(ledger: health.Ledger, words: tuple[str, ...]) -> list[dict[str, Any]]:
    """Genetic traits whose name carries one of the topic's words, flagged ones first. Reference only (DTC test)."""
    rows = [{"trait": t.trait, "area": t.area, "grade": t.grade, "risk_alleles": t.risk_alleles, "alleles_total": t.alleles_total,
             "korean_mean": t.korean_mean, "result": t.result}
            for t in ledger.genetics if any(w in t.trait for w in words)]
    return sorted(rows, key=lambda r: (r["grade"] in (None, "안심", "보통", "낮음"), r["trait"]))


def score_rows(ledger: health.Ledger, codes: tuple[str, ...]) -> list[dict[str, Any]]:
    if ledger.microbiome is None or not codes:
        return []
    return [{"code": s.code, "label": s.label, "value": s.value, "korean_mean": s.korean_mean, "grade": s.grade}
            for s in ledger.microbiome.scores if s.code in codes]


def activity_estimate(ledger: health.Ledger) -> dict[str, Any]:
    """Weekly activity from what the client said they do (until Fitbit): aerobic minutes as a range, strength days,
    against the guideline. Stretching/yoga sessions count half; nothing here is a measurement."""
    low = high = Decimal(0)
    strength_days = 0
    for item in ledger.routine:
        factor = ESTIMATE_DISCOUNT if any(w in item.activity for w in ("스트레칭", "요가")) else Decimal(1)
        low += item.per_week_min * item.minutes_min * factor
        high += item.per_week_max * item.minutes_max * factor
        if any(w in item.activity for w in STRENGTH_WORDS) and not any(w in item.activity for w in ("스트레칭", "요가")):
            strength_days += item.per_week_min
    return {"source": "estimate", "aerobic_minutes": [int(low), int(high)], "strength_days": strength_days,
            "guideline": {"aerobic_minutes": AEROBIC_MINUTES_WEEK, "strength_days": STRENGTH_DAYS_WEEK, "source": GUIDELINE_SOURCE},
            "meets_aerobic": low >= AEROBIC_MINUTES_WEEK, "meets_strength": strength_days >= STRENGTH_DAYS_WEEK}


# Health free text (a report's sentence, a device name, what the client takes) is masked before it reaches a model:
# the ledger import keeps identifiers out by construction, this is the second wall (review 2026-10-25).
_HEALTH_PII = (re.compile(r"(?:등록|수진|차트|환자|회원)\s*번호\s*[:：]?\s*[A-Za-z0-9-]{4,}"),
               re.compile(r"(?:생년월일|생일)\s*[:：]?\s*\d{2,4}[.\-/년]\s*\d{1,2}[.\-/월]\s*\d{1,2}일?"),
               re.compile(r"\d{4}[.\-/년]\s*\d{1,2}[.\-/월]\s*\d{1,2}일?\s*생(?![가-힣])"),
               re.compile(r"[가-힣]{2,4}\s*(?:환자|님|씨)(?=[\s,.:;)]|$)"))


def safe_text(value: Any) -> Any:
    """Free text with identifiers masked (the shared rules plus the health ones); non-text passes through."""
    if not isinstance(value, str):
        return value
    text = mask_identifiers(value)
    for pattern in _HEALTH_PII:
        text = pattern.sub("[식별정보 제거]", text)
    return text


def payload(data: dict[str, Any]) -> dict[str, Any]:
    """What the writer model receives: the topic's items with code's trend and level words, the goals, the series,
    the unresolved findings and notes, the activity, last week's actions. No name, birth date, hospital number or page
    reference leaves here, free text is masked, and a finding the client reported resolved is not sent at all
    (health design §7; review 2026-10-25)."""
    def obs(o: dict[str, Any] | None) -> dict[str, Any] | None:
        if o is None:
            return None
        return {"on": o["on"].isoformat(), "value": o["text"], "unit": o["unit"], "ref": o["ref"], "flag": o["flag"],
                **({"method": o["method"]} if o.get("method") else {})}

    def status(s: dict[str, Any] | None) -> dict[str, Any] | None:
        if s is None:
            return None
        return {"code": s["code"], "label": s["label"], "latest": obs(s["latest"]), "previous": obs(s["previous"]),
                "measurements": s["count"], "trend": s["trend"], "trend_word": s["trend_word"], "level": s["level"],
                "level_word": s["level_word"], "better": s["better"], **({"device": s["device"]} if s.get("device") else {})}
    return {
        "week": data["week_key"], "letter_day": data["day"].isoformat(), "kind": data["kind"],
        "topic": data["topic"],
        "goals": [{"key": g["key"], "label": g["label"], "status": status(g["status"]), "pair": status(g["pair"]),
                   "posture": [status(p) for p in g["posture"]]} for g in data["goals"]],
        "items": [status(i) for i in data["items"]],
        "series": {code: [obs(o) for o in rows] for code, rows in data["series"].items()},
        "posture": [{"label": p["label"], "value": p["text"], "unit": p["unit"], "on": p["on"].isoformat(), "device": safe_text(p["device"]),
                     "side": p["side"]} for p in data["posture"]],
        "findings": [{"on": f["on"].isoformat(), "exam": safe_text(f["track_label"]), "summary": safe_text(f["summary"]),
                      "severity": f["severity"], "recommendation": safe_text(f["recommendation"])}
                     for f in data["findings"] if f["resolved_on"] is None],
        "follow_ups": [{"exam": safe_text(f["label"]), "due": f["due"].isoformat(), "days_left": f["days_left"]} for f in data["follow_ups"]],
        "genetics_reference_only": [{"trait": safe_text(t["trait"]), "grade": t["grade"] or t["result"], "risk_alleles": t["risk_alleles"],
                                     "alleles_total": t["alleles_total"], "korean_mean": t["korean_mean"]} for t in data["genetics"]],
        "microbiome": [{**s, "label": safe_text(s["label"])} for s in data["microbiome"]],
        "regimen": [{**r, "name": safe_text(r["name"])} for r in data["regimen"]],
        "regimen_notes": [safe_text(n["text"]) for n in data["regimen_notes"]],
        "routine": [{**r, "activity": safe_text(r["activity"])} for r in data["routine"]], "activity": data["activity"],
        "previous_actions": [{k: safe_text(v) for k, v in a.items()} for a in data["previous_actions"] if isinstance(a, dict)],
        "previous_week": data["previous_week"],
        "uncertain_values": data["uncertain"],
        # What the client told Smith to respect in every suggestion (an allergy, a food they do not eat): the model
        # must honour it, and code checks every action and line against it (client mail 2026-10-25).
        "client_constraints": [safe_text(c) for c in data["constraints"]],
    }
