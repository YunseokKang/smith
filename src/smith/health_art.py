"""Pictures beside the health letter's actions (user request 2026-10-25): a small, friendly picture of the food, the
movement or the habit, because a picture is what stays in memory.

The artwork is Microsoft's Fluent Emoji (3D style, MIT licence; `assets/health/LICENSE-fluentui-emoji.txt`), vendored
like the daily mail's pictures so a letter never depends on a download. Which picture goes with an action is decided
here by words in the action's text, never by the model: the first rule whose words appear wins, else the category's
default. Mail clients do not load `data:` images, so a sent mail carries them as inline attachments referenced by
`cid:`; the local preview embeds them as data URIs.
"""
import base64
from pathlib import Path
from typing import Any

ASSETS = Path(__file__).resolve().parents[2] / "assets" / "health"
# (words in the action's `what`/`amount`, picture). Order matters: the more specific first.
RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("테니스",), "tennis"),
    (("스쿼트", "런지", "팔굽혀", "덤벨", "바벨", "근력", "웨이트", "기구", "밴드"), "weights"),
    (("스트레칭", "요가", "턱 당기", "턱을", "가슴 열기", "가슴 펴기", "목과 어깨", "자세"), "lotus"),
    (("조깅", "달리기", "뛰기", "러닝"), "running"),
    (("걷기", "산책", "걸음", "계단"), "walking"),
    (("자전거",), "bicycle"),
    (("일어나", "앉아 있", "앉은"), "chair"),
    (("생선", "고등어", "연어", "등푸른"), "fish"),
    (("달걀", "계란"), "egg"),
    (("닭", "살코기", "단백질"), "chicken"),
    (("두부", "콩", "낫토", "렌틸"), "beans"),
    (("귀리", "오트", "현미", "통곡물", "잡곡"), "bowl"),
    (("채소", "샐러드", "나물", "브로콜리", "야채"), "salad"),
    (("물 ", "물을", "물로", "물이나", "탄산수", "수분"), "water"),
    (("술", "맥주", "소주", "음주", "술자리", "반주"), "beer"),
    (("단 음료", "탄산음료", "주스", "가당", "커피"), "cup"),
    (("잠", "수면", "취침", "기상"), "bed"),
    (("시각", "시간 고정", "알람"), "clock"),
    (("햇빛", "햇볕", "볕"), "sun"),
    (("체중", "허리둘레", "재기", "재어"), "scale"),
    (("기록", "표시", "적어", "메모", "달력"), "memo"),
)
DEFAULTS = {"food": "plate", "exercise": "running", "life": "sparkles"}


def picture_for(action: dict[str, Any]) -> str:
    """The picture key for one action: the first rule whose words appear in what/amount, else the category default."""
    text = f"{action.get('what', '')} {action.get('amount', '')}"
    for words, key in RULES:
        if any(w in text for w in words):
            return key
    return DEFAULTS.get(str(action.get("category", "")), "sparkles")


def images_for(data: dict[str, Any]) -> dict[str, Path]:
    """cid -> file for the actions the letter shows (prose or fallback), in order; only files that exist."""
    prose = data.get("prose") or {}
    actions = prose.get("actions") or data.get("fallback_actions") or []
    found: dict[str, Path] = {}
    for number, action in enumerate(actions, 1):
        path = ASSETS / f"{picture_for(action)}.png"
        if path.exists():
            found[f"action-{number}"] = path
    return found


def source(cid: str, images: dict[str, Path], *, inline: bool) -> str | None:
    """The `src` for an <img>: a data URI for the preview file, a cid reference for the mail."""
    if cid not in images:
        return None
    if not inline:
        return f"cid:{cid}"
    return "data:image/png;base64," + base64.b64encode(images[cid].read_bytes()).decode("ascii")


def payload(images: dict[str, Path]) -> dict[str, bytes]:
    """The attachment bytes by cid, for `gmail.send(images=...)`."""
    return {cid: path.read_bytes() for cid, path in images.items()}
