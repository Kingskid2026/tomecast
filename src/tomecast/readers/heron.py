"""Docling Heron, the page layout model, on ONNX Runtime (the optional ``layout`` extra).

Heron (``docling-project/docling-layout-heron``, IBM, Apache 2.0) is an RT-DETRv2 object
detector with a ResNet-50 backbone, 43M parameters, trained on DocLayNet and more. Tomecast
runs the official ONNX export (``docling-project/docling-layout-heron-onnx``, FP32, 171 MB):
it takes a 640 x 640 RGB picture of the page as bytes (0 to 255; the graph scales them) and
returns 300 boxes with a label and a score each, already sorted out from the model's queries.

The page is rendered straight to 640 x 640 by PDFium (stretched, as Docling's own resize
stretches it), and the boxes are mapped back to PDF coordinates with PDFium's own transform,
so rotated pages and odd crop boxes come out right.

numpy and onnxruntime are imported only when a model is loaded, so the base install doesn't
need them: ``uv tool install 'tomecast[layout]'`` adds them.
"""

from __future__ import annotations

import ctypes
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pypdfium2.raw as pdfium_c

from tomecast import downloads
from tomecast.readers.pdf_layout import MODEL_LABELS, HeronLayout, Label, Region

SIZE = 640  # the model's input is SIZE x SIZE pixels
# Boxes less sure than this are ignored (their words still get read, as orphan text). Docling
# keeps boxes from 0.3 and then filters by label at about 0.45 to 0.5.
THRESHOLD = 0.5
THRESHOLDS = {Label.PAGE_HEADER: 0.45, Label.PAGE_FOOTER: 0.45, Label.SECTION_HEADER: 0.45}
DEFAULT_THREADS = 4


class LayoutUnavailable(RuntimeError):
    """The layout extra isn't installed, or the model file can't be loaded."""


def heron_layout(root: Path | None = None, *, threads: int = DEFAULT_THREADS) -> HeronLayout:
    """The AI layout pass with the pinned Heron model, for ``read_pdf(path, layout=...)``.

    The model must already be downloaded (``downloads.ensure(manifest.HERON_LAYOUT, root)``,
    after asking the user: it's 171 MB).

    Raises:
        LayoutUnavailable: if the extra isn't installed or the model isn't downloaded.
    """
    from tomecast import manifest

    root = root if root is not None else downloads.data_dir()
    if not downloads.is_installed(manifest.HERON_LAYOUT, root):
        raise LayoutUnavailable("The PDF layout model isn't downloaded yet")
    path = downloads.local_path(manifest.HERON_LAYOUT, root)
    return HeronLayout(HeronModel(path, threads=threads))


class HeronModel:
    """Docling Heron on ONNX Runtime's CPU backend: a :class:`~tomecast.readers.pdf_layout.
    Detector`."""

    def __init__(self, model: Path, *, threads: int = DEFAULT_THREADS) -> None:
        try:
            import numpy
            import onnxruntime
        except ImportError as error:
            raise LayoutUnavailable(
                "The PDF layout add-on needs the 'layout' extra: uv tool install 'tomecast[layout]'"
            ) from error
        self._np = numpy
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = max(1, threads)
        options.inter_op_num_threads = 1
        options.execution_mode = onnxruntime.ExecutionMode.ORT_SEQUENTIAL
        options.graph_optimization_level = onnxruntime.GraphOptimizationLevel.ORT_ENABLE_ALL
        try:
            self._session = onnxruntime.InferenceSession(
                str(model), options, providers=["CPUExecutionProvider"]
            )
        except Exception as error:  # onnxruntime raises its own exception types
            raise LayoutUnavailable(
                f"The layout model {model.name} can't be loaded: {error}"
            ) from error
        self._sizes = numpy.array([[SIZE, SIZE]], dtype=numpy.int64)

    def render(self, page: Any) -> Any:
        """The page as the model's input: uint8, shape (1, 3, SIZE, SIZE), RGB."""
        np = self._np
        bitmap = pdfium_c.FPDFBitmap_Create(SIZE, SIZE, 0)
        if not bitmap:
            raise MemoryError("PDFium couldn't make a bitmap")
        try:
            pdfium_c.FPDFBitmap_FillRect(bitmap, 0, 0, SIZE, SIZE, 0xFFFFFFFF)
            flags = pdfium_c.FPDF_ANNOT | pdfium_c.FPDF_REVERSE_BYTE_ORDER
            pdfium_c.FPDF_RenderPageBitmap(bitmap, page.raw, 0, 0, SIZE, SIZE, 0, flags)
            stride = int(pdfium_c.FPDFBitmap_GetStride(bitmap))
            buffer = ctypes.cast(
                pdfium_c.FPDFBitmap_GetBuffer(bitmap), ctypes.POINTER(ctypes.c_uint8)
            )
            pixels = np.ctypeslib.as_array(buffer, shape=(SIZE, stride)).copy()
        finally:
            pdfium_c.FPDFBitmap_Destroy(bitmap)
        rgb = pixels[:, : SIZE * 4].reshape(SIZE, SIZE, 4)[:, :, :3]
        return np.ascontiguousarray(rgb.transpose(2, 0, 1))[None]

    def run(self, pixels: Any) -> tuple[Any, Any, Any]:
        """The raw model output for a rendered page: labels, boxes (x0, y0, x1, y1 in
        pixels, y going down) and scores, 300 of each."""
        labels, boxes, scores = self._session.run(
            None, {"images": pixels, "orig_target_sizes": self._sizes}
        )
        return labels[0], boxes[0], scores[0]

    def detect(self, page: Any, width: float, height: float) -> list[Region]:
        labels, boxes, scores = self.run(self.render(page))
        return to_regions(page, labels, boxes, scores)


def to_regions(page: Any, labels: Any, boxes: Any, scores: Any) -> list[Region]:
    """The confident boxes, in PDF page coordinates."""
    to_page = _device_to_page(page)
    regions: list[Region] = []
    rows = zip(labels.tolist(), boxes.tolist(), scores.tolist(), strict=True)
    for label_index, box, score in rows:
        if not 0 <= label_index < len(MODEL_LABELS):
            continue
        label = MODEL_LABELS[label_index]
        if score < THRESHOLDS.get(label, THRESHOLD):
            continue
        ax, ay = to_page(box[0], box[1])
        bx, by = to_page(box[2], box[3])
        regions.append(
            Region(label, float(score), min(ax, bx), min(ay, by), max(ax, bx), max(ay, by))
        )
    return _without_duplicates(regions)


def _device_to_page(page: Any) -> Callable[[float, float], tuple[float, float]]:
    """A function mapping a pixel of the SIZE x SIZE rendering to PDF page coordinates."""

    def point(x: int, y: int) -> tuple[float, float]:
        px, py = ctypes.c_double(), ctypes.c_double()
        pdfium_c.FPDF_DeviceToPage(page.raw, 0, 0, SIZE, SIZE, 0, x, y, px, py)
        return px.value, py.value

    ox, oy = point(0, 0)
    xx, xy = point(SIZE, 0)
    yx, yy = point(0, SIZE)

    def transform(x: float, y: float) -> tuple[float, float]:
        u, v = x / SIZE, y / SIZE
        return ox + u * (xx - ox) + v * (yx - ox), oy + u * (xy - oy) + v * (yy - oy)

    return transform


def iou(a: Region, b: Region) -> float:
    w = min(a.x1, b.x1) - max(a.x0, b.x0)
    h = min(a.y1, b.y1) - max(a.y0, b.y0)
    if w <= 0 or h <= 0:
        return 0.0
    inter = w * h
    return inter / (a.area + b.area - inter)


def _without_duplicates(regions: list[Region], limit: float = 0.8) -> list[Region]:
    """Drop a box that's nearly the same as a more confident one (any label)."""
    kept: list[Region] = []
    for region in sorted(regions, key=lambda r: -r.score):
        if all(iou(region, other) < limit for other in kept):
            kept.append(region)
    return kept
