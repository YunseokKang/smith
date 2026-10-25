"""The health ledger (FR-39): the client's check-up values, written findings, genetic and gut-microbiome reports,
posture measurements, what they take and how they move, kept as JSON under `data/health/` (next to the finance
ledger, ignored by Git) so the weekly health letter and its replies read structured records, never the PDFs.

Rules (health design §4):
- No identifiers are stored: values, units, reference ranges, dates, the reports' own sentences and page refs only.
- A value measured another way is another series (LDL direct vs calculated; a posture device vs another): `series`
  keeps them apart and `check` names mixed series so a trend never joins them silently.
- An unknown value is absent, never zero. Years differ in what was measured; trends join the points that exist.
- A finding belongs to a `track` (위내시경, 경동맥 초음파, ...). The latest finding on a track decides the next
  follow-up from the interval the report itself wrote; a finding the client reports as resolved drops out.
- Only track labels reach the daily date book (`as_dates`): "위내시경 추적 D-30", never a finding or a value.
- Import is idempotent: an identical record is skipped, a different value on the same key is a conflict that stops
  the whole import (nothing is written), so a typo never overwrites history.
"""
import json
import os
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from smith import people

FILES = ("observations", "findings", "posture", "genetics", "microbiome", "regimen", "routine", "constraints")
FLAGS = ("normal", "high", "low", "attention", "unknown")
SEVERITIES = ("normal", "note", "follow_up", "see_doctor")
# Lab and exam codes: code -> (Korean label, usual unit). Imports accept only these, so a batch cannot invent a column.
CODES: dict[str, tuple[str, str]] = {
    "height": ("신장", "cm"), "weight": ("체중", "kg"), "bmi": ("체질량지수", "kg/m²"), "waist": ("허리둘레", "cm"),
    "body_fat_pct": ("체지방률", "%"), "body_fat_kg": ("체지방량", "kg"), "muscle_mass": ("근육량", "kg"),
    "abdominal_fat_ratio": ("복부지방률", ""), "sbp": ("수축기 혈압", "mmHg"), "dbp": ("이완기 혈압", "mmHg"),
    "pulse": ("맥박", "회/분"), "tc": ("총콜레스테롤", "mg/dL"), "ldl": ("LDL 콜레스테롤", "mg/dL"),
    "hdl": ("HDL 콜레스테롤", "mg/dL"), "tg": ("중성지방", "mg/dL"), "apoa1": ("아포지단백 A1", "mg/dL"),
    "apob": ("아포지단백 B", "mg/dL"), "lpa": ("지단백(a)", "mg/dL"), "hs_crp": ("고감도 CRP", "mg/L"),
    "homocysteine": ("호모시스테인", "µmol/L"), "glucose_fasting": ("공복혈당", "mg/dL"), "hba1c": ("당화혈색소", "%"),
    "insulin": ("인슐린", "µU/mL"), "ast": ("AST", "U/L"), "alt": ("ALT", "U/L"), "ggt": ("감마GT", "U/L"),
    "alp": ("ALP", "U/L"), "bilirubin_total": ("총빌리루빈", "mg/dL"), "protein_total": ("총단백", "g/dL"),
    "albumin": ("알부민", "g/dL"), "bun": ("BUN", "mg/dL"), "creatinine": ("크레아티닌", "mg/dL"),
    "egfr": ("사구체여과율", "mL/min/1.73m²"), "uric_acid": ("요산", "mg/dL"), "sodium": ("나트륨", "mmol/L"),
    "potassium": ("칼륨", "mmol/L"), "calcium": ("칼슘", "mg/dL"), "magnesium": ("마그네슘", "mg/dL"),
    "amylase": ("아밀라아제", "U/L"), "tsh": ("갑상선자극호르몬", "µIU/mL"), "ft4": ("유리 T4", "ng/dL"),
    "ft3": ("유리 T3", "pg/mL"), "afp": ("AFP", "ng/mL"), "cea": ("CEA", "ng/mL"), "psa": ("PSA", "ng/mL"),
    "ca19_9": ("CA 19-9", "U/mL"), "nse": ("NSE", "ng/mL"), "cyfra": ("Cyfra 21-1", "ng/mL"),
    "wbc": ("백혈구", "10³/µL"), "hb": ("혈색소", "g/dL"), "hct": ("헤마토크릿", "%"), "platelet": ("혈소판", "10³/µL"),
    "ferritin": ("페리틴", "ng/mL"), "iron": ("철", "µg/dL"), "tibc": ("총철결합능", "µg/dL"),
    "vitamin_d": ("비타민 D", "ng/mL"), "hbs_ab": ("B형간염 항체", ""), "hcv_ab": ("C형간염 항체", ""),
    "hav_ab": ("A형간염 항체", ""), "rf": ("류마티스인자", "IU/mL"), "cotinine": ("코티닌", "ng/mL"),
    "iop_r": ("안압(우)", "mmHg"), "iop_l": ("안압(좌)", "mmHg"), "va_r": ("교정시력(우)", ""), "va_l": ("교정시력(좌)", ""),
    "fvc_pct": ("폐활량 FVC", "%예측"), "fev1_pct": ("1초 호기량 FEV1", "%예측"), "fev1_fvc": ("FEV1/FVC", "%"),
    "cv_risk_10y_pct": ("심뇌혈관 10년 위험", "%"), "cv_age": ("심뇌혈관 나이", "세"), "health_age": ("건강나이", "세"),
    "stress_index": ("스트레스 지수", ""), "hrv": ("심박변이도", "ms"), "heart_age": ("심장 나이", "세"),
    "bone_density_t": ("골밀도 T", ""),
    # Body-composition analyzer (InBody) printout, 2026 check-up p.18 (user 2026-10-25).
    "skeletal_muscle_mass": ("골격근량", "kg"), "fat_free_mass": ("제지방량", "kg"), "visceral_fat_level": ("내장지방 레벨", ""),
    "whr": ("복부지방률(허리/엉덩이)", ""), "bmr": ("기초대사량", "kcal"), "inbody_score": ("인바디 점수", "점"),
    "body_water": ("체수분", "L"), "protein_mass": ("단백질량", "kg"), "mineral_mass": ("무기질량", "kg"),
}
POSTURE_CODES: dict[str, tuple[str, str]] = {
    "forward_head_deg": ("거북목 각도", "°"), "forward_head_mm": ("거북목 거리", "mm"), "pcmt_kg": ("목 뒤 근긴장", "kg"),
    "pelvic_tilt_deg": ("골반 경사", "°"), "pelvic_tilt_mm": ("골반 경사 거리", "mm"), "knee_flex_deg": ("무릎 각도", "°"),
    "shoulder_tilt_deg": ("어깨 기울기", "°"), "head_tilt_deg": ("머리 기울기", "°"),
    "pelvis_tilt_lr_deg": ("골반 좌우 기울기", "°"), "knee_tilt_deg": ("무릎 기울기", "°"),
    "ankle_tilt_deg": ("발목 기울기", "°"), "q_angle_deg": ("Q각", "°"), "loh_mm": ("키 손실", "mm"),
    "rotation_l_deg": ("좌회전", "°"), "rotation_r_deg": ("우회전", "°"), "lateral_bend_l_deg": ("왼쪽 기울임", "°"),
    "lateral_bend_r_deg": ("오른쪽 기울임", "°"), "overall_index": ("종합 지수", ""), "overall_grade": ("종합 단계", ""),
    "misalignment_dev": ("부정렬 편차", ""), "imbalance_dev": ("불균형 편차", ""),
}
# Follow-up tracks: the exam a finding belongs to. Only these labels ever leave the health thread (as a date).
TRACKS: dict[str, str] = {
    "egd": "위내시경", "colonoscopy": "대장내시경", "liver-us": "상복부 초음파", "carotid-us": "경동맥 초음파",
    "echo": "심장초음파", "tcd": "뇌혈류 검사", "brain-ct": "뇌 CT", "lung-ct": "저선량 폐 CT", "thyroid-us": "갑상선 초음파",
    "prostate-us": "전립선 초음파", "cac-ct": "관상동맥 석회화 CT", "eye": "안과 검진", "ecg": "심전도", "chest-xray": "흉부 X선",
    "lung-function": "폐기능 검사", "general": "건강검진", "dental": "구강검진", "fobt": "대변잠혈검사",
}
# Findings on a blood, urine or questionnaire item: tracked in the health letter, never exported as a date (the next
# check-up covers them, and "요산 검사 추적 D-30" beside "건강검진 D-30" would only repeat the same day).
LAB_TRACKS: dict[str, str] = {
    "lipids": "지질", "uric-acid": "요산", "vitamin-d": "비타민 D", "hepatitis-ab": "간염 항체", "rf": "류마티스인자",
    "body-composition": "체성분", "urine": "소변검사", "metabolic": "대사증후군", "cv-risk": "심뇌혈관 위험도",
    "hf-screen": "심부전 선별", "lifestyle": "생활습관", "mental": "마음건강", "verdict": "종합 판정",
}
POSTURE_TEXT = {"overall_grade"}
# Result words a report prints instead of, or beside, a number ("음성", "양성 (785.00)"): the word stays in `text`.
QUALITATIVE_WORDS = {"음성", "양성", "약양성", "정상", "비정상", "불검출", "검출", "negative", "positive"}
CHECKUP_INTERVAL_MONTHS = 12
FOLLOW_UP_REMIND = (30, 7)
# What a medicine the client takes does to a check-up value: the interpretation note the ledger attaches (health
# design §4). Source and the day it was checked sit in the row; a medicine not here changes nothing.
REGIMEN_EFFECTS: dict[str, dict[str, Any]] = {
    "dutasteride": {"codes": ("psa",), "class": "5-알파 환원효소 억제제", "label_frequency": "daily",
                    "note": "PSA가 복용 3~6개월 뒤 약 절반으로 낮아지므로 한 값은 2배로 놓고 참고 범위와 비교하고, "
                            "복용 3개월 뒤 값을 새 기준선으로 삼아 그 최저값에서 오르는지를 본다.",
                    "source": "제품 라벨(AVODART prescribing information), 확인 2026-10-25"},
    "finasteride": {"codes": ("psa",), "class": "5-알파 환원효소 억제제", "label_frequency": "daily",
                    "note": "PSA가 복용 뒤 약 절반으로 낮아지므로 한 값은 2배로 놓고 참고 범위와 비교하고, "
                            "복용 중 최저값에서 오르는지를 본다.",
                    "source": "제품 라벨(PROPECIA/PROSCAR prescribing information), 확인 2026-10-25"},
}
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
MAX_TEXT = 400


class HealthError(ValueError):
    """A file or batch is unreadable or breaks a rule; the message names the record so the client can fix it."""


@dataclass(frozen=True)
class Observation:
    code: str
    value: Decimal | None   # None when the report printed a result word alone ("음성") and no number.
    text: str            # The value as printed ("<0.5" keeps its sign in `text`; `value` is the number).
    unit: str
    ref: str | None      # The report's own reference text ("0~129"); None when it printed none.
    on: date
    flag: str
    method: str | None   # "direct" | "calculated" for LDL; None elsewhere.
    source: str
    uncertain: bool = False
    note: str | None = None   # How the value or flag was read ("flag from the printed range"), for the reader.

    @property
    def series_key(self) -> tuple[str, str | None]:
        return (self.code, self.method)


@dataclass(frozen=True)
class Finding:
    id: str
    area: str
    summary: str
    detail: str | None
    on: date
    track: str | None
    follow_up_months: int | None
    recommendation: str | None
    severity: str
    source: str
    resolved_on: date | None = None
    resolved_note: str | None = None
    uncertain: bool = False   # Read from an unlabelled chart or a pictogram; the letter marks it.


@dataclass(frozen=True)
class Posture:
    code: str
    value: Decimal | None   # None only for POSTURE_TEXT codes (a printed grade such as "6단계").
    text: str
    unit: str
    on: date
    device: str          # The device/report label; values from different labels are never joined (health design §4).
    side: str | None
    note: str | None
    source: str


@dataclass(frozen=True)
class Trait:
    trait: str
    area: str
    grade: str | None
    risk_alleles: int | None
    alleles_total: int | None
    korean_mean: Decimal | None
    genes: tuple[str, ...]
    source: str
    result: str | None = None   # An unscored trait's printed result ("아침형"); grade is None then.


@dataclass(frozen=True)
class Score:
    code: str
    label: str
    value: str
    korean_mean: str | None
    grade: str | None


@dataclass(frozen=True)
class Microbiome:
    on: date
    lab: str
    scores: tuple[Score, ...]
    enterotype: str | None
    probiotics_detected: tuple[str, ...]
    probiotics_absent: tuple[str, ...]
    enterotype_label: str | None = None
    wellness_type: str | None = None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Regimen:
    """Something the client takes, as they told it: name, active ingredient, how often, since when. Never a dose judgement."""
    name: str
    ingredient: str | None
    frequency: str       # "daily" | "weekly" | free text
    since: str | None    # "2018-06" or a year; the client's recollection.
    note: str | None


@dataclass(frozen=True)
class Routine:
    """A regular activity as the client told it: the Fitbit record checks it later; before that it stands in."""
    activity: str
    per_week_min: int
    per_week_max: int
    minutes_min: int
    minutes_max: int
    note: str | None


@dataclass(frozen=True)
class Constraint:
    """Something the client told Smith to respect in every suggestion (a food allergy, a food they do not eat, a joint
    that must not be loaded), as they said it. The letter and the replies filter by it in code (client mail 2026-10-25)."""
    text: str
    since: str | None
    source: str | None


@dataclass(frozen=True)
class Ledger:
    observations: tuple[Observation, ...] = ()
    findings: tuple[Finding, ...] = ()
    posture: tuple[Posture, ...] = ()
    genetics: tuple[Trait, ...] = ()
    genetics_types: tuple[str, ...] = ()
    genetics_on: date | None = None
    microbiome: Microbiome | None = None
    regimen: tuple[Regimen, ...] = ()
    routine: tuple[Routine, ...] = ()
    constraints: tuple[Constraint, ...] = ()


@dataclass(frozen=True)
class FollowUp:
    track: str
    label: str           # The track's label: what the date book may show.
    finding: Finding
    due: date
    days_left: int       # Negative when overdue.


@dataclass(frozen=True)
class ImportResult:
    added: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    conflicts: tuple[str, ...] = ()


def path(db: Path) -> Path:
    """`data/health/` next to the finance ledger (data/smith.db): private by the same rule."""
    return Path(db).parent / "health"


def load(folder: Path) -> Ledger:
    """The validated ledger; an absent folder or file is empty, a broken file raises HealthError."""
    raw = {name: _read(folder / f"{name}.json") for name in FILES}
    return parse(raw)


def parse(raw: dict[str, Any]) -> Ledger:
    raw = normalize(raw)
    observations = tuple(_observation(item) for item in _list(raw.get("observations"), "observations"))
    findings = tuple(_finding(item) for item in _list(raw.get("findings"), "findings"))
    ids = [f.id for f in findings]
    if len(set(ids)) != len(ids):
        raise HealthError("findings: duplicate id " + ", ".join(sorted({i for i in ids if ids.count(i) > 1})))
    posture = tuple(_posture(item) for item in _list(raw.get("posture"), "posture"))
    genetics_raw = raw.get("genetics") or {}
    if not isinstance(genetics_raw, dict) or set(genetics_raw) - _GENETICS_KEYS:
        raise HealthError("genetics must be an object with genetics, types, on (and the report's area_summary)")
    traits = tuple(_trait(item) for item in _list(genetics_raw.get("genetics"), "genetics"))
    types = tuple(_texts(genetics_raw.get("types"), "genetics.types"))
    genetics_on = _date(genetics_raw.get("on"), "genetics.on", required=bool(traits))
    microbiome = _microbiome(raw.get("microbiome"))
    regimen = tuple(_regimen(item) for item in _list(raw.get("regimen"), "regimen"))
    routine = tuple(_routine(item) for item in _list(raw.get("routine"), "routine"))
    constraints = tuple(_constraint(item) for item in _list(raw.get("constraints"), "constraints"))
    return Ledger(tuple(sorted(observations, key=lambda o: (o.code, o.on))), tuple(sorted(findings, key=lambda f: f.on)),
                  tuple(sorted(posture, key=lambda p: (p.code, p.on))), traits, types, genetics_on, microbiome, regimen, routine,
                  constraints)


def add_constraint(folder: Path, text: str, *, since: str | None, source: str) -> bool:
    """Record one constraint the client stated (idempotent on the text). Returns True when it was new."""
    result = import_batch(folder, {"constraints": [{"text": text.strip(), "since": since, "source": source}]})
    return result.added.get("constraints", 0) > 0


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """A genetics file written flat ({"genetics": [...], "types": [...], "on": ...}) as the nested section."""
    if isinstance(raw.get("genetics"), list):
        flat = {k: raw.get(k) for k in _GENETICS_KEYS if k in raw}
        raw = {k: v for k, v in raw.items() if k not in _GENETICS_KEYS} | {"genetics": flat}
    return raw


def resolve(folder: Path, finding_id: str, on: date, note: str) -> Finding:
    """Mark a finding resolved on the client's word (a re-measurement, a doctor's reading): it leaves the follow-ups.

    Raises:
        HealthError: no such finding, or the files are unreadable.
    """
    current = _read(folder / "findings.json") or {"findings": []}
    rows = _list(current, "findings")
    hit = [r for r in rows if isinstance(r, dict) and r.get("id") == finding_id]
    if not hit:
        raise HealthError(f"findings: no finding with id {finding_id!r}")
    hit[0]["resolved_on"], hit[0]["resolved_note"] = on.isoformat(), note.strip()
    ledger = parse({"findings": rows})
    with _locked(folder):
        _write_all(folder, {"findings": {"findings": rows}})
    return next(f for f in ledger.findings if f.id == finding_id)


def series(ledger: Ledger, code: str, method: str | None = None) -> list[Observation]:
    """The observations of one code in date order; `method` narrows to one way of measuring (None: every method)."""
    return [o for o in ledger.observations if o.code == code and (method is None or o.method == method)]


def latest(ledger: Ledger, code: str) -> Observation | None:
    found = series(ledger, code)
    return found[-1] if found else None


def label(code: str) -> str:
    return CODES.get(code, POSTURE_CODES.get(code, (code, "")))[0]


def follow_ups(ledger: Ledger, today: date) -> list[FollowUp]:
    """The next check per track from the latest reading on it, from the interval the report wrote; resolved findings
    and tracks whose latest reading sets no interval have none. When one day's reading left several findings on a
    track, the one asking for a follow-up (the longest interval) speaks for the day. Soonest first; overdue ones
    (negative days) lead."""
    latest_on_track: dict[str, Finding] = {}
    for finding in ledger.findings:
        track = finding.track or finding.id
        current = latest_on_track.get(track)
        if current is None or finding.on > current.on or (finding.on == current.on and
                                                          (finding.follow_up_months or 0) > (current.follow_up_months or 0)):
            latest_on_track[track] = finding
    found = []
    for track, finding in latest_on_track.items():
        if finding.follow_up_months is None or finding.resolved_on is not None:
            continue
        due = add_months(finding.on, finding.follow_up_months)
        found.append(FollowUp(track, track_label(track, finding), finding, due, (due - today).days))
    return sorted(found, key=lambda f: f.due)


def track_label(track: str, finding: Finding) -> str:
    return TRACKS.get(track) or LAB_TRACKS.get(track) or finding.area


def next_checkup(ledger: Ledger) -> date | None:
    """A year after the latest measured value: the usual annual check-up, shown as an estimate."""
    if not ledger.observations:
        return None
    return add_months(max(o.on for o in ledger.observations), CHECKUP_INTERVAL_MONTHS)


def as_dates(ledger: Ledger, today: date) -> list[people.Dated]:
    """Follow-ups and the next check-up as expiry dates for the daily date book: track labels only, future dates
    only (an overdue follow-up is the health letter's business, not the morning mail's). Findings on a track outside
    `TRACKS` stay inside the health thread, so no finding text can reach the daily model through a label."""
    dates = []
    for item in follow_ups(ledger, today):
        if item.track in TRACKS and item.due > today:
            dates.append(people.Dated(f"health-{item.track}", f"{item.label} 추적", "expiry", item.due.month, item.due.day,
                                      item.due.year, False, FOLLOW_UP_REMIND, None))
    checkup = next_checkup(ledger)
    if checkup is not None and checkup > today:
        dates.append(people.Dated("health-checkup", "건강검진(지난 검진 1년 뒤)", "expiry", checkup.month, checkup.day,
                                  checkup.year, False, FOLLOW_UP_REMIND, None))
    return dates


def merge_dates(book: people.Book, ledger: Ledger, today: date) -> people.Book:
    return people.Book(book.people, book.dates + tuple(as_dates(ledger, today)))


def interpretation_notes(ledger: Ledger) -> list[dict[str, Any]]:
    """What the client's medicines do to the values on file (REGIMEN_EFFECTS), with the source. A frequency other than
    the label's adds the line that the size of the effect is then unverified and for the clinician to read."""
    notes = []
    for item in ledger.regimen:
        effect = REGIMEN_EFFECTS.get((item.ingredient or "").strip().lower())
        if effect is None:
            continue
        codes = [c for c in effect["codes"] if series(ledger, c)]
        if not codes:
            continue
        text = f"{item.name}({effect['class']}) 복용 중: {effect['note']}"
        if item.frequency != effect["label_frequency"]:
            text += f" 라벨의 연구는 {_frequency_ko(effect['label_frequency'])} 복용 기준이고 고객은 {_frequency_ko(item.frequency)} " \
                    "복용이므로 감소 폭은 다를 수 있다 — 검진 때 복용 사실과 빈도를 의료진에게 알린다."
        notes.append({"codes": codes, "regimen": item.name, "since": item.since, "text": text, "source": effect["source"]})
    return notes


def check(ledger: Ledger) -> list[str]:
    """Notes a reader should know: codes measured two ways, posture devices that differ, findings off any known track."""
    notes = []
    for code in sorted({o.code for o in ledger.observations}):
        methods = {o.method for o in series(ledger, code)}
        if len(methods) > 1:
            notes.append(f"{label(code)}({code}): 측정 방식이 {len(methods)}가지라 추이는 방식별로 따로 본다.")
    devices = {p.device for p in ledger.posture}
    if len(devices) > 1:
        notes.append("자세 측정 장비·방식이 " + ", ".join(sorted(devices)) + "로 달라 서로 잇지 않는다.")
    for finding in ledger.findings:
        if finding.track is None:
            notes.append(f"{finding.id}: track이 없어 같은 검사의 다음 소견이 이어받지 못한다.")
        elif finding.track not in TRACKS and finding.track not in LAB_TRACKS:
            notes.append(f"{finding.id}: track '{finding.track}'은 알려진 검사가 아니라 아침 메일의 날짜로 나가지 않는다.")
    return notes


def import_batch(folder: Path, batch: dict[str, Any]) -> ImportResult:
    """Merge a batch (the same shape as the files) into `folder`. Identical records are skipped; a record whose key
    exists with other content is a conflict and nothing is written. Regimen and routine entries replace the entry
    with the same name (they are the client's current statement). The merged ledger is validated before any write.

    Raises:
        HealthError: a malformed batch, or a conflict (listed in the message).
    """
    batch = normalize(batch)
    unknown = sorted(set(batch) - set(FILES))
    if unknown:
        raise HealthError("batch: unknown section(s) " + ", ".join(unknown))
    current = {name: _read(folder / f"{name}.json") for name in FILES}
    merged: dict[str, Any] = {}
    added: dict[str, int] = {}
    skipped: dict[str, int] = {}
    conflicts: list[str] = []
    for name in FILES:
        if name not in batch:
            merged[name] = current[name]
            continue
        if name in ("genetics", "microbiome"):
            merged[name], outcome = _merge_single(name, current[name], batch[name])
        else:
            merged[name], outcome = _merge_rows(name, current[name], batch[name])
        added[name], skipped[name], clashes = outcome
        conflicts += clashes
    if conflicts:
        raise HealthError("conflicts, nothing written: " + "; ".join(conflicts))
    parse(merged)   # Validate the whole before touching a file.
    with _locked(folder):
        _write_all(folder, {name: merged[name] for name in batch})
    return ImportResult(added, skipped)


def _write_all(folder: Path, payloads: dict[str, Any]) -> None:
    """All sections to temp files first, then every temp file replaced in one pass: a failure while writing leaves
    the originals whole, and the window in which a reader could see a mix is the replaces alone (review 2026-10-25)."""
    folder.mkdir(parents=True, exist_ok=True)
    temps = []
    try:
        for name, payload in payloads.items():
            temp = folder / f"{name}.json.tmp"
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            temps.append((temp, folder / f"{name}.json"))
    except OSError:
        for temp, _ in temps:
            temp.unlink(missing_ok=True)
        raise
    for temp, target in temps:
        os.replace(temp, target)


LOCK_STALE_SECONDS = 300


@contextmanager
def _locked(folder: Path) -> Iterator[None]:
    """A process lock for the ledger folder: an import and a resolve that overlap would otherwise lose each other's
    write. A lock older than LOCK_STALE_SECONDS belongs to a crashed run and is taken over.

    Raises:
        HealthError: another live write holds the lock.
    """
    folder.mkdir(parents=True, exist_ok=True)
    lock = folder / ".write.lock"
    try:
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        try:
            stale = time.time() - lock.stat().st_mtime > LOCK_STALE_SECONDS
        except OSError:
            stale = False
        if not stale:
            raise HealthError("another write is in progress (lock held); try again in a moment") from None
        lock.unlink(missing_ok=True)
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def add_months(when: date, months: int) -> date:
    month = when.month - 1 + months
    year, month = when.year + month // 12, month % 12 + 1
    last = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(when.day, last))


# ---- merge helpers ----------------------------------------------------------------------------------------------

_KEYS = {
    "observations": lambda r: ("observations", r.get("code"), r.get("on"), r.get("method")),
    "findings": lambda r: ("findings", r.get("id")),
    "posture": lambda r: ("posture", r.get("code"), r.get("on"), r.get("device"), r.get("side")),
    "regimen": lambda r: ("regimen", r.get("name")),
    "routine": lambda r: ("routine", r.get("activity")),
    "constraints": lambda r: ("constraints", r.get("text")),
}
_REPLACE = ("regimen", "routine", "constraints")
# What must agree for two records with the same key to be the same record: the measured content, not the page it was
# read from (the same value is printed again in a later report's comparison column).
_CONTENT = {"observations": lambda r: (str(r.get("value")).strip(), r.get("unit")),
            "posture": lambda r: (str(r.get("value")).strip(), r.get("unit")),
            "findings": lambda r: {k: v for k, v in r.items() if k != "source"}}


def _merge_rows(name: str, current: Any, incoming: Any) -> tuple[Any, tuple[int, int, list[str]]]:
    rows = list(_list(current, name)) if current else []
    new = _list(incoming, name)
    key = _KEYS[name]
    index = {}
    for row in rows:
        if not isinstance(row, dict):
            raise HealthError(f"{name}: each record must be an object")
        index[key(row)] = row
    added = skipped = 0
    clashes = []
    for row in new:
        if not isinstance(row, dict):
            raise HealthError(f"{name}: each record must be an object")
        k = key(row)
        if k in index:
            same = _CONTENT.get(name, lambda r: r)
            if same(index[k]) == same(row):
                skipped += 1
            elif name in _REPLACE:
                rows[rows.index(index[k])] = row
                index[k] = row
                added += 1
            else:
                clashes.append(f"{name} {'/'.join(str(p) for p in k[1:] if p is not None)}: value differs")
        else:
            rows.append(row)
            index[k] = row
            added += 1
    return {name: rows}, (added, skipped, clashes)


def _merge_single(name: str, current: Any, incoming: Any) -> tuple[Any, tuple[int, int, list[str]]]:
    if current in (None, {}):
        return incoming, (1, 0, [])
    if current == incoming:
        return current, (0, 1, [])
    return current, (0, 0, [f"{name}: a different report is already on file (remove the file to replace it)"])


# ---- parsing helpers --------------------------------------------------------------------------------------------

def _read(file: Path) -> Any:
    if not file.exists():
        return None
    try:
        return json.loads(file.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise HealthError(f"{file.name}: {error}") from None


def _write(file: Path, payload: Any) -> None:
    temp = file.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temp, file)


def _list(raw: Any, name: str) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = raw.get(name)
        if raw is None:
            return []
    if not isinstance(raw, list):
        raise HealthError(f"{name} must be a list (or an object with a {name} list)")
    return raw


def _check_keys(where: str, item: Any, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise HealthError(f"{where}: each record must be an object")
    unknown = sorted(set(item) - allowed)
    if unknown:
        raise HealthError(f"{where}: unknown key(s) {', '.join(unknown)}")
    return item


def _text(item: dict[str, Any], key: str, where: str, *, required: bool = False, limit: int = MAX_TEXT) -> str | None:
    value = item.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):   # An empty text is an absent one.
        if required:
            raise HealthError(f"{where}: {key} is required")
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise HealthError(f"{where}: {key} must be text up to {limit} characters")
    return value.strip()


def _texts(raw: Any, where: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or any(not isinstance(t, str) or not t.strip() for t in raw):
        raise HealthError(f"{where} must be a list of texts")
    return [t.strip() for t in raw]


def _names(raw: Any, where: str) -> list[str]:
    """A list of texts, or of objects naming something ({"name": ...} with other detail): the names."""
    if isinstance(raw, list) and raw and all(isinstance(t, dict) for t in raw):
        raw = [t.get("name") or t.get("label") or t.get("species") for t in raw]
    return _texts(raw, where)


def _date(raw: Any, where: str, *, required: bool = True) -> date | None:
    if raw is None:
        if required:
            raise HealthError(f"{where}: a date YYYY-MM-DD is required")
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        raise HealthError(f"{where}: date must be YYYY-MM-DD, not {raw!r}") from None


def _decimal(raw: Any, where: str) -> tuple[Decimal | None, str]:
    """The number in a printed value: "145", "<0.5", "5.3 ", "1,020", or the one number inside a result word
    ("양성 (785.00)"); a result word alone has no number. The printed text is kept beside it."""
    if isinstance(raw, bool) or raw is None:
        raise HealthError(f"{where}: value is required")
    text = str(raw).strip()
    cleaned = text.replace(",", "").lstrip("<>≤≥").strip()
    try:
        return Decimal(cleaned), text
    except InvalidOperation:
        # A result word with one number ("음성 (0)", "양성 (< 2.00)") is read for any code; a result word alone
        # ("음성") is a qualitative value with no number. Any other text ("높음") is not a value.
        numbers = re.findall(r"-?\d+(?:\.\d+)?", cleaned)
        word = re.sub(r"[\s()<>≤≥\d.\-]", "", cleaned)
        if word in QUALITATIVE_WORDS and len(numbers) == 1:
            return Decimal(numbers[0]), text
        if word in QUALITATIVE_WORDS and not numbers:
            return None, text
        raise HealthError(f"{where}: value must be a number, not {text!r}") from None


def _int(item: dict[str, Any], key: str, where: str, low: int, high: int) -> int | None:
    value = item.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise HealthError(f"{where}: {key} must be a whole number from {low} to {high}")
    return value


def _choice(item: dict[str, Any], key: str, where: str, choices: tuple[str, ...], default: str) -> str:
    value = item.get(key, default)
    if value not in choices:
        raise HealthError(f"{where}: {key} must be one of {', '.join(choices)}")
    return value


_GENETICS_KEYS = {"genetics", "types", "on", "area_summary"}
_OBS_KEYS = {"code", "value", "unit", "ref", "on", "flag", "method", "source", "uncertain", "note"}
_FINDING_KEYS = {"id", "area", "summary", "detail", "on", "track", "follow_up_months", "recommendation", "severity",
                 "source", "resolved_on", "resolved_note", "uncertain"}
_POSTURE_KEYS = {"code", "value", "unit", "on", "device", "side", "note", "source"}
_TRAIT_KEYS = {"trait", "area", "grade", "score", "risk_alleles", "alleles_total", "korean_mean", "genes", "source", "result"}
_SCORE_KEYS = {"code", "label", "value", "korean_mean", "grade", "source"}
_MICRO_KEYS = {"on", "lab", "scores", "enterotype", "probiotics_detected", "probiotics_absent", "enterotype_label",
               "wellness_type", "notes", "probiotics_source"}
_REGIMEN_KEYS = {"name", "ingredient", "frequency", "since", "note"}
_ROUTINE_KEYS = {"activity", "per_week", "minutes", "note"}
_CONSTRAINT_KEYS = {"text", "since", "source"}


def _observation(item: Any) -> Observation:
    item = _check_keys("observations", item, _OBS_KEYS)
    code = item.get("code")
    if code not in CODES:
        raise HealthError(f"observations: unknown code {code!r} (add it to health.CODES first)")
    where = f"observations {code}"
    value, text = _decimal(item.get("value"), where)
    on = _date(item.get("on"), where)
    assert on is not None
    unit = _text(item, "unit", where, limit=30) or CODES[code][1]
    uncertain = item.get("uncertain", False)
    if not isinstance(uncertain, bool):
        raise HealthError(f"{where}: uncertain must be true or false")
    return Observation(code, value, text, unit, _text(item, "ref", where, limit=40), on,
                       _choice(item, "flag", where, FLAGS, "unknown"), _text(item, "method", where, limit=20),
                       _text(item, "source", where, required=True) or "", uncertain, _text(item, "note", where))


def _finding(item: Any) -> Finding:
    item = _check_keys("findings", item, _FINDING_KEYS)
    key = item.get("id")
    if not isinstance(key, str) or not _ID.match(key):
        raise HealthError(f"findings: id must be lowercase letters, digits and dashes, not {key!r}")
    on = _date(item.get("on"), key)
    assert on is not None
    track = _text(item, "track", key, limit=30)
    if track is not None and not _ID.match(track):
        raise HealthError(f"{key}: track must be lowercase letters, digits and dashes")
    return Finding(key, _text(item, "area", key, required=True, limit=30) or "", _text(item, "summary", key, required=True) or "",
                   _text(item, "detail", key, limit=1200), on, track, _int(item, "follow_up_months", key, 1, 120),
                   _text(item, "recommendation", key, limit=1200), _choice(item, "severity", key, SEVERITIES, "note"),
                   _text(item, "source", key, required=True) or "", _date(item.get("resolved_on"), key, required=False),
                   _text(item, "resolved_note", key), bool(item.get("uncertain", False)))


def _posture(item: Any) -> Posture:
    item = _check_keys("posture", item, _POSTURE_KEYS)
    code = item.get("code")
    if code not in POSTURE_CODES:
        raise HealthError(f"posture: unknown code {code!r}")
    where = f"posture {code}"
    if code in POSTURE_TEXT:
        value, text = None, _text(item, "value", where, required=True, limit=20) or ""
    else:
        value, text = _decimal(item.get("value"), where)
    on = _date(item.get("on"), where)
    assert on is not None
    side = _text(item, "side", where, limit=1)
    if side is not None and side not in ("L", "R"):
        raise HealthError(f"{where}: side must be L or R")
    unit = _text(item, "unit", where, limit=10) or POSTURE_CODES[code][1]
    return Posture(code, value, text, {"deg": "°", "degree": "°"}.get(unit, unit), on,
                   _text(item, "device", where, required=True, limit=40) or "", side, _text(item, "note", where),
                   _text(item, "source", where, required=True) or "")


def _trait(item: Any) -> Trait:
    item = _check_keys("genetics", item, _TRAIT_KEYS)
    name = _text(item, "trait", "genetics", required=True, limit=60) or ""
    where = f"genetics {name}"
    mean = item.get("korean_mean")
    return Trait(name, _text(item, "area", where, required=True, limit=30) or "", _text(item, "grade", where, limit=20),
                 _int(item, "risk_alleles", where, 0, 200), _int(item, "alleles_total", where, 0, 200),
                 None if mean is None else _decimal(mean, where)[0], tuple(_texts(item.get("genes"), where)),
                 _text(item, "source", where, required=True) or "", _text(item, "result", where, limit=60))


def _microbiome(raw: Any) -> Microbiome | None:
    if raw is None:
        return None
    if isinstance(raw, dict) and set(raw) == {"microbiome"}:
        raw = raw["microbiome"]
    item = _check_keys("microbiome", raw, _MICRO_KEYS)
    on = _date(item.get("on"), "microbiome")
    assert on is not None
    scores = []
    for row in _list(item.get("scores"), "microbiome.scores"):
        row = _check_keys("microbiome.scores", row, _SCORE_KEYS)
        code = _text(row, "code", "microbiome.scores", required=True, limit=40) or ""
        scores.append(Score(code, _text(row, "label", code, required=True, limit=40) or "", str(row.get("value", "")).strip(),
                            None if row.get("korean_mean") is None else str(row["korean_mean"]).strip(),
                            _text(row, "grade", code, limit=20)))
    return Microbiome(on, _text(item, "lab", "microbiome", required=True, limit=40) or "", tuple(scores),
                      _text(item, "enterotype", "microbiome", limit=40),
                      tuple(_names(item.get("probiotics_detected"), "microbiome.probiotics_detected")),
                      tuple(_names(item.get("probiotics_absent"), "microbiome.probiotics_absent")),
                      _text(item, "enterotype_label", "microbiome", limit=60), _text(item, "wellness_type", "microbiome", limit=60),
                      tuple(_texts([item["notes"]] if isinstance(item.get("notes"), str) else item.get("notes"), "microbiome.notes")))


def _regimen(item: Any) -> Regimen:
    item = _check_keys("regimen", item, _REGIMEN_KEYS)
    name = _text(item, "name", "regimen", required=True, limit=60) or ""
    return Regimen(name, _text(item, "ingredient", name, limit=60), _text(item, "frequency", name, required=True, limit=30) or "",
                   _text(item, "since", name, limit=20), _text(item, "note", name))


def _routine(item: Any) -> Routine:
    item = _check_keys("routine", item, _ROUTINE_KEYS)
    name = _text(item, "activity", "routine", required=True, limit=60) or ""
    per_week = _range(item.get("per_week"), f"{name} per_week", 0, 14)
    minutes = _range(item.get("minutes"), f"{name} minutes", 1, 600)
    return Routine(name, per_week[0], per_week[1], minutes[0], minutes[1], _text(item, "note", name))


def _constraint(item: Any) -> Constraint:
    item = _check_keys("constraints", item, _CONSTRAINT_KEYS)
    text = _text(item, "text", "constraints", required=True, limit=200) or ""
    return Constraint(text, _text(item, "since", text, limit=20), _text(item, "source", text, limit=60))


def _range(raw: Any, where: str, low: int, high: int) -> tuple[int, int]:
    """"2", "1~2", "1-2" or [1, 2] as (min, max)."""
    if isinstance(raw, int) and not isinstance(raw, bool):
        raw = [raw, raw]
    elif isinstance(raw, str):
        parts = re.split(r"[~\-–]", raw.strip())
        try:
            raw = [int(p) for p in parts] if len(parts) in (1, 2) else None
            if raw is not None and len(raw) == 1:
                raw = raw * 2
        except ValueError:
            raw = None
    if (not isinstance(raw, list) or len(raw) != 2 or any(isinstance(v, bool) or not isinstance(v, int) for v in raw)
            or not low <= raw[0] <= raw[1] <= high):
        raise HealthError(f"{where}: must be a count or a range such as \"1~2\" within {low}~{high}")
    return raw[0], raw[1]


def _frequency_ko(frequency: str) -> str:
    return {"daily": "매일", "weekly": "주 1회"}.get(frequency, frequency)
