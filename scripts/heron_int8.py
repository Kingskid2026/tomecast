"""Make a backbone-only INT8 copy of Docling Heron's ONNX file, to test against FP32 (#12).

Only the image-reading part (the ResNet-50 backbone's convolutions) is quantized; the
encoder, the decoder and the box heads stay FP32, as docs/architecture.md (section 10) says.
Two kinds:

* ``static`` (default): weights INT8 per channel, activations UINT8 with ranges measured on
  rendered pages (QDQ, which ONNX Runtime runs as QLinearConv).
* ``dynamic``: weights INT8, activation ranges found per run (ConvInteger).

Needs the ``onnx`` package as well as onnxruntime (a dev tool, not a Tomecast dependency):

    uv run --with onnx python -I scripts/heron_int8.py MODEL.onnx OUT.onnx --pdf BOOK.pdf ...

Calibration renders up to ``--pages`` pages, spread over the PDFs given. Use public-domain
PDFs only. ONNX Runtime's calibration session picks its own thread count, so run this under
``taskset -c 0-3`` to keep it to four CPUs.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path


def backbone_convs(path: Path) -> list[str]:
    import onnx

    model = onnx.load(str(path), load_external_data=False)
    names = [
        node.name
        for node in model.graph.node
        if node.op_type == "Conv" and any(name.startswith("model.backbone") for name in node.input)
    ]
    if not names:  # an optimizer renamed the weights: take the convolutions before the encoder
        for node in model.graph.node:
            if any(name.startswith("model.encoder") for name in node.input):
                break
            if node.op_type == "Conv":
                names.append(node.name)
    return names


def pages(pdfs: list[Path], count: int) -> Iterator[tuple[Path, int]]:
    import pypdfium2 as pdfium

    sizes = []
    for pdf in pdfs:
        document = pdfium.PdfDocument(pdf)
        sizes.append(len(document))
        document.close()
    per_pdf = max(1, count // max(1, len(pdfs)))
    for pdf, size in zip(pdfs, sizes, strict=True):
        step = max(1, size // per_pdf)
        for index in range(0, size, step)[:per_pdf]:
            yield pdf, index


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("model", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--pdf", type=Path, action="append", default=[])
    parser.add_argument("--pages", type=int, default=24)
    parser.add_argument("--kind", choices=("static", "dynamic"), default="static")
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args()
    os.environ.setdefault("OMP_NUM_THREADS", str(args.threads))

    import numpy
    import pypdfium2 as pdfium
    from onnxruntime.quantization import (
        CalibrationDataReader,
        CalibrationMethod,
        QuantFormat,
        QuantType,
        quantize_dynamic,
        quantize_static,
    )
    from onnxruntime.quantization.shape_inference import quant_pre_process

    from tomecast.readers.heron import SIZE, HeronModel

    with tempfile.TemporaryDirectory() as scratch:
        prepared = Path(scratch) / "prepared.onnx"
        quant_pre_process(str(args.model), str(prepared), skip_symbolic_shape=True)
        nodes = backbone_convs(prepared)
        print(f"quantizing {len(nodes)} backbone convolutions ({args.kind})", file=sys.stderr)

        if args.kind == "dynamic":
            quantize_dynamic(
                str(prepared),
                str(args.out),
                nodes_to_quantize=nodes,
                op_types_to_quantize=["Conv"],
                weight_type=QuantType.QInt8,
                per_channel=True,
            )
            return 0

        if not args.pdf:
            parser.error("static quantization needs --pdf pages to measure ranges on")
        renderer = HeronModel(args.model, threads=args.threads)
        inputs: list[dict[str, numpy.ndarray]] = []
        for pdf, index in pages(args.pdf, args.pages):
            document = pdfium.PdfDocument(pdf)
            page = document[index]
            inputs.append(
                {
                    "images": renderer.render(page),
                    "orig_target_sizes": numpy.array([[SIZE, SIZE]], dtype=numpy.int64),
                }
            )
            page.close()
            document.close()
        print(f"calibrating on {len(inputs)} pages", file=sys.stderr)

        class Pages(CalibrationDataReader):  # type: ignore[misc]
            def __init__(self) -> None:
                self._items = iter(inputs)

            def get_next(self) -> dict[str, numpy.ndarray] | None:
                return next(self._items, None)

        quantize_static(
            str(prepared),
            str(args.out),
            Pages(),
            quant_format=QuantFormat.QDQ,
            nodes_to_quantize=nodes,
            op_types_to_quantize=["Conv"],
            per_channel=True,
            activation_type=QuantType.QUInt8,
            weight_type=QuantType.QInt8,
            calibrate_method=CalibrationMethod.MinMax,
            extra_options={"WeightSymmetric": True, "ActivationSymmetric": False},
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
