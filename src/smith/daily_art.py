"""The daily mail's pictures (user request 2026-10-25): a small character that makes the weather readable at a glance
and a mood picture beside the sentence of the day, so the mail lifts the mood before a word is read.

The artwork is Microsoft's Fluent Emoji (3D style, MIT licence; `assets/daily/LICENSE-fluentui-emoji.txt`), vendored so
a morning never depends on a download. The weather fairy (a fairy figure) is constant; the weather picture beside her
follows the day's rules in code (severe warning, snow, rain, bad air, pollen, heat, cold, sky). The mood picture is one
of a small catalogue the writer may choose from; without a choice the season picks.

Mail clients do not load `data:` images, so a sent mail carries them as inline attachments referenced by `cid:`;
the local preview embeds them as data URIs instead.
"""
import base64
from datetime import date
from pathlib import Path
from typing import Any

from smith import weather

ASSETS = Path(__file__).resolve().parents[2] / "assets" / "daily"
FAIRY = "fairy"
MOODS = {"sparkles": "반짝임", "seedling": "새싹", "sunrise": "해돋이", "rainbow": "무지개", "coffee": "따뜻한 커피",
         "clover": "네잎클로버", "star": "빛나는 별", "hug": "포옹", "blossom": "벚꽃", "leaf": "낙엽", "maple": "단풍",
         "moon": "초승달", "snowman": "눈사람", "sunglasses": "선글라스"}
_SEASON_MOODS = {12: ("snowman", "coffee", "star"), 1: ("snowman", "coffee", "moon"), 2: ("coffee", "sparkles", "seedling"),
                 3: ("seedling", "blossom", "sunrise"), 4: ("blossom", "seedling", "rainbow"), 5: ("rainbow", "clover", "blossom"),
                 6: ("sunglasses", "rainbow", "sparkles"), 7: ("sunglasses", "star", "rainbow"), 8: ("sunglasses", "sunrise", "hug"),
                 9: ("leaf", "coffee", "star"), 10: ("maple", "leaf", "coffee"), 11: ("maple", "coffee", "hug")}


def weather_key(report: weather.Report | None, advice: weather.Advice | None) -> str:
    """Which picture stands beside the fairy today (the first rule that applies)."""
    if report is None or advice is None:
        return "fog"
    today = report.today
    precip = set(today.precip) if today else set()
    air = report.air
    if advice.severe:
        return "storm"
    if "눈" in precip or "비·눈" in precip:
        return "snow"
    if "우산" in advice.items:
        return "rain"
    if air is not None and max(air.pm25_grade or 0, air.pm10_grade or 0) >= 3:
        return "mask"
    if any(p.in_season and (p.today or 0) >= 2 for p in report.pollen):
        return "sneeze"
    if today is not None and today.tmax is not None and today.tmax >= weather.HOT_MAX:
        return "hot"
    if today is not None and today.tmin is not None and today.tmin <= 0:
        return "cold"
    if today is not None and today.wind_max is not None and today.wind_max >= 9:
        return "wind"
    sky = today.sky if today else None
    return {"흐림": "cloud", "구름많음": "sun_cloud"}.get(sky or "", "sun")


def mood_key(choice: str | None, day: date) -> str:
    """The writer's choice when it is in the catalogue, else the season's picture, rotating by the day."""
    if choice in MOODS:
        return choice
    options = _SEASON_MOODS[day.month]
    return options[day.toordinal() % len(options)]


def images_for(data: dict[str, Any]) -> dict[str, Path]:
    """cid -> file for one mail: the fairy, the weather picture, the mood picture (only files that exist)."""
    prose = data.get("prose") or {}
    block = data.get("weather")
    wanted = {FAIRY: FAIRY, "weather": weather_key(None if block is None else block["report"], None if block is None else block["advice"]),
              "mood": mood_key(prose.get("mood"), data["day"])}
    found = {}
    for cid, key in wanted.items():
        path = ASSETS / f"{key}.png"
        if path.exists():
            found[cid] = path
    return found


def source(cid: str, images: dict[str, Path], *, inline: bool) -> str | None:
    """The `src` for an <img>: a data URI for the preview file, a cid reference for the mail."""
    if cid not in images:
        return None
    if not inline:
        return f"cid:{cid}"
    return "data:image/png;base64," + base64.b64encode(images[cid].read_bytes()).decode("ascii")


def payload(images: dict[str, Path]) -> dict[str, bytes]:
    """The inline attachments of a sent mail."""
    return {cid: path.read_bytes() for cid, path in images.items()}
