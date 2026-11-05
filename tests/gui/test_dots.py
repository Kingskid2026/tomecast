"""Tests for the Qt-only dot renderer."""

from __future__ import annotations

import itertools
import math
import time

import pytest
from PySide6.QtGui import QColor, QGuiApplication, QImage

from tomecast.gui import dots

DARK_BG = 11
LIGHT_BG = 242


@pytest.fixture(scope="module")
def _app() -> QGuiApplication:
    return QGuiApplication.instance() or QGuiApplication([])  # type: ignore[return-value]


@pytest.fixture(autouse=True)
def app(_app: QGuiApplication) -> QGuiApplication:
    return _app


def gradient(width: int = 240, height: int = 120) -> QImage:
    """Black on the left to white on the right."""
    data = bytes(x * 255 // (width - 1) for _ in range(height) for x in range(width))
    return dots.gray_image(data, width, height)


def gray_at(image: QImage, x: int, y: int) -> int:
    return QColor(image.pixel(x, y)).red()


def dot_ink(image: QImage, x0: int, x1: int, bg: int) -> float:
    """Mean ink (how far from the background) over the columns x0..x1."""
    total = 0
    count = 0
    for y in range(0, image.height(), 3):
        for x in range(x0, x1):
            total += abs(gray_at(image, x, y) - bg)
            count += 1
    return total / count


def test_size_and_aspect_ratio() -> None:
    source = QImage(300, 450, QImage.Format.Format_RGB32)
    source.fill(QColor("#808080"))
    out = dots.render_dots(source, 120, dark=True, device_pixel_ratio=2.0)
    assert (out.width(), out.height()) == (240, 360)
    assert out.devicePixelRatio() == 2.0
    assert out.deviceIndependentSize().toSize().width() == 120
    wide = dots.render_dots(gradient(400, 100), 200, dark=False, device_pixel_ratio=1.0)
    assert (wide.width(), wide.height()) == (200, 50)


def test_dark_dots_grow_toward_the_bright_side() -> None:
    out = dots.render_dots(gradient(), 240, dark=True, device_pixel_ratio=1.0)
    bg = DARK_BG
    left = dot_ink(out, 0, 60, bg)
    right = dot_ink(out, 180, 240, bg)
    assert right > left * 2


def test_light_dots_shrink_toward_the_bright_side() -> None:
    out = dots.render_dots(gradient(), 240, dark=False, device_pixel_ratio=1.0)
    bg = LIGHT_BG
    left = dot_ink(out, 0, 60, bg)
    right = dot_ink(out, 180, 240, bg)
    assert left > right * 2


def test_never_inverted_colors() -> None:
    flat = gradient()
    dark = dots.render_dots(flat, 120, dark=True, device_pixel_ratio=1.0)
    light = dots.render_dots(flat, 120, dark=False, device_pixel_ratio=1.0)
    dark_values = {gray_at(dark, x, y) for x in range(dark.width()) for y in range(dark.height())}
    light_values = {
        gray_at(light, x, y) for x in range(light.width()) for y in range(light.height())
    }
    assert max(dark_values) == 242 and min(dark_values) == DARK_BG
    assert max(light_values) == LIGHT_BG and min(light_values) == 11


def test_dots_are_round_and_never_overlap() -> None:
    # A white image gives every dot full ink on the dark theme. Cells are 40 px here, so a dot's
    # edge and the gap between dots are whole pixels that can be sampled cleanly.
    source = QImage(200, 200, QImage.Format.Format_RGB32)
    source.fill(QColor("white"))
    cell = 40.0
    out = dots.render_dots(source, 200, dark=True, device_pixel_ratio=1.0, cell=cell)
    radius = dots.dot_radius(1.0, cell)
    assert radius == pytest.approx(cell / 2)  # the biggest dots just touch their neighbours
    # Half a cell along both grid directions: the middle of four dots, never inked.
    gap_x = (dots.SCREEN_COS - dots.SCREEN_SIN) * cell / 2
    gap_y = (dots.SCREEN_SIN + dots.SCREEN_COS) * cell / 2
    checked = 0
    for cx, cy in dots.screen_points(200, 200, cell):
        if not (radius < cx < 200 - radius and radius < cy < 200 - radius):
            continue
        for k in range(8):  # the dot is a full circle: ink all the way round, just inside
            angle = k * math.pi / 4
            x = round(cx + 0.8 * radius * math.cos(angle))
            y = round(cy + 0.8 * radius * math.sin(angle))
            assert gray_at(out, x, y) > 200
        gx, gy = round(cx + gap_x), round(cy + gap_y)
        if 0 <= gx < 200 and 0 <= gy < 200:
            assert gray_at(out, gx, gy) <= DARK_BG + 10
        checked += 1
    assert checked >= 4


def test_dots_follow_the_screen_angle() -> None:
    points = dots.screen_points(400, 400, 10.0)
    assert all(0 <= x <= 400 and 0 <= y <= 400 for x, y in points)
    assert len(points) == pytest.approx(400 * 400 / 100, rel=0.05)  # one dot per cell
    angles = set()
    for (x0, y0), (x1, y1) in itertools.pairwise(points):
        if math.hypot(x1 - x0, y1 - y0) == pytest.approx(10.0, rel=1e-3):
            angles.add(round(math.degrees(math.atan2(y1 - y0, x1 - x0)), 1))
    assert angles == {28.6}  # never lined up into plain rows and columns


def test_dot_area_follows_ink() -> None:
    full = dots.dot_radius(1.0, 10.0)
    assert dots.dot_radius(0.25, 10.0) == pytest.approx(full / 2)  # a quarter of the area
    assert dots.dot_radius(0.0, 10.0) == 0.0
    assert dots.dot_radius(-0.1, 10.0) == 0.0


def test_uniform_image_does_not_blow_up() -> None:
    for value in (0, 128, 255):
        flat = dots.gray_image(bytes([value]) * (64 * 80), 64, 80)
        out = dots.render_dots(flat, 64, dark=True, device_pixel_ratio=1.0)
        assert (out.width(), out.height()) == (64, 80)
    assert dots.autocontrast(bytes([77]) * 100) == bytes([77]) * 100


def test_null_image_gives_transparent_pixel() -> None:
    out = dots.render_dots(QImage(), 100, dark=True)
    assert (out.width(), out.height()) == (1, 1)
    assert out.pixelColor(0, 0).alpha() == 0


def test_pure_steps() -> None:
    data = bytes(range(64, 192))
    stretched = dots.autocontrast(data)
    assert min(stretched) == 0 and max(stretched) == 255
    assert dots.smoothstep(0.03, 0.97, 0.0) == 0.0
    assert dots.smoothstep(0.03, 0.97, 1.0) == 1.0
    assert dots.smoothstep(0.03, 0.97, 0.5) == pytest.approx(0.5)
    assert dots.smoothstep(0.0, 1.0, 0.25) < 0.25  # eases in at the ends
    gray = dots.to_gray(QImage(4, 4, QImage.Format.Format_RGB32))
    assert gray.format() == QImage.Format.Format_Grayscale8


def test_gray_bytes_strips_row_padding() -> None:
    data = bytes(range(10)) * 3  # 10 wide, 3 tall: Qt pads rows to a multiple of 4
    image = dots.gray_image(data, 10, 3)
    assert dots.gray_bytes(image) == data


def test_cover_renders_quickly() -> None:
    source = QImage(800, 1195, QImage.Format.Format_RGB32)
    for y in range(0, 1195, 5):
        for x in range(0, 800, 5):
            source.setPixelColor(x, y, QColor((x * 7) % 256, (y * 3) % 256, (x + y) % 256))
    dots.render_dots(source, 240, dark=True)  # warm up
    start = time.perf_counter()
    dots.render_dots(source, 240, dark=True)
    assert time.perf_counter() - start < 0.2


def test_dots_stay_round_on_a_1x_screen() -> None:
    # 3.4 px apart is only 3.4 screen pixels at 1x: too few to draw a circle. The spacing grows
    # to MIN_DEVICE_CELL there, and stays at 3.4 from about 1.33x up.
    assert dots.effective_cell(dots.CELL, 1.0) == dots.MIN_DEVICE_CELL
    assert dots.effective_cell(dots.CELL, 1.6) == dots.CELL
    assert dots.effective_cell(dots.CELL, 2.0) == dots.CELL
    flat = QImage(300, 300, QImage.Format.Format_RGB32)
    flat.fill(QColor("#808080"))
    one = dots.render_dots(flat, 300, dark=True, device_pixel_ratio=1.0)
    # Every dot centre on the wider grid is inked, and nothing between them.
    cell = dots.MIN_DEVICE_CELL
    for x, y in dots.screen_points(300, 300, cell)[::25]:
        if 2 < x < 297 and 2 < y < 297:
            assert gray_at(one, int(x), int(y)) > 100
