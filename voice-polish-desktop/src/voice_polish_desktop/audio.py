"""In-memory microphone capture — audio is never written to disk.

sounddevice's InputStream callback runs on PortAudio's own thread, not a
Qt thread. Emitting a Qt Signal from there is safe: the connection to any
receiver living on the Qt main thread automatically resolves to a queued
connection, so the actual slot call happens back on the main thread.
"""
from __future__ import annotations

import io
import wave

import numpy as np
import sounddevice as sd
from PySide6.QtCore import QObject, Signal

PREFERRED_SAMPLERATE = 16000
CHANNELS = 1
DTYPE = "int16"
LEVEL_GAIN = 5.0  # empirical: typical speech RMS at normal mic gain is ~0.02-0.15


class AudioRecorder(QObject):
    level = Signal(float)  # 0..1 RMS amplitude, emitted while recording
    error = Signal(str)    # human-readable device error

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._stream: sd.InputStream | None = None
        self._chunks: list[bytes] = []
        self._samplerate: int = PREFERRED_SAMPLERATE

    def is_recording(self) -> bool:
        return self._stream is not None

    def start(self) -> None:
        if self._stream is not None:
            return
        self._chunks = []

        for samplerate in self._candidate_samplerates():
            try:
                stream = sd.InputStream(
                    samplerate=samplerate,
                    channels=CHANNELS,
                    dtype=DTYPE,
                    callback=self._on_audio_block,
                )
                stream.start()
            except (sd.PortAudioError, ValueError):
                continue
            self._stream = stream
            self._samplerate = samplerate
            return

        self.error.emit("Couldn't open a microphone input stream.")

    def stop(self) -> bytes:
        if self._stream is None:
            return b""
        stream = self._stream
        self._stream = None
        try:
            stream.stop()
            stream.close()
        except sd.PortAudioError:
            pass

        if not self._chunks:
            return b""
        pcm = b"".join(self._chunks)
        self._chunks = []
        return self._to_wav_bytes(pcm, self._samplerate)

    def _candidate_samplerates(self) -> list[int]:
        rates = [PREFERRED_SAMPLERATE]
        try:
            default_rate = int(sd.query_devices(kind="input")["default_samplerate"])
            if default_rate not in rates:
                rates.append(default_rate)
        except (sd.PortAudioError, TypeError, KeyError, ValueError):
            pass
        return rates

    def _on_audio_block(self, indata, frames, time_info, status) -> None:
        # `status` flags xruns etc. — not fatal, and never contains audio
        # content, so no logging concern either way; simply ignored.
        self._chunks.append(bytes(indata))
        samples = indata.astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(np.square(samples)))) if samples.size else 0.0
        self.level.emit(min(1.0, rms * LEVEL_GAIN))

    @staticmethod
    def _to_wav_bytes(pcm: bytes, samplerate: int) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)  # int16
            wf.setframerate(samplerate)
            wf.writeframes(pcm)
        return buf.getvalue()
