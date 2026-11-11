"""Self-check on the RAG PC: python -m bench.check [--root D:\\Meeting] [--gpu-test]

Prints what is installed and whether CUDA/cuBLAS really work for CTranslate2 on this card.
"""
import argparse
import os
import time
from pathlib import Path

import numpy as np

from . import VERSION, config, cuda, gpu


def main():
    p = argparse.ArgumentParser(prog="bench.check")
    p.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    p.add_argument("--gpu-test", action="store_true", help="load the fast Whisper model on the GPU once")
    args = p.parse_args()
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    cfg = config.load(args.root)
    print(f"bench {VERSION}, root {cfg.root}")
    cuda.setup()
    import ctranslate2
    import sherpa_onnx

    print("ctranslate2", ctranslate2.__version__, "| sherpa-onnx", sherpa_onnx.__version__)
    n = ctranslate2.get_cuda_device_count()
    print("CUDA devices:", n)
    if n:
        print("compute types on cuda:", sorted(ctranslate2.get_supported_compute_types("cuda")))
    print("GPU:", gpu.memory())
    print("Ollama loaded:", gpu.ollama_loaded(cfg["ollama_url"]))
    print("Parakeet:", (cfg.parakeet_dir / "tokens.txt").exists(), "| VAD:", cfg.vad_model.exists())
    from .server import _whisper_ready

    for key, name in cfg["whisper_models"].items():
        print(f"Whisper {key}: {name} ->", "ready" if _whisper_ready(cfg, name) else "MISSING")
    if args.gpu_test and n:
        from faster_whisper import WhisperModel

        name = cfg["whisper_models"]["fast"]
        before = gpu.memory()
        t0 = time.time()
        model = WhisperModel(name, device="cuda", compute_type=cfg["whisper_compute_type"],
                             download_root=str(cfg.whisper_dir), local_files_only=True)
        load = time.time() - t0
        t1 = time.time()
        segments, info = model.transcribe(np.zeros(16000 * 5, dtype=np.float32), beam_size=1, language="en")
        list(segments)
        after = gpu.memory()
        print(f"GPU test {name}: compute {model.model.compute_type}, load {load:.1f} s, 5 s of silence in "
              f"{time.time() - t1:.1f} s, VRAM used {before and before['used']} -> {after and after['used']} MB")


if __name__ == "__main__":
    main()
