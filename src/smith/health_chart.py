"""Line charts for the health letter (user request 2026-10-25: time on the x-axis, the value on the y-axis, not bars).

Mail clients drop SVG, so a chart is a PNG drawn with Pillow (already a dependency) and travels like the daily mail's
pictures: a data URI in the preview file, an inline `cid:` attachment in the sent mail. The report's own reference range
is a shaded band, each point carries its figure and the report's flag colour, and the band plus the word beside the
colour keep the meaning readable without colour (dataviz status rule).
"""
import io
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SCALE = 2                       # Drawn at 2x and shown at 1x: crisp on high-density screens.
WIDTH, HEIGHT = 560, 230        # Displayed size (CSS pixels); the mail column is 560 wide inside the card.
MARGIN_LEFT, MARGIN_RIGHT, MARGIN_TOP, MARGIN_BOTTOM = 54, 18, 44, 34
PAGE, GRID, INK, MUTED, LINE = "#ffffff", "#e6ece6", "#1d2a22", "#6b7a70", "#2f7d4f"
BAND = "#e3f0e3"
FLAG_COLORS = {"high": "#c9552d", "low": "#c9552d", "attention": "#d79a1e", "normal": "#2f7d4f", "unknown": "#9aa89f"}
FONT_CANDIDATES = (Path(r"C:\Windows\Fonts\malgun.ttf"), Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf"))


@dataclass(frozen=True)
class Point:
    on: date
    value: Decimal
    flag: str
    text: str


def parse_ref(ref: str | None) -> tuple[Decimal | None, Decimal | None]:
    """The report's reference text as (low, high): "0~129" → (0, 129); "60~" → (60, None); "~24.9" or "7.0 이하" or
    "<130" → (None, high); "90 미만" → (None, 90). Anything else: (None, None)."""
    if not ref:
        return None, None
    text = ref.replace(",", "").replace(" ", "")
    if "남" in text and "여" in text:   # "남 90미만 여 85미만": the client's own limit is the first.
        text = text.split("여")[0].replace("남", "")
    numbers = [Decimal(n) for n in re.findall(r"-?\d+(?:\.\d+)?", text)]
    if "~" in text or "-" in text[1:]:
        parts = re.split(r"[~]|(?<=\d)-", text, maxsplit=1)
        low = Decimal(re.findall(r"-?\d+(?:\.\d+)?", parts[0])[0]) if parts[0] and re.search(r"\d", parts[0]) else None
        high = Decimal(re.findall(r"-?\d+(?:\.\d+)?", parts[1])[0]) if len(parts) > 1 and re.search(r"\d", parts[1]) else None
        return low, high
    if len(numbers) == 1 and any(w in text for w in ("이하", "미만", "<", "≤", "까지")):
        return None, numbers[0]
    if len(numbers) == 1 and any(w in text for w in ("이상", "초과", ">", "≥")):
        return numbers[0], None
    return None, None


def line_chart(points: list[Point], *, title: str, unit: str, ref: str | None = None) -> bytes:
    """PNG of one series over time with the reference band and flag-coloured points. One point still draws."""
    low, high = parse_ref(ref)
    width, height = WIDTH * SCALE, HEIGHT * SCALE
    image = Image.new("RGB", (width, height), PAGE)
    draw = ImageDraw.Draw(image)
    font, small, bold = _font(13 * SCALE), _font(11 * SCALE), _font(14 * SCALE, bold=True)
    left, right = MARGIN_LEFT * SCALE, width - MARGIN_RIGHT * SCALE
    top, bottom = MARGIN_TOP * SCALE, height - MARGIN_BOTTOM * SCALE
    values = [p.value for p in points] + [v for v in (low, high) if v is not None]
    lo, hi = min(values), max(values)
    pad = (hi - lo) * Decimal("0.18") if hi != lo else (abs(hi) * Decimal("0.2") or Decimal(1))
    y_min, y_max = lo - pad, hi + pad
    if low is None and y_min < 0 <= lo:
        y_min = Decimal(0)

    def y_of(value: Decimal) -> float:
        return float(bottom - (value - y_min) / (y_max - y_min) * (bottom - top))

    first, last = points[0].on.toordinal(), points[-1].on.toordinal()

    def x_of(index: int) -> float:
        # Time on the x-axis (user request 2026-10-25): a gap of three years is three times a gap of one.
        if last == first:
            return (left + right) / 2
        return left + (points[index].on.toordinal() - first) * (right - left) / (last - first)

    draw.text((left, 10 * SCALE), f"{title} ({unit})" if unit else title, fill=INK, font=bold)
    ref_text = f"참고 범위 {ref}" if ref else "참고 범위 없음"
    draw.text((right - draw.textlength(ref_text, font=small), 14 * SCALE), ref_text, fill=MUTED, font=small)
    if low is not None or high is not None:
        band_top = y_of(high) if high is not None else top
        band_bottom = y_of(low) if low is not None else bottom
        draw.rectangle((left, band_top, right, band_bottom), fill=BAND)
    for tick in _ticks(y_min, y_max):
        y = y_of(tick)
        draw.line((left, y, right, y), fill=GRID, width=SCALE)
        label = f"{tick.normalize():f}"
        draw.text((left - draw.textlength(label, font=small) - 6 * SCALE, y - 7 * SCALE), label, fill=MUTED, font=small)
    xs = [x_of(i) for i in range(len(points))]
    ys = [y_of(p.value) for p in points]
    if len(points) > 1:
        draw.line(list(zip(xs, ys)), fill=LINE, width=2 * SCALE, joint="curve")
    last_stamp_end = -1.0
    for i, point in enumerate(points):
        r = 5 * SCALE
        draw.ellipse((xs[i] - r, ys[i] - r, xs[i] + r, ys[i] + r), fill=FLAG_COLORS.get(point.flag, FLAG_COLORS["unknown"]), outline=PAGE,
                     width=SCALE)
        label = point.text
        tw = draw.textlength(label, font=font)
        draw.text((min(max(xs[i] - tw / 2, left), right - tw), ys[i] - 22 * SCALE), label, fill=INK, font=font)
        stamp = f"{point.on:%y.%m}"
        sw = draw.textlength(stamp, font=small)
        sx = min(max(xs[i] - sw / 2, left), right - sw)
        if sx >= last_stamp_end + 4 * SCALE:   # Dates too close to label both keep the first; the point still shows.
            draw.text((sx, bottom + 6 * SCALE), stamp, fill=MUTED, font=small)
            last_stamp_end = sx + sw
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _ticks(y_min: Decimal, y_max: Decimal, count: int = 4) -> list[Decimal]:
    span = y_max - y_min
    if span <= 0:
        return [y_min]
    raw = span / count
    magnitude = Decimal(10) ** (raw.adjusted())
    step = next((m * magnitude for m in (Decimal(1), Decimal(2), Decimal("2.5"), Decimal(5), Decimal(10)) if m * magnitude >= raw),
                magnitude * 10)
    first = (y_min / step).to_integral_value(rounding="ROUND_CEILING") * step
    ticks = []
    tick = first
    while tick <= y_max:
        ticks.append(tick)
        tick += step
    return ticks


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in FONT_CANDIDATES:
        path = candidate.with_name("malgunbd.ttf") if bold and candidate.name == "malgun.ttf" else candidate
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue
    return ImageFont.load_default()
