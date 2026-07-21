r"""Standalone preview of the pill overlay — no mic, no hotkey, no backend.

Run it to see the real visual states end-to-end, cycling continuously:
  idle (flat line, always visible) -> listening (waveform) -> processing
  (shimmer) -> success/error (alternating) -> back to idle -> repeat

Usage:
    ..\.venv\Scripts\python.exe demo_overlay.py [--loop]

Press Ctrl+C in the console to quit at any time.
"""
from __future__ import annotations

import math
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from voice_polish_desktop.overlay import PillOverlay

IDLE_SECONDS = 1.6
LISTENING_SECONDS = 3.2
PROCESSING_SECONDS = 1.4
SUCCESS_SECONDS = 1.0


def main() -> None:
    loop = "--loop" in sys.argv
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    pill = PillOverlay()
    start = time.monotonic()
    cycle_count = {"n": 0}

    level_timer = QTimer()
    level_timer.setInterval(16)

    def feed_fake_mic_level() -> None:
        t = time.monotonic() - start
        # A speech-like envelope: slow syllable pulses + noise, occasional pause.
        envelope = 0.5 + 0.5 * math.sin(t * 2.3)
        syllable = 0.5 + 0.5 * math.sin(t * 9.0)
        noise = random.uniform(-0.08, 0.08)
        level = max(0.0, envelope * syllable + noise)
        pill.push_level(level)

    level_timer.timeout.connect(feed_fake_mic_level)

    def start_idle() -> None:
        level_timer.stop()
        pill.enter_idle()
        if loop:
            QTimer.singleShot(int(IDLE_SECONDS * 1000), run_cycle)

    def run_cycle() -> None:
        pill.enter_listening()
        level_timer.start()
        QTimer.singleShot(int(LISTENING_SECONDS * 1000), start_processing)

    def start_processing() -> None:
        level_timer.stop()
        pill.enter_processing()
        QTimer.singleShot(int(PROCESSING_SECONDS * 1000), start_outcome)

    def start_outcome() -> None:
        cycle_count["n"] += 1
        hold = int(SUCCESS_SECONDS * 1000)
        if cycle_count["n"] % 2 == 0:
            pill.enter_error(hold_ms=hold)
        else:
            pill.enter_success(hold_ms=hold)
        QTimer.singleShot(hold + 50, start_idle)
        if not loop:
            QTimer.singleShot(hold + int(IDLE_SECONDS * 1000) + 100, app.quit)

    QTimer.singleShot(300, start_idle)
    QTimer.singleShot(300 + int(IDLE_SECONDS * 1000), run_cycle)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
