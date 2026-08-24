"""Windows DPAPI protection for the user supplied model key."""

import base64
import ctypes
import sys

_ENTROPY = b"job-application-orchestrator:model-key:v1"
CRYPTPROTECT_UI_FORBIDDEN = 1


def encrypt_secret(value: str) -> str:
    if sys.platform != "win32":
        raise RuntimeError("Безопасное хранение ключа доступно только в Windows")
    return _protect(value.encode("utf-8"))


def decrypt_secret(value: str) -> str:
    if sys.platform != "win32":
        raise RuntimeError("Безопасное хранение ключа доступно только в Windows")
    return _unprotect(value).decode("utf-8")


def _protect(data: bytes) -> str:
    class Blob(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    raw = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    entropy = (ctypes.c_ubyte * len(_ENTROPY)).from_buffer_copy(_ENTROPY)
    inp, ent, out = Blob(len(data), raw), Blob(len(_ENTROPY), entropy), Blob()
    api = ctypes.windll.crypt32.CryptProtectData
    api.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.c_wchar_p,
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(Blob),
    ]
    api.restype = ctypes.c_int
    if not api(
        ctypes.byref(inp),
        None,
        ctypes.byref(ent),
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out),
    ):
        raise RuntimeError("Не удалось защитить ключ Windows DPAPI")
    try:
        return base64.b64encode(ctypes.string_at(out.pbData, out.cbData)).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _unprotect(encoded: str) -> bytes:
    class Blob(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    if not encoded:
        raise ValueError("Пустой шифротекст")
    try:
        data = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise ValueError("Повреждённый шифротекст") from exc
    raw = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    entropy = (ctypes.c_ubyte * len(_ENTROPY)).from_buffer_copy(_ENTROPY)
    inp, ent, out = Blob(len(data), raw), Blob(len(_ENTROPY), entropy), Blob()
    api = ctypes.windll.crypt32.CryptUnprotectData
    api.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.POINTER(ctypes.c_wchar_p),
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(Blob),
    ]
    api.restype = ctypes.c_int
    if not api(
        ctypes.byref(inp),
        None,
        ctypes.byref(ent),
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out),
    ):
        raise RuntimeError("Не удалось расшифровать ключ Windows DPAPI")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)
