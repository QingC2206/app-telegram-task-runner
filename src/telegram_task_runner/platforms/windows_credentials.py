"""Windows-only DPAPI adapter. Adapted from app-codex-telegram-bridge (MIT)."""
from __future__ import annotations
import base64
import ctypes
from ctypes import wintypes
import os


class Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(value: bytes, decrypt=False):
    if os.name != 'nt':
        raise RuntimeError('当前凭据实现需要 Windows DPAPI。')
    buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
    source = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    api = ctypes.WinDLL('crypt32', use_last_error=True)
    function = api.CryptUnprotectData if decrypt else api.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    label = None if decrypt else ctypes.cast(ctypes.c_wchar_p('Telegram Task Runner'), ctypes.c_void_p)
    if not function(ctypes.byref(source), label, None, None, None, 1, ctypes.byref(output)):
        raise ctypes.WinError(ctypes.get_last_error())
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(ctypes.cast(output.data, ctypes.c_void_p))


def protect(token: str) -> str:
    return base64.b64encode(_crypt(token.encode('utf-8'))).decode('ascii')


def unprotect(value: str) -> str:
    return _crypt(base64.b64decode(value, validate=True), decrypt=True).decode('utf-8')
