"""Make the pip-installed cuBLAS 12 DLLs visible to CTranslate2 on Windows.

The ctranslate2 wheel loads cublas64_12.dll / cublasLt64_12.dll from the DLL search path;
nvidia-cublas-cu12 puts them in site-packages/nvidia/cublas/bin. Call before importing
faster_whisper / ctranslate2.
"""
import os
import sys
from pathlib import Path

_done = False


def setup():
    global _done
    if _done or sys.platform != "win32":
        return
    _done = True
    for base in map(Path, sys.path):
        nvidia = base / "nvidia"
        if not nvidia.is_dir():
            continue
        for bin_dir in nvidia.glob("*/bin"):
            os.add_dll_directory(str(bin_dir))
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
