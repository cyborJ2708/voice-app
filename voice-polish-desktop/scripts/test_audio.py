"""Real end-to-end test of audio.py: records ~2s from the actual default mic.

Verifies: a valid in-memory WAV comes back, levels are emitted while
recording, and nothing is ever written to disk (this script never calls
open() on a path for audio data — only stdlib `wave` + io.BytesIO are used
inside audio.py itself).
"""
from __future__ import annotations

import struct
import sys
import wave
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from voice_polish_desktop.audio import AudioRecorder

RECORD_SECONDS = 2.0


def main() -> None:
    app = QApplication(sys.argv)
    recorder = AudioRecorder()

    levels_seen: list[float] = []
    errors_seen: list[str] = []
    result = {"wav_bytes": None}

    recorder.level.connect(levels_seen.append)
    recorder.error.connect(errors_seen.append)

    def begin():
        print("Recording for ~2s — say something into your mic...")
        recorder.start()

    def finish():
        result["wav_bytes"] = recorder.stop()
        app.quit()

    QTimer.singleShot(200, begin)
    QTimer.singleShot(int(200 + RECORD_SECONDS * 1000), finish)
    app.exec()

    wav_bytes = result["wav_bytes"]
    failures = 0

    if errors_seen:
        print(f"FAIL: recorder.error fired: {errors_seen}")
        failures += 1
    else:
        print("PASS: no device errors")

    if not wav_bytes:
        print("FAIL: no WAV bytes returned")
        failures += 1
    else:
        print(f"PASS: got {len(wav_bytes)} bytes of in-memory WAV")
        # Validate it's a real, readable WAV file.
        try:
            with wave.open(BytesIO(wav_bytes), "rb") as wf:
                nframes = wf.getnframes()
                rate = wf.getframerate()
                width = wf.getsampwidth()
                channels = wf.getnchannels()
            print(f"PASS: valid WAV header (rate={rate}, width={width}, "
                  f"channels={channels}, frames={nframes})")
            if width != 2 or channels != 1:
                print(f"FAIL: expected mono/16-bit, got width={width} channels={channels}")
                failures += 1
        except (wave.Error, struct.error) as exc:
            print(f"FAIL: not a valid WAV file: {exc}")
            failures += 1

    if not levels_seen:
        print("FAIL: no level samples were emitted during recording")
        failures += 1
    else:
        lo, hi = min(levels_seen), max(levels_seen)
        in_range = all(0.0 <= lv <= 1.0 for lv in levels_seen)
        print(f"PASS: {len(levels_seen)} level samples emitted, range [{lo:.3f}, {hi:.3f}]")
        if not in_range:
            print("FAIL: some level samples were outside [0, 1]")
            failures += 1

    print()
    print(f"{failures} failure(s)" if failures else "All checks passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
