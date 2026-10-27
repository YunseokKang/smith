"""Tonight's sleep corner of the evening mail (daily design §16.7, user request 2026-10-27): what to do in the bedroom
tonight given the district's weather — a humidifier or a dehumidifier, a heavier blanket before a cold dawn, the window
against a strong wind — and what the client's own recent nights say (Fitbit through the Google Health API, read-only):
a bedtime from his usual wake time, sleep debt, an irregular bedtime, a long time to fall asleep, a body that has not
recovered.

Everything here is a rule in code with its basis written next to it; no model sees these figures (the health record
stays out of model input in the daily thread). The mail shows the outcome as lines; the client decides. This is a
coach's suggestion, not a medical judgement: no condition is named and nothing is prescribed.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from statistics import median
from typing import Any

from smith import weather

NEED_MINUTES = 450          # 7½ hours in bed for about seven hours asleep.
DEBT_EXTRA = 30             # Thirty minutes earlier after a short week.
SHORT_WEEK = 420            # Under seven hours on average (the 7~9 hours guidance).
SHORT_NIGHT = 360
IRREGULAR_MINUTES = 60      # Bedtimes spread wider than this across the week.
SLOW_ONSET = 30             # Minutes to fall asleep.
LOW_EFFICIENCY = 0.85       # Asleep / in bed.
RHR_UP = 3                  # bpm above the four-week median.
HRV_DOWN = 0.85             # Of the four-week median.
STALE_AFTER = timedelta(hours=48)
DRY_RH, HUMID_RH = 45.0, 75.0
COLD_NIGHT, FREEZING, TROPICAL = 5.0, 0.0, 25.0
BIG_DROP = 6.0
BASIS = {"duration": "성인 수면 7~9시간: 미국수면재단 권고(2015)",
         "room": "침실 18~20℃ 안팎: 미국 Sleep Foundation",
         "humidity": "실내 습도 40~50%: 미국 환경보호청 권고(30~50%)와 국내 생활 권고(40~60%)가 겹치는 구간"}


@dataclass(frozen=True)
class Plan:
    bedtime: str | None            # "23:00", from the usual wake time; None without enough nights.
    wake: str | None               # The wake time the bedtime counts back from.
    wake_reason: str               # "평소 기상" or "내일 첫 일정 09:00".
    environment: tuple[str, ...]   # Weather-led actions for the bedroom tonight.
    body: tuple[str, ...]          # What the recent nights suggest.
    facts: tuple[str, ...]         # The figures behind it, one short line each.
    basis: tuple[str, ...]         # The guidance the lines lean on.
    data_note: str | None          # Why the personal part is missing or partial, when it is.


def plan(*, night: weather.Night | None, now_obs: weather.Observation | None, days: dict[str, dict[str, Any]],
         synced_at: datetime | None, tonight: date, now: datetime, first_event: datetime | None, rest_day: bool,
         airways_sensitive: bool, air_bad: bool, sleep_failed: bool = False) -> Plan:
    """The corner for the night of `tonight` (sleep booked on the morning it ends, so last night is `tonight`).
    `synced_at` is the sleep source's own last successful read; `sleep_failed` says the latest read of it failed."""
    environment, facts, basis = _environment(night, now_obs, airways_sensitive, air_bad)
    nights = _nights(days, tonight)
    note = None
    if synced_at is None or not nights:
        note = "수면 자료가 아직 없습니다(Fitbit 연결 뒤 채워집니다)."
    elif now - synced_at > STALE_AFTER:
        note = f"수면 자료가 {synced_at:%m/%d} 이후 갱신되지 않았습니다."
    elif sleep_failed:
        note = f"최근 동기화에서 수면 자료를 받지 못해 {synced_at:%m/%d} 자료까지 반영했습니다."
    body, sleep_facts = ([], []) if not nights else _body(nights, days, tonight)
    bedtime, wake, why = _bedtime(nights, first_event, rest_day, short=_short_week(nights))
    if body or bedtime:
        basis.append(BASIS["duration"])
    return Plan(bedtime, wake, why, tuple(environment), tuple(body), tuple(facts + sleep_facts), tuple(dict.fromkeys(basis)), note)


def _environment(night: weather.Night | None, now_obs: weather.Observation | None, airways_sensitive: bool,
                 air_bad: bool) -> tuple[list[str], list[str], list[str]]:
    items: list[str] = []
    facts: list[str] = []
    basis: list[str] = []
    if now_obs is not None and now_obs.humidity is not None:
        facts.append(f"지금 동네 습도 {now_obs.humidity:g}%" + (f", 기온 {now_obs.temp:g}℃" if now_obs.temp is not None else ""))
    if night is None:
        facts.append("밤 예보를 받지 못했습니다")
        return items, facts, basis
    if night.rh_mean is not None:
        facts.append(f"오늘 밤 바깥 습도 평균 {night.rh_mean:g}%")
    if night.tmin is not None:
        facts.append(f"새벽 최저 {night.tmin:g}℃" + (f"({night.tmin_hour:02d}시)" if night.tmin_hour is not None else ""))
    cold = night.tmin is not None and night.tmin <= COLD_NIGHT
    if night.rh_mean is not None:
        if night.rh_mean < DRY_RH or (cold and night.rh_mean < 60):
            why = "바깥이 건조하고" if night.rh_mean < DRY_RH else "난방을 켜면 실내가 금방 건조해지고"
            items.append(f"가습기를 틀고 주무세요. {why}{', 기관지가 예민한 편이니 특히' if airways_sensitive else ''} 침실 습도 40~50%를 맞추면 좋습니다")
            basis.append(BASIS["humidity"])
        elif night.rh_mean >= HUMID_RH and night.tmin is not None and night.tmin >= 15:
            items.append("제습기나 에어컨 제습을 켜 두세요. 침실 습도를 60% 아래로 낮추면 덜 눅눅합니다")
            basis.append(BASIS["humidity"])
    if night.tmin is not None:
        drop = None if night.t_first is None else round(night.t_first - night.tmin, 1)
        if night.tmin >= TROPICAL:
            items.append("열대야입니다. 에어컨 취침 예약과 얇은 이불이 좋습니다")
        elif cold or (drop is not None and drop >= BIG_DROP):
            gap = f"자기 전보다 {drop:g}℃ 낮은 " if drop is not None and drop >= 1 else ""
            items.append(f"새벽에 {gap}{night.tmin:g}℃까지 떨어집니다. 두꺼운 이불을 꺼내 두세요")
            if night.tmin <= FREEZING:
                items.append("영하로 내려갑니다. 보일러 예약을 새벽 시간에 맞춰 두세요")
            items.append("난방은 너무 덥지 않게, 침실은 18~20℃ 안팎이 잠들기 좋습니다")
            basis.append(BASIS["room"])
    if night.wind_max is not None and night.wind_max >= weather.WINDY:
        items.append("밤사이 바람이 강합니다. 창문을 단단히 닫아 외풍을 막으세요")
    if air_bad:
        items.append("내일 미세먼지가 나쁨 예보입니다. 자기 전 환기는 짧게 하고 공기청정기를 켜 두세요")
    return items, facts, basis


def _nights(days: dict[str, dict[str, Any]], tonight: date, span: int = 7) -> list[dict[str, Any]]:
    """The last `span` nights with sleep, newest first (a night is booked on the morning it ended)."""
    found = []
    for back in range(span):
        day = days.get((tonight - timedelta(days=back)).isoformat()) or {}
        if day.get("sleep_minutes"):
            found.append({**day, "day": tonight - timedelta(days=back)})
    return found


def _short_week(nights: list[dict[str, Any]]) -> bool:
    return len(nights) >= 3 and sum(n["sleep_minutes"] for n in nights) / len(nights) < SHORT_WEEK


def _body(nights: list[dict[str, Any]], days: dict[str, dict[str, Any]], tonight: date) -> tuple[list[str], list[str]]:
    items: list[str] = []
    facts: list[str] = []
    minutes = [n["sleep_minutes"] for n in nights]
    average = sum(minutes) / len(minutes)
    last = nights[0] if nights[0]["day"] == tonight else None
    facts.append(f"최근 {len(nights)}일 평균 수면 {_hm(average)}" + (f", 어젯밤 {_hm(last['sleep_minutes'])}" if last else ""))
    if _short_week(nights):
        items.append(f"최근 {len(nights)}일 평균 수면이 {_hm(average)}입니다. 권장 7~9시간보다 짧으니 오늘은 평소보다 30분 일찍 누우세요")
    elif last is not None and last["sleep_minutes"] < SHORT_NIGHT:
        items.append(f"어젯밤 {_hm(last['sleep_minutes'])}만 잤습니다. 오늘은 일찍 쉬는 편이 좋습니다")
    beds = [_from_noon(n["bed_at"]) for n in nights if n.get("bed_at")]
    if len(beds) >= 3 and max(beds) - min(beds) > IRREGULAR_MINUTES:
        items.append(f"잠드는 시각이 {_clock(min(beds))}~{_clock(max(beds))}로 들쭉날쭉합니다. 매일 비슷한 시각에 눕는 것이 숙면에 가장 좋습니다")
    onset = [n["latency_minutes"] for n in nights if n.get("latency_minutes") is not None]
    if len(onset) >= 3 and median(onset) > SLOW_ONSET:
        items.append(f"잠들기까지 보통 {median(onset):g}분 걸립니다. 잠자리 30분 전부터 휴대폰과 밝은 화면을 멀리 두세요")
    # The main sleep's minutes over its own period: a nap adds to sleep_minutes but not to the period (review 2026-10-27).
    ratio = [n["main_minutes"] / n["in_bed_minutes"] for n in nights if n.get("in_bed_minutes") and n.get("main_minutes")]
    if len(ratio) >= 3 and median(ratio) < LOW_EFFICIENCY:
        items.append(f"누워 있는 시간 중 실제로 잔 비율이 {median(ratio) * 100:.0f}%입니다. 잠이 안 오면 20분쯤 뒤 일어났다가 졸릴 때 다시 누우세요")
    items += _recovery(days, tonight, facts)
    return items, facts


def _recovery(days: dict[str, dict[str, Any]], tonight: date, facts: list[str]) -> list[str]:
    """Resting heart rate up or heart-rate variability down against the four weeks before: rest tonight."""
    today = days.get(tonight.isoformat()) or {}
    before = [days.get((tonight - timedelta(days=n)).isoformat()) or {} for n in range(1, 29)]
    signs: list[str] = []
    rhr = [d["resting_hr"] for d in before if d.get("resting_hr")]
    if today.get("resting_hr") and len(rhr) >= 7 and today["resting_hr"] - median(rhr) >= RHR_UP:
        signs.append(f"안정 시 심박이 4주 중앙값보다 {today['resting_hr'] - median(rhr):g}회 높음")
    hrv = [d["hrv_ms"] for d in before if d.get("hrv_ms")]
    if today.get("hrv_ms") and len(hrv) >= 7 and today["hrv_ms"] <= median(hrv) * HRV_DOWN:
        signs.append(f"심박 변이도가 4주 중앙값의 {today['hrv_ms'] / median(hrv) * 100:.0f}%")
    if not signs:
        return []
    facts.append("회복 신호: " + ", ".join(signs))
    return ["몸이 아직 덜 회복됐다는 신호가 있습니다. 늦은 운동과 음주를 피하고 일찍 쉬세요"]


def _bedtime(nights: list[dict[str, Any]], first_event: datetime | None, rest_day: bool,
             short: bool) -> tuple[str | None, str | None, str]:
    """The bedtime counted back from the usual wake time (the median of the week), or from an early first event."""
    wakes = [_from_midnight(n["wake_at"]) for n in nights if n.get("wake_at")]
    if len(wakes) < 3:
        return None, None, ""
    wake, reason = int(median(wakes)), "평소 기상"
    if first_event is not None and not rest_day:
        early = first_event.hour * 60 + first_event.minute - 90
        if 0 < early < wake:
            wake, reason = early, f"내일 첫 일정 {first_event:%H:%M}"
    bed = (wake - NEED_MINUTES - (DEBT_EXTRA if short else 0)) % (24 * 60)
    return _round_clock(bed), _round_clock(wake), reason


def _from_noon(clock: str) -> int:
    """Minutes after noon, so a bedtime around midnight compares in one line (23:30 < 00:30)."""
    hours, minutes = (int(p) for p in clock.split(":"))
    return (hours * 60 + minutes - 12 * 60) % (24 * 60)


def _from_midnight(clock: str) -> int:
    hours, minutes = (int(p) for p in clock.split(":"))
    return hours * 60 + minutes


def _clock(from_noon: int) -> str:
    total = (from_noon + 12 * 60) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def _round_clock(minutes: int) -> str:
    rounded = int(round(minutes / 10) * 10) % (24 * 60)
    return f"{rounded // 60:02d}:{rounded % 60:02d}"


def _hm(minutes: float) -> str:
    whole = int(round(minutes))
    return f"{whole // 60}시간 {whole % 60}분" if whole % 60 else f"{whole // 60}시간"
