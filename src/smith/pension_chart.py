"""Pictures of the pension stack for the tax letter (user request 2026-11-07: the public pension portal's donut and
age-by-age bars, drawn more cleanly than table cells can).

Mail clients drop SVG and scripts, so each chart is a PNG drawn with Pillow (as the health letter's charts) and travels
as a data URI in the preview and an inline `cid:` attachment in the sent mail (delivery.inline_images). The exact
figures stay in the HTML table beside them, so nothing is only in a picture.
"""
import io
from decimal import Decimal

from PIL import Image, ImageDraw

from smith.health_chart import _font

SCALE = 2                     # Drawn at 2x and shown at 1x: crisp on high-density screens.
WIDTH = 560                   # Displayed width (CSS pixels), the report column.
PAGE, GRID, INK, MUTED = "#ffffff", "#e6e8eb", "#0a0b0d", "#2f3337"
GAP = "#ffffff"


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _man(value: Decimal) -> str:
    """Won as 만 원 with thousands separators (연 2,140만)."""
    return f"{value / 10_000:,.0f}만"


DONUT = 190                   # The ring's size (CSS pixels), whatever the number of parts.
LEGEND_ROW = 44               # One legend entry: the name, and under it the share and yearly amount.


def _fit(draw: ImageDraw.ImageDraw, text: str, font: object, room: float) -> str:
    """`text` cut with an ellipsis to fit `room` pixels (a long product name must not run into the figures)."""
    if draw.textlength(text, font=font) <= room:
        return text
    while text and draw.textlength(text + "…", font=font) > room:
        text = text[:-1]
    return text.rstrip(" (·") + "…"


def donut(parts: list[tuple[str, Decimal, str]], *, center: str, caption: str) -> bytes:
    """A donut of each part's share (label, yearly amount, colour) with the total in the hole and a legend on the
    right: the name on one line and its share and yearly amount on the next, so a long name never runs into the
    figures (user review 2026-11-08). The ring keeps one size; the legend sets the height. Zero parts are left out."""
    parts = [p for p in parts if p[1] > 0]
    height = max(DONUT + 32, 24 + LEGEND_ROW * len(parts))
    image = Image.new("RGB", (WIDTH * SCALE, height * SCALE), PAGE)
    draw = ImageDraw.Draw(image)
    total = sum((v for _, v, _ in parts), Decimal(0))
    size, top = DONUT * SCALE, (height - DONUT) * SCALE // 2
    box = (16 * SCALE, top, 16 * SCALE + size, top + size)
    angle = -90.0
    for _, value, color in parts:
        sweep = float(value / total * 360)
        draw.pieslice(box, angle, angle + sweep, fill=color, outline=GAP, width=2 * SCALE)
        angle += sweep
    hole = size * 0.58
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    draw.ellipse((cx - hole / 2, cy - hole / 2, cx + hole / 2, cy + hole / 2), fill=PAGE)
    big, small, label_font = _font(18 * SCALE, bold=True), _font(13 * SCALE), _font(14 * SCALE)
    for text, font, dy in ((center, big, -11), (caption, small, 11)):
        width = draw.textlength(text, font=font)
        draw.text((cx - width / 2, cy + dy * SCALE - getattr(font, "size", 12) / 2), text, fill=INK if font is big else MUTED, font=font)
    x = box[2] + 24 * SCALE
    room = WIDTH * SCALE - 16 * SCALE - (x + 20 * SCALE)
    first = (height * SCALE - LEGEND_ROW * SCALE * len(parts)) // 2
    for i, (label, value, color) in enumerate(parts):
        y = first + i * LEGEND_ROW * SCALE + 2 * SCALE
        draw.rounded_rectangle((x, y + 3 * SCALE, x + 12 * SCALE, y + 15 * SCALE), radius=3 * SCALE, fill=color)
        draw.text((x + 20 * SCALE, y), _fit(draw, label, label_font, room), fill=INK, font=label_font)
        figure = f"{value / total * 100:.0f}% · 연 {_man(value)}"
        draw.text((x + 20 * SCALE, y + 19 * SCALE), figure, fill=MUTED, font=small)
    return _png(image)


def age_bars(rows: list[dict], order: list[tuple[str, str]], *, retire_age: int, focus_age: int | None) -> bytes:
    """One stacked bar per age of the yearly payouts (`rows` from pension.timeline, `order` the pipeline keys and
    colours from the bottom up), a light grid in 만 원, ages every five years, a dashed line at the retirement age
    and the focus age's bar outlined."""
    height = 260
    image = Image.new("RGB", (WIDTH * SCALE, height * SCALE), PAGE)
    draw = ImageDraw.Draw(image)
    small = _font(13 * SCALE)
    left, right, top, bottom = 58 * SCALE, (WIDTH - 10) * SCALE, 14 * SCALE, (height - 26) * SCALE
    peak = max((r["total"] for r in rows), default=Decimal(0)) or Decimal(1)
    step = _step(peak)
    ceiling = (peak / step).to_integral_value(rounding="ROUND_CEILING") * step

    def y_of(value: Decimal) -> float:
        return float(bottom - value / ceiling * (bottom - top))

    tick = Decimal(0)
    while tick <= ceiling:
        y = y_of(tick)
        draw.line((left, y, right, y), fill=GRID, width=SCALE)
        label = _man(tick) if tick else "0"
        draw.text((left - 8 * SCALE - draw.textlength(label, font=small), y - 7 * SCALE), label, fill=MUTED, font=small)
        tick += step
    slot = (right - left) / len(rows)
    bar = slot * 0.72
    for i, row in enumerate(rows):
        x0 = left + i * slot + (slot - bar) / 2
        base = Decimal(0)
        for key, color in order:
            value = row["parts"].get(key, Decimal(0))
            if value <= 0:
                continue
            y_top, y_bottom = y_of(base + value), y_of(base)
            # A white gap between segments, but never more than the segment itself (a tiny payout stays drawable).
            gap = SCALE if base and y_bottom - y_top > 2 * SCALE else 0
            draw.rectangle((x0, y_top, x0 + bar, max(y_top, y_bottom - gap)), fill=color)
            base += value
        if row["age"] == focus_age and base > 0:
            draw.rectangle((x0 - SCALE, y_of(base) - SCALE, x0 + bar + SCALE, bottom), outline=INK, width=SCALE)
        if row["age"] % 5 == 0:
            label = f"{row['age']}"
            draw.text((x0 + bar / 2 - draw.textlength(label, font=small) / 2, bottom + 6 * SCALE), label, fill=MUTED,
                      font=small)
    marker = next((i for i, r in enumerate(rows) if r["age"] == retire_age), None)
    if marker is not None:
        x = left + marker * slot
        for y in range(int(top), int(bottom), 8 * SCALE):
            draw.line((x, y, x, min(y + 4 * SCALE, bottom)), fill=MUTED, width=SCALE)
        draw.text((x + 4 * SCALE, top), f"{retire_age}세 은퇴", fill=MUTED, font=small)
    draw.line((left, bottom, right, bottom), fill=MUTED, width=SCALE)
    return _png(image)


def _step(peak: Decimal) -> Decimal:
    """A round grid step (만 원 units of 1, 2 or 5 times a power of ten) giving three to six lines."""
    for unit in (Decimal(1_000_000) * m for m in (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000)):
        if peak / unit <= 5:
            return unit
    return peak / 4
