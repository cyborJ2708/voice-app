r"""Renders each capsule state to a PNG for visual review — no mic, no
hotkey, no backend. Uses QWidget.grab() to capture the real widget exactly
as Qt paints it.

Usage:
    ..\.venv\Scripts\python.exe capture_capsule_states.py <output_dir>
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from voice_polish_desktop.overlay import PillOverlay


def main() -> None:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    out_dir.mkdir(parents=True, exist_ok=True)

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    pill = PillOverlay()

    strong_timer = QTimer()
    strong_timer.setInterval(16)
    strong_timer.timeout.connect(lambda: pill.push_level(1.0))

    def grab(name: str) -> None:
        pixmap = pill.grab()
        path = out_dir / f"{name}.png"
        pixmap.save(str(path))
        print(f"saved {path}")

    def step_idle() -> None:
        pill.enter_idle()
        QTimer.singleShot(500, lambda: grab("0_idle"))
        QTimer.singleShot(600, step_listening)

    def step_listening() -> None:
        pill.enter_listening()

        def hold_strong_level() -> None:
            strong_timer.start()

        QTimer.singleShot(280, hold_strong_level)
        QTimer.singleShot(900, lambda: (strong_timer.stop(), grab("1_listening")))
        QTimer.singleShot(1000, step_processing)

    def step_processing() -> None:
        pill.enter_processing()
        QTimer.singleShot(400, lambda: grab("2_processing"))
        QTimer.singleShot(500, step_success)

    def step_success() -> None:
        pill.enter_success(hold_ms=3000)
        QTimer.singleShot(400, lambda: grab("3_success"))
        QTimer.singleShot(500, step_error)

    def step_error() -> None:
        pill.enter_error(hold_ms=3000)
        QTimer.singleShot(400, lambda: grab("4_error"))
        QTimer.singleShot(500, step_card)

    def step_card() -> None:
        pill.enter_card(
            "Thanks so much for helping me out yesterday — I really appreciate it "
            "and I'll make sure to return the favor soon.",
            subtle_label="Pasted — or copy here",
        )
        QTimer.singleShot(400, lambda: grab("5_card"))
        QTimer.singleShot(500, step_processing_to_idle)

    def step_processing_to_idle() -> None:
        # Regression check: does the processing "3 dots" indicator leave any
        # stale pixels behind once we return to idle immediately after?
        pill.enter_processing()
        QTimer.singleShot(300, lambda: grab("6_processing_before_idle"))
        QTimer.singleShot(320, pill.enter_idle)
        QTimer.singleShot(400, lambda: grab("7_idle_right_after_processing"))
        QTimer.singleShot(600, app.quit)

    QTimer.singleShot(300, step_idle)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
