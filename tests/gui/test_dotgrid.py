"""Tests for DotGrid and the dot image provider."""

from __future__ import annotations

import time
from typing import Any, cast

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter

from tomecast.gui import dotgrid
from tomecast.gui.dotgrid import DotGrid
from tomecast.gui.images import DotImageProvider

DONE = QColor("#00ff00")
CURRENT = QColor("#ff0000")
WAIT = QColor("#0000ff")


@pytest.fixture(scope="module")
def _app() -> QGuiApplication:
    return QGuiApplication.instance() or QGuiApplication([])  # type: ignore[return-value]


@pytest.fixture(autouse=True)
def app(_app: QGuiApplication) -> QGuiApplication:
    return _app


def make_grid(**values: object) -> DotGrid:
    grid = DotGrid()
    grid.setProperty("doneColor", DONE)
    grid.setProperty("currentColor", CURRENT)
    grid.setProperty("waitColor", WAIT)
    for name, value in values.items():
        grid.setProperty(name, value)
    return grid


def paint(grid: DotGrid) -> QImage:
    image = QImage(
        max(1, int(grid.implicitWidth())),
        max(1, int(grid.implicitHeight())),
        QImage.Format.Format_ARGB32,
    )
    image.fill(QColor("black"))
    painter = QPainter(image)
    grid.paint(painter)
    painter.end()
    return image


def centre(grid: DotGrid, index: int) -> tuple[int, int]:
    pitch = grid.property("dotSize") + grid.property("gap")
    row, col = divmod(index, grid.property("columns"))
    return int(col * pitch + grid.property("dotSize") / 2), int(
        row * pitch + grid.property("dotSize") / 2
    )


def colour_at(image: QImage, point: tuple[int, int]) -> str:
    return image.pixelColor(*point).name()


def test_defaults() -> None:
    grid = DotGrid()
    assert grid.property("total") == 0
    assert grid.property("done") == 0
    assert grid.property("columns") == 70
    assert grid.property("dotSize") == 5.0
    assert grid.property("gap") == 3.0
    assert grid.property("complete") is False
    assert isinstance(grid.property("doneColor"), QColor)
    assert grid.property("description") == "0 of 0 pieces voiced"


def test_notify_signals_fire_once_and_only_on_change() -> None:
    grid = DotGrid()
    seen: list[str] = []
    for name in (
        "total",
        "done",
        "columns",
        "dotSize",
        "gap",
        "doneColor",
        "currentColor",
        "waitColor",
        "complete",
        "description",
    ):
        getattr(grid, f"{name}Changed").connect(lambda n=name: seen.append(n))
    grid.setProperty("total", 100)
    grid.setProperty("total", 100)
    grid.setProperty("done", 4)
    grid.setProperty("columns", 10)
    grid.setProperty("dotSize", 4.0)
    grid.setProperty("gap", 2.0)
    grid.setProperty("doneColor", QColor("#123456"))
    grid.setProperty("doneColor", QColor("#123456"))
    grid.setProperty("currentColor", QColor("#654321"))
    grid.setProperty("waitColor", QColor("#111111"))
    grid.setProperty("complete", True)
    grid.setProperty("complete", True)
    assert seen == [
        "total",
        "description",
        "done",
        "description",
        "columns",
        "dotSize",
        "gap",
        "doneColor",
        "currentColor",
        "waitColor",
        "complete",
    ]
    assert grid.property("description") == "4 of 100 pieces voiced"


def test_implicit_size_follows_the_grid() -> None:
    grid = DotGrid()
    assert (grid.implicitWidth(), grid.implicitHeight()) == (0, 0)
    grid.setProperty("total", 1610)  # 70 columns by 23 rows, like Alice
    assert grid.implicitWidth() == 70 * 8 - 3
    assert grid.implicitHeight() == 23 * 8 - 3
    grid.setProperty("columns", 10)
    grid.setProperty("total", 25)
    grid.setProperty("dotSize", 4.0)
    grid.setProperty("gap", 1.0)
    assert grid.implicitWidth() == 10 * 5 - 1
    assert grid.implicitHeight() == 3 * 5 - 1
    grid.setProperty("total", 4)  # fewer dots than columns: a short row
    assert grid.implicitWidth() == 4 * 5 - 1


def test_update_only_when_a_property_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    grid = DotGrid()
    calls: list[int] = []
    monkeypatch.setattr(DotGrid, "update", lambda self, *a: calls.append(1))
    grid.setProperty("total", 10)
    assert len(calls) == 1
    grid.setProperty("total", 10)
    grid.setProperty("done", 0)
    assert len(calls) == 1
    grid.setProperty("done", 3)
    assert len(calls) == 2


def test_paint_colours_done_current_and_wait() -> None:
    grid = make_grid(total=30, done=12, columns=10)
    image = paint(grid)
    assert colour_at(image, centre(grid, 0)) == DONE.name()
    assert colour_at(image, centre(grid, 11)) == DONE.name()
    assert colour_at(image, centre(grid, 12)) == CURRENT.name()
    assert colour_at(image, centre(grid, 13)) == WAIT.name()
    assert colour_at(image, centre(grid, 29)) == WAIT.name()
    # The gap between dots is left clear.
    assert colour_at(image, (7, 2)) == "#000000"


def test_complete_paints_everything_done() -> None:
    grid = make_grid(total=30, done=12, columns=10, complete=True)
    image = paint(grid)
    for index in (0, 12, 13, 29):
        assert colour_at(image, centre(grid, index)) == DONE.name()


def test_done_past_total_is_all_done() -> None:
    grid = make_grid(total=5, done=99, columns=5)
    image = paint(grid)
    assert colour_at(image, centre(grid, 4)) == DONE.name()


def test_paint_is_fast_for_1610_dots() -> None:
    grid = make_grid(total=1610, done=800)
    image = QImage(560, 181, QImage.Format.Format_ARGB32)
    painter = QPainter(image)
    grid.paint(painter)  # warm up
    start = time.perf_counter()
    grid.paint(painter)
    elapsed = time.perf_counter() - start
    painter.end()
    assert elapsed < 2.0  # generous, so a busy runner can't fail it


def test_register_types_is_idempotent() -> None:
    dotgrid.register_types()
    dotgrid.register_types()


def png_bytes(color: str = "#cccccc") -> bytes:
    image = QImage(60, 90, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    for x in range(30):
        for y in range(90):
            image.setPixelColor(x, y, QColor("#202020"))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, cast(Any, "PNG"))
    return bytes(data.data())


def test_provider_serves_the_cantoria_in_both_themes() -> None:
    provider = DotImageProvider()
    dark = provider.requestImage("cantoria/dark", QSize(), QSize())
    light = provider.requestImage("cantoria/light?3", QSize(), QSize())
    # The tests run at a 1x screen scale, so the default 240 px wide art is 240 screen pixels.
    assert dark.width() == 240 and dark.devicePixelRatio() == 1.0
    assert dark.height() == pytest.approx(358, abs=2)
    assert dark.pixelColor(0, 0).lightness() < 60
    assert light.size() == dark.size()


def test_provider_honours_requested_width_and_caches() -> None:
    provider = DotImageProvider()
    small = provider.requestImage("cantoria/dark", QSize(), QSize(120, 0))
    assert small.width() == 120  # Qt asks in screen pixels and gets exactly that
    again = provider.requestImage("cantoria/dark", QSize(), QSize(120, 0))
    assert again.cacheKey() == small.cacheKey()


def test_provider_serves_a_cover_once_set_and_forgets_it() -> None:
    provider = DotImageProvider()
    assert provider.requestImage("cover/dark", QSize(), QSize()).size() == QSize(1, 1)
    provider.set_cover(png_bytes())
    cover = provider.requestImage("cover/dark?1", QSize(), QSize(60, 0))
    assert cover.width() == 60
    assert cover.height() == 90
    provider.set_cover(png_bytes("#ffffff"))
    other = provider.requestImage("cover/dark?2", QSize(), QSize(60, 0))
    assert other.cacheKey() != cover.cacheKey()
    provider.set_cover(None)
    assert provider.requestImage("cover/light", QSize(), QSize()).size() == QSize(1, 1)
    provider.set_cover(b"not an image")
    assert provider.requestImage("cover/light", QSize(), QSize()).size() == QSize(1, 1)


@pytest.mark.parametrize("name", ["nope/dark", "cover", "cantoria/blue", "", "a/b/c"])
def test_provider_unknown_ids_are_transparent(name: str) -> None:
    image = DotImageProvider().requestImage(name, QSize(), QSize())
    assert image.size() == QSize(1, 1)
    assert image.pixelColor(0, 0).alpha() == 0


def sliver_png(width: int, height: int) -> bytes:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor("#cccccc"))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, cast(Any, "PNG"))
    return bytes(data.data())


@pytest.mark.parametrize("size", [(1, 4000), (4000, 1), (10, 41), (41, 10)])
def test_provider_treats_a_sliver_cover_as_no_cover(size: tuple[int, int]) -> None:
    provider = DotImageProvider()
    provider.set_cover(sliver_png(*size))
    assert provider.requestImage("cover/dark", QSize(), QSize(200, 0)).size() == QSize(1, 1)


def test_provider_keeps_covers_at_the_edge_of_the_allowed_shape() -> None:
    provider = DotImageProvider()
    provider.set_cover(sliver_png(10, 40))  # exactly 1:4
    assert provider.requestImage("cover/dark", QSize(), QSize(100, 0)).height() == 400


def test_provider_renders_outside_its_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slow render must not block set_cover (which runs on the GUI thread)."""
    import threading

    from tomecast.gui import dots

    provider = DotImageProvider()
    provider.set_cover(png_bytes())
    entered, release = threading.Event(), threading.Event()
    real = dots.render_dots

    def slow(*args: Any, **kwargs: Any) -> QImage:
        entered.set()
        assert release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr("tomecast.gui.images.render_dots", slow)
    result: list[QImage] = []
    thread = threading.Thread(
        target=lambda: result.append(provider.requestImage("cover/dark", QSize(), QSize(60, 0)))
    )
    thread.start()
    try:
        assert entered.wait(10)
        done = threading.Event()

        def replace() -> None:
            provider.set_cover(png_bytes("#ffffff"))
            done.set()

        setter = threading.Thread(target=replace)
        setter.start()
        assert done.wait(2), "set_cover was blocked by a render in progress"
        setter.join()
    finally:
        release.set()
        thread.join()
    assert result and result[0].width() == 60
    # The render of the old cover was not cached for the new one.
    monkeypatch.setattr("tomecast.gui.images.render_dots", real)
    fresh = provider.requestImage("cover/dark", QSize(), QSize(60, 0))
    assert fresh.cacheKey() != result[0].cacheKey()
