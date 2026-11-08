"""Render the app icon's PNG sizes from the SVG: ``uv run python scripts/render_icons.py``.

The PNGs are committed, so nothing runs this at install time. Run it after editing
``src/tomecast/gui/icons/tomecast.svg``, and commit the results. It needs ``rsvg-convert``
(from librsvg, usually the ``librsvg2-bin`` or ``librsvg`` package), a dev-time tool only.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SVG = ROOT / "src" / "tomecast" / "gui" / "icons" / "tomecast.svg"
OUT = ROOT / "src" / "tomecast" / "gui" / "icons" / "png"
# Keep in step with ICON_SIZES in src/tomecast/desktop.py.
SIZES = (16, 24, 32, 48, 64, 128, 256)


def main() -> int:
    renderer = shutil.which("rsvg-convert")
    if renderer is None:
        print("render_icons: error: rsvg-convert isn't installed", file=sys.stderr)
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        target = OUT / f"tomecast-{size}.png"
        # The smallest sizes have their own pixel-snapped drawing (tomecast-16.svg, …), because
        # scaling the 256 px grid down puts its dots between pixels and they blur together.
        snapped = SVG.with_name(f"tomecast-{size}.svg")
        source = snapped if snapped.is_file() else SVG
        command = [renderer, "--width", str(size), "--height", str(size)]
        subprocess.run([*command, "--output", str(target), str(source)], check=True)
        print(f"wrote {target.relative_to(ROOT)} from {source.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
