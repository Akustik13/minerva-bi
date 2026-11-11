"""Read the RAG's stored cloud keys (D:\\RAG\\provider-secrets.dpapi): per-user Windows DPAPI, JSON {name: key}.

The same store and the same decrypt call as rag_secrets.py on the RAG PC, copied so the bench does not import the
RAG's modules. Read-only: keys are saved in «RAG — керування» (tab «🎙 Наради» or «Налаштування»). Only processes of
the same Windows user can decrypt the file. Never log or return a key to the page.
"""
import ctypes
import json
import sys
from pathlib import Path

DEFAULT_FILE = r"D:\RAG\provider-secrets.dpapi"


class _Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _unprotect(data):
    buffer = ctypes.create_string_buffer(data, len(data))
    src = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    dst = _Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    if not crypt32.CryptUnprotectData(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(dst)):
        raise OSError("не вдалося розшифрувати збережені ключі цього користувача Windows")
    try:
        return ctypes.string_at(dst.data, dst.size)
    finally:
        free = ctypes.WinDLL("kernel32").LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(ctypes.cast(dst.data, ctypes.c_void_p))


def read_all(path=None):
    """{name: key}; {} when there is no store (or not on Windows). Raises OSError / ValueError when unreadable."""
    p = Path(path or DEFAULT_FILE)
    if sys.platform != "win32" or not p.exists():
        return {}
    data = json.loads(_unprotect(p.read_bytes()))
    return data if isinstance(data, dict) else {}


def get(name, path=None):
    """The key or '' (missing or unreadable store)."""
    try:
        return str(read_all(path).get(name) or "").strip()
    except (OSError, ValueError):
        return ""
