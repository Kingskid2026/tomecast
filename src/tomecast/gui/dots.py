"""Turn an image into Night Studio dots, using Qt only (no Pillow, no numpy).

A classic halftone, drawn the same way as the art on the maintainer's site
(ambrose-sigler.pages.dev): round dots on a grid turned by the screen angle (about 29 degrees),
each dot's *area* following the ink, so the picture is made of circles of different sizes rather
than a grid of pixels.

The steps:

1. Grayscale, shrunk to two samples per dot so that a dot stands for its whole cell. Fine detail
   such as engraving lines would otherwise turn into noise.
2. Autocontrast (2% cutoff at each end), so dull covers still use the full range of dot sizes.
3. For each dot, sample the tone at its centre, soften the extremes with a smoothstep, and draw a
   circle of radius ``cell * 0.5 * sqrt(ink)``.

One difference from the site: there a full-ink dot is 0.74 of a cell wide each side, so the
deepest tones melt into blobs. Here the biggest dots just touch their neighbours, so every dot
stays a whole circle, like the dots in the rest of the window.

Dark theme draws light dots on near-black (big dots where the picture is bright); light theme
draws dark dots on paper (big dots where it is dark). The picture is never inverted.

On a 1x screen the dots spread out to at least ``MIN_DEVICE_CELL`` screen pixels apart, because a
dot only a couple of pixels wide can't look round.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter

DARK_FG = QColor(242, 242, 242)
DARK_BG = QColor(11, 11, 12)
LIGHT_FG = QColor(11, 11, 12)
LIGHT_BG = QColor(242, 242, 242)

CELL = 3.4
"""Distance between neighbouring dots, in logical px (the site's plates use the same)."""
SCREEN_COS = 0.8776
SCREEN_SIN = 0.4794
"""The halftone screen angle, about 28.6 degrees: dots never line up into rows and columns."""
DOT_SCALE = 0.5
"""A full-ink dot's radius as a fraction of ``CELL``: the biggest dots touch, never overlap."""
TONE_LOW = 0.03
TONE_HIGH = 0.97
"""Tones below ``TONE_LOW`` get no ink at all, tones above ``TONE_HIGH`` get full ink."""
MIN_RADIUS = 0.2
"""Dots smaller than this (logical px) are left out."""
MIN_DEVICE_CELL = 4.5
"""Dots are never closer than this many *screen* pixels. Any closer and a dot is too few pixels
to look round, so on a 1x screen the dots spread out a little instead of turning into specks."""

MAX_ASPECT = 4
"""The tallest an image is drawn, as a multiple of its width."""

_IDENTITY = bytes(range(256))
_CUTOFF_PERCENT = 2.0
_SAMPLES_PER_CELL = 2


def to_gray(image: QImage) -> QImage:
    """An 8-bit grayscale copy of ``image``."""
    return image.convertToFormat(QImage.Format.Format_Grayscale8)


def gray_bytes(image: QImage) -> bytes:
    """The pixels of a Grayscale8 image, row by row, without Qt's row padding."""
    width, height = image.width(), image.height()
    stride = image.bytesPerLine()
    raw = memoryview(image.constBits())[: stride * height]
    if stride == width:
        return bytes(raw)
    return b"".join(raw[y * stride : y * stride + width] for y in range(height))


def gray_image(data: bytes, width: int, height: int) -> QImage:
    """A Grayscale8 image (owning its pixels) from tightly packed ``data``."""
    return QImage(data, width, height, width, QImage.Format.Format_Grayscale8).copy()


def histogram(data: bytes) -> list[int]:
    """How many pixels have each value 0-255."""
    counts = [0] * 256
    for value in set(data):
        counts[value] = data.count(value)
    return counts


def autocontrast_lut(counts: Sequence[int], cutoff: float = _CUTOFF_PERCENT) -> bytes:
    """Lookup table that stretches the tones, ignoring ``cutoff`` percent at each end."""
    hist = list(counts)
    total = sum(hist)
    if total == 0:
        return _IDENTITY
    cut = total * cutoff / 100
    lo = 0
    remaining = cut
    while lo < 255 and remaining > 0:
        if hist[lo] > remaining:
            hist[lo] -= int(remaining)
            remaining = 0
        else:
            remaining -= hist[lo]
            hist[lo] = 0
            lo += 1
    hi = 255
    remaining = cut
    while hi > 0 and remaining > 0:
        if hist[hi] > remaining:
            hist[hi] -= int(remaining)
            remaining = 0
        else:
            remaining -= hist[hi]
            hist[hi] = 0
            hi -= 1
    nonzero = [i for i, n in enumerate(hist) if n]
    if not nonzero or nonzero[-1] <= nonzero[0]:
        return _IDENTITY
    lo, hi = nonzero[0], nonzero[-1]
    scale = 255 / (hi - lo)
    return bytes(min(255, max(0, int((i - lo) * scale))) for i in range(256))


def autocontrast(data: bytes, cutoff: float = _CUTOFF_PERCENT) -> bytes:
    """Stretch tones so the darkest and lightest 2% are clipped."""
    return data.translate(autocontrast_lut(histogram(data), cutoff))


def smoothstep(low: float, high: float, value: float) -> float:
    """0 below ``low``, 1 above ``high``, and an S-curve between (as in GLSL)."""
    t = min(1.0, max(0.0, (value - low) / (high - low)))
    return t * t * (3 - 2 * t)


def effective_cell(cell: float, device_pixel_ratio: float) -> float:
    """The dot spacing actually used: ``cell``, but never under ``MIN_DEVICE_CELL`` screen px."""
    return max(cell, MIN_DEVICE_CELL / device_pixel_ratio)


def dot_radius(ink: float, cell: float = CELL) -> float:
    """The radius of a dot carrying ``ink`` (0-1): its area, not its width, follows the ink."""
    return cell * DOT_SCALE * math.sqrt(max(0.0, ink))


def screen_points(width: float, height: float, cell: float = CELL) -> list[tuple[float, float]]:
    """Dot centres on the turned halftone grid that fall inside a ``width`` x ``height`` box."""
    cos, sin = SCREEN_COS, SCREEN_SIN
    points: list[tuple[float, float]] = []
    reach = math.ceil((width + height) / cell) + 2
    for row in range(-reach, reach + 1):
        u = (row + 0.5) * cell
        # x = cos*s - sin*u must lie in [0, width]; y = sin*s + cos*u in [0, height].
        s_lo = max(sin * u / cos, -cos * u / sin)
        s_hi = min((width + sin * u) / cos, (height - cos * u) / sin)
        if s_hi < s_lo:
            continue
        for col in range(math.ceil(s_lo / cell - 0.5), math.floor(s_hi / cell - 0.5) + 1):
            s = (col + 0.5) * cell
            x, y = cos * s - sin * u, sin * s + cos * u
            if 0 <= x <= width and 0 <= y <= height:
                points.append((x, y))
    return points


def _sampler(
    tones: bytes, width: int, height: int, scale_x: float, scale_y: float
) -> Callable[[float, float], float]:
    """A function giving the bilinear-filtered tone (0-1) at a point in logical px."""
    last_x, last_y = width - 1, height - 1

    def sample(x: float, y: float) -> float:
        fx = min(float(last_x), max(0.0, x * scale_x - 0.5))
        fy = min(float(last_y), max(0.0, y * scale_y - 0.5))
        x0, y0 = int(fx), int(fy)
        x1, y1 = min(x0 + 1, last_x), min(y0 + 1, last_y)
        tx, ty = fx - x0, fy - y0
        top = tones[y0 * width + x0] * (1 - tx) + tones[y0 * width + x1] * tx
        bottom = tones[y1 * width + x0] * (1 - tx) + tones[y1 * width + x1] * tx
        return (top * (1 - ty) + bottom * ty) / 255

    return sample


def render_dots(
    source: QImage,
    width: int,
    *,
    dark: bool,
    device_pixel_ratio: float = 2.0,
    cell: float = CELL,
) -> QImage:
    """Draw ``source`` as dots, ``width`` logical px wide, height following its aspect ratio."""
    if source.isNull() or width < 1:
        blank = QImage(1, 1, QImage.Format.Format_ARGB32)
        blank.fill(Qt.GlobalColor.transparent)
        return blank
    # A second guard (the cover provider already refuses extreme shapes): never draw taller
    # than 4x the width, however thin the source is.
    height = min(max(1, round(width * source.height() / source.width())), width * MAX_ASPECT)
    cell = effective_cell(cell, device_pixel_ratio)

    # Two samples per dot: sampling between them averages the whole cell under each dot.
    map_w = max(1, round(width / cell * _SAMPLES_PER_CELL))
    map_h = max(1, round(height / cell * _SAMPLES_PER_CELL))
    small = to_gray(source).scaled(
        map_w,
        map_h,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    tone_at = _sampler(autocontrast(gray_bytes(small)), map_w, map_h, map_w / width, map_h / height)

    fg, bg = (DARK_FG, DARK_BG) if dark else (LIGHT_FG, LIGHT_BG)
    out_w, out_h = round(width * device_pixel_ratio), round(height * device_pixel_ratio)
    canvas = QImage(out_w, out_h, QImage.Format.Format_RGB32)
    canvas.fill(bg)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(fg)
    dpr = device_pixel_ratio
    for x, y in screen_points(width, height, cell):
        tone = smoothstep(TONE_LOW, TONE_HIGH, tone_at(x, y))
        radius = dot_radius(tone if dark else 1 - tone, cell)
        if radius < MIN_RADIUS:
            continue
        painter.drawEllipse(QPointF(x * dpr, y * dpr), radius * dpr, radius * dpr)
    painter.end()
    canvas.setDevicePixelRatio(dpr)
    return canvas
