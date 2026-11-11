"""Recognisers for live captions: one short speech segment in, text out.

parakeet - NVIDIA Parakeet-TDT-0.6B-v3 int8 via sherpa-onnx on the CPU (25 European languages,
           including uk/de/en/ru; the language cannot be set, it is detected by the model);
whisper  - faster-whisper on the GPU (language per channel can be fixed).
"""
import threading
import time

import numpy as np

SAMPLE_RATE = 16000

# Whisper's well-known "phantom" lines on near-silent input
HALLUCINATIONS = {
    "thank you.", "thanks for watching!", "дякую за перегляд!", "субтитры сделал dimatorzok",
    "продолжение следует...", "untertitel im auftrag des zdf, 2017", "untertitel der amara.org-community",
}


class ParakeetEngine:
    name = "parakeet"
    device = "cpu"

    def __init__(self, cfg):
        import sherpa_onnx

        d = cfg.parakeet_dir
        t0 = time.time()
        self.recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(d / "encoder.int8.onnx"),
            decoder=str(d / "decoder.int8.onnx"),
            joiner=str(d / "joiner.int8.onnx"),
            tokens=str(d / "tokens.txt"),
            num_threads=int(cfg["live_threads"]),
            model_type="nemo_transducer",
            provider="cpu",
        )
        self.load_s = round(time.time() - t0, 1)
        self.lock = threading.Lock()

    def transcribe(self, samples, language=None):
        with self.lock:
            stream = self.recognizer.create_stream()
            stream.accept_waveform(SAMPLE_RATE, np.asarray(samples, dtype=np.float32))
            self.recognizer.decode_stream(stream)
            return stream.result.text.strip(), None

    def close(self):
        self.recognizer = None


class WhisperEngine:
    name = "whisper"
    device = "cuda"

    def __init__(self, cfg, device="cuda"):
        from . import cuda

        cuda.setup()
        from faster_whisper import WhisperModel

        t0 = time.time()
        self.device = device
        self.model = WhisperModel(
            cfg["whisper_live_model"], device=device,
            compute_type=cfg["whisper_compute_type"], cpu_threads=int(cfg["cpu_threads"]),
            download_root=str(cfg.whisper_dir), local_files_only=True,
        )
        self.load_s = round(time.time() - t0, 1)
        self.lock = threading.Lock()

    def transcribe(self, samples, language=None):
        with self.lock:
            segments, info = self.model.transcribe(
                np.asarray(samples, dtype=np.float32), language=language or None, beam_size=1,
                vad_filter=False, condition_on_previous_text=False, without_timestamps=True,
            )
            parts = []
            for seg in segments:
                text = seg.text.strip()
                if seg.no_speech_prob > 0.6 and seg.avg_logprob < -1.0:
                    continue
                if text.lower() in HALLUCINATIONS:
                    continue
                parts.append(text)
            return " ".join(parts).strip(), info.language

    def close(self):
        self.model = None
