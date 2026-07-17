"""Real end-to-end test of backend_client.py against the actual local backend.

Records ~1.5s from the mic, sends it through BackendClient.polish() (real
HTTP call, real auth token from env, real Gemini round-trip), and prints
what came back. Requires: the backend running locally (see main.py) and
APP_AUTH_TOKEN set in this process's environment to match the server's.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from voice_polish_desktop.audio import AudioRecorder
from voice_polish_desktop.backend_client import BackendClient, BackendError

BASE_URL = "http://localhost:8000"
RECORD_SECONDS = 1.5


def main() -> None:
    app = QApplication(sys.argv)
    recorder = AudioRecorder()
    client = BackendClient(BASE_URL, os.environ.get("APP_AUTH_TOKEN", ""))

    def begin():
        print("Recording — say something...")
        recorder.start()

    def finish():
        wav_bytes = recorder.stop()
        print(f"Captured {len(wav_bytes)} bytes, calling backend...")
        try:
            text = client.polish(wav_bytes)
            print(f"PASS: backend returned: {text!r}")
        except BackendError as exc:
            print(f"FAIL: {type(exc).__name__}: {exc}")
        app.quit()

    QTimer.singleShot(200, begin)
    QTimer.singleShot(int(200 + RECORD_SECONDS * 1000), finish)
    app.exec()


if __name__ == "__main__":
    main()
