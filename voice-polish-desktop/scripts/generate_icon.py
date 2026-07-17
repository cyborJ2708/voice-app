r"""Generates src/voice_polish_desktop/assets/icon.ico from scratch.

Dependency-free beyond what's already required (PySide6 + stdlib struct) —
deliberately avoids adding Pillow just for a one-time build artifact. Draws
the app mark at several sizes with QPainter, reusing overlay.py's exact
periwinkle->violet gradient so the icon and the pill visually match, then
hand-assembles a valid multi-size .ico from PNG-format entries (supported
by Windows .ico readers since Vista — no need for legacy BMP entries).

Run once at dev time:
    ..\.venv\Scripts\python.exe generate_icon.py
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtCore import QBuffer, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QApplication

from voice_polish_desktop.overlay import BAR_COLOR_BOTTOM, BAR_COLOR_TOP

SIZES = (16, 32, 48, 256)
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "src" / "voice_polish_desktop" / "assets" / "icon.ico"


def draw_icon(size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)

    rect = QRectF(0, 0, size, size)
    gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
    gradient.setColorAt(0.0, BAR_COLOR_TOP)
    gradient.setColorAt(1.0, BAR_COLOR_BOTTOM)

    path = QPainterPath()
    radius = size * 0.28
    path.addRoundedRect(rect, radius, radius)
    painter.fillPath(path, gradient)

    # small centered waveform-bar glyph, legible even at 16px
    bar_color = QColor(255, 255, 255, 235)
    pen = QPen(bar_color, max(1.4, size * 0.09), Qt.SolidLine, Qt.RoundCap)
    painter.setPen(pen)
    cy = size / 2
    bar_heights = (0.28, 0.55, 0.85, 0.55, 0.28)
    n = len(bar_heights)
    spacing = size * 0.16
    start_x = size / 2 - spacing * (n - 1) / 2
    for i, h_frac in enumerate(bar_heights):
        x = start_x + i * spacing
        h = size * h_frac * 0.5
        painter.drawLine(QPointF(x, cy - h / 2), QPointF(x, cy + h / 2))

    painter.end()
    return pixmap


def pixmap_to_png_bytes(pixmap: QPixmap) -> bytes:
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    pixmap.save(buf, "PNG")
    data = bytes(buf.data())
    buf.close()
    return data


def build_ico(png_entries: list[tuple[int, bytes]]) -> bytes:
    """Hand-assemble a valid .ico with PNG-format entries (no encoder library needed)."""
    count = len(png_entries)
    header = struct.pack("<HHH", 0, 1, count)  # reserved, type=icon, count

    dir_entries = b""
    image_data = b""
    offset = 6 + 16 * count  # header + one ICONDIRENTRY per image

    for size, png_bytes in png_entries:
        # width/height byte fields: 0 means 256 per the ICO spec
        w = size if size < 256 else 0
        h = size if size < 256 else 0
        entry = struct.pack(
            "<BBBBHHII",
            w, h,
            0,          # color count (0 = no palette, true color)
            0,          # reserved
            1,          # color planes
            32,         # bits per pixel
            len(png_bytes),
            offset,
        )
        dir_entries += entry
        image_data += png_bytes
        offset += len(png_bytes)

    return header + dir_entries + image_data


def main() -> None:
    app = QApplication(sys.argv)  # QPainter/QPixmap need a QApplication instance

    entries = []
    for size in SIZES:
        pixmap = draw_icon(size)
        png_bytes = pixmap_to_png_bytes(pixmap)
        entries.append((size, png_bytes))
        print(f"rendered {size}x{size} ({len(png_bytes)} bytes PNG)")

    ico_bytes = build_ico(entries)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_bytes(ico_bytes)
    print(f"wrote {OUTPUT_PATH} ({len(ico_bytes)} bytes)")


if __name__ == "__main__":
    main()
